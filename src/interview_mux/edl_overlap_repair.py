"""Merge overlapping same-speaker EDL speech into one survivor span.

When two or more speech clips share intersecting source ranges, play the union
once under a surviving segment id (earliest child, or parent if the parent is
in the component). Remap consumed ids with the shared fuse walker. Do not mint
a new canonical ``seg_*``.
"""
from __future__ import annotations

from typing import Any

from interview_mux.run_context import RunContext

STAGE_KEY = "edl_overlap_repair"


def _as_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _source_span(clip: dict[str, Any]) -> tuple[int, int] | None:
    ss = _as_int(clip.get("source_start_ms"))
    se = _as_int(clip.get("source_end_ms"), ss)
    if se <= ss:
        return None
    return ss, se


def _ranges_overlap(a: tuple[int, int], b: tuple[int, int]) -> bool:
    return a[0] < b[1] and b[0] < a[1]


def _speaker_of(seg: dict[str, Any] | None) -> str:
    if not isinstance(seg, dict):
        return ""
    return str(seg.get("speaker_id") or seg.get("speaker") or "").strip()


def _parent_id(
    sid: str,
    segs: dict[str, dict[str, Any]],
    overrides: dict[str, Any],
) -> str:
    ov = overrides.get(sid) if isinstance(overrides.get(sid), dict) else {}
    pid = str((ov or {}).get("parent_id") or "").strip()
    if pid:
        return pid
    row = segs.get(sid) or {}
    return str(row.get("parent_id") or "").strip()


def _chapter_by_segment(ctx: RunContext) -> dict[str, str]:
    out: dict[str, str] = {}
    if not ctx.artifact_exists("master/selection.json"):
        return out
    try:
        selection = ctx.read_json("master/selection.json")
    except Exception:
        return out
    if not isinstance(selection, dict):
        return out
    for ch in selection.get("chapters") or []:
        if not isinstance(ch, dict):
            continue
        cid = str(ch.get("chapter_id") or "").strip()
        if not cid:
            continue
        for sid in ch.get("segment_ids") or []:
            if sid:
                # First chapter wins, as in relabel_chapters_contiguous; the last
                # chapter winning let a doubly-claimed id union across chapters.
                out.setdefault(str(sid), cid)
    return out


def _load_seg_lookup(ctx: RunContext) -> dict[str, dict[str, Any]]:
    from interview_mux.nle_state import segments_by_id_with_nle

    try:
        return segments_by_id_with_nle(ctx)
    except Exception:
        pass
    if not ctx.artifact_exists("segments/manifest.json"):
        return {}
    try:
        manifest = ctx.read_json("segments/manifest.json")
    except Exception:
        return {}
    out: dict[str, dict[str, Any]] = {}
    for row in (manifest.get("segments") or []) if isinstance(manifest, dict) else []:
        if isinstance(row, dict) and row.get("segment_id"):
            out[str(row["segment_id"])] = row
    return out


def _load_words(ctx: RunContext) -> list[dict[str, Any]]:
    from interview_mux.segment_fuse import _load_words as fuse_words

    try:
        return fuse_words(ctx)
    except Exception:
        return []


def _union_text(
    words: list[dict[str, Any]],
    start_ms: int,
    end_ms: int,
    members: list[dict[str, Any]],
) -> str:
    from interview_mux.segment_fuse import _rebuild_text

    fallback = " ".join(
        str(row.get("text") or "").strip() for row in members if str(row.get("text") or "").strip()
    )
    try:
        return _rebuild_text(words, start_ms, end_ms, fallback=fallback)
    except Exception:
        return fallback.strip()


def _union_find_components(
    items: list[tuple[int, dict[str, Any], tuple[int, int]]],
    *,
    segs: dict[str, dict[str, Any]],
    chapters: dict[str, str],
) -> list[list[int]]:
    n = len(items)
    parent = list(range(n))

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    def union(a: int, b: int) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[rb] = ra

    for i in range(n):
        _, clip_i, span_i = items[i]
        sid_i = str(clip_i.get("segment_id") or "")
        spk_i = _speaker_of(segs.get(sid_i))
        ch_i = chapters.get(sid_i, "")
        for j in range(i + 1, n):
            _, clip_j, span_j = items[j]
            if not _ranges_overlap(span_i, span_j):
                continue
            sid_j = str(clip_j.get("segment_id") or "")
            spk_j = _speaker_of(segs.get(sid_j))
            if spk_i and spk_j and spk_i != spk_j:
                continue
            ch_j = chapters.get(sid_j, "")
            if ch_i and ch_j and ch_i != ch_j:
                continue
            union(i, j)

    buckets: dict[int, list[int]] = {}
    for i in range(n):
        buckets.setdefault(find(i), []).append(i)
    return [idxs for idxs in buckets.values() if len(idxs) >= 2]


def _pick_survivor(
    member_sids: list[str],
    *,
    clip_by_sid: dict[str, dict[str, Any]],
    segs: dict[str, dict[str, Any]],
    overrides: dict[str, Any],
    ordered: list[str],
) -> str:
    order_index = {sid: i for i, sid in enumerate(ordered)}

    def sort_key(sid: str) -> tuple[int, int, int, str]:
        clip = clip_by_sid.get(sid) or {}
        seg = segs.get(sid) or {}
        span = _source_span(clip)
        ss = span[0] if span else _as_int(seg.get("start_ms"))
        ts = _as_int(clip.get("timeline_start_ms"), 10**12)
        return (ss, ts, order_index.get(sid, 10**12), sid)

    in_component_parents = [
        sid
        for sid in member_sids
        if sid in { _parent_id(m, segs, overrides) for m in member_sids }
    ]
    if in_component_parents:
        return min(in_component_parents, key=sort_key)
    return min(member_sids, key=sort_key)


def _reorder_speech_blocks(clips: list[Any], order: list[str]) -> list[Any] | None:
    """Clips with speech blocks in ``order``; None when nothing needs to move.

    A block is a speech clip with the non-speech clips just before it (its
    before-VO, silences, transition) and, for the last block, the trailing
    clips. Only a pure reordering is applied: the speech ids must be the same
    set as ``order``.
    """
    speech_idx = [
        i for i, c in enumerate(clips) if isinstance(c, dict) and str(c.get("type") or "") == "speech"
    ]
    speech_ids = [str(clips[i].get("segment_id") or "") for i in speech_idx]
    if not order or speech_ids == order or sorted(speech_ids) != sorted(order):
        return None
    if len(set(speech_ids)) != len(speech_ids):
        return None
    head = list(clips[: speech_idx[0]])
    blocks: dict[str, list[Any]] = {}
    start = speech_idx[0]
    for n, i in enumerate(speech_idx):
        sid = speech_ids[n]
        nxt = speech_idx[n + 1] if n + 1 < len(speech_idx) else len(clips)
        # After-VO for this speech clip travels with it; everything else after
        # it belongs to the next block's lead-in.
        end = i + 1
        while end < nxt:
            c = clips[end]
            if (
                isinstance(c, dict)
                and str(c.get("type") or "") == "vo_pickup"
                and str(c.get("placement") or "").lower() == "after"
                and str(c.get("targets_segment_id") or "") == sid
            ):
                end += 1
                continue
            break
        blocks[sid] = list(clips[start:end])
        start = end
    tail = list(clips[start:])
    out = list(head)
    for sid in order:
        out.extend(blocks[sid])
    out.extend(tail)
    return out


def _drop_non_adjacent_transitions(clips: list[Any]) -> list[Any]:
    speech = [
        str(c.get("segment_id") or "")
        for c in clips
        if isinstance(c, dict) and str(c.get("type") or "") == "speech"
    ]
    adjacent = {(speech[i], speech[i + 1]) for i in range(len(speech) - 1)}
    out: list[Any] = []
    for c in clips:
        if isinstance(c, dict) and str(c.get("type") or "") == "transition":
            pair = (str(c.get("after_segment_id") or ""), str(c.get("before_segment_id") or ""))
            if all(pair) and pair not in adjacent:
                continue
        out.append(c)
    return out


def _retime_clips(clips: list[Any]) -> int:
    cursor = 0
    max_end = 0
    for clip in clips:
        if not isinstance(clip, dict):
            continue
        dur = max(0, _as_int(clip.get("duration_ms")))
        overlap = max(0, _as_int(clip.get("mix_overlap_ms")))
        start = max(0, cursor - overlap) if overlap else cursor
        clip["timeline_start_ms"] = start
        end = start + dur
        max_end = max(max_end, end)
        if dur > 0:
            cursor = end
    return max_end


def _rebuild_clips(
    clips: list[Any],
    *,
    members: set[str],
    survivor: str,
    union_start: int,
    union_end: int,
) -> list[Any]:
    """Seat the union at the survivor's own air slot; drop consumed clips in place.

    ``retire_consumed_ids_from_selection`` keeps the survivor where it sits in
    the selection, so the EDL must too. Seating it at the first member's slot
    moved it ahead of a segment airing between the members (exec_019:
    seg_019 -> seg_017 put seg_017 before seg_018), and EDL narrative QC then
    refused the EDL for not matching the selection (ISSUES 167). VO pickups
    that targeted any member follow the survivor.
    """
    survivor_clip: dict[str, Any] | None = None
    before_vo: list[dict[str, Any]] = []
    after_vo: list[dict[str, Any]] = []
    for clip in clips:
        if not isinstance(clip, dict):
            continue
        ctype = str(clip.get("type") or "")
        sid = str(clip.get("segment_id") or "")
        if ctype == "speech" and sid == survivor and survivor_clip is None:
            row = dict(clip)
            row["segment_id"] = survivor
            row["source_start_ms"] = union_start
            row["source_end_ms"] = union_end
            row["duration_ms"] = max(0, union_end - union_start)
            survivor_clip = row
        elif ctype == "vo_pickup" and str(clip.get("targets_segment_id") or "") in members:
            if str(clip.get("placement") or "").lower() == "after":
                after_vo.append(dict(clip))
            else:
                before_vo.append(dict(clip))
    if survivor_clip is None:
        return list(clips)

    out: list[Any] = []
    seated = False
    for clip in clips:
        if not isinstance(clip, dict):
            out.append(clip)
            continue
        ctype = str(clip.get("type") or "")
        sid = str(clip.get("segment_id") or "")
        if ctype == "speech" and sid in members:
            if sid == survivor and not seated:
                out.extend(before_vo)
                out.append(survivor_clip)
                out.extend(after_vo)
                seated = True
            continue
        if ctype == "transition":
            after_id = str(clip.get("after_segment_id") or "")
            before_id = str(clip.get("before_segment_id") or "")
            if after_id in members and before_id in members:
                continue
        if ctype == "vo_pickup" and str(clip.get("targets_segment_id") or "") in members:
            continue
        out.append(clip)
    return out


def _union_manifest_row(
    survivor: dict[str, Any],
    consumed_rows: list[dict[str, Any]],
    *,
    union_start: int,
    union_end: int,
    consumed_ids: list[str],
    words: list[dict[str, Any]],
) -> dict[str, Any]:
    out = dict(survivor)
    members = [survivor, *consumed_rows]
    out["start_ms"] = union_start
    out["end_ms"] = union_end
    out["duration_ms"] = max(0, union_end - union_start)
    out["text"] = _union_text(words, union_start, union_end, members)
    tags: list[str] = []
    for row in members:
        for tag in row.get("topic_tags") or []:
            token = str(tag).strip()
            if token and token not in tags:
                tags.append(token)
    if tags:
        out["topic_tags"] = tags
    fused = list(dict.fromkeys([*(survivor.get("fused_from") or [survivor.get("segment_id")]), *consumed_ids]))
    out["fused_from"] = [str(x) for x in fused if x]
    out["fuse_reason"] = "overlapping_source_range"
    out["fuse_pass_id"] = STAGE_KEY
    if any(str(row.get("retention") or "") == "must_keep" for row in members):
        out["retention"] = "must_keep"
    if any(row.get("high_value_speech") for row in members):
        out["high_value_speech"] = True
    return out


def _update_manifest(
    ctx: RunContext,
    *,
    survivor: str,
    consumed: list[str],
    union_start: int,
    union_end: int,
    words: list[dict[str, Any]],
) -> None:
    if not ctx.artifact_exists("segments/manifest.json"):
        return
    try:
        manifest = ctx.read_json("segments/manifest.json")
    except Exception:
        return
    if not isinstance(manifest, dict):
        return
    segs = [s for s in (manifest.get("segments") or []) if isinstance(s, dict)]
    by_id = {str(s.get("segment_id") or ""): s for s in segs if s.get("segment_id")}
    survivor_row = by_id.get(survivor)
    if survivor_row is None:
        survivor_row = {
            "segment_id": survivor,
            "start_ms": union_start,
            "end_ms": union_end,
            "speaker_id": "unknown",
            "speaker_role": "unknown",
            "type": "interviewee_answer",
            "text": "",
            "topic_tags": [],
        }
    consumed_rows = [by_id[cid] for cid in consumed if cid in by_id]
    merged = _union_manifest_row(
        survivor_row,
        consumed_rows,
        union_start=union_start,
        union_end=union_end,
        consumed_ids=consumed,
        words=words,
    )
    drop = set(consumed)
    next_segs: list[dict[str, Any]] = []
    wrote_survivor = False
    for row in segs:
        sid = str(row.get("segment_id") or "")
        if sid in drop:
            continue
        if sid == survivor:
            next_segs.append(merged)
            wrote_survivor = True
            continue
        next_segs.append(row)
    if not wrote_survivor:
        next_segs.append(merged)
    manifest = dict(manifest)
    manifest["segments"] = next_segs
    ctx.write_json("segments/manifest.json", manifest, stage_key=STAGE_KEY)
    try:
        from interview_mux.asset_transcripts import sync_speech_sidecars

        sync_speech_sidecars(ctx)
    except Exception:
        pass


def _update_boundaries(
    ctx: RunContext,
    *,
    survivor: str,
    consumed: set[str],
    union_start: int,
    union_end: int,
    fused_from: list[str],
) -> None:
    if not ctx.artifact_exists("segments/boundaries.json"):
        return
    try:
        doc = ctx.read_json("segments/boundaries.json")
    except Exception:
        return
    if not isinstance(doc, dict):
        return
    rows: list[dict[str, Any]] = []
    saw_survivor = False
    for row in doc.get("boundaries") or []:
        if not isinstance(row, dict):
            continue
        sid = str(row.get("segment_id") or "")
        if sid in consumed:
            continue
        merged = dict(row)
        if sid == survivor:
            merged["start_ms"] = union_start
            merged["end_ms"] = union_end
            merged["fused_from"] = fused_from
            merged["fuse_pass_id"] = STAGE_KEY
            saw_survivor = True
        if not str(merged.get("proposed_split_reason") or "").strip():
            merged["proposed_split_reason"] = "topic_shift"
        rows.append(merged)
    if not saw_survivor:
        rows.append(
            {
                "segment_id": survivor,
                "start_ms": union_start,
                "end_ms": union_end,
                "fused_from": fused_from,
                "fuse_pass_id": STAGE_KEY,
                "proposed_split_reason": "topic_shift",
            }
        )
    rows.sort(key=lambda r: _as_int(r.get("start_ms")))
    out = dict(doc)
    out["boundaries"] = rows
    from interview_mux.shared_path_commit import commit_boundaries_doc

    commit_boundaries_doc(
        ctx,
        out,
        stage_key=STAGE_KEY,
        claim_producer=True,
        skip_handoff=True,
    )


def _update_nle(
    ctx: RunContext,
    *,
    survivor: str,
    consumed: list[str],
    union_start: int,
    union_end: int,
    segs: dict[str, dict[str, Any]],
) -> None:
    from interview_mux.nle_state import load_nle

    nle = load_nle(ctx)
    overrides = dict(nle.get("segment_overrides") or {})
    ov_s = dict(overrides.get(survivor) or {})
    ov_s["start_ms"] = union_start
    ov_s["end_ms"] = union_end
    overrides[survivor] = ov_s
    for cid in consumed:
        ov_c = dict(overrides.get(cid) or {})
        ov_c["excluded"] = True
        ov_c["exclude_reason"] = STAGE_KEY
        overrides[cid] = ov_c
    parent = _parent_id(survivor, segs, overrides)
    if not parent:
        for cid in consumed:
            parent = _parent_id(cid, segs, overrides)
            if parent:
                break
    if parent:
        ov_p = dict(overrides.get(parent) or {})
        kids = [str(x) for x in (ov_p.get("split_into") or []) if x]
        next_kids: list[str] = []
        for kid in kids:
            mapped = survivor if kid in consumed or kid == survivor else kid
            if mapped not in next_kids:
                next_kids.append(mapped)
        if next_kids:
            ov_p["split_into"] = next_kids
            overrides[parent] = ov_p
    order = [str(x) for x in (nle.get("sequence_order") or []) if x]
    if order:
        seen: set[str] = set()
        next_order: list[str] = []
        drop = set(consumed)
        for sid in order:
            mapped = survivor if sid in drop else sid
            if mapped in seen:
                continue
            seen.add(mapped)
            next_order.append(mapped)
        nle["sequence_order"] = next_order
    nle["segment_overrides"] = overrides
    from interview_mux.removal_authority import refuse_nle_excludes

    nle = refuse_nle_excludes(ctx, nle, producer=STAGE_KEY)
    # A remap stage persisting a remap path must name the integrity-only
    # mutation class, or ownership refuses the write on every overlap union
    # (ISSUES 113: exec_009's first EDL attempt died here).
    ctx.write_json(
        "segments/nle_edits.json",
        nle,
        skip_handoff=True,
        stage_key=STAGE_KEY,
        mutation_class="segment_id_remap",
    )


def _drop_self_transitions(ctx: RunContext) -> None:
    if not ctx.artifact_exists("master/transitions.json"):
        return
    try:
        doc = ctx.read_json("master/transitions.json")
    except Exception:
        return
    if not isinstance(doc, dict):
        return
    changed = False
    for key in ("transitions", "pairs"):
        rows = doc.get(key)
        if not isinstance(rows, list):
            continue
        kept: list[Any] = []
        for row in rows:
            if not isinstance(row, dict):
                kept.append(row)
                continue
            after_id = str(row.get("after_segment_id") or row.get("after_id") or "")
            before_id = str(row.get("before_segment_id") or row.get("before_id") or "")
            if after_id and before_id and after_id == before_id:
                changed = True
                continue
            kept.append(row)
        doc[key] = kept
    if changed:
        ctx.write_json(
            "master/transitions.json",
            doc,
            skip_handoff=True,
            stage_key=STAGE_KEY,
            mutation_class="segment_id_remap",
        )


# Share of a retired id's tape that must stay on air for the retire to be a union.
CONSUMED_COVERAGE = 0.9


def _covered_by_on_air_span(ctx: RunContext, overrides: dict[str, Any]) -> set[str]:
    """Excluded ids whose manifest span sits inside an on-air segment's span."""
    return set(_on_air_carriers(ctx, overrides))


def _on_air_carriers(
    ctx: RunContext,
    overrides: dict[str, Any],
    *,
    on_air: list[str] | None = None,
) -> dict[str, str]:
    """Map each covered excluded id to the on-air segment that carries its tape.

    ``on_air`` is the proposed order when a write is being judged before it
    lands; the selection on disk otherwise.
    """
    if not ctx.artifact_exists("segments/manifest.json"):
        return {}
    if on_air is None and not ctx.artifact_exists("master/selection.json"):
        return {}
    manifest = ctx.read_json("segments/manifest.json")
    rows = (manifest or {}).get("segments") or [] if isinstance(manifest, dict) else []
    span: dict[str, tuple[int, int]] = {}
    for row in rows:
        if isinstance(row, dict) and row.get("segment_id") and row.get("start_ms") is not None:
            span[str(row["segment_id"])] = (int(row["start_ms"]), int(row["end_ms"]))
    if on_air is None:
        sel = ctx.read_json("master/selection.json")
        on_air = (
            [str(s) for s in ((sel or {}).get("ordered_segment_ids") or []) if s]
            if isinstance(sel, dict)
            else []
        )
    on_air = [str(s) for s in on_air if s]

    def _effective(sid: str) -> tuple[int, int] | None:
        ov = overrides.get(sid) if isinstance(overrides.get(sid), dict) else {}
        base = span.get(sid)
        if ov and "start_ms" in ov and "end_ms" in ov:
            return int(ov["start_ms"]), int(ov["end_ms"])
        return base

    out: dict[str, str] = {}
    for sid, row in overrides.items():
        if not isinstance(row, dict) or not row.get("excluded"):
            continue
        mine = span.get(str(sid))
        if not mine:
            continue
        for other in on_air:
            if other == sid:
                continue
            eff = _effective(other)
            if not eff:
                continue
            length = max(1, mine[1] - mine[0])
            overlap = max(0, min(eff[1], mine[1]) - max(eff[0], mine[0]))
            # Thought-complete recuts move a neighbour over most of the retired
            # tape, not always to the millisecond (exec_052 seg_024 covered 97 %
            # of seg_025). The retire is a union when the tape essentially airs.
            if overlap >= CONSUMED_COVERAGE * length:
                out[str(sid)] = other
                break
    return out


def _overrides_or_disk(ctx: RunContext, overrides: dict[str, Any] | None) -> dict[str, Any]:
    if overrides is not None:
        return overrides if isinstance(overrides, dict) else {}
    try:
        from interview_mux.nle_state import load_nle

        got = load_nle(ctx).get("segment_overrides") or {}
    except Exception:
        return {}
    return got if isinstance(got, dict) else {}


def consumed_carrier_ids(
    ctx: RunContext,
    ids: set[str],
    *,
    overrides: dict[str, Any] | None = None,
    on_air: list[str] | None = None,
) -> set[str]:
    """On-air segments whose span carries the tape of a retired id in ``ids``.

    A hard keep retired as covered (entry 64) airs only while its carrier airs.
    Omitting the carrier later silently drops the keep (exec_055: seg_060
    carried by seg_059, then junction omitted seg_059 as on_a_roll, ISSUES 73).
    """
    ov = _overrides_or_disk(ctx, overrides)
    try:
        carriers = _on_air_carriers(ctx, ov, on_air=on_air)
    except Exception:
        return set()
    return {c for sid, c in carriers.items() if sid in ids}


def consumed_segment_ids(
    ctx: RunContext,
    *,
    overrides: dict[str, Any] | None = None,
    on_air: list[str] | None = None,
) -> set[str]:
    """Ids a fuse / overlap union absorbed into a survivor (no longer on air).

    Sources: NLE overrides stamped by ``_update_nle`` and ``fused_from`` on live
    manifest rows. Such an id is not a creative cut — the survivor's span already
    covers its tape. ``overrides`` / ``on_air`` judge a proposed write before
    it lands (removal authority); disk state otherwise.
    """
    out: set[str] = set()
    overrides = _overrides_or_disk(ctx, overrides)
    if isinstance(overrides, dict):
        for sid, row in overrides.items():
            if not isinstance(row, dict) or not row.get("excluded"):
                continue
            reason = str(row.get("exclude_reason") or "")
            # Junction fuse unions retire the drop id the same way
            # ("junction_snip_qa:<kind>:fuse_<why>"); omits do not count.
            if reason == STAGE_KEY or (
                reason.startswith("junction_snip_qa:") and ":fuse_" in reason
            ):
                out.add(str(sid))
    # Geometric truth, whatever path wrote the retire: an excluded id whose
    # tape lies inside an on-air survivor's effective span still airs
    # (exec_052: seg_059 extended to 3163670 covering all of seg_060, retire
    # stamped "junction_snip_qa:on_a_roll", ISSUES entry 64).
    try:
        out |= set(_on_air_carriers(ctx, overrides, on_air=on_air))
    except Exception:
        pass
    if ctx.artifact_exists("segments/manifest.json"):
        try:
            manifest = ctx.read_json("segments/manifest.json")
        except Exception:
            manifest = None
        rows = (manifest or {}).get("segments") or [] if isinstance(manifest, dict) else []
        live = {str(r.get("segment_id") or "") for r in rows if isinstance(r, dict)}
        for row in rows:
            if not isinstance(row, dict):
                continue
            if str(row.get("fuse_pass_id") or "") != STAGE_KEY:
                continue
            survivor = str(row.get("segment_id") or "")
            for sid in row.get("fused_from") or []:
                token = str(sid or "")
                if token and token != survivor and token not in live:
                    out.add(token)
    return {s for s in out if s}


def retire_consumed_ids_from_selection(ctx: RunContext) -> list[str]:
    """Drop union-consumed ids from selection so it cannot outrun the EDL.

    exec_11871: the merge's selection write was refused by seat freeze, selection
    stayed at n+1 versus the EDL, and every mix / dispatch after that refused with
    ``selection_edl_order_drift`` — with no overlap left on the EDL, the merge
    could never retry.
    """
    if not ctx.artifact_exists("master/selection.json"):
        return []
    consumed = consumed_segment_ids(ctx)
    if not consumed:
        return []
    try:
        selection = ctx.read_json("master/selection.json")
    except Exception:
        return []
    if not isinstance(selection, dict):
        return []
    order = [str(s) for s in (selection.get("ordered_segment_ids") or []) if s]
    retired = [s for s in order if s in consumed]
    if not retired:
        return []
    out = dict(selection)
    out["ordered_segment_ids"] = [s for s in order if s not in consumed]
    excl = list(out.get("excluded_segment_ids") or [])
    have = {
        str(r.get("segment_id") if isinstance(r, dict) else r or "") for r in excl
    }
    for sid in retired:
        if sid not in have:
            excl.append({"segment_id": sid, "reason": f"{STAGE_KEY}:absorbed_by_survivor"})
    out["excluded_segment_ids"] = excl
    for ch in out.get("chapters") or []:
        if isinstance(ch, dict):
            ch["segment_ids"] = [
                str(x) for x in (ch.get("segment_ids") or []) if str(x) not in consumed
            ]
    from interview_mux.air_order_boundary import commit_selection_mutation
    from interview_mux.order_hash import bump_order_lock

    commit_selection_mutation(
        ctx,
        bump_order_lock(out, source=f"{STAGE_KEY}:retire_consumed"),
        producer=STAGE_KEY,
        stage_key=STAGE_KEY,
        checkpoint_mode="detect",
        write_committed=True,
    )
    ctx.log(
        "EDL overlap merge: retired absorbed id(s) from selection: "
        + ", ".join(retired[:6]),
        level="success",
        stage="edl",
        detail=STAGE_KEY,
    )
    return retired


def overlapping_source_components(
    ctx: RunContext,
    edl: dict[str, Any],
) -> list[dict[str, Any]]:
    """Return mergeable overlapping speech components (no mutation)."""
    clips = edl.get("clips") or []
    if not isinstance(clips, list):
        return []
    segs = _load_seg_lookup(ctx)
    chapters = _chapter_by_segment(ctx)
    from interview_mux.nle_state import load_nle

    try:
        overrides = load_nle(ctx).get("segment_overrides") or {}
    except Exception:
        overrides = {}
    ordered = [str(s) for s in (edl.get("ordered_segment_ids") or [])]
    items: list[tuple[int, dict[str, Any], tuple[int, int]]] = []
    for index, clip in enumerate(clips):
        if not isinstance(clip, dict) or clip.get("type") != "speech":
            continue
        span = _source_span(clip)
        if span is None or not str(clip.get("segment_id") or "").strip():
            continue
        items.append((index, clip, span))
    if len(items) < 2:
        return []
    components: list[dict[str, Any]] = []
    clip_by_sid = {str(c.get("segment_id")): c for _, c, _ in items}
    for idxs in _union_find_components(items, segs=segs, chapters=chapters):
        member_clips = [items[i][1] for i in idxs]
        member_sids = [str(c.get("segment_id")) for c in member_clips]
        survivor = _pick_survivor(
            member_sids,
            clip_by_sid=clip_by_sid,
            segs=segs,
            overrides=overrides if isinstance(overrides, dict) else {},
            ordered=ordered,
        )
        spans = [items[i][2] for i in idxs]
        union_start = min(span[0] for span in spans)
        union_end = max(span[1] for span in spans)
        consumed = [sid for sid in dict.fromkeys(member_sids) if sid != survivor]
        if not consumed:
            continue
        components.append(
            {
                "survivor": survivor,
                "consumed": consumed,
                "members": list(dict.fromkeys(member_sids)),
                "union_start_ms": union_start,
                "union_end_ms": union_end,
            }
        )
    return components


MIN_TRIMMED_CLIP_MS = 200


def trim_residual_source_overlaps(
    ctx: RunContext,
    edl: dict[str, Any],
    *,
    stage: str = "edl",
    persist: bool = False,
) -> list[dict[str, Any]]:
    """Trim overlapping source ranges the union repair cannot merge (ISSUES 97).

    The union repair merges overlapping *same-speaker* speech. A cut-edge
    refinement can extend a clip past the next clip's start across a speaker
    change (exec_063: seg_005's end pushed 660 ms into seg_006); no union
    applies, and strict EDL QC halts on the 380 ms of tape that would play
    twice. This trims the earlier clip's end back to the later clip's start
    (or, when that would leave too little, the later clip's start forward),
    mutating ``edl`` in place. Returns the actions applied.
    """
    clips = edl.get("clips") if isinstance(edl, dict) else None
    if not isinstance(clips, list):
        return []
    speech = [
        (i, c) for i, c in enumerate(clips)
        if isinstance(c, dict) and c.get("type") == "speech" and _source_span(c) is not None
    ]
    speech.sort(key=lambda ic: _source_span(ic[1])[0])
    actions: list[dict[str, Any]] = []
    just_short: list[dict[str, Any]] = []
    for (ia, a), (ib, b) in zip(speech, speech[1:]):
        sa, ea = _source_span(a)
        sb, eb = _source_span(b)
        if not (sa < eb and sb < ea):
            continue
        overlap = min(ea, eb) - max(sa, sb)
        trimmed_clip = None
        if sb - sa >= MIN_TRIMMED_CLIP_MS and ea > sb:
            a["source_end_ms"] = sb
            a["duration_ms"] = max(0, sb - sa)
            trimmed_clip = a
            trimmed, side = str(a.get("segment_id") or f"clips[{ia}]"), "end"
        elif eb - ea >= MIN_TRIMMED_CLIP_MS:
            b["source_start_ms"] = ea
            b["duration_ms"] = max(0, eb - ea)
            trimmed_clip = b
            trimmed, side = str(b.get("segment_id") or f"clips[{ib}]"), "start"
        else:
            continue
        if trimmed_clip is not None and int(trimmed_clip.get("duration_ms") or 0) < 400:
            just_short.append(trimmed_clip)
        actions.append(
            {
                "action": "trim_residual_source_overlap",
                "trimmed": trimmed,
                "side": side,
                "against": str((b if side == "end" else a).get("segment_id") or ""),
                "overlap_ms": int(overlap),
            }
        )
    if not actions:
        return []
    speech_now = [
        c for c in clips if isinstance(c, dict) and c.get("type") == "speech"
    ]

    def _dur(clip: dict[str, Any]) -> int:
        try:
            return int(clip.get("duration_ms") or 0)
        except (TypeError, ValueError):
            return 0

    shorts = [c for c in just_short if 0 < _dur(c) < 400]
    playable = [c for c in speech_now if c not in shorts and _dur(c) >= 400]
    if shorts and playable:
        short_ids = {str(c.get("segment_id") or "") for c in shorts if c.get("segment_id")}
        clips = [c for c in clips if c not in shorts]
        edl["clips"] = clips
        edl["ordered_segment_ids"] = [
            str(s)
            for s in (edl.get("ordered_segment_ids") or [])
            if str(s) not in short_ids
        ]
        omitted = [
            str(s) for s in (edl.get("omitted_unplayable_segment_ids") or []) if s
        ]
        edl["omitted_unplayable_segment_ids"] = omitted + [
            sid for sid in short_ids if sid not in omitted
        ]
    elif len(speech_now) == 1 and shorts:
        only = shorts[0]
        try:
            start = int(only.get("source_start_ms") or 0)
        except (TypeError, ValueError):
            start = 0
        room = start + 400
        for other in speech_now:
            if other is only:
                continue
        nxt = None
        for clip in clips:
            if not isinstance(clip, dict) or clip is only:
                continue
            try:
                other_start = int(clip.get("source_start_ms") or 0)
            except (TypeError, ValueError):
                continue
            if other_start > start:
                nxt = other_start if nxt is None else min(nxt, other_start)
        if nxt is not None:
            room = min(room, nxt - 80)
        blocked = False
        try:
            from interview_mux.media_ip_cta import never_touch_source_intervals

            for interval in never_touch_source_intervals(ctx):
                nt0 = int(interval[0])
                if start < nt0 < room:
                    room = nt0
                    blocked = room - start < 400
        except Exception:
            blocked = False
        if not blocked and room - start >= 400:
            only["source_end_ms"] = start + 400
            only["duration_ms"] = 400
    edl["timeline_duration_ms"] = _retime_clips([c for c in clips if isinstance(c, dict)])
    try:
        ctx.log(
            f"EDL overlap trim: {len(actions)} residual overlap(s) trimmed "
            "(cross-speaker; no union possible)",
            level="warning",
            stage=stage,
            detail=actions[:8],
        )
    except Exception:
        pass
    if persist:
        from interview_mux.air_order import write_live_edl

        write_live_edl(ctx, edl, source=STAGE_KEY)
    return actions


def repair_overlapping_source_ranges(
    ctx: RunContext,
    edl: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Union overlapping same-speaker speech; remap consumed ids onto the survivor.

    Mutates ``edl`` in place when provided. Returns a result dict with
    ``repaired``, ``remap``, ``components``, and ``edl``.
    """
    working = edl
    loaded_from_disk = False
    if working is None:
        if not ctx.artifact_exists("master/edl.json"):
            return {"repaired": False, "remap": {}, "components": [], "edl": None}
        loaded = ctx.read_json("master/edl.json")
        if not isinstance(loaded, dict):
            return {"repaired": False, "remap": {}, "components": [], "edl": None}
        working = loaded
        loaded_from_disk = True
    if not isinstance(working, dict):
        return {"repaired": False, "remap": {}, "components": [], "edl": working}

    components = overlapping_source_components(ctx, working)
    if not components:
        return {"repaired": False, "remap": {}, "components": [], "edl": working}

    segs = _load_seg_lookup(ctx)
    words = _load_words(ctx)
    remap: dict[str, str] = {}
    clips = list(working.get("clips") or [])
    applied: list[dict[str, Any]] = []

    for comp in components:
        survivor = str(comp["survivor"])
        consumed = [str(x) for x in (comp.get("consumed") or [])]
        members = set(comp.get("members") or [survivor, *consumed])
        union_start = int(comp["union_start_ms"])
        union_end = int(comp["union_end_ms"])
        clips = _rebuild_clips(
            clips,
            members=members,
            survivor=survivor,
            union_start=union_start,
            union_end=union_end,
        )
        fused_from = [survivor, *consumed]
        _update_boundaries(
            ctx,
            survivor=survivor,
            consumed=set(consumed),
            union_start=union_start,
            union_end=union_end,
            fused_from=fused_from,
        )
        _update_manifest(
            ctx,
            survivor=survivor,
            consumed=consumed,
            union_start=union_start,
            union_end=union_end,
            words=words,
        )
        _update_nle(
            ctx,
            survivor=survivor,
            consumed=consumed,
            union_start=union_start,
            union_end=union_end,
            segs=segs,
        )
        for cid in consumed:
            remap[cid] = survivor
        applied.append(comp)

    from interview_mux.segment_id_remap import apply_full_segment_id_remap, apply_segment_id_map
    from interview_mux.segment_fuse import remap_fused_ids

    working["clips"] = apply_segment_id_map(clips, remap)
    working["ordered_segment_ids"] = remap_fused_ids(
        list(working.get("ordered_segment_ids") or []), remap
    )
    self_drop = []
    # A transition renamed onto the survivor no longer sits between adjacent
    # speech clips when the union moved; QC refused it once per merge and the
    # retry cost one of the EDL's three invokes (client exec_018; ISSUES 175).
    speech_seq = [
        str(c.get("segment_id") or "")
        for c in (working.get("clips") or [])
        if isinstance(c, dict) and str(c.get("type") or "") == "speech"
    ]
    adjacent = {(speech_seq[i], speech_seq[i + 1]) for i in range(len(speech_seq) - 1)}
    next_clips: list[Any] = []
    for clip in working.get("clips") or []:
        if not isinstance(clip, dict) or str(clip.get("type") or "") != "transition":
            next_clips.append(clip)
            continue
        after_id = str(clip.get("after_segment_id") or "")
        before_id = str(clip.get("before_segment_id") or "")
        if after_id and before_id and after_id == before_id:
            self_drop.append(clip)
            continue
        if after_id and before_id and (after_id, before_id) not in adjacent:
            self_drop.append(clip)
            continue
        next_clips.append(clip)
    working["clips"] = next_clips
    working["timeline_duration_ms"] = _retime_clips(
        [c for c in working["clips"] if isinstance(c, dict)]
    )
    working["vo_pickup_clip_count"] = sum(
        1
        for c in working["clips"]
        if isinstance(c, dict) and c.get("type") == "vo_pickup"
    )

    persist_edl = loaded_from_disk or ctx.artifact_exists("master/edl.json")
    if persist_edl:
        from interview_mux.air_order import write_live_edl

        write_live_edl(ctx, working, source=STAGE_KEY)

    apply_full_segment_id_remap(ctx, remap, stage_key=STAGE_KEY, skip_handoff=True)
    _drop_self_transitions(ctx)
    try:
        retire_consumed_ids_from_selection(ctx)
    except Exception as exc:
        ctx.log(
            f"EDL overlap merge: could not retire absorbed ids from selection ({exc})",
            level="warning",
            stage="edl",
            detail=STAGE_KEY,
        )

    # The selection decides where the union airs: its rename either landed
    # (first occurrence kept) or the freeze refused it (consumed id retired,
    # survivor in place). Either way the EDL follows the committed selection,
    # so narrative QC parity cannot fail on the merge (exec_019 and exec_023
    # took opposite branches; ISSUES 176).
    try:
        sel_now = ctx.read_json("master/selection.json") if ctx.artifact_exists("master/selection.json") else None
        from interview_mux.order_hash import seatable_selection_ids

        # Omitted ids are not clips; leaving them in made the reorder a no-op.
        sel_order = seatable_selection_ids(sel_now, working)
        reordered = _reorder_speech_blocks(working.get("clips") or [], sel_order)
        if reordered is not None:
            working["clips"] = _drop_non_adjacent_transitions(reordered)
            working["ordered_segment_ids"] = [
                str(c.get("segment_id") or "")
                for c in working["clips"]
                if isinstance(c, dict) and str(c.get("type") or "") == "speech"
            ]
            working["timeline_duration_ms"] = _retime_clips(
                [c for c in working["clips"] if isinstance(c, dict)]
            )
            if persist_edl:
                from interview_mux.air_order import write_live_edl

                write_live_edl(ctx, working, source=STAGE_KEY)
            ctx.log(
                "EDL overlap merge: speech order aligned to the committed selection",
                level="info",
                stage="edl",
                detail=STAGE_KEY,
            )
    except Exception as exc:
        ctx.log(
            f"EDL overlap merge: could not align EDL order to selection ({exc})",
            level="warning",
            stage="edl",
            detail=STAGE_KEY,
        )

    labels = ", ".join(
        f"{','.join(c['consumed'])}→{c['survivor']}" for c in applied
    )
    ctx.log(
        f"EDL overlap merge: {labels}",
        level="success",
        stage="edl",
        detail=STAGE_KEY,
    )
    if edl is not None and edl is not working:
        edl.clear()
        edl.update(working)
    elif edl is not None:
        edl["clips"] = working["clips"]
        edl["ordered_segment_ids"] = working["ordered_segment_ids"]
        edl["timeline_duration_ms"] = working["timeline_duration_ms"]
        edl["vo_pickup_clip_count"] = working.get("vo_pickup_clip_count")

    return {
        "repaired": True,
        "remap": remap,
        "components": applied,
        "edl": working,
        "self_transitions_dropped": len(self_drop),
    }

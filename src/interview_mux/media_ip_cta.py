"""Host execution of 0.1.0 flagship media-IP CTA judgments + editorial omits.

Flagship ranking decides which natives are clear this-listener media pitches.
This module drops those ids, optionally recuts mixed story+pitch clips, and
exposes never-touch / coverage-exempt / clone-cover flags to layup. 0.0.0 is
a no-op.

Sponsor-bumper phrases are host hard-omit; media-IP CTA speech-acts stay
LLM-first. Editorial exclude rationales must leave the locked air order.
"""

from __future__ import annotations

import re
from contextlib import contextmanager
from typing import Any, Iterator

from interview_mux.run_context import RunContext

# Selection.json producers (must match artifact_ownership catalog).
_SELECTION_COMMIT_STAGES = frozenset(
    {
        "full_master_ranking",
        "selection_order_sanitize",
        "selection",
        "nugget_layup_compose",
        "junction_snip_qa",
    }
)


def _selection_commit_stage_key() -> str:
    """Live stage for selection CTA commits — never spoof ranking/edl.

    exec_13177: hardcoding full_master_ranking/edl hid ownership failures and
    broke when anti-spoof / active-stage checks tightened. Prefer the active
    producer; fall back to ranking (primary CTA host) when unknown.
    """
    try:
        from interview_mux.write_staging import active_stage_id

        sk = str(active_stage_id() or "").strip()
    except Exception:
        sk = ""
    if sk in _SELECTION_COMMIT_STAGES:
        return sk
    return "full_master_ranking"


def _skip_foreign_owner_side_writes() -> bool:
    """Layup CTA path may only touch selection — not manifest/brief/evals."""
    try:
        from interview_mux.write_staging import active_stage_id

        return str(active_stage_id() or "").strip() == "nugget_layup_compose"
    except Exception:
        return False


_SEG_ID_RE = re.compile(r"\bseg_[a-zA-Z0-9]+\b")
_SEG_RANGE_RE = re.compile(
    r"(seg_[a-zA-Z0-9]+)\s+through\s+(seg_[a-zA-Z0-9]+)",
    flags=re.IGNORECASE,
)
_REVERSE_JUMP_INTO_RE = re.compile(
    r"reverse[-\s]?jumps?\b[^.]{0,120}?\b(?:into|to|->|→)\s+(seg_[a-zA-Z0-9]+)",
    flags=re.IGNORECASE,
)
_REVERSE_JUMP_ARROW_RE = re.compile(
    r"reverse[-\s]?jump\s+seg_[a-zA-Z0-9]+\s*(?:→|->)\s*(seg_[a-zA-Z0-9]+)",
    flags=re.IGNORECASE,
)
_OUTRO_REASON_TOKENS = (
    "outro",
    "sign_off",
    "sign-off",
    "credits",
    "follow us",
    "direct listener",
    "contact the programme",
    "contact the program",
    # exec_010: "post-roll/end-credit or corrupt fragments after the coherent
    # close" was not read as an outro, so layup's selection need stayed blocking.
    "post_roll",
    "postroll",
    "post roll",
    "end_credit",
    "end credit",
    "closing_credit",
    "closing credit",
    "end card",
    "after the coherent close",
    "after the close",
)
_SELECTION_RERUN_STAGES = frozenset(
    {
        "selection",
        "full_master_ranking",
        "selection_framing_apply",
        "ranking",
        # Layup often asks boundary_detection to recut/remove post-sign-off scraps;
        # host-execute the omit instead of bouncing layup forever.
        "boundary_detection",
    }
)
_FRAGMENTARY_TAIL_TOKENS = (
    "fragmentary",
    "empty or fragmentary",
    "after an already complete sign-off",
    "after sign-off",
    "complete sign-off",
    "incomplete fragment",
    "approve its removal",
    "heavily degraded",
    "degraded transcript",
    "after the cta",
    "cta cut",
    "required to keep it on air",
    # Layup asks for excerpts after host already omitted the scrap (exec_13198).
    "current text is empty",
    "text is empty",
    "temporary omission",
    "empty/heavily degraded",
    "blank/heavily degraded",
    "empty, so no listener",
    "corrupt fragment",
    "corrupted fragment",
    "garbled",
    "unintelligible fragment",
    "fragments after",
)

REASON = "media_ip_cta"
ARTIFACT_REL = "mastering/media_ip_cta.json"
SKIP_HOLE = "media_ip_cta_hole"
MIN_CHILD_MS = 1500
MIN_PLAYABLE_KEEP_MS = 400
_CTA_CLASS_LABELS = frozenset(
    {"cta", "sponsor", "subscribe", "monetize", "outro", "credits"}
)
_LETS_HEAR_RE = (
    "let's hear",
    "lets hear",
    "let us hear",
)


def enabled(ctx: RunContext) -> bool:
    try:
        from interview_mux.homunculus.runtime import has_homunculus_features

        return bool(has_homunculus_features(ctx))
    except Exception:
        return False


def cta_cover_budget_exempt(ctx: RunContext) -> bool:
    return bool(getattr(ctx, "_cta_cover_regenerate_inner", False))


@contextmanager
def cta_cover_regenerate_scope(ctx: RunContext) -> Iterator[None]:
    """Mandatory cover text+voice rerun — does not burn the 3-invoke cap."""
    setattr(ctx, "_cta_cover_regenerate_inner", True)
    try:
        yield
    finally:
        if hasattr(ctx, "_cta_cover_regenerate_inner"):
            delattr(ctx, "_cta_cover_regenerate_inner")


def load_state(ctx: RunContext) -> dict[str, Any]:
    if not ctx.artifact_exists(ARTIFACT_REL):
        return {}
    raw = ctx.read_json(ARTIFACT_REL)
    return raw if isinstance(raw, dict) else {}


def never_touch_segment_ids(ctx: RunContext) -> set[str]:
    state = load_state(ctx)
    ids = {str(x) for x in (state.get("dropped_segment_ids") or []) if x}
    ids |= {str(x) for x in (state.get("never_touch_segment_ids") or []) if x}
    return ids


def _is_cta_exclude_reason(reason: Any) -> bool:
    key = str(reason or "").strip().lower().replace("-", "_")
    return key == REASON or key.startswith(f"{REASON}_") or key.startswith(f"{REASON}:")


def selection_cta_exclude_ids(ctx: RunContext) -> set[str]:
    """Never-touch CTA ids from media-IP state plus selection exclude rationales.

    Selection alone is enough when ``mastering/media_ip_cta.json`` is thin or
    stale — excluded subscribe/pitch tape must stay out of speech source bounds.
    """
    ids = set(never_touch_segment_ids(ctx))
    if not ctx.artifact_exists("master/selection.json"):
        return ids
    try:
        sel = ctx.read_json("master/selection.json")
    except Exception:
        return ids
    if not isinstance(sel, dict):
        return ids
    rationales = (
        sel.get("exclude_rationales")
        if isinstance(sel.get("exclude_rationales"), dict)
        else {}
    )
    for sid, reason in rationales.items():
        if sid and _is_cta_exclude_reason(reason):
            ids.add(str(sid))
    for row in sel.get("excluded_segment_ids") or []:
        if isinstance(row, dict):
            sid = str(row.get("segment_id") or "").strip()
            if sid and _is_cta_exclude_reason(row.get("reason")):
                ids.add(sid)
        elif isinstance(row, str) and row.strip():
            sid = row.strip()
            if _is_cta_exclude_reason(rationales.get(sid)):
                ids.add(sid)
    return ids


def never_touch_source_intervals(ctx: RunContext) -> list[tuple[int, int, str]]:
    """Source-time ranges that speech clips must never play (CTA / never-touch).

    Parent CTA slabs are skipped when NLE children of that parent are still on
    the air order — those children (and leaf CTA siblings) own the tape map.
    Remaining dropped-parent slabs punch holes for on-air keeps (manifest bounds)
    so packaging speech is not zeroed inside a mega parent range.
    """
    # Excluded ids are the point here; the live view has no row for them.
    by_id = _all_segments_by_id(ctx)
    ordered: list[str] = []
    if ctx.artifact_exists("master/selection.json"):
        try:
            sel = ctx.read_json("master/selection.json")
        except Exception:
            sel = None
        if isinstance(sel, dict):
            ordered = [str(s) for s in (sel.get("ordered_segment_ids") or []) if s]
    out: list[tuple[int, int, str]] = []
    heal_nle_unplayable_keep_overrides(ctx)
    for sid in sorted(selection_cta_exclude_ids(ctx)):
        if ordered and any(_is_nle_child(child, sid) for child in ordered):
            continue
        seg = by_id.get(sid) or {}
        if not isinstance(seg, dict):
            continue
        try:
            start = int(seg.get("start_ms") or 0)
            end = int(seg.get("end_ms") or 0)
        except (TypeError, ValueError):
            continue
        if end > start:
            out.append((start, end, sid))
    return _punch_ordered_keeps_from_never_touch(
        out, _manifest_segments_by_id(ctx), ordered
    )


def _is_cta_class_keep(seg: dict[str, Any] | None) -> bool:
    """True when segment tags/roles mark sponsor/CTA/promo — do not punch onto air."""
    if not isinstance(seg, dict):
        return False
    role = str(seg.get("speaker_role") or seg.get("role") or "").casefold()
    if any(label in role for label in _CTA_CLASS_LABELS):
        return True
    tags = seg.get("topic_tags") or seg.get("tags") or []
    if isinstance(tags, list):
        for tag in tags:
            key = str(tag or "").casefold().replace("-", "_")
            if key in _CTA_CLASS_LABELS or any(lbl in key for lbl in _CTA_CLASS_LABELS):
                return True
    seg_type = str(seg.get("type") or "").casefold()
    if any(label in seg_type for label in ("cta", "sponsor", "promo", "outro")):
        return True
    flags = seg.get("flags") or []
    if isinstance(flags, list):
        for flag in flags:
            key = str(flag or "").casefold().replace("-", "_")
            if key in _CTA_CLASS_LABELS or any(lbl in key for lbl in _CTA_CLASS_LABELS):
                return True
    return False


def _punch_ordered_keeps_from_never_touch(
    intervals: list[tuple[int, int, str]],
    by_id: dict[str, dict[str, Any]],
    ordered: list[str],
) -> list[tuple[int, int, str]]:
    """Carve on-air keep tape out of never-touch slabs using manifest bounds.

    Skips CTA-class keeps (sponsor/subscribe/promo labels) — those omit instead
    of punching a hole. Unrelated packaging keeps inside a dropped parent (e.g.
    seg_003a inside seg_002) must not clamp to zero duration.
    """
    if not intervals or not ordered:
        return intervals
    keep_spans: list[tuple[int, int]] = []
    for kid in ordered:
        seg = by_id.get(kid) or {}
        if not isinstance(seg, dict) or _is_cta_class_keep(seg):
            continue
        try:
            ks = int(seg.get("start_ms") or 0)
            ke = int(seg.get("end_ms") or 0)
        except (TypeError, ValueError):
            continue
        if ke > ks:
            keep_spans.append((ks, ke))
    if not keep_spans:
        return intervals
    punched: list[tuple[int, int, str]] = []
    for nt_s, nt_e, sid in intervals:
        holes: list[tuple[int, int]] = []
        for ks, ke in keep_spans:
            if ke <= nt_s or ks >= nt_e:
                continue
            holes.append((max(ks, nt_s), min(ke, nt_e)))
        if not holes:
            punched.append((nt_s, nt_e, sid))
            continue
        holes.sort()
        merged: list[tuple[int, int]] = []
        for hs, he in holes:
            if merged and hs <= merged[-1][1]:
                merged[-1] = (merged[-1][0], max(merged[-1][1], he))
            else:
                merged.append((hs, he))
        cursor = nt_s
        for hs, he in merged:
            if hs > cursor:
                punched.append((cursor, hs, sid))
            cursor = max(cursor, he)
        if cursor < nt_e:
            punched.append((cursor, nt_e, sid))
    return punched


def never_touch_end_cap_ms(
    source_start_ms: int,
    intervals: list[tuple[int, int, str]] | None,
) -> int | None:
    """Earliest never-touch start that an extend past ``source_start_ms`` would invade."""
    if not intervals:
        return None
    start = int(source_start_ms)
    cap: int | None = None
    for nt_s, nt_e, _sid in intervals:
        if nt_e <= start:
            continue
        if nt_s <= start < nt_e:
            return start
        if start < nt_s:
            cap = nt_s if cap is None else min(cap, nt_s)
    return cap


def clamp_source_away_from_never_touch(
    source_start_ms: int,
    source_end_ms: int,
    intervals: list[tuple[int, int, str]] | None,
    *,
    min_span_ms: int = 400,
) -> tuple[int, int, list[str]]:
    """Trim speech source bounds so they do not overlap never-touch CTA tape.

    Prefer ending before the invaded CTA over extending through subscribe pitch.
    """
    start = int(source_start_ms)
    end = int(source_end_ms)
    notes: list[str] = []
    if end <= start or not intervals:
        return start, end, notes
    for nt_s, nt_e, sid in sorted(intervals, key=lambda row: (row[0], row[1])):
        if end <= nt_s or start >= nt_e:
            continue
        if start < nt_s:
            new_end = nt_s
            if new_end != end:
                end = new_end
                notes.append(f"clamp_end_before_never_touch:{sid}")
            continue
        # Open is inside never-touch — push past when possible.
        if nt_e < end and end - nt_e >= int(min_span_ms):
            start = nt_e
            notes.append(f"clamp_start_after_never_touch:{sid}")
            continue
        # Entire keep is never-touch tape — zero it rather than air CTA.
        end = start
        notes.append(f"zeroed_inside_never_touch:{sid}")
        break
    if end < start:
        end = start
    return start, end, notes


def clamp_edl_speech_away_from_never_touch(
    ctx: RunContext,
    edl: dict[str, Any] | None,
    *,
    intervals: list[tuple[int, int, str]] | None = None,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Clamp every speech clip's source range away from never-touch CTA intervals."""
    if not isinstance(edl, dict):
        return {}, []
    heal_nle_unplayable_keep_overrides(ctx)
    out = dict(edl)
    clips = [dict(c) for c in (out.get("clips") or []) if isinstance(c, dict)]
    ranges = (
        list(intervals)
        if intervals is not None
        else never_touch_source_intervals(ctx)
    )
    if not ranges:
        out["clips"] = clips
        return out, []
    changed_rows: list[dict[str, Any]] = []
    clamped_ids: set[int] = set()
    for index, clip in enumerate(clips):
        if str(clip.get("type") or "") != "speech":
            continue
        try:
            ss = int(clip.get("source_start_ms") or 0)
            se = int(clip.get("source_end_ms") or ss)
        except (TypeError, ValueError):
            continue
        new_ss, new_se, notes = clamp_source_away_from_never_touch(ss, se, ranges)
        if not notes or (new_ss == ss and new_se == se):
            continue
        clamped_ids.add(id(clip))
        clip["source_start_ms"] = new_ss
        clip["source_end_ms"] = new_se
        clip["duration_ms"] = max(0, new_se - new_ss)
        prior = str(clip.get("air_bound_reason") or "")
        clip["air_bound_reason"] = (
            f"{prior}+never_touch_clamp" if prior else "never_touch_clamp"
        )
        changed_rows.append(
            {
                "clip_index": index,
                "segment_id": clip.get("segment_id"),
                "before": [ss, se],
                "after": [new_ss, new_se],
                "notes": notes,
            }
        )
        clips[index] = clip
    min_keep_ms = MIN_PLAYABLE_KEEP_MS
    kept: list[dict[str, Any]] = []
    dropped_ids: list[str] = []
    drop_reasons: dict[str, str] = {}
    man_by_id = _manifest_segments_by_id(ctx)
    on_air = [
        str(c.get("segment_id"))
        for c in clips
        if str(c.get("type") or "") == "speech" and c.get("segment_id")
    ]
    for clip in clips:
        if str(clip.get("type") or "") != "speech":
            kept.append(clip)
            continue
        try:
            dur = int(clip.get("duration_ms") or 0)
        except (TypeError, ValueError):
            dur = 0
        if dur >= min_keep_ms:
            kept.append(clip)
            continue
        sid = str(clip.get("segment_id") or "")
        # A must-air keep is never dropped here: the selection would refuse the
        # omit and the EDL would never match it again (exec_025 seg_021, a
        # 280 ms clip this clamp never touched; ISSUES 179). The live-EDL
        # writer reseats it on its own tape instead.
        try:
            from interview_mux.removal_authority import removal_block_reason

            blocked = removal_block_reason(ctx, sid, on_air=on_air)
        except Exception:
            blocked = None
        if blocked:
            kept.append(clip)
            continue
        seg = man_by_id.get(sid) or {}
        if id(clip) not in clamped_ids:
            reason = "dropped_unplayable_short"
        elif _is_cta_class_keep(seg if isinstance(seg, dict) else None):
            reason = "never_touch_unplayable"
        else:
            reason = "dropped_unplayable_never_touch"
        dropped_ids.append(sid)
        drop_reasons[sid] = reason
        changed_rows.append(
            {
                "clip_index": len(kept),
                "segment_id": sid,
                "before": [
                    clip.get("source_start_ms"),
                    clip.get("source_end_ms"),
                ],
                "after": None,
                "notes": [reason],
            }
        )
    if dropped_ids:
        ordered = [
            str(s)
            for s in (out.get("ordered_segment_ids") or [])
            if str(s) not in set(dropped_ids)
        ]
        out["ordered_segment_ids"] = ordered
        _omit_unplayable_keeps_from_selection(ctx, dropped_ids, drop_reasons)
    if changed_rows:
        from interview_mux.listenability_guards import reindex_clip_timeline

        out["clips"] = kept
        out["timeline_duration_ms"] = reindex_clip_timeline(kept)
    else:
        out["clips"] = kept
    return out, changed_rows


def _omit_unplayable_keeps_from_selection(
    ctx: RunContext,
    dropped_ids: list[str],
    drop_reasons: dict[str, str] | None = None,
) -> None:
    """Selection leads: drop keeps that never-touch clamp made unplayable."""
    ids = [str(s) for s in dropped_ids if s]
    if not ids or not ctx.artifact_exists("master/selection.json"):
        return
    try:
        sel = ctx.read_json("master/selection.json")
    except Exception:
        return
    if not isinstance(sel, dict):
        return
    reasons = drop_reasons if isinstance(drop_reasons, dict) else {}
    drop = set(ids)
    ordered = [str(s) for s in (sel.get("ordered_segment_ids") or []) if s]
    kept = [s for s in ordered if s not in drop]
    if kept == ordered and not drop:
        return
    sel["ordered_segment_ids"] = kept
    have = {
        str(r.get("segment_id") if isinstance(r, dict) else r)
        for r in (sel.get("excluded_segment_ids") or [])
    }
    excl = list(sel.get("excluded_segment_ids") or [])
    rationales = (
        dict(sel.get("exclude_rationales") or {})
        if isinstance(sel.get("exclude_rationales"), dict)
        else {}
    )
    for sid in ids:
        reason = reasons.get(sid) or "never_touch_unplayable"
        if sid not in have:
            excl.append({"segment_id": sid, "reason": reason})
            have.add(sid)
        rationales.setdefault(sid, reason)
    sel["excluded_segment_ids"] = excl
    sel["exclude_rationales"] = rationales
    keep_set = set(kept)
    for ch in sel.get("chapters") or []:
        if isinstance(ch, dict):
            ch["segment_ids"] = [
                str(x) for x in (ch.get("segment_ids") or []) if str(x) in keep_set
            ]
    try:
        from interview_mux.artifact_repairs import reconcile_ordered_vs_excluded
        from interview_mux.order_hash import stamp_order_hash

        sel = reconcile_ordered_vs_excluded(sel)
        sel = stamp_order_hash(sel)
    except Exception:
        pass
    from interview_mux.air_order_boundary import commit_selection_mutation

    commit_selection_mutation(
        ctx,
        sel,
        producer="media_ip_cta",
        stage_key=_selection_commit_stage_key(),
        checkpoint_mode="detect",
        skip_checkpoint=True,
        write_committed=True,
    )


def release_false_cta_never_touch(ctx: RunContext) -> list[str]:
    """Unstamp never-touch ids whose tape is not a CTA (reverse-jump intros)."""
    state = load_state(ctx)
    if not isinstance(state, dict):
        return []
    dropped = [str(x) for x in (state.get("dropped_segment_ids") or []) if x]
    never = [str(x) for x in (state.get("never_touch_segment_ids") or []) if x]
    by_id = _segments_by_id(ctx)
    pool = [sid for sid in dict.fromkeys([*dropped, *never]) if sid]
    parents: set[str] = set()
    for sid in pool:
        suffix = sid.split("_", 1)[-1]
        letter = re.search(r"[a-z]+$", suffix)
        if letter:
            parents.add(sid[: -len(letter.group(0))])
        elif not re.search(r"[a-z]$", suffix):
            parents.add(sid)
    released: list[str] = []
    # Host-executed editorial omits (degraded transcript_excerpt scraps, etc.) must
    # stick even when tape text is not a hard-omit CTA phrase (exec_13198 seg_070).
    editorial_keep: set[str] = set()
    try:
        if ctx.artifact_exists("master/selection.json"):
            loaded = ctx.read_json("master/selection.json") or {}
            rats = (
                loaded.get("exclude_rationales")
                if isinstance(loaded.get("exclude_rationales"), dict)
                else {}
            )
            for sid, reason in (rats or {}).items():
                key = str(sid or "").strip()
                if key and is_editorial_exclude_reason(str(reason or "")):
                    editorial_keep.add(key)
            for row in loaded.get("excluded_segment_ids") or []:
                if isinstance(row, dict):
                    sid = str(row.get("segment_id") or "").strip()
                    reason = str(row.get("reason") or (rats or {}).get(sid) or "")
                else:
                    sid = str(row or "").strip()
                    reason = str((rats or {}).get(sid) or "")
                if sid and is_editorial_exclude_reason(reason):
                    editorial_keep.add(sid)
    except Exception:
        editorial_keep = set()

    def _keep(sid: str) -> bool:
        if sid in editorial_keep:
            return True
        text = str((by_id.get(sid) or {}).get("text") or "").strip()
        if not text:
            return True
        if _tape_is_hard_omit_cta(ctx, sid, by_id):
            return True
        # Late-tape short scraps (garbled post-CTA) must stay never-touch even
        # when the phrase is not a hard-omit CTA (exec_13198 seg_070).
        try:
            start = int((by_id.get(sid) or {}).get("start_ms") or 0)
            max_end = 0
            for row in by_id.values():
                try:
                    max_end = max(max_end, int((row or {}).get("end_ms") or 0))
                except (TypeError, ValueError):
                    pass
            if (
                max_end
                and start >= int(max_end * 0.85)
                and len(text.split()) <= 16
            ):
                return True
        except Exception:
            pass
        if any(_is_nle_child(sid, parent) for parent in parents):
            return True
        if any(_is_nle_child(child, sid) for child in pool):
            return True
        released.append(sid)
        return False

    if not dropped and not never:
        return []
    new_dropped = [sid for sid in dropped if _keep(sid)]
    new_never = [sid for sid in never if _keep(sid)]
    # `_keep` appends once per call; de-dupe released.
    released = list(dict.fromkeys(released))
    if new_dropped == dropped and new_never == never:
        return []
    state["dropped_segment_ids"] = new_dropped
    state["never_touch_segment_ids"] = new_never
    try:
        from interview_mux.write_staging import write_committed_json

        write_committed_json(ctx, ARTIFACT_REL, state, stage_key="full_master_ranking")
    except Exception:
        _write_state(ctx, state)
    return released


def _restore_released_to_order(
    ctx: RunContext, ordered: list[str], released: list[str]
) -> list[str]:
    """Put wrongly never-touched natives back on the air, source-time order."""
    if not released:
        return list(ordered)
    by_id = _segments_by_id(ctx)
    out = list(ordered)
    for sid in released:
        if not sid or sid in out:
            continue
        if not str((by_id.get(sid) or {}).get("text") or "").strip():
            continue
        try:
            start = int((by_id.get(sid) or {}).get("start_ms") or 0)
        except (TypeError, ValueError):
            start = 0
        idx = 0
        for i, other in enumerate(out):
            try:
                ostart = int((by_id.get(other) or {}).get("start_ms") or 0)
            except (TypeError, ValueError):
                ostart = 0
            if ostart <= start:
                idx = i + 1
        out.insert(idx, sid)
    return out


def ranking_cta_omit_ids(ctx: RunContext) -> set[str]:
    """IDs ranking must exclude: stored never-touch plus tape-scan hard-omit CTAs.

    ``media_ip_cta.json`` is written during ranking persist, so the packet-time
    keep list cannot wait on that file. Scan native text now so must-keep and
    CTA omit are consistent before the LLM runs.
    """
    ids = set(never_touch_segment_ids(ctx))
    if not enabled(ctx):
        return {s for s in ids if s}
    try:
        from interview_mux.homunculus.values import should_hard_omit_cta
    except Exception:
        return {s for s in ids if s}
    by_id = _segments_by_id(ctx)
    for sid, row in by_id.items():
        text = str((row or {}).get("text") or "")
        if text and should_hard_omit_cta(text):
            ids.add(str(sid))
    try:
        from interview_mux.homunculus.issues import read_issues

        for issue in read_issues(ctx):
            if not isinstance(issue, dict):
                continue
            kind = str(issue.get("kind") or "")
            if kind not in {
                "perspective_direct_monetization",
                "direct_listener_sponsor_promotion",
            }:
                continue
            for sid in issue.get("implicated") or []:
                key = str(sid or "").strip()
                if key and _tape_is_hard_omit_cta(ctx, key, by_id):
                    ids.add(str(sid))
    except Exception:
        pass
    return {s for s in ids if s}


def never_touch_texts(ctx: RunContext) -> list[str]:
    state = load_state(ctx)
    texts = [str(t).strip() for t in (state.get("never_touch_texts") or []) if str(t).strip()]
    if texts:
        return texts
    ids = never_touch_segment_ids(ctx)
    if not ids:
        return []
    by_id = _all_segments_by_id(ctx)
    out: list[str] = []
    for sid in ids:
        text = str((by_id.get(sid) or {}).get("text") or "").strip()
        if text:
            out.append(text)
    return out


def cover_target_ids(ctx: RunContext) -> set[str]:
    state = load_state(ctx)
    return {str(x) for x in (state.get("cover_target_ids") or []) if x}


def admitted_story_segment_ids(ctx: RunContext) -> set[str]:
    """Keepable recut remainders — first-class candidates for ranking/shape/master."""
    state = load_state(ctx)
    ids = {str(x) for x in (state.get("admitted_story_segment_ids") or []) if x}
    if ids:
        return ids
    return set(_story_ids_from_recuts(list(state.get("recuts") or [])))


def is_lets_hear_hinge(text: str) -> bool:
    key = " ".join(str(text or "").casefold().split())
    if not key:
        return False
    return any(p in key for p in _LETS_HEAR_RE)


def _on_air_story_tokens(ctx: RunContext) -> set[str]:
    """Tokens of the tape that airs: wording shared with it is story, not CTA."""
    try:
        if not ctx.artifact_exists("master/selection.json"):
            return set()
        sel = ctx.read_json("master/selection.json")
        order = [str(s) for s in ((sel or {}).get("ordered_segment_ids") or []) if s]
    except Exception:
        return set()
    by_id = _segments_by_id(ctx)
    out: set[str] = set()
    for sid in order:
        out |= _tokens(str((by_id.get(sid) or {}).get("text") or ""))
    return out


def air_overlaps_never_touch(ctx: RunContext, text: str, *, min_overlap: float = 0.45) -> bool:
    """True when clone VO reuses dropped CTA wording (token overlap).

    Never-touch texts are whole segments, and a mixed CTA parent carries
    minutes of story around the sponsor read. Only wording that does not also
    air as story counts, or every story line on the parent's topic read as CTA
    reuse (exec_015: a CTC layup overlapped seg_001's 650-token intro at 0.59
    and layup QC refused it; ISSUES 157).
    """
    air = _tokens(text)
    if len(air) < 4:
        return False
    story = _on_air_story_tokens(ctx)
    for blob in never_touch_texts(ctx):
        other = _tokens(blob)
        if story:
            other = other - story
        if len(other) < 4:
            continue
        if story:
            if len(air & other) / float(len(air)) >= min_overlap:
                return True
        elif _overlap(air, other) >= min_overlap:
            return True
    return False


def _prune_cfg() -> dict[str, int]:
    try:
        from interview_mux.config import merged_config

        row = (merged_config().get("mastering") or {}).get("media_ip_cta") or {}
    except Exception:
        row = {}
    if not isinstance(row, dict):
        row = {}
    return {
        "prune_max_depth": int(row.get("prune_max_depth") or 2),
        "prune_max_children": int(row.get("prune_max_children") or 12),
        "min_child_ms": int(row.get("min_child_ms") or MIN_CHILD_MS),
        "prune_max_seed_passes": int(row.get("prune_max_seed_passes") or 3),
    }


def _g0_words(ctx: RunContext) -> list[dict[str, Any]]:
    for rel in (
        "transcript/full.json",
        "operator/transcript_corrected.json",
        "ingest/transcript.json",
    ):
        if not ctx.artifact_exists(rel):
            continue
        try:
            tr = ctx.read_json(rel)
        except Exception:
            continue
        words = tr.get("words") if isinstance(tr, dict) else None
        if isinstance(words, list):
            return [w for w in words if isinstance(w, dict)]
    return []


def _word_token(w: dict[str, Any]) -> str:
    return str(w.get("text") or w.get("word") or "").strip()


def _span_text(words: list[dict[str, Any]], start_ms: int, end_ms: int) -> str:
    parts: list[str] = []
    for w in words:
        try:
            ws = int(w.get("start_ms") or 0)
            we = int(w.get("end_ms") or ws)
        except (TypeError, ValueError):
            continue
        if we <= start_ms or ws >= end_ms:
            continue
        tok = _word_token(w)
        if tok:
            parts.append(tok)
    return " ".join(parts).strip()


def _mixed_story_cta_cut_ms(
    words: list[dict[str, Any]], start_ms: int, end_ms: int
) -> int | None:
    """First listen-complete hinge whose leftover is CTA / a new beat.

    Keeps a short story prefix (list close) even when it is under min_child_ms.
    """
    if not words or end_ms <= start_ms:
        return None
    from interview_mux.homunculus.values import should_hard_omit_cta
    from interview_mux.thought_complete_recut import (
        _next_opens_new_beat,
        complete_thought_candidates,
    )

    cands = complete_thought_candidates(
        words, start_ms, horizon_ms=end_ms, speaker=""
    )
    for cut in cands:
        try:
            hinge = int(cut)
        except (TypeError, ValueError):
            continue
        if not (start_ms < hinge < end_ms):
            continue
        prefix = _span_text(words, start_ms, hinge)
        rest = _span_text(words, hinge, end_ms)
        if not rest:
            continue
        if prefix and should_hard_omit_cta(prefix):
            continue
        if should_hard_omit_cta(rest) or _next_opens_new_beat(rest):
            return hinge
        if prefix and not should_hard_omit_cta(prefix):
            return hinge
    return None


def _partition_complete_thoughts(
    words: list[dict[str, Any]],
    start_ms: int,
    end_ms: int,
    *,
    min_child_ms: int,
    extra_cuts: list[int] | None = None,
    max_children: int = 12,
    allow_short_extra: bool = False,
) -> list[tuple[int, int]]:
    """Left-to-right complete-thought spans; ranking cut_ms are extra hinges."""
    window = []
    for w in words:
        try:
            ws = int(w.get("start_ms") or 0)
        except (TypeError, ValueError):
            continue
        if start_ms <= ws < end_ms and _word_token(w):
            window.append(w)
    window.sort(key=lambda w: int(w.get("start_ms") or 0))
    hinges: list[int] = []
    if window:
        from interview_mux.audio_timeline import snap_cut_to_word_boundary
        from interview_mux.gap_vo_prior_context import is_legal_conceptual_hinge

        span_start = max(start_ms, int(window[0].get("start_ms") or start_ms))
        acc: list[dict[str, Any]] = []
        for i, w in enumerate(window):
            acc.append(w)
            text = " ".join(_word_token(x) for x in acc)
            try:
                w_end = int(w.get("end_ms") or w.get("start_ms") or 0)
            except (TypeError, ValueError):
                continue
            nxt = window[i + 1] if i + 1 < len(window) else None
            pause = None
            if nxt is not None:
                try:
                    pause = int(nxt.get("start_ms") or 0) - w_end
                except (TypeError, ValueError):
                    pause = None
            if not is_legal_conceptual_hinge(
                text, words=words, end_ms=w_end, next_pause_ms=pause
            ):
                continue
            if w_end - span_start < min_child_ms:
                continue
            snapped = snap_cut_to_word_boundary(w_end, words)
            snapped = min(max(snapped, span_start + min_child_ms), end_ms - min_child_ms)
            if start_ms + min_child_ms <= snapped <= end_ms - min_child_ms:
                hinges.append(snapped)
                span_start = snapped
                acc = []
            if len(hinges) >= max_children - 1:
                break
    for cut in extra_cuts or []:
        try:
            c = int(cut)
        except (TypeError, ValueError):
            continue
        if allow_short_extra and start_ms < c < end_ms:
            hinges.append(c)
        elif start_ms + min_child_ms <= c <= end_ms - min_child_ms:
            hinges.append(c)
    extra_pref = []
    for cut in extra_cuts or []:
        try:
            extra_pref.append(int(cut))
        except (TypeError, ValueError):
            continue
    if extra_pref:
        prefer = set(extra_pref)
        collapsed: list[int] = []
        for h in sorted(set(hinges)):
            near_pref = next((p for p in prefer if abs(h - p) <= 80), None)
            if near_pref is not None and h != near_pref:
                continue
            collapsed.append(h)
        hinges = collapsed
    cuts = sorted(set(hinges))
    bounds = [start_ms, *cuts, end_ms]
    spans: list[tuple[int, int]] = []
    for a, b in zip(bounds, bounds[1:]):
        if b <= a:
            continue
        if b - a < min_child_ms and spans:
            la, _lb = spans[-1]
            spans[-1] = (la, b)
            continue
        spans.append((a, b))
    if not spans:
        return [(start_ms, end_ms)]
    if len(spans) > max_children:
        head = spans[: max_children - 1]
        rest_end = spans[-1][1]
        head.append((head[-1][1], rest_end) if head else (start_ms, rest_end))
        return head
    return spans


def _speech_act_cut_ms(
    words: list[dict[str, Any]], start_ms: int, end_ms: int
) -> int | None:
    """Hinge at the first listener-CTA phrase in the window (depth-2 recovery)."""
    from interview_mux.homunculus.values import should_hard_omit_cta

    parts: list[tuple[int, str]] = []
    for w in words:
        try:
            ws = int(w.get("start_ms") or 0)
            we = int(w.get("end_ms") or ws)
        except (TypeError, ValueError):
            continue
        if we <= start_ms or ws >= end_ms:
            continue
        tok = _word_token(w)
        if tok:
            parts.append((ws, tok))
    for i, (ws, _tok) in enumerate(parts):
        for j in range(i, len(parts)):
            text = " ".join(t for _s, t in parts[i : j + 1])
            if not should_hard_omit_cta(text):
                continue
            if start_ms + MIN_CHILD_MS <= ws <= end_ms - MIN_CHILD_MS:
                return ws
            return None
    return None


def _classify_span(
    text: str,
    *,
    start_ms: int,
    end_ms: int,
    parent_start: int,
    parent_end: int,
    mixed: bool,
    region: str,
    ranking_cuts: list[int],
    ranking_whole: bool,
) -> str:
    from interview_mux.homunculus.values import should_hard_omit_cta

    if should_hard_omit_cta(text):
        return "dirty"
    if ranking_whole and not mixed:
        return "dirty"
    if mixed:
        if region == "end":
            cut = ranking_cuts[-1] if ranking_cuts else parent_start + (parent_end - parent_start) // 3 * 2
            return "dirty" if start_ms >= cut - 1 else "clean"
        if region == "start":
            cut = ranking_cuts[0] if ranking_cuts else parent_start + (parent_end - parent_start) // 3
            return "dirty" if end_ms <= cut + 1 else "clean"
        if region == "middle":
            if len(ranking_cuts) >= 2:
                lo, hi = ranking_cuts[0], ranking_cuts[-1]
            else:
                lo = parent_start + (parent_end - parent_start) // 3
                hi = parent_end - (parent_end - parent_start) // 3
            return "dirty" if start_ms >= lo - 1 and end_ms <= hi + 1 else "clean"
    return "clean"


def _already_pruned_parent(ctx: RunContext, sid: str) -> bool:
    if sid in never_touch_segment_ids(ctx):
        return True
    try:
        from interview_mux.nle_state import load_nle

        ov = ((load_nle(ctx).get("segment_overrides") or {}).get(sid) or {})
        if ov.get("split_into"):
            return True
    except Exception:
        pass
    return False


def _collect_cta_seeds(
    ctx: RunContext,
    artifacts: dict[str, Any],
    *,
    judgments: list[dict[str, Any]] | None = None,
) -> list[str]:
    """Union of ranking hits, host speech-acts, and editorial/sponsor issues."""
    from interview_mux.homunculus.values import should_hard_omit_cta

    out = dict(artifacts) if isinstance(artifacts, dict) else {}
    hits = judgments if judgments is not None else extract_judgments(out)
    seeds: list[str] = []
    for row in hits:
        sid = str(row.get("segment_id") or "").strip()
        if sid:
            seeds.append(sid)
    seeds.extend(_cta_like_excluded_ids(out))
    rationales = out.get("exclude_rationales") if isinstance(out.get("exclude_rationales"), dict) else {}
    for sid, reason in (rationales or {}).items():
        key = str(sid or "").strip()
        if key and is_editorial_exclude_reason(str(reason or "")):
            seeds.append(key)
    ordered = [str(s) for s in (out.get("ordered_segment_ids") or []) if s]
    must_keep: list[str] = []
    try:
        from interview_mux.hard_keep import hard_keep_segment_ids

        must_keep = [str(s) for s in (hard_keep_segment_ids(ctx) or []) if s]
    except Exception:
        must_keep = []
    by_id = _segments_by_id(ctx)
    for sid in list(dict.fromkeys([*ordered, *must_keep, *seeds])):
        text = str((by_id.get(sid) or {}).get("text") or "")
        if should_hard_omit_cta(text):
            seeds.append(sid)
    try:
        from interview_mux.homunculus.issues import read_issues

        for issue in read_issues(ctx):
            if not isinstance(issue, dict):
                continue
            kind = str(issue.get("kind") or "")
            if kind not in {
                "perspective_direct_monetization",
                "direct_listener_sponsor_promotion",
                "sponsor_bumper",
            } and "sponsor" not in kind and "monetization" not in kind:
                continue
            for sid in issue.get("implicated") or []:
                key = str(sid or "").strip()
                if key and _tape_is_hard_omit_cta(ctx, key, by_id):
                    seeds.append(key)
    except Exception:
        pass
    # Source-time order so later splits do not invalidate earlier windows.
    def _start(sid: str) -> int:
        try:
            return int((by_id.get(sid) or {}).get("start_ms") or 0)
        except (TypeError, ValueError):
            return 0

    return sorted(dict.fromkeys(s for s in seeds if s), key=_start)


def _split_parent(ctx: RunContext, segment_id: str, cuts: list[int]) -> list[str]:
    from interview_mux.nle_state import load_nle, split_segment_at_cuts
    from interview_mux.split_plan import clear_split_rerank_cascade

    split_segment_at_cuts(ctx, segment_id, cuts)
    try:
        clear_split_rerank_cascade(ctx)
    except Exception:
        pass
    nle = load_nle(ctx)
    children = [
        str(x)
        for x in ((nle.get("segment_overrides") or {}).get(segment_id) or {}).get("split_into") or []
        if x
    ]
    return children


def _prune_parent(
    ctx: RunContext,
    segment_id: str,
    *,
    judgment: dict[str, Any] | None = None,
    depth: int = 0,
    visited: set[str] | None = None,
) -> dict[str, Any]:
    """Partition one seed into complete thoughts; never-touch dirty; admit clean."""
    cfg = _prune_cfg()
    visited = visited if visited is not None else set()
    bounds = _parent_bounds(ctx, segment_id)
    if not bounds:
        return {"parent_id": segment_id, "ok": False, "reason": "missing_parent", "depth": depth}
    start, end = bounds
    min_child = cfg["min_child_ms"]
    if end - start < min_child * 2:
        return {"parent_id": segment_id, "ok": False, "reason": "too_short", "depth": depth}
    key = f"{segment_id}:{start}:{end}"
    if key in visited:
        return {"parent_id": segment_id, "ok": False, "reason": "visited", "depth": depth}
    visited.add(key)

    row = judgment if isinstance(judgment, dict) else {}
    mixed = _truthy(row.get("mixed_with_story")) or _truthy(row.get("must_keep_in_clip"))
    region = str(row.get("cta_region") or ("end" if mixed else "whole")).strip().lower()
    ranking_whole = (not mixed) and region == "whole"
    ranking_cuts = [int(c) for c in (row.get("cut_ms") or []) if str(c).strip() != ""]
    ranking_cuts = [c for c in ranking_cuts if start + min_child <= c <= end - min_child]
    words = _g0_words(ctx)
    extra = list(ranking_cuts)
    mixed_hinge = _mixed_story_cta_cut_ms(words, start, end) if mixed else None
    if mixed_hinge is not None:
        extra.append(int(mixed_hinge))
    if depth > 0:
        act_cut = _speech_act_cut_ms(words, start, end)
        if act_cut is not None:
            extra.append(act_cut)
    spans = _partition_complete_thoughts(
        words,
        start,
        end,
        min_child_ms=min_child,
        extra_cuts=extra,
        max_children=cfg["prune_max_children"],
        allow_short_extra=bool(mixed and mixed_hinge is not None),
    )
    by_id = _segments_by_id(ctx)
    parent_text = str((by_id.get(segment_id) or {}).get("text") or "")
    classified: list[dict[str, Any]] = []
    for a, b in spans:
        text = _span_text(words, a, b)
        if not text and len(spans) == 1:
            text = parent_text
        kind = _classify_span(
            text,
            start_ms=a,
            end_ms=b,
            parent_start=start,
            parent_end=end,
            mixed=mixed,
            region=region,
            ranking_cuts=ranking_cuts,
            ranking_whole=ranking_whole and len(spans) == 1,
        )
        classified.append({"start_ms": a, "end_ms": b, "class": kind, "text": text})

    dirty_n = sum(1 for s in classified if s["class"] == "dirty")
    clean_n = sum(1 for s in classified if s["class"] == "clean")
    tree = {
        "parent_id": segment_id,
        "depth": depth,
        "spans": [
            {"start_ms": s["start_ms"], "end_ms": s["end_ms"], "class": s["class"]}
            for s in classified
        ],
    }
    if dirty_n == 0:
        return {
            "parent_id": segment_id,
            "ok": True,
            "reason": "clean_only",
            "children": [],
            "cta_children": [],
            "story_children": [],
            "tree": tree,
        }
    if clean_n == 0:
        return {
            "parent_id": segment_id,
            "ok": False,
            "reason": "all_dirty",
            "children": [],
            "cta_children": [],
            "story_children": [],
            "tree": tree,
        }

    cuts = [s["start_ms"] for s in classified[1:]]
    cuts = [c for c in cuts if start < c < end]
    if not cuts:
        if depth + 1 < cfg["prune_max_depth"]:
            return _prune_parent(
                ctx,
                segment_id,
                judgment={**row, "mixed_with_story": True, "cta_region": region or "end"},
                depth=depth + 1,
                visited=visited,
            )
        return {
            "parent_id": segment_id,
            "ok": False,
            "reason": "still_mixed",
            "children": [],
            "cta_children": [],
            "story_children": [],
            "tree": tree,
        }
    try:
        children = _split_parent(ctx, segment_id, cuts)
    except Exception as exc:
        return {
            "parent_id": segment_id,
            "ok": False,
            "reason": "error",
            "detail": str(exc)[:240],
            "tree": tree,
        }
    if not children:
        return {
            "parent_id": segment_id,
            "ok": False,
            "reason": "no_cta_child",
            "tree": tree,
        }
    # Align children to spans by index; leftover hanging dirty → drop.
    n = min(len(children), len(classified))
    cta_kids: list[str] = []
    story_kids: list[str] = []
    nested: list[dict[str, Any]] = []
    for i in range(n):
        kid = children[i]
        kind = classified[i]["class"]
        if kind == "dirty":
            cta_kids.append(kid)
            continue
        child_row = _segments_by_id(ctx).get(kid) or {}
        child_text = str(child_row.get("text") or classified[i].get("text") or "")
        from interview_mux.homunculus.values import should_hard_omit_cta

        if should_hard_omit_cta(child_text):
            cta_kids.append(kid)
            continue
        story_kids.append(kid)
    tree["children"] = nested
    if not cta_kids and not story_kids:
        return {
            "parent_id": segment_id,
            "ok": False,
            "reason": "no_cta_child",
            "children": children,
            "tree": tree,
        }
    return {
        "parent_id": segment_id,
        "ok": True,
        "children": children,
        "cta_children": list(dict.fromkeys(cta_kids)),
        "story_children": list(dict.fromkeys(story_kids)),
        "tree": tree,
        "cut_ms": cuts,
        "depth": depth,
    }


def run_cta_prune(
    ctx: RunContext,
    artifacts: dict[str, Any] | None,
    *,
    judgments: list[dict[str, Any]] | None = None,
    notes: list[str] | None = None,
) -> dict[str, Any]:
    """Scan 0–N seeds, prune each, rescan. Writes mastering/media_ip_cta.json."""
    out = dict(artifacts) if isinstance(artifacts, dict) else {}
    pre_ordered = [str(s) for s in (out.get("ordered_segment_ids") or []) if s]
    cfg = _prune_cfg()
    notes = list(notes or [])
    hits = judgments if judgments is not None else extract_judgments(out)
    by_judgment = {
        str(r.get("segment_id") or ""): r
        for r in hits
        if isinstance(r, dict) and r.get("segment_id")
    }
    dropped: list[str] = []
    recuts: list[dict[str, Any]] = []
    trees: list[dict[str, Any]] = []
    id_map: dict[str, list[str]] = {}
    recut_texts: list[str] = []
    processed: set[str] = set()
    seed_ids: list[str] = []
    cta_open_parent: str | None = None
    open_choice = ""

    for _pass in range(max(1, cfg["prune_max_seed_passes"])):
        seeds = _collect_cta_seeds(ctx, out, judgments=hits)
        new_seeds = [
            s for s in seeds if s not in processed and not _already_pruned_parent(ctx, s)
        ]
        if not new_seeds:
            break
        if not seed_ids:
            seed_ids = list(seeds)
        else:
            seed_ids = list(dict.fromkeys([*seed_ids, *new_seeds]))
        for sid in new_seeds:
            processed.add(sid)
            row = by_judgment.get(sid) or {}
            if _truthy(row.get("cta_open")) and cta_open_parent is None:
                cta_open_parent = sid
                open_choice = str(row.get("open_choice") or "").strip()
            recut = _prune_parent(ctx, sid, judgment=row, depth=0)
            trees.append(recut.get("tree") or {"parent_id": sid, "ok": recut.get("ok")})
            if recut.get("reason") == "clean_only":
                notes.append(f"seed_clean_only:{sid}")
                recuts.append(recut)
                continue
            if not recut.get("ok"):
                notes.append(f"recut_unclean:{sid}" if recut.get("reason") != "error" else f"recut_failed:{sid}:{recut.get('reason')}")
                dropped.append(sid)
                recuts.append(
                    {
                        "parent_id": sid,
                        "ok": False,
                        "reason": recut.get("reason") or "unclean",
                        "detail": recut.get("detail"),
                    }
                )
                continue
            children = [str(c) for c in (recut.get("children") or []) if c]
            cta_kids = [str(c) for c in (recut.get("cta_children") or []) if c]
            story_kids = [str(c) for c in (recut.get("story_children") or []) if c]
            if not cta_kids and children:
                notes.append(f"recut_unclean:{sid}")
                dropped.append(sid)
                recuts.append({"parent_id": sid, "ok": False, "reason": "no_cta_child"})
                continue
            if children:
                id_map[sid] = children
            dropped.extend(cta_kids)
            dropped.append(sid)
            recut_texts.extend(_texts_for(ctx, cta_kids))
            _exclude_nle_ids(ctx, cta_kids)
            recuts.append(
                {
                    "parent_id": sid,
                    "ok": True,
                    "children": children,
                    "cta_children": cta_kids,
                    "story_children": story_kids,
                    "cut_ms": recut.get("cut_ms"),
                    "depth": recut.get("depth") or 0,
                }
            )

    dropped = list(dict.fromkeys(dropped))
    recut_parents = _recut_parent_ids(recuts)
    story_ids = _story_ids_from_recuts(recuts)
    for recut in recuts:
        if not isinstance(recut, dict) or not recut.get("ok"):
            continue
        parent = str(recut.get("parent_id") or "")
        kids = [str(c) for c in (recut.get("children") or []) if c]
        if parent and kids:
            _persist_recut_children(ctx, parent, kids, story_ids)
    ordered = [str(s) for s in (out.get("ordered_segment_ids") or []) if s]
    if id_map:
        ordered = _rewrite_order(ordered, id_map)

    drop_set = set(dropped)
    story_first = ""
    if cta_open_parent:
        recut = next((r for r in recuts if r.get("parent_id") == cta_open_parent and r.get("ok")), None)
        story_kids = list((recut or {}).get("story_children") or [])
        if open_choice == "story_child_first" and story_kids:
            story_first = story_kids[0]
        elif not open_choice and story_kids:
            story_first = story_kids[0]
            open_choice = "story_child_first"
        elif not open_choice:
            open_choice = "third_person_opener"
        notes.append(f"cta_open:{cta_open_parent}:{open_choice or 'third_person_opener'}")

    cover_targets = _cover_targets(ordered, dropped, id_map)
    ordered = [s for s in ordered if s not in drop_set]
    ordered = _admit_story_ids(ctx, ordered, story_ids, drop_set)
    if story_first and story_first in ordered:
        ordered = [story_first] + [s for s in ordered if s != story_first]
    elif story_first and story_first not in drop_set:
        ordered = [story_first] + ordered

    leftover_ranking_cta = [
        sid for sid in _cta_like_excluded_ids(out) if sid not in drop_set and sid not in set(story_ids)
    ]
    dropped = list(dict.fromkeys([*dropped, *leftover_ranking_cta]))
    drop_set = set(dropped) | never_touch_segment_ids(ctx)
    dropped = list(dict.fromkeys([*dropped, *sorted(drop_set)]))
    ordered = [s for s in ordered if s not in drop_set]
    never_touch = list(dict.fromkeys([*dropped, *recut_parents]))

    excl = _without_excluded_ids(list(out.get("excluded_segment_ids") or []), set(story_ids))
    excl = _stamp_excludes(excl, dropped + recut_parents)

    if not ordered:
        notes.append("drop_emptied_selection")
        fallback = [str(s) for s in (out.get("ordered_segment_ids") or []) if s and s not in drop_set]
        ordered = fallback or [str(s) for s in (out.get("ordered_segment_ids") or []) if s][:1]
        ordered = _admit_story_ids(ctx, ordered, story_ids, drop_set)

    out["ordered_segment_ids"] = ordered
    out["excluded_segment_ids"] = excl
    # CTA↔floor: do not drop the only live targets of hosted VO floor lines.
    out = restore_floor_anchor_natives(ctx, pre_ordered, out)
    # FMR S2: CTA omit must not drop enforceable primary-impact tape.
    try:
        from interview_mux.framing_coverage_guard import (
            restore_enforceable_primary_impact_natives,
        )

        out = restore_enforceable_primary_impact_natives(ctx, pre_ordered, out)
    except Exception:
        pass
    ordered = [str(s) for s in (out.get("ordered_segment_ids") or []) if s]
    excl = list(out.get("excluded_segment_ids") or [])
    out["media_ip_cta"] = hits
    if story_ids:
        out["admitted_story_segment_ids"] = list(story_ids)
        out["considerable_segment_ids"] = list(story_ids)
    _rewrite_chapter_ids(out, id_map, drop_set, story_ids)
    _sync_cold_open(out, drop_set, id_map, ordered)

    texts = list(dict.fromkeys([*recut_texts, *_texts_for(ctx, never_touch)]))
    if not seed_ids and not dropped:
        notes = list(dict.fromkeys([*notes, "no_clear_media_ip_cta"]))
    state = {
        "version": 1,
        "locked": bool(dropped or recuts),
        "judgments": hits,
        "dropped_segment_ids": dropped,
        "never_touch_segment_ids": never_touch,
        "never_touch_texts": texts,
        "cover_target_ids": cover_targets,
        "admitted_story_segment_ids": story_ids,
        "considerable_segment_ids": list(story_ids),
        "recuts": recuts,
        "cta_open_parent": cta_open_parent,
        "open_choice": open_choice or None,
        "notes": notes[-48:],
        "prune_tree": trees,
        "seed_ids": seed_ids,
        "seed_count": len(seed_ids),
    }
    prev = load_state(ctx)
    if isinstance(prev, dict) and prev.get("locked"):
        state["locked"] = True
        state["dropped_segment_ids"] = list(
            dict.fromkeys([*(prev.get("dropped_segment_ids") or []), *dropped])
        )
        state["never_touch_segment_ids"] = list(
            dict.fromkeys([*(prev.get("never_touch_segment_ids") or []), *never_touch])
        )
        state["never_touch_texts"] = list(
            dict.fromkeys([*(prev.get("never_touch_texts") or []), *texts])
        )
        if prev.get("cover_target_ids") and not cover_targets:
            state["cover_target_ids"] = list(prev.get("cover_target_ids") or [])
        if prev.get("cta_open_parent") and not cta_open_parent:
            state["cta_open_parent"] = prev.get("cta_open_parent")
            state["open_choice"] = prev.get("open_choice")
        if prev.get("judgments") and not hits:
            state["judgments"] = list(prev.get("judgments") or [])
        merged_recuts = list(prev.get("recuts") or [])
        have_p = {str(r.get("parent_id") or "") for r in merged_recuts if isinstance(r, dict)}
        for recut in recuts:
            pid = str((recut or {}).get("parent_id") or "")
            if pid and pid not in have_p:
                merged_recuts.append(recut)
                have_p.add(pid)
        state["recuts"] = merged_recuts
        state["prune_tree"] = list(prev.get("prune_tree") or []) + trees
        state["seed_ids"] = list(dict.fromkeys([*(prev.get("seed_ids") or []), *seed_ids]))
        state["seed_count"] = len(state["seed_ids"])
        state["notes"] = list(dict.fromkeys([*(prev.get("notes") or []), *notes]))[-48:]
        state["admitted_story_segment_ids"] = list(
            dict.fromkeys(
                [
                    *(prev.get("admitted_story_segment_ids") or []),
                    *story_ids,
                    *_story_ids_from_recuts(merged_recuts),
                ]
            )
        )
        state["considerable_segment_ids"] = list(state["admitted_story_segment_ids"])
    _write_state(ctx, state)
    return out


def heal_on_air_cta_residue(
    ctx: RunContext, artifacts: dict[str, Any] | None = None
) -> dict[str, Any]:
    """Blocking late gate: prune every remaining ordered clip that still has a listener CTA."""
    out = dict(artifacts) if isinstance(artifacts, dict) else {}
    if not out and ctx.artifact_exists("master/selection.json"):
        loaded = ctx.read_json("master/selection.json")
        out = dict(loaded) if isinstance(loaded, dict) else {}
    if not enabled(ctx):
        return out
    released = release_false_cta_never_touch(ctx)
    from interview_mux.homunculus.values import should_hard_omit_cta

    ordered = [str(s) for s in (out.get("ordered_segment_ids") or []) if s]
    ordered = _restore_released_to_order(ctx, ordered, released)
    if released:
        out["ordered_segment_ids"] = ordered
    by_id = _segments_by_id(ctx)
    never_touch = never_touch_segment_ids(ctx)
    stripped_never_touch = False
    if never_touch:
        before_nt = list(ordered)
        kept = [sid for sid in ordered if sid not in never_touch]
        if kept != ordered:
            out["ordered_segment_ids"] = kept
            out = restore_floor_anchor_natives(ctx, before_nt, out)
            ordered = [str(s) for s in (out.get("ordered_segment_ids") or []) if s]
            stripped_never_touch = True
    excluded_ids: set[str] = set(never_touch)
    for row in out.get("excluded_segment_ids") or []:
        if isinstance(row, dict):
            excluded_ids.add(str(row.get("segment_id") or ""))
        else:
            excluded_ids.add(str(row or ""))
    empty_shells = [
        sid
        for sid in ordered
        if not str((by_id.get(sid) or {}).get("text") or "").strip()
        and any(_is_nle_child(other, sid) for other in excluded_ids if other)
    ]
    if empty_shells:
        ordered = [sid for sid in ordered if sid not in set(empty_shells)]
        out["ordered_segment_ids"] = ordered
        stripped_never_touch = True
    scrap_kids = on_air_orphaned_cta_scrap_ids(ctx, out)
    if scrap_kids:
        drop = set(scrap_kids)
        ordered = [sid for sid in ordered if sid not in drop]
        out["ordered_segment_ids"] = ordered
        rationales = (
            dict(out.get("exclude_rationales") or {})
            if isinstance(out.get("exclude_rationales"), dict)
            else {}
        )
        excl = list(out.get("excluded_segment_ids") or [])
        have = {
            str(row.get("segment_id") if isinstance(row, dict) else row) for row in excl
        }
        for sid in scrap_kids:
            rationales[sid] = "media_ip_cta"
            if sid not in have:
                excl.append({"segment_id": sid, "reason": "media_ip_cta"})
        out["exclude_rationales"] = rationales
        out["excluded_segment_ids"] = excl
        if ctx.artifact_exists("master/selection.json"):
            from interview_mux.air_order_boundary import commit_selection_mutation

            commit_selection_mutation(
                ctx,
                out,
                producer="media_ip_cta.orphaned_cta_child_omit",
                stage_key=_selection_commit_stage_key(),
                checkpoint_mode="detect",
                skip_checkpoint=True,
                write_committed=True,
            )
        return out
    residue = [
        sid
        for sid in ordered
        if should_hard_omit_cta(str((by_id.get(sid) or {}).get("text") or ""))
    ]
    if not residue:
        if stripped_never_touch and ctx.artifact_exists("master/selection.json"):
            from interview_mux.air_order_boundary import commit_selection_mutation

            commit_selection_mutation(
                ctx,
                out,
                producer="media_ip_cta.heal_on_air_cta_residue",
                stage_key=_selection_commit_stage_key(),
                checkpoint_mode="detect",
                skip_checkpoint=True,
                write_committed=True,
            )
        return out
    before = list(ordered)
    out = run_cta_prune(ctx, out, notes=["layup_residue_scan"])
    after = [str(s) for s in (out.get("ordered_segment_ids") or []) if s]
    if before != after and ctx.artifact_exists("master/selection.json"):
        from interview_mux.air_order_boundary import commit_selection_mutation

        commit_selection_mutation(
            ctx,
            out,
            producer="media_ip_cta.heal_on_air_cta_residue",
            stage_key=_selection_commit_stage_key(),
            checkpoint_mode="detect",
            skip_checkpoint=True,
            write_committed=True,
        )
    return out


def _reapply_locked(
    ctx: RunContext, artifacts: dict[str, Any], prev: dict[str, Any]
) -> dict[str, Any]:
    """Keep previously chosen CTA drops so --from-stage does not retarget."""
    dropped = [str(x) for x in (prev.get("dropped_segment_ids") or []) if x]
    drop_set = set(dropped) | {
        str(x) for x in (prev.get("never_touch_segment_ids") or []) if x
    }
    recuts = list(prev.get("recuts") or [])
    id_map = _id_map_from_recuts(recuts)
    story_ids = _story_ids_from_recuts(recuts)
    recut_parents = _recut_parent_ids(recuts)
    ordered = [str(s) for s in (artifacts.get("ordered_segment_ids") or []) if s]
    if id_map:
        ordered = _rewrite_order(ordered, id_map)
    ordered = [s for s in ordered if s not in drop_set]
    ordered = _admit_story_ids(ctx, ordered, story_ids, drop_set)
    story_first = _story_first_from_state(prev)
    if story_first and story_first in ordered:
        ordered = [story_first] + [s for s in ordered if s != story_first]
    elif story_first and story_first not in drop_set:
        ordered = [story_first] + ordered
    excl = _without_excluded_ids(list(artifacts.get("excluded_segment_ids") or []), set(story_ids))
    artifacts["ordered_segment_ids"] = ordered
    artifacts["excluded_segment_ids"] = _stamp_excludes(excl, dropped + recut_parents)
    artifacts["media_ip_cta"] = extract_judgments(
        {"media_ip_cta": list(prev.get("judgments") or [])}
    )
    if story_ids:
        artifacts["admitted_story_segment_ids"] = list(story_ids)
        artifacts["considerable_segment_ids"] = list(story_ids)
    _rewrite_chapter_ids(artifacts, id_map, drop_set, story_ids)
    _sync_cold_open(artifacts, drop_set, id_map, ordered)
    for recut in recuts:
        if not isinstance(recut, dict) or not recut.get("ok"):
            continue
        parent = str(recut.get("parent_id") or "")
        kids = [str(c) for c in (recut.get("children") or []) if c]
        if parent and kids:
            _persist_recut_children(ctx, parent, kids, story_ids)
    return artifacts


def normalize_media_ip_cta_rows(selection: dict[str, Any]) -> dict[str, Any]:
    """Coerce selection.media_ip_cta to master_selection schema before commit.

    exec_13183: schema refused missing ``clearly_media_ip_pitch`` and illegal
    extras (``action``, ``reason``, …). Normalize once so commit admits; second
    failure still refuses via schema validate.
    """
    out = dict(selection or {})
    raw = out.get("media_ip_cta")
    if not isinstance(raw, list):
        return out
    allow = {
        "segment_id",
        "clearly_media_ip_pitch",
        "mixed_with_story",
        "must_keep_in_clip",
        "cta_region",
        "cut_ms",
        "cta_open",
        "open_choice",
    }
    cleaned: list[dict[str, Any]] = []
    for row in raw:
        if not isinstance(row, dict):
            continue
        sid = str(row.get("segment_id") or "").strip()
        if not sid:
            continue
        item: dict[str, Any] = {"segment_id": sid}
        if "clearly_media_ip_pitch" in row:
            item["clearly_media_ip_pitch"] = bool(_truthy(row.get("clearly_media_ip_pitch")))
        else:
            # Fail-closed: omit/recut intent without pitch flag → not a clear pitch.
            action = str(row.get("action") or "").strip().lower()
            if action in {"omit", "drop", "exclude"}:
                item["clearly_media_ip_pitch"] = True
            elif any(k in row for k in ("cta_region", "cut_ms", "mixed_with_story")):
                item["clearly_media_ip_pitch"] = True
            else:
                item["clearly_media_ip_pitch"] = False
        for key in allow - {"segment_id", "clearly_media_ip_pitch"}:
            if key not in row:
                continue
            val = row.get(key)
            if val is None:
                # The LLM envelope allows null; the selection schema does not.
                # Copied through, it refused the selection commit and the lock
                # re-applied it on every retry (ISSUES 169).
                continue
            if key in {"mixed_with_story", "must_keep_in_clip", "cta_open"}:
                item[key] = bool(_truthy(val))
                continue
            if key == "cut_ms":
                if isinstance(val, (int, float)):
                    item[key] = [int(val)]
                elif isinstance(val, list):
                    item[key] = [int(x) for x in val if isinstance(x, (int, float))]
            elif key == "cta_region":
                region = str(val).strip().lower()
                if region in {"whole", "start", "end", "middle"}:
                    item[key] = region
                continue
            elif key == "open_choice":
                choice = str(val).strip().lower()
                if choice in {"story_child_first", "third_person_opener"}:
                    item[key] = choice
                continue
            else:
                item[key] = val
        cleaned.append(item)
    out["media_ip_cta"] = cleaned
    return out


def extract_judgments(payload: dict[str, Any] | None) -> list[dict[str, Any]]:
    """Keep only clearly flagged pitches. Unsure / missing → keep native."""
    if not isinstance(payload, dict):
        return []
    raw = payload.get("media_ip_cta")
    if isinstance(raw, dict):
        raw = raw.get("hits") or raw.get("judgments") or []
    if not isinstance(raw, list):
        return []
    # Normalize schema shape first (commit path + judgment extract).
    norm = normalize_media_ip_cta_rows({"media_ip_cta": raw})
    raw = list(norm.get("media_ip_cta") or [])
    out: list[dict[str, Any]] = []
    for row in raw:
        if not isinstance(row, dict):
            continue
        sid = str(row.get("segment_id") or "")
        if not sid:
            continue
        if not _truthy(row.get("clearly_media_ip_pitch")):
            continue
        out.append(dict(row))
    return out


def apply_cta_judgments(ctx: RunContext, artifacts: dict[str, Any] | None) -> dict[str, Any]:
    """Execute flagship CTA calls on selection. Homunculus 0.1.0 only."""
    out = dict(artifacts) if isinstance(artifacts, dict) else {}
    if not enabled(ctx):
        return out
    prev = load_state(ctx)
    if prev.get("locked") and prev.get("dropped_segment_ids"):
        return _reapply_locked(ctx, out, prev)
    judgments = extract_judgments(out)
    # Prefer-drop-when-unsure (§7B): ambiguous/clear-pitch → omit; never hard-keeps.
    # Do not override LLM/flagship judgments already present (esp. mixed story+CTA
    # parents that must recut rather than wholesale-omit — story children stay on air).
    judged_ids = {
        str(j.get("segment_id") or "")
        for j in judgments
        if isinstance(j, dict) and j.get("segment_id")
    }
    try:
        from interview_mux.hard_keep import hard_keep_segment_ids
        from interview_mux.homunculus.values import should_hard_omit_cta

        keeps = set(hard_keep_segment_ids(ctx) or set())
        try:
            from interview_mux.framing_coverage_guard import enforceable_primary_impact_ids

            keeps |= enforceable_primary_impact_ids(ctx, out)
        except Exception:
            pass
        by_id = _segments_by_id(ctx)
        for sid, row in by_id.items():
            if sid in keeps or sid in judged_ids:
                continue
            text = str((row or {}).get("text") or "")
            if text and should_hard_omit_cta(text):
                judgments.append(
                    {
                        "segment_id": sid,
                        "action": "omit",
                        "reason": "deterministic_cta_prefer_drop",
                        "confidence": "high",
                    }
                )
    except Exception:
        pass
    out = run_cta_prune(ctx, out, judgments=judgments)
    state = load_state(ctx)
    dropped = list(state.get("dropped_segment_ids") or [])
    recuts = list(state.get("recuts") or [])
    cover_targets = list(state.get("cover_target_ids") or [])
    ctx.log(
        "media_ip_cta: dropped "
        f"{len(dropped)} native(s); recuts={sum(1 for r in recuts if r.get('ok'))}",
        level="info",
        stage="full_master_ranking",
        detail={"dropped_segment_ids": dropped[:12], "cover_target_ids": cover_targets[:12]},
    )
    return out


def _tape_is_hard_omit_cta(ctx: RunContext, sid: str, by_id: dict[str, dict[str, Any]] | None = None) -> bool:
    """True only when this id's native tape is a listener CTA / sponsor bumper."""
    from interview_mux.homunculus.values import should_hard_omit_cta

    rows = by_id if by_id is not None else _segments_by_id(ctx)
    text = str((rows.get(sid) or {}).get("text") or "")
    return bool(text and should_hard_omit_cta(text))


def _is_nle_child(sid: str, parent: str) -> bool:
    """True when sid is an NLE letter-suffix child of parent (seg_074b of seg_074)."""
    if not sid or not parent or sid == parent or not sid.startswith(parent):
        return False
    rest = sid[len(parent) :]
    return bool(rest) and rest[0].isalpha()


_SCRAP_TOKEN_RE = re.compile(
    r"\b(sponsor|agilisium|fullview|podcast|platform|collective|subscribe|"
    r"episodes|newsletter|patreon|promo|remind)\b",
    flags=re.IGNORECASE,
)


def looks_like_orphaned_cta_scrap(text: str) -> bool:
    """Short sponsor/credit/incomplete-CTA leftover — not mixed-recut story."""
    from interview_mux.homunculus.values import should_hard_omit_cta

    raw = str(text or "").strip()
    if not raw:
        return True
    if should_hard_omit_cta(raw):
        return True
    if _looks_like_degraded_signoff(raw):
        return True
    if "\ufffd" in raw:
        # Undecodable STT output (exec_010 seg_038k "usHS\ufffd bone…") is never story.
        return True
    from interview_mux.homunculus.values import normalize_omit_text

    words = normalize_omit_text(raw).split()
    if len(words) <= 16 and _SCRAP_TOKEN_RE.search(raw):
        return True
    collapsed = re.sub(r"\s+", "", raw.lower())
    if len(words) <= 16 and any(
        tld in collapsed for tld in (".com", ".net", ".org", ".io")
    ):
        return True
    if raw.rstrip(" .").endswith(" by"):
        return True
    return False


def _cta_exclude_parent_ids(selection: dict[str, Any], extra: set[str] | None = None) -> set[str]:
    parents: set[str] = set(extra or ())
    rationales = (
        selection.get("exclude_rationales")
        if isinstance(selection.get("exclude_rationales"), dict)
        else {}
    )
    for row in selection.get("excluded_segment_ids") or []:
        if isinstance(row, dict):
            sid = str(row.get("segment_id") or "").strip()
            reason = str(row.get("reason") or rationales.get(sid) or "")
        else:
            sid = str(row or "").strip()
            reason = str(rationales.get(sid) or "")
        if sid and is_editorial_exclude_reason(reason):
            parents.add(sid)
    return parents


def on_air_orphaned_cta_scrap_ids(
    ctx: RunContext, selection: dict[str, Any] | None = None
) -> list[str]:
    """On-air NLE children of excluded CTA parents that are leftover scraps."""
    sel = selection
    if sel is None:
        if not ctx.artifact_exists("master/selection.json"):
            return []
        loaded = ctx.read_json("master/selection.json")
        sel = loaded if isinstance(loaded, dict) else {}
    if not isinstance(sel, dict):
        return []
    ordered = [str(s) for s in (sel.get("ordered_segment_ids") or []) if s]
    parents = _cta_exclude_parent_ids(sel)
    try:
        parents |= never_touch_segment_ids(ctx)
    except Exception:
        pass
    if not ordered or not parents:
        return []
    try:
        story = set(admitted_story_segment_ids(ctx) or [])
    except Exception:
        story = set()
    by_id = _segments_by_id(ctx)
    tail = _closing_outro_tail_ids(by_id, ordered, parents, sel, _all_segments_by_id(ctx)) | _tape_tail_scrap_ids(
        by_id, ordered, parents, _all_segments_by_id(ctx)
    )
    out: list[str] = []
    for sid in ordered:
        if sid in tail:
            out.append(sid)
            continue
        if sid in story:
            continue
        if not any(_is_nle_child(sid, parent) for parent in parents if parent):
            continue
        text = str((by_id.get(sid) or {}).get("text") or "")
        if looks_like_orphaned_cta_scrap(text):
            out.append(sid)
    return out


def closing_outro_tail_segment_ids(
    ctx: RunContext, selection: dict[str, Any] | None = None
) -> set[str]:
    """On-air children of a tape-closing CTA parent after its excluded sponsor reads."""
    sel = selection
    if sel is None:
        if not ctx.artifact_exists("master/selection.json"):
            return set()
        loaded = ctx.read_json("master/selection.json")
        sel = loaded if isinstance(loaded, dict) else {}
    if not isinstance(sel, dict):
        return set()
    ordered = [str(s) for s in (sel.get("ordered_segment_ids") or []) if s]
    parents = _cta_exclude_parent_ids(sel)
    try:
        parents |= never_touch_segment_ids(ctx)
    except Exception:
        pass
    if not ordered or not parents:
        return set()
    by_id = _segments_by_id(ctx)
    return _closing_outro_tail_ids(by_id, ordered, parents, sel, _all_segments_by_id(ctx)) | _tape_tail_scrap_ids(
        by_id, ordered, parents, _all_segments_by_id(ctx)
    )


def _tape_tail_scrap_ids(
    by_id: dict[str, Any],
    ordered: list[str],
    parents: set[str],
    parent_rows: dict[str, Any] | None = None,
) -> set[str]:
    """Top-level on-air scraps that start after the tape's closing sponsor read.

    The child-based checks only look inside CTA parents. A garbled sign-off
    cut as its own segment after the closing sponsor parent (exec_020:
    seg_050 "You are listening to usHS\ufffd bone and cut-" after seg_049)
    stayed on air as the last thing in the episode (ISSUES 170). Only text
    that fails ``looks_like_orphaned_cta_scrap`` is taken, so closing story
    after the outro stays.
    """
    rows = [r for r in by_id.values() if isinstance(r, dict)]
    rows += [r for r in (parent_rows or {}).values() if isinstance(r, dict)]
    ends = [int(r.get("end_ms") or 0) for r in rows]
    if not ends:
        return set()
    tape_end = max(ends)
    closing_start: int | None = None
    for parent in parents:
        # The live NLE view drops excluded rows, and the closing sponsor read
        # is excluded: without the manifest row there was no closing_start and
        # nothing after the outro was ever a tail scrap (exec_026 seg_033,
        # ISSUES 180).
        row = by_id.get(parent)
        if not isinstance(row, dict) or row.get("start_ms") is None:
            row = (parent_rows or {}).get(parent)
        if not isinstance(row, dict) or row.get("start_ms") is None:
            continue
        if tape_end - int(row.get("end_ms") or 0) > 120_000:
            continue
        start = int(row.get("start_ms") or 0)
        closing_start = start if closing_start is None else max(closing_start, start)
    if closing_start is None:
        return set()
    out: set[str] = set()
    for sid in ordered:
        if sid in parents or any(_is_nle_child(sid, p) for p in parents if p):
            continue
        row = by_id.get(sid)
        if not isinstance(row, dict) or row.get("start_ms") is None:
            row = (parent_rows or {}).get(sid)
        if not isinstance(row, dict) or row.get("start_ms") is None:
            continue
        if int(row.get("start_ms") or 0) < closing_start:
            continue
        if looks_like_orphaned_cta_scrap(str(row.get("text") or "")):
            out.add(sid)
    return out


def _split_suffix_key(sid: str, parent: str) -> tuple[int, str]:
    suffix = str(sid)[len(str(parent)) :]
    return (len(suffix), suffix)


def _closing_outro_tail_ids(
    by_id: dict[str, Any],
    ordered: list[str],
    parents: set[str],
    selection: dict[str, Any],
    all_rows: dict[str, Any] | None = None,
) -> set[str]:
    """Children after the sponsor read in an excluded CTA parent that closes the tape.

    A recut admits the non-CTA remainder of a CTA parent as story. When that
    parent is the last thing on the tape, the remainder after the excluded
    sponsor/credit reads is the sign-off and credits, not story (exec_010:
    seg_038a-f excluded as media_ip_cta; g-k "The Life Sciences DNA.", "I'm
    Daniel Levine. Thanks for joining us.", corrupt fragments stayed on air
    and nugget_layup_compose refused them as post-roll on every attempt).
    Excluded siblings are often gone from the live manifest, so they are read
    from the selection and ordered by split suffix (a, b, ... aa).
    """
    # The tape's real end includes excluded closing material; the live view
    # alone understates it and lets a mid-tape parent pass as closing (ISSUES 180).
    ends = [
        int((row or {}).get("end_ms") or 0)
        for row in [*by_id.values(), *(all_rows or {}).values()]
        if isinstance(row, dict)
    ]
    if not ends:
        return set()
    tape_end = max(ends)
    rationales = (
        selection.get("exclude_rationales")
        if isinstance(selection.get("exclude_rationales"), dict)
        else {}
    )
    excluded: set[str] = set()
    for row in selection.get("excluded_segment_ids") or []:
        if isinstance(row, dict):
            sid = str(row.get("segment_id") or "")
            reason = str(row.get("reason") or rationales.get(sid) or "")
        else:
            sid = str(row or "")
            reason = str(rationales.get(sid) or "")
        if sid and is_editorial_exclude_reason(reason):
            excluded.add(sid)
    out: set[str] = set()
    roots = {p for p in parents if p and not any(_is_nle_child(p, q) for q in parents if q and q != p)}
    for parent in roots:
        on_air = [sid for sid in ordered if sid != parent and _is_nle_child(sid, parent)]
        gone = [sid for sid in excluded if sid != parent and _is_nle_child(sid, parent)]
        if not on_air or not gone:
            continue
        if tape_end - max(int((by_id.get(k) or {}).get("end_ms") or 0) for k in on_air) > 5000:
            continue
        # After the sponsor read *begins*, not after the last excluded piece:
        # garbled sign-off pieces can be excluded on both sides of one that
        # stayed (exec_022: seg_031a-h and j-k excluded, seg_031i "You most of
        # the time, Michael." aired as the last clip; ISSUES 174). Story
        # children before the sponsor read stay.
        first_gone = min(_split_suffix_key(k, parent) for k in gone)
        out |= {k for k in on_air if _split_suffix_key(k, parent) > first_gone}
    return out

def _reverse_jump_keep_ids(reason: str) -> set[str]:
    """Air-order destinations named only as reverse-jump landings — do not omit."""
    text = str(reason or "")
    dests = {m.group(1) for m in _REVERSE_JUMP_INTO_RE.finditer(text)}
    dests |= {m.group(1) for m in _REVERSE_JUMP_ARROW_RE.finditer(text)}
    return dests


def _active_floor_target_ids(ctx: RunContext) -> list[str]:
    """targets_segment_id for active synthesize-family gap lines (floor count)."""
    if not ctx.artifact_exists("understanding/gap_report.json"):
        return []
    try:
        gap = ctx.read_json("understanding/gap_report.json")
    except Exception:
        return []
    if not isinstance(gap, dict):
        return []
    out: list[str] = []
    for row in gap.get("interviewer_lines") or []:
        if not isinstance(row, dict):
            continue
        if row.get("skipped_optional") or row.get("omit") or row.get("air_script_omit"):
            continue
        delivery = str(row.get("delivery") or "").strip().lower()
        if delivery not in {"synthesize", "chatterbox", "record", "mlx_audio"}:
            continue
        tid = str(row.get("targets_segment_id") or "").strip()
        if tid:
            out.append(tid)
    return out


def floor_anchor_keep_ids(ctx: RunContext, proposed_ordered: list[str]) -> set[str]:
    """Natives that must stay on air so hosted VO floor targets remain live.

    exec_13177 residual: CTA prune dropped floor-line targets →
    ``_framing_floor_topup`` skipped ``tid not in live`` → hosted_vo_floor_unmet.
    """
    try:
        from interview_mux.gap_fill_eligibility import (
            hosted_framing_requires_synthetic_vo,
            min_synthetic_vo_lines,
        )
    except Exception:
        return set()
    try:
        if not hosted_framing_requires_synthetic_vo(ctx):
            return set()
        need = int(min_synthetic_vo_lines(ctx) or 0)
    except Exception:
        return set()
    if need <= 0:
        return set()
    live = {str(s) for s in proposed_ordered if s}
    targets = _active_floor_target_ids(ctx)
    if not targets:
        return set()
    live_count = sum(1 for t in targets if t in live)
    if live_count >= need:
        return set()
    keep: set[str] = set()
    shortfall = need - live_count
    # Prefer restoring unique floor targets (stable order of appearance).
    seen: set[str] = set()
    for tid in targets:
        if shortfall <= 0:
            break
        if not tid or tid in live or tid in seen:
            continue
        keep.add(tid)
        seen.add(tid)
        shortfall -= 1
    return keep


def restore_floor_anchor_natives(
    ctx: RunContext,
    before_ordered: list[str],
    after: dict[str, Any],
) -> dict[str, Any]:
    """Re-admit floor-anchor natives CTA prune removed when that would starve the floor."""
    out = dict(after) if isinstance(after, dict) else {}
    after_ids = [str(s) for s in (out.get("ordered_segment_ids") or []) if s]
    keep = floor_anchor_keep_ids(ctx, after_ids)
    # Only restore ids that were on air before this prune.
    before_set = {str(s) for s in before_ordered if s}
    keep &= before_set
    if not keep:
        return out
    # Rebuild ordered: previous relative order, then any new admissions after.
    restored = [sid for sid in before_ordered if sid in keep or sid in set(after_ids)]
    # Append after-only ids (story children etc.) preserving their relative order.
    have = set(restored)
    for sid in after_ids:
        if sid not in have:
            restored.append(sid)
            have.add(sid)
    if restored == after_ids:
        return out
    out["ordered_segment_ids"] = restored
    # Pull restored anchors out of excluded so membership stays coherent.
    keep_excl: list[Any] = []
    for row in out.get("excluded_segment_ids") or []:
        sid = str(row.get("segment_id") if isinstance(row, dict) else row or "").strip()
        if sid and sid in keep:
            continue
        keep_excl.append(row)
    out["excluded_segment_ids"] = keep_excl
    rationales = out.get("exclude_rationales")
    if isinstance(rationales, dict):
        for sid in keep:
            rationales.pop(sid, None)
        out["exclude_rationales"] = rationales
    try:
        ctx.log(
            "cta_omit_refused_floor_anchor: kept "
            f"{sorted(keep)[:12]} so hosted VO floor targets stay live",
            level="warning",
            stage=_selection_commit_stage_key(),
            detail={"kept": sorted(keep)[:24]},
        )
    except Exception:
        pass
    return out


def _ids_from_cta_need_reason(reason: str, ordered: list[str]) -> list[str]:
    """Segment ids named in a need, including 'seg_X through seg_Y' ranges on the air order."""
    text = str(reason or "")
    keep = _reverse_jump_keep_ids(text)
    ids = [sid for sid in _SEG_ID_RE.findall(text) if sid not in keep]
    order_set = {str(s): i for i, s in enumerate(ordered)}
    for start_id, end_id in _SEG_RANGE_RE.findall(text):
        if start_id in order_set and end_id in order_set:
            lo = min(order_set[start_id], order_set[end_id])
            hi = max(order_set[start_id], order_set[end_id])
            ids.extend(sid for sid in ordered[lo : hi + 1] if sid not in keep)
    return list(dict.fromkeys(ids))


def _outro_like_reason(reason: str) -> bool:
    key = str(reason or "").casefold().replace("-", "_")
    return any(token.replace("-", "_") in key for token in _OUTRO_REASON_TOKENS)


def _fragmentary_tail_reason(reason: str) -> bool:
    """True when layup asks to drop empty/fragment scraps after a finished sign-off."""
    key = str(reason or "").casefold()
    return any(token in key for token in _FRAGMENTARY_TAIL_TOKENS)


def is_editorial_exclude_reason(reason: str) -> bool:
    """True for CTA / sponsor / monetization / editorial-omit / blank-audio reasons."""
    r = str(reason or "").strip().lower()
    if r in {
        "blank_or_unusable_answer_audio",
        "blank_or_unusable",
        "drop_never_touch_cta",
        "media_ip_cta",
    }:
        return True
    if "never_touch" in r.replace("-", "_"):
        return True
    return (
        _cta_like_reason(reason)
        or _outro_like_reason(reason)
        or _fragmentary_tail_reason(reason)
    )


def is_selection_cta_omit_need(need: Any) -> bool:
    """True when an LLM need asks ranking/selection to drop sponsor or media-IP CTA.

    Also matches layup ``transcript_excerpt`` needs that describe empty/degraded
    post-CTA scraps (host should omit rather than spin on unverifiable excerpts).
    """
    if not isinstance(need, dict):
        return False
    typ = str(need.get("type") or "").strip()
    reason = str(need.get("reason") or "")
    if typ == "rerun_stage":
        stage = str(need.get("stage") or "").strip()
        if stage not in _SELECTION_RERUN_STAGES:
            return False
        return is_editorial_exclude_reason(reason)
    if typ == "transcript_excerpt":
        return is_editorial_exclude_reason(reason)
    return False


def apply_editorial_omits(ctx: RunContext, artifacts: dict[str, Any] | None) -> dict[str, Any]:
    """Execute stage omit suggestions + sponsor hard-omit into locked air order.

    Homunculus 0.1.0 only. Consumes exclude_rationales, editorial excluded_segment_ids,
    hard-omit phrase hits, and perspective issues. Parent hard-keep never skips prune;
    hard-keep applies only to admitted clean children.
    """
    out = dict(artifacts) if isinstance(artifacts, dict) else {}
    if not enabled(ctx):
        return out

    try:
        from interview_mux.seat_authority import gate_seat_mutation

        if not gate_seat_mutation(
            ctx,
            reason="media_ip_cta_editorial_omits",
            symptoms=["media_ip_cta"],
        ):
            out["seat_freeze_blocked"] = True
            return out
    except Exception:
        try:
            from interview_mux.seat_authority import soft_freeze_active, hard_freeze_active

            if soft_freeze_active(ctx) or hard_freeze_active(ctx):
                out["seat_freeze_blocked"] = True
                return out
        except Exception:
            out["seat_freeze_blocked"] = True
            return out

    from interview_mux.homunculus.values import should_hard_omit_cta

    ordered = [str(s) for s in (out.get("ordered_segment_ids") or []) if s]
    if not ordered:
        return out

    drop_reasons: dict[str, str] = {}

    rationales = out.get("exclude_rationales") if isinstance(out.get("exclude_rationales"), dict) else {}
    for sid, reason in (rationales or {}).items():
        key = str(sid or "").strip()
        if key and is_editorial_exclude_reason(str(reason or "")):
            drop_reasons.setdefault(key, str(reason)[:240] or "editorial_omit")

    for row in out.get("excluded_segment_ids") or []:
        if isinstance(row, dict):
            sid = str(row.get("segment_id") or "").strip()
            reason = str(row.get("reason") or "")
        else:
            sid = str(row or "").strip()
            reason = str((rationales or {}).get(sid) or "")
        if not sid:
            continue
        if reason and is_editorial_exclude_reason(reason):
            drop_reasons.setdefault(sid, reason[:240])
        elif not reason and sid in (rationales or {}) and is_editorial_exclude_reason(
            str(rationales.get(sid) or "")
        ):
            drop_reasons.setdefault(sid, str(rationales.get(sid))[:240])

    by_id = _segments_by_id(ctx)
    for sid in ordered:
        if sid in drop_reasons:
            continue
        text = str((by_id.get(sid) or {}).get("text") or "")
        if should_hard_omit_cta(text):
            drop_reasons[sid] = "hard_omit_sponsor_or_cta"

    try:
        from interview_mux.homunculus.issues import read_issues

        for issue in read_issues(ctx):
            if not isinstance(issue, dict):
                continue
            kind = str(issue.get("kind") or "")
            if kind not in {
                "perspective_direct_monetization",
                "direct_listener_sponsor_promotion",
                "sponsor_bumper",
            } and "sponsor" not in kind and "monetization" not in kind:
                continue
            for sid in issue.get("implicated") or []:
                key = str(sid or "").strip()
                if key and _tape_is_hard_omit_cta(ctx, key, by_id):
                    drop_reasons.setdefault(key, kind or "perspective_direct_monetization")
    except Exception:
        pass

    drop_ids = list(dict.fromkeys(drop_reasons))
    before_drop = set(never_touch_segment_ids(ctx))
    out = run_cta_prune(ctx, out, notes=["editorial_omits"])
    after_drop = never_touch_segment_ids(ctx)
    new_drops = [sid for sid in drop_ids if sid in after_drop and sid not in before_drop]
    drop_set = (after_drop - before_drop) | {s for s in drop_ids if s in after_drop}
    if not drop_ids and after_drop == before_drop:
        return out

    rationales_out = dict(rationales) if isinstance(rationales, dict) else {}
    for sid in drop_ids:
        rationales_out[sid] = drop_reasons.get(sid) or rationales_out.get(sid) or "editorial_omit"
    out["exclude_rationales"] = rationales_out

    state = load_state(ctx)
    if not isinstance(state, dict):
        state = {"version": 1}
    notes = list(state.get("notes") or [])
    if drop_ids:
        notes.append(f"editorial_omits:{','.join(drop_ids[:12])}")
        state["notes"] = notes[-48:]
        _write_state(ctx, state)

    try:
        from interview_mux.homunculus.issues import emit_issue

        implicated = new_drops or drop_ids[:12]
        if implicated:
            emit_issue(
                ctx,
                kind="perspective_direct_monetization",
                source="apply_editorial_omits",
                stage_id="full_master_ranking",
                implicated=implicated[:12],
                evidence={"reasons": {sid: drop_reasons[sid] for sid in drop_ids[:12] if sid in drop_reasons}},
            )
    except Exception:
        pass

    ctx.log(
        f"editorial_omits: dropped {len(drop_set)} native(s) from air order",
        level="info",
        stage="full_master_ranking",
        detail={"dropped_segment_ids": list(drop_set)[:12]},
    )
    return out


def execute_cta_omit_from_needs(
    ctx: RunContext, needs: list[Any] | None = None
) -> list[str]:
    """Host-drop on-air sponsor/CTA natives named by layup rerun_stage needs.

    Flagship-on-the-fly: the host executes omits. Do not bounce layup through
    selection_framing_apply. Only drops ids whose tape text is a hard-omit CTA
    so reverse-jump mentions in the same need stay on the air.
    """
    if not enabled(ctx):
        return []
    released = release_false_cta_never_touch(ctx)
    before: list[str] = []
    if ctx.artifact_exists("master/selection.json"):
        loaded = ctx.read_json("master/selection.json")
        if isinstance(loaded, dict):
            before = [str(s) for s in (loaded.get("ordered_segment_ids") or []) if s]
    before = _restore_released_to_order(ctx, before, released)
    from interview_mux.homunculus.values import should_hard_omit_cta

    by_id = _segments_by_id(ctx)
    extra: dict[str, str] = {}
    excluded_now: set[str] = set()
    if ctx.artifact_exists("master/selection.json"):
        loaded = ctx.read_json("master/selection.json")
        if isinstance(loaded, dict):
            for row in loaded.get("excluded_segment_ids") or []:
                if isinstance(row, dict):
                    excluded_now.add(str(row.get("segment_id") or ""))
                else:
                    excluded_now.add(str(row or ""))
    excluded_now |= never_touch_segment_ids(ctx)
    excluded_now -= set(released)
    # Already-excluded media_ip CTA ids must leave ordered (exec_13183 / omit sync).
    for sid in list(excluded_now):
        if sid not in before:
            continue
        text = str((by_id.get(sid) or {}).get("text") or "")
        if text and should_hard_omit_cta(text):
            extra.setdefault(sid, "media_ip_cta")
            continue
        # Explicit exclude_rationales / excluded reason naming media_ip_cta.
        try:
            loaded = ctx.read_json("master/selection.json") if ctx.artifact_exists(
                "master/selection.json"
            ) else {}
            rats = (loaded or {}).get("exclude_rationales") or {}
            rat = str(rats.get(sid) or "").lower()
            if "media_ip" in rat or "cta" in rat:
                extra.setdefault(sid, str(rats.get(sid) or "media_ip_cta")[:240])
        except Exception:
            pass
    for need in needs or []:
        if not is_selection_cta_omit_need(need):
            continue
        reason = str(need.get("reason") or "media_ip_cta")
        named = _ids_from_cta_need_reason(reason, before)
        range_drop = bool(_SEG_RANGE_RE.search(reason))
        keep = _reverse_jump_keep_ids(reason)
        omitted_parents = {
            sid
            for sid in named
            if sid in excluded_now and not re.search(r"[a-z]$", sid.split("_", 1)[-1])
        }
        omitted_parents |= {
            sid
            for sid in excluded_now
            if any(_is_nle_child(n, sid) for n in named)
        }
        omitted_parents |= {
            sid
            for sid in named
            if sid not in keep
            and not str((by_id.get(sid) or {}).get("text") or "").strip()
            and not re.search(r"[a-z]$", sid.split("_", 1)[-1])
        }
        frag_tail = _fragmentary_tail_reason(reason)
        for sid in list(dict.fromkeys([*named, *before])):
            if sid in keep:
                continue
            text = str((by_id.get(sid) or {}).get("text") or "")
            child_of_omitted = any(_is_nle_child(sid, parent) for parent in omitted_parents)
            empty_parent = (not text.strip()) and sid in omitted_parents
            # Named post-sign-off scraps ("We'll" / orphaned goodbye) are often not
            # hard-omit CTA phrases — still drop them when layup names them.
            named_frag_tail = frag_tail and sid in named
            # transcript_excerpt / empty-degraded CTA scraps: host-omit named ids
            # even when packed text still looks like a short goodbye.
            named_empty_degraded = (
                sid in named
                and str(need.get("type") or "").strip() == "transcript_excerpt"
                and (
                    frag_tail
                    or _outro_like_reason(reason)
                    or "empty" in reason.casefold()
                )
            )
            if (
                (text and should_hard_omit_cta(text))
                or range_drop
                or child_of_omitted
                or empty_parent
                or named_frag_tail
                or named_empty_degraded
            ):
                extra.setdefault(sid, reason[:240] or "media_ip_cta")
    out: dict[str, Any] | None = None
    if ctx.artifact_exists("master/selection.json") and (extra or released):
        sel = ctx.read_json("master/selection.json")
        sel = dict(sel) if isinstance(sel, dict) else {}
        sel["ordered_segment_ids"] = list(before)
        rationales = (
            dict(sel.get("exclude_rationales") or {})
            if isinstance(sel.get("exclude_rationales"), dict)
            else {}
        )
        excl = []
        released_set = set(released)
        for row in sel.get("excluded_segment_ids") or []:
            sid = str(row.get("segment_id") if isinstance(row, dict) else row)
            if sid in released_set:
                rationales.pop(sid, None)
                continue
            excl.append(row)
        have = {
            str(row.get("segment_id") if isinstance(row, dict) else row) for row in excl
        }
        for sid, reason in extra.items():
            if sid in released_set:
                continue
            rationales[sid] = reason
            if sid not in have:
                excl.append({"segment_id": sid, "reason": reason})
        sel["exclude_rationales"] = rationales
        sel["excluded_segment_ids"] = excl
        out = apply_editorial_omits(ctx, sel) if extra else sel
    if out is None and ctx.artifact_exists("master/selection.json"):
        loaded = ctx.read_json("master/selection.json")
        out = dict(loaded) if isinstance(loaded, dict) else {}
    healed = heal_on_air_cta_residue(ctx, out) if out else {}
    # heal_on_air may release_false + restore garbled scraps whose text is not a
    # hard-omit CTA phrase. Host-executed editorial extras must stay off-air
    # (exec_13198 seg_070 transcript_excerpt / CTA-hole cover thrash).
    if healed and extra:
        drop_extra = set(extra)
        ordered_h = [
            str(s) for s in (healed.get("ordered_segment_ids") or []) if s and s not in drop_extra
        ]
        if ordered_h:
            healed = dict(healed)
            healed["ordered_segment_ids"] = ordered_h
            rats_h = (
                dict(healed.get("exclude_rationales") or {})
                if isinstance(healed.get("exclude_rationales"), dict)
                else {}
            )
            excl_h: list[Any] = []
            have_h: set[str] = set()
            for row in healed.get("excluded_segment_ids") or []:
                sid = str(row.get("segment_id") if isinstance(row, dict) else row or "").strip()
                if sid in drop_extra:
                    continue
                excl_h.append(row)
                if sid:
                    have_h.add(sid)
            for sid, reason in extra.items():
                rats_h[sid] = reason
                if sid not in have_h:
                    excl_h.append({"segment_id": sid, "reason": reason})
                    have_h.add(sid)
            healed["exclude_rationales"] = rats_h
            healed["excluded_segment_ids"] = excl_h
            try:
                state = load_state(ctx)
                if isinstance(state, dict):
                    nt = [
                        str(x)
                        for x in (state.get("never_touch_segment_ids") or [])
                        if x
                    ]
                    state["never_touch_segment_ids"] = list(
                        dict.fromkeys([*nt, *sorted(drop_extra)])
                    )
                    dropped_prev = [
                        str(x) for x in (state.get("dropped_segment_ids") or []) if x
                    ]
                    state["dropped_segment_ids"] = list(
                        dict.fromkeys([*dropped_prev, *sorted(drop_extra)])
                    )
                    _write_state(ctx, state)
            except Exception:
                pass
    if not extra and healed:
        try:
            from interview_mux.artifact_repairs import reconcile_ordered_vs_excluded

            healed = reconcile_ordered_vs_excluded(healed)
        except Exception:
            pass
    after = [str(s) for s in (healed.get("ordered_segment_ids") or []) if s]
    if ctx.artifact_exists("master/selection.json") and healed and (
        (after and after != before) or released
    ):
        from interview_mux.air_order_boundary import commit_selection_mutation

        commit_selection_mutation(
            ctx,
            healed,
            producer="media_ip_cta",
            stage_key=_selection_commit_stage_key(),
            checkpoint_mode="detect",
            skip_checkpoint=True,
            write_committed=True,
        )
    return [sid for sid in before if sid not in set(after)]


_SIGNOFF_RE = re.compile(
    r"\b(thanks for (joining|listening|watching)|goodbye|good night|see you next)\b",
    flags=re.IGNORECASE,
)


def _looks_like_degraded_signoff(text: str) -> bool:
    words = str(text or "").split()
    if not words or len(words) > 12:
        return False
    return bool(_SIGNOFF_RE.search(text or ""))


def omit_locked_degraded_cta_scraps(ctx: RunContext) -> list[str]:
    """Producer omit of empty/degraded CTA-tail scraps still on locked order (F3 2C).

    Synthesizes the same ``transcript_excerpt`` need i5 host-executes, then runs
    ``execute_cta_omit_from_needs``. Does not drop arbitrary mid-show empty segs:
    hard-omit CTA, NLE children of excluded/never-touch CTA parents, or tail
    scraps after those parents.
    """
    if not enabled(ctx):
        return []
    if not ctx.artifact_exists("master/selection.json"):
        return []
    loaded = ctx.read_json("master/selection.json")
    if not isinstance(loaded, dict):
        return []
    ordered = [str(s) for s in (loaded.get("ordered_segment_ids") or []) if s]
    if not ordered:
        return []
    by_id = _segments_by_id(ctx)
    never_touch = never_touch_segment_ids(ctx)
    rationales = (
        loaded.get("exclude_rationales")
        if isinstance(loaded.get("exclude_rationales"), dict)
        else {}
    )
    cta_parents: set[str] = set(never_touch)
    for row in loaded.get("excluded_segment_ids") or []:
        if isinstance(row, dict):
            sid = str(row.get("segment_id") or "").strip()
            reason = str(row.get("reason") or rationales.get(sid) or "")
        else:
            sid = str(row or "").strip()
            reason = str(rationales.get(sid) or "")
        if sid and (is_editorial_exclude_reason(reason) or sid in never_touch):
            cta_parents.add(sid)
    from interview_mux.homunculus.values import should_hard_omit_cta

    scraps: list[str] = []
    n = len(ordered)
    for i, sid in enumerate(ordered):
        text = str((by_id.get(sid) or {}).get("text") or "")
        empty = not text.strip()
        child = any(_is_nle_child(sid, parent) for parent in cta_parents if parent)
        tail = i >= max(0, n - 3)
        if should_hard_omit_cta(text):
            scraps.append(sid)
            continue
        if empty and (child or (tail and cta_parents)):
            scraps.append(sid)
            continue
        if child and tail and _looks_like_degraded_signoff(text):
            scraps.append(sid)
            continue
        if child and looks_like_orphaned_cta_scrap(text):
            try:
                if sid in set(admitted_story_segment_ids(ctx) or []):
                    continue
            except Exception:
                pass
            scraps.append(sid)
            continue
    if not scraps:
        return []
    needs = [
        {
            "type": "transcript_excerpt",
            "stage": "nugget_layup_compose",
            "blocking": True,
            "reason": (
                f"{sid} is retained in the locked order but has an empty, heavily "
                "degraded transcript after the CTA cut; a verified substantive excerpt "
                "is required to keep it on air."
            ),
        }
        for sid in scraps
    ]
    return execute_cta_omit_from_needs(ctx, needs)


def strip_never_touch_nuggets(ctx: RunContext, corpus: dict[str, Any] | None) -> dict[str, Any]:
    """Remove any nuggets whose evidence sits on a dropped CTA clip."""
    out = dict(corpus) if isinstance(corpus, dict) else {"nuggets": []}
    banned = never_touch_segment_ids(ctx)
    try:
        intervals = never_touch_source_intervals(ctx)
    except Exception:
        intervals = []
    if not banned and not intervals:
        return out
    kept: list[dict[str, Any]] = []
    dropped_ids: list[str] = []
    for nug in out.get("nuggets") or []:
        if not isinstance(nug, dict):
            continue
        if nugget_from_never_touch(ctx, nug, banned_ids=banned, intervals=intervals):
            nid = str(nug.get("nugget_id") or "")
            if nid:
                dropped_ids.append(nid)
            continue
        kept.append(nug)
    out["nuggets"] = kept
    if dropped_ids:
        warnings = list(out.get("warnings") or [])
        warnings.append(f"never_touch_cta_nuggets:{','.join(dropped_ids[:12])}")
        out["warnings"] = warnings
    return out


def attach_to_mine_input(ctx: RunContext, payload: dict[str, Any]) -> dict[str, Any]:
    out = dict(payload)
    banned = sorted(never_touch_segment_ids(ctx))
    if banned:
        out["never_touch_cta_segment_ids"] = banned
    return out


def attach_to_compose_input(ctx: RunContext, payload: dict[str, Any]) -> dict[str, Any]:
    out = dict(payload)
    state = load_state(ctx)
    banned = sorted(never_touch_segment_ids(ctx))
    covers = sorted(cover_target_ids(ctx))
    if banned:
        out["never_touch_cta_segment_ids"] = banned
    if covers:
        out["cta_cover_target_ids"] = covers
    natives = []
    cover_set = set(covers)
    for row in out.get("natives") or []:
        if not isinstance(row, dict):
            natives.append(row)
            continue
        native = dict(row)
        sid = str(native.get("segment_id") or "")
        if sid in cover_set:
            native["cta_hole_before"] = True
            native["cta_cover_allowed"] = True
        natives.append(native)
    if natives:
        out["natives"] = natives
    if state.get("open_choice"):
        out["cta_open_choice"] = state.get("open_choice")
    return out


def apply_cover_policy(
    ctx: RunContext, plan: dict[str, Any] | None = None
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Mark optional CTA-hole covers; stamp coverage-exempt skips; allow clone-adjacent."""
    out = dict(plan) if isinstance(plan, dict) else {"layups": []}
    notes: list[dict[str, Any]] = []
    if not enabled(ctx):
        return out, notes
    covers = cover_target_ids(ctx)
    if not covers:
        return out, notes
    from interview_mux.nugget_layup import stamp_typed_skip

    for row in out.get("layups") or []:
        if not isinstance(row, dict):
            continue
        tid = str(row.get("target_segment_id") or "").strip()
        if tid not in covers:
            continue
        text = str(row.get("text") or "").strip()
        if row.get("skip") or not text:
            stamp_typed_skip(
                row,
                reason_code=SKIP_HOLE,
                evidence_refs=[f"target:{tid}", f"skip_reason:{SKIP_HOLE}"],
                compensating_path="media_ip_cta_omit",
                revisit_if=["cta_cover_needed"],
                decision_confidence=0.9,
                owner_stage="nugget_layup_compose",
            )
            notes.append({"action": "cta_hole_skip", "target_segment_id": tid})
            continue
        row["cta_cover"] = True
        row["cta_cover_regenerate"] = True
        row["clone_adjacency_exempt"] = True
        row["vo_shape"] = str(row.get("vo_shape") or "third_person")
        notes.append(
            {
                "action": "cta_cover_allow",
                "target_segment_id": tid,
                "line_id": row.get("line_id"),
            }
        )
        ctx.log(
            f"media_ip_cta: clone-adjacent cover flagged for {tid}; "
            "third-person regenerate does not count toward invoke cap",
            level="info",
            stage="nugget_layup_compose",
        )
    return out, notes


def nugget_from_never_touch(
    ctx: RunContext,
    nugget: dict[str, Any] | None,
    *,
    banned_ids: set[str] | None = None,
    intervals: list[tuple[int, int, str]] | None = None,
) -> bool:
    """True when a nugget's tape is CTA / never-touch, by segment id or by time.

    Full-tape mining (ISSUES 191) yields nuggets with no segment id (words
    between segments), so the source span is judged against the never-touch
    intervals too. Callers that loop pass ``banned_ids`` / ``intervals`` once.
    """
    if not isinstance(nugget, dict):
        return False
    banned = banned_ids if banned_ids is not None else never_touch_segment_ids(ctx)
    sources = {str(x) for x in (nugget.get("source_segment_ids") or []) if x}
    if sources & set(banned or set()):
        return True
    ranges = intervals
    if ranges is None:
        try:
            ranges = never_touch_source_intervals(ctx)
        except Exception:
            ranges = []
    if not ranges:
        return False
    try:
        s0 = nugget.get("source_start_ms")
        e0 = nugget.get("source_end_ms")
        if s0 is None or e0 is None:
            return False
        start, end = int(s0), int(e0)
    except (TypeError, ValueError):
        return False
    if end <= start:
        return False
    for interval in ranges:
        try:
            nt0, nt1 = int(interval[0]), int(interval[1])
        except (TypeError, ValueError, IndexError):
            continue
        if nt0 < end and start < nt1:
            return True
    return False


def _write_state(ctx: RunContext, state: dict[str, Any]) -> None:
    from interview_mux.artifact_writes import write_validated_artifact

    try:
        write_validated_artifact(
            ctx,
            ARTIFACT_REL,
            state,
            merge_from_disk=False,
            stage_key="full_master_ranking",
        )
    except Exception:
        ctx.write_json(ARTIFACT_REL, state)


def _truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    return str(value or "").strip().lower() in {"true", "yes", "1"}


def _segments_by_id(ctx: RunContext) -> dict[str, dict[str, Any]]:
    by_id: dict[str, dict[str, Any]] = {}
    try:
        from interview_mux.nle_state import segments_by_id_with_nle

        by_id = segments_by_id_with_nle(ctx)
    except Exception:
        by_id = {}
    if by_id:
        return by_id
    return _manifest_segments_by_id(ctx)


def _all_segments_by_id(ctx: RunContext) -> dict[str, dict[str, Any]]:
    """Rows for excluded ids too: CTA parents and children are excluded by design.

    Falls back to the manifest when the NLE state cannot be read (ISSUES 180).
    """
    try:
        from interview_mux.nle_state import segments_by_id_including_excluded

        rows = segments_by_id_including_excluded(ctx)
    except Exception:
        rows = {}
    if rows:
        merged = dict(_manifest_segments_by_id(ctx))
        merged.update(rows)
        return merged
    return _manifest_segments_by_id(ctx)


def _manifest_segments_by_id(ctx: RunContext) -> dict[str, dict[str, Any]]:
    """Manifest bounds only — NLE trims must not shrink never-touch keep holes."""
    if not ctx.artifact_exists("segments/manifest.json"):
        return {}
    try:
        man = ctx.read_json("segments/manifest.json")
    except Exception:
        return {}
    return {
        str(s.get("segment_id")): s
        for s in ((man or {}).get("segments") or [])
        if isinstance(s, dict) and s.get("segment_id")
    }


def heal_nle_unplayable_keep_overrides(ctx: RunContext) -> list[str]:
    """Drop NLE trims that collapsed a selected keep below a listenable span.

    Never-touch caps write sub-400 ms overrides that then zero on clamp. Restore
    manifest keep bounds so punch-holes and EDL air bounds use real tape.
    Persists via write_committed_json (not pending-only write_json).
    """
    restored: list[str] = []
    if not ctx.artifact_exists("master/selection.json"):
        return restored
    try:
        sel = ctx.read_json("master/selection.json")
    except Exception:
        return restored
    if not isinstance(sel, dict):
        return restored
    ordered = {str(s) for s in (sel.get("ordered_segment_ids") or []) if s}
    if not ordered:
        return restored
    man = _manifest_segments_by_id(ctx)
    try:
        from interview_mux.nle_state import NLE_REL, load_nle

        nle = load_nle(ctx)
    except Exception:
        return restored
    overrides = nle.get("segment_overrides") or {}
    if not isinstance(overrides, dict):
        return restored
    changed = False
    for sid in list(overrides):
        if sid not in ordered:
            continue
        ov = overrides.get(sid)
        if not isinstance(ov, dict):
            continue
        if ov.get("excluded"):
            continue
        if "start_ms" not in ov and "end_ms" not in ov:
            continue
        man_seg = man.get(sid) or {}
        try:
            man_s = int(man_seg.get("start_ms") or 0)
            man_e = int(man_seg.get("end_ms") or 0)
            ov_s = int(ov["start_ms"]) if "start_ms" in ov else man_s
            ov_e = int(ov["end_ms"]) if "end_ms" in ov else man_e
        except (TypeError, ValueError):
            continue
        if man_e - man_s < MIN_PLAYABLE_KEEP_MS:
            continue
        if ov_e - ov_s >= MIN_PLAYABLE_KEEP_MS:
            continue
        ov.pop("start_ms", None)
        ov.pop("end_ms", None)
        if not ov:
            overrides.pop(sid, None)
        else:
            overrides[sid] = ov
        restored.append(sid)
        changed = True
    if not changed:
        return restored
    nle["segment_overrides"] = overrides
    try:
        from interview_mux.write_staging import write_committed_json

        write_committed_json(ctx, NLE_REL, nle, stage_key="edl")
    except Exception:
        try:
            from interview_mux.nle_state import save_nle

            save_nle(ctx, nle)
        except Exception:
            return restored
    return restored


def _exclude_nle_ids(ctx: RunContext, segment_ids: list[str]) -> None:
    if not segment_ids:
        return
    try:
        from interview_mux.nle_state import load_nle, save_nle

        nle = load_nle(ctx)
        overrides = nle.setdefault("segment_overrides", {})
        drop = {str(s) for s in segment_ids if s}
        for sid in drop:
            row = dict(overrides.get(sid) or {})
            row["excluded"] = True
            overrides[sid] = row
        order = [str(s) for s in (nle.get("sequence_order") or []) if s]
        nle["sequence_order"] = [s for s in order if s not in drop]
        save_nle(ctx, nle)
    except Exception:
        pass


def _parent_bounds(ctx: RunContext, segment_id: str) -> tuple[int, int] | None:
    row = _segments_by_id(ctx).get(segment_id)
    if not row:
        return None
    try:
        start = int(row.get("start_ms") or 0)
        end = int(row.get("end_ms") or start)
    except (TypeError, ValueError):
        return None
    return start, end


def _recut_parent(
    ctx: RunContext,
    segment_id: str,
    judgment: dict[str, Any],
    region: str,
) -> tuple[list[str], str]:
    bounds = _parent_bounds(ctx, segment_id)
    if not bounds:
        return [], "missing_parent"
    start, end = bounds
    span = end - start
    if span < MIN_CHILD_MS * 2:
        return [], "too_short"
    cuts = [int(c) for c in (judgment.get("cut_ms") or []) if str(c).strip() != ""]
    cuts = [c for c in cuts if start + MIN_CHILD_MS <= c <= end - MIN_CHILD_MS]
    if not cuts:
        if region == "start":
            cuts = [start + max(MIN_CHILD_MS, span // 3)]
        elif region == "middle":
            a = start + max(MIN_CHILD_MS, span // 3)
            b = end - max(MIN_CHILD_MS, span // 3)
            cuts = [a, b] if b > a else [start + span // 2]
        else:
            cuts = [end - max(MIN_CHILD_MS, span // 3)]
    cuts = sorted(set(cuts))
    points = [start, *cuts, end]
    for a, b in zip(points, points[1:]):
        if b - a < MIN_CHILD_MS:
            return [], "child_too_short"
    from interview_mux.nle_state import load_nle, split_segment_at_cuts

    split_segment_at_cuts(ctx, segment_id, cuts)
    nle = load_nle(ctx)
    children = [
        str(x)
        for x in ((nle.get("segment_overrides") or {}).get(segment_id) or {}).get("split_into") or []
        if x
    ]
    return children, "ok"


def _cta_children(children: list[str], region: str) -> list[str]:
    if not children:
        return []
    if region == "start":
        return children[:1]
    if region == "middle":
        return children[1:-1] or children[-1:]
    return children[-1:]


def _cta_like_reason(reason: str) -> bool:
    key = str(reason or "").casefold().replace("-", "_")
    return any(
        token in key
        for token in (
            "media_ip_cta",
            "direct_listener_monetization",
            "perspective_direct_monetization",
            "direct_monetization",
            "direct_listener_sponsor",
            "sponsor",
            "promo",
            "hard_omit",
            "editorial_omit",
            "subscribe",
            "visit_them",
            "visit_the_site",
            "visit_us_at",
            "direct_listener",
            "direct listener",
            "outro",
            "credits",
            "sign_off",
            "follow us",
        )
    )


def _cta_like_excluded_ids(artifacts: dict[str, Any]) -> list[str]:
    out: list[str] = []
    for row in artifacts.get("excluded_segment_ids") or []:
        if not isinstance(row, dict):
            continue
        sid = str(row.get("segment_id") or "").strip()
        if sid and _cta_like_reason(str(row.get("reason") or "")):
            out.append(sid)
    return list(dict.fromkeys(out))


def _story_ids_from_recuts(recuts: list[Any]) -> list[str]:
    out: list[str] = []
    for recut in recuts:
        if not isinstance(recut, dict) or not recut.get("ok"):
            continue
        out.extend(str(c) for c in (recut.get("story_children") or []) if c)
    return list(dict.fromkeys(out))


def _recut_parent_ids(recuts: list[Any]) -> list[str]:
    return list(
        dict.fromkeys(
            str(r.get("parent_id") or "")
            for r in recuts
            if isinstance(r, dict) and r.get("ok") and r.get("parent_id")
        )
    )


def _exclude_id(row: Any) -> str:
    if isinstance(row, str):
        return row
    if isinstance(row, dict):
        return str(row.get("segment_id") or "")
    return ""


def _without_excluded_ids(excl: list[Any], ids: set[str]) -> list[Any]:
    if not ids:
        return list(excl)
    return [row for row in excl if _exclude_id(row) not in ids]


def _stamp_excludes(excl: list[Any], ids: list[str]) -> list[Any]:
    out = list(excl)
    have = {_exclude_id(row) for row in out}
    for sid in ids:
        if not sid:
            continue
        if sid not in have:
            out.append({"segment_id": sid, "reason": REASON})
            have.add(sid)
            continue
        for i, row in enumerate(out):
            if isinstance(row, dict) and str(row.get("segment_id") or "") == sid:
                out[i] = {**row, "reason": REASON}
            elif row == sid:
                out[i] = {"segment_id": sid, "reason": REASON}
    return out


def _stamp_story_keep_ok_on_admitted(
    ctx: RunContext,
    child_ids: list[str],
    story_ids: list[str],
) -> None:
    """Stamp listen-complete admitted story prefixes; never incomplete microfragments.

    ``artifact_repairs._segment_is_blank_or_unusable`` keeps short admitted kids on
    air only when ``_meta.story_keep_ok`` is True. Incomplete hangers must stay
    unstamped so blank-repair drops them.
    """
    if _skip_foreign_owner_side_writes():
        return
    if not story_ids or not ctx.artifact_exists("segments/manifest.json"):
        return
    keep_set = {str(s) for s in story_ids if s} & {str(c) for c in child_ids if c}
    if not keep_set:
        return
    try:
        from interview_mux.gap_vo_prior_context import ends_complete_thought
    except Exception:
        return
    try:
        man = ctx.read_json("segments/manifest.json")
    except Exception:
        return
    if not isinstance(man, dict):
        return
    segs = [s for s in (man.get("segments") or []) if isinstance(s, dict)]
    changed = False
    for row in segs:
        sid = str(row.get("segment_id") or "")
        if sid not in keep_set:
            continue
        text = str(row.get("text") or "").strip()
        if not text or not ends_complete_thought(text):
            # Incomplete microfragment — leave unstamped (blank-on-air SSOT).
            continue
        meta = dict(row.get("_meta") or {}) if isinstance(row.get("_meta"), dict) else {}
        if meta.get("story_keep_ok") is True:
            continue
        meta["story_keep_ok"] = True
        row["_meta"] = meta
        changed = True
    if not changed:
        return
    man["segments"] = segs
    try:
        ctx.write_json(
            "segments/manifest.json",
            man,
            skip_handoff=True,
            stage_key="full_master_ranking",
            mutation_class="cta_child_materialize",
        )
    except Exception:
        try:
            from interview_mux.write_staging import write_mirrored_json

            write_mirrored_json(ctx, "segments/manifest.json", man)
        except Exception:
            try:
                ctx.log(
                    "story_keep_ok stamp skipped (manifest write denied/failed)",
                    level="warning",
                    stage="full_master_ranking",
                )
            except Exception:
                pass


def _persist_recut_children(
    ctx: RunContext,
    parent_id: str,
    child_ids: list[str],
    story_ids: list[str],
) -> None:
    """Write recut children into the candidate-source artifacts ranking/shape read."""
    if _skip_foreign_owner_side_writes():
        return
    try:
        from interview_mux.seat_authority import hard_freeze_active

        if hard_freeze_active(ctx):
            # Hard freeze: no invent of new manifest children (paperwork cover only).
            return
    except Exception:
        pass
    try:
        from interview_mux.nle_state import materialize_split_children_into_manifest

        materialize_split_children_into_manifest(
            ctx,
            parent_id,
            child_ids,
            stage_key="full_master_ranking",
            mutation_class="cta_child_materialize",
        )
    except Exception:
        pass
    try:
        _stamp_story_keep_ok_on_admitted(ctx, child_ids, story_ids)
    except Exception:
        pass
    try:
        from interview_mux.artifact_repairs import propagate_nle_split_segment_refs

        keep = [str(s) for s in story_ids if s] or list(child_ids)
        propagate_nle_split_segment_refs(ctx, parent_id, keep)
        _publish_story_children_sources(ctx, parent_id, keep)
    except Exception:
        pass


def _publish_story_children_sources(
    ctx: RunContext, parent_id: str, story_ids: list[str]
) -> None:
    """Rewrite shape/structure sources so admitted children stay in the candidate pool."""
    if _skip_foreign_owner_side_writes():
        return
    if not parent_id or not story_ids:
        return
    from interview_mux.artifact_repairs import _rewrite_segment_id_list

    def _rewrite_doc(rel: str, rewriter: Any) -> None:
        if not ctx.artifact_exists(rel):
            return
        try:
            doc = ctx.read_json(rel)
        except Exception:
            return
        if not isinstance(doc, dict):
            return
        if rewriter(doc):
            # Ranking-era remap only: never inherit the active stage. Ask the
            # table which key may write instead of finding out by denial; each
            # probing denial was logged at error level and counted toward a
            # halt against the stage that happened to be active (ISSUES 127).
            from interview_mux.artifact_ownership import owner_of, write_permitted
            from interview_mux.write_staging import write_mirrored_json

            for key in ("full_master_ranking", owner_of(rel)):
                if not key:
                    continue
                try:
                    ok, _why = write_permitted(ctx, rel, key, role="producer", verb="persist")
                except Exception:
                    ok = False
                if not ok:
                    continue
                try:
                    ctx.write_json(rel, doc, skip_handoff=True, stage_key=key)
                    return
                except Exception:
                    break
            write_mirrored_json(ctx, rel, doc)

    def _episode(doc: dict[str, Any]) -> bool:
        changed = False
        order = doc.get("segment_order")
        if isinstance(order, list) and parent_id in [str(x) for x in order]:
            doc["segment_order"] = _rewrite_segment_id_list(order, parent_id, story_ids)
            changed = True
        hook = doc.get("hook_reel")
        if isinstance(hook, dict) and str(hook.get("segment_id") or "") == parent_id:
            hook["segment_id"] = story_ids[0]
            changed = True
        for slot in doc.get("slot_plan") or []:
            if not isinstance(slot, dict):
                continue
            bound = slot.get("bound_segment_ids")
            if isinstance(bound, list) and parent_id in [str(x) for x in bound]:
                slot["bound_segment_ids"] = _rewrite_segment_id_list(bound, parent_id, story_ids)
                changed = True
        return changed

    def _plan(doc: dict[str, Any]) -> bool:
        order = doc.get("ordered_segment_ids")
        if isinstance(order, list) and parent_id in [str(x) for x in order]:
            doc["ordered_segment_ids"] = _rewrite_segment_id_list(order, parent_id, story_ids)
            return True
        return False

    def _talking(doc: dict[str, Any]) -> bool:
        changed = False
        rows = doc.get("talking_points") if isinstance(doc.get("talking_points"), list) else []
        if not rows and isinstance(doc, dict) and isinstance(doc.get("points"), list):
            rows = doc["points"]
        for row in rows:
            if not isinstance(row, dict):
                continue
            for key in ("segment_id", "cut_id", "source_segment_id"):
                if str(row.get(key) or "") == parent_id:
                    row[key] = story_ids[0]
                    changed = True
            segs = row.get("segment_ids")
            if isinstance(segs, list) and parent_id in [str(x) for x in segs]:
                row["segment_ids"] = _rewrite_segment_id_list(segs, parent_id, story_ids)
                changed = True
        return changed

    def _cuts(doc: dict[str, Any]) -> bool:
        changed = False
        keeps = doc.get("must_keep_segment_ids")
        if isinstance(keeps, list) and parent_id in [str(x) for x in keeps]:
            doc["must_keep_segment_ids"] = _rewrite_segment_id_list(keeps, parent_id, story_ids)
            changed = True
        for row in doc.get("cuts") or []:
            if not isinstance(row, dict):
                continue
            if str(row.get("segment_id") or "") == parent_id:
                row["segment_id"] = story_ids[0]
                changed = True
        return changed

    def _spine(doc: dict[str, Any]) -> bool:
        changed = False
        for key in ("segment_ids", "ordered_segment_ids"):
            val = doc.get(key)
            if isinstance(val, list) and parent_id in [str(x) for x in val]:
                doc[key] = _rewrite_segment_id_list(val, parent_id, story_ids)
                changed = True
        return changed

    _rewrite_doc("understanding/episode_structure.json", _episode)
    _rewrite_doc("mastering/mastering_plan.json", _plan)
    _rewrite_doc("understanding/talking_points.json", _talking)
    _rewrite_doc("understanding/ideal_cuts.json", _cuts)
    _rewrite_doc("understanding/interview_spine.json", _spine)


def _admit_story_ids(
    ctx: RunContext,
    ordered: list[str],
    story_ids: list[str],
    drop_set: set[str],
) -> list[str]:
    """Put CTA-sanitized story remainder back on the master allow-list."""
    out = list(ordered)
    have = set(out)
    by_id = _segments_by_id(ctx)
    try:
        from interview_mux.homunculus.values import should_hard_omit_cta
    except Exception:
        should_hard_omit_cta = lambda _text: False  # noqa: E731
    for sid in story_ids:
        if not sid or sid in drop_set or sid in have:
            continue
        text = str((by_id.get(sid) or {}).get("text") or "")
        if text and should_hard_omit_cta(text):
            continue
        start = 0
        try:
            start = int((by_id.get(sid) or {}).get("start_ms") or 0)
        except (TypeError, ValueError):
            start = 0
        idx = len(out)
        for i, other in enumerate(out):
            try:
                other_start = int((by_id.get(other) or {}).get("start_ms") or 0)
            except (TypeError, ValueError):
                other_start = 0
            if start < other_start:
                idx = i
                break
        out.insert(idx, sid)
        have.add(sid)
    return out


def _rewrite_chapter_ids(
    artifacts: dict[str, Any],
    id_map: dict[str, list[str]],
    drop_set: set[str],
    story_ids: list[str],
) -> None:
    del story_ids
    chapters = artifacts.get("chapters")
    if not isinstance(chapters, list):
        return
    for ch in chapters:
        if not isinstance(ch, dict):
            continue
        segs = [str(s) for s in (ch.get("segment_ids") or []) if s]
        if id_map:
            segs = _rewrite_order(segs, id_map)
        ch["segment_ids"] = list(dict.fromkeys(s for s in segs if s not in drop_set))


def _id_map_from_recuts(recuts: list[Any]) -> dict[str, list[str]]:
    id_map: dict[str, list[str]] = {}
    for recut in recuts:
        if not isinstance(recut, dict) or not recut.get("ok"):
            continue
        parent = str(recut.get("parent_id") or "").strip()
        kids = [str(c) for c in (recut.get("children") or []) if c]
        if parent and kids:
            id_map[parent] = kids
    return id_map


def _story_first_from_state(state: dict[str, Any]) -> str:
    if str(state.get("open_choice") or "") != "story_child_first":
        return ""
    parent = str(state.get("cta_open_parent") or "")
    for recut in state.get("recuts") or []:
        if not isinstance(recut, dict) or str(recut.get("parent_id") or "") != parent:
            continue
        kids = [str(c) for c in (recut.get("story_children") or []) if c]
        return kids[0] if kids else ""
    return ""


def _sync_cold_open(
    artifacts: dict[str, Any],
    drop_set: set[str],
    id_map: dict[str, list[str]],
    ordered: list[str],
) -> None:
    hook = str(artifacts.get("native_cold_open_segment_id") or "").strip()
    if not hook:
        return
    if hook in drop_set or hook in id_map:
        replacement = next((s for s in ordered if s not in drop_set), "")
        if replacement:
            artifacts["native_cold_open_segment_id"] = replacement
        else:
            artifacts.pop("native_cold_open_segment_id", None)


def _rewrite_order(ordered: list[str], id_map: dict[str, list[str]]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for sid in ordered:
        kids = id_map.get(sid)
        repl = kids if kids else [sid]
        for item in repl:
            if item in seen:
                continue
            seen.add(item)
            out.append(item)
    return out


def _cover_targets(
    ordered: list[str],
    dropped: list[str],
    id_map: dict[str, list[str]],
) -> list[str]:
    """Kept natives that immediately follow a CTA drop in the pre-filter order."""
    del id_map
    if not ordered or not dropped:
        return []
    drop_set = set(dropped)
    targets: list[str] = []
    for i, sid in enumerate(ordered):
        if sid not in drop_set:
            continue
        for later in ordered[i + 1 :]:
            if later not in drop_set:
                targets.append(later)
                break
    return list(dict.fromkeys(targets))


def _texts_for(ctx: RunContext, ids: list[str]) -> list[str]:
    by_id = _all_segments_by_id(ctx)
    out: list[str] = []
    for sid in ids:
        text = str((by_id.get(sid) or {}).get("text") or "").strip()
        if text:
            out.append(text)
    return out


def _tokens(text: str) -> set[str]:
    return {t for t in "".join(ch.lower() if ch.isalnum() else " " for ch in (text or "")).split() if len(t) > 2}


def _overlap(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / float(min(len(a), len(b)))

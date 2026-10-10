"""Sealed AirOrder generation — one live bundle of lock, clips, glue, and VO keys.

Adapters propose the next generation; ``commit`` writes it atomically or
``rollback`` leaves generation N unchanged. Mix/junction/finalize refuse mixed gens.
"""

from __future__ import annotations

import copy
import hashlib
import shutil
from datetime import datetime, timezone
from typing import Any

from interview_mux.run_context import RunContext

AIR_ORDER_REL = "master/air_order.json"
SNAPSHOT_REL = "master/air_order_snapshot.json"
ROLLBACK_ASSEMBLY_REL = "master/air_order_rollback/assembly.wav"
SELECTION_REL = "master/selection.json"
EDL_REL = "master/edl.json"
RENDER_LEDGER_REL = "master/render_ledger.json"
ASSEMBLY_LEDGER_REL = "master/assembly_ledger.json"

_INPUT_HASH_RELS = (
    "master/transitions.json",
    "understanding/gap_report.json",
    "understanding/sound_design_plan.json",
    "understanding/nugget_layup_plan.json",
)


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _ids(doc: dict[str, Any] | None, key: str = "ordered_segment_ids") -> list[str]:
    if not isinstance(doc, dict):
        return []
    return [str(s) for s in (doc.get(key) or []) if s]


def _sha_rel(ctx: RunContext, rel: str) -> str | None:
    path = ctx.final_path(*rel.split("/"))
    if not path.is_file():
        return None
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()[:16]
    except OSError:
        return None


def _read_dict(ctx: RunContext, rel: str) -> dict[str, Any] | None:
    if not ctx.artifact_exists(rel):
        return None
    try:
        doc = ctx.read_json(rel)
    except Exception:
        return None
    return doc if isinstance(doc, dict) else None


def _committing(ctx: RunContext) -> bool:
    return bool(getattr(ctx, "_air_order_committing", False))


def _set_committing(ctx: RunContext, value: bool) -> None:
    setattr(ctx, "_air_order_committing", value)


def _write_json(ctx: RunContext, rel: str, data: dict[str, Any]) -> None:
    nested = _committing(ctx)
    if not nested:
        _set_committing(ctx, True)
    try:
        sk = ""
        try:
            from interview_mux.write_staging import active_stage_id

            sk = str(active_stage_id() or "").strip()
        except Exception:
            sk = ""
        if sk not in {"edl", "mix", "junction_snip_qa"}:
            sk = "edl"
        ctx.write_json(rel, data, skip_handoff=True, stage_key=sk)
    finally:
        if not nested:
            _set_committing(ctx, False)


def generation(ctx: RunContext) -> int:
    live = read_live(ctx)
    try:
        return int(live.get("generation") or 0)
    except (TypeError, ValueError):
        return 0


def read_live(ctx: RunContext) -> dict[str, Any]:
    doc = _read_dict(ctx, AIR_ORDER_REL)
    if isinstance(doc, dict):
        doc.setdefault("version", 1)
        doc.setdefault("generation", 0)
        return doc
    return {
        "version": 1,
        "generation": 0,
        "source": None,
        "ordered_segment_ids": [],
        "speech_clip_ids": [],
        "glue_occupancy": {},
        "vo_keys": [],
        "input_hashes": {},
    }


def propose(ctx: RunContext) -> dict[str, Any]:
    """Snapshot the next candidate generation without writing it."""
    return {
        "generation": generation(ctx) + 1,
        "selection": copy.deepcopy(_read_dict(ctx, SELECTION_REL) or {}),
        "edl": copy.deepcopy(_read_dict(ctx, EDL_REL) or {}),
    }


def snapshot(ctx: RunContext) -> dict[str, Any]:
    """Persist live JSON (+ assembly wav) so ``rollback`` can restore generation N."""
    snap: dict[str, Any] = {
        "version": 1,
        "captured_at": _now(),
        "generation": generation(ctx),
        "selection": _read_dict(ctx, SELECTION_REL),
        "edl": _read_dict(ctx, EDL_REL),
        "air_order": _read_dict(ctx, AIR_ORDER_REL),
        "render_ledger": _read_dict(ctx, RENDER_LEDGER_REL),
        "assembly_ledger": _read_dict(ctx, ASSEMBLY_LEDGER_REL),
        "assembly_copied": False,
    }
    _write_json(ctx, SNAPSHOT_REL, snap)
    src = ctx.final_path("master", "assembly.wav")
    if src.is_file():
        dest = ctx.final_path(*ROLLBACK_ASSEMBLY_REL.split("/"))
        dest.parent.mkdir(parents=True, exist_ok=True)
        try:
            shutil.copy2(src, dest)
            snap["assembly_copied"] = True
            _write_json(ctx, SNAPSHOT_REL, snap)
        except OSError:
            pass
    return snap


def rollback(ctx: RunContext) -> dict[str, Any]:
    """Restore the last snapshot. Live show is generation N again."""
    snap = _read_dict(ctx, SNAPSHOT_REL)
    if not isinstance(snap, dict):
        return {"ok": False, "error": "no_snapshot"}
    nested = _committing(ctx)
    if not nested:
        _set_committing(ctx, True)
    try:
        for rel, key in (
            (SELECTION_REL, "selection"),
            (EDL_REL, "edl"),
            (AIR_ORDER_REL, "air_order"),
            (RENDER_LEDGER_REL, "render_ledger"),
            (ASSEMBLY_LEDGER_REL, "assembly_ledger"),
        ):
            doc = snap.get(key)
            if isinstance(doc, dict):
                ctx.write_json(rel, doc, skip_handoff=True)
        if snap.get("assembly_copied"):
            src = ctx.final_path(*ROLLBACK_ASSEMBLY_REL.split("/"))
            dest = ctx.final_path("master", "assembly.wav")
            if src.is_file():
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, dest)
    finally:
        if not nested:
            _set_committing(ctx, False)
    return {"ok": True, "generation": snap.get("generation")}


def _exclude_unseated(
    selection: dict[str, Any],
    clip_ids: list[str],
    *,
    source: str,
) -> dict[str, Any]:
    from interview_mux.order_hash import bump_order_lock

    sel_ids = _ids(selection)
    clip_set = set(clip_ids)
    drop = [sid for sid in sel_ids if sid not in clip_set]
    out = dict(selection)
    out["ordered_segment_ids"] = list(clip_ids)
    excl = list(out.get("excluded_segment_ids") or [])
    have = {
        str(row.get("segment_id") if isinstance(row, dict) else row)
        for row in excl
    }
    for sid in drop:
        if sid not in have:
            excl.append({"segment_id": sid, "reason": "edl_unseated"})
            have.add(sid)
    out["excluded_segment_ids"] = excl
    return bump_order_lock(out, source=source)


def _vo_keys(edl: dict[str, Any] | None) -> list[dict[str, Any]]:
    keys: list[dict[str, Any]] = []
    if not isinstance(edl, dict):
        return keys
    for clip in edl.get("clips") or []:
        if not isinstance(clip, dict) or str(clip.get("type") or "") != "vo_pickup":
            continue
        keys.append(
            {
                "line_id": str(clip.get("line_id") or ""),
                "script_hash": str(clip.get("script_hash") or ""),
                "speaker_id": str(
                    clip.get("voice_speaker_id") or clip.get("speaker_id") or ""
                ),
                "wav_rel": str(clip.get("source_path") or ""),
            }
        )
    return keys


def _glue_occupancy(ctx: RunContext, edl: dict[str, Any] | None) -> dict[str, Any]:
    """One owner per air target: orientation > gap interviewer > transition > layup."""
    occupancy: dict[str, Any] = {}
    gap = _read_dict(ctx, "understanding/gap_report.json") or {}
    try:
        from interview_mux.opening_orientation import is_episode_orientation
    except Exception:
        def is_episode_orientation(_ln: dict[str, Any]) -> bool:  # type: ignore[misc]
            return False

    def _prio(line: dict[str, Any]) -> int:
        if is_episode_orientation(line):
            return 0
        origin = str(line.get("origin") or line.get("source") or "").lower()
        if "layup" in origin or str(line.get("line_id") or "").startswith("vo_layup"):
            return 3
        return 1

    for line in gap.get("interviewer_lines") or []:
        if not isinstance(line, dict):
            continue
        if line.get("skipped_optional") or line.get("air_script_omit") or line.get("skip"):
            continue
        target = str(
            line.get("targets_segment_id")
            or line.get("target_segment_id")
            or line.get("before_segment_id")
            or ""
        )
        if not target:
            continue
        row = occupancy.get(target)
        cand = {
            "owner": "episode_orientation"
            if is_episode_orientation(line)
            else ("nugget_layup" if _prio(line) >= 3 else "gap_interviewer"),
            "line_id": str(line.get("line_id") or ""),
            "priority": _prio(line),
        }
        if row is None or int(cand["priority"]) < int(row.get("priority") or 99):
            occupancy[target] = cand

    if isinstance(edl, dict):
        for clip in edl.get("clips") or []:
            if not isinstance(clip, dict) or str(clip.get("type") or "") != "transition":
                continue
            after = str(clip.get("after_segment_id") or "")
            before = str(clip.get("before_segment_id") or "")
            key = f"{after}->{before}" if after and before else after or before
            if key and key not in occupancy:
                occupancy[key] = {
                    "owner": "transition",
                    "line_id": str(clip.get("line_id") or ""),
                    "priority": 2,
                }
    return occupancy


def _resolve_glue_slots(ctx: RunContext) -> list[str]:
    notes: list[str] = []
    try:
        from interview_mux.opening_adjacency_repair import (
            suppress_opening_layup_when_orientation_owns_slot,
        )

        notes.extend(suppress_opening_layup_when_orientation_owns_slot(ctx) or [])
    except Exception:
        pass
    return notes


def _stamp_gen(doc: dict[str, Any], gen: int) -> dict[str, Any]:
    out = dict(doc)
    out["air_order_generation"] = int(gen)
    return out


def _input_hashes(ctx: RunContext) -> dict[str, str | None]:
    return {rel: _sha_rel(ctx, rel) for rel in _INPUT_HASH_RELS}


def _reconcile(
    ctx: RunContext,
    selection: dict[str, Any] | None,
    edl: dict[str, Any] | None,
    *,
    source: str,
    exclude_unseated: bool,
) -> tuple[dict[str, Any] | None, dict[str, Any] | None, str]:
    from interview_mux.order_hash import (
        bump_order_lock,
        copy_order_lock_if_clips_match,
        edl_speech_clip_ids,
        order_drift_heal_action,
        stamp_order_hash,
    )

    sel = dict(selection) if isinstance(selection, dict) else None
    edl_out = dict(edl) if isinstance(edl, dict) else None
    if sel is None and edl_out is None:
        return None, None, "ok"
    action = "ok"
    if sel is not None and edl_out is not None:
        action = order_drift_heal_action(sel, edl_out)
        clip_ids = edl_speech_clip_ids(edl_out)
        if action == "exclude_unseated" and exclude_unseated and clip_ids:
            sel = _exclude_unseated(sel, clip_ids, source=source)
            edl_out = copy_order_lock_if_clips_match(sel, edl_out)
            action = "exclude_unseated"
        elif action == "stamp":
            if not sel.get("order_lock"):
                sel = bump_order_lock(sel, source=source)
            sel = stamp_order_hash(sel)
            edl_out = copy_order_lock_if_clips_match(sel, edl_out)
        elif action == "ok" and clip_ids:
            try:
                edl_out = copy_order_lock_if_clips_match(sel, edl_out)
            except ValueError:
                pass
        elif action == "rebuild":
            raise ValueError(
                "selection_edl_order_drift: speech clip order diverges from "
                "ordered_segment_ids — rebuild EDL from selection (run_edl) "
                "before commit"
            )
    elif sel is not None:
        if not sel.get("order_lock"):
            sel = bump_order_lock(sel, source=source)
        sel = stamp_order_hash(sel)
    elif edl_out is not None:
        edl_out = stamp_order_hash(edl_out)
    return sel, edl_out, action


def commit(
    ctx: RunContext,
    *,
    selection: dict[str, Any] | None = None,
    edl: dict[str, Any] | None = None,
    source: str = "air_order",
    exclude_unseated: bool = True,
    snapshot_first: bool = True,
    mutation_class: str | None = None,
) -> dict[str, Any]:
    """Write a sealed generation. On failure the caller should ``rollback``."""
    if (
        snapshot_first
        and not _committing(ctx)
        and not getattr(ctx, "_air_order_hold_snapshot", False)
    ):
        snapshot(ctx)
    glue_notes = _resolve_glue_slots(ctx)
    sel_in = selection if isinstance(selection, dict) else _read_dict(ctx, SELECTION_REL)
    edl_in = edl if isinstance(edl, dict) else _read_dict(ctx, EDL_REL)
    sel_out, edl_out, action = _reconcile(
        ctx,
        sel_in,
        edl_in,
        source=source,
        exclude_unseated=exclude_unseated,
    )
    gen = generation(ctx) + 1
    if isinstance(sel_out, dict):
        sel_out = _stamp_gen(sel_out, gen)
    if isinstance(edl_out, dict):
        try:
            from interview_mux.media_ip_cta import clamp_edl_speech_away_from_never_touch

            edl_out, _ = clamp_edl_speech_away_from_never_touch(ctx, edl_out)
        except Exception:
            pass
        edl_out = _stamp_gen(edl_out, gen)
    occupancy = _glue_occupancy(ctx, edl_out)
    from interview_mux.order_hash import edl_speech_clip_ids

    clip_ids = edl_speech_clip_ids(edl_out) if isinstance(edl_out, dict) else []
    ordered = _ids(sel_out)
    bundle = {
        "version": 1,
        "generation": gen,
        "source": source,
        "committed_at": _now(),
        "action": action,
        "ordered_segment_ids": ordered,
        "speech_clip_ids": clip_ids,
        "glue_occupancy": occupancy,
        "glue_notes": glue_notes[:24],
        "vo_keys": _vo_keys(edl_out),
        "input_hashes": _input_hashes(ctx),
        "air_order_generation": gen,
    }
    _set_committing(ctx, True)
    try:
        if isinstance(sel_out, dict):
            from interview_mux.air_order_boundary import commit_selection_mutation

            # Ownership: selection persist ALLOW is ranking/sanitize/"selection"
            # (legacy air_order bus). Never stage_key=source ("edl"/"mix"/…) —
            # those are DENY / not_allow under fail-closed and break write_live_edl.
            sel_out = commit_selection_mutation(
                ctx,
                sel_out,
                producer="air_order",
                stage_key="selection",
                checkpoint_mode="detect",
                skip_handoff=True,
                skip_checkpoint=False,
            )
        if isinstance(edl_out, dict):
            ctx.write_json(
                EDL_REL,
                edl_out,
                skip_handoff=True,
                stage_key=source if mutation_class else "edl",
                mutation_class=mutation_class,
            )
        air_sk = source if source in {"edl", "mix", "junction_snip_qa"} else "edl"
        ctx.write_json(AIR_ORDER_REL, bundle, skip_handoff=True, stage_key=air_sk)
    finally:
        _set_committing(ctx, False)
    return bundle


def restore_protected_speech_clips(
    ctx: RunContext,
    edl: dict[str, Any],
    *,
    source: str = "edl",
) -> list[str]:
    """Put back a must-air keep that an EDL write would leave without a clip.

    The selection keeps a protected id (removal authority refuses its removal)
    while some EDL producer dropped its speech clip; the EDL then never matches
    the selection, ``stage_outputs_present`` stays false and the stage refuses
    itself to the invoke cap (exec_025 seg_021; ISSUES 179). The writer is the
    one place every EDL producer passes, so the keep is reseated here from its
    tape span, after the nearest earlier keep in the selection order.
    """
    clips = edl.get("clips") if isinstance(edl, dict) else None
    if not isinstance(clips, list):
        return []
    sel = _read_dict(ctx, SELECTION_REL)
    order = [str(s) for s in ((sel or {}).get("ordered_segment_ids") or []) if s]
    if not order:
        return []
    seated = {
        str(c.get("segment_id"))
        for c in clips
        if isinstance(c, dict) and str(c.get("type") or "") == "speech"
    }
    try:
        from interview_mux.removal_authority import protected_segment_ids

        protected = protected_segment_ids(ctx, on_air=order)
    except Exception:
        return []
    if not protected:
        return []
    min_ms = 400
    try:
        from interview_mux.media_ip_cta import MIN_PLAYABLE_KEEP_MS

        min_ms = int(MIN_PLAYABLE_KEEP_MS)
    except Exception:
        pass
    try:
        manifest = ctx.read_json("segments/manifest.json") if ctx.artifact_exists("segments/manifest.json") else {}
    except Exception:
        manifest = {}
    by_id = {
        str(r.get("segment_id")): r
        for r in ((manifest or {}).get("segments") or [])
        if isinstance(r, dict) and r.get("segment_id")
    }
    try:
        from interview_mux.media_ip_cta import (
            clamp_source_away_from_never_touch,
            never_touch_source_intervals,
        )

        ranges = never_touch_source_intervals(ctx)
    except Exception:
        ranges = []
    def _own_span(sid: str) -> tuple[int, int] | None:
        row = by_id.get(sid) or {}
        try:
            s0 = int(row.get("start_ms"))
            e0 = int(row.get("end_ms"))
        except (TypeError, ValueError):
            return None
        if ranges:
            s0, e0, _notes = clamp_source_away_from_never_touch(s0, e0, ranges)
        # Stay off tape another on-air clip already plays.
        for c in clips:
            if not isinstance(c, dict) or str(c.get("type") or "") != "speech":
                continue
            if str(c.get("segment_id")) == sid:
                continue
            try:
                cs = int(c.get("source_start_ms") or 0)
                ce = int(c.get("source_end_ms") or cs)
            except (TypeError, ValueError):
                continue
            if cs < e0 and s0 < ce:
                if cs <= s0:
                    s0 = max(s0, ce)
                else:
                    e0 = min(e0, cs)
        return (s0, e0) if e0 - s0 >= min_ms else None

    restored: list[str] = []
    # A seated must-air clip that is unplayable or plays none of its own tape
    # (seated on a neighbour's head by a bad bound) is reseated in place.
    for c in clips:
        if not isinstance(c, dict) or str(c.get("type") or "") != "speech":
            continue
        sid = str(c.get("segment_id") or "")
        if sid not in protected:
            continue
        row = by_id.get(sid) or {}
        try:
            cs = int(c.get("source_start_ms") or 0)
            ce = int(c.get("source_end_ms") or cs)
            r0 = int(row.get("start_ms"))
            r1 = int(row.get("end_ms"))
        except (TypeError, ValueError):
            continue
        own_overlap = min(ce, r1) - max(cs, r0)
        if ce - cs >= min_ms and own_overlap > 0:
            continue
        span = _own_span(sid)
        if span is None:
            continue
        c["source_start_ms"], c["source_end_ms"] = span
        c["duration_ms"] = span[1] - span[0]
        c["air_bound_reason"] = "reseated_must_air"
        restored.append(sid)
    want = [s for s in order if s not in seated and s in protected]
    for sid in want:
        span = _own_span(sid)
        if span is None:
            continue
        s0, e0 = span
        at = 0
        pos = order.index(sid)
        for prior in reversed(order[:pos]):
            idxs = [
                i
                for i, c in enumerate(clips)
                if isinstance(c, dict)
                and str(c.get("type") or "") == "speech"
                and str(c.get("segment_id")) == prior
            ]
            if idxs:
                at = idxs[-1] + 1
                break
        clips.insert(
            at,
            {
                "segment_id": sid,
                "type": "speech",
                "source_start_ms": s0,
                "source_end_ms": e0,
                "duration_ms": e0 - s0,
                "timeline_start_ms": 0,
                "air_bound_reason": "restored_must_air",
            },
        )
        restored.append(sid)
    if not restored:
        return []
    from interview_mux.listenability_guards import reindex_clip_timeline

    edl["clips"] = clips
    edl["timeline_duration_ms"] = reindex_clip_timeline(clips)
    speech_ids = [
        str(c.get("segment_id"))
        for c in clips
        if isinstance(c, dict) and str(c.get("type") or "") == "speech"
    ]
    edl["ordered_segment_ids"] = list(dict.fromkeys(speech_ids))
    omitted = [
        str(s) for s in (edl.get("omitted_unplayable_segment_ids") or []) if str(s) not in restored
    ]
    if "omitted_unplayable_segment_ids" in edl:
        edl["omitted_unplayable_segment_ids"] = omitted
    try:
        ctx.log(
            "EDL write would drop must-air keep(s) "
            + ", ".join(restored)
            + f"; reseated from tape (source={source})",
            level="warning",
            stage=str(source or "edl"),
            detail={"restored": restored, "source": source},
        )
    except Exception:
        pass
    return restored


def write_live_edl(
    ctx: RunContext,
    edl: dict[str, Any],
    *,
    source: str = "edl",
    mutation_class: str | None = None,
) -> dict[str, Any]:
    """Production EDL persist — bumps generation (or no-ops while already committing)."""
    from interview_mux.artifact_sanitize.one_writer import (
        admitting,
        begin_admit,
        end_admit,
    )

    nested_admit = admitting(ctx)
    if not nested_admit:
        begin_admit(ctx)
    try:
        edl_out = edl
        before_token = ""
        try:
            if ctx.artifact_exists(EDL_REL):
                prev = ctx.read_json(EDL_REL)
                from interview_mux.thrash_hardening import edl_content_authority_token

                before_token = edl_content_authority_token(
                    prev if isinstance(prev, dict) else None
                )
        except Exception:
            before_token = ""
        try:
            from interview_mux.media_ip_cta import clamp_edl_speech_away_from_never_touch

            edl_out, _nt_rows = clamp_edl_speech_away_from_never_touch(ctx, edl)
        except Exception:
            edl_out = edl
        try:
            if isinstance(edl_out, dict):
                restore_protected_speech_clips(ctx, edl_out, source=source)
        except Exception:
            pass
        if _committing(ctx):
            ctx.write_json(EDL_REL, edl_out, skip_handoff=True, mutation_class=mutation_class)
            return read_live(ctx)
        live = commit(ctx, edl=edl_out, source=source, mutation_class=mutation_class)
        # EDL rewrite without content change must not look unseated / force remaster.
        src_l = str(source or "").lower()
        if src_l not in {"mix", "stamp_after_mix", "master_finalize"}:
            try:
                from interview_mux.thrash_hardening import maybe_bump_seating_for_edl_rewrite

                maybe_bump_seating_for_edl_rewrite(
                    ctx,
                    before_token=before_token,
                    after_edl=edl_out if isinstance(edl_out, dict) else None,
                    source=source,
                )
            except Exception:
                pass
        return live
    finally:
        if not nested_admit:
            end_admit(ctx)


def write_live_selection(
    ctx: RunContext,
    selection: dict[str, Any],
    *,
    source: str = "selection",
) -> dict[str, Any]:
    from interview_mux.artifact_sanitize.one_writer import (
        admitting,
        begin_admit,
        end_admit,
    )

    nested_admit = admitting(ctx)
    if not nested_admit:
        begin_admit(ctx)
    try:
        if _committing(ctx):
            ctx.write_json(SELECTION_REL, selection, skip_handoff=True)
            return read_live(ctx)
        return commit(ctx, selection=selection, source=source)
    finally:
        if not nested_admit:
            end_admit(ctx)


def stamp_after_mix(ctx: RunContext) -> dict[str, Any]:
    """Tag render_ledger + air_order with the live generation after a successful mix."""
    live = read_live(ctx)
    gen = int(live.get("generation") or 0)
    ledger = _read_dict(ctx, RENDER_LEDGER_REL)
    if isinstance(ledger, dict) and gen:
        ledger = dict(ledger)
        ledger["air_order_generation"] = gen
        _write_json(ctx, RENDER_LEDGER_REL, ledger)
        live["assembly_sha"] = ledger.get("assembly")
    else:
        live["assembly_sha"] = None
    live["mix_stamped_at"] = _now()
    live["air_order_generation"] = gen
    _write_json(ctx, AIR_ORDER_REL, live)
    return live


def live_generation_matches(ctx: RunContext) -> bool:
    live = read_live(ctx)
    gen = int(live.get("generation") or 0)
    if not gen:
        return True
    edl = _read_dict(ctx, EDL_REL) or {}
    sel = _read_dict(ctx, SELECTION_REL) or {}
    try:
        edl_gen = int(edl.get("air_order_generation") or 0)
        sel_gen = int(sel.get("air_order_generation") or 0)
    except (TypeError, ValueError):
        return False
    if edl_gen and edl_gen != gen:
        return False
    if sel_gen and sel_gen != gen:
        return False
    return True


def mix_wav_fresh_versus_edl(ctx: RunContext) -> bool:
    """True when final ``master/assembly.wav`` exists and is not older than live EDL."""
    asm = ctx.final_path("master", "assembly.wav")
    edl = ctx.final_path("master", "edl.json")
    if not asm.is_file() or not edl.is_file():
        return False
    try:
        asm_m = asm.stat().st_mtime
        edl_m = edl.stat().st_mtime
    except OSError:
        return False
    if asm_m + 1.0 >= edl_m:
        return True
    # The EDL file is newer, but was its content changed? A denied promote or
    # a stamp rewrite touches edl.json with the same order; unseating the mix
    # on mtime alone sent exec_049 into a mix -> unseated -> mix loop
    # (ISSUES entry 52). Trust the render commitment only when the render
    # ledger still describes this EDL clip for clip and the assembly matches.
    try:
        from interview_mux.seam_autopsy import verify_commitment

        if not _render_ledger_matches_edl(ctx):
            return False
        return str(verify_commitment(ctx).get("status") or "") == "committed"
    except Exception:
        return False


def _clip_signature(clips: Any) -> list[tuple[str, str, str]]:
    return [
        (str(c.get("type") or ""), str(c.get("segment_id") or ""), str(c.get("line_id") or ""))
        for c in (clips or [])
        if isinstance(c, dict)
    ]


def _render_ledger_matches_edl(ctx: RunContext) -> bool:
    """True when the render ledger's clip sequence equals the live EDL's.

    A stamp rewrite of edl.json keeps the clips; a real re-cut changes them.
    No ledger, or a ledger without clips, never counts as a match.
    """
    ledger = _read_dict(ctx, RENDER_LEDGER_REL) or {}
    edl = _read_dict(ctx, EDL_REL) or {}
    want = _clip_signature(ledger.get("clips"))
    have = _clip_signature(edl.get("clips"))
    return bool(want) and want == have


def ensure_assembly_mtime_seats_edl(ctx: RunContext) -> None:
    """After promote, bump assembly mtime so HX-2 seating is not falsely unseated.

    Only touches mtime when the wav already exists and is older than EDL —
    used immediately after a remaster that rendered from that EDL.
    """
    import os

    asm = ctx.final_path("master", "assembly.wav")
    edl = ctx.final_path("master", "edl.json")
    if not asm.is_file() or not edl.is_file():
        return
    try:
        edl_m = edl.stat().st_mtime
        if asm.stat().st_mtime + 1.0 >= edl_m:
            return
        os.utime(asm, (edl_m, edl_m))
    except OSError:
        return


def live_render_generation_matches(ctx: RunContext) -> bool:
    """True when render_ledger air_order_generation matches live AirOrder gen."""
    live = read_live(ctx)
    gen = int(live.get("generation") or 0)
    if not gen:
        return True
    ledger = _read_dict(ctx, RENDER_LEDGER_REL) or {}
    try:
        led_gen = int(ledger.get("air_order_generation") or 0)
    except (TypeError, ValueError):
        return True
    if led_gen and led_gen != gen:
        return False
    return True


def mix_outputs_seated(ctx: RunContext) -> bool:
    """True only when mix is fully seated for the live EDL.

    Requires **both**:
    - mtime seat (``mix_wav_fresh_versus_edl`` — assembly not older than EDL)
    - commitment seat (ledger SHA / ``mix_stale_versus_live`` false)

    mtime-only is not enough to stamp done or leave mix. Junction / finalize
    still use ``mix_committed_for_live_gen`` for autopsy commitment.
    """
    if not mix_wav_fresh_versus_edl(ctx):
        return False
    if not live_generation_matches(ctx):
        return False
    if not live_render_generation_matches(ctx):
        return False
    # Ledger SHA must match final assembly — otherwise orphan promote / mtime
    # seat can mark_done while verify_commitment still diverges (exec_13167).
    try:
        if mix_stale_versus_live(ctx):
            return False
    except Exception:
        # Fail-open on check crash; stale=True already returns False above.
        pass
    sel = _read_dict(ctx, SELECTION_REL)
    edl = _read_dict(ctx, EDL_REL)
    if not isinstance(sel, dict):
        return True
    sel_ids = [str(s) for s in (sel.get("ordered_segment_ids") or []) if s]
    if not sel_ids:
        return True
    from interview_mux.order_hash import (
        edl_speech_clip_ids,
        order_drift_heal_action,
        seatable_selection_ids,
    )

    if (
        order_drift_heal_action(sel, edl if isinstance(edl, dict) else None)
        not in {"ok", "stamp"}
    ):
        return False
    clip_ids = edl_speech_clip_ids(edl if isinstance(edl, dict) else {})
    seated = seatable_selection_ids(sel, edl if isinstance(edl, dict) else None, use_lock=True)
    if clip_ids and seated and clip_ids != seated:
        return False
    return True


def mix_stale_versus_live(ctx: RunContext) -> bool:
    """True when assembly is not the mix of the live AirOrder generation."""
    # Committed assembly only — orphan .pending_writes/mix/… after abort must not
    # look like a live mix that drifted from EDL (blocks mmaudio/mix as stale).
    asm = ctx.final_path("master", "assembly.wav")
    if not asm.is_file() or not ctx.artifact_exists(EDL_REL):
        return False
    if not live_generation_matches(ctx):
        return True
    try:
        from interview_mux.seam_autopsy import verify_commitment

        result = verify_commitment(ctx)
    except Exception:
        return False
    if not isinstance(result, dict):
        return False
    if result.get("status") == "committed":
        return False
    reasons = [str(r) for r in (result.get("reasons") or [])]
    return "assembly_not_rendered_from_current_edl" in reasons


def mix_committed_for_live_gen(ctx: RunContext) -> bool:
    if not ctx.final_path("master", "assembly.wav").is_file():
        return False
    if mix_stale_versus_live(ctx):
        return False
    try:
        from interview_mux.seam_autopsy import verify_commitment

        result = verify_commitment(ctx)
    except Exception:
        return False
    return isinstance(result, dict) and result.get("status") == "committed"


def mix_seat_resume_stage(ctx: RunContext) -> str:
    """Single resume pin for mix ↔ junction ↔ finalize after music.

    All driver / path_to_master / safe_mix callers must use this — never
    ``is_done("mix")`` alone or ``assembly.wav`` existence as a seat proxy.

    - Not fully seated (mtime **and** commitment) → ``mix``
    - Seated, junction incomplete → ``junction_snip_qa``
    - Seated, junction complete → ``master_finalize``
    """
    if not mix_outputs_seated(ctx):
        return "mix"
    try:
        from interview_mux.homunculus.agenda import assembly_stale_versus_edl

        if assembly_stale_versus_edl(ctx):
            return "mix"
    except Exception:
        pass
    try:
        from interview_mux.delivery_guardrails import seed_stage_complete

        if not seed_stage_complete(ctx, "junction_snip_qa"):
            return "junction_snip_qa"
    except Exception:
        if not ctx.is_done("junction_snip_qa"):
            return "junction_snip_qa"
    return "master_finalize"


def assert_consumer(ctx: RunContext, stage: str) -> None:
    """Fail closed when mix/junction/finalize would read a mixed generation."""
    sel = _read_dict(ctx, SELECTION_REL)
    edl = _read_dict(ctx, EDL_REL)
    if sel is None or edl is None:
        return
    from interview_mux.order_hash import order_drift_heal_action

    action = order_drift_heal_action(sel, edl)
    if action in {"rebuild", "exclude_unseated"}:
        raise SystemExit(
            "selection_edl_order_drift: speech clip order diverges from "
            f"ordered_segment_ids (heal={action}) — sealed commit required before {stage}"
        )
    # A5-1 / seat authority: skip generation/commitment when assembly is missing
    # (first recut). Also when the ordering authority seats junction ahead of
    # mix (a recut is owed): the assembly then predates the EDL by design and
    # mix re-renders it after junction. Verifying it here halted the resume
    # with no legal producer (exec_055, ISSUES 76). A stale assembly with no
    # owed recut still verifies (A5).
    skip_pre_mix_commitment = False
    if stage == "junction_snip_qa":
        try:
            from interview_mux.mix_junction_seat import must_verify_commitment

            skip_pre_mix_commitment = not must_verify_commitment(ctx)
        except Exception:
            skip_pre_mix_commitment = not ctx.artifact_exists("master/assembly.wav")
        if not skip_pre_mix_commitment:
            try:
                from interview_mux.ordering_authority import ordering_exempt

                skip_pre_mix_commitment = bool(
                    ordering_exempt(ctx, "junction_snip_qa", "mix")
                )
            except Exception:
                pass
    if (
        stage in {"mix", "junction_snip_qa", "master_finalize"}
        and not skip_pre_mix_commitment
        and not live_generation_matches(ctx)
    ):
        live = generation(ctx)
        edl_gen = (edl or {}).get("air_order_generation")
        raise SystemExit(
            "assembly_not_rendered_from_current_edl: air_order generation mismatch "
            f"(live={live} edl={edl_gen}) — remaster mix from the sealed EDL"
        )
    if stage in {"junction_snip_qa", "master_finalize"} and not skip_pre_mix_commitment:
        try:
            from interview_mux.seam_autopsy import verify_commitment

            result = verify_commitment(ctx)
        except Exception:
            result = {}
        reasons = (
            [str(r) for r in (result.get("reasons") or [])] if isinstance(result, dict) else []
        )
        if isinstance(result, dict) and result.get("status") == "committed":
            return
        if "selection_edl_order_drift" in reasons:
            raise SystemExit(
                "selection_edl_order_drift: verify_commitment reasons=" + ",".join(reasons)
            )
        if "assembly_not_rendered_from_current_edl" in reasons or mix_stale_versus_live(ctx):
            raise SystemExit(
                "assembly_not_rendered_from_current_edl: verify_commitment reasons="
                + ",".join(reasons)
            )

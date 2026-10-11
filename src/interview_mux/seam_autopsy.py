"""Seam-level decision and commitment integrity for the final podcast.

The autopsy is deliberately deterministic.  It turns the current air order,
EDL, assembly ledger, junction findings, and sound-design plan into decisions
that downstream stages can consume.  It also proves that claimed junction
repairs exist in the EDL and in a freshly rendered assembly.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from interview_mux.config import merged_config
from interview_mux.order_hash import order_hashes_match
from interview_mux.run_context import RunContext

AUTOPSY_REL = "master/seam_autopsy.json"
RENDER_LEDGER_REL = "master/render_ledger.json"
VERSION = 1


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def seam_autopsy_cfg(cfg: dict[str, Any] | None = None) -> dict[str, Any]:
    master = (cfg or merged_config()).get("mastering") or {}
    block = master.get("seam_autopsy") if isinstance(master.get("seam_autopsy"), dict) else {}
    defaults = {
        "enabled": True,
        "commitment_blocks_finalize": True,
        "synthetic_share_guide_min": 0.2,
        "synthetic_share_guide_max": 0.8,
        "synthetic_duration_ratio_min": 0.4,
        "synthetic_duration_ratio_max": 2.0,
        "prefer_contiguous_beds": True,
    }
    return {**defaults, **block}


def _canonical_hash(value: Any) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(payload).hexdigest()


def _file_fingerprint(path: Path) -> dict[str, Any]:
    """Cheap stable fingerprint for large audio: size + first/last 1 MiB."""
    if not path.is_file():
        return {"exists": False, "size": 0, "sha256_edges": ""}
    size = path.stat().st_size
    h = hashlib.sha256()
    with path.open("rb") as fh:
        h.update(fh.read(1 << 20))
        if size > (1 << 20):
            fh.seek(max(0, size - (1 << 20)))
            h.update(fh.read(1 << 20))
    h.update(str(size).encode())
    return {
        "exists": True,
        "size": size,
        "sha256_edges": h.hexdigest(),
        "mtime_ns": path.stat().st_mtime_ns,
    }


def _pair_hash(after_id: str, before_id: str, order_hash: str) -> str:
    return _canonical_hash([after_id, before_id, order_hash])[:16]


def _pack_conflicts(selection: dict[str, Any]) -> list[dict[str, Any]]:
    """Live pack conflicts only — applied leftover inserts must not tank clarity.

    Selection ``_meta.repairs`` keeps historical insert_leftovers rows even after
    those ids land on air. Clarity must penalize unresolved leftovers only.
    """
    meta = selection.get("_meta") if isinstance(selection.get("_meta"), dict) else {}
    repairs = [r for r in (meta.get("repairs") or []) if isinstance(r, dict)]
    conflict_actions = {
        "include_narrative_chapter_segments",
        "unexclude_narrative_segments",
        "insert_chapter_leftovers_before_finale",
        "insert_leftovers_before_finale_span",
    }
    ordered = {str(s) for s in (selection.get("ordered_segment_ids") or []) if s}
    out: list[dict[str, Any]] = []
    for row in repairs:
        action = str(row.get("action") or "")
        if action not in conflict_actions:
            continue
        ids = [str(x) for x in (row.get("ids") or []) if x]
        unresolved = [i for i in ids if i not in ordered]
        if not unresolved:
            continue
        out.append(
            {
                "action": action,
                "count": len(unresolved),
                "ids": unresolved,
                "risk_code": "plan_pack_conflict",
            }
        )
    return out


def score_seam(
    seam: dict[str, Any], *, order_hash: str, cfg: dict[str, Any] | None = None
) -> dict[str, Any]:
    """Return an actionable, stable decision for one speech-to-speech seam."""
    settings = cfg or seam_autopsy_cfg()
    after_id = str(seam.get("after_segment_id") or "")
    before_id = str(seam.get("before_segment_id") or "")
    risks: list[str] = []
    if seam.get("naked"):
        risks.append("naked_seam")
    if seam.get("chapter_scale"):
        risks.append("chapter_jump")
    if str(seam.get("kind") or "") not in {"", "contiguous"}:
        risks.append("source_reorder")
    glue_ids = [str(x) for x in (seam.get("glue_piece_ids") or []) if x]
    if len(glue_ids) > 1:
        risks.append("synthetic_density")

    try:
        source_gap = abs(int(seam.get("source_gap_ms") or 0))
    except (TypeError, ValueError):
        source_gap = 0
    contiguous = not seam.get("requires_glue") and source_gap <= 2500
    continue_bed = contiguous and bool(settings.get("prefer_contiguous_beds", True))
    # A genuinely contiguous source seam that will *not* carry its bed across
    # (only possible when `prefer_contiguous_beds` is turned off) is an honest
    # music_hard_edge risk: continuous speech audio would get an audible bed
    # restart/cut. This keeps `music_completeness` (build_autopsy) and
    # `listen_delight._sonic_weave` non-trivial instead of a hard-coded 1.0 —
    # under the default (prefer_contiguous_beds=True) this never fires.
    if contiguous and not continue_bed:
        risks.append("music_hard_edge")
    if contiguous:
        preferred = ["extend_native", "air_pad"]
        synthetic_allowed = False
    elif seam.get("chapter_scale"):
        preferred = ["restore_native_setup", "bed_crossfade", "bespoke_spoken", "stinger"]
        synthetic_allowed = True
    else:
        preferred = ["restore_native_setup", "extend_native", "bed_crossfade", "bespoke_spoken"]
        synthetic_allowed = True

    penalty = 0.0
    penalty += 0.55 if seam.get("naked") else 0.0
    # Glued reorders are expected on long-form masters — only lightly penalize when
    # audible glue is present; keep the stronger hit for naked/unglued jumps.
    if "source_reorder" in risks:
        penalty += 0.05 if glue_ids else 0.15
    penalty += 0.1 if "synthetic_density" in risks else 0.0
    penalty += 0.08 if seam.get("chapter_scale") else 0.0
    penalty += 0.05 if "music_hard_edge" in risks else 0.0
    listen_score = round(max(0.0, min(1.0, 1.0 - penalty)), 4)
    glue_ideal = 0 if contiguous else (1800 if seam.get("chapter_scale") else 900)
    seam_id = f"{after_id}__{before_id}"
    return {
        "seam_id": seam_id,
        "after_segment_id": after_id,
        "before_segment_id": before_id,
        "listen_score": listen_score,
        "risk_codes": risks,
        "glue_budget_ms": {
            "min": 0 if contiguous else 300,
            "ideal": glue_ideal,
            "max": 4000 if seam.get("chapter_scale") else 2500,
        },
        "preferred_glue": preferred,
        "music_hint": {
            "continue_bed": continue_bed,
            "stinger": bool(seam.get("chapter_scale")),
            "crossfade_ms": 1500 if continue_bed else 2200,
        },
        "synthetic_voice_allowed": synthetic_allowed,
        "necessity_score": round(max(0.0, 1.0 - listen_score), 4),
        "pair_continuity_hash": _pair_hash(after_id, before_id, order_hash),
        "block_reason": "naked_seam" if seam.get("naked") else None,
    }


def _speech_clips(edl: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Last speech clip per segment_id (legacy). Prefer `_clip_for_repair`."""
    return {
        str(c.get("segment_id")): c
        for c in (edl.get("clips") or [])
        if isinstance(c, dict) and c.get("type") == "speech" and c.get("segment_id")
    }


def _clip_for_repair(edl: dict[str, Any], row: dict[str, Any]) -> dict[str, Any] | None:
    """Resolve the EDL clip a repair actually targeted (clip_index wins)."""
    clips = [c for c in (edl.get("clips") or []) if isinstance(c, dict)]
    sid = str(row.get("segment_id") or "")
    raw_idx = row.get("clip_index")
    if raw_idx is not None:
        try:
            idx = int(raw_idx)
        except (TypeError, ValueError):
            idx = -1
        if 0 <= idx < len(clips):
            clip = clips[idx]
            if clip.get("type") == "speech" and (
                not sid or str(clip.get("segment_id") or "") == sid
            ):
                return clip
    if not sid:
        return None
    return _speech_clips(edl).get(sid)


def _has_impact_hold(edl: dict[str, Any], segment_id: str) -> bool:
    clips = [c for c in (edl.get("clips") or []) if isinstance(c, dict)]
    for i, clip in enumerate(clips[:-1]):
        if clip.get("type") == "speech" and str(clip.get("segment_id") or "") == segment_id:
            nxt = clips[i + 1]
            return nxt.get("type") == "silence" and nxt.get("air_kind") == "impact_hold"
    return False


def _bound_edge_for_applied(row: dict[str, Any]) -> str | None:
    """Return start/end for bound-mutating repairs; None for non-bound actions."""
    action = str(row.get("action") or "")
    if action not in {
        "nudge_source_bounds",
        "extend_later",
        "cut_earlier",
        "thought_complete_recut",
    }:
        return None
    detail = row.get("detail") if isinstance(row.get("detail"), dict) else {}
    if action in {"extend_later", "cut_earlier", "thought_complete_recut"}:
        return "end"
    return str(detail.get("edge") or "end")


def _applied_repairs_resolved(
    edl: dict[str, Any], report: dict[str, Any]
) -> tuple[list[str], list[str]]:
    resolved: list[str] = []
    unresolved: list[str] = []
    applied_rows = [
        (index, row)
        for index, row in enumerate(report.get("applied") or [])
        if isinstance(row, dict) and row.get("status") in {"applied", "already_present"}
    ]
    # Two remediation runs often re-nudge the same edge on the same clip.
    # Only the last write per (segment, edge, clip_index) must match final EDL.
    # clip_index only tells apart the airings of a segment that airs more than
    # once. A clip inserted between runs shifts every later index, so for a
    # segment with one speech clip the run-1 row kept its own key and was held
    # to an end the run-2 rows replaced (exec_035 seg_017: 37 then 38).
    speech_count: dict[str, int] = {}
    for clip in edl.get("clips") or []:
        if isinstance(clip, dict) and clip.get("type") == "speech":
            csid = str(clip.get("segment_id") or "")
            speech_count[csid] = speech_count.get(csid, 0) + 1

    def _clip_key(row: dict[str, Any], sid: str) -> str:
        if speech_count.get(sid, 0) <= 1:
            return ""
        return str(row.get("clip_index") if row.get("clip_index") is not None else "")

    latest_bound: dict[tuple[str, str, str], int] = {}
    for index, row in applied_rows:
        edge = _bound_edge_for_applied(row)
        sid = str(row.get("segment_id") or "")
        if edge and sid:
            latest_bound[(sid, edge, _clip_key(row, sid))] = index

    for index, row in applied_rows:
        action = str(row.get("action") or "")
        sid = str(row.get("segment_id") or "")
        key = f"{index}:{action}:{sid}"
        edge = _bound_edge_for_applied(row)
        if edge and sid and latest_bound.get((sid, edge, _clip_key(row, sid))) != index:
            resolved.append(key)
            continue
        ok = True
        if action == "exclude_micro":
            ok = sid not in _speech_clips(edl)
        elif action in {
            "nudge_source_bounds",
            "extend_later",
            "cut_earlier",
            "thought_complete_recut",
        }:
            clip = _clip_for_repair(edl, row)
            detail = row.get("detail") if isinstance(row.get("detail"), dict) else {}
            # Prefer the bound actually written into the EDL (capped apply), not the
            # uncapped detector recommendation — otherwise commitment always diverges.
            rec = row.get("applied_ms")
            if rec is None:
                rec = row.get("keep_end_ms")
            if rec is None:
                rec = detail.get("recommended_ms") or detail.get("keep_end_ms")
            if clip is None or rec is None:
                ok = False
            elif edge == "start":
                ok = abs(int(clip.get("source_start_ms") or 0) - int(rec)) <= 500
            else:
                ok = abs(int(clip.get("source_end_ms") or 0) - int(rec)) <= 500
        elif action == "insert_impact_hold":
            ok = _has_impact_hold(edl, sid)
        # Music adjustments are verified by the placement artifact/mix stamp,
        # not by speech clips.  The fresh assembly fingerprint below covers them.
        (resolved if ok else unresolved).append(key)
    return resolved, unresolved


def verify_commitment(
    ctx: RunContext,
    snip_report: dict[str, Any] | None = None,
    *,
    edl: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Prove claimed repairs, order authority, and a fresh assembly agree."""
    edl_doc = edl if isinstance(edl, dict) else (
        ctx.read_json("master/edl.json") if ctx.artifact_exists("master/edl.json") else {}
    )
    report = snip_report if isinstance(snip_report, dict) else (
        ctx.read_json("master/junction_snip_qa.json")
        if ctx.artifact_exists("master/junction_snip_qa.json")
        else {}
    )
    selection = (
        ctx.read_json("master/selection.json")
        if ctx.artifact_exists("master/selection.json")
        else {}
    )
    resolved, unresolved = _applied_repairs_resolved(edl_doc, report)
    order_ok = bool(selection and edl_doc and order_hashes_match(selection, edl_doc))
    # Commitment freshness must use the committed run tree — a stale
    # .pending_writes/*/master/assembly.wav must not shadow a remastered final.
    assembly_path = ctx.final_path("master", "assembly.wav")
    edl_path = ctx.final_path("master", "edl.json")
    assembly_fp = _file_fingerprint(assembly_path)
    ledger = {}
    if ctx.artifact_exists(RENDER_LEDGER_REL):
        try:
            ledger = ctx.read_json(RENDER_LEDGER_REL) or {}
        except Exception:
            ledger = {}
    ledger_hash = ""
    if isinstance(ledger, dict):
        assembly_block = ledger.get("assembly")
        if isinstance(assembly_block, dict):
            ledger_hash = str(
                assembly_block.get("sha256_edges")
                or assembly_block.get("assembly_sha256_edges")
                or ""
            )
        if not ledger_hash:
            ledger_hash = str(
                ledger.get("assembly_sha256_edges") or ledger.get("sha256_edges") or ""
            )
    content_ok = bool(
        assembly_fp.get("exists")
        and ledger_hash
        and ledger_hash == str(assembly_fp.get("sha256_edges") or "")
    )
    fresh = bool(content_ok and assembly_fp.get("exists") and edl_path.is_file())
    reasons: list[str] = []
    if unresolved:
        reasons.append("claimed_repairs_missing_from_edl")
    if selection and not order_ok:
        reasons.append("selection_edl_order_drift")
    if not fresh:
        reasons.append("assembly_not_rendered_from_current_edl")
    status = "committed" if not reasons else "diverged"
    return {
        "status": status,
        "verified_at": _now(),
        "edl_hash": _canonical_hash(edl_doc),
        "assembly": assembly_fp,
        "repairs_claimed": len(resolved) + len(unresolved),
        "repairs_committed": len(resolved),
        "resolved_repair_keys": resolved,
        "unresolved_repair_keys": unresolved,
        "order_hash_match": order_ok,
        "assembly_fresh": fresh,
        "reasons": reasons,
    }


def build_autopsy(
    ctx: RunContext,
    *,
    phase: str,
    snip_report: dict[str, Any] | None = None,
    edl: dict[str, Any] | None = None,
) -> dict[str, Any]:
    selection = (
        ctx.read_json("master/selection.json")
        if ctx.artifact_exists("master/selection.json")
        else {}
    )
    edl_doc = edl if isinstance(edl, dict) else (
        ctx.read_json("master/edl.json") if ctx.artifact_exists("master/edl.json") else {}
    )
    ledger: dict[str, Any] = {}
    if ctx.artifact_exists("master/assembly_ledger.json"):
        loaded = ctx.read_json("master/assembly_ledger.json")
        ledger = loaded if isinstance(loaded, dict) else {}
    elif edl_doc:
        from interview_mux.assembly_ledger import build_assembly_ledger

        ledger = build_assembly_ledger(ctx, edl=edl_doc)

    order_hash = str(
        edl_doc.get("order_content_hash")
        or selection.get("order_content_hash")
        or ledger.get("order_content_hash")
        or ""
    )
    seams = [
        score_seam(s, order_hash=order_hash)
        for s in (ledger.get("seams") or [])
        if isinstance(s, dict)
    ]
    mean = sum(float(s["listen_score"]) for s in seams) / max(1, len(seams))
    synthetic = sum(
        1
        for c in (edl_doc.get("clips") or [])
        if isinstance(c, dict) and c.get("type") in {"vo_pickup", "transition"}
    )
    speech = sum(
        1
        for c in (edl_doc.get("clips") or [])
        if isinstance(c, dict) and c.get("type") == "speech"
    )
    synthetic_share = synthetic / max(1, synthetic + speech)
    commitment = (
        verify_commitment(ctx, snip_report, edl=edl_doc)
        if phase in {"post_junction", "post_master"}
        else {"status": "pending", "verified_at": _now()}
    )
    worst = sorted(seams, key=lambda s: float(s["listen_score"]))[:20]
    n_seams = max(1, len(seams))
    hard_edges = sum(1 for s in seams if "music_hard_edge" in (s.get("risk_codes") or []))
    # LD4: continuous music completeness (not binary {0.5, 1.0}).
    music_completeness = round(max(0.0, min(1.0, 1.0 - (hard_edges / n_seams))), 4)
    # Distinct clarity signal: penalize pack conflicts + synthetic overload separately from seam mean.
    pack_n = len(_pack_conflicts(selection))
    clarity = round(
        max(0.0, min(1.0, mean - 0.04 * pack_n - max(0.0, abs(synthetic_share - 0.35) - 0.15))),
        4,
    )
    return {
        "version": VERSION,
        "generated_at": _now(),
        "phase": phase,
        "order_content_hash": order_hash,
        "commitment": commitment,
        "scores": {
            "continuity": round(mean, 4),
            "finishability": round(max(0.0, mean - 0.05 * pack_n), 4),
            "sonic_density_fit": round(max(0.0, 1.0 - abs(synthetic_share - 0.35)), 4),
            "information_clarity": clarity,
            "music_completeness": music_completeness,
        },
        "guides": {
            "synthetic_input_share": round(synthetic_share, 4),
            "synthetic_share_min": seam_autopsy_cfg()["synthetic_share_guide_min"],
            "synthetic_share_max": seam_autopsy_cfg()["synthetic_share_guide_max"],
            "music_hard_edge_count": hard_edges,
            "music_seam_count": n_seams,
        },
        "pack_conflicts": _pack_conflicts(selection),
        "seams": seams,
        "worst_seam_ids": [str(s["seam_id"]) for s in worst],
        "blocking_reasons": sorted(
            {str(s["block_reason"]) for s in seams if s.get("block_reason")}
            | set(commitment.get("reasons") or [])
        ),
    }


def write_autopsy(ctx: RunContext, doc: dict[str, Any]) -> dict[str, Any]:
    from interview_mux.write_staging import write_committed_json

    write_committed_json(ctx, AUTOPSY_REL, doc)
    return doc


def refresh_autopsy_commitment(ctx: RunContext) -> dict[str, Any] | None:
    """Re-verify commitment against current EDL/assembly and rewrite autopsy.

    Used when an archived autopsy still says ``diverged`` but the working tree
    has since been remastered / order-synced.
    """
    if not ctx.artifact_exists(AUTOPSY_REL):
        return None
    autopsy = ctx.read_json(AUTOPSY_REL)
    if not isinstance(autopsy, dict):
        return None
    snip = (
        ctx.read_json("master/junction_snip_qa.json")
        if ctx.artifact_exists("master/junction_snip_qa.json")
        else None
    )
    edl = ctx.read_json("master/edl.json") if ctx.artifact_exists("master/edl.json") else None
    commitment = verify_commitment(
        ctx,
        snip if isinstance(snip, dict) else None,
        edl=edl if isinstance(edl, dict) else None,
    )
    out = dict(autopsy)
    out["commitment"] = commitment
    out["generated_at"] = _now()
    prior_blocks = {
        str(x)
        for x in (autopsy.get("blocking_reasons") or [])
        if str(x)
        not in {
            "assembly_not_rendered_from_current_edl",
            "selection_edl_order_drift",
            "claimed_repairs_missing_from_edl",
        }
    }
    out["blocking_reasons"] = sorted(prior_blocks | set(commitment.get("reasons") or []))
    write_autopsy(ctx, out)
    return out


def _ledger_annotation_permitted(ctx: RunContext) -> bool:
    """True when the active stage may annotate the assembly ledger.

    The autopsy annotation is pure telemetry on a delivery ledger owned by
    edl/mix/junction_snip_qa. The ship pass (master_finalize) re-scans the autopsy
    and used to raise here, killing the whole post-master quality pass
    (exec_11871). A sealed ledger simply keeps its prior annotation.
    """
    try:
        from interview_mux.artifact_ownership import write_permitted
        from interview_mux.write_staging import active_stage_id

        stage_now = str(active_stage_id() or "")
        if not stage_now:
            return True
        allowed, reason = write_permitted(
            ctx,
            "master/assembly_ledger.json",
            stage_now,
            role="producer",
            verb="persist",
        )
    except Exception:
        return True
    if not allowed:
        try:
            ctx.log(
                "seam autopsy: assembly_ledger sealed — autopsy annotation skipped "
                f"({reason})",
                level="info",
                stage=stage_now or None,
            )
        except Exception:
            pass
        return False
    return True


def enrich_ledger(ctx: RunContext, autopsy: dict[str, Any]) -> dict[str, Any] | None:
    if not ctx.artifact_exists("master/assembly_ledger.json"):
        return None
    if not _ledger_annotation_permitted(ctx):
        return None
    ledger = ctx.read_json("master/assembly_ledger.json")
    if not isinstance(ledger, dict):
        return None
    decisions = {
        (str(s.get("after_segment_id") or ""), str(s.get("before_segment_id") or "")): s
        for s in (autopsy.get("seams") or [])
        if isinstance(s, dict)
    }
    seams: list[dict[str, Any]] = []
    for seam in ledger.get("seams") or []:
        if not isinstance(seam, dict):
            continue
        row = dict(seam)
        key = (str(row.get("after_segment_id") or ""), str(row.get("before_segment_id") or ""))
        decision = decisions.get(key)
        if decision:
            row["autopsy"] = {
                k: decision.get(k)
                for k in (
                    "seam_id",
                    "listen_score",
                    "risk_codes",
                    "glue_budget_ms",
                    "preferred_glue",
                    "music_hint",
                    "synthetic_voice_allowed",
                    "necessity_score",
                    "pair_continuity_hash",
                    "block_reason",
                )
            }
        seams.append(row)
    out = dict(ledger)
    out["seams"] = seams
    out["autopsy_version"] = VERSION
    out["autopsy_generated_at"] = autopsy.get("generated_at")
    out["seam_listen_score_mean"] = (autopsy.get("scores") or {}).get("continuity")
    out["worst_seam_ids"] = autopsy.get("worst_seam_ids") or []
    from interview_mux.write_staging import write_committed_json

    write_committed_json(ctx, "master/assembly_ledger.json", out)
    return out


def write_render_ledger(ctx: RunContext, *, edl: dict[str, Any] | None = None) -> dict[str, Any]:
    """Stamp the realized mix against its EDL for later commitment checks.

    Fingerprint the committed final assembly — ``read_path`` can still see a
    stale pending WAV and desync ledger vs ``verify_commitment`` (final_path),
    which leaves ``assembly_not_rendered_from_current_edl`` after a successful
    mix (exec_13167).
    """
    doc = edl if isinstance(edl, dict) else ctx.read_json("master/edl.json")
    assembly = _file_fingerprint(ctx.final_path("master", "assembly.wav"))
    clips = [c for c in (doc.get("clips") or []) if isinstance(c, dict)]
    out = {
        "version": 1,
        "generated_at": _now(),
        "edl_hash": _canonical_hash(doc),
        "order_content_hash": doc.get("order_content_hash"),
        "assembly": assembly,
        "timeline_duration_ms": doc.get("timeline_duration_ms"),
        "clips": [
            {
                "type": c.get("type"),
                "segment_id": c.get("segment_id"),
                "line_id": c.get("line_id"),
                "timeline_start_ms": c.get("timeline_start_ms"),
                "duration_ms": c.get("duration_ms"),
            }
            for c in clips
        ],
    }
    try:
        from interview_mux.air_order import generation as air_generation

        gen = int(air_generation(ctx) or 0)
        if gen:
            out["air_order_generation"] = gen
    except Exception:
        pass
    from interview_mux.write_staging import write_committed_json

    write_committed_json(ctx, RENDER_LEDGER_REL, out)
    return out

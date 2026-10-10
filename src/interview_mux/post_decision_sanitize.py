"""Post-decision sanitize — gap inventory + shared-path authority restamp.

Wave 0.2: after major decisions (resplit, framing, remutate, …) write a gap
inventory and unmark only implicated producers. Shared paths stamp
``authoritative_producer`` + content hash under ``_meta`` without breaking
body schemas.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from interview_mux.run_context import RunContext

SHARED_PATHS: dict[str, str] = {
    "content_brief": "understanding/content_brief.json",
    "boundaries": "segments/boundaries.json",
    "sound_design_plan": "understanding/sound_design_plan.json",
}

# A-05 peer unmark set — intentionally narrower than ownership ALLOW.
# Full ALLOW includes hitch/fuse/overlap/ideal_cuts; unmarking all of them on
# every fingerprint flip would over-rewind. Keep primary analysis pairs here.
SHARED_PATH_CO_PRODUCERS: dict[str, tuple[str, ...]] = {
    "understanding/content_brief.json": ("content_context", "content_brief_reanchor"),
    "segments/boundaries.json": ("boundary_detection", "boundary_topic_resplit"),
    "understanding/sound_design_plan.json": ("sound_design_palettes", "sound_design_plan"),
}

GAP_INVENTORY_DIR = "operator/gap_inventory"


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def resolve_shared_path(name_or_rel: str) -> str:
    key = str(name_or_rel or "").strip()
    if key in SHARED_PATHS:
        return SHARED_PATHS[key]
    return key.replace("\\", "/").lstrip("/")


def stamp_authoritative_producer(
    ctx: RunContext,
    rel: str,
    producer_stage: str,
) -> dict[str, Any] | None:
    """Write ``_meta.authoritative_producer`` + content hash on a shared artifact.

    Uses the lifecycle fingerprint helper so body schemas stay intact (``_meta``
    only). Writes the committed path via ``file_store`` — never ``ctx.write_json`` —
    so admit/sanitize cannot oscillate hashes mid-stamp. Returns the stamped
    document, or None when the path is missing / unreadable.
    """
    path = resolve_shared_path(rel)
    stage = str(producer_stage or "").strip()
    if not path or not stage:
        return None
    if not ctx.artifact_exists(path):
        return None
    try:
        raw = ctx.read_json(path)
    except Exception:
        return None
    if not isinstance(raw, dict):
        return None

    from interview_mux.artifact_lifecycle import fingerprint_artifact
    from interview_mux.file_store import write_json as fs_write_json

    fp = fingerprint_artifact(raw, stage)
    meta = dict(fp.get("_meta") or {})
    meta["authoritative_producer"] = stage
    meta["authoritative_stamped_at"] = _utc_now()
    fp["_meta"] = meta
    dest = ctx.final_path(*path.split("/"))
    dest.parent.mkdir(parents=True, exist_ok=True)
    fs_write_json(dest, fp)
    h = str(meta.get("content_hash") or "")
    if h and hasattr(ctx, "mutate_run_meta"):

        def _mut(run_meta: dict[str, Any]) -> None:
            fps = dict(run_meta.get("artifact_fingerprints") or {})
            fps[path] = {
                "hash": h,
                "producer_stage": stage,
                "authoritative_producer": stage,
            }
            run_meta["artifact_fingerprints"] = fps

        try:
            ctx.mutate_run_meta(_mut)
        except Exception:
            pass
    return fp


def after_shared_path_write(
    ctx: RunContext,
    rel: str,
    writer_stage: str,
) -> dict[str, Any]:
    """A-05: on brief/boundaries/SDP writes — stamp producer + reconcile co-producer done.

    Unmarks other co-producers for *that* path only when content fingerprint flips
    after a prior stamp (observe→unmark). Initial/scaffold stamps do not unmark.
    Skips while an admit is in flight (stamp after sole write settles).
    """
    try:
        from interview_mux.artifact_sanitize.reentry import admitting

        if admitting(ctx):
            return {"ok": True, "skipped": "admitting"}
    except Exception:
        pass

    path = resolve_shared_path(rel)
    writers = SHARED_PATH_CO_PRODUCERS.get(path)
    stage = str(writer_stage or "").strip()
    if not writers or stage not in writers:
        return {"ok": False, "reason": "not_shared_writer"}

    if getattr(ctx, "_shared_path_stamping", False):
        return {"ok": True, "skipped": "reentrant"}

    prev_hash = ""
    prev_producer = ""
    try:
        meta = ctx.read_json("run_meta.json") if ctx.artifact_exists("run_meta.json") else {}
        if isinstance(meta, dict):
            row = (meta.get("artifact_fingerprints") or {}).get(path) or {}
            if isinstance(row, dict):
                prev_hash = str(row.get("hash") or "")
                prev_producer = str(row.get("authoritative_producer") or "")
    except Exception:
        pass

    try:
        ctx._shared_path_stamping = True
        stamped = stamp_authoritative_producer(ctx, path, stage)
    finally:
        ctx._shared_path_stamping = False

    new_hash = ""
    if isinstance(stamped, dict) and isinstance(stamped.get("_meta"), dict):
        new_hash = str(stamped["_meta"].get("content_hash") or "")

    if prev_hash and new_hash and prev_hash == new_hash and prev_producer == stage:
        return {"ok": True, "fingerprint_unchanged": True, "cleared": []}

    # First authoritative stamp (scaffold / bootstrap): observe only — do not
    # unmark co-producers. Otherwise ensure_analysis_workspace SDP scaffolds
    # wipe sound_design_palettes mid eligibility/auto-accept (gap framing gates).
    if not prev_hash:
        return {"ok": True, "fingerprint_initial": True, "cleared": []}

    # Only co-producers *downstream* of the writer are stale. A later writer
    # (sound_design_plan, content_brief_reanchor, boundary_topic_resplit) never
    # invalidates the earlier producer of the same path: unmarking it makes the
    # analysis walk re-run it, which rewrites the path under its own producer
    # stamp and unmarks the later writer again. That loop cost one LLM call per
    # cycle and stalled the 6-minute run at 53 of 72 (ISSUES entry 46).
    to_clear = [s for s in _downstream_co_producers(writers, stage)]
    if not to_clear:
        return {"ok": True, "cleared": [], "upstream_protected": True}
    return post_decision_sanitize(
        ctx,
        f"shared_restamp_{stage}",
        implicated_stages=to_clear,
        reason=f"A-05 co-producer reconcile after {stage} wrote {path}",
    )


def _downstream_co_producers(writers: tuple[str, ...], stage: str) -> list[str]:
    """Co-producers that run after ``stage`` in pipeline order (unknown → after)."""
    try:
        from interview_mux.v2.config import ANALYSIS_ORDER, DELIVERY_ORDER

        order = list(ANALYSIS_ORDER) + list(DELIVERY_ORDER)
    except Exception:
        order = []

    def _idx(sid: str) -> int:
        return order.index(sid) if sid in order else len(order)

    mine = _idx(stage)
    return [s for s in writers if s != stage and _idx(s) >= mine]


def co_producers_for(rel: str) -> tuple[str, ...]:
    path = resolve_shared_path(rel)
    return SHARED_PATH_CO_PRODUCERS.get(path, ())


def _music_blocks(ctx: RunContext, stage_id: str, *, source: str) -> bool:
    try:
        from interview_mux.delivery_guardrails import music_clear_blocked

        return bool(music_clear_blocked(ctx, stage_id, source=source))
    except Exception:
        return False


def _rewind_locked(ctx: RunContext, stage_id: str) -> bool:
    """True when clearing ``stage_id`` would create an unsatisfiable prerequisite.

    After G0, ``_refuse_delivery_timeline_rewind`` refuses to re-run a protected
    core stage whose artifacts already exist, so that the classified tape is not
    rewound. Unmarking such a stage therefore produces a prerequisite that can
    never be satisfied: downstream dispatch raises "seed order: complete <stage>",
    the stage itself refuses to run, and recovery_controller loops on
    resume=<stage>.

    That is exactly what a shared artifact triggers. content_context and
    content_brief_reanchor both own understanding/content_brief.json, so when
    boundary_topic_resplit nests content_brief_reanchor and rewrites that file,
    after_shared_path_write lands here and clears content_context, which had
    legitimately completed and cannot be re-run. The refinement did not
    invalidate the upstream producer's work, so its marker must stand.
    """
    try:
        from interview_mux.homunculus.agenda import (
            DELIVERY_LOCKED_TIMELINE_STAGES,
            PROTECTED_CORE_STAGES,
        )
        from interview_mux.homunculus.packer import g0_closed

        if stage_id not in DELIVERY_LOCKED_TIMELINE_STAGES:
            return False
        if not g0_closed(ctx):
            return False
        needed = PROTECTED_CORE_STAGES.get(stage_id) or ()
        if not needed:
            return False
        # Only locked while the artifacts that make the re-run refusable exist.
        if not all(ctx.artifact_exists(rel) for rel in needed):
            return False
        # The guard lets a stage run while its own output is incomplete for it,
        # so clearing such a stage leaves a prerequisite it can satisfy.
        from interview_mux.stage_completion import stage_artifact_incompleteness

        return not stage_artifact_incompleteness(ctx, stage_id)
    except Exception:
        return False


def post_decision_sanitize(
    ctx: RunContext,
    decision_id: str,
    *,
    implicated_stages: list[str] | tuple[str, ...] | None = None,
    profile_id: str = "",
    shared_path: str = "",
    producer_stage: str = "",
    gaps: list[Any] | None = None,
    reason: str = "",
) -> dict[str, Any]:
    """Write gap inventory and unmark only implicated stages.

    Prefer ``apply_bounded_invalidation(profile_id)`` when a profile is named;
    otherwise unlink ``.stage_done`` for each implicated stage (honoring
    ``music_clear_blocked``).
    """
    did = str(decision_id or "").strip() or "unnamed"
    stages = [str(s).strip() for s in (implicated_stages or ()) if str(s).strip()]
    inventory: dict[str, Any] = {
        "version": 1,
        "decision_id": did,
        "updated_at": _utc_now(),
        "reason": str(reason or ""),
        "implicated_stages": list(stages),
        "profile_id": str(profile_id or ""),
        "shared_path": resolve_shared_path(shared_path) if shared_path else "",
        "producer_stage": str(producer_stage or ""),
        "gaps": list(gaps or []),
        "cleared": [],
        "blocked_music": [],
        "stamp": None,
    }

    stamp_doc = None
    if shared_path and producer_stage:
        stamp_doc = stamp_authoritative_producer(ctx, shared_path, producer_stage)
        if stamp_doc is not None:
            meta = stamp_doc.get("_meta") if isinstance(stamp_doc, dict) else {}
            inventory["stamp"] = {
                "authoritative_producer": (meta or {}).get("authoritative_producer"),
                "content_hash": (meta or {}).get("content_hash"),
            }

    cleared: list[str] = []
    blocked: list[str] = []
    blocked_rewind: list[str] = []
    profile = str(profile_id or "").strip()
    if profile:
        from interview_mux.execution_invalidation_profiles import apply_bounded_invalidation

        result = apply_bounded_invalidation(
            ctx, profile, reason=reason or f"post_decision:{did}"
        )
        cleared = list(result.get("cleared") or [])
        inventory["invalidation"] = {
            "profile_id": result.get("profile_id"),
            "capped": bool(result.get("capped")),
            "forbidden_skipped": list(result.get("forbidden_skipped") or []),
        }
    else:
        for sid in stages:
            if _music_blocks(ctx, sid, source=f"post_decision:{did}"):
                blocked.append(sid)
                continue
            if _rewind_locked(ctx, sid):
                # Clearing this would demand a re-run the rewind guard refuses.
                blocked_rewind.append(sid)
                continue
            marker = ctx.run_dir / ".stage_done" / sid
            if marker.is_file():
                marker.unlink()
                cleared.append(sid)

    inventory["cleared"] = cleared
    inventory["blocked_music"] = blocked
    inventory["blocked_rewind_locked"] = blocked_rewind
    if blocked_rewind:
        try:
            ctx.log(
                "post_decision_sanitize kept "
                + ", ".join(blocked_rewind)
                + " marked: the post-G0 rewind guard refuses to re-run them, so "
                "clearing would leave an unsatisfiable seed-order prerequisite",
                level="info",
                stage=str(producer_stage or "") or None,
                detail={"event": "rewind_locked_keep", "stages": list(blocked_rewind)},
            )
        except Exception:
            pass

    rel = f"{GAP_INVENTORY_DIR}/{did}.json"
    try:
        ctx.write_json(rel, inventory, skip_handoff=True)
    except Exception:
        dest = ctx.run_dir / GAP_INVENTORY_DIR
        dest.mkdir(parents=True, exist_ok=True)
        from interview_mux.file_store import write_json as fs_write_json

        fs_write_json(dest / f"{did}.json", inventory)

    inventory["inventory_rel"] = rel
    return inventory

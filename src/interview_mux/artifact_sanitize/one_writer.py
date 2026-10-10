"""One-writer admission for hot delivery JSON artifacts.

Hardness: every persist of a hot authority file must go through a single commit
API (sanitize → stamp → one disk write → invalidate). Parallel ``write_json`` /
``write_committed_json`` paths are routed here; ``fs_write_json`` of hot rels
is banned outside sanitize / staging / tests (see ``tools/check_*_write_paths.sh``).

EDL note: auto-admit sanitizes and writes only. Sealed generation + selection
reconcile still go through ``write_live_edl`` / ``air_order.commit`` explicitly.

Escape hatch: set ``ctx._one_writer_raw = True`` for intentional seed writes
(tests / progression fixtures). Internal commit helpers set
``ctx._one_writer_admit`` while persisting to avoid recursion.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from interview_mux.run_context import RunContext

SELECTION_REL = "master/selection.json"
GAP_REL = "understanding/gap_report.json"
EDL_REL = "master/edl.json"
TRANSITIONS_REL = "master/transitions.json"
SDP_REL = "understanding/sound_design_plan.json"
LAYUP_REL = "understanding/nugget_layup_plan.json"

HOT_ARTIFACT_RELS: frozenset[str] = frozenset(
    {
        SELECTION_REL,
        GAP_REL,
        EDL_REL,
        TRANSITIONS_REL,
        SDP_REL,
        LAYUP_REL,
    }
)


def is_hot_artifact(rel: str) -> bool:
    return str(rel or "").replace("\\", "/").strip("/") in HOT_ARTIFACT_RELS


def _raw_escape(ctx: RunContext) -> bool:
    return bool(getattr(ctx, "_one_writer_raw", False))


def admitting(ctx: RunContext) -> bool:
    return bool(getattr(ctx, "_one_writer_admit", False))


def begin_admit(ctx: RunContext) -> None:
    setattr(ctx, "_one_writer_admit", True)


def end_admit(ctx: RunContext) -> None:
    try:
        delattr(ctx, "_one_writer_admit")
    except Exception:
        setattr(ctx, "_one_writer_admit", False)


def maybe_admit_hot_write(
    ctx: RunContext,
    rel: str,
    data: Any,
    *,
    stage_key: str | None = None,
    skip_handoff: bool = False,
    write_committed: bool = False,
    reason: str = "",
    mutation_class: str | None = None,
) -> Path | None:
    """If ``rel`` is hot, persist via the sole commit API and return the path.

    Returns ``None`` when the caller should continue with a normal write
    (non-hot, raw escape, or already inside an admit).

    A′′ Global Freeze: under freeze, ``_one_writer_raw`` cannot bypass gap /
    transitions / SDP — those always admit (commit then End-A-or-skip).
    """
    rel_n = str(rel or "").replace("\\", "/").strip("/")
    if rel_n not in HOT_ARTIFACT_RELS:
        return None
    if not isinstance(data, dict):
        return None
    if admitting(ctx):
        return None
    if _raw_escape(ctx):
        try:
            from interview_mux.seat_authority import freeze_blocks_raw_escape

            if not freeze_blocks_raw_escape(ctx, rel_n):
                return None
            # Fall through to admit under freeze (raw escape blocked).
        except Exception:
            return None

    begin_admit(ctx)
    try:
        return _admit_impl(
            ctx,
            rel_n,
            data,
            stage_key=stage_key,
            skip_handoff=skip_handoff,
            write_committed=write_committed,
            reason=reason or stage_key or "one_writer_admit",
            mutation_class=mutation_class,
        )
    finally:
        end_admit(ctx)


def _admit_impl(
    ctx: RunContext,
    rel: str,
    data: dict[str, Any],
    *,
    stage_key: str | None,
    skip_handoff: bool,
    write_committed: bool,
    reason: str,
    mutation_class: str | None = None,
) -> Path:
    if rel == SELECTION_REL:
        from interview_mux.air_order_boundary import commit_selection_mutation

        commit_selection_mutation(
            ctx,
            data,
            producer=str(stage_key or reason or "one_writer"),
            stage_key=str(stage_key or "selection"),
            write_committed=write_committed,
            skip_handoff=skip_handoff,
            skip_checkpoint=False,
            checkpoint_mode="detect",
            mutation_class=mutation_class,
        )
        return ctx.final_path(*SELECTION_REL.split("/"))

    if rel == GAP_REL:
        from interview_mux.artifact_sanitize.gap_report import commit_gap_report_doc

        commit_gap_report_doc(
            ctx,
            data,
            reason=reason,
            skip_handoff=skip_handoff,
            stage_key=stage_key,
            mutation_class=mutation_class,
        )
        return ctx.final_path(*GAP_REL.split("/"))

    if rel == EDL_REL:
        # Sanitize + single write. Do NOT call write_live_edl here — that seals a
        # generation and reconciles selection (exclude_unseated). Production code
        # that needs a sealed generation must call write_live_edl explicitly.
        from interview_mux.artifact_sanitize.admit import admit_sanitized
        from interview_mux.artifact_sanitize.edl import sanitize_edl
        from interview_mux.artifact_sanitize.reentry import stamp_sanitize_meta
        from interview_mux.artifact_sanitize.types import SanitizeResult

        def _edl_sanitize(c: Any, d: dict[str, Any]) -> SanitizeResult:
            result = sanitize_edl(c, dict(d))
            out = result.doc if result.ok and isinstance(result.doc, dict) else dict(d)
            out.pop("_pending_ledger", None)
            if result.ok:
                out = stamp_sanitize_meta(
                    out,
                    ok=True,
                    source=reason or "one_writer_edl",
                    actions_n=len(result.actions or []),
                )
            return SanitizeResult(
                doc=out,
                ok=result.ok,
                errors=list(result.errors or []),
                actions=list(result.actions or []),
                metrics=dict(result.metrics or {}),
            )

        admit_sanitized(
            ctx,
            EDL_REL,
            data,
            sanitize_fn=_edl_sanitize,
            stage_key=stage_key or "edl",
            skip_handoff=skip_handoff,
            write_committed=False,
            refuse_if_unsanitary=False,
            action_class=reason or "one_writer_edl",
            mutation_class=mutation_class,
        )
        return ctx.final_path(*EDL_REL.split("/"))

    if rel == TRANSITIONS_REL:
        return commit_transitions_doc(
            ctx,
            data,
            stage_key=stage_key,
            skip_handoff=skip_handoff,
            reason=reason,
            mutation_class=mutation_class,
        )

    if rel == SDP_REL:
        return commit_sound_design_plan_doc(
            ctx,
            data,
            stage_key=stage_key,
            skip_handoff=skip_handoff,
            reason=reason,
            mutation_class=mutation_class,
        )

    if rel == LAYUP_REL:
        return commit_nugget_layup_plan_doc(
            ctx,
            data,
            stage_key=stage_key,
            skip_handoff=skip_handoff,
            reason=reason,
            mutation_class=mutation_class,
        )

    raise RuntimeError(f"one_writer: unhandled hot rel {rel}")


def commit_transitions_doc(
    ctx: RunContext,
    doc: dict[str, Any],
    *,
    stage_key: str | None = None,
    skip_handoff: bool = False,
    reason: str = "",
    mutation_class: str | None = None,
) -> Path:
    """Sole transitions persist: retain pairs → sanitize → one write (+ cascade)."""
    from interview_mux.artifact_sanitize.admit import admit_sanitized
    from interview_mux.artifact_sanitize.reentry import stamp_sanitize_meta
    from interview_mux.artifact_sanitize.transitions import sanitize_transitions
    from interview_mux.artifact_sanitize.types import SanitizeResult
    from interview_mux.transition_vo import retain_required_transition_pairs

    enda_reason = str(reason or stage_key or "").strip()
    try:
        from interview_mux.seat_authority import frozen_seat_write_allowed

        # The End-A reason is matched exactly; a segment id remap is admitted
        # on its own and never by suffixing the reason.
        if mutation_class != "segment_id_remap" and not frozen_seat_write_allowed(
            ctx, TRANSITIONS_REL, reason=enda_reason
        ):
            return ctx.final_path(*TRANSITIONS_REL.split("/"))
    except ImportError:
        pass

    nested = admitting(ctx)
    if not nested:
        begin_admit(ctx)
    try:
        retained, notes = retain_required_transition_pairs(ctx, doc)
        if notes:
            try:
                ctx.log(
                    f"transitions: retained {len(notes)} adjacency-required pair(s)",
                    level="info",
                    stage=stage_key or "transitions",
                    detail={"pairs": notes[:12]},
                )
            except Exception:
                pass

        def _tr_sanitize(c: Any, d: dict[str, Any]) -> SanitizeResult:
            result = sanitize_transitions(c, dict(d))
            out = result.doc if result.ok and isinstance(result.doc, dict) else dict(d)
            if result.ok:
                out = stamp_sanitize_meta(
                    out,
                    ok=True,
                    source=reason or "commit_transitions_doc",
                    actions_n=len(result.actions or []),
                )
            return SanitizeResult(
                doc=out,
                ok=result.ok,
                errors=list(result.errors or []),
                actions=list(result.actions or []),
                metrics=dict(result.metrics or {}),
            )

        admit_sanitized(
            ctx,
            TRANSITIONS_REL,
            retained,
            sanitize_fn=_tr_sanitize,
            stage_key=stage_key or "transitions",
            skip_handoff=skip_handoff,
            write_committed=False,
            refuse_if_unsanitary=False,
            action_class=reason or "commit_transitions_doc",
            mutation_class=mutation_class,
        )
        return ctx.final_path(*TRANSITIONS_REL.split("/"))
    finally:
        if not nested:
            end_admit(ctx)

def commit_sound_design_plan_doc(
    ctx: RunContext,
    doc: dict[str, Any],
    *,
    stage_key: str | None = None,
    skip_handoff: bool = False,
    reason: str = "",
    mutation_class: str | None = None,
) -> Path:
    """Sole SDP persist: harden when schema-invalid → sanitize → stamp → one write."""
    from interview_mux.analysis_memory import default_sound_design_plan
    from interview_mux.artifact_sanitize.reentry import stamp_sanitize_meta
    from interview_mux.artifact_sanitize.sound_design_plan import (
        sanitize_sound_design_plan,
    )
    from interview_mux.prompt_validation import validate_artifact_write

    enda_reason = str(reason or stage_key or "").strip()
    try:
        from interview_mux.seat_authority import frozen_seat_write_allowed

        # The End-A reason is matched exactly; a segment id remap is admitted
        # on its own and never by suffixing the reason.
        if mutation_class != "segment_id_remap" and not frozen_seat_write_allowed(
            ctx, SDP_REL, reason=enda_reason
        ):
            return ctx.final_path(*SDP_REL.split("/"))
    except ImportError:
        pass

    nested = admitting(ctx)
    if not nested:
        begin_admit(ctx)
    try:
        incoming = dict(doc) if isinstance(doc, dict) else {}
        base = dict(incoming)
        # Harden only when the payload is clearly schema-incomplete (raw escape
        # still refuses). Valid thin/custom plans must stay intact.
        required = ("coherence", "palettes", "assets", "flow_plans", "generated")
        needs_harden = any(k not in base for k in required) or int(base.get("version") or 0) != 1
        if mutation_class == "segment_id_remap":
            needs_harden = False
        if not needs_harden:
            schema_errors = validate_artifact_write(SDP_REL, base)
            needs_harden = bool(schema_errors)
        if needs_harden:
            seeded = default_sound_design_plan()
            for key, value in incoming.items():
                if key == "version":
                    continue
                if value is not None:
                    seeded[key] = value
            seeded["version"] = 1
            try:
                from interview_mux.artifact_repairs import repair_sound_design_plan

                hardened, _actions = repair_sound_design_plan(ctx, seeded)
                if isinstance(hardened, dict):
                    seeded = hardened
            except Exception:
                pass
            if not (isinstance(seeded.get("assets"), list) and seeded["assets"]):
                try:
                    from interview_mux.music_motif import (
                        build_music_brief,
                        ensure_motif_on_plan,
                    )

                    seeded = ensure_motif_on_plan(
                        seeded, build_music_brief(ctx), ctx=ctx
                    )
                except Exception:
                    pass
            base = seeded
        result = sanitize_sound_design_plan(ctx, dict(base))
        out = result.doc if result.ok and isinstance(result.doc, dict) else dict(base)
        meta = dict(out.get("_meta") or {}) if isinstance(out.get("_meta"), dict) else {}
        producer = str(
            (incoming.get("_meta") or {}).get("producer_stage")
            if isinstance(incoming.get("_meta"), dict)
            else ""
        ) or str(stage_key or "")
        if producer:
            meta["producer_stage"] = producer
            out["_meta"] = meta
        if result.ok:
            out = stamp_sanitize_meta(
                out,
                ok=True,
                source=reason or "commit_sound_design_plan",
                actions_n=len(result.actions or []),
            )
            if producer:
                meta2 = dict(out.get("_meta") or {})
                meta2["producer_stage"] = producer
                out["_meta"] = meta2

        from interview_mux.artifact_sanitize.admit import admit_sanitized
        from interview_mux.artifact_sanitize.types import SanitizeResult

        def _sdp_passthrough(_c: Any, d: dict[str, Any]) -> SanitizeResult:
            # Already sanitized above; admit records undo + sole write.
            return SanitizeResult(doc=d, ok=result.ok, errors=list(result.errors or []))

        admit_sanitized(
            ctx,
            SDP_REL,
            out,
            sanitize_fn=_sdp_passthrough,
            stage_key=stage_key or "sound_design_plan",
            skip_handoff=skip_handoff,
            write_committed=False,
            refuse_if_unsanitary=False,
            action_class="sound_design_plan",
            mutation_class=mutation_class,
        )
        return ctx.final_path(*SDP_REL.split("/"))
    finally:
        if not nested:
            end_admit(ctx)


def commit_nugget_layup_plan_doc(
    ctx: RunContext,
    doc: dict[str, Any],
    *,
    stage_key: str | None = None,
    skip_handoff: bool = False,
    reason: str = "",
    mutation_class: str | None = None,
) -> Path:
    """Sole layup persist: sanitize → stamp → one write."""
    from interview_mux.artifact_sanitize.admit import admit_sanitized
    from interview_mux.artifact_sanitize.nugget_layup_plan import (
        sanitize_nugget_layup_plan,
    )
    from interview_mux.artifact_sanitize.reentry import stamp_sanitize_meta
    from interview_mux.artifact_sanitize.types import SanitizeResult

    nested = admitting(ctx)
    if not nested:
        begin_admit(ctx)
    try:

        def _layup_sanitize(c: Any, d: dict[str, Any]) -> SanitizeResult:
            result = sanitize_nugget_layup_plan(c, dict(d))
            out = result.doc if result.ok and isinstance(result.doc, dict) else dict(d)
            if result.ok:
                out = stamp_sanitize_meta(
                    out,
                    ok=True,
                    source=reason or "commit_nugget_layup_plan",
                    actions_n=len(result.actions or []),
                    content_keys=["ordered_segment_ids", "layups", "status"],
                )
            return SanitizeResult(
                doc=out,
                ok=result.ok,
                errors=list(result.errors or []),
                actions=list(result.actions or []),
                metrics=dict(result.metrics or {}),
            )

        admit_sanitized(
            ctx,
            LAYUP_REL,
            doc,
            sanitize_fn=_layup_sanitize,
            stage_key=stage_key or "nugget_layup_compose",
            skip_handoff=True if skip_handoff else False,
            write_committed=False,
            refuse_if_unsanitary=False,
            content_keys=["ordered_segment_ids", "layups", "status"],
            action_class=reason or "commit_nugget_layup_plan",
            mutation_class=mutation_class,
        )
        return ctx.final_path(*LAYUP_REL.split("/"))
    finally:
        if not nested:
            end_admit(ctx)

__all__ = [
    "HOT_ARTIFACT_RELS",
    "SELECTION_REL",
    "GAP_REL",
    "EDL_REL",
    "TRANSITIONS_REL",
    "SDP_REL",
    "LAYUP_REL",
    "is_hot_artifact",
    "maybe_admit_hot_write",
    "admitting",
    "begin_admit",
    "end_admit",
    "commit_transitions_doc",
    "commit_sound_design_plan_doc",
    "commit_nugget_layup_plan_doc",
]

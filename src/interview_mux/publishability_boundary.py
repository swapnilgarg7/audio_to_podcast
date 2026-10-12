"""Publishability boundary — Tier 0/1 invariants at producer checkpoints."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Literal

from interview_mux.run_context import RunContext

CheckpointName = Literal[
    "post_cta_prune",
    "post_edl",
    "post_junction",
    "pre_mix",
    "pre_finalize",
]

PUBLISHABILITY_REPORT_REL = "operator/publishability_report.json"
REPAIR_PLAN_REL = "operator/publishability_repair_plan.json"

_MIN_SPEECH_MS = 400
_PENDING_WRITE_STAGES = frozenset({"edl", "junction_snip_qa", "mix", "master_finalize"})


#: Violation classes that are reported but never block (ISSUES 185). Each is a
#: judgement whose heal rebuilt the same artifact (junction residuals, claim
#: paperwork, hosted-VO remint, phantom VO), or stale staging that mix ignores.
#: Required VO, order drift, zeroed speech and orientation count stay blocking.
ADVISORY_ERROR_CLASSES: frozenset[str] = frozenset(
    {
        "quality_advisory",
        "incomplete_cut_unresolved",
        "junction_claim_inventory_stale",
        "vo_audibility_drift",
        "pending_write_barrier",
    }
)


@dataclass
class PublishabilityViolation:
    error_class: str
    code: str
    detail: str
    segment_id: str = ""
    line_id: str = ""


@dataclass
class PublishabilityReport:
    checkpoint: str
    ok: bool
    violations: list[PublishabilityViolation] = field(default_factory=list)
    selection_hash: str = ""
    edl_hash: str = ""
    producer_stage: str = ""


class PublishabilityBlocked(Exception):
    """Raised when a checkpoint cannot commit downstream work."""

    def __init__(
        self,
        report: PublishabilityReport,
        *,
        error_class: str,
        message: str = "",
    ) -> None:
        self.report = report
        self.error_class = error_class
        super().__init__(message or f"publishability blocked: {error_class}")


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def publishability_enforce(ctx: RunContext) -> bool:
    """Block on violation when homunculus or resilience.publishability_enforce."""
    try:
        from interview_mux.homunculus.runtime import has_homunculus_features

        if has_homunculus_features(ctx):
            return True
    except Exception:
        pass
    try:
        from interview_mux.config import merged_config

        root = merged_config()
        resilience = root.get("resilience") if isinstance(root, dict) else {}
        if isinstance(resilience, dict) and resilience.get("publishability_enforce"):
            return True
    except Exception:
        pass
    return False


def _selection_hash(ctx: RunContext) -> str:
    if not ctx.artifact_exists("master/selection.json"):
        return ""
    try:
        from interview_mux.order_hash import ordered_segment_ids_hash

        sel = ctx.read_json("master/selection.json")
        ids = [
            str(s)
            for s in ((sel or {}).get("ordered_segment_ids") or [])
            if s
        ]
        return ordered_segment_ids_hash(ids) if ids else ""
    except Exception:
        return ""


def _edl_hash(ctx: RunContext) -> str:
    if not ctx.artifact_exists("master/edl.json"):
        return ""
    try:
        edl = ctx.read_json("master/edl.json")
        if isinstance(edl, dict):
            return str(edl.get("order_content_hash") or "")
    except Exception:
        pass
    return ""


def _load_selection(ctx: RunContext) -> dict[str, Any] | None:
    if not ctx.artifact_exists("master/selection.json"):
        return None
    doc = ctx.read_json("master/selection.json")
    return dict(doc) if isinstance(doc, dict) else None


def _load_edl(ctx: RunContext) -> dict[str, Any] | None:
    if not ctx.artifact_exists("master/edl.json"):
        return None
    doc = ctx.read_json("master/edl.json")
    return dict(doc) if isinstance(doc, dict) else None


def _load_gap_report(ctx: RunContext) -> dict[str, Any] | None:
    if not ctx.artifact_exists("understanding/gap_report.json"):
        return None
    doc = ctx.read_json("understanding/gap_report.json")
    return dict(doc) if isinstance(doc, dict) else None


def _gap_report_aligned_with_edl(
    ctx: RunContext,
    gap_report: dict[str, Any] | None,
) -> dict[str, Any] | None:
    """Apply the same air_script VO filter ``run_edl`` uses before building clips."""
    if not isinstance(gap_report, dict):
        return gap_report
    try:
        from interview_mux.air_script import filter_gap_lines_for_air_script
        from interview_mux.mastering_plan_loader import load_plan_raw

        filtered = filter_gap_lines_for_air_script(gap_report, load_plan_raw(ctx))
        return filtered if isinstance(filtered, dict) else gap_report
    except Exception:
        return gap_report


def _check_zero_keeps(ctx: RunContext, edl: dict[str, Any] | None) -> list[PublishabilityViolation]:
    if not isinstance(edl, dict):
        return []
    out: list[PublishabilityViolation] = []
    for clip in edl.get("clips") or []:
        if not isinstance(clip, dict) or str(clip.get("type") or "") != "speech":
            continue
        sid = str(clip.get("segment_id") or "")
        try:
            dur = int(clip.get("duration_ms") or 0)
            ss = int(clip.get("source_start_ms") or 0)
            se = int(clip.get("source_end_ms") or ss)
        except (TypeError, ValueError):
            dur = 0
            ss = se = 0
        notes = clip.get("notes") or []
        note_blob = " ".join(str(n) for n in notes) if isinstance(notes, list) else str(notes)
        zeroed = (
            dur < _MIN_SPEECH_MS
            or se <= ss
            or "zeroed_inside_never_touch" in note_blob
        )
        if zeroed:
            out.append(
                PublishabilityViolation(
                    error_class="never_touch_zeroed_keep",
                    code="zero_duration_speech",
                    detail=f"speech clip {sid} duration_ms={dur}",
                    segment_id=sid,
                )
            )
    return out


def _check_phantom_vo(
    ctx: RunContext,
    edl: dict[str, Any] | None,
    gap_report: dict[str, Any] | None,
) -> list[PublishabilityViolation]:
    if not isinstance(edl, dict):
        return []
    edl_line_ids = {
        str(c.get("line_id") or "")
        for c in (edl.get("clips") or [])
        if isinstance(c, dict) and c.get("type") == "vo_pickup" and c.get("line_id")
    }
    # Lines the builder chose not to seat on purpose are not phantom: rebuilding
    # the EDL reaches the same decision every time (ISSUES 175).
    warn = edl.get("warnings") if isinstance(edl.get("warnings"), dict) else {}
    builder_skipped = {
        str(x)
        for key in ("suppressed_clone_adjacency", "gap_targets_not_in_selection")
        for x in (warn.get(key) or [])
        if x
    }
    out: list[PublishabilityViolation] = []
    for line in (gap_report or {}).get("interviewer_lines") or []:
        if not isinstance(line, dict):
            continue
        delivery = str(line.get("delivery") or "").lower()
        if delivery not in {"record", "synthesize"}:
            continue
        if line.get("skipped_optional") or line.get("air_script_omit"):
            continue
        lid = str(line.get("line_id") or "")
        if not lid or lid in edl_line_ids:
            continue
        if lid in builder_skipped or str(line.get("targets_segment_id") or "") in builder_skipped:
            continue
        try:
            from interview_mux.hosted_vo_authority import (
                ORIENTATION_LINE_ID,
                assert_books_agree,
                decide_orientation,
                identify_hosted_vo_floor,
            )

            if lid == ORIENTATION_LINE_ID and isinstance(gap_report, dict):
                ordered: list[str] = []
                if ctx.artifact_exists("master/selection.json"):
                    sel = ctx.read_json("master/selection.json")
                    if isinstance(sel, dict):
                        ordered = [
                            str(x)
                            for x in (sel.get("ordered_segment_ids") or [])
                            if x
                        ]
                decision = decide_orientation(ctx, gap_report, ordered)
                if decision.disposition in {"HEARD_KEEP", "HOLLOW_MINT"}:
                    ident = identify_hosted_vo_floor(ctx, persist=True)
                    books = assert_books_agree(ctx)
                    detail = (
                        f"orientation {decision.disposition}: remint via ensure/apply "
                        f"({ident.prose})"
                    )
                    if books:
                        detail += f" books={books[0][:120]}"
                    out.append(
                        PublishabilityViolation(
                            error_class="vo_audibility_drift",
                            code="hosted_vo_remint_needed",
                            detail=detail,
                            line_id=lid,
                            segment_id=str(line.get("targets_segment_id") or ""),
                        )
                    )
                    continue
        except Exception:
            pass
        try:
            from interview_mux.stages.assembly import resolve_vo_pickup_path

            wav = resolve_vo_pickup_path(ctx, line)
        except Exception:
            wav = None
        if wav is not None and wav.is_file():
            out.append(_phantom_vo_violation(ctx, line, lid))
            continue
        # Fallback: on-disk synth/clean WAV without G1-green resolve still counts
        # as phantom (publishability must see bytes, not only audit-bound paths).
        try:
            pickup = ctx.final_path("vo_pickup")
            for sub in ("matched", "synthesized", "clean", "normalized", ""):
                base = pickup / sub if sub else pickup
                for key in (lid, str(line.get("targets_segment_id") or "")):
                    if not key:
                        continue
                    candidate = base / f"{key}.wav"
                    if candidate.is_file():
                        out.append(_phantom_vo_violation(ctx, line, lid))
                        raise StopIteration
        except StopIteration:
            pass
        except Exception:
            pass
    return out


def _phantom_vo_violation(
    ctx: RunContext, line: dict[str, Any], lid: str
) -> PublishabilityViolation:
    """Tag survivor-class phantoms as edl_survivor_wipe so heal pins EDL rebuild."""
    detail = f"WAV exists without EDL vo_pickup for {lid}"
    code = "phantom_vo"
    try:
        from interview_mux.hosted_vo_authority import (
            edl_before_line_survives,
            identify_hosted_vo_floor,
        )

        if edl_before_line_survives(line, after_vo_stack=True):
            ident = identify_hosted_vo_floor(ctx, persist=False)
            origin = str(line.get("origin") or "")
            code = "edl_survivor_wipe"
            if ident.cause == "edl_survivor_wipe":
                detail = (
                    f"edl_survivor_wipe: {lid} WAV on disk but not seated "
                    f"(origin={origin}; {ident.prose}) — rebuild edl"
                )
            else:
                detail = (
                    f"edl_survivor_wipe: survivor-class {lid} WAV without EDL seat "
                    f"(origin={origin}) — rebuild edl, do not remint"
                )
    except Exception:
        pass
    return PublishabilityViolation(
        error_class="vo_audibility_drift",
        code=code,
        detail=detail,
        line_id=lid,
        segment_id=str(line.get("targets_segment_id") or ""),
    )


def _check_unseated_required_vo(
    ctx: RunContext,
    edl: dict[str, Any] | None,
    gap_report: dict[str, Any] | None,
) -> list[PublishabilityViolation]:
    """Required synthesize lines must appear on EDL or have omit-ledger compensation."""
    if not isinstance(gap_report, dict):
        return []
    edl_line_ids = {
        str(c.get("line_id") or "")
        for c in ((edl or {}).get("clips") or [])
        if isinstance(c, dict) and c.get("type") == "vo_pickup" and c.get("line_id")
    }
    out: list[PublishabilityViolation] = []
    try:
        from interview_mux.air_script import gap_line_air_eligible
        from interview_mux.omit_ledger import active_entries, line_is_omitted

        ledger = ctx.read_json("understanding/omit_ledger.json") if ctx.artifact_exists(
            "understanding/omit_ledger.json"
        ) else {}
    except Exception:
        gap_line_air_eligible = None  # type: ignore[assignment,misc]
        active_entries = None  # type: ignore[assignment,misc]
        line_is_omitted = None  # type: ignore[assignment,misc]
        ledger = {}
    compensated: set[str] = set()
    # A before-line whose target the EDL omitted as unplayable has no seat to
    # take: the segment is not in ordered speech, so the line is omitted with
    # it (ISSUES 190). The EDL records both lists; neither needs ledger
    # paperwork the edl stage may not write.
    omitted_targets = {
        str(s) for s in ((edl or {}).get("omitted_unplayable_segment_ids") or []) if s
    }
    omitted_lines = {
        str(s) for s in ((edl or {}).get("omitted_unplayable_vo_line_ids") or []) if s
    }
    if active_entries is not None and isinstance(ledger, dict):
        try:
            # Ledger rows key the line as ``subject_id`` (omit_ledger.mint_entry);
            # reading only ``line_id`` meant no active omit ever compensated
            # (ISSUES 184).
            for row in active_entries(ledger):
                if not isinstance(row, dict):
                    continue
                for key in ("subject_id", "line_id"):
                    if row.get(key):
                        compensated.add(str(row[key]))
        except Exception:
            pass
    for line in gap_report.get("interviewer_lines") or []:
        if not isinstance(line, dict):
            continue
        if gap_line_air_eligible is not None and not gap_line_air_eligible(line):
            continue
        if str(line.get("delivery") or "").lower() != "synthesize":
            continue
        if not line.get("required", True):
            continue
        lid = str(line.get("line_id") or "")
        if not lid or lid in edl_line_ids or lid in compensated:
            continue
        if lid in omitted_lines or (
            omitted_targets
            and str(line.get("targets_segment_id") or "") in omitted_targets
        ):
            continue
        # Pillar B: frozen omitted lines are intentional, not collateral strip
        try:
            from interview_mux.seat_authority import frozen_omitted_line_ids

            if lid in frozen_omitted_line_ids(ctx):
                continue
        except Exception:
            pass
        if line_is_omitted is not None:
            try:
                # Signature is (ledger, line_id); passing the line dict raised a
                # TypeError that the except swallowed (ISSUES 184).
                if line_is_omitted(ledger, lid):
                    continue
            except Exception:
                pass
        out.append(
            PublishabilityViolation(
                error_class="omit_collateral_vo_strip",
                code="unseated_required_vo",
                detail=f"required synthesize line not seated on EDL: {lid}",
                line_id=lid,
            )
        )
    return out


def _check_order_drift(
    selection: dict[str, Any] | None,
    edl: dict[str, Any] | None,
) -> list[PublishabilityViolation]:
    if not isinstance(selection, dict) or not isinstance(edl, dict):
        return []
    try:
        from interview_mux.order_hash import order_drift_heal_action

        action = order_drift_heal_action(selection, edl)
    except Exception:
        return []
    if action != "rebuild":
        return []
    return [
        PublishabilityViolation(
            error_class="selection_edl_order_drift",
            code="speech_order_drift",
            detail="speech clip order diverges from ordered_segment_ids",
        )
    ]


def _orientation_demand_applies(
    ctx: RunContext, gap_report: dict[str, Any] | None
) -> bool:
    """False when no stage may mint hosted VO and none is on the books.

    With gap framing disabled (gap fill skipped, native_only posture) the
    orientation line can never appear; demanding it pinned mix on
    ``opening_orientation_count=0 expected=1`` with no producer able to act
    (exec_049, ISSUES entry 51). A line that does exist is still validated.
    """
    try:
        from interview_mux.gap_vo_gates import gap_framing_enabled
        from interview_mux.opening_orientation import is_episode_orientation

        if gap_framing_enabled(ctx):
            return True
        lines = (gap_report or {}).get("interviewer_lines") if isinstance(gap_report, dict) else []
        return any(isinstance(x, dict) and is_episode_orientation(x) for x in (lines or []))
    except Exception:
        return True


def _check_opening_orientation(
    gap_report: dict[str, Any] | None,
    edl: dict[str, Any] | None,
) -> list[PublishabilityViolation]:
    try:
        from interview_mux.opening_orientation import validate_opening_orientation

        errors = validate_opening_orientation(gap_report=gap_report, edl=edl)
    except Exception:
        return []
    if not errors:
        return []
    audible = any("opening_orientation_audible_count" in e for e in errors)
    error_class = (
        "opening_orientation_inaudible"
        if audible
        else "opening_orientation_inaudible"
    )
    return [
        PublishabilityViolation(
            error_class=error_class,
            code="opening_orientation_contract",
            detail="; ".join(errors[:4]),
        )
    ]


def _check_pending_writes(ctx: RunContext) -> list[PublishabilityViolation]:
    try:
        from interview_mux.v2.config import DELIVERY_ORDER
        from interview_mux.write_staging import active_stage_id, stages_with_pending_writes

        # Own mid-stage staging is expected (mix runs pre_mix while writing).
        active = None
        try:
            active = active_stage_id()
        except Exception:
            active = None
        order = list(DELIVERY_ORDER)
        active_i = order.index(active) if active in order else None
        pending: list[str] = []
        for s in stages_with_pending_writes(ctx):
            if s not in _PENDING_WRITE_STAGES or s == active:
                continue
            # Abandoned staging from a later failed stage (e.g. junction) must not
            # block an earlier remaster (forensics exec_11130 pre_mix barrier).
            if active_i is not None and s in order and order.index(s) > active_i:
                continue
            pending.append(s)
    except Exception:
        return []
    if not pending:
        return []
    return [
        PublishabilityViolation(
            error_class="pending_write_barrier",
            code="staging_ghost",
            detail="pending writes: " + ", ".join(sorted(pending)),
        )
    ]


# Commitment diverge is a remaster signal for mix — never an incomplete-cut.
# Mapping it to incomplete_cut_unresolved deadlocks pre_mix (exec_13167:
# publishability blocked … incomplete_cut_unresolved — assembly_not_rendered…).
_REMASTER_ONLY_SEAM_REASONS = frozenset(
    {
        "assembly_not_rendered_from_current_edl",
    }
)

# Stale junction "applied" stamps vs EDL — paperwork, not a live hanging cut.
# Mapping to incomplete_cut_unresolved deadlocks mix↔junction under speech-first
# remaster (exec_023: claimed_repairs_missing_from_edl + speech_first_remaster_pending).
_CLAIM_INVENTORY_SEAM_REASONS = frozenset(
    {
        "claimed_repairs_missing_from_edl",
    }
)


def _seam_autopsy_blocking_reasons(ctx: RunContext) -> list[str]:
    """HX-3 1B: live ``master/seam_autopsy.json`` first, ``mastering/`` fallback."""
    from interview_mux.seam_autopsy import AUTOPSY_REL

    for rel in (AUTOPSY_REL, "mastering/seam_autopsy.json"):
        if not ctx.artifact_exists(rel):
            continue
        try:
            autopsy = ctx.read_json(rel)
        except Exception:
            continue
        if not isinstance(autopsy, dict):
            continue
        return [str(r) for r in (autopsy.get("blocking_reasons") or []) if str(r).strip()]
    return []


def _incomplete_cut_seam_reasons(ctx: RunContext) -> list[str]:
    """Seam blockers that are true incomplete-cut / residual — not remaster/paperwork."""
    return [
        r
        for r in _seam_autopsy_blocking_reasons(ctx)
        if r not in _REMASTER_ONLY_SEAM_REASONS and r not in _CLAIM_INVENTORY_SEAM_REASONS
    ]


def _claim_inventory_stale_reasons(ctx: RunContext) -> list[str]:
    """Paperwork-only claim/EDL diverge when live incomplete-cut detect is clean."""
    reasons = [
        r
        for r in _seam_autopsy_blocking_reasons(ctx)
        if r in _CLAIM_INVENTORY_SEAM_REASONS
    ]
    if not reasons:
        return []
    try:
        from interview_mux.junction_snip_qa import live_incomplete_cut_critical_findings

        if live_incomplete_cut_critical_findings(ctx):
            return []
    except Exception:
        return []
    return reasons


def _check_critical_junction(ctx: RunContext) -> list[PublishabilityViolation]:
    # Junction remaster calls run_mix → pre_mix. Non-incomplete residuals that
    # junction is actively repairing must not self-deadlock. Live hanging
    # mid-thought clips still block (F5 1A/3A).
    try:
        from interview_mux.write_staging import active_stage_id
        from interview_mux.junction_snip_qa import live_incomplete_cut_critical_findings

        if active_stage_id() == "junction_snip_qa" and not live_incomplete_cut_critical_findings(
            ctx
        ):
            return []
        # The ladder's own remaster rounds render *while* it repairs — refusing
        # here aborts the recut owner before it can rescan (exec_11871 spin).
        # junction_snip_qa still blocks at its terminal residual gate, and the
        # real mix stage keeps this refusal.
        if getattr(ctx, "_junction_snip_qa_inner", False):
            return []
        # Mix remaster is the owner of assembly_not_rendered diverge — do not
        # refuse pre_mix solely for that remaster-only reason. Claim-inventory
        # stale still falls through (reconcile / junction_claim_inventory_stale).
        if active_stage_id() == "mix" and not live_incomplete_cut_critical_findings(ctx):
            if not _incomplete_cut_seam_reasons(ctx) and not _claim_inventory_stale_reasons(
                ctx
            ):
                return []
    except Exception:
        pass
    # Stale pending/committed incomplete-cut stamps must not block mix when the
    # live EDL no longer has critical on_a_roll / incomplete_clause findings.
    try:
        from interview_mux.junction_snip_qa import clear_stale_incomplete_cut_residuals

        clear_stale_incomplete_cut_residuals(ctx)
    except Exception:
        pass
    out: list[PublishabilityViolation] = []
    try:
        from interview_mux.delivery_guardrails import critical_residual_view, has_critical_residuals

        if has_critical_residuals(ctx):
            view = critical_residual_view(ctx)
            blocking: list[Any] = []
            qa_rel = "master/junction_snip_qa.json"
            if ctx.artifact_exists(qa_rel):
                try:
                    report = ctx.read_json(qa_rel)
                    if isinstance(report, dict):
                        blocking = list(report.get("blocking_reasons") or [])
                except Exception:
                    blocking = []
            out.append(
                PublishabilityViolation(
                    error_class="incomplete_cut_unresolved",
                    code="critical_junction_residual",
                    detail=(
                        f"critical_residuals={view.count} kinds={list(view.kinds)[:3]} "
                        f"sources={list(view.sources)} blocking={blocking[:3]}"
                    ),
                )
            )
    except Exception:
        # Fall back to legacy stamped int when SSOT import/path fails.
        qa_rel = "master/junction_snip_qa.json"
        if ctx.artifact_exists(qa_rel):
            try:
                report = ctx.read_json(qa_rel)
                if isinstance(report, dict):
                    critical = int(report.get("critical_residuals") or 0)
                    blocking = list(report.get("blocking_reasons") or [])
                    if critical > 0 or any("critical" in str(b).lower() for b in blocking):
                        out.append(
                            PublishabilityViolation(
                                error_class="incomplete_cut_unresolved",
                                code="critical_junction_residual",
                                detail=f"critical_residuals={critical} blocking={blocking[:3]}",
                            )
                        )
            except Exception:
                pass
    if not out:
        reasons = _incomplete_cut_seam_reasons(ctx)
        if reasons:
            out.append(
                PublishabilityViolation(
                    error_class="incomplete_cut_unresolved",
                    code="seam_autopsy_blocking",
                    detail="; ".join(reasons[:4]),
                )
            )
            return out
        claim_reasons = _claim_inventory_stale_reasons(ctx)
        if claim_reasons:
            # Best-effort paperwork reconcile before blocking mix (no ladder/remaster).
            reconciled = False
            try:
                from interview_mux.junction_snip_qa import reconcile_junction_claim_inventory

                reconciled = bool(reconcile_junction_claim_inventory(ctx))
            except Exception:
                reconciled = False
            if reconciled:
                claim_reasons = []
            else:
                claim_reasons = _claim_inventory_stale_reasons(ctx)
            if claim_reasons:
                out.append(
                    PublishabilityViolation(
                        error_class="junction_claim_inventory_stale",
                        code="seam_autopsy_claim_inventory",
                        detail="; ".join(claim_reasons[:4]),
                    )
                )
    return out


def _check_pmq_envelope(ctx: RunContext) -> list[PublishabilityViolation]:
    rel = "master/post_master_quality.json"
    if not ctx.artifact_exists(rel):
        return [
            PublishabilityViolation(
                error_class="post_master_quality_missing",
                code="pmq_missing",
                detail="post_master_quality.json missing at finalize",
            )
        ]
    try:
        doc = ctx.read_json(rel)
    except Exception:
        return [
            PublishabilityViolation(
                error_class="post_master_quality_missing",
                code="pmq_unreadable",
                detail="post_master_quality.json unreadable",
            )
        ]
    if not isinstance(doc, dict):
        return [
            PublishabilityViolation(
                error_class="post_master_quality_missing",
                code="pmq_invalid",
                detail="post_master_quality.json not a dict",
            )
        ]
    if not doc.get("publish_allowed"):
        failed = list(doc.get("failed_checks") or [])
        detail = "publish_allowed=false"
        if failed:
            detail += " failed=" + ",".join(str(x) for x in failed[:6])
        return [
            PublishabilityViolation(
                error_class="pmq_incomplete_ship_walk",
                code="pmq_not_publishable",
                detail=detail,
            )
        ]
    from interview_mux.quality_status import STATUS_ADVISORY_FAIL, is_advisory_status

    if is_advisory_status(doc.get("status")):
        from interview_mux.aspirational_quality import is_aspirational_enabled

        if is_aspirational_enabled(ctx):
            failed = list(doc.get("rubric_failed_checks") or doc.get("failed_checks") or [])
            if failed:
                return [
                    PublishabilityViolation(
                        error_class="quality_advisory",
                        code="pmq_advisory",
                        detail=f"{STATUS_ADVISORY_FAIL}: "
                        + ",".join(str(x) for x in failed[:6]),
                    )
                ]
    return []


_CHECKPOINT_CHECKS: dict[str, tuple[str, ...]] = {
    "post_cta_prune": ("zero_keeps",),
    "post_edl": (
        "zero_keeps",
        "phantom_vo",
        "order_drift",
        "opening_orientation",
    ),
    "post_junction": (
        "zero_keeps",
        "order_drift",
        "critical_junction",
    ),
    "pre_mix": (
        "zero_keeps",
        "phantom_vo",
        "unseated_required_vo",
        "order_drift",
        "opening_orientation",
        "pending_writes",
        "critical_junction",
    ),
    "pre_finalize": ("pmq_envelope",),
}


def validate_publishability(
    ctx: RunContext,
    *,
    checkpoint: str,
) -> PublishabilityReport:
    """Run checkpoint-scoped publishability checks."""
    checks = _CHECKPOINT_CHECKS.get(str(checkpoint or ""), ())
    selection = _load_selection(ctx)
    edl = _load_edl(ctx)
    gap_report = _load_gap_report(ctx)
    edl_gap_report = _gap_report_aligned_with_edl(ctx, gap_report)
    violations: list[PublishabilityViolation] = []

    for name in checks:
        if name == "zero_keeps":
            violations.extend(_check_zero_keeps(ctx, edl))
        elif name == "phantom_vo":
            violations.extend(_check_phantom_vo(ctx, edl, edl_gap_report))
        elif name == "unseated_required_vo":
            violations.extend(_check_unseated_required_vo(ctx, edl, edl_gap_report))
        elif name == "order_drift":
            violations.extend(_check_order_drift(selection, edl))
        elif name == "opening_orientation":
            if _orientation_demand_applies(ctx, gap_report):
                violations.extend(_check_opening_orientation(gap_report, edl))
        elif name == "pending_writes":
            violations.extend(_check_pending_writes(ctx))
        elif name == "critical_junction":
            violations.extend(_check_critical_junction(ctx))
        elif name == "pmq_envelope":
            violations.extend(_check_pmq_envelope(ctx))

    producer = ""
    if violations:
        producer = violation_playbook(violations[0]).resume_stage

    return PublishabilityReport(
        checkpoint=str(checkpoint or ""),
        ok=not violations,
        violations=violations,
        selection_hash=_selection_hash(ctx),
        edl_hash=_edl_hash(ctx),
        producer_stage=producer,
    )


def violation_playbook(v: PublishabilityViolation):
    """Map a violation to registry playbook spec."""
    from interview_mux.heal_routing import PLAYBOOK_REGISTRY, PlaybookSpec

    spec = PLAYBOOK_REGISTRY.get(v.error_class)
    if spec is not None:
        return spec
    return PlaybookSpec(resume_stage="edl", action="rebuild_edl")


def write_publishability_report(ctx: RunContext, report: PublishabilityReport) -> None:
    primary = report.violations[0] if report.violations else None
    playbook = violation_playbook(primary) if primary else None
    doc: dict[str, Any] = {
        "version": 1,
        "updated_at": _utc_now(),
        "checkpoint": report.checkpoint,
        "ok": report.ok,
        "selection_hash": report.selection_hash,
        "edl_hash": report.edl_hash,
        "producer_stage": report.producer_stage,
        "violations": [
            {
                "error_class": v.error_class,
                "code": v.code,
                "detail": v.detail,
                "segment_id": v.segment_id,
                "line_id": v.line_id,
            }
            for v in report.violations
        ],
    }
    if playbook is not None and primary is not None:
        doc["playbook"] = {
            "error_class": primary.error_class,
            "resume_stage": playbook.resume_stage,
            "action": playbook.action,
        }
    ctx.write_json(PUBLISHABILITY_REPORT_REL, doc, skip_handoff=True)


def _snapshot_edl_ledger(ctx: RunContext) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    edl = _load_edl(ctx)
    ledger: dict[str, Any] | None = None
    if ctx.artifact_exists("master/assembly_ledger.json"):
        try:
            doc = ctx.read_json("master/assembly_ledger.json")
            ledger = dict(doc) if isinstance(doc, dict) else None
        except Exception:
            ledger = None
    return edl, ledger


def _reemit_edl_ledger(
    ctx: RunContext,
    edl: dict[str, Any] | None,
    ledger: dict[str, Any] | None,
) -> None:
    """Restore just-committed EDL/ledger after a hard invalidate that archived them."""
    from interview_mux.artifact_sanitize.one_writer import (
        admitting,
        begin_admit,
        end_admit,
    )
    from interview_mux.file_store import write_json as fs_write_json

    # Restore exact bytes under admit — do not re-enter write_live_edl (gen bump).
    nested = admitting(ctx)
    if not nested:
        begin_admit(ctx)
    try:
        if isinstance(edl, dict) and edl:
            dest = ctx.final_path("master", "edl.json")
            dest.parent.mkdir(parents=True, exist_ok=True)
            fs_write_json(dest, edl)
        if isinstance(ledger, dict) and ledger:
            dest = ctx.final_path("master", "assembly_ledger.json")
            dest.parent.mkdir(parents=True, exist_ok=True)
            fs_write_json(dest, ledger)
        elif isinstance(edl, dict) and edl:
            try:
                from interview_mux.assembly_ledger import write_assembly_ledger

                write_assembly_ledger(ctx, edl=edl)
            except Exception:
                pass
    finally:
        if not nested:
            end_admit(ctx)


def write_publishability_repair_plan(
    ctx: RunContext,
    report: PublishabilityReport,
    *,
    playbook,
    soft: bool = False,
) -> dict[str, Any]:
    """Persist cascade repair plan with invalidate_set for downstream failures.

    Soft/advisory: write the plan only — never ``clear_from(edl)`` / archive live EDL.
    Hard: may invalidate, but re-emits EDL+ledger in the same call so finalize keeps a ledger.
    """
    from interview_mux.artifact_dependency_graph import transitive_invalidate

    primary = report.violations[0]
    resume_stage = playbook.resume_stage
    invalidate_set = transitive_invalidate(resume_stage)
    plan_id_src = "|".join(
        [
            report.checkpoint,
            primary.error_class,
            report.selection_hash,
            report.edl_hash,
        ]
    )
    repair_plan_id = hashlib.sha256(plan_id_src.encode("utf-8")).hexdigest()[:16]
    plan: dict[str, Any] = {
        "version": 1,
        "repair_plan_id": repair_plan_id,
        "error_class": primary.error_class,
        "from_stage": resume_stage,
        "checkpoint": report.checkpoint,
        "invalidate_set": invalidate_set,
        "cascade_error_classes": sorted({v.error_class for v in report.violations}),
        "completed": False,
        "soft": bool(soft),
        "created_at": _utc_now(),
    }
    ctx.write_json(REPAIR_PLAN_REL, plan, skip_handoff=True)
    if soft:
        # Soft/advisory: plan only. Never clear/archive live EDL — even if
        # resume_stage is edl. Downstream-of-mix clear only for mix/junction/finalize.
        if resume_stage == "edl" or "edl" in {str(s) for s in invalidate_set}:
            plan["cleared_from"] = None
            plan["soft_clear_mode"] = "plan_only_refuse_edl_clear"
            plan["protected_artifacts"] = ["master/edl.json", "master/assembly_ledger.json"]
            ctx.write_json(REPAIR_PLAN_REL, plan, skip_handoff=True)
            return plan
        if resume_stage in {"mix", "junction_snip_qa", "master_finalize"}:
            try:
                # C13: soft mix invalidate after assembly needs reopen allow.
                allow_mix = True
                try:
                    asm = ctx.final_path("master", "assembly.wav")
                    if asm.is_file() and asm.stat().st_size > 0:
                        from interview_mux.timeline_reopen_meta_gate import (
                            INTENT_PUB_SOFT_MIX,
                            decide_timeline_reopen,
                        )

                        gate = decide_timeline_reopen(
                            ctx,
                            intent=INTENT_PUB_SOFT_MIX,
                            failed_dims=[primary.error_class],
                            detail={"from_stage": resume_stage},
                        )
                        allow_mix = bool(gate.get("allow"))
                        plan["gate"] = gate
                except Exception:
                    allow_mix = False  # c13 fail-closed
                if allow_mix:
                    from interview_mux.homunculus.agenda import invalidate_downstream

                    invalidate_downstream(ctx, "mix")
                    plan["cleared_from"] = "mix"
                    plan["soft_clear_mode"] = "downstream_of_mix"
                else:
                    plan["cleared_from"] = None
                    plan["soft_clear_mode"] = "refused_low_gain"
            except Exception:
                pass
        else:
            plan["cleared_from"] = None
            plan["soft_clear_mode"] = "plan_only"
        ctx.write_json(REPAIR_PLAN_REL, plan, skip_handoff=True)
        return plan

    edl_snap, ledger_snap = _snapshot_edl_ledger(ctx)
    reemit_needed = resume_stage in {"edl", "mix", "junction_snip_qa"} or report.checkpoint in {
        "post_edl",
        "post_junction",
        "pre_mix",
    }
    # C9: hard repair that invalidates edl/mix after assembly needs reopen allow.
    try:
        asm = ctx.final_path("master", "assembly.wav")
        if asm.is_file() and asm.stat().st_size > 0:
            from interview_mux.timeline_reopen_meta_gate import (
                INTENT_PUB_HARD,
                decide_timeline_reopen,
            )

            gate = decide_timeline_reopen(
                ctx,
                intent=INTENT_PUB_HARD,
                failed_dims=[primary.error_class],
                detail={
                    "from_stage": resume_stage,
                    "checkpoint": report.checkpoint,
                    "proposed_cost_stages": list(invalidate_set)[:12],
                },
            )
            if not gate.get("allow"):
                plan["cleared_from"] = None
                plan["hard_clear_mode"] = "refused_low_gain"
                plan["gate"] = gate
                ctx.write_json(REPAIR_PLAN_REL, plan, skip_handoff=True)
                return plan
    except Exception:
        # c9 fail-closed: refuse hard invalidate when gate errors after assembly
        plan["cleared_from"] = None
        plan["hard_clear_mode"] = "refused_fail_closed"
        plan["gate"] = {"allow": False, "refuse_reason": "decide_error_fail_closed"}
        ctx.write_json(REPAIR_PLAN_REL, plan, skip_handoff=True)
        return plan
    # B-06: hard repair via profiles only — never combined clear_from fallback.
    try:
        from interview_mux.execution_invalidation_profiles import (
            apply_bounded_invalidation,
            resolve_invalidation_profile,
        )

        resolved = resolve_invalidation_profile(ctx, primary.error_class)
        profile_id = resolved.profile_id if resolved is not None else "structural_delivery"
        apply_bounded_invalidation(
            ctx,
            profile_id,
            reason=f"publishability:{primary.error_class}",
        )
        plan["cleared_from"] = resume_stage
        plan["invalidation_profile"] = profile_id
    except Exception as inv_exc:
        plan["cleared_from"] = None
        plan["invalidation_error"] = str(inv_exc)[:240]
    # Hard invalidate must not leave finalize without the just-committed EDL/ledger.
    # T6: always reemit from snapshot even if invalidate raised mid-way.
    if reemit_needed:
        try:
            _reemit_edl_ledger(ctx, edl_snap, ledger_snap)
            plan["edl_ledger_reemitted"] = True
        except Exception as reemit_exc:
            plan["edl_ledger_reemit_error"] = str(reemit_exc)[:240]
            # Best-effort restore snapshot bytes even after partial failure.
            try:
                _reemit_edl_ledger(ctx, edl_snap, ledger_snap)
                plan["edl_ledger_reemitted"] = True
            except Exception:
                pass
    ctx.write_json(REPAIR_PLAN_REL, plan, skip_handoff=True)
    return plan


def commit_or_block(
    ctx: RunContext,
    report: PublishabilityReport,
    *,
    enforce: bool | None = None,
) -> None:
    """Raise PublishabilityBlocked when violations exist and enforcement is on."""
    write_publishability_report(ctx, report)
    if report.ok:
        return
    blocking = [v for v in report.violations if v.error_class not in ADVISORY_ERROR_CLASSES]
    primary = blocking[0] if blocking else report.violations[0]
    playbook = violation_playbook(primary)
    should_block = publishability_enforce(ctx) if enforce is None else bool(enforce)
    advisory_only = not blocking
    soft = advisory_only or not should_block
    write_publishability_repair_plan(ctx, report, playbook=playbook, soft=soft)
    if advisory_only:
        try:
            ctx.log(
                f"publishability {report.checkpoint}: advisory only "
                f"({', '.join(sorted({v.error_class for v in report.violations}))}) — not blocking",
                level="warning",
                stage=playbook.resume_stage,
            )
        except Exception:
            pass
        return
    if not should_block:
        try:
            ctx.log(
                f"publishability {report.checkpoint}: {len(report.violations)} violation(s) "
                f"(fail-open error_class={primary.error_class})",
                level="warning",
                stage=playbook.resume_stage,
            )
        except Exception:
            pass
        return
    raise PublishabilityBlocked(
        report,
        error_class=primary.error_class,
        message=(
            f"publishability blocked at {report.checkpoint}: "
            f"{primary.error_class} — {primary.detail}"
        ),
    )


def checkpoint_publishability(
    ctx: RunContext,
    *,
    checkpoint: str,
    enforce: bool | None = None,
) -> PublishabilityReport:
    """Validate, persist report, and optionally block."""
    if checkpoint == "pre_mix":
        try:
            from interview_mux.seed_policy import seal_freeze_sticky_stages

            seal_freeze_sticky_stages(ctx)
        except Exception:
            pass
    report = validate_publishability(ctx, checkpoint=checkpoint)
    commit_or_block(ctx, report, enforce=enforce)
    return report


def read_active_repair_plan(ctx: RunContext) -> dict[str, Any] | None:
    if not ctx.artifact_exists(REPAIR_PLAN_REL):
        return None
    try:
        doc = ctx.read_json(REPAIR_PLAN_REL)
    except Exception:
        return None
    return doc if isinstance(doc, dict) and not doc.get("completed") else None


def failure_in_active_repair_cascade(
    ctx: RunContext,
    *,
    failed_stage: str,
    producer: str,
) -> bool:
    """True when failure is expected downstream of an active publishability repair plan."""
    plan = read_active_repair_plan(ctx)
    if not plan:
        return False
    root_class = str(plan.get("error_class") or "")
    if producer and producer == root_class:
        return True
    cascade = {str(c) for c in (plan.get("cascade_error_classes") or [])}
    if producer in cascade:
        return True
    invalidate_set = {str(s) for s in (plan.get("invalidate_set") or [])}
    stage = str(failed_stage or "").strip()
    if stage and stage in invalidate_set:
        return True
    return False


def mark_repair_plan_completed(ctx: RunContext) -> None:
    plan = read_active_repair_plan(ctx)
    if not plan:
        return
    plan = dict(plan)
    plan["completed"] = True
    plan["completed_at"] = _utc_now()
    ctx.write_json(REPAIR_PLAN_REL, plan, skip_handoff=True)

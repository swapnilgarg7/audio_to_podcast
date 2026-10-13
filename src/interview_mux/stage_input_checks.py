"""Deterministic stage-input validation before pipeline stages run."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from interview_mux.gates import (
    check_g1_vo,
    check_transcript_review_pending,
    get_selected_flow,
)
from interview_mux.llm_preflight import run_preflight
from interview_mux.run_context import RunContext
from interview_mux.write_staging import (
    has_pending_writes,
    stages_with_pending_writes,
    staging_approval_hint,
)


@dataclass(frozen=True)
class StageInputIssue:
    message: str
    remediation: str | None = None
    kind: str = "prerequisite"
    related_stage: str | None = None


class StageInputError(RuntimeError):
    """Raised when a stage cannot run because prerequisites are missing or blocked."""

    def __init__(self, stage_id: str, issues: list[StageInputIssue]) -> None:
        self.stage_id = stage_id
        self.issues = issues
        lines = [issue.message for issue in issues]
        fixes = [issue.remediation for issue in issues if issue.remediation]
        msg = f"Stage '{stage_id}' blocked — " + "; ".join(lines)
        if fixes:
            msg += "\nRemediation: " + " | ".join(fixes)
        super().__init__(msg)

    @property
    def write_approval_only(self) -> bool:
        return bool(self.issues) and all(issue.kind == "write_approval" for issue in self.issues)

    @property
    def pending_write_stage(self) -> str | None:
        for issue in self.issues:
            if issue.kind == "write_approval":
                return issue.related_stage or self.stage_id
        return None


def collect_stage_input_issues(ctx: RunContext, stage_id: str) -> list[StageInputIssue]:
    """Return actionable issues for a stage (empty list = ready to run)."""
    issues: list[StageInputIssue] = []
    issues.extend(_pending_write_approval_issues(ctx, stage_id))
    # Sanitary preflight before VO-contract repair: repair_vo_contract_drift may
    # commit/sanitize gap and would otherwise hide dirty-gap blockers.
    try:
        from interview_mux.artifact_sanitize.preflight import sanitary_preflight_errors

        for err in sanitary_preflight_errors(ctx, stage_id):
            related = None
            low = str(err).lower()
            if "air_contract" in low:
                related = "air_contract_sanitize"
            elif "selection" in low:
                related = "selection_order_sanitize"
            elif "gap" in low:
                related = "gap_report_sanitize"
            issues.append(
                StageInputIssue(
                    err,
                    "Run the matching sanitize stage (selection/gap/air_contract) before continuing.",
                    kind="sanitize",
                    related_stage=related,
                )
            )
    except Exception:
        pass
    # Holistic seat review before transitions / adjudicate / edl — pin sanitize once.
    if stage_id in {"transitions", "vo_line_adjudicate", "edl"}:
        try:
            from interview_mux.seat_authority import holistic_seat_review, soft_freeze_active

            if soft_freeze_active(ctx) or stage_id in {"transitions", "edl"}:
                report = holistic_seat_review(ctx)
                if isinstance(report, dict) and report.get("ok") is False:
                    fails = report.get("failures") or []
                    msg = (
                        "Holistic seat review failed: "
                        + "; ".join(str(f) for f in fails[:4])
                    )
                    issues.append(
                        StageInputIssue(
                            msg,
                            "Run air_contract_sanitize once to heal seats (not Pass B remutate).",
                            kind="seat_review",
                            related_stage="air_contract_sanitize",
                        )
                    )
        except Exception:
            pass
    issues.extend(_vo_contract_issues(ctx, stage_id))
    checker = _STAGE_CHECKERS.get(stage_id)
    if checker is not None:
        issues.extend(checker(ctx))
    elif stage_id in _LLM_STAGES:
        for err in run_preflight(stage_id, ctx):
            # Avoid duplicate sanitary lines already added above.
            if any(err == i.message for i in issues):
                continue
            issues.append(StageInputIssue(err, _llm_remediation(err, ctx)))
    return issues


_SANITIZE_REMEDY_STAGES = frozenset(
    {"selection_order_sanitize", "gap_report_sanitize", "air_contract_sanitize"}
)


def _run_sanitize_remedy(
    ctx: RunContext,
    stage_id: str,
    issues: list[StageInputIssue],
) -> bool:
    """Run the sanitize stage a sanitize issue names, once, ahead of the blocked stage.

    A sanitize issue carries its own remedy (``related_stage``). When that
    stage sits *after* the blocked stage in the order, nothing dispatches it:
    exec_041 (2026-10-13) had junction's recovery unmark full_master_ranking,
    whose input check then refused the unsanitary selection junction left
    behind (drop_orphan_ref, bump_order_lock, incomplete_seam_pull_completion);
    selection_order_sanitize is the next stage, so the seed walk re-dispatched
    ranking to the identical-failure cap and the run stopped with the remedy
    printed in every error line (ISSUES 194). The remedy is deterministic and
    cheap; run it here and let the caller re-collect.
    """
    remedies: list[str] = []
    for issue in issues:
        if issue.kind != "sanitize":
            continue
        remedy = str(issue.related_stage or "").strip()
        if remedy in _SANITIZE_REMEDY_STAGES and remedy != stage_id and remedy not in remedies:
            remedies.append(remedy)
    if not remedies:
        return False
    ran = getattr(ctx, "_sanitize_remedy_ran", None)
    if not isinstance(ran, set):
        ran = set()
        setattr(ctx, "_sanitize_remedy_ran", ran)
    did = False
    for remedy in remedies:
        key = f"{stage_id}<-{remedy}"
        if key in ran:
            continue
        ran.add(key)
        why = next(
            (i.message for i in issues if i.kind == "sanitize" and i.related_stage == remedy),
            "",
        )
        try:
            ctx.log(
                f"Stage input check: {stage_id} blocked on {why[:140]}; running "
                f"{remedy} first",
                level="warning",
                stage=stage_id,
                detail={"event": "sanitize_remedy_ahead", "remedy": remedy, "issue": why[:300]},
            )
        except Exception:
            pass
        try:
            if ctx.is_done(remedy):
                from interview_mux.homunculus.agenda import unmark_stage_only

                unmark_stage_only(ctx, remedy)
            from interview_mux.pipeline import run_single_stage

            run_single_stage(ctx, remedy)
            did = True
        except Exception as exc:  # noqa: BLE001 - the caller re-collects and refuses in its own words
            try:
                ctx.log(
                    f"{remedy} ahead of {stage_id} did not land: "
                    f"{type(exc).__name__}: {str(exc)[:200]}",
                    level="warning",
                    stage=stage_id,
                )
            except Exception:
                pass
    return did


def require_stage_inputs(ctx: RunContext, stage_id: str) -> None:
    """Raise StageInputError when prerequisites are not satisfied."""
    issues = collect_stage_input_issues(ctx, stage_id)
    if not issues:
        return
    if _run_sanitize_remedy(ctx, stage_id, issues):
        issues = collect_stage_input_issues(ctx, stage_id)
        if not issues:
            return
    if _try_preflight_recovery(ctx, stage_id, issues):
        issues = collect_stage_input_issues(ctx, stage_id)
        if not issues:
            return
    from interview_mux.operator_trace import log_step

    # Operator/gate pauses — not pipeline crashes. Keep gui_log at warning.
    log_step(
        f"Stage input check blocked: {stage_id}",
        ctx=ctx,
        stage=stage_id,
        level="warning",
        detail={
            "event": "stage_input_blocked",
            "issues": [issue.message for issue in issues],
            "remediation": [issue.remediation for issue in issues if issue.remediation],
            "kinds": [issue.kind for issue in issues],
        },
    )
    raise StageInputError(stage_id, issues)


def _try_preflight_recovery(
    ctx: RunContext,
    stage_id: str,
    issues: list[StageInputIssue],
) -> bool:
    """H0c: homunculus 0.1.0 recovery before surfacing delivery StageInputError."""
    try:
        from interview_mux.homunculus.runtime import has_homunculus_features
        from interview_mux.recovery_controller import classify_error_class
        from interview_mux.remediation_framework import run_classified_ladder

        if not has_homunculus_features(ctx):
            return False
        recoverable = any(
            issue.kind in {"vo_contract", "upstream_stale"}
            or "vo coverage" in issue.message.lower()
            or "vo contract" in issue.message.lower()
            or "stale upstream" in issue.message.lower()
            for issue in issues
        )
        if not recoverable:
            return False
        exc = RuntimeError(issues[0].message)
        error_class = classify_error_class(stage_id, exc)
        if issue_vo := next((i for i in issues if i.kind == "vo_contract"), None):
            error_class = error_class or "vo_contract_repair"
            exc = RuntimeError(issue_vo.message)
        if any("vo coverage" in i.message.lower() for i in issues):
            error_class = error_class or "vo_seated_coverage"
            exc = RuntimeError(next(i.message for i in issues if "vo coverage" in i.message.lower()))
        if not error_class:
            return False
        outcome = run_classified_ladder(
            ctx,
            consumer_stage=stage_id,
            exc=exc,
            error_class=error_class,
        )
        return outcome.recovered
    except Exception:
        return False


_VO_CONTRACT_STAGES = frozenset(
    {"nugget_layup_compose", "air_script_seams", "vo_line_adjudicate", "vo_synthesize"}
)


def _hosted_floor_due(ctx: RunContext, stage_id: str) -> bool:
    """False in layup's own preflight before it has written a plan.

    The floor judged the analysis-era body there, recorded identical failures
    against a layup that had never run (exec_016: x3/3 halt=True before the
    first layup call) and reseated lines layup then replaced (ISSUES 161).
    Layup re-checks the floor after it publishes.
    """
    if stage_id != "nugget_layup_compose":
        return True
    try:
        from interview_mux.nugget_layup import PLAN_REL

        return bool(ctx.artifact_exists(PLAN_REL))
    except Exception:
        return True


def _vo_contract_issues(ctx: RunContext, stage_id: str) -> list[StageInputIssue]:
    if stage_id not in _VO_CONTRACT_STAGES:
        return []
    from interview_mux.execution_invariants import run_consumer_invariants
    from interview_mux.execution_contract import run_vo_contract_ladder
    from interview_mux.vo_contract import validate_vo_contract

    run_consumer_invariants(ctx, stage_id)
    # Align omit ledger before preflight — stale omitted∩on-air thrash blocks synth.
    try:
        from interview_mux.vo_contract import (
            ensure_hosted_framing_vo_seats,
            repair_vo_contract_drift,
        )

        repair_vo_contract_drift(ctx)
        if _hosted_floor_due(ctx, stage_id):
            ensure_hosted_framing_vo_seats(ctx)
    except Exception:
        pass
    violations = validate_vo_contract(ctx)
    # Pre-render + render stages: missing WAV is expected until vo_synthesize
    # finishes. Blocking nugget_layup_compose on absent WAVs unmarks layup, then
    # premature-G1 resume picks layup as seed front → endless layup↔synth thrash.
    if stage_id in {
        "vo_synthesize",
        "vo_line_adjudicate",
        "nugget_layup_compose",
        "air_script_seams",
    }:
        violations = [v for v in violations if "missing WAV" not in v and "missing wav" not in v.lower()]
    if violations:
        result = run_vo_contract_ladder(ctx, consumer_stage=stage_id)
        try:
            repair_vo_contract_drift(ctx)
            if _hosted_floor_due(ctx, stage_id):
                ensure_hosted_framing_vo_seats(ctx)
        except Exception:
            pass
        violations = validate_vo_contract(ctx)
        if stage_id in {
            "vo_synthesize",
            "vo_line_adjudicate",
            "nugget_layup_compose",
            "air_script_seams",
        }:
            violations = [
                v for v in violations if "missing WAV" not in v and "missing wav" not in v.lower()
            ]
    book_errs: list[str] = []
    try:
        from interview_mux.hosted_vo_authority import assert_books_agree

        book_errs = assert_books_agree(ctx)
    except Exception:
        pass
    if not violations and not book_errs:
        return []
    msgs = violations + book_errs
    return [
        StageInputIssue(
            f"VO contract: {msgs[0]}",
            "Run vo_contract ladder or re-run layup/adjudicate before synthesis.",
            kind="vo_contract",
        )
    ]


def _pending_write_approval_issues(ctx: RunContext, stage_id: str) -> list[StageInputIssue]:
    """Block only when write approval must pause execute for this stage.

    Under first-try ``defer_write_approval_until=phase_end``, other stages may
    keep staged files without blocking subsequent stages. Re-running a stage
    that still has its own pending writes remains blocked.
    """
    from interview_mux.first_try import write_approval_deferred
    from interview_mux.write_staging import write_approval_enabled

    if not write_approval_enabled():
        return []

    if write_approval_deferred():
        if not has_pending_writes(ctx, stage_id):
            return []
        return [
            StageInputIssue(
                f"Write approval pending for stage '{stage_id}'",
                f"Open the write review modal for '{stage_id}' and choose Save & continue or Discard & re-run.",
                kind="write_approval",
                related_stage=stage_id,
            )
        ]

    from interview_mux.write_staging import stages_with_pending_writes

    pending = stages_with_pending_writes(ctx)
    if not pending:
        return []
    sid = pending[0]
    return [
        StageInputIssue(
            f"Write approval pending for stage '{sid}'",
            f"Open the write review modal for '{sid}' and choose Save & continue or Discard & re-run.",
            kind="write_approval",
            related_stage=sid,
        )
    ]


def _require_artifact(
    ctx: RunContext,
    rel: str,
    *,
    label: str | None = None,
    remediation: str | None = None,
    require_complete: bool = False,
) -> StageInputIssue | None:
    must_be_complete = require_complete or rel in _UPSTREAM_ARTIFACT_PRODUCER
    if ctx.artifact_exists(rel):
        from interview_mux.artifact_lifecycle import lifecycle_cfg, read_stale_guard

        if lifecycle_cfg().get("read_stale_guard", True):
            consumer = getattr(ctx, "_lifecycle_consumer_stage", None) or "preflight"
            stale = read_stale_guard(ctx, rel, consumer_stage=str(consumer))
            if stale:
                return StageInputIssue(stale, remediation or f"Re-run upstream producer for {rel}.")
        if must_be_complete:
            from interview_mux.artifact_completeness import artifact_status

            st = artifact_status(rel, ctx)
            if st != "complete":
                fix = remediation or _remediation_for_missing_artifact(ctx, rel)
                return StageInputIssue(
                    f"{label or rel} is {st} (not complete)",
                    fix or f"Re-run upstream producer for {rel}.",
                )
        return None
    hint = staging_approval_hint(ctx, rel)
    msg = f"Missing {label or rel}"
    fix = remediation or hint
    if hint and remediation:
        fix = f"{remediation} ({hint})"
    return StageInputIssue(msg, fix)


def _require_audio(ctx: RunContext) -> list[StageInputIssue]:
    issues: list[StageInputIssue] = []
    if ctx.artifact_exists("preclean/isolated.wav"):
        return issues
    issue = _require_artifact(
        ctx,
        "ingest/normalized.wav",
        label="normalized interview audio",
        remediation="Run and approve the ingest stage.",
    )
    if issue:
        issues.append(issue)
    return issues


def _g0_issues(ctx: RunContext) -> list[StageInputIssue]:
    if not check_transcript_review_pending(ctx):
        return []
    return [
        StageInputIssue(
            "Transcript review (G0) is incomplete",
            "Open Transcript review in the GUI, correct ranked clips, then Complete transcript review.",
        )
    ]


def _analysis_gate_issues(ctx: RunContext) -> list[StageInputIssue]:
    return _g0_issues(ctx)


def _check_source_acoustic_profile(ctx: RunContext) -> list[StageInputIssue]:
    issues = _analysis_gate_issues(ctx)
    issues.extend(_require_audio(ctx))
    issue = _require_artifact(
        ctx,
        "transcript/full.json",
        remediation="Run transcribe and complete G0 transcript review.",
    )
    if issue:
        issues.append(issue)
    return issues


def _check_interview_spine_build(ctx: RunContext) -> list[StageInputIssue]:
    issues = _analysis_gate_issues(ctx)
    issues.extend(_require_audio(ctx))
    for rel, remediation in (
        ("transcript/full.json", "Run transcribe and complete G0."),
        (
            "understanding/source_acoustic_profile.json",
            "Run source_acoustic_profile (stage 8) and approve its outputs.",
        ),
    ):
        issue = _require_artifact(ctx, rel, remediation=remediation)
        if issue:
            issues.append(issue)
    return issues


def _check_sonic_context_build(ctx: RunContext) -> list[StageInputIssue]:
    issues = _analysis_gate_issues(ctx)
    if not ctx.artifact_exists("segments/manifest.json") and ctx.artifact_exists(
        "segments/boundaries.json"
    ):
        try:
            from interview_mux.artifact_completeness import align_manifest_ids_to_boundaries

            align_manifest_ids_to_boundaries(ctx)
        except Exception:
            pass
    for rel, remediation in (
        ("understanding/content_brief.json", "Run content_context and content_brief_reanchor."),
        ("segments/manifest.json", "Run boundary_detection and segment_classification."),
        (
            "understanding/source_acoustic_profile.json",
            "Run source_acoustic_profile before sonic context.",
        ),
    ):
        issue = _require_artifact(ctx, rel, remediation=remediation)
        if issue:
            issues.append(issue)
    return issues


def _check_transcript_review_build(ctx: RunContext) -> list[StageInputIssue]:
    issues: list[StageInputIssue] = []
    issues.extend(_require_audio(ctx))
    issue = _require_artifact(ctx, "transcript/full.json", remediation="Run transcribe first.")
    if issue:
        issues.append(issue)
    return issues


def _check_vo_ingest(ctx: RunContext) -> list[StageInputIssue]:
    issues: list[StageInputIssue] = []
    issue = _require_artifact(ctx, "understanding/gap_report.json", remediation="Run optimal_questions.")
    if issue:
        issues.append(issue)
        return issues
    missing = check_g1_vo(ctx)
    if missing:
        issues.append(
            StageInputIssue(
                f"Missing VO pickup WAV(s) for: {missing}",
                f"Record files under vo_pickup/{{line_id}}.wav — see understanding/interviewer_script.txt.",
            )
        )
    return issues


def _check_assembly_preview(ctx: RunContext) -> list[StageInputIssue]:
    issues: list[StageInputIssue] = []
    issues.extend(_require_audio(ctx))
    issue = _require_artifact(ctx, "master/edl.json", remediation="Run edl.")
    if issue:
        issues.append(issue)
    issues.extend(_edl_seat_preflight_issues(ctx, consumer="assembly_preview"))
    stale = compact_vo_coverage_stale_or_missing(ctx)
    if stale:
        issues.append(
            StageInputIssue(
                f"VO coverage not rendered: {stale[:4]}",
                "Re-run vo_synthesize so seated WAVs exist before assembly_preview.",
                kind="vo_coverage",
                related_stage="vo_synthesize",
            )
        )
    try:
        from interview_mux.stage_completion import assembly_preview_unsourced_glue_ids

        edl = (
            ctx.read_json("master/edl.json")
            if ctx.artifact_exists("master/edl.json")
            else None
        )
        unsourced = assembly_preview_unsourced_glue_ids(
            edl if isinstance(edl, dict) else None
        )
        # A vo_pickup clip whose rendered WAV is already on disk is a *bind* gap,
        # and run_preview heals it on entry. Refusing here made the heal
        # unreachable and mix silenced the host lines (exec_11871).
        if unsourced and isinstance(edl, dict):
            from interview_mux.stages.assembly import vo_clip_wav_resolvable

            bindable = {
                str(clip.get("line_id") or "")
                for clip in (edl.get("clips") or [])
                if isinstance(clip, dict)
                and not str(clip.get("source_path") or "").strip()
                and clip.get("line_id")
                and vo_clip_wav_resolvable(ctx, clip)
            }
            if bindable:
                unsourced = [i for i in unsourced if str(i) not in bindable]
    except Exception:
        unsourced = []
    if unsourced:
        issues.append(
            StageInputIssue(
                f"current transition pairs missing WAV: {unsourced[:4]}",
                "Re-run vo_synthesize; do not skip unsourced glue then stamp preview done.",
                kind="vo_coverage",
                related_stage="vo_synthesize",
            )
        )
    return issues


def _check_mix(ctx: RunContext) -> list[StageInputIssue]:
    issues: list[StageInputIssue] = []
    issues.extend(_require_audio(ctx))
    for rel, remediation in (
        ("master/selection.json", "Run full_master_ranking so mix has a selection."),
        ("master/edl.json", "Run edl."),
        ("understanding/sound_design_plan.json", "Run sound_design_plan."),
    ):
        issue = _require_artifact(ctx, rel, remediation=remediation)
        if issue:
            issues.append(issue)
    issues.extend(_edl_seat_preflight_issues(ctx, consumer="mix"))
    issues.extend(_vo_script_wav_agreement_issues(ctx, consumer="mix"))
    return issues


def _edl_seat_preflight_issues(
    ctx: RunContext, *, consumer: str
) -> list[StageInputIssue]:
    """Wave 3: seat authority before hash gates (heal ≠ skip required seats)."""
    if not ctx.artifact_exists("master/edl.json"):
        return []
    try:
        from interview_mux.vo_synthesis_audit import edl_seat_preflight

        report = edl_seat_preflight(ctx)
    except Exception as exc:
        return [
            StageInputIssue(
                f"EDL seat preflight failed: {exc}",
                f"Re-run vo_synthesize / edl before {consumer}.",
                kind="edl_seat",
            )
        ]
    issues: list[StageInputIssue] = []
    if not report.get("ok", True):
        errs = list(report.get("errors") or [])[:6]
        issues.append(
            StageInputIssue(
                f"EDL seat preflight blocked {consumer}: {', '.join(str(e) for e in errs)}",
                "Heal seated VO WAVs / script stamps; never skip required seats.",
                kind="edl_seat",
            )
        )
    try:
        from interview_mux.hosted_vo_authority import assert_books_agree

        for err in assert_books_agree(ctx):
            issues.append(
                StageInputIssue(
                    err,
                    "Reconcile orientation gap/EDL via hosted_vo_authority apply/ensure.",
                    kind="edl_seat",
                )
            )
    except Exception:
        pass
    return issues


def _vo_script_wav_agreement_issues(
    ctx: RunContext, *, consumer: str
) -> list[StageInputIssue]:
    """Hard-stop when synthetic VO text and audible WAV audit disagree."""
    from interview_mux.vo_synthesis_audit import (
        audible_script_hash_errors,
        canonicalize_synthesis_out_wav_paths,
        purge_stale_vo_wavs_for_script_drift,
    )

    # Pre-mix: restore dropped adjacency transitions, regenerate missing WAVs,
    # and rewrite audit/EDL paths to committed files before failing the gate.
    if consumer == "mix":
        try:
            from interview_mux.transition_vo import ensure_pre_mix_transition_integrity

            ensure_pre_mix_transition_integrity(ctx, synthesize=True)
        except Exception:
            try:
                canonicalize_synthesis_out_wav_paths(ctx)
            except Exception:
                pass

    if not ctx.artifact_exists("master/edl.json"):
        # Pre-EDL: seated gap coverage still must not be stale.
        stale = compact_vo_coverage_stale_or_missing(ctx)
        if stale:
            return [
                StageInputIssue(
                    f"Synthetic VO script/WAV mismatch for: {stale[:6]}",
                    "Re-run vo_synthesize so every seated line is re-rendered "
                    f"before {consumer}; never mix with stale VO audio.",
                    kind="vo_coverage",
                )
            ]
        return []
    edl = ctx.read_json("master/edl.json")
    errors = audible_script_hash_errors(ctx, edl if isinstance(edl, dict) else None)
    if not errors:
        return []
    # Path-only / inventory drift may clear after canonicalization — recheck once.
    try:
        canonicalize_synthesis_out_wav_paths(ctx)
        from interview_mux.transition_vo import restamp_edl_transition_source_paths
        from interview_mux.vo_synthesis_audit import sync_edl_vo_script_metadata

        restamp_edl_transition_source_paths(ctx)
        sync_edl_vo_script_metadata(ctx)
        edl = ctx.read_json("master/edl.json")
        errors = audible_script_hash_errors(ctx, edl if isinstance(edl, dict) else None)
    except Exception:
        pass
    if not errors:
        return []
    # Only purge gap-line WAVs for non-transition script drift — transition path
    # heal above must not collateral-delete layup pickups.
    transition_only = all(
        (":missing_wav" in e and e.startswith("tr_"))
        or "missing_current_transition" in e
        or e.startswith("tr_")
        for e in errors
    )
    if not transition_only:
        try:
            purge_stale_vo_wavs_for_script_drift(ctx)
        except Exception:
            pass
    return [
        StageInputIssue(
            f"Synthetic VO does not match EDL/gap script: {errors[:6]}",
            "Re-run vo_synthesize (never rebind audit onto old WAVs), then rebuild "
            f"edl before {consumer}.",
            kind="vo_coverage",
        )
    ]


def _check_junction_snip_qa(ctx: RunContext) -> list[StageInputIssue]:
    issues: list[StageInputIssue] = []
    required: list[tuple[str, str]] = [
        ("master/edl.json", "Run edl then mix before junction_snip_qa."),
        ("master/assembly.wav", "Run mix before junction_snip_qa."),
    ]
    # exec_11871: mix refuses while live incomplete-cut criticals exist and pins
    # junction_snip_qa, so the recut pass has to be able to run before the first
    # assembly exists. The EDL recut ladder needs only master/edl.json — the
    # remaster it drives is what mints assembly.wav.
    if not ctx.artifact_exists("master/assembly.wav"):
        try:
            from interview_mux.junction_snip_qa import junction_recut_precedes_mix

            if junction_recut_precedes_mix(ctx):
                required = [r for r in required if r[0] != "master/assembly.wav"]
        except Exception:
            pass
    for rel, remediation in required:
        issue = _require_artifact(ctx, rel, remediation=remediation)
        if issue:
            issues.append(issue)
    return issues


def _check_master_finalize(ctx: RunContext) -> list[StageInputIssue]:
    issues: list[StageInputIssue] = []
    issues.extend(_edl_seat_preflight_issues(ctx, consumer="master_finalize"))
    issue = _require_artifact(
        ctx,
        "master/assembly.wav",
        remediation="Run mix and approve assembly.wav.",
    )
    if issue:
        issues.append(issue)
    # Soft publishability / invalidate must not leave finalize without EDL —
    # restore from archive when present, else pin edl (never ranking).
    if not ctx.artifact_exists("master/edl.json"):
        try:
            from interview_mux.delivery_recovery import restore_master_artifact

            restored = restore_master_artifact(ctx, "master/edl.json", min_bytes=32)
            if restored is not None and restored.is_file():
                restore_master_artifact(ctx, "master/assembly_ledger.json", min_bytes=32)
        except Exception:
            pass
    if not ctx.artifact_exists("master/edl.json"):
        issues.append(
            StageInputIssue(
                "master/edl.json missing",
                "Restore archived EDL or re-run edl before master_finalize.",
            )
        )
        return issues
    if ctx.artifact_exists("master/selection.json") and ctx.artifact_exists("master/edl.json"):
        from interview_mux.order_hash import order_hashes_match

        sel = ctx.read_json("master/selection.json")
        edl = ctx.read_json("master/edl.json")
        if isinstance(sel, dict) and isinstance(edl, dict) and not order_hashes_match(sel, edl):
            issues.append(
                StageInputIssue(
                    "selection order drifted from edl",
                    "Re-run edl after order changes, then mix before master_finalize.",
                )
            )
    if ctx.artifact_exists("master/assembly_ledger.json"):
        ledger = ctx.read_json("master/assembly_ledger.json")
        if isinstance(ledger, dict) and not ledger.get("complete", True):
            n = int(ledger.get("naked_seam_count") or 0)
            meta = (
                ctx.read_json("run_meta.json")
                if ctx.artifact_exists("run_meta.json")
                else {}
            )
            soft_junction = bool(
                isinstance(meta, dict) and meta.get("e2e_soft_junction_residuals")
            )
            # Soft e2e ship: allow finalize with residual naked seams once assembly
            # exists and junction budget was exhausted / soft-passed.
            if soft_junction and ctx.artifact_exists("master/assembly.wav"):
                pass
            else:
                issues.append(
                    StageInputIssue(
                        f"assembly_ledger has {n} naked seam(s)",
                        "Rebuild edl so every reorder join has audible VO/transition glue.",
                    )
                )
    elif ctx.artifact_exists("master/edl.json"):
        # Ledger is re-runnable from EDL without a full NLE — emit it here so
        # identical_failures do not spin on a missing consumer-only pin.
        try:
            from interview_mux.assembly_ledger import write_assembly_ledger

            edl_doc = ctx.read_json("master/edl.json")
            write_assembly_ledger(ctx, edl=edl_doc if isinstance(edl_doc, dict) else None)
        except Exception:
            issues.append(
                StageInputIssue(
                    "master/assembly_ledger.json missing",
                    "Re-run edl to emit the assembly ledger.",
                )
            )
    # bridge_completeness incomplete is advisory (ISSUES 185), not an input issue.
    if not ctx.artifact_exists("master/seam_autopsy.json"):
        issues.append(
            StageInputIssue(
                "master/seam_autopsy.json missing",
                "Re-run junction_snip_qa so seam decisions and commitment are verified.",
            )
        )
    else:
        autopsy = ctx.read_json("master/seam_autopsy.json")
        commitment = autopsy.get("commitment") if isinstance(autopsy, dict) else {}
        if not isinstance(commitment, dict) or commitment.get("status") != "committed":
            # Stale archived autopsy often says diverged after a later remaster.
            try:
                from interview_mux.seam_autopsy import refresh_autopsy_commitment

                refreshed = refresh_autopsy_commitment(ctx)
                commitment = (
                    (refreshed or {}).get("commitment")
                    if isinstance(refreshed, dict)
                    else commitment
                )
            except Exception:
                pass
        if not isinstance(commitment, dict) or commitment.get("status") != "committed":
            issues.append(
                StageInputIssue(
                    "seam autopsy commitment is not committed",
                    "Run the bounded junction remediation and remaster before master_finalize.",
                )
            )
    if not ctx.artifact_exists("master/render_ledger.json"):
        issues.append(
            StageInputIssue(
                "master/render_ledger.json missing",
                "Re-run mix/junction so rendered timing is bound to the current EDL.",
            )
        )
    if (
        (ctx.is_done("mastering_plan_synthesize") or ctx.is_done("mastering_plan_confirm"))
        and not ctx.artifact_exists("mastering/mastering_plan.json")
    ):
        issues.append(
            StageInputIssue(
                "mastering plan stage is complete but mastering/mastering_plan.json is missing",
                "Re-run mastering_plan_synthesize and confirm the resulting plan.",
            )
        )
    return issues


def _check_master_transcript_build(ctx: RunContext) -> list[StageInputIssue]:
    issues: list[StageInputIssue] = []
    for rel, hint in (
        ("master/master.wav", "Run master_finalize first."),
        ("master/edl.json", "EDL is required to remap sidecars onto the master timeline."),
    ):
        issue = _require_artifact(ctx, rel, remediation=hint)
        if issue:
            issues.append(issue)
    return issues


def _check_mmaudio_sfx(ctx: RunContext) -> list[StageInputIssue]:
    issues: list[StageInputIssue] = []
    for rel, remediation in (
        ("understanding/sound_design_plan.json", "Run sound_design_plan."),
        ("sound_design/sfx_prompts.json", "Run sfx_prompt_craft and approve prompts."),
    ):
        issue = _require_artifact(ctx, rel, remediation=remediation)
        if issue:
            issues.append(issue)
    issue = _require_artifact(
        ctx,
        "master/assembly_preview.wav",
        label="assembly preview WAV",
        remediation="Run assembly_preview and listen before SFX spend.",
    )
    if issue:
        issues.append(issue)
    return issues


def _check_edl_narrative_audit(ctx: RunContext) -> list[StageInputIssue]:
    from interview_mux.delivery_guardrails import seed_stage_complete

    issues: list[StageInputIssue] = []
    if not seed_stage_complete(ctx, "vo_synthesize"):
        issues.append(
            StageInputIssue(
                "vo_synthesize not seed-complete — EDL narrative audit requires heard WAV flow (5C).",
                "Run vo_line_adjudicate then vo_synthesize before edl_narrative_audit.",
                related_stage="vo_synthesize",
            )
        )
    synth_missing = compact_vo_coverage_stale_or_missing(ctx)
    if synth_missing:
        issues.append(
            StageInputIssue(
                f"VO coverage not rendered: {synth_missing[:4]}",
                "Re-run vo_synthesize after adjudicate text is final.",
                kind="vo_coverage",
            )
        )
    return issues


def compact_vo_coverage_stale_or_missing(
    ctx: RunContext, *, coverage: set[str] | None = None
) -> list[str]:
    from interview_mux.air_script import seated_vo_line_ids
    from interview_mux.mastering_plan_loader import load_plan_raw
    from interview_mux.stages.edl_narrative_audit import compact_vo_coverage

    seated: set[str] = set()
    if ctx.artifact_exists("mastering/mastering_plan.json"):
        try:
            plan = load_plan_raw(ctx)
            seated = set(seated_vo_line_ids(plan))
        except Exception:
            seated = set()
    missing: list[str] = []
    for row in compact_vo_coverage(ctx):
        if not isinstance(row, dict):
            continue
        cov = str(row.get("coverage") or "")
        lid = str(row.get("line_id") or "")
        if cov in (coverage or {"missing", "wav_stale"}) and (
            row.get("required") or (lid and lid in seated)
        ):
            missing.append(lid)
    return [x for x in missing if x]


def _check_edl(ctx: RunContext) -> list[StageInputIssue]:
    issues: list[StageInputIssue] = []
    for rel, remediation in (
        ("master/selection.json", "Run full_master_ranking."),
        ("master/transitions.json", "Run transitions."),
        ("understanding/gap_report.json", "Run optimal_questions."),
    ):
        issue = _require_artifact(ctx, rel, remediation=remediation)
        if issue:
            issues.append(issue)
    missing = check_g1_vo(ctx)
    if missing:
        issues.append(
            StageInputIssue(
                f"G1 VO pickup missing for: {missing}",
                "Record vo_pickup WAVs before building the EDL.",
            )
        )
    stale = compact_vo_coverage_stale_or_missing(ctx)
    if stale:
        issues.append(
            StageInputIssue(
                f"Synthetic VO script/WAV mismatch for: {stale[:6]}",
                "Re-run vo_synthesize so WAVs match current gap text before edl.",
                kind="vo_coverage",
            )
        )
    return issues


def _check_flow1_profile_gate(ctx: RunContext) -> list[StageInputIssue]:
    """v2 removed the analysis-profile / ``operator_verified`` operator gate.

    Callers keep the helper so stage checkers stay call-compatible, but it must
    never block delivery. Homunculus 0.1.0 and Full-auto both hit this path.
    """
    return []


def _check_topic_coverage_audit(ctx: RunContext) -> list[StageInputIssue]:
    issues = _check_flow1_profile_gate(ctx)
    from interview_mux.artifact_repairs import heal_content_brief_orphan_segment_ids
    from interview_mux.progression_readiness import build_delivery_readiness_report

    # A segment that left the manifest after reanchor must not halt delivery
    # at its first stage when its tape is still on the manifest (ISSUES 139).
    heal_content_brief_orphan_segment_ids(ctx)
    report = build_delivery_readiness_report(ctx, target_stage="topic_coverage_audit")
    for row in report.get("blockers") or []:
        if isinstance(row, dict):
            issues.append(
                StageInputIssue(
                    str(row.get("message", "Flow 1 not ready")),
                    f"Layer: {row.get('layer', 'readiness')}",
                )
            )
    return issues


def _check_narrative_arc_plan(ctx: RunContext) -> list[StageInputIssue]:
    issues: list[StageInputIssue] = []
    issues.extend(_check_flow1_profile_gate(ctx))
    for rel, remediation in (
        ("master/coverage_audit.json", "Run topic_coverage_audit."),
        ("understanding/content_brief.json", "Run content_context and content_brief_reanchor."),
    ):
        issue = _require_artifact(ctx, rel, remediation=remediation)
        if issue:
            issues.append(issue)
    return issues


def _check_chapter_close_hitch(ctx: RunContext) -> list[StageInputIssue]:
    issues: list[StageInputIssue] = []
    if ctx.artifact_exists("master/narrative_plan.json"):
        return issues
    # Crash-resume: clear_from archives narrative_plan; frozen intent is enough.
    if ctx.artifact_exists("mastering/chapter_close_hitch/intent_plan.json"):
        return issues
    issue = _require_artifact(
        ctx, "master/narrative_plan.json", remediation="Run narrative_arc_plan."
    )
    if issue:
        issues.append(issue)
    return issues


def _check_full_master_ranking(ctx: RunContext) -> list[StageInputIssue]:
    issues: list[StageInputIssue] = []
    issues.extend(_check_flow1_profile_gate(ctx))
    for rel, remediation in (
        ("master/narrative_plan.json", "Run narrative_arc_plan."),
        ("segments/manifest.json", "Run segment_classification."),
        ("understanding/gap_report.json", "Run optimal_questions."),
    ):
        issue = _require_artifact(ctx, rel, remediation=remediation)
        if issue:
            issues.append(issue)
    return issues


def _check_sound_design_plan(ctx: RunContext) -> list[StageInputIssue]:
    issues: list[StageInputIssue] = []
    for rel, remediation in (
        ("master/selection.json", "Run full_master_ranking."),
        ("master/transitions.json", "Run transitions."),
        ("understanding/gap_report.json", "Run optimal_questions."),
    ):
        issue = _require_artifact(ctx, rel, remediation=remediation)
        if issue:
            issues.append(issue)
    return issues


def _check_sound_design_vo_finalize(ctx: RunContext) -> list[StageInputIssue]:
    """C-02 / expanded WS2: finalize measures seated WAVs after vo_synthesize."""
    issues: list[StageInputIssue] = []
    try:
        from interview_mux.delivery_guardrails import seed_stage_complete

        if not seed_stage_complete(ctx, "vo_synthesize"):
            issues.append(
                StageInputIssue(
                    message="sound_design_vo_finalize requires seed-complete vo_synthesize",
                    remediation="Run vo_synthesize until seated WAVs are complete.",
                    related_stage="vo_synthesize",
                )
            )
    except Exception:
        if not ctx.is_done("vo_synthesize"):
            issues.append(
                StageInputIssue(
                    message="sound_design_vo_finalize requires vo_synthesize done",
                    remediation="Run vo_synthesize.",
                    related_stage="vo_synthesize",
                )
            )
    issue = _require_artifact(
        ctx,
        "understanding/sound_design_plan.json",
        remediation="Run sound_design_plan.",
    )
    if issue:
        issues.append(issue)
    return issues


def _check_sanitize_predecessor(
    ctx: RunContext, *, producer: str, consumer: str
) -> list[StageInputIssue]:
    issues: list[StageInputIssue] = []
    try:
        from interview_mux.delivery_guardrails import seed_stage_complete

        if not seed_stage_complete(ctx, producer):
            issues.append(
                StageInputIssue(
                    message=f"{consumer} requires seed-complete {producer}",
                    remediation=f"Run {producer}.",
                    related_stage=producer,
                )
            )
    except Exception:
        pass
    return issues


def _check_air_script_compose(ctx: RunContext) -> list[StageInputIssue]:
    return _check_sanitize_predecessor(
        ctx, producer="selection_order_sanitize", consumer="air_script_compose"
    )


def _check_gap_report_sanitize(ctx: RunContext) -> list[StageInputIssue]:
    return _check_sanitize_predecessor(
        ctx, producer="nugget_layup_compose", consumer="gap_report_sanitize"
    )


def _check_air_contract_sanitize(ctx: RunContext) -> list[StageInputIssue]:
    return _check_sanitize_predecessor(
        ctx, producer="air_script_seams", consumer="air_contract_sanitize"
    )


_UPSTREAM_ARTIFACT_PRODUCER: dict[str, str] = {
    "understanding/speakers.json": "speaker_roles",
    "understanding/content_brief.json": "content_context",
    "segments/boundaries.json": "boundary_detection",
    "segments/manifest.json": "segment_classification",
}


def _latest_stage_attempt_excerpt(ctx: RunContext, stage_id: str) -> str | None:
    audit_dir = ctx.path("understanding", "stage_runs", stage_id)
    if not audit_dir.is_dir():
        return None
    attempts = sorted(audit_dir.glob("attempt_*.json"))
    if not attempts:
        return None
    try:
        doc = ctx.read_json(str(attempts[-1].relative_to(ctx.run_dir)).replace("\\", "/"))
    except Exception:
        return None
    if not isinstance(doc, dict):
        return None
    extra = doc.get("extra") or {}
    if isinstance(extra, dict):
        for key in ("schema_errors", "verification_errors", "blocked_paths"):
            val = extra.get(key)
            if val:
                return str(val)[:240]
    llm_meta = doc.get("_llm_meta") or {}
    if isinstance(llm_meta, dict):
        ver = llm_meta.get("verification_errors")
        if ver:
            return str(ver)[:240]
    env = doc.get("envelope") or doc
    if isinstance(env, dict):
        needs = env.get("needs") or []
        for need in needs:
            if isinstance(need, dict) and need.get("reason"):
                return str(need["reason"])[:240]
    return None


def _remediation_for_missing_artifact(ctx: RunContext, rel: str) -> str | None:
    producer = _UPSTREAM_ARTIFACT_PRODUCER.get(rel)
    if not producer:
        return None
    excerpt = _latest_stage_attempt_excerpt(ctx, producer)
    base = f"Re-run upstream stage '{producer}'"
    if excerpt:
        return f"{base} — last error: {excerpt}"
    return f"{base} or use Rerun from this step on {producer}."


def _llm_remediation(error: str, ctx: RunContext | None = None) -> str | None:
    low = error.lower()
    if ctx is not None:
        for rel, producer in _UPSTREAM_ARTIFACT_PRODUCER.items():
            if rel.replace("/", " ") in low or rel.split("/")[-1] in low:
                hint = _remediation_for_missing_artifact(ctx, rel)
                if hint:
                    return hint
    if "g0" in low or "transcript review" in low:
        return "Complete G0 transcript review in the GUI."
    if "interview_spine" in low:
        return "Run interview_spine_build and approve outputs."
    if "interviewer" in low:
        return "Re-run speaker_roles or edit understanding/speakers.json."
    if "boundaries" in low:
        return "Re-run boundary_detection."
    if "manifest" in low:
        return "Re-run segment_classification."
    if "content_brief" in low or "thesis" in low:
        return "Re-run content_context or content_brief_reanchor."
    if "gap_evaluations" in low:
        return "Re-run missing_framing."
    if "operator_verified" in low or "profile" in low:
        return "Mark the interview profile verified in the GUI."
    if "sdp" in low or "sound_design_plan" in low:
        return "Re-run sound design plan stages."
    if "sfx_prompts" in low:
        return "Run sfx_prompt_craft and approve prompts."
    return None


_LLM_STAGES = frozenset(
    {
        "speaker_roles",
        "content_context",
        "boundary_detection",
        "segment_classification",
        "content_brief_reanchor",
        "sound_design_palettes",
        "missing_framing",
        "optimal_questions",
        "topic_coverage_audit",
        "narrative_arc_plan",
        "full_master_ranking",
        "transitions",
        "sound_design_plan",
        "sound_design_plan_flow2",
        "sfx_prompt_craft",
        "edl_narrative_audit",
        "vo_line_adjudicate",
        "podcast_show_description",
    }
)

_STAGE_CHECKERS: dict[str, Callable[[RunContext], list[StageInputIssue]]] = {
    "transcript_review_build": _check_transcript_review_build,
    "source_acoustic_profile": _check_source_acoustic_profile,
    "interview_spine_build": _check_interview_spine_build,
    "sonic_context_build": _check_sonic_context_build,
    "vo_ingest": _check_vo_ingest,
    "topic_coverage_audit": _check_topic_coverage_audit,
    "narrative_arc_plan": _check_narrative_arc_plan,
    "chapter_close_hitch": _check_chapter_close_hitch,
    "full_master_ranking": _check_full_master_ranking,
    "sound_design_plan": _check_sound_design_plan,
    "sound_design_vo_finalize": _check_sound_design_vo_finalize,
    "air_script_compose": _check_air_script_compose,
    "gap_report_sanitize": _check_gap_report_sanitize,
    "air_contract_sanitize": _check_air_contract_sanitize,
    "assembly_preview": _check_assembly_preview,
    "mix": _check_mix,
    "junction_snip_qa": _check_junction_snip_qa,
    "master_finalize": _check_master_finalize,
    "master_transcript_build": _check_master_transcript_build,
    "mmaudio_sfx": _check_mmaudio_sfx,
    "edl": _check_edl,
    "edl_narrative_audit": _check_edl_narrative_audit,
}

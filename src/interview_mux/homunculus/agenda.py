"""Homunculus phase scheduler: seed-order walk, skips, and surgical reruns."""

from __future__ import annotations

import re

import json
import shutil
from datetime import datetime, timezone
from typing import Any

from interview_mux.homunculus.ledger import append_ledger, remainder_requested
from interview_mux.run_context import RunContext
from interview_mux.stage_completion import heal_or_refuse_mark
from interview_mux.v2.config import ANALYSIS_ORDER, DELIVERY_ORDER, SHIP_AFTER_MASTER

AGENDA_REL = "mastering/homunculus/agenda.json"
PROTECTED_ISLAND_STAGES = frozenset(
    {
        "low_conf_island_scan",
        "connector_fuse_pass",
        "connector_fuse_pass_pre_ranking",
    }
)
_ISLAND_ARTIFACTS = (
    "analysis/low_conf_must_keep.json",
    "analysis/low_conf_islands.json",
    "analysis/connector_fuse_audit.json",
)
_PRE_RANKING_ROUNDS = "analysis/connector_fuse_rounds_pre_ranking.json"
# After G0 is closed, re-STT / re-ingest / re-clip is not a surgical rerun — pack g0_transcript.
G0_LOCKED_RERUN_STAGES = frozenset(
    {"transcribe", "ingest", "audio_preclean", "transcript_review_build"}
)
# Delivery must not rewind the classified timeline; fill gap artifacts instead.
DELIVERY_LOCKED_TIMELINE_STAGES = frozenset(
    {
        "transcript_review_build",
        "speaker_roles",
        "content_context",
        "talking_points_compose",
        "ideal_cuts_propose",
        "ideal_cuts_materialize",
        "boundary_detection",
        "segment_classification",
        "content_brief_reanchor",
    }
)
DELIVERY_ANALYSIS_PREREQS: tuple[tuple[str, str], ...] = (
    ("source_topology_build", "understanding/source_topology.json"),
    ("boundary_detection", "segments/boundaries.json"),
    ("segment_classification", "segments/manifest.json"),
    # content_context before reanchor: shared brief path; seed-order for
    # missing_framing requires content_context when the brief is gone.
    ("content_context", "understanding/content_brief.json"),
    ("content_brief_reanchor", "understanding/content_brief.json"),
    ("framing_posture_decide", "understanding/framing_posture_decision.json"),
    ("missing_framing", "understanding/gap_evaluations.json"),
    ("gap_framing_compose", "understanding/gap_report.json"),
    ("delivery_brief_build", "understanding/delivery_brief.json"),
)
PREPARE_STAGE_OUTPUTS: dict[str, tuple[str, ...]] = {
    "audio_preclean": (
        "preclean/isolated.wav",
        "preclean/provider.json",
        "preclean/lineage.json",
        # HP-3: operator skip is a finished skip — do not hollow-unmark it.
        "preclean/skip.json",
    ),
    "ingest": ("ingest/normalized.wav",),
    "transcribe": ("transcript/full.json",),
    "transcript_review_build": ("transcript/review_queue.json",),
}


def prepare_outputs_present(ctx: RunContext, stage: str) -> bool:
    if stage == "transcript_review_build":
        try:
            from interview_mux.stage_completion import stage_artifact_incompleteness

            return stage_artifact_incompleteness(ctx, stage) is None
        except Exception:
            return ctx.artifact_exists("transcript/review_queue.json")
    needed = PREPARE_STAGE_OUTPUTS.get(stage)
    if not needed:
        return True
    if stage in {"ingest", "transcribe"}:
        return all(ctx.artifact_exists(rel) for rel in needed)
    return any(ctx.artifact_exists(rel) for rel in needed)


def unmark_hollow_prepare_stages(ctx: RunContext) -> list[str]:
    """Clear .stage_done for prepare stages that never wrote their artifacts."""
    cleared: list[str] = []
    for stage in G0_LOCKED_RERUN_STAGES:
        # DETECTION_ONLY_IS_DONE: hollow stamp detection → unmark, never skip work.
        if ctx.is_done(stage) and not prepare_outputs_present(ctx, stage):
            unmark_stage_only(ctx, stage)
            cleared.append(stage)
    return cleared


def _refuse_g0_locked_rerun(ctx: RunContext, stage: str, *, action: str) -> None:
    from interview_mux.homunculus.packer import g0_closed

    if stage not in G0_LOCKED_RERUN_STAGES:
        return
    if not g0_closed(ctx):
        return
    if not prepare_outputs_present(ctx, stage):
        # Hollow done marker — this is the first real run, not a post-G0 rerun.
        return
    raise RuntimeError(
        f"cannot {action} {stage}: G0 is closed; pack g0_transcript instead of re-running STT"
    )


def _manifest_has_classified_segments(ctx: RunContext) -> bool:
    if not ctx.artifact_exists("segments/manifest.json"):
        return False
    try:
        man = ctx.read_json("segments/manifest.json")
    except Exception:
        return False
    segs = man.get("segments") if isinstance(man, dict) else []
    return any(
        isinstance(s, dict) and s.get("segment_id") and s.get("speaker_role")
        for s in (segs or [])
    )


def _refuse_classified_manifest_rerun(ctx: RunContext, stage: str, *, action: str) -> None:
    """Nested gap-eval often asks to reclassify when it only saw G0 words."""
    if stage != "segment_classification":
        return
    if not _manifest_has_classified_segments(ctx):
        return
    raise RuntimeError(
        f"cannot {action} segment_classification: classified segment_id and "
        "speaker_role already exist; pack segment_manifest for gap eval"
    )


def _delivery_phase_active(ctx: RunContext) -> bool:
    return str(_read_agenda(ctx).get("phase") or "") == "delivery"


_GAP_FILL_ANALYSIS_PREREQS = frozenset({"missing_framing", "gap_framing_compose"})


def _restore_skipped_gap_prereqs(ctx: RunContext) -> None:
    """Re-materialize skip artifacts so delivery is not blocked after a wrap discard."""
    try:
        from interview_mux.gap_fill_eligibility import (
            assess_gap_fill_eligibility,
            gap_fill_was_skipped,
        )
        from interview_mux.stages.gaps import ensure_gap_fill_skipped
    except Exception:
        return
    if not gap_fill_was_skipped(ctx):
        return
    decision = assess_gap_fill_eligibility(ctx)
    ensure_gap_fill_skipped(ctx, reason=decision.reason, signals=decision.signals)


def pending_analysis_for_delivery(ctx: RunContext) -> list[str]:
    """Analysis producers topic_coverage_audit needs before a delivery walk."""
    pending: list[str] = []
    gap_skipped = False
    native_only = False
    try:
        from interview_mux.pipeline_mode import is_native_only

        native_only = bool(is_native_only(ctx))
    except Exception:
        native_only = False
    try:
        from interview_mux.gap_fill_eligibility import gap_fill_was_skipped

        gap_skipped = bool(gap_fill_was_skipped(ctx))
    except Exception:
        gap_skipped = False
    if native_only:
        gap_skipped = True
    if gap_skipped:
        _restore_skipped_gap_prereqs(ctx)
    from interview_mux.stage_completion import stage_artifact_incompleteness

    def _keep_if_incomplete(stage_id: str, *, already_healed: bool = False) -> None:
        """Wave 1a: file-exists is not delivery-ready when heal refuses or not done."""
        if not already_healed:
            heal_or_refuse_mark(ctx, stage_id, force=True)
        try:
            inc_after = stage_artifact_incompleteness(ctx, stage_id)
        except Exception:
            inc_after = "incompleteness_check_failed"
        from interview_mux.delivery_guardrails import seed_stage_complete

        if inc_after or not seed_stage_complete(ctx, stage_id):
            if stage_id not in pending:
                pending.append(stage_id)

    for stage, rel in DELIVERY_ANALYSIS_PREREQS:
        if gap_skipped and stage in _GAP_FILL_ANALYSIS_PREREQS:
            if ctx.artifact_exists(rel):
                from interview_mux.delivery_guardrails import seed_stage_complete

                if not seed_stage_complete(ctx, stage):
                    heal_or_refuse_mark(ctx, stage, force=True)
                _keep_if_incomplete(stage, already_healed=True)
                continue
            pending.append(stage)
            continue
        if ctx.artifact_exists(rel):
            # Stale shared producers (e.g. boundaries.json stamped by
            # clear_from(boundary_detection)) must stay pending even when
            # stage_done is set — else delivery walks SC first.
            try:
                inc = stage_artifact_incompleteness(ctx, stage)
            except Exception:
                inc = None
            if inc and "stale" in str(inc).lower():
                pending.append(stage)
                continue
            if stage == "source_topology_build" and inc:
                # DETECTION_ONLY_IS_DONE: hollow/stale topology — attempt heal then keep pending.
                if ctx.is_done(stage):
                    heal_or_refuse_mark(ctx, stage)
                pending.append(stage)
                continue
            from interview_mux.delivery_guardrails import seed_stage_complete

            if seed_stage_complete(ctx, stage):
                continue
            _keep_if_incomplete(stage)
            continue
        pending.append(stage)
    return pending


def _refuse_delivery_timeline_rewind(ctx: RunContext, stage: str, *, action: str) -> None:
    """Do not rewind the classified timeline after G0 when artifacts already exist."""
    if stage not in DELIVERY_LOCKED_TIMELINE_STAGES:
        return
    from interview_mux.homunculus.packer import g0_closed

    if not g0_closed(ctx):
        return
    if stage == "transcript_review_build":
        try:
            from interview_mux.stage_completion import stage_artifact_incompleteness

            if stage_artifact_incompleteness(ctx, stage):
                return
        except Exception:
            if not ctx.artifact_exists("transcript/review_queue.json"):
                return
        from interview_mux.delivery_guardrails import seed_stage_complete

        if not seed_stage_complete(ctx, stage):
            heal_or_refuse_mark(ctx, stage, force=True)
        raise RuntimeError(
            f"cannot {action} transcript_review_build: G0 is closed"
        )
    if stage == "segment_classification":
        _refuse_classified_manifest_rerun(ctx, stage, action=action)
        return
    needed = PROTECTED_CORE_STAGES.get(stage) or ()
    has_art = bool(needed) and all(ctx.artifact_exists(rel) for rel in needed)
    # Missing protected artifact → allow producer re-run even when classified
    # (e.g. content_brief archived by a heal; seed-order still needs content_context).
    if not has_art:
        return
    # A stage whose own output is still incomplete never finished, so running
    # it is not a rewind. content_context writes content_brief.json with empty
    # topics[].segment_ids and content_brief_reanchor fills them after G0; the
    # file existing used to refuse reanchor while the heal below refused the
    # mark, and framing_posture_decide waited on it until the identical cap.
    if not ctx.is_done(stage):
        try:
            from interview_mux.stage_completion import stage_artifact_incompleteness

            if stage_artifact_incompleteness(ctx, stage):
                return
        except Exception:
            pass
    from interview_mux.delivery_guardrails import seed_stage_complete

    if not seed_stage_complete(ctx, stage):
        heal_or_refuse_mark(ctx, stage, force=True)
    raise RuntimeError(
        f"cannot {action} {stage}: timeline artifacts exist after G0; "
        "pack existing facts and fill missing_framing / gap_framing_compose / "
        "delivery_brief_build instead of rewinding the classified tape"
    )


# Required analysis stages — skip only when the compensating artifact exists.
PROTECTED_CORE_STAGES: dict[str, tuple[str, ...]] = {
    "ingest": ("ingest/normalized.wav",),
    "transcribe": ("transcript/full.json",),
    "transcript_review_build": ("transcript/review_queue.json",),
    "speaker_roles": ("understanding/speakers.json",),
    "source_acoustic_profile": ("understanding/source_acoustic_profile.json",),
    "sonic_context_build": ("understanding/sonic_context.json",),
    "source_topology_build": (
        "understanding/source_topology.json",
        "understanding/flow_adaptation.json",
    ),
    "content_context": ("understanding/content_brief.json",),
    "talking_points_compose": ("understanding/talking_points.json",),
    "ideal_cuts_propose": ("understanding/ideal_cuts.json",),
    "ideal_cuts_materialize": ("understanding/ideal_cuts_materialized.json",),
    "boundary_detection": ("segments/boundaries.json",),
    "segment_classification": ("segments/manifest.json",),
    "content_brief_reanchor": ("understanding/content_brief.json",),
    "framing_posture_decide": ("understanding/framing_posture_decision.json",),
    "delivery_brief_build": ("understanding/delivery_brief.json",),
    "soundscape_policy_build": ("understanding/soundscape_policy.json",),
    "episode_structure_compose": ("understanding/episode_structure.json",),
    "chapter_close_hitch": ("mastering/chapter_close_hitch.json",),
    "full_master_ranking": ("master/selection.json",),
    "transitions": ("master/transitions.json",),
    "sound_design_plan": ("understanding/sound_design_plan.json",),
    "vo_synthesize": ("mastering/vo_synthesize.json",),
}

# Delivery producers — skip only when THIS stage wrote its output. Skip never marks done.
PROTECTED_DELIVERY_OUTPUTS: dict[str, tuple[str, ...]] = {
    "selection_order_sanitize": ("master/selection.json",),
    "gap_report_sanitize": ("understanding/gap_report.json",),
    # Air-contract authority is mastering_plan vo_seats; omit ledger mirrors only.
    "air_contract_sanitize": ("mastering/mastering_plan.json",),
    "edl_narrative_audit": ("master/edl_narrative_audit.json",),
    "edl": ("master/edl.json",),
    "assembly_preview": ("master/assembly_preview.wav",),
    "listen_delight_audit": ("mastering/listen_delight_audit.json",),
    "nugget_layup_compose": ("understanding/nugget_layup_plan.json",),
    "vo_line_adjudicate": ("understanding/vo_line_adjudication.json",),
    "music_palette_compose": ("sound_design/music_palette_compose.json",),
    "sfx_prompt_craft": ("sound_design/sfx_prompts.json",),
    "mmaudio_sfx": ("sound_design/mmaudio_qa.json",),
    "mix": ("master/assembly.wav",),
    # QA report is the stage authority — autopsy alone let heal remake
    # .stage_done while critical incomplete residuals stayed pending-only
    # (forensics exec_11130: false ship_path_ready → skip to finalize).
    "junction_snip_qa": (
        "master/junction_snip_qa.json",
        "master/seam_autopsy.json",
    ),
    "master_finalize": ("master/master.wav", "master/post_master_quality.json"),
    "master_transcript_build": ("master/transcript.json",),
    "episode_meta_build": ("publish/episode_meta.json",),
    "episode_cover_prompt_craft": ("publish/cover_prompt.json",),
    "podcast_encode_mp3": ("publish/audio.mp3",),
    # HPUB-2: cover generate is hollow without the jpg; publish remaining is
    # package_ready (ready:true), not chapters.json written mid-package.
    "episode_cover_generate": ("publish/cover.jpg",),
    "podcast_publish": ("publish/package_ready.json",),
}

MUSIC_SKIP_GUARD = frozenset(
    {"sfx_prompt_craft", "mmaudio_sfx", "music_palette_compose", "mix"}
)
# Alias of delivery_guardrails.MUSIC_REQUIRES_ASSEMBLY (Always-HAU dual-set DoD).
from interview_mux.delivery_guardrails import MUSIC_REQUIRES_ASSEMBLY  # noqa: E402

IDENTICAL_ERROR_REL = "mastering/homunculus/identical_stage_errors.json"
IDENTICAL_ERROR_CAP = 3
HOLLOW_SKIP_FP = "hollow_skip_blocked:{stage}:outputs_missing"


class HollowSkipBlockedError(RuntimeError):
    """10C: structured refuse when skip would hide a hollow .stage_done marker."""

    def __init__(self, payload: dict[str, Any]) -> None:
        self.payload = payload
        super().__init__(str(payload.get("reason") or "hollow_done"))


def _speaker_samples_present(ctx: RunContext) -> bool:
    """True when topology speaker_stats each have an on-disk sample WAV."""
    if not ctx.artifact_exists("understanding/source_topology.json"):
        return False
    try:
        topo = ctx.read_json("understanding/source_topology.json")
    except Exception:
        return False
    stats = topo.get("speaker_stats") if isinstance(topo, dict) else []
    if not isinstance(stats, list) or not stats:
        return True
    sample_dir = ctx.path("understanding", "speaker_samples")
    for row in stats:
        if not isinstance(row, dict):
            continue
        sid = str(row.get("speaker_id") or "")
        if sid and not (sample_dir / f"{sid}.wav").is_file():
            return False
    return True


def _refuse_topology_skip_without_samples(ctx: RunContext, stage: str) -> None:
    """2A: synthetic path needs topology + speaker samples before skip."""
    if stage != "source_topology_build":
        return
    try:
        from interview_mux.pipeline_mode import is_native_only

        if is_native_only(ctx):
            return
    except Exception:
        pass
    has_topo = ctx.artifact_exists("understanding/source_topology.json") and ctx.artifact_exists(
        "understanding/flow_adaptation.json"
    )
    if has_topo and _speaker_samples_present(ctx):
        return
    raise RuntimeError(
        "cannot skip source_topology_build: topology and speaker sample WAVs required "
        "when pipeline_mode is not native_only"
    )


def _block_hollow_skip(ctx: RunContext, stage: str) -> None:
    """10C: refuse skip when .stage_done lies — unmark once, fingerprint escalation."""
    # DETECTION_ONLY_IS_DONE: hollow stamp without outputs → unmark / escalate.
    if not ctx.is_done(stage) or stage_outputs_present(ctx, stage):
        return
    fingerprint = HOLLOW_SKIP_FP.format(stage=stage)
    hit = note_identical_stage_error(ctx, stage, fingerprint)
    from interview_mux.homunculus.budget import remaining as budget_remaining

    remaining_budget = budget_remaining(ctx, stage)
    count = int(hit.get("count") or 0)
    exhausted = bool(hit.get("exhausted"))
    if count <= 1:
        unmark_hollow_delivery_producers(ctx, {stage})
        # DETECTION_ONLY_IS_DONE: still stamped after hollow unmark → force clear.
        if ctx.is_done(stage):
            unmark_stage_only(ctx, stage)
    payload: dict[str, Any] = {
        "ok": False,
        "reason": "hollow_done",
        "stage": stage,
        "action": "unmark_and_rerun_once" if count <= 1 else "needs_operator",
        "remaining_rerun_budget": remaining_budget,
        "do_not": ["skip_stage", "invalidate_downstream"],
        "attempt": count,
        "exhausted": exhausted,
        "operator_card": (
            "Stage marked done but output incomplete — rerun required. "
            "Do not skip or invalidate downstream."
        ),
    }
    if count >= 2 or exhausted:
        try:
            from interview_mux.homunculus.issues import write_homunculus_plan

            plan = resolve_stage_plan(ctx, stage)
            write_homunculus_plan(
                ctx,
                last_target=stage,
                blockers=[f"hollow_done:{stage}"],
                attempted_heals=[f"hollow_skip_blocked x{count}"],
                recommended_next=str(plan.get("recommended_next") or stage),
                reason="hollow_skip_escalation",
            )
        except Exception:
            pass
        try:

            def _mark(meta: dict[str, Any]) -> None:
                from interview_mux.operator_gates import should_stamp_needs_operator

                reason = fingerprint[:240]
                if not should_stamp_needs_operator(stage, reason, meta=meta):
                    return
                meta["needs_operator"] = True
                meta["needs_operator_stage"] = stage
                meta["needs_operator_reason"] = reason

            ctx.mutate_run_meta(_mark)
        except Exception:
            pass
        payload["action"] = "needs_operator"
    append_ledger(
        ctx,
        {
            "kind": "hollow_skip_blocked",
            "identity": f"hollow_skip:{stage}",
            "stage": stage,
            "attempt": count,
            "exhausted": exhausted,
        },
    )
    raise HollowSkipBlockedError(payload)


def _refuse_music_before_assembly(ctx: RunContext, stage: str, *, action: str) -> None:
    """Theme/SFX generation admits only via HAU ``may_admit_music`` (not preview OR)."""
    if stage not in MUSIC_REQUIRES_ASSEMBLY:
        return
    try:
        from interview_mux.mix_junction_seat import may_admit_music, music_admit_block_reason

        if may_admit_music(ctx):
            return
        blocked = music_admit_block_reason(ctx) or "assembly_not_ready_for_music"
    except Exception:
        blocked = "assembly_not_ready_for_music"
    raise RuntimeError(
        f"cannot {action} {stage}: {blocked} — "
        "HAU requires seated assembly (or operator preview_music) before MusicGen/SFX"
    )


def _order_for(phase: str) -> list[str]:
    return list(ANALYSIS_ORDER if phase == "analysis" else DELIVERY_ORDER)


def earliest_incomplete_seed_stage(
    ctx: RunContext, phase: str, candidates: set[str]
) -> str | None:
    """First incomplete (or hollow-done) stage in seed order.

    Delivery walks the full seed order, not only ``candidates``. A hollow-done
    producer (e.g. air_script_compose) must still pin the conductor; otherwise
    nugget_corpus_mine is offered and seed-order raises.

    Analysis (RC8) likewise walks the full order — do not skip stages merely
    because they are absent from ``candidates``.
    """
    from interview_mux.delivery_guardrails import seed_stage_complete
    from interview_mux.stage_completion import stage_artifact_incompleteness

    for sid in _order_for(phase):
        if phase == "delivery":
            if seed_stage_complete(ctx, sid):
                continue
            # HAU speech-first: MusicGen/SFX wait until mix seats assembly
            # (exec_13170: constrain_conductor pinned mix → music_palette).
            try:
                from interview_mux.delivery_guardrails import MUSIC_BEFORE_MIX
                from interview_mux.mix_junction_seat import next_delivery_seat

                if sid in MUSIC_BEFORE_MIX and not seed_stage_complete(ctx, "mix"):
                    pin = next_delivery_seat(ctx)
                    if pin == "mix" and (not candidates or "mix" in candidates):
                        return "mix"
                    if pin and pin != sid:
                        continue
            except Exception:
                pass
            # Late-added spoken transition pairs after assembly must not yank the
            # conductor back to vo_synthesize — mix last-chance synths them.
            if sid == "vo_synthesize" and ctx.artifact_exists("master/assembly.wav"):
                try:
                    from interview_mux.delivery_guardrails import may_rewind_to_vo_synthesize

                    if not may_rewind_to_vo_synthesize(ctx):
                        try:
                            from interview_mux.delivery_guardrails import record_wasted_work

                            record_wasted_work(
                                ctx,
                                event="refuse_vo_synthesize_rewind",
                                stage="vo_synthesize",
                                detail={"reason": "monotonic_delivery"},
                            )
                        except Exception:
                            pass
                        continue
                except Exception:
                    pass
                try:
                    from interview_mux.gates import check_g1_vo

                    if check_g1_vo(ctx):
                        return sid
                except Exception:
                    return sid
                try:
                    inc = stage_artifact_incompleteness(ctx, sid)
                except Exception:
                    inc = None
                if inc and "transition pairs missing" in str(inc):
                    continue
            try:
                from interview_mux.hosted_vo_authority import seed_walk_pin_for_hollow_hosted_vo

                return seed_walk_pin_for_hollow_hosted_vo(ctx, sid)
            except Exception:
                return sid
        # Analysis: full seed-order walk (ignore candidates membership).
        from interview_mux.done_authority import may_skip_as_complete

        if may_skip_as_complete(ctx, sid):
            continue
        if stage_outputs_present(ctx, sid):
            try:
                if stage_artifact_incompleteness(ctx, sid) is None:
                    continue
            except Exception:
                pass
        try:
            from interview_mux.hosted_vo_authority import seed_walk_pin_for_hollow_hosted_vo

            return seed_walk_pin_for_hollow_hosted_vo(ctx, sid)
        except Exception:
            return sid
    return None


def constrain_conductor_to_seed_front(
    ctx: RunContext, phase: str, remaining: list[str]
) -> list[str]:
    """Conductor must not skip ahead of the earliest incomplete seed stage."""
    if not remaining:
        return remaining
    if phase == "delivery":
        try:
            from interview_mux.delivery_guardrails import filter_delivery_candidates

            remaining = filter_delivery_candidates(ctx, remaining)
        except Exception:
            pass
        if not remaining:
            return remaining
    front = earliest_incomplete_seed_stage(ctx, phase, set(remaining))
    if front:
        # Never pin EDL while narrative audit is missing or still fail.
        if phase == "delivery" and front == "edl":
            try:
                from interview_mux.edl_narrative_remutate import narrative_audit_blocks_edl

                audit_missing = not ctx.artifact_exists("master/edl_narrative_audit.json")
                if audit_missing or narrative_audit_blocks_edl(ctx):
                    front = "edl_narrative_audit"
            except Exception:
                pass
        # Never pin mix while live incomplete-cut criticals stand — mix refuses on
        # them and the recut owner is junction_snip_qa (exec_11871 deadlock).
        if phase == "delivery" and front == "mix":
            try:
                from interview_mux.junction_snip_qa import junction_recut_precedes_mix

                if junction_recut_precedes_mix(ctx):
                    front = "junction_snip_qa"
            except Exception:
                pass
        try:
            from interview_mux.hosted_vo_authority import seed_walk_pin_for_hollow_hosted_vo

            front = seed_walk_pin_for_hollow_hosted_vo(ctx, front)
        except Exception:
            pass
        if phase == "delivery" and remaining[0] != front:
            try:
                from interview_mux.delivery_guardrails import record_wasted_work

                record_wasted_work(
                    ctx,
                    event="seed_order_violation_attempt",
                    stage=remaining[0],
                    detail={"pinned": front, "requested": remaining[:6]},
                )
            except Exception:
                pass
        return [front]
    return remaining


def _read_agenda(ctx: RunContext) -> dict[str, Any]:
    if not ctx.artifact_exists(AGENDA_REL):
        return {"skipped": [], "remaining": [], "scheduled": [], "reruns": []}
    raw = ctx.read_json(AGENDA_REL)
    if not isinstance(raw, dict):
        return {"skipped": [], "remaining": [], "scheduled": [], "reruns": []}
    raw.setdefault("skipped", [])
    raw.setdefault("remaining", [])
    raw.setdefault("scheduled", [])
    raw.setdefault("reruns", [])
    return raw


def skipped_stages(ctx: RunContext) -> set[str]:
    return {str(s) for s in (_read_agenda(ctx).get("skipped") or [])}


def stage_required_outputs(stage: str) -> tuple[str, ...]:
    if stage in PROTECTED_DELIVERY_OUTPUTS:
        return PROTECTED_DELIVERY_OUTPUTS[stage]
    if stage in PROTECTED_CORE_STAGES:
        return PROTECTED_CORE_STAGES[stage]
    from interview_mux.prompt_validation import STAGE_ARTIFACT_DISK_PATHS

    rel = STAGE_ARTIFACT_DISK_PATHS.get(stage)
    return (rel,) if rel else ()


def _sdp_producer_stage(ctx: RunContext) -> str:
    if not ctx.artifact_exists("understanding/sound_design_plan.json"):
        return ""
    try:
        doc = ctx.read_json("understanding/sound_design_plan.json")
    except Exception:
        return ""
    if not isinstance(doc, dict):
        return ""
    meta = doc.get("_meta") if isinstance(doc.get("_meta"), dict) else {}
    return str(meta.get("producer_stage") or "")


def delivery_sdp_present(ctx: RunContext) -> bool:
    """True after SDP exists on disk with a paid writer and transitions exist.

    ``music_palette_compose`` is a paid land co-writer (theme cues). Exact-match
    ``sound_design_plan`` only would re-invoke the LLM and wipe landed cues
    (exec_002).
    """
    if not ctx.artifact_exists("master/transitions.json"):
        return False
    return _sdp_producer_stage(ctx) in {"sound_design_plan", "music_palette_compose"}


def _pre_ranking_rounds_present(ctx: RunContext) -> bool:
    """True only after the pre-ranking fuse pass wrote its own rounds doc.

    Analysis ``connector_fuse_rounds.json`` (and the first-pass audit) must not
    satisfy ``connector_fuse_pass_pre_ranking`` or remaining_stages drops the pass.

    H6-B: ``skip_reason=missing_manifest`` is not present (broken upstream).
    """
    if not ctx.artifact_exists(_PRE_RANKING_ROUNDS):
        return False
    try:
        doc = ctx.read_json(_PRE_RANKING_ROUNDS)
    except Exception:
        return False
    if not isinstance(doc, dict):
        return False
    if str(doc.get("pass_id") or "") != "pre_ranking":
        return False
    if str(doc.get("skip_reason") or "") == "missing_manifest":
        return False
    return True


def _final_mtime(ctx: RunContext, *parts: str) -> float | None:
    path = ctx.final_path(*parts)
    if not path.is_file():
        return None
    try:
        return path.stat().st_mtime
    except OSError:
        return None


def assembly_stale_versus_edl(ctx: RunContext) -> bool:
    """True when assembly is not the mix of the live AirOrder generation."""
    try:
        from interview_mux.air_order import mix_stale_versus_live

        return mix_stale_versus_live(ctx)
    except Exception:
        pass
    edl_m = _final_mtime(ctx, "master", "edl.json")
    asm_m = _final_mtime(ctx, "master", "assembly.wav")
    if edl_m is None or asm_m is None:
        return False
    return edl_m > asm_m + 1.0


def _producer_older_than_assembly(ctx: RunContext, *parts: str) -> bool:
    asm_m = _final_mtime(ctx, "master", "assembly.wav")
    other_m = _final_mtime(ctx, *parts)
    if asm_m is None or other_m is None:
        return False
    return asm_m > other_m + 1.0


def _junction_commitment_matches_assembly(ctx: RunContext) -> bool:
    """True when seam_autopsy commitment still describes the live assembly.wav.

    Bare mtime skew (assembly touched after autopsy rewrite of identical
    content) must not look like missing junction outputs — that was the
    exec_5404 master_finalize ↔ junction seed thrash.

    Requires size match and, when present, sha match (aligned with
    ``verify_commitment`` fingerprints).
    """
    if not ctx.artifact_exists("master/seam_autopsy.json"):
        return False
    try:
        autopsy = ctx.read_json("master/seam_autopsy.json")
    except Exception:
        return False
    if not isinstance(autopsy, dict):
        return False
    commit = autopsy.get("commitment")
    if not isinstance(commit, dict):
        return False
    if str(commit.get("status") or "") != "committed":
        return False
    asm_info = commit.get("assembly") if isinstance(commit.get("assembly"), dict) else {}
    asm_path = ctx.final_path("master", "assembly.wav")
    if not asm_path.is_file():
        return False
    try:
        live_size = int(asm_path.stat().st_size)
    except OSError:
        return False
    claimed = int(asm_info.get("size") or 0)
    if claimed <= 0 or claimed != live_size:
        return False
    claimed_sha = str(
        asm_info.get("sha256")
        or asm_info.get("sha")
        or asm_info.get("fingerprint")
        or ""
    ).strip()
    if claimed_sha:
        try:
            from interview_mux.seam_autopsy import _file_fingerprint

            live_fp = _file_fingerprint(asm_path)
            live_sha = ""
            if isinstance(live_fp, dict):
                live_sha = str(
                    live_fp.get("sha256") or live_fp.get("sha") or live_fp.get("fingerprint") or ""
                ).strip()
            elif isinstance(live_fp, str):
                live_sha = live_fp.strip()
            if live_sha and live_sha != claimed_sha:
                return False
        except Exception:
            pass
    # ENDD-7: fingerprint match alone is not enough — mix must be seated vs live EDL.
    try:
        from interview_mux.air_order import mix_outputs_seated

        if not mix_outputs_seated(ctx):
            return False
    except Exception:
        return False
    return True


def _package_ready_true(ctx: RunContext) -> bool:
    """HPUB-2: podcast_publish outputs present when ready:true or honest skip.

    Clinic B1: ``ready:false`` + ``skipped:true`` counts as present so Skip
    seed-completes without a local package.
    """
    if not ctx.artifact_exists("publish/package_ready.json"):
        return False
    try:
        doc = ctx.read_json("publish/package_ready.json")
    except Exception:
        return False
    if not isinstance(doc, dict):
        return False
    if doc.get("ready") is True:
        return True
    return doc.get("skipped") is True


def stage_outputs_present(ctx: RunContext, stage: str) -> bool:
    if stage == "audio_preclean":
        # Skip is a finished outcome (HP-3). Isolated wav / provider / lineage
        # still count. Do not demand isolated.wav after operator dismiss.
        return prepare_outputs_present(ctx, stage)
    if stage == "gap_framing_recompose":
        return ctx.artifact_exists(
            "understanding/gap_framing_recompose.json"
        ) or ctx.artifact_exists("understanding/refinement_skip_copy.json")
    if stage == "low_conf_island_scan":
        return ctx.artifact_exists("analysis/low_conf_islands.json") or ctx.artifact_exists(
            "analysis/low_conf_must_keep.json"
        )
    if stage == "connector_fuse_pass":
        return ctx.artifact_exists("analysis/connector_fuse_audit.json")
    if stage == "connector_fuse_pass_pre_ranking":
        return _pre_ranking_rounds_present(ctx)
    if stage == "sound_design_plan":
        return delivery_sdp_present(ctx)
    if stage == "vo_synthesize":
        if not ctx.artifact_exists("mastering/vo_synthesize.json"):
            return False
        if not ctx.artifact_exists("master/transitions.json"):
            return False
        try:
            from interview_mux.transition_vo import current_transition_pairs_missing

            return not current_transition_pairs_missing(ctx)
        except Exception:
            return False
    if stage in MUSIC_REQUIRES_ASSEMBLY:
        try:
            from interview_mux.mix_junction_seat import may_admit_music

            if not may_admit_music(ctx):
                return False
        except Exception:
            return False
        rels = stage_required_outputs(stage)
        if not (bool(rels) and all(ctx.artifact_exists(rel) for rel in rels)):
            return False
        if stage == "mmaudio_sfx":
            try:
                from interview_mux.artifact_completeness import artifact_status

                if artifact_status("sound_design/mmaudio_qa.json", ctx) != "complete":
                    return False
            except Exception:
                return False
            try:
                from interview_mux.sdp_cross_validate import missing_sdp_asset_wavs

                if missing_sdp_asset_wavs(ctx):
                    return False
            except Exception:
                return False
            return True
        return True
    if stage == "edl":
        if not ctx.artifact_exists("master/edl.json"):
            return False
        try:
            from interview_mux.order_hash import order_drift_heal_action

            sel = (
                ctx.read_json("master/selection.json")
                if ctx.artifact_exists("master/selection.json")
                else None
            )
            edl_doc = ctx.read_json("master/edl.json")
            if (
                order_drift_heal_action(
                    sel if isinstance(sel, dict) else None,
                    edl_doc if isinstance(edl_doc, dict) else None,
                )
                not in {"ok", "stamp"}
            ):
                return False
            from interview_mux.order_hash import edl_speech_clip_ids, seatable_selection_ids

            clip_ids = edl_speech_clip_ids(edl_doc if isinstance(edl_doc, dict) else {})
            seated = seatable_selection_ids(
                sel if isinstance(sel, dict) else None,
                edl_doc if isinstance(edl_doc, dict) else None,
                use_lock=True,
            )
            if clip_ids and seated and clip_ids != seated:
                return False
        except Exception:
            return True
        return True
    if stage == "mix":
        try:
            from interview_mux.air_order import mix_outputs_seated

            return bool(mix_outputs_seated(ctx))
        except Exception:
            if assembly_stale_versus_edl(ctx):
                return False
            needed = stage_required_outputs(stage)
            return bool(needed) and all(ctx.artifact_exists(rel) for rel in needed)
    if stage == "junction_snip_qa":
        # exec_5404: committed autopsy that still matches live assembly size must
        # not look hollow solely because mix_stale_versus_live / mtime skew.
        # End-D: autopsy/QA files alone are never seed-complete without commitment.
        if _junction_commitment_matches_assembly(ctx):
            needed = stage_required_outputs(stage)
            return bool(needed) and all(ctx.artifact_exists(rel) for rel in needed)
        return False
    if stage == "master_finalize" and (
        assembly_stale_versus_edl(ctx)
        or _producer_older_than_assembly(ctx, "master", "master.wav")
    ):
        return False
    if stage == "nugget_layup_compose":
        # Plan on disk is the producer output. Freshness/hash drift must not
        # look like a missing artifact — that unmarked compose on EDL resume
        # and rewrote G1 after a selection-order heal.
        rels = stage_required_outputs(stage)
        return bool(rels) and all(ctx.artifact_exists(rel) for rel in rels)
    if stage == "source_acoustic_profile":
        try:
            from interview_mux.stage_completion import stage_artifact_incompleteness

            return stage_artifact_incompleteness(ctx, stage) is None
        except Exception:
            return ctx.artifact_exists("understanding/source_acoustic_profile.json")
    if stage == "sonic_context_build":
        try:
            from interview_mux.stage_completion import stage_artifact_incompleteness

            return stage_artifact_incompleteness(ctx, stage) is None
        except Exception:
            return ctx.artifact_exists("understanding/sonic_context.json")
    if stage in {
        "delivery_brief_build",
        "soundscape_policy_build",
        "episode_structure_compose",
    }:
        try:
            from interview_mux.stage_completion import stage_artifact_incompleteness

            return stage_artifact_incompleteness(ctx, stage) is None
        except Exception:
            rels = stage_required_outputs(stage)
            return bool(rels) and all(ctx.artifact_exists(rel) for rel in rels)
    if stage == "missing_framing":
        try:
            from interview_mux.stage_completion import stage_artifact_incompleteness

            return stage_artifact_incompleteness(ctx, stage) is None
        except Exception:
            return ctx.artifact_exists("understanding/gap_evaluations.json")
    if stage == "optimal_questions":
        # Alias of gap_framing_compose (same disk primary). Hollow-refuse must not
        # treat the alias as outputs_missing when compose artifacts are present.
        return stage_outputs_present(ctx, "gap_framing_compose")
    if stage == "vernacular_segment_sanitize":
        try:
            from interview_mux.stage_completion import stage_artifact_incompleteness

            return stage_artifact_incompleteness(ctx, stage) is None
        except Exception:
            return ctx.artifact_exists("vernacular/resplit_report.json")
    if stage in {
        "mastering_research_routing",
        "mastering_research_waves",
        "mastering_research_rollup",
        "mastering_shape_agenda",
        "mastering_shape_candidates",
    }:
        try:
            from interview_mux.stage_completion import stage_artifact_incompleteness

            return stage_artifact_incompleteness(ctx, stage) is None
        except Exception:
            rels = stage_required_outputs(stage)
            return bool(rels) and all(ctx.artifact_exists(rel) for rel in rels)
    if stage == "transcript_review_build":
        try:
            from interview_mux.stage_completion import stage_artifact_incompleteness

            return stage_artifact_incompleteness(ctx, stage) is None
        except Exception:
            return ctx.artifact_exists("transcript/review_queue.json")
    if stage == "transcript_review":
        # Gate stage: sign-off stamps .stage_done after materialize. Empty
        # STAGE_ARTIFACT_DISK_PATHS used to make stage_outputs_present always
        # False → authority_denied:mark_done:hollow (exec_11871 G0 lie).
        return (
            ctx.artifact_exists("transcript/review_queue.json")
            and ctx.artifact_exists("transcript/corrections.json")
            and ctx.artifact_exists("transcript/full.json")
        )
    if stage == "episode_cover_generate":
        return ctx.artifact_exists("publish/cover.jpg")
    if stage == "podcast_publish":
        return _package_ready_true(ctx)
    # 0G: present≠sanitary for shared-path producers (file alone can be hollow).
    if stage in {
        "air_script_compose",
        "selection_order_sanitize",
        "gap_report_sanitize",
    }:
        try:
            from interview_mux.stage_completion import stage_artifact_incompleteness

            return stage_artifact_incompleteness(ctx, stage) is None
        except Exception:
            # Sanitize-refused / schema blow-ups ⇒ not seed-complete.
            return False
    needed = stage_required_outputs(stage)
    if not needed:
        # Ownership constitution: empty required outputs must NOT fall back to
        # is_done (circular after hollow stamp). Pipeline stages without a
        # registered primary are treated as absent until cataloged.
        try:
            from interview_mux.v2.config import ANALYSIS_ORDER, DELIVERY_ORDER

            if stage in ANALYSIS_ORDER or stage in DELIVERY_ORDER:
                return False
        except Exception:
            return False
        return False
    return all(ctx.artifact_exists(rel) for rel in needed)


def unmark_hollow_delivery_producers(
    ctx: RunContext, stages: list[str] | set[str] | None = None
) -> list[str]:
    """Clear .stage_done when the producer output is missing or from another stage."""
    if stages is None:
        want = set(PROTECTED_CORE_STAGES) | set(PROTECTED_DELIVERY_OUTPUTS) | set(
            PROTECTED_ISLAND_STAGES
        )
    else:
        want = {str(s) for s in stages}
    cleared: list[str] = []
    for stage in sorted(want):
        if stage == "mmaudio_sfx" and not stage_outputs_present(ctx, stage):
            # Empty QA with theme WAVs already on disk is a label hole, not a
            # missing producer — rebuild QA instead of regenerating MusicGen.
            try:
                from interview_mux.mmaudio_asset_qa import heal_mmaudio_qa_wav_parity

                heal_mmaudio_qa_wav_parity(ctx)
            except Exception:
                pass
            from interview_mux.delivery_guardrails import seed_stage_complete

            if stage_outputs_present(ctx, stage) and not seed_stage_complete(ctx, stage):
                heal_or_refuse_mark(ctx, stage, force=True)
        # DETECTION_ONLY_IS_DONE: hollow stamp census for unmark (never advance/skip).
        hollow_missing = ctx.is_done(stage) and not stage_outputs_present(ctx, stage)
        hollow_incomplete = False
        if ctx.is_done(stage) and not hollow_missing:
            try:
                from interview_mux.stage_completion import stage_artifact_incompleteness

                hollow_incomplete = stage_artifact_incompleteness(ctx, stage) is not None
            except Exception:
                hollow_incomplete = False
        if hollow_missing or hollow_incomplete:
            try:
                from interview_mux.delivery_guardrails import music_clear_blocked

                if music_clear_blocked(ctx, stage, source="hollow_unmark"):
                    ctx.log(
                        f"homunculus refuse hollow unmark {stage} — music epoch sealed "
                        "(heal ≠ waive; call break_music_epoch_seal)",
                        level="warning",
                        stage=stage,
                    )
                    continue
            except Exception:
                pass
            unmark_stage_only(ctx, stage)
            cleared.append(stage)
    if cleared:
        ctx.log(
            "homunculus unmarked hollow delivery producers: " + ", ".join(cleared),
            level="warning",
            stage=cleared[0],
        )
    return cleared


def unskip_hollow_stages(ctx: RunContext, stages: list[str] | set[str]) -> list[str]:
    """Drop skip entries that never produced their output — skip without artifact is a hole."""
    want = {str(s) for s in stages}
    unmark_hollow_delivery_producers(ctx, want)
    doc = _read_agenda(ctx)
    skipped = [str(s) for s in (doc.get("skipped") or [])]
    keep: list[str] = []
    dropped: list[str] = []
    for sid in skipped:
        hollow = sid in want and not stage_outputs_present(ctx, sid)
        if hollow:
            dropped.append(sid)
        else:
            keep.append(sid)
    if not dropped:
        return []
    doc["skipped"] = keep
    ctx.write_json(AGENDA_REL, doc)
    ctx.log(
        "homunculus unskipped hollow stages (missing outputs): " + ", ".join(dropped),
        level="warning",
        stage=dropped[0],
    )
    append_ledger(
        ctx,
        {
            "kind": "unskip_hollow",
            "identity": "unskip_hollow",
            "stages": dropped[:40],
        },
    )
    return dropped


def prepare_delivery_guardrails(ctx: RunContext, stages: list[str] | set[str] | None = None) -> list[str]:
    """Unmark/unskip hollow producers so resume cannot fake progress. Returns hole ids."""
    want = set(stages) if stages is not None else set(_order_for("delivery"))
    holes = unmark_hollow_prepare_stages(ctx)
    holes.extend(unmark_hollow_delivery_producers(ctx, want))
    holes.extend(unskip_hollow_stages(ctx, want))
    try:
        from interview_mux.delivery_guardrails import reconcile_delivery_batch, seal_phase_a_if_stable

        holes.extend(reconcile_delivery_batch(ctx))
        seal_phase_a_if_stable(ctx)
    except Exception:
        pass
    return list(dict.fromkeys(holes))


def note_identical_stage_error(ctx: RunContext, stage: str, fingerprint: str) -> dict[str, Any]:
    """Cap identical prestage/input errors via operator/identical_failures.json."""
    from interview_mux.identical_failures import record_identical_failure

    row = record_identical_failure(
        ctx,
        failed_stage=stage,
        producer="homunculus_agenda",
        reason=fingerprint[:400],
    )
    # Keep legacy mirror for older readers.
    doc: dict[str, Any] = {}
    if ctx.artifact_exists(IDENTICAL_ERROR_REL):
        raw = ctx.read_json(IDENTICAL_ERROR_REL)
        if isinstance(raw, dict):
            doc = raw
    key = f"{stage}:{fingerprint[:160]}"
    doc[key] = {
        "stage": stage,
        "fingerprint": fingerprint[:160],
        "count": int(row.get("count") or 0),
    }
    ctx.write_json(IDENTICAL_ERROR_REL, doc, skip_handoff=True)
    exhausted = bool(row.get("halt"))
    try:
        from interview_mux.thrash_hardening import note_authority_undo_attempt

        undo = note_authority_undo_attempt(
            ctx,
            artifact=f"stage:{stage}",
            action_class="identical_stage_error",
            content_hash=str(fingerprint or "")[:64],
        )
        if undo.get("halt"):
            exhausted = True
            fingerprint = (
                f"authority_undo_thrash:{stage}: "
                + str(undo.get("reason") or fingerprint)
            )
    except Exception:
        pass
    if exhausted:
        # Declared fallback (ISSUES 124): keep the old version when there is
        # one, record the decision, and carry on instead of halting.
        try:
            from interview_mux.fallback_backstop import apply_declared_fallback

            if apply_declared_fallback(ctx, stage, fingerprint):
                exhausted = False
        except Exception:
            pass
    if exhausted:
        def _mark(meta: dict[str, Any]) -> None:
            meta["needs_operator"] = True
            meta["needs_operator_stage"] = stage
            meta["needs_operator_reason"] = fingerprint[:240]

        try:
            ctx.mutate_run_meta(_mark)
        except Exception:
            pass
        try:
            from interview_mux.homunculus.issues import write_homunculus_plan

            plan = resolve_stage_plan(ctx, stage)
            write_homunculus_plan(
                ctx,
                last_target=stage,
                blockers=[f"identical_error:{fingerprint[:120]}"],
                attempted_heals=[f"identical_stage_error x{int(row.get('count') or 0)}"],
                recommended_next=str(plan.get("recommended_next") or stage),
                reason="identical_error_exhausted",
            )
        except Exception:
            pass
    return {
        "count": int(row.get("count") or 0),
        "exhausted": exhausted,
        "stage": stage,
    }


def _truncate_analysis_while_g0_open(ctx: RunContext, stages: list[str]) -> list[str]:
    """HP-4 2A: while the review queue exists unsigned, do not walk past G0."""
    from interview_mux.gates import g0_blocks_analysis

    if not g0_blocks_analysis(ctx):
        return list(stages)
    order = {sid: idx for idx, sid in enumerate(ANALYSIS_ORDER + DELIVERY_ORDER)}
    g0_idx = order.get("transcript_review_build")
    if g0_idx is None:
        return list(stages)
    return [s for s in stages if order.get(s, 10**9) <= g0_idx]


def _drop_delivery_while_voice_ref_open(ctx: RunContext, stages: list[str]) -> list[str]:
    """HG-4 2A: while G-VoiceRef is open, do not walk into delivery topic coverage."""
    try:
        from interview_mux.gap_vo_gates import check_voice_reference_pending

        if not check_voice_reference_pending(ctx):
            return list(stages)
    except Exception:
        return list(stages)
    return [s for s in stages if s not in DELIVERY_ORDER]


def remaining_stages(ctx: RunContext, phase: str) -> list[str]:
    # Analysis remainder is .stage_done, except island/fuse (HS-1) which drop
    # when outputs exist. Delivery remainder is seated outputs,
    # plus G1 incompleteness so hollow VO / music cannot drop off the agenda.
    if phase != "delivery":
        rem: list[str] = []
        from interview_mux.done_authority import may_skip_as_complete

        for sid in _order_for(phase):
            if may_skip_as_complete(ctx, sid):
                continue
            # HS-1: island/fuse outputs complete the analysis seat even without
            # a done marker (wrappers historically skipped heal-mark).
            if sid in PROTECTED_ISLAND_STAGES and stage_outputs_present(ctx, sid):
                continue
            rem.append(sid)
        return _truncate_analysis_while_g0_open(ctx, rem)
    from interview_mux.stage_completion import stage_artifact_incompleteness

    remutate_force: set[str] = set()
    try:
        from interview_mux.delivery_invariants import active_remutate_stages
        from interview_mux.done_authority import land_honest

        # Cleared markers with leftover artifacts must still walk (exec_10066:
        # remutate dropped air/transitions/edl off remaining → mix seed thrash).
        # Hollow stamps are not land-honest → stay forced onto the agenda.
        remutate_force = {
            sid
            for sid in active_remutate_stages(ctx)
            if not land_honest(ctx, sid)
        }
    except Exception:
        remutate_force = set()

    out: list[str] = []
    committed_master = False
    try:
        from interview_mux.done_authority import honest_finalize_seeded

        committed_master = bool(honest_finalize_seeded(ctx))
    except Exception:
        committed_master = False
    for sid in _order_for(phase):
        if sid in remutate_force:
            out.append(sid)
            continue
        # Post-master: do not resurface pre-master holes in remaining_after.
        if committed_master and sid not in SHIP_AFTER_MASTER:
            continue
        # Pre-master remaining “done” requires seed_stage_complete (O13).
        try:
            from interview_mux.delivery_guardrails import seed_stage_complete

            if seed_stage_complete(ctx, sid):
                continue
        except Exception:
            if stage_outputs_present(ctx, sid):
                if stage_artifact_incompleteness(ctx, sid) is None:
                    continue
        out.append(sid)
    return _drop_delivery_while_voice_ref_open(ctx, out)


def ship_stage_output_stale(ctx: RunContext, stage: str) -> bool:
    """True when a ship stage's outputs predate ``master/master.wav``.

    Ship outputs are cut from the master; ones older than it belong to an
    earlier master and must be made again (ISSUES 112).
    """
    try:
        master_mtime = ctx.final_path("master", "master.wav").stat().st_mtime
    except OSError:
        return False
    for rel in stage_required_outputs(stage):
        try:
            if ctx.final_path(*str(rel).split("/")).stat().st_mtime < master_mtime:
                return True
        except OSError:
            return True
    return False


def ship_after_master_remaining(ctx: RunContext) -> list[str]:
    """Cover / encode / publish stages still missing after master.wav exists.

    Present but older than the master counts as missing (ISSUES 112).
    """
    return [
        s
        for s in SHIP_AFTER_MASTER
        if not stage_outputs_present(ctx, s) or ship_stage_output_stale(ctx, s)
    ]


def backfill_ship_holes_after_master(ctx: RunContext) -> list[str]:
    """Re-mark ship stages whose outputs are present and current but unmarked.

    A re-entry clears the markers after its stage; the ship outputs stay on
    disk and, when the master was not rebuilt, are still this master's. The
    walk keys ship stages on their outputs, so it never ran them again, and
    the missing markers left the run unable to complete (ISSUES 112). Mirrors
    ``backfill_delivery_holes_after_master``: outputs first, never hollow.
    """
    if not ctx.final_path("master", "master.wav").is_file():
        return []
    # DETECTION_ONLY_IS_DONE: hole-fill mode after a shipped master, as in
    # backfill_delivery_holes_after_master; outputs decide, markers only select.
    if not ctx.is_done("master_finalize"):
        return []
    from interview_mux.done_authority import raw_stamp_session, try_mark_done

    filled: list[str] = []
    for stage in SHIP_AFTER_MASTER:
        if ctx.is_done(stage):
            continue
        if not stage_outputs_present(ctx, stage) or ship_stage_output_stale(ctx, stage):
            continue
        stamped = try_mark_done(ctx, stage, force=True)
        if not stamped:
            try:
                with raw_stamp_session(ctx, "post_master_backfill"):
                    stamped = try_mark_done(ctx, stage, force=True)
            except Exception:
                stamped = False
        if not stamped:
            continue
        filled.append(stage)
        ctx.log(
            f"homunculus backfilled ship hole {stage} (outputs current for this master)",
            level="warning",
            stage=stage,
        )
    return filled


def backfill_delivery_holes_after_master(ctx: RunContext) -> list[str]:
    """Mark unmarked pre-master delivery holes once master_finalize has already shipped.

    Hollow-pass B+ R3: never hollow-stamp — ``may_post_master_backfill`` requires
    outputs present; stamp via ``try_mark_done`` (raw only if needed after outputs).

    vo_synthesize was inserted between edl_narrative_audit and edl. Runs that
    already mixed/finalized must not rewind; persist a pair-gap report from
    on-disk transition WAVs and close the marker when outputs exist.
    """
    if not ctx.final_path("master", "master.wav").is_file():
        return []
    # DETECTION_ONLY_IS_DONE: post-master hole-fill mode — master.wav shipped and
    # finalize was claimed. Walk/advance still uses seed_stage_complete below.
    if not ctx.is_done("master_finalize"):
        return []
    from interview_mux.done_authority import (
        may_post_master_backfill,
        raw_stamp_session,
        try_mark_done,
    )

    filled: list[str] = []
    for stage in DELIVERY_ORDER:
        if stage in SHIP_AFTER_MASTER:
            break
        from interview_mux.delivery_guardrails import seed_stage_complete

        if seed_stage_complete(ctx, stage):
            continue
        if stage == "vo_synthesize":
            try:
                from interview_mux.transition_vo import (
                    current_transition_pairs_missing,
                    persist_vo_pair_gap,
                )

                missing = current_transition_pairs_missing(ctx)
                persist_vo_pair_gap(
                    ctx,
                    missing,
                    source="post_master_hole_backfill",
                    extra={"backfilled": True},
                    skip_handoff=True,
                    stage_key="vo_synthesize",
                )
            except Exception:
                ctx.write_json(
                    "mastering/vo_synthesize.json",
                    {
                        "still_missing_pairs": [],
                        "last_source": "post_master_hole_backfill",
                    },
                    skip_handoff=True,
                    stage_key="vo_synthesize",
                )
        if not may_post_master_backfill(ctx, stage):
            ctx.log(
                f"homunculus skip backfill {stage}: outputs missing (hollow_pass R3)",
                level="warning",
                stage=stage,
            )
            continue
        stamped = try_mark_done(ctx, stage, force=True)
        if not stamped:
            try:
                with raw_stamp_session(ctx, "post_master_backfill"):
                    stamped = try_mark_done(ctx, stage, force=True)
            except Exception:
                stamped = False
        if not stamped:
            continue
        filled.append(stage)
        ctx.log(
            f"homunculus backfilled pre-master hole {stage} (master already exists)",
            level="warning",
            stage=stage,
        )
    return filled


def write_agenda(ctx: RunContext, phase: str, remaining: list[str], *, source: str) -> dict[str, Any]:
    prev = _read_agenda(ctx)
    doc = {
        "phase": phase,
        "remaining": list(remaining),
        "source": source,
        "seed_order": _order_for(phase),
        "skipped": list(prev.get("skipped") or []),
        "scheduled": list(prev.get("scheduled") or []),
        "reruns": list(prev.get("reruns") or []),
    }
    ctx.write_json(AGENDA_REL, doc)
    append_ledger(
        ctx,
        {
            "kind": "agenda",
            "identity": "agenda",
            "phase": phase,
            "source": source,
            "remaining": remaining[:40],
        },
    )
    return doc


def skip_stage(ctx: RunContext, stage: str, *, reason: str, compensating_fact: str | None = None) -> dict[str, Any]:
    if compensating_fact and not ctx.artifact_exists(compensating_fact):
        # Fact IDs are not compensating artifacts. A skip without the on-disk
        # output is a hole (exec_087 skipped transitions with fact 67b9d444).
        compensating_fact = None
    _refuse_music_before_assembly(ctx, stage, action="skip")
    _block_hollow_skip(ctx, stage)
    _refuse_topology_skip_without_samples(ctx, stage)
    if stage == "chapter_close_hitch":
        from interview_mux.chapter_close_hitch import hitch_latch_committed

        if not hitch_latch_committed(ctx):
            raise RuntimeError(
                "cannot skip chapter_close_hitch until the one-shot latch is committed"
            )
    if stage in MUSIC_SKIP_GUARD:
        from interview_mux.sdp_cross_validate import missing_sdp_asset_wavs

        if not delivery_sdp_present(ctx):
            raise RuntimeError(
                f"cannot skip {stage}: sound_design_plan has not written the delivery SDP"
            )
        missing_theme = missing_sdp_asset_wavs(ctx)
        if missing_theme:
            raise RuntimeError(
                f"cannot skip {stage}: missing WAV for asset_id "
                + ", ".join(missing_theme[:6])
                + " — run music_palette_compose → sfx_prompt_craft → mmaudio_sfx"
            )
    if stage == "vo_synthesize":
        from interview_mux.transition_vo import current_transition_pairs_missing

        if not ctx.artifact_exists("master/transitions.json"):
            raise RuntimeError(
                "cannot skip vo_synthesize: master/transitions.json missing"
            )
        missing = current_transition_pairs_missing(ctx)
        if missing:
            raise RuntimeError(
                "cannot skip vo_synthesize: current transition pairs missing WAV: "
                + ", ".join(missing[:6])
            )
    if stage in PROTECTED_ISLAND_STAGES:
        if not stage_outputs_present(ctx, stage):
            raise RuntimeError(
                f"cannot skip {stage}: language-island artifacts missing "
                "(low_conf_island_scan / connector_fuse_pass / "
                "connector_fuse_pass_pre_ranking required)"
            )
    protected = (
        stage in PROTECTED_CORE_STAGES
        or stage in PROTECTED_DELIVERY_OUTPUTS
        or stage in PROTECTED_ISLAND_STAGES
        or bool(stage_required_outputs(stage))
    )
    if protected and not stage_outputs_present(ctx, stage):
        needed = stage_required_outputs(stage)
        raise RuntimeError(
            f"cannot skip {stage}: required artifact missing "
            f"({', '.join(needed) if needed else stage})"
        )
    # Skip is an agenda note only — never mark_done. Walk still runs unless
    # stage_outputs_present is true for this producer.
    doc = _read_agenda(ctx)
    skipped = [str(s) for s in (doc.get("skipped") or [])]
    if stage not in skipped:
        skipped.append(stage)
    doc["skipped"] = skipped
    if compensating_fact:
        doc.setdefault("skip_reasons", {})
        if isinstance(doc["skip_reasons"], dict):
            doc["skip_reasons"][stage] = {"reason": reason, "compensating_fact": compensating_fact}
    ctx.write_json(AGENDA_REL, doc)
    append_ledger(
        ctx,
        {
            "kind": "skip_stage",
            "identity": f"skip:{stage}",
            "stage": stage,
            "reason": reason,
            "compensating_fact": compensating_fact,
        },
    )
    return doc


def schedule_stage(ctx: RunContext, stage: str, *, before: str | None = None) -> dict[str, Any]:
    doc = _read_agenda(ctx)
    phase = str(doc.get("phase") or "analysis")
    order = list(doc.get("scheduled") or []) or remaining_stages(ctx, phase)
    if stage in order:
        order.remove(stage)
    if before and before in order:
        order.insert(order.index(before), stage)
    else:
        order.insert(0, stage)
    doc["scheduled"] = order
    # DETECTION_ONLY_IS_DONE: schedule remaining list for operator display;
    # walk/advance uses remaining_stages / may_skip_as_complete.
    doc["remaining"] = [s for s in order if not ctx.is_done(s) and s not in skipped_stages(ctx)]
    ctx.write_json(AGENDA_REL, doc)
    append_ledger(
        ctx,
        {"kind": "schedule_stage", "identity": "schedule_stage", "stage": stage, "order": order[:40]},
    )
    return doc


def unmark_stage_only(ctx: RunContext, stage: str) -> int:
    """Archive and remove this stage's done marker. Does not clear downstream."""
    marker = ctx.final_path(".stage_done", stage)
    if not marker.is_file():
        return 0
    doc = _read_agenda(ctx)
    seq = len(list(doc.get("reruns") or [])) + 1
    dest = ctx.path(f"mastering/homunculus/reruns/{seq}")
    dest.mkdir(parents=True, exist_ok=True)
    shutil.copy2(marker, dest / stage)
    marker.unlink()
    reruns = list(doc.get("reruns") or [])
    reruns.append({"seq": seq, "stage": stage})
    doc["reruns"] = reruns
    ctx.write_json(AGENDA_REL, doc)
    return seq


def rerun_stage(
    ctx: RunContext,
    stage: str,
    *,
    extra_fact_ids: list[str] | None = None,
    overlay_rel: str | None = None,
) -> dict[str, Any]:
    _refuse_g0_locked_rerun(ctx, stage, action="rerun")
    _refuse_classified_manifest_rerun(ctx, stage, action="rerun")
    _refuse_delivery_timeline_rewind(ctx, stage, action="rerun")
    _refuse_music_before_assembly(ctx, stage, action="rerun")
    seq = unmark_stage_only(ctx, stage)
    if extra_fact_ids:
        from interview_mux.homunculus.packer import pack_volley

        pack_volley(ctx, fact_ids=list(extra_fact_ids), tool_id=stage)
    append_ledger(
        ctx,
        {
            "kind": "rerun_stage",
            "identity": "rerun_stage",
            "stage": stage,
            "seq": seq,
            "overlay_rel": overlay_rel,
            "extra_fact_ids": list(extra_fact_ids or []),
        },
    )
    from interview_mux.pipeline import run_single_stage

    setattr(ctx, "_homunculus_inner_stage", True)
    try:
        from interview_mux.homunculus.runtime import dispatch_stage

        dispatch_stage(ctx, stage, lambda sid: run_single_stage(ctx, sid), source="rerun")
    finally:
        if hasattr(ctx, "_homunculus_inner_stage"):
            delattr(ctx, "_homunculus_inner_stage")
    return {"ok": True, "stage": stage, "seq": seq}


def heal_air_order_integrity(ctx: RunContext) -> dict[str, Any]:
    """Deterministic repair for reverse tape jumps and late opening clusters."""
    from interview_mux.stages.selection import finalize_selection_order

    if not ctx.artifact_exists("master/selection.json"):
        return {"ok": False, "error": "no_selection"}
    sel = ctx.read_json("master/selection.json")
    if not isinstance(sel, dict):
        return {"ok": False, "error": "invalid_selection"}
    try:
        out = finalize_selection_order(ctx, sel, stage="full_master_ranking", apply_cta=True)
        from interview_mux.artifact_writes import write_validated_artifact

        write_validated_artifact(
            ctx,
            "master/selection.json",
            out,
            merge_from_disk=True,
            stage_key="full_master_ranking",
        )
    except Exception as exc:
        return {"ok": False, "error": str(exc)}
    append_ledger(
        ctx,
        {"kind": "heal_air_order_integrity", "identity": "heal_air_order_integrity"},
    )
    return {"ok": True}


INVALIDATION_LOG_REL = "operator/invalidation_log.jsonl"


def _append_invalidation_log(
    ctx: RunContext,
    *,
    stage: str,
    mode: str,
    detail: dict[str, Any] | None = None,
) -> None:
    row = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "stage": stage,
        "mode": mode,
        "detail": detail or {},
    }
    try:
        path = ctx.path(INVALIDATION_LOG_REL)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
    except Exception:
        pass


def invalidate_downstream(ctx: RunContext, stage: str) -> dict[str, Any]:
    try:
        from interview_mux.remediation_framework import policy_remediation_active, read_active_remediation_plan

        if policy_remediation_active(ctx):
            plan = read_active_remediation_plan(ctx)
            from interview_mux.refinement_passes import filter_retired_refine_stages

            allowed = set(
                filter_retired_refine_stages((plan or {}).get("allowed_rerun_stages") or [])
            )
            if allowed and str(stage) not in allowed:
                from interview_mux.execution_invalidation_profiles import (
                    resolve_invalidation_profile,
                    profile_allows_clear,
                )

                ec = str((plan or {}).get("error_class") or "")
                profile = resolve_invalidation_profile(ctx, ec)
                if profile and not profile_allows_clear(profile, stage):
                    unmark_stage_only(ctx, stage)
                append_ledger(
                    ctx,
                    {
                        "kind": "invalidate_downstream",
                        "identity": "invalidate_downstream",
                        "stage": stage,
                        "mode": "remediation_heal_only",
                    },
                )
                return {"ok": True, "cleared_from": stage, "mode": "remediation_heal_only"}
    except Exception:
        pass
    # C12: post-assembly/master mix|edl rewalk needs reopen allow.
    if str(stage) in {"edl", "mix"}:
        try:
            asm = ctx.final_path("master", "assembly.wav")
            master = ctx.final_path("master", "master.wav")
            if (asm.is_file() and asm.stat().st_size > 0) or (
                master.is_file() and master.stat().st_size > 0
            ):
                from interview_mux.timeline_reopen_meta_gate import (
                    INTENT_MIX_REWALK,
                    decide_timeline_reopen,
                )

                gate = decide_timeline_reopen(
                    ctx,
                    intent=INTENT_MIX_REWALK,
                    detail={"from_stage": stage},
                )
                if not gate.get("allow"):
                    append_ledger(
                        ctx,
                        {
                            "kind": "invalidate_downstream",
                            "identity": "invalidate_downstream",
                            "stage": stage,
                            "mode": "refused_low_gain",
                            "gate": gate,
                        },
                    )
                    return {
                        "ok": False,
                        "cleared_from": None,
                        "mode": "refused_low_gain",
                        "gate": gate,
                    }
        except Exception:
            append_ledger(
                ctx,
                {
                    "kind": "invalidate_downstream",
                    "identity": "invalidate_downstream",
                    "stage": stage,
                    "mode": "refused_fail_closed",
                },
            )
            return {
                "ok": False,
                "cleared_from": None,
                "mode": "refused_fail_closed",
            }
    _refuse_g0_locked_rerun(ctx, stage, action="invalidate")
    _refuse_delivery_timeline_rewind(ctx, stage, action="invalidate")
    try:
        from interview_mux.delivery_guardrails import (
            delivery_epoch_locked,
            invalidation_is_structural,
            maybe_restore_master_bundle,
            record_wasted_work,
        )

        if invalidation_is_structural(ctx, stage) and delivery_epoch_locked(ctx):
            raise RuntimeError(
                f"delivery epoch locked — operator unlock required before structural "
                f"invalidation from {stage}"
            )
        if not invalidation_is_structural(ctx, stage):
            unmark_stage_only(ctx, stage)
            restored = maybe_restore_master_bundle(ctx, stage=stage)
            append_ledger(
                ctx,
                {
                    "kind": "invalidate_downstream",
                    "identity": "invalidate_downstream",
                    "stage": stage,
                    "mode": "heal_only",
                    "restored": list(restored)[:20],
                },
            )
            _append_invalidation_log(ctx, stage=stage, mode="heal_only")
            return {"ok": True, "cleared_from": stage, "mode": "heal_only"}
        record_wasted_work(ctx, event="orphan", stage=stage, detail={"mode": "structural"})
    except RuntimeError:
        raise
    except Exception:
        pass
    try:
        from interview_mux.journey_state import reconcile_milestones_after_invalidation

        reconcile_milestones_after_invalidation(ctx)
    except Exception:
        pass
    # B-06: structural heals use profiles only — never combined clear_from.
    profile_id = "structural_delivery"
    try:
        from interview_mux.execution_invalidation_profiles import (
            apply_bounded_invalidation,
            resolve_invalidation_profile,
        )
        from interview_mux.remediation_framework import read_active_remediation_plan

        plan = read_active_remediation_plan(ctx)
        if isinstance(plan, dict):
            ec = str(plan.get("error_class") or "").strip()
            resolved = resolve_invalidation_profile(ctx, ec) if ec else None
            if resolved is not None:
                profile_id = resolved.profile_id
        result = apply_bounded_invalidation(
            ctx,
            profile_id,
            reason=f"invalidate_downstream:{stage}",
        )
    except Exception as inv_exc:
        ctx.log(
            f"invalidate_downstream: bounded invalidation failed ({inv_exc})",
            level="warning",
            stage=stage,
        )
        result = {"profile_id": profile_id, "cleared": [], "error": str(inv_exc)[:240]}
    append_ledger(
        ctx,
        {
            "kind": "invalidate_downstream",
            "identity": "invalidate_downstream",
            "stage": stage,
            "mode": "structural",
            "profile_id": profile_id,
            "cleared": list(result.get("cleared") or [])[:40],
        },
    )
    _append_invalidation_log(ctx, stage=stage, mode="structural")
    # Remaster/invalidate of pre-mix producers must not leave assembly looking seated.
    try:
        if stage in DELIVERY_ORDER and "mix" in DELIVERY_ORDER:
            if DELIVERY_ORDER.index(stage) <= DELIVERY_ORDER.index("mix"):
                from interview_mux.thrash_hardening import bump_assembly_seating_generation

                bump_assembly_seating_generation(ctx, f"invalidate:{stage}")
    except Exception:
        pass
    try:
        from interview_mux.remediation_framework import reconcile_invalidated_bundle

        reconcile_invalidated_bundle(ctx, [stage], reason="invalidate_downstream")
    except Exception:
        pass
    return {
        "ok": True,
        "cleared_from": stage,
        "mode": "structural",
        "profile_id": profile_id,
        "cleared": list(result.get("cleared") or []),
    }


def resolve_stage_plan(ctx: RunContext, stage: str) -> dict[str, Any]:
    """ADG-backed plan: blockers, prereqs, invalidation, recommended next stage."""
    from interview_mux.artifact_dependency_graph import transitive_invalidate, upstream_closure

    stage = str(stage or "").strip()
    if not stage:
        raise ValueError("stage required")
    prereq_chain = upstream_closure(stage)
    invalidate_set = transitive_invalidate(stage)
    blockers: list[str] = []
    native_only = False
    try:
        from interview_mux.pipeline_mode import is_native_only

        native_only = bool(is_native_only(ctx))
    except Exception:
        native_only = False
    gap_skipped = native_only
    if not gap_skipped:
        try:
            from interview_mux.gap_fill_eligibility import gap_fill_was_skipped

            gap_skipped = bool(gap_fill_was_skipped(ctx))
        except Exception:
            gap_skipped = False
    for prereq_stage, rel in DELIVERY_ANALYSIS_PREREQS:
        if gap_skipped and prereq_stage in _GAP_FILL_ANALYSIS_PREREQS:
            from interview_mux.delivery_guardrails import seed_stage_complete

            if ctx.artifact_exists(rel) and not seed_stage_complete(ctx, prereq_stage):
                heal_or_refuse_mark(ctx, prereq_stage, force=True)
            continue
        if not ctx.artifact_exists(rel):
            blockers.append(f"missing_artifact:{rel}")
            continue
        from interview_mux.delivery_guardrails import seed_stage_complete

        if not seed_stage_complete(ctx, prereq_stage):
            blockers.append(f"stage_not_done:{prereq_stage}")
    skip = skipped_stages(ctx)
    for up in prereq_chain:
        if up in skip:
            continue
        if not stage_outputs_present(ctx, up):
            blockers.append(f"upstream_incomplete:{up}")
    try:
        from interview_mux.delivery_guardrails import (
            MIX_EPOCH_RUN_BLOCK,
            current_delivery_phase,
            mix_epoch_block,
            upstream_stale_blockers,
            vo_synthesize_stability_block,
        )

        for token in upstream_stale_blockers(ctx, stage):
            blockers.append(f"stale_upstream:{token}")
        if stage == "vo_synthesize":
            vo_b = vo_synthesize_stability_block(ctx)
            if vo_b:
                blockers.append(f"vo_synth_unstable:{vo_b}")
        if stage in MIX_EPOCH_RUN_BLOCK:
            mix_b = mix_epoch_block(ctx, stage=stage)
            if mix_b:
                blockers.append(f"mix_epoch:{mix_b}")
        phase_name = current_delivery_phase(ctx)
    except Exception:
        phase_name = None
    recommended_next = stage
    for token in blockers:
        if token.startswith("stage_not_done:"):
            recommended_next = token.split(":", 1)[1]
            break
        if token.startswith("upstream_incomplete:"):
            recommended_next = token.split(":", 1)[1]
            break
        if token.startswith("vo_synth_unstable:"):
            try:
                from interview_mux.delivery_guardrails import resolve_vo_synth_seed_resume

                recommended_next = (
                    resolve_vo_synth_seed_resume(token.split(":", 1)[1]) or stage
                )
            except Exception:
                recommended_next = "vo_line_adjudicate"
            break
        if token.startswith("stale_upstream:"):
            token_val = token.split(":", 1)[1]
            try:
                from interview_mux.delivery_guardrails import (
                    resolve_assembly_stale_resume,
                    resolve_gap_report_stale_producer,
                    resolve_vo_synth_seed_resume,
                )

                if token_val == "assembly_stale_versus_edl":
                    recommended_next = resolve_assembly_stale_resume(ctx)
                elif token_val == "gap_report":
                    recommended_next = resolve_gap_report_stale_producer(ctx)
                elif token_val in {"transitions", "sound_design_plan"}:
                    recommended_next = token_val
                else:
                    recommended_next = (
                        resolve_vo_synth_seed_resume(token_val) or token_val
                    )
            except Exception:
                recommended_next = token_val
            break
        if token.startswith("mix_epoch:"):
            token_val = token.split(":", 1)[1]
            try:
                from interview_mux.delivery_guardrails import (
                    MUSIC_BEFORE_MIX,
                    resolve_assembly_stale_resume,
                    seed_stage_complete,
                )

                if token_val == "assembly_stale_versus_edl":
                    recommended_next = resolve_assembly_stale_resume(ctx)
                elif token_val == "music_incomplete":
                    recommended_next = "mmaudio_sfx"
                    for sid in MUSIC_BEFORE_MIX:
                        if not seed_stage_complete(ctx, sid):
                            recommended_next = sid
                            break
                else:
                    recommended_next = token_val
            except Exception:
                recommended_next = token_val
            break
        if token.startswith("missing_artifact:"):
            for ps, rel in DELIVERY_ANALYSIS_PREREQS:
                if rel == token.split(":", 1)[1]:
                    recommended_next = ps
                    break
            break
    try:
        from interview_mux.delivery_guardrails import ship_path_ready

        ready, ready_reason = ship_path_ready(ctx)
        if ready and stage == "junction_snip_qa":
            recommended_next = "master_finalize"
            blockers.append(f"ship_path_ready:{ready_reason or 'pin_finalize'}")
    except Exception:
        pass
    pipeline_mode_val: str | None = None
    try:
        from interview_mux.pipeline_mode import resolve_effective_mode

        pipeline_mode_val = str(resolve_effective_mode(ctx).get("mode") or "") or None
    except Exception:
        pipeline_mode_val = None
    return {
        "stage": stage,
        "blockers": blockers,
        "prereq_chain": prereq_chain,
        "invalidate_set": invalidate_set,
        "recommended_next": recommended_next,
        "pipeline_mode": pipeline_mode_val,
        "delivery_phase": phase_name,
    }


def rerun_with_impact(ctx: RunContext, stage: str) -> dict[str, Any]:
    """8A: invalidate downstream plus ADG transitive consumer set.

    Depth budget 8: after clear, pin earliest incomplete stage in the impact
    chain so seed-front re-emit walks the ADG without thrash (remaster-edge-adg).
    """
    from interview_mux.artifact_dependency_graph import transitive_invalidate

    stage = str(stage or "").strip()
    if not stage:
        raise ValueError("stage required")
    invalidate_set = list(transitive_invalidate(stage))
    cleared = invalidate_downstream(ctx, stage)
    order = list(ANALYSIS_ORDER) + list(DELIVERY_ORDER)
    chain = [stage] + [s for s in invalidate_set if s != stage]
    budget = min(max(len(chain), 3), 8)
    pin = stage
    for sid in chain[:budget]:
        if sid not in order:
            continue
        try:
            from interview_mux.delivery_guardrails import seed_stage_complete

            if not seed_stage_complete(ctx, sid):
                pin = sid
                break
        except Exception:
            from interview_mux.done_authority import land_honest

            if not land_honest(ctx, sid):
                pin = sid
                break
    append_ledger(
        ctx,
        {
            "kind": "rerun_with_impact",
            "identity": "rerun_with_impact",
            "stage": stage,
            "invalidate_set": invalidate_set[:40],
            "earliest_incomplete": pin,
            "adg_budget": budget,
        },
    )
    return {
        "ok": True,
        "stage": stage,
        "invalidate_set": invalidate_set,
        "earliest_incomplete": pin,
        "adg_budget": budget,
        **cleared,
    }


def request_walk_seed_remainder(ctx: RunContext, *, reason: str = "conductor") -> dict[str, Any]:
    append_ledger(
        ctx,
        {"kind": "walk_seed_remainder", "identity": "walk_seed_remainder", "reason": reason},
    )
    return {"ok": True, "reason": reason}


def _walk_sequence(ctx: RunContext, walk_stages: list[str], *, reason: str):
    """Stages to attempt in fixed seed order (brain 0.2.0).

    The candidate list is already filtered (G0 truncation, voice-reference drop,
    ``filter_delivery_candidates``). Each yielded stage still passes through the
    dispatch door, defect ledger, and reachability halt in the walk loop.
    """
    _ = (ctx, reason)
    return iter(walk_stages)


def _constrain_delivery_walk_for_sticky(
    ctx: RunContext, stages: list[str]
) -> list[str]:
    """While sticky HARD is active, permit its producer pin and nothing else.

    If the sticky pin is already seed-complete, clear the halt and allow the
    remainder walk — otherwise a sealed SDP (etc.) blocks adjudicate/synth
    forever (exec_13170 incomplete-after-conductor ↔ sound_design_plan).
    """
    try:
        sticky = (
            ctx.read_json("operator/sticky_heal.json")
            if ctx.artifact_exists("operator/sticky_heal.json")
            else {}
        )
    except Exception:
        return list(stages)
    active = sticky.get("active_halt") if isinstance(sticky, dict) else None
    if not isinstance(active, dict):
        return list(stages)
    pin = str(active.get("pin") or "").strip()
    if not pin:
        return list(stages)

    pin_sealed = False
    try:
        from interview_mux.thrash_hardening import sticky_pin_is_sealed

        pin_sealed = bool(sticky_pin_is_sealed(ctx, pin))
    except Exception:
        try:
            from interview_mux.delivery_guardrails import seed_stage_complete

            pin_sealed = bool(seed_stage_complete(ctx, pin))
        except Exception:
            pin_sealed = False
    if pin_sealed:
        try:
            cleared = dict(sticky)
            cleared.pop("active_halt", None)
            ctx.write_json("operator/sticky_heal.json", cleared, skip_handoff=True)
        except Exception:
            pass
        return list(stages)

    if pin in stages:
        return [pin]
    raise RuntimeError(
        "Delivery incomplete after conductor — sticky halt refuses multi-stage "
        f"walk; resume={pin or (stages[0] if stages else 'delivery')}"
    )


_SEED_ORDER_RE = re.compile(r"seed order: complete (\S+) before running (\S+)")


def _run_seed_prerequisites_first(
    ctx: RunContext, stage: str, retried: set[str], run_stage: Any
) -> list[str]:
    """Resolve the seed-order chain ahead of dispatch, not after a refusal (ISSUES 106).

    ``dispatch_stage`` refuses a stage whose earlier seed-order stage is not
    complete, and the walk then learns the prerequisite from the exception,
    one per pass. Every run paid two or three failed passes to "seed order:
    complete chapter_close_hitch before running refinement_agenda", then
    full_master_ranking, then the next. Ask the same check first and run the
    chain up front, bounded by the order length and by ``retried`` (one run
    per prerequisite per walk, shared with the reactive path below). A
    prerequisite that fails to run falls through to dispatch, which raises
    the same seed-order error the reactive path already handles.
    """
    from interview_mux.homunculus.runtime import _seed_prereq_block

    # Delivery stages only, and only delivery prerequisites: that is the chain
    # every run climbed one failed pass at a time. An analysis hole behind a
    # delivery stage is the engine's hop-back, not the walk's, and analysis
    # stages keep the reactive path (their walks are short and their fixtures
    # in the suite are deliberately partial).
    if stage not in DELIVERY_ORDER:
        return []
    ran: list[str] = []
    if stage == "mix" and _junction_owes_recut_before_mix(ctx, retried):
        # Mix refuses while the live EDL carries critical incomplete-cut
        # residuals and names junction_snip_qa as the owner. The walk used to
        # learn that from the refusal: an error line, "Failed: Stage mix", a
        # recovery playbook, a failed delivery pass (ISSUES 131). Ask the same
        # check first and run the owner ahead.
        retried.add("junction_snip_qa")
        ctx.log(
            "seed walk: running junction_snip_qa before mix "
            "(live incomplete-cut residuals need recut, fuse or omit)",
            level="info",
            stage="mix",
            detail={"event": "seed_prereq_ahead", "prereq": "junction_snip_qa"},
        )
        try:
            if ctx.is_done("junction_snip_qa"):
                unmark_stage_only(ctx, "junction_snip_qa")
            _run_demanded_prereq(ctx, "junction_snip_qa", stage, run_stage)
        except Exception as exc:  # noqa: BLE001 - re-raised as the stage's prerequisite failure
            ctx.log(
                f"seed walk: junction_snip_qa did not land ahead of mix: "
                f"{type(exc).__name__}: {str(exc)[:160]}",
                level="warning",
                stage="mix",
            )
            raise SeedPrerequisiteFailed(
                f"Prerequisite stage junction_snip_qa is not complete ahead of mix: "
                f"{type(exc).__name__}: {str(exc)[:200]}"
            ) from exc
        ran.append("junction_snip_qa")
    for _ in range(len(DELIVERY_ORDER)):
        try:
            blocked = _seed_prereq_block(ctx, stage)
        except Exception:
            return ran
        blocked = str(blocked or "").strip()
        if not blocked or blocked == stage or blocked in retried or blocked not in DELIVERY_ORDER:
            return ran
        if not _seed_prereq_needs_run(ctx, blocked):
            return ran
        retried.add(blocked)
        ctx.log(
            f"seed walk: running prerequisite {blocked} before {stage}",
            level="info",
            stage=stage,
            detail={"event": "seed_prereq_ahead", "prereq": blocked},
        )
        try:
            _run_demanded_prereq(ctx, blocked, stage, run_stage)
        except Exception as exc:  # noqa: BLE001 - re-raised as the stage's prerequisite failure
            ctx.log(
                f"seed walk: prerequisite {blocked} did not land ahead of {stage}: "
                f"{type(exc).__name__}: {str(exc)[:160]}",
                level="warning",
                stage=stage,
            )
            # Dispatching the consumer now would only make it raise its own
            # "Prerequisite stage X is not complete" at error level (every
            # fresh run carried that line, ISSUES 119). Raise here, in the
            # words the walk's prerequisite parser already understands.
            raise SeedPrerequisiteFailed(
                f"Prerequisite stage {blocked} is not complete ahead of {stage}: "
                f"{type(exc).__name__}: {str(exc)[:200]}"
            ) from exc
        ran.append(blocked)
    return ran


class SeedPrerequisiteFailed(RuntimeError):
    """A prerequisite the walk ran ahead of a stage did not land (ISSUES 119)."""


def _junction_owes_recut_before_mix(ctx: RunContext, retried: set[str]) -> bool:
    """Whether mix would refuse for live incomplete-cut residuals junction may fix now.

    Reads the two rules the stages themselves use: mix's refusal
    (``live_incomplete_cut_critical_findings``) and the ordering exemption
    that lets junction run while mix is incomplete (entry 62). Both must
    hold; if junction may not run ahead, dispatching it would only trade one
    refusal for another. Once per walk.
    """
    if "junction_snip_qa" in retried:
        return False
    try:
        from interview_mux.junction_snip_qa import live_incomplete_cut_critical_findings
        from interview_mux.ordering_authority import ordering_exempt

        if not live_incomplete_cut_critical_findings(ctx):
            return False
        return bool(ordering_exempt(ctx, "junction_snip_qa", "mix"))
    except Exception:
        return False


def _run_demanded_prereq(ctx: RunContext, prereq: str, consumer: str, run_stage: Any) -> None:
    """Run ``prereq`` because the seed order demands it before ``consumer`` (ISSUES 127).

    The dispatch door is told the run is demanded, so "nothing changed" cannot
    refuse it. If the prerequisite still is not seed-complete afterwards, its
    marker is offered to the heal ladder (which marks only a complete body),
    and whatever is still wrong is logged in the prerequisite's own words:
    the consumer's "seed order: complete X" line is the symptom, and it was
    all the log used to say.
    """
    from interview_mux.dispatch_door import demand_seed_prereq

    with demand_seed_prereq(ctx, prereq):
        run_stage(ctx, prereq)
    try:
        from interview_mux.delivery_guardrails import seed_stage_complete

        if seed_stage_complete(ctx, prereq):
            return
        from interview_mux.stage_completion import (
            heal_or_refuse_mark,
            stage_artifact_incompleteness,
        )

        verdict: dict[str, Any] = {}
        if not ctx.is_done(prereq):
            verdict = heal_or_refuse_mark(ctx, prereq, force=True) or {}
            if seed_stage_complete(ctx, prereq):
                ctx.log(
                    f"seed walk: prerequisite {prereq} was complete on disk without its "
                    f"marker; marked ahead of {consumer}",
                    level="info",
                    stage=prereq,
                    detail={"event": "seed_prereq_marker_healed", "consumer": consumer},
                )
                return
        why = stage_artifact_incompleteness(ctx, prereq) or (
            verdict.get("reason") if isinstance(verdict, dict) else ""
        ) or ("marker missing" if not ctx.is_done(prereq) else "outputs missing")
        if _recover_incomplete_prereq(ctx, prereq, str(why)):
            ctx.log(
                f"seed walk: prerequisite {prereq} completed by its recovery playbook "
                f"ahead of {consumer} ({str(why)[:160]})",
                level="info",
                stage=prereq,
                detail={"event": "seed_prereq_recovered", "consumer": consumer, "why": str(why)[:600]},
            )
            return
        ctx.log(
            f"seed walk: prerequisite {prereq} is still incomplete after its run "
            f"ahead of {consumer}: {str(why)[:300]}",
            level="warning",
            stage=prereq,
            detail={"event": "seed_prereq_still_incomplete", "consumer": consumer, "why": str(why)[:600]},
        )
    except Exception:
        pass


def _recover_incomplete_prereq(ctx: RunContext, prereq: str, why: str) -> bool:
    """Give an incomplete prerequisite its own recovery playbook, once (ISSUES 127).

    A stage that raises gets ``handle_stage_failure`` and its playbook. A
    stage that returned without becoming complete (the door refused it, or it
    finished short of its completion bar) got nothing: the consumer raised a
    seed-order error instead, and recovery was run for the consumer. Route
    the prerequisite's own incompleteness through the same controller, then
    offer the marker to the heal ladder again. True when it is now complete.
    """
    try:
        from interview_mux.delivery_guardrails import seed_stage_complete
        from interview_mux.recovery_controller import handle_stage_failure
        from interview_mux.stage_completion import heal_or_refuse_mark

        result = handle_stage_failure(ctx, prereq, RuntimeError(str(why)[:400]))
        if getattr(result, "status", "") != "recovered":
            return False
        if not ctx.is_done(prereq):
            heal_or_refuse_mark(ctx, prereq, force=True)
        return bool(seed_stage_complete(ctx, prereq))
    except Exception:
        return False


#: Audio stages where a rerun on unchanged inputs is exactly the ping-pong the
#: no-delta guard exists to stop (exec_11871: 55 mix and junction dispatches).
#: For these a refusal stands even when the marker cannot be restored.
_REFUSAL_STANDS_FOR: frozenset[str] = frozenset(
    {
        "mix",
        "junction_snip_qa",
        "mmaudio_sfx",
        "vo_synthesize",
        "music_palette_compose",
        "master_finalize",
    }
)


def _refusal_strands_stage(ctx: RunContext, stage: str, reason: str, rerun: set[str]) -> bool:
    """Whether a door refusal would leave ``stage`` incomplete with no way forward.

    ``no_delta`` means "the last success stands, do not run it again". When
    the stage is seed-complete that is true and the walk advances. (The
    attempt memo is a different claim, "this already failed at this state",
    and is left alone: voiding it would re-walk the same failed stages on
    every re-entry, which is what it exists to stop.) When it is not, the marker is first offered to the heal
    ladder, which marks a body that is complete on disk. If that also fails,
    the last result does not stand: advancing strands the next stage on the
    seed order (or ends the phase with a hole), so the stage is run once in
    this walk under the seed order's demand (ISSUES 127). Returns True when
    the refusal is void and the stage must run.
    """
    if str(reason or "") != "no_delta":
        return False
    if stage in rerun or stage in _REFUSAL_STANDS_FOR:
        return False
    try:
        from interview_mux.delivery_guardrails import seed_stage_complete

        if seed_stage_complete(ctx, stage):
            return False
        if not ctx.is_done(stage):
            from interview_mux.stage_completion import heal_or_refuse_mark

            heal_or_refuse_mark(ctx, stage, force=True)
            if seed_stage_complete(ctx, stage):
                ctx.log(
                    f"seed walk: {stage} was complete on disk without its marker; "
                    f"marked, refusal ({reason}) stands",
                    level="info",
                    stage=stage,
                    detail={"event": "refused_stage_marker_healed", "reason": reason},
                )
                return False
    except Exception:
        return False
    rerun.add(stage)
    ctx.log(
        f"seed walk: {stage} is refused ({reason}) but is not complete; "
        "running it once instead of advancing past it",
        level="warning",
        stage=stage,
        detail={"event": "refusal_void_stage_incomplete", "reason": reason},
    )
    return True


def _seed_prereq_needs_run(ctx: RunContext, prereq: str) -> bool:
    """A named prerequisite is worth one run unless it is genuinely complete.

    ``dispatch_stage`` raised because ``prereq`` is not seed-complete. When a
    ``.stage_done`` marker exists anyway (a refused commit left it behind),
    the marker is the lie, not the check: drop it so the rerun is a real run.
    """
    if not ctx.is_done(prereq):
        return True
    try:
        from interview_mux.delivery_guardrails import seed_stage_complete

        if seed_stage_complete(ctx, prereq):
            return False
    except Exception:
        return False
    ctx.log(
        f"seed walk: {prereq} is marked done but not seed-complete; "
        "dropping the stale marker before rerunning it",
        level="warning",
        stage=prereq,
        detail={"event": "seed_prereq_stale_marker"},
    )
    try:
        unmark_stage_only(ctx, prereq)
    except Exception:
        return False
    return True


def _seed_order_prereq_from(exc: BaseException) -> str:
    """Prerequisite named by a seed-order RuntimeError, or "" when not one."""
    if not isinstance(exc, RuntimeError):
        return ""
    m = _SEED_ORDER_RE.search(str(exc))
    return m.group(1) if m else ""


def walk_seed_agenda(ctx: RunContext, stages: list[str], *, reason: str) -> None:
    """Explicit logged fallback — not a silent linear fall-through."""
    append_ledger(
        ctx,
        {
            "kind": "fallback",
            "identity": "walk_seed_agenda",
            "reason": reason,
            "stages": list(stages)[:80],
        },
    )
    ctx.log(
        f"homunculus seed-agenda fallback ({reason}): {len(stages)} stage(s)",
        level="warning",
        stage="homunculus",
    )
    from interview_mux.gates import g0_blocks_analysis
    from interview_mux.pipeline import run_single_stage

    setattr(ctx, "_homunculus_seed_walk", True)
    # Each prerequisite is auto-run at most once per walk, so a genuinely broken
    # stage cannot ping-pong the walk forever.
    seed_prereq_retried: set[str] = set()
    # Stages run despite a no-delta refusal because they were left
    # incomplete; once each per walk (ISSUES 127).
    refusal_void_rerun: set[str] = set()
    try:
        from interview_mux.web.job_progress import notify_batch_plan

        walk_stages = _drop_delivery_while_voice_ref_open(
            ctx, _truncate_analysis_while_g0_open(ctx, list(stages))
        )

        notify_batch_plan(
            ctx.run_id,
            walk_stages,
            message=f"Walking {len(walk_stages)} remaining stage(s) ({reason})",
        )
        prepare_delivery_guardrails(ctx, walk_stages)
        try:
            from interview_mux.delivery_guardrails import filter_delivery_candidates

            walk_stages = filter_delivery_candidates(ctx, walk_stages)
        except Exception:
            pass
        for stage in _walk_sequence(ctx, walk_stages, reason=reason):
            from interview_mux.done_authority import may_skip_as_complete

            if may_skip_as_complete(ctx, stage):
                continue
            # DETECTION_ONLY_IS_DONE: hollow stamp without outputs → unmark then run.
            if ctx.is_done(stage) and not stage_outputs_present(ctx, stage):
                unmark_stage_only(ctx, stage)
            if stage in skipped_stages(ctx) and stage_outputs_present(ctx, stage):
                continue
            try:
                _refuse_g0_locked_rerun(ctx, stage, action="walk")
                _refuse_delivery_timeline_rewind(ctx, stage, action="walk")
                _refuse_music_before_assembly(ctx, stage, action="walk")
            except RuntimeError as exc:
                from interview_mux.delivery_guardrails import seed_stage_complete

                if prepare_outputs_present(ctx, stage) and not seed_stage_complete(
                    ctx, stage
                ):
                    from interview_mux.stage_completion import heal_or_refuse_mark

                    heal_or_refuse_mark(ctx, stage, force=True)
                exc_text = str(exc)
                if "assembly audio missing" in exc_text:
                    ctx.log(
                        f"music_deferred: {stage} — pin assembly_preview",
                        level="warning",
                        stage="assembly_preview",
                    )
                    # RC9: prepend assembly_preview and restart walk (do not skip).
                    rest = [s for s in walk_stages if s != "assembly_preview"]
                    try:
                        idx = walk_stages.index(stage)
                        rest = [
                            s
                            for s in walk_stages[idx:]
                            if s not in {"assembly_preview", stage}
                        ]
                    except ValueError:
                        pass
                    restart = ["assembly_preview"] + rest
                    if restart != walk_stages:
                        walk_seed_agenda(
                            ctx, restart, reason="music_deferred_pin_assembly"
                        )
                        return
                # HAU: preview-only assembly blocks MusicGen — do not silent-continue
                # (exec_13170: ESR walk skipped music → hollow Finished). Speech-first
                # mix seats assembly.wav so music can admit afterward.
                if stage in MUSIC_REQUIRES_ASSEMBLY and (
                    "assembly_preview_only" in exc_text
                    or "assembly_not_seated" in exc_text
                    or "assembly_not_ready_for_music" in exc_text
                ):
                    try:
                        from interview_mux.mix_junction_seat import (
                            beds_deferred_for_mix,
                            hold_speech_first_mix,
                        )

                        if hold_speech_first_mix(ctx, "mix") or beds_deferred_for_mix(ctx):
                            # Only walk mix — never re-queue MUSIC_REQUIRES_ASSEMBLY
                            # stages here (exec_13170: recursive hau_speech_first
                            # grew mix+mmaudio+sfx+music and thrashed).
                            if reason == "hau_speech_first_before_music":
                                ctx.log(
                                    f"hau_speech_first: skip blocked {stage} until "
                                    "mix seats assembly (already in speech-first walk)",
                                    level="warning",
                                    stage="mix",
                                )
                                continue
                            ctx.log(
                                f"hau_speech_first: {stage} blocked ({exc_text[:120]}) "
                                "— walk mix to seat assembly before MusicGen",
                                level="warning",
                                stage="mix",
                            )
                            walk_seed_agenda(
                                ctx,
                                ["mix"],
                                reason="hau_speech_first_before_music",
                            )
                            return
                    except Exception:
                        pass
                    raise
                ctx.log(
                    f"seed walk: {stage} refused for this walk and skipped: {str(exc)[:200]}",
                    level="warning",
                    stage=stage,
                    detail={"event": "walk_refusal_skipped", "reason": reason},
                )
                continue
            if stage == "transcript_review":
                # 3A: remainder walk must not sign G0 off. Driver owns complete_g0 / wait.
                ctx.log(
                    "g0_pending: transcript_review operator must-act — walk will not run the gate",
                    level="warning",
                    stage="transcript_review",
                )
                break
            if stage == "topic_coverage_audit":
                try:
                    from interview_mux.gap_vo_gates import check_voice_reference_pending

                    if check_voice_reference_pending(ctx):
                        ctx.log(
                            "voice_reference_pending: walk will not enter topic_coverage_audit",
                            level="warning",
                            stage="missing_framing",
                        )
                        break
                except Exception:
                    pass
            # Attempt memo / caps / no-delta (§5.2-§5.3). Runs after the gate breaks
            # above so G0 and voice-reference pauses keep their semantics; a refusal
            # advances past the stage with a defect (D1) instead of re-walking it.
            verdict = None
            try:
                from interview_mux.dispatch_door import evaluate_dispatch

                verdict = evaluate_dispatch(ctx, stage, source=reason, layer="walk")
            except Exception:
                verdict = None
            run_demanded = False
            if (
                verdict is not None
                and verdict.refused
                and _refusal_strands_stage(ctx, stage, verdict.reason, refusal_void_rerun)
            ):
                verdict = None
                run_demanded = True
            if verdict is not None and verdict.refused:
                try:
                    from interview_mux.dispatch_door import refuse_dispatch

                    refuse_dispatch(ctx, stage, verdict, source=reason)
                except Exception:
                    pass
                # D1: advance past the refusal unless the ship path is *provably*
                # severed. The halt lives outside the try above on purpose — a
                # halt payload must reach the operator, not get swallowed.
                halt = None
                try:
                    from interview_mux.ship_reachability import unreachable_halt

                    halt = unreachable_halt(ctx)
                except Exception:
                    halt = None
                if halt:
                    from interview_mux.ship_reachability import ShipUnreachable

                    ctx.log(
                        "ship path provably severed — halting walk: "
                        f"{halt.get('blockers')} (resume {halt.get('resume') or 'unknown'})",
                        level="error",
                        stage=stage,
                        detail=halt,
                    )
                    raise ShipUnreachable(halt)
                # G1 / VO criticals + gap framing: do not advance to a hollow
                # "Finished" when required outputs are still missing (budget refuse
                # otherwise skips vo_line → premature vo_synthesize thrash, or
                # gap_framing_compose → hollow analysis Finished — DP-BUD1 A).
                try:
                    from interview_mux.defect_ledger import SHIP_BAR_CRITICAL_STAGES
                    from interview_mux.llm_flow_hardening import FLOW_CRITICAL_LLM_STAGES

                    must_land = set(SHIP_BAR_CRITICAL_STAGES) | set(FLOW_CRITICAL_LLM_STAGES) | {
                        "gap_framing_compose",
                        "missing_framing",
                    }
                    if stage in must_land and not stage_outputs_present(ctx, stage):
                        ctx.log(
                            f"dispatch refused for incomplete critical {stage} "
                            f"({verdict.reason}) — stopping walk (no advance)",
                            level="error",
                            stage=stage,
                            detail={"reason": verdict.reason, "detail": verdict.detail},
                        )
                        break
                except Exception:
                    pass
                continue
            try:
                _run_seed_prerequisites_first(ctx, stage, seed_prereq_retried, run_single_stage)
                if run_demanded:
                    from interview_mux.dispatch_door import demand_seed_prereq

                    with demand_seed_prereq(ctx, stage):
                        run_single_stage(ctx, stage)
                else:
                    run_single_stage(ctx, stage)
            except Exception as exc:
                prereq = _seed_order_prereq_from(exc)
                # A stage can be invalidated mid-walk by an upstream rewrite:
                # boundary_topic_resplit rewrites segments/boundaries.json, which
                # correctly makes framing_posture_decide stale. Raising here
                # stalls the whole walk on a prerequisite the walk is perfectly
                # able to satisfy, and recovery_controller has already worked out
                # the same answer (resume=<prereq>). Run it and retry once.
                # The seed-order check judges completeness, not the marker: a
                # prerequisite whose commit was refused can still carry a stale
                # .stage_done (ISSUES 86). Treat that marker as hollow and rerun.
                if (
                    prereq
                    and prereq not in seed_prereq_retried
                    and prereq != stage
                    and _seed_prereq_needs_run(ctx, prereq)
                ):
                    seed_prereq_retried.add(prereq)
                    ctx.log(
                        f"seed walk: running prerequisite {prereq} before retrying {stage}",
                        level="warning",
                        stage=stage,
                        detail={"event": "seed_prereq_autorun", "prereq": prereq},
                    )
                    try:
                        _run_demanded_prereq(ctx, prereq, stage, run_single_stage)
                        run_single_stage(ctx, stage)
                        continue
                    except Exception as retry_exc:
                        exc = retry_exc
                fp = f"{type(exc).__name__}:{str(exc)[:160]}"
                hit = note_identical_stage_error(ctx, stage, fp)
                if hit.get("exhausted"):
                    ctx.log(
                        f"homunculus identical error cap on {stage} — needs_operator ({fp})",
                        level="error",
                        stage=stage,
                    )
                    break
                raise
            if stage == "transcript_review_build" and g0_blocks_analysis(ctx):
                break
    finally:
        if hasattr(ctx, "_homunculus_seed_walk"):
            delattr(ctx, "_homunculus_seed_walk")


def run_homunculus_phase(
    ctx: RunContext,
    phase: str,
    remaining: list[str],
    *,
    client: Any | None = None,
) -> dict[str, Any]:
    """Conductor selects tools. Leftover stages walk seed order only if requested."""
    prior = list(remaining)
    seed = _order_for(phase)
    # Resume slices (from_stage=edl) must not pull earlier hollow producers
    # (nugget_layup / sound_design_plan) back into the walk — that regenerates
    # MusicGen after a junction/mix order heal.
    if phase == "delivery" and prior:
        first = prior[0]
        start_idx = seed.index(first) if first in seed else 0
        # Resume at edl with no narrative audit must re-include the audit producer
        # (exec_13159: from_stage=edl sliced audit out → hollow Finished EDL).
        if first == "edl" and "edl_narrative_audit" in seed:
            try:
                if not ctx.artifact_exists("master/edl_narrative_audit.json"):
                    start_idx = min(start_idx, seed.index("edl_narrative_audit"))
            except Exception:
                pass
        # Expanded WS2 O19: reinject incomplete MUST_PRECEDE producers even when
        # they sit *before* the from_stage slice (bounded — once per prepare).
        try:
            from interview_mux.delivery_guardrails import (
                MUST_PRECEDE,
                earliest_incomplete_must_precede,
                seed_stage_complete,
            )
            from interview_mux.delivery_invariants import committed_master_wav
            from interview_mux.done_authority import honest_finalize_seeded

            if not (committed_master_wav(ctx) and honest_finalize_seeded(ctx)):
                for consumer in prior:
                    hole = earliest_incomplete_must_precede(ctx, consumer)
                    if not hole:
                        # Also walk MUST_PRECEDE producers of the resume head.
                        for prod in MUST_PRECEDE.get(str(first or ""), ()):
                            if not seed_stage_complete(ctx, prod) and prod in seed:
                                hole = prod
                                break
                    if hole and hole in seed:
                        start_idx = min(start_idx, seed.index(hole))
        except Exception:
            pass
        forward = set(seed[start_idx:])
        holes = prepare_delivery_guardrails(ctx, forward)
        allow = set(prior) | (set(holes) & forward)
        # Reinject holes that are producers outside the original slice.
        try:
            from interview_mux.delivery_guardrails import earliest_incomplete_must_precede

            for consumer in list(prior):
                hole = earliest_incomplete_must_precede(ctx, consumer)
                if hole and hole in seed and hole not in allow:
                    allow.add(hole)
        except Exception:
            pass
    else:
        holes = prepare_delivery_guardrails(ctx, set(prior) | set(seed))
        allow = set(prior) | set(holes)
    remaining = [s for s in remaining_stages(ctx, phase) if s in allow]
    pinned = constrain_conductor_to_seed_front(ctx, phase, remaining)
    if pinned and pinned != remaining:
        ctx.log(
            f"homunculus {phase}: pinning conductor to seed front {pinned[0]} "
            f"(was {len(remaining)} stage(s))",
            level="info",
            stage=pinned[0],
        )
        remaining = pinned
    write_agenda(ctx, phase, remaining, source="conductor")
    if phase == "delivery":
        from interview_mux.delivery_invariants import committed_master_wav
        from interview_mux.done_authority import honest_finalize_seeded

        if committed_master_wav(ctx) and honest_finalize_seeded(ctx):
            filled = backfill_delivery_holes_after_master(ctx)
            filled = list(filled) + backfill_ship_holes_after_master(ctx)
            if filled:
                from interview_mux.done_authority import may_skip_as_complete

                remaining = [
                    s
                    for s in remaining
                    if s not in filled and not may_skip_as_complete(ctx, s)
                ]
                write_agenda(ctx, phase, remaining, source="conductor")
        try:
            from interview_mux.delivery_guardrails import music_epoch_complete
            from interview_mux.sdp_cross_validate import missing_sdp_asset_wavs

            if delivery_sdp_present(ctx) and music_epoch_complete(ctx):
                from interview_mux.delivery_recovery import MUSIC_BEFORE_MIX
                from interview_mux.done_authority import land_honest

                kept: list[str] = []
                for sid in remaining:
                    if sid == "mmaudio_sfx" and not stage_outputs_present(ctx, sid):
                        try:
                            from interview_mux.mmaudio_asset_qa import heal_mmaudio_qa_wav_parity

                            heal_mmaudio_qa_wav_parity(ctx)
                        except Exception:
                            pass
                    if sid in MUSIC_BEFORE_MIX and music_epoch_complete(ctx):
                        if not land_honest(ctx, sid):
                            heal_or_refuse_mark(ctx, sid, force=True)
                        if land_honest(ctx, sid):
                            ctx.log(
                                f"homunculus keeping {sid} — music epoch complete",
                                level="info",
                                stage=sid,
                            )
                            continue
                        # Hollow after heal → stay on agenda (do not skip work).
                    kept.append(sid)
                if kept != remaining:
                    remaining = kept
                    write_agenda(ctx, phase, remaining, source="conductor")
        except Exception:
            pass
        pending_analysis = pending_analysis_for_delivery(ctx)
        if pending_analysis:
            ctx.log(
                "homunculus delivery blocked on analysis prereqs — "
                f"walking {pending_analysis}",
                level="warning",
                stage=pending_analysis[0],
            )
            walk_seed_agenda(ctx, pending_analysis, reason="delivery_needs_analysis")
            pending_analysis = pending_analysis_for_delivery(ctx)
            if pending_analysis:
                ctx.log(
                    "homunculus delivery still blocked on analysis "
                    f"({', '.join(pending_analysis)}); topic_coverage must wait",
                    level="warning",
                    stage=pending_analysis[0],
                )
                return {
                    "conductor": {
                        "ok": False,
                        "blocked_on_analysis": pending_analysis,
                    },
                    "remaining_after": remaining_stages(ctx, phase),
                }
            remaining = [s for s in remaining_stages(ctx, phase) if s in allow]
            write_agenda(ctx, phase, remaining, source="conductor")
    from interview_mux.homunculus.persona import write_persona
    from interview_mux.homunculus.source_card import build_source_card
    from interview_mux.homunculus.speakers import build_speaker_dossier

    write_persona(ctx)
    try:
        build_source_card(ctx)
    except Exception:
        pass
    if ctx.artifact_exists("understanding/speakers.json") or ctx.artifact_exists("ingest/transcript.json"):
        try:
            build_speaker_dossier(ctx)
        except Exception:
            pass
    conductor_out: dict[str, Any] = {"ok": False, "skipped": True}
    from interview_mux.homunculus.runtime import conductor_owns_control_flow

    deterministic_control = not conductor_owns_control_flow(ctx)
    if remaining and deterministic_control:
        # 0.2.0: stage order is the seed walk. Burn no conductor turns.
        conductor_out = {"ok": True, "skipped": "deterministic_control_plane"}
        append_ledger(
            ctx,
            {
                "kind": "control_plane",
                "identity": "deterministic_control_plane",
                "phase": phase,
                "remaining": list(remaining[:40]),
            },
        )
    elif remaining:
        try:
            from interview_mux.homunculus.loop import run_conductor
            from interview_mux.web.job_progress import notify_batch_plan

            notify_batch_plan(
                ctx.run_id,
                remaining,
                message=(
                    f"Homunculus selecting next {phase} stage "
                    f"({len(remaining)} remaining)"
                ),
            )
            msg = (
                f"Complete the {phase} phase for this tape. Remaining stages (seed order): "
                f"{', '.join(remaining)}. You may skip, reorder, or surgically re-run. "
                f"Select run_stage_* tools. Admit every output. Pack volleys by fact IDs. "
                f"Cite docs via retrieve_canon. Do not invent dialogue. Respect G0. "
                f"Do not skip low_conf_island_scan or connector_fuse_pass unless artifacts exist. "
                f"Do not skip content_context, talking_points_compose, ideal_cuts_propose, "
                f"ideal_cuts_materialize, boundary_detection, episode_structure_compose, "
                f"or chapter_close_hitch unless artifacts exist (hitch only after latch). "
                f"Do not skip transitions, sound_design_plan, edl, assembly_preview, "
                f"listen_delight_audit, mix, junction_snip_qa, master_finalize, or ship "
                f"stages without their on-disk outputs. Prefer MusicGen large for beds. "
                f"Do not run mix until sound_design/assets WAVs exist for every SDP asset_id "
                f"(music_palette_compose → sfx_prompt_craft → mmaudio_sfx). Hard limits apply. "
                f"walk_seed_remainder is optional catch-up only."
            )
            conductor_out = run_conductor(ctx, user_message=msg, client=client)
        except Exception as exc:
            conductor_out = {"ok": False, "error": type(exc).__name__, "message": str(exc)[:400]}
            append_ledger(
                ctx,
                {
                    "kind": "fallback",
                    "identity": "conductor_error",
                    "reason": "conductor_error",
                    "error": conductor_out["message"],
                },
            )
    still = [s for s in remaining_stages(ctx, phase) if s in allow]
    from interview_mux.delivery_invariants import committed_master_wav as _committed_master

    if still:
        ctx.log(
            f"homunculus {phase} incomplete after conductor "
            f"({len(still)} remaining; master QA must wait)",
            level="info",
            stage=still[0],
        )
    if still and phase == "delivery" and not _committed_master(ctx):
        try:
            from interview_mux.thrash_hardening import (
                ensure_phase_a_seal_deadline,
                note_progress_stall,
            )

            ensure_phase_a_seal_deadline(ctx)
            stall = note_progress_stall(ctx, remaining=still, stage=still[0])
            if stall:
                # Soft signal only — do not raise / pause a healthy long producer.
                ctx.log(
                    "delivery progress stall soft signal "
                    f"pin={stall.get('pin')} elapsed={stall.get('elapsed_sec')}s",
                    level="warning",
                    stage=str(stall.get("pin") or still[0]),
                )
        except Exception:
            pass
    if still and remainder_requested(ctx):
        walk_seed_agenda(ctx, still, reason="walk_seed_remainder")
    elif (
        still
        and phase == "delivery"
        and not _committed_master(ctx)
    ):
        # Delivery keeps its protected walk below (candidate filter + audit cap)
        # on every brain, deterministic or not.
        try:
            from interview_mux.delivery_guardrails import filter_delivery_candidates

            filtered = filter_delivery_candidates(ctx, still)
        except Exception:
            filtered = list(still)
        if filtered:
            # A sticky HARD halt owns the only legal resume producer. Walking a
            # multi-stage remainder while it is active hides the halt and burns
            # unrelated producers.
            filtered = _constrain_delivery_walk_for_sticky(ctx, filtered)
            # Cap narrative-audit-only cycles — force music/producer pin after N.
            try:
                from interview_mux.thrash_hardening import (
                    FAIL_CLASS_MUSIC_EPOCH,
                    heal_navigate,
                    narrative_audit_cap_exceeded,
                    note_narrative_audit_cycle,
                    path_to_master_pin,
                    reset_narrative_audit_cycle,
                )

                only_narrative = filtered == ["edl_narrative_audit"] or (
                    len(filtered) == 1 and filtered[0] == "edl_narrative_audit"
                )
                if only_narrative:
                    note_narrative_audit_cycle(ctx)
                    if narrative_audit_cap_exceeded(ctx):
                        try:
                            pin = path_to_master_pin(ctx)
                        except Exception:
                            nav = heal_navigate(ctx, intent=FAIL_CLASS_MUSIC_EPOCH)
                            pin = nav["from_stage"]
                        raise RuntimeError(
                            "Delivery incomplete after conductor — "
                            f"edl_narrative_audit thrash cap; resume={pin}"
                        )
                else:
                    reset_narrative_audit_cycle(ctx)
            except RuntimeError:
                raise
            except Exception:
                pass
            ctx.log(
                "homunculus delivery walking remaining seed to master "
                f"({len(filtered)} stage(s))",
                level="warning",
                stage=filtered[0],
            )
            walk_seed_agenda(ctx, filtered, reason="delivery_walk_to_master")
        elif still:
            # T2: filter empty with pre-master holes — never silent complete.
            # Prefer remutate / seed-front pin when remutate active (Wave 8).
            try:
                from interview_mux.delivery_invariants import active_remutate_stages
                from interview_mux.thrash_hardening import (
                    FAIL_CLASS_DELIVERY_BLOCKED,
                    canonical_resume_pin,
                )

                rem = active_remutate_stages(ctx)
                if rem:
                    pin = next(
                        (s for s in still if s in rem),
                        next(iter(rem), still[0]),
                    )
                    # Force seed-front constrain rather than silent complete.
                    pinned = constrain_conductor_to_seed_front(ctx, phase, still)
                    if pinned:
                        pin = pinned[0]
                else:
                    pin = canonical_resume_pin(
                        ctx, FAIL_CLASS_DELIVERY_BLOCKED, hint=still[0]
                    )
            except Exception:
                pin = still[0]
            try:
                from interview_mux.execution_status import (
                    should_wait_incomplete_after_conductor,
                )

                wait_row = should_wait_incomplete_after_conductor(
                    ctx, pin=pin, remaining=still
                )
                if wait_row is not None:
                    lease = str(wait_row.get("lease_stage") or pin or still[0])
                    # HAU: ESR wait must not walk MusicGen while only preview
                    # assembly exists — prefer speech-first mix to seat.
                    try:
                        from interview_mux.mix_junction_seat import (
                            music_admit_block_reason,
                            next_delivery_seat,
                        )

                        if lease in MUSIC_REQUIRES_ASSEMBLY and music_admit_block_reason(
                            ctx
                        ):
                            pin = next_delivery_seat(ctx)
                            if pin:
                                lease = pin
                    except Exception:
                        pass
                    ctx.log(
                        "homunculus delivery ESR wait "
                        f"({wait_row.get('why')}); resume={lease}",
                        level="warning",
                        stage=lease,
                    )
                    walk_seed_agenda(
                        ctx, [lease], reason="esr_wait_incomplete_after_conductor"
                    )
                    return {
                        "conductor": conductor_out,
                        "remaining_after": [
                            s for s in remaining_stages(ctx, phase) if s in allow
                        ],
                        "esr_wait": True,
                        "resume": lease,
                    }
            except Exception:
                pass
            raise RuntimeError(
                "Delivery incomplete after conductor — remaining stages "
                f"(filter empty): {', '.join(still[:12])}; resume={pin}"
            )
    elif still and phase == "delivery" and ctx.final_path("master", "master.wav").is_file():
        # Committed master only — pending finalize WAVs must not look shipped
        # (exec_10066: orphaned .pending_writes/master_finalize/master/master.wav
        # skipped junction via ship_path_ready + filter).
        pmq: dict[str, Any] | None = None
        pmq_missing = not ctx.artifact_exists("master/post_master_quality.json")
        if not pmq_missing:
            loaded = ctx.read_json("master/post_master_quality.json")
            pmq = loaded if isinstance(loaded, dict) else None
        pmq_failed = pmq_missing or bool(
            pmq
            and (
                pmq.get("status") == "fail"
                or pmq.get("publish_allowed") is False
            )
        )
        if pmq_failed:
            pre_ship = [s for s in still if s not in SHIP_AFTER_MASTER]
            if pre_ship:
                from interview_mux.v2.config import DELIVERY_ORDER

                delivery_only = set(DELIVERY_ORDER)
                pre_ship = [s for s in pre_ship if s in delivery_only]
                if ctx.artifact_exists("master/assembly.wav") and "mix" in DELIVERY_ORDER:
                    mix_idx = DELIVERY_ORDER.index("mix")
                    pre_ship = [s for s in pre_ship if s in DELIVERY_ORDER[mix_idx:]]
            if pre_ship:
                ctx.log(
                    "homunculus delivery remastering after failed post-master quality "
                    f"({len(pre_ship)} stage(s))",
                    level="warning",
                    stage=pre_ship[0],
                )
                walk_seed_agenda(ctx, pre_ship, reason="delivery_walk_unpublishable_master")
        else:
            ship = ship_after_master_remaining(ctx)
            if ship:
                ctx.log(
                    "homunculus delivery walking remaining ship stages "
                    f"({len(ship)} stage(s))",
                    level="warning",
                    stage=ship[0],
                )
                walk_seed_agenda(ctx, ship, reason="delivery_walk_to_publish")
    elif phase == "analysis":
        pending = pending_analysis_for_delivery(ctx)
        prereq_ids = {s for s, _ in DELIVERY_ANALYSIS_PREREQS}
        if pending and any(s in prereq_ids for s in still):
            ctx.log(
                "homunculus analysis filling delivery prereqs — "
                f"walking {still[:12]}",
                level="warning",
                stage=still[0],
            )
            walk_seed_agenda(ctx, still, reason="analysis_fill_delivery_prereqs")
        elif still and deterministic_control:
            # 0.2.0: no conductor chose a stage, so the walk owns analysis
            # progress. Gate breaks (G0 / voice reference) live in walk_seed_agenda.
            ctx.log(
                f"deterministic control plane walking analysis ({len(still)} stage(s))",
                level="info",
                stage=still[0],
            )
            walk_seed_agenda(ctx, still, reason="deterministic_control_plane")
    return {"conductor": conductor_out, "remaining_after": [s for s in remaining_stages(ctx, phase) if s in allow]}

from __future__ import annotations

import hashlib
import wave
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
import json

import numpy as np

from interview_mux.acoustic_profile import compact_for_volley, load_profile, pacing_one_liner
from interview_mux.config import merged_config
from interview_mux.stage_input_helpers import attach_disfluency_context
from interview_mux.stage_input_helpers import transcript_quality_for_ctx
from interview_mux.operator_trace import logged_step
from interview_mux.run_context import RunContext
from interview_mux.transcript_sampling import stratified_transcript_samples_from_words
from interview_mux.speaker_role_evidence import build_speaker_role_evidence
from interview_mux.conversation_context import enrich_speakers_artifact
from interview_mux.value_analysis.extract import (
    maybe_auto_extract_value_features,
    maybe_enqueue_orchestration_investigations,
)
from interview_mux.production_profile import prompt_variant
from interview_mux.source_topology import attach_adaptation_to_payload
from interview_mux.artifact_completeness import make_stage_persist
from interview_mux.stage_completion import heal_or_refuse_mark
from interview_mux.stages.analysis_stage import (
    run_analysis_llm_stage,
    sync_content_brief_reanchor_to_state,
    sync_content_brief_to_state,
    sync_speakers_to_state,
)


def _speaker_roles_sample_chars() -> int:
    ctx_cfg = (merged_config().get("analysis") or {}).get("context") or {}
    return int(ctx_cfg.get("speaker_roles_sample_chars", 24000))


def run_speaker_roles(ctx: RunContext) -> None:
    def build_input(c: RunContext) -> dict:
        transcript = c.read_json("transcript/full.json")
        speakers = c.read_json("transcript/speakers.json")
        words = transcript.get("words") or []
        sample_chars = _speaker_roles_sample_chars()
        if words:
            samples = stratified_transcript_samples_from_words(
                words,
                total_chars=sample_chars,
            )
        else:
            from interview_mux.transcript_sampling import stratified_transcript_samples

            samples = stratified_transcript_samples(
                transcript.get("text", ""),
                total_chars=sample_chars,
            )
        from interview_mux.source_topology import _speaker_talk_stats

        speakers_input = {"speakers": speakers.get("speakers") or speakers}
        talk_stats = _speaker_talk_stats(transcript, speakers_input)
        evidence_in: dict[str, Any] = {"transcript_samples": samples, "speakers": speakers}
        if c.artifact_exists("transcript/diarization_repairs.json"):
            try:
                evidence_in["diarization_repairs"] = c.read_json(
                    "transcript/diarization_repairs.json"
                )
            except Exception:
                pass
        return {
            "transcript_samples": samples,
            "speakers": speakers,
            "speaker_talk_stats": talk_stats,
            **build_speaker_role_evidence(evidence_in),
        }

    base_persist = make_stage_persist("understanding/speakers.json", "speaker_roles")

    def persist(c: RunContext, artifacts: dict) -> None:
        base_persist(c, enrich_speakers_artifact(c, artifacts))
        try:
            from interview_mux.speaker_role_evidence import enrich_content_brief_from_evidence

            enrich_content_brief_from_evidence(c)
        except Exception:
            pass

    def _sync_full(c: RunContext, artifacts: dict) -> None:
        sync_speakers_to_state(c, artifacts)

    with logged_step("speaker_roles/llm_stage", ctx=ctx, stage="speaker_roles"):
        run_analysis_llm_stage(
            ctx,
            "speaker_roles",
            "understanding/speaker-roles.system.txt",
            build_input,
            persist,
            sync_fn=_sync_full,
        )


def _content_context_base_payload(c: RunContext, transcript_text: str) -> dict[str, Any]:
    # Contract hard: topology (SEED_ORDER). Refuse before LLM — no thin packet (CC-B1).
    if not c.artifact_exists("understanding/source_topology.json"):
        raise RuntimeError("source_topology required before content_context")
    payload: dict[str, Any] = {"transcript": transcript_text}
    if c.artifact_exists("understanding/speakers.json"):
        payload["speakers"] = c.read_json("understanding/speakers.json")
    quality = transcript_quality_for_ctx(c)
    if quality:
        payload["transcript_quality"] = quality
    profile = load_profile(c)
    if profile:
        payload["source_acoustic_pacing"] = pacing_one_liner(profile)
    from interview_mux.interview_spine.compact import attach_spine_to_payload

    attach_spine_to_payload(c, payload, "content_context")
    return attach_disfluency_context(attach_adaptation_to_payload(c, payload), c)


def _talking_points_base_payload(c: RunContext, transcript_text: str) -> dict[str, Any]:
    # Contract hard: content_brief (SEED_ORDER). Refuse before LLM — no thin packet (TPC-B1).
    if not c.artifact_exists("understanding/content_brief.json"):
        raise RuntimeError("content_brief required before talking_points_compose")
    payload: dict[str, Any] = {
        "transcript_text": transcript_text,
        "transcript_samples": {"opening": transcript_text},
        "content_brief": c.read_json("understanding/content_brief.json"),
    }
    if c.artifact_exists("understanding/speakers.json"):
        payload["speakers"] = c.read_json("understanding/speakers.json")
    quality = transcript_quality_for_ctx(c)
    if quality:
        payload["transcript_quality"] = quality
    from interview_mux.interview_spine.compact import attach_spine_to_payload

    attach_spine_to_payload(c, payload, "talking_points_compose")
    return attach_disfluency_context(attach_adaptation_to_payload(c, payload), c)


def run_content_context(ctx: RunContext) -> None:
    from interview_mux.llm_simple import run_llm_stage_simple
    from interview_mux.transcript_shards import (
        build_transcript_shards,
        merge_content_brief_artifacts,
        needs_transcript_sharding,
    )

    persist = make_stage_persist("understanding/content_brief.json", "content_context")
    prompt_rel = prompt_variant("understanding/content-context.system.txt", ctx)

    transcript = ctx.read_json("transcript/full.json")
    full_text = str(transcript.get("text") or "")
    words = [w for w in (transcript.get("words") or []) if isinstance(w, dict)]

    # Match contract hard topology before any OpenAI call (CC-B1).
    if not ctx.artifact_exists("understanding/source_topology.json"):
        raise RuntimeError("source_topology required before content_context")

    with logged_step("content_context/llm_stage", ctx=ctx, stage="content_context"):
        if not needs_transcript_sharding(full_text):

            def build_input(c: RunContext) -> dict:
                t = c.read_json("transcript/full.json")
                return _content_context_base_payload(c, str(t.get("text") or ""))

            run_analysis_llm_stage(
                ctx,
                "content_context",
                prompt_rel,
                build_input,
                persist,
                sync_fn=lambda c, a: sync_content_brief_to_state(c, a),
            )
        else:
            shards = build_transcript_shards(full_text, words=words or None)
            ctx.log(
                f"content_context proactive batch: {len(full_text)} chars → "
                f"{len(shards)} shard(s)",
                level="info",
                stage="content_context",
                action_id="content_context.proactive_batch",
                detail={"chars": len(full_text), "shards": len(shards)},
            )

            def _noop_persist(_c: RunContext, _artifacts: dict) -> None:
                return None

            parts: list[dict[str, Any]] = []
            for shard in shards:

                def build_batch(
                    c: RunContext,
                    *,
                    _shard=shard,
                ) -> dict:
                    payload = _content_context_base_payload(c, _shard.text)
                    payload["_transcript_shard"] = _shard.meta()
                    return payload

                ctx.log(
                    f"content_context shard {shard.shard_index}/{shard.shard_total} "
                    f"({len(shard.text)} chars)",
                    level="action",
                    stage="content_context",
                    action_id="content_context.shard",
                )
                envelope = run_llm_stage_simple(
                    ctx,
                    "content_context",
                    prompt_rel,
                    build_batch,
                    _noop_persist,
                    auto_complete=False,
                )
                arts = envelope.get("artifacts") if isinstance(envelope.get("artifacts"), dict) else {}
                if isinstance(arts, dict) and arts:
                    parts.append(arts)

            if not parts:
                raise RuntimeError("content_context proactive batch produced no shard artifacts")
            merged = merge_content_brief_artifacts(parts)
            persist(ctx, merged)
            sync_content_brief_to_state(ctx, merged)
            from interview_mux.stage_completion import heal_or_raise

            heal_or_raise(ctx, "content_context")
            ctx.log(
                f"content_context batched complete ({len(parts)} shards merged)",
                level="success",
                stage="content_context",
                action_id="content_context.proactive_batch_complete",
            )

    from interview_mux.delivery_guardrails import seed_stage_complete

    if seed_stage_complete(ctx, "content_context"):
        with logged_step("content_context/post_hooks", ctx=ctx, stage="content_context"):
            maybe_auto_extract_value_features(ctx)
            maybe_enqueue_orchestration_investigations(ctx)
            from interview_mux.coherence import maybe_run_coherence_analysis

            maybe_run_coherence_analysis(ctx, phase="post_content_context")


def run_talking_points_compose(ctx: RunContext) -> None:
    """Holistic talking-point plan before ideal cut windows."""
    from interview_mux.ideal_cuts import ideal_cuts_cfg
    from interview_mux.llm_simple import run_llm_stage_simple
    from interview_mux.transcript_shards import (
        build_transcript_shards,
        merge_talking_points_artifacts,
        needs_transcript_sharding,
    )

    if not ideal_cuts_cfg().get("enable", True):
        ctx.write_json(
            "understanding/talking_points.json",
            {
                "strategy_summary": "ideal_cuts disabled — legacy boundary path remains authoritative.",
                "through_line": "Legacy segmentation",
                "talking_points": [
                    {
                        "talking_point_id": "tp_disabled",
                        "title": "ideal_cuts disabled",
                        "importance": "optional",
                        "why_it_matters": "analysis.ideal_cuts.enable=false",
                    }
                ],
                "warnings": ["analysis.ideal_cuts.enable=false"],
            },
            stage_key="talking_points_compose",
        )
        heal_or_refuse_mark(ctx, "talking_points_compose", force=True)
        return

    base_tp_persist = make_stage_persist(
        "understanding/talking_points.json", "talking_points_compose"
    )

    def persist(c: RunContext, artifacts: dict[str, Any]) -> None:
        from interview_mux.ideal_cuts import spread_talking_point_time_hints
        from interview_mux.interview_duration_policy import transcript_duration_ms

        doc = artifacts if isinstance(artifacts, dict) else {}
        duration_ms = int(transcript_duration_ms(c) or 0)
        base_tp_persist(c, spread_talking_point_time_hints(doc, duration_ms))

    prompt_rel = "understanding/talking-points-compose.system.txt"
    transcript = ctx.read_json("transcript/full.json")
    full_text = str(transcript.get("text") or "")
    words = [w for w in (transcript.get("words") or []) if isinstance(w, dict)]

    # Match contract hard content_brief before any OpenAI call (TPC-B1).
    if not ctx.artifact_exists("understanding/content_brief.json"):
        raise RuntimeError("content_brief required before talking_points_compose")

    with logged_step(
        "talking_points_compose/llm_stage", ctx=ctx, stage="talking_points_compose"
    ):
        if not needs_transcript_sharding(full_text):

            def build_input(c: RunContext) -> dict:
                t = c.read_json("transcript/full.json")
                return _talking_points_base_payload(c, str(t.get("text") or ""))

            run_analysis_llm_stage(
                ctx,
                "talking_points_compose",
                prompt_rel,
                build_input,
                persist,
            )
        else:
            shards = build_transcript_shards(full_text, words=words or None)
            ctx.log(
                f"talking_points_compose proactive batch: {len(full_text)} chars → "
                f"{len(shards)} shard(s)",
                level="info",
                stage="talking_points_compose",
                action_id="talking_points.proactive_batch",
                detail={"chars": len(full_text), "shards": len(shards)},
            )

            def _noop_persist(_c: RunContext, _artifacts: dict) -> None:
                return None

            parts: list[dict[str, Any]] = []
            for shard in shards:

                def build_batch(
                    c: RunContext,
                    *,
                    _shard=shard,
                ) -> dict:
                    payload = _talking_points_base_payload(c, _shard.text)
                    payload["_transcript_shard"] = _shard.meta()
                    return payload

                ctx.log(
                    f"talking_points_compose shard {shard.shard_index}/{shard.shard_total} "
                    f"({len(shard.text)} chars)",
                    level="action",
                    stage="talking_points_compose",
                    action_id="talking_points.shard",
                )
                envelope = run_llm_stage_simple(
                    ctx,
                    "talking_points_compose",
                    prompt_rel,
                    build_batch,
                    _noop_persist,
                    auto_complete=False,
                )
                arts = envelope.get("artifacts") if isinstance(envelope.get("artifacts"), dict) else {}
                if isinstance(arts, dict) and arts:
                    parts.append(arts)

            if not parts:
                raise RuntimeError(
                    "talking_points_compose proactive batch produced no shard artifacts"
                )
            merged = merge_talking_points_artifacts(parts)
            persist(ctx, merged)
            from interview_mux.stage_completion import heal_or_raise

            heal_or_raise(ctx, "talking_points_compose")
            ctx.log(
                f"talking_points_compose batched complete ({len(parts)} shards, "
                f"{len(merged.get('talking_points') or [])} points)",
                level="success",
                stage="talking_points_compose",
                action_id="talking_points.proactive_batch_complete",
            )


def run_ideal_cuts_propose(ctx: RunContext) -> None:
    """Propose timed native windows for each talking point."""
    from interview_mux.ideal_cuts import (
        cut_span_coverage_ratio,
        ideal_cuts_cfg,
        redistribute_clustered_cuts,
    )
    from interview_mux.interview_duration_policy import transcript_duration_ms
    from interview_mux.stage_input_helpers import compact_transcript_for_boundaries

    if not ideal_cuts_cfg().get("enable", True):
        ctx.write_json(
            "understanding/ideal_cuts.json",
            {
                "cuts": [
                    {
                        "cut_id": "cut_disabled",
                        "talking_point_id": "tp_disabled",
                        "start_ms": 0,
                        "end_ms": 3000,
                        "priority": "optional",
                        "rationale": "analysis.ideal_cuts.enable=false — placeholder only",
                    }
                ],
                "warnings": ["analysis.ideal_cuts.enable=false"],
            },
            stage_key="ideal_cuts_propose",
        )
        heal_or_refuse_mark(ctx, "ideal_cuts_propose", force=True)
        return

    def build_input(c: RunContext) -> dict:
        if not c.artifact_exists("understanding/talking_points.json"):
            raise RuntimeError("talking_points required before ideal_cuts_propose")
        payload: dict[str, Any] = {
            "talking_points": c.read_json("understanding/talking_points.json"),
            "transcript": compact_transcript_for_boundaries(
                c.read_json("transcript/full.json")
            ),
        }
        if c.artifact_exists("understanding/content_brief.json"):
            payload["content_brief"] = c.read_json("understanding/content_brief.json")
        if c.artifact_exists("understanding/speakers.json"):
            payload["speakers"] = c.read_json("understanding/speakers.json")
        quality = transcript_quality_for_ctx(c)
        if quality:
            payload["transcript_quality"] = quality
        from interview_mux.stage_enrichment import pause_ladder_hints

        payload["pause_ladder_hints"] = pause_ladder_hints(c)
        return attach_adaptation_to_payload(c, payload)

    base_persist = make_stage_persist("understanding/ideal_cuts.json", "ideal_cuts_propose")

    def persist(c: RunContext, artifacts: dict[str, Any]) -> None:
        duration_ms = int(transcript_duration_ms(c) or 0)
        floor = float(ideal_cuts_cfg().get("min_span_coverage_ratio") or 0.45)
        doc = artifacts if isinstance(artifacts, dict) else {}
        # Only enforce span distribution on long interviews (short tapes can cluster).
        if duration_ms >= 900_000:
            ratio = cut_span_coverage_ratio(doc, duration_ms)
            if ratio < floor:
                transcript = (
                    c.read_json("transcript/full.json")
                    if c.artifact_exists("transcript/full.json")
                    else {}
                )
                tps = (
                    c.read_json("understanding/talking_points.json")
                    if c.artifact_exists("understanding/talking_points.json")
                    else {}
                )
                doc = redistribute_clustered_cuts(
                    doc,
                    duration_ms,
                    talking_points=tps if isinstance(tps, dict) else {},
                    transcript=transcript if isinstance(transcript, dict) else {},
                    floor=floor,
                )
                ratio = cut_span_coverage_ratio(doc, duration_ms)
                if ratio < floor:
                    # Span coverage is a quality judgement, never a refusal.
                    # Redistribution only opens whole-sentence windows, so a
                    # probe point inside a short sentence adds nothing.
                    c.log(
                        f"ideal_cuts_propose span coverage {ratio:.3f} < min {floor:.3f} "
                        f"after redistribution; continuing with the proposed cuts",
                        level="warning",
                        stage="ideal_cuts_propose",
                    )
                c.log(
                    f"ideal_cuts_propose redistributed clustered cuts to span {ratio:.3f}",
                    level="warning",
                    stage="ideal_cuts_propose",
                )
        base_persist(c, doc)

    with logged_step(
        "ideal_cuts_propose/llm_stage", ctx=ctx, stage="ideal_cuts_propose"
    ):
        run_analysis_llm_stage(
            ctx,
            "ideal_cuts_propose",
            "understanding/ideal-cuts-propose.system.txt",
            build_input,
            persist,
        )


def run_content_brief_reanchor(ctx: RunContext) -> None:
    from interview_mux.topic_tag_bootstrap import bootstrap_manifest_topic_tags

    with logged_step("content_brief_reanchor/bootstrap_tags", ctx=ctx, stage="content_brief_reanchor"):
        patched = bootstrap_manifest_topic_tags(ctx)
        if patched:
            ctx.log(
                f"Topic-tag bootstrap applied to {patched} untagged segment(s) before re-anchor.",
                level="info",
                stage="content_brief_reanchor",
                action_id="reanchor.topic_tag_bootstrap",
            )

    def build_input(c: RunContext) -> dict:
        from interview_mux.stage_enrichment import compact_manifest_for_volley

        manifest = c.read_json("segments/manifest.json")
        compact_manifest = compact_manifest_for_volley(manifest if isinstance(manifest, dict) else {})
        payload: dict[str, Any] = {
            "content_brief": c.read_json("understanding/content_brief.json"),
            "segments": compact_manifest,
            "speakers": c.read_json("understanding/speakers.json"),
        }
        if c.artifact_exists("segments/boundaries.json"):
            # Boundaries are pre-vernacular; keep ids only to avoid duplicating timeline bulk.
            boundaries = c.read_json("segments/boundaries.json")
            if isinstance(boundaries, dict):
                rows = []
                for b in boundaries.get("boundaries") or []:
                    if not isinstance(b, dict):
                        continue
                    rows.append(
                        {
                            "segment_id": b.get("segment_id"),
                            "start_ms": b.get("start_ms"),
                            "end_ms": b.get("end_ms"),
                            "speaker_id": b.get("speaker_id"),
                        }
                    )
                payload["boundaries"] = {"boundaries": rows}
        from interview_mux.coherence import attach_coherence_summary

        attach_coherence_summary(payload, c, "content_brief_reanchor")
        from interview_mux.conversation_context import attach_conversation_context
        from interview_mux.interview_spine.compact import attach_spine_to_payload

        excerpts: list[dict[str, Any]] = []
        for row in (compact_manifest.get("segments") if isinstance(compact_manifest, dict) else None) or []:
            if not isinstance(row, dict):
                continue
            text = str(row.get("text") or row.get("excerpt") or "").strip()
            if not text:
                continue
            excerpts.append(
                {
                    "segment_id": row.get("segment_id"),
                    "topic": row.get("topic"),
                    "excerpt": text[:240],
                }
            )
            if len(excerpts) >= 40:
                break
        if excerpts:
            payload["topic_excerpts"] = excerpts
        payload = attach_conversation_context(c, payload, "content_brief_reanchor")
        attach_spine_to_payload(c, payload, "content_brief_reanchor")
        return attach_disfluency_context(payload, c)

    inner_persist = make_stage_persist("understanding/content_brief.json", "content_brief_reanchor")

    def persist(c: RunContext, artifacts: dict) -> None:
        inner_persist(c, artifacts)
        from interview_mux.artifact_completeness import artifact_status_for_stage

        st = artifact_status_for_stage(
            "understanding/content_brief.json", c, "content_brief_reanchor"
        )
        if st != "complete":
            raise RuntimeError(
                f"content_brief_reanchor must persist a complete brief (status={st})"
            )
    prompt_rel = prompt_variant("understanding/content-brief-reanchor.system.txt", ctx)

    with logged_step("content_brief_reanchor/llm_stage", ctx=ctx, stage="content_brief_reanchor"):
        run_analysis_llm_stage(
            ctx,
            "content_brief_reanchor",
            prompt_rel,
            build_input,
            persist,
            sync_fn=lambda c, a: sync_content_brief_reanchor_to_state(c, a),
        )
    from interview_mux.delivery_guardrails import seed_stage_complete

    if seed_stage_complete(ctx, "content_brief_reanchor"):
        with logged_step("content_brief_reanchor/post_hooks", ctx=ctx, stage="content_brief_reanchor"):
            from interview_mux.coherence import maybe_run_coherence_analysis

            maybe_run_coherence_analysis(ctx, phase="post_reanchor")


def run_source_acoustic_profile(ctx: RunContext) -> None:
    transcript = ctx.read_json("transcript/full.json")
    normalized_wav = ctx.read_artifact_path(
        "ingest/normalized.wav",
        stage="source_acoustic_profile",
        label="normalized interview audio",
    )

    preclean = ctx.read_path("preclean", "isolated.wav")
    analysis_wav = preclean if preclean.is_file() else normalized_wav

    with logged_step("source_acoustic_profile/derive_metrics", ctx=ctx, stage="source_acoustic_profile"):
        pacing = _derive_pacing(transcript)
        energy = _derive_energy_profile(analysis_wav)
        source_music_risk = _derive_source_music_risk(pacing, energy)
        mix_contract = _derive_mix_contract(pacing, source_music_risk)

    prior_overrides: dict[str, Any] = {}
    if ctx.artifact_exists("understanding/source_acoustic_profile.json"):
        prior = ctx.read_json("understanding/source_acoustic_profile.json")
        if isinstance(prior, dict) and isinstance(prior.get("operator_overrides"), dict):
            prior_overrides = prior["operator_overrides"]

    profile = {
        "schema_version": 1,
        "derived_from": _derived_from(transcript, normalized_wav, preclean if preclean.is_file() else None),
        "pacing": pacing,
        "energy": energy,
        "prosody_summary": _derive_prosody_summary(pacing, ctx),
        "source_music_risk": source_music_risk,
        "mix_contract": mix_contract,
        "prompt_tokens": _derive_prompt_tokens(mix_contract, pacing, energy),
        "placement_hints": _placement_hints(pacing),
        "operator_overrides": prior_overrides,
    }
    with logged_step("source_acoustic_profile/write", ctx=ctx, stage="source_acoustic_profile"):
        ctx.write_json("understanding/source_acoustic_profile.json", profile)
    ctx.log(
        f"Source acoustic profile complete — pace={pacing.get('pace_class', 'unknown')}.",
        level="success",
        stage="source_acoustic_profile",
    )
    ctx.mark_done("source_acoustic_profile")
    try:
        from interview_mux.source_readiness import maybe_auto_dismiss_preclean, write_source_readiness

        write_source_readiness(ctx, stage="source_acoustic_profile")
        maybe_auto_dismiss_preclean(ctx, checkpoint="before_ingest")
    except Exception as exc:  # noqa: BLE001
        ctx.log(f"source_readiness refresh failed: {exc}", level="warning", stage="source_acoustic_profile")



def _derived_from(transcript: dict[str, Any], normalized_wav: Path, preclean_wav: Path | None) -> dict[str, Any]:
    return {
        "normalized_wav": "ingest/normalized.wav",
        "normalized_wav_sha256": _sha256(normalized_wav),
        "transcript": "transcript/full.json",
        "transcript_sha256": _sha256_json(transcript),
        "preclean_isolated": "preclean/isolated.wav" if preclean_wav else None,
        "preclean_isolated_sha256": _sha256(preclean_wav) if preclean_wav else None,
        "computed_at": datetime.now(timezone.utc).isoformat(),
        "stage": "source_acoustic_profile",
    }


def _derive_pacing(transcript: dict[str, Any]) -> dict[str, Any]:
    words = [w for w in (transcript.get("words") or []) if isinstance(w, dict)]
    words = [w for w in words if isinstance(w.get("start_ms"), (int, float)) and isinstance(w.get("end_ms"), (int, float))]
    words.sort(key=lambda w: float(w.get("start_ms", 0)))

    if not words:
        return {
            "global_wpm": 0,
            "wpm_by_quartile": [0, 0, 0, 0],
            "pause_p50_ms": 0,
            "pause_p90_ms": 0,
            "phrase_boundary_density_per_min": 0.0,
            "overlap_proxy": 0.0,
            "speech_active_ratio": 0.0,
            "pace_class": "calm",
        }

    word_count = len(words)
    first_start = float(words[0]["start_ms"])
    last_end = float(words[-1]["end_ms"])
    duration_ms = max(1.0, last_end - first_start)

    speech_ms = 0.0
    pauses_ms: list[float] = []
    pause_threshold_ms = 250.0
    phrase_boundary_threshold_ms = 400.0
    long_gap_exclusion_ms = 2000.0
    speech_active_for_wpm_ms = 0.0

    prev_end = first_start
    for w in words:
        start = float(w["start_ms"])
        end = float(w["end_ms"])
        speech_ms += max(0.0, end - start)
        gap = max(0.0, start - prev_end)
        if gap >= pause_threshold_ms:
            pauses_ms.append(gap)
        if gap < long_gap_exclusion_ms:
            speech_active_for_wpm_ms += gap
        speech_active_for_wpm_ms += max(0.0, end - start)
        prev_end = end

    speech_active_minutes = max(1e-6, speech_active_for_wpm_ms / 60000.0)
    global_wpm = int(round(word_count / speech_active_minutes))
    pause_p50 = int(round(float(np.percentile(pauses_ms, 50, method="linear")))) if pauses_ms else 0
    pause_p90 = int(round(float(np.percentile(pauses_ms, 90, method="linear")))) if pauses_ms else 0
    phrase_boundaries = sum(1 for p in pauses_ms if p >= phrase_boundary_threshold_ms)
    phrase_density = round(phrase_boundaries / speech_active_minutes, 2)
    speech_active_ratio = round(min(1.0, speech_ms / duration_ms), 3)

    quartile_wpm = _quartile_wpm(words, first_start, duration_ms)
    overlap_proxy = _overlap_proxy(transcript.get("segments") or [])
    pace_class = _pace_class(global_wpm, pause_p50, overlap_proxy, speech_active_ratio)

    return {
        "global_wpm": global_wpm,
        "wpm_by_quartile": quartile_wpm,
        "pause_p50_ms": pause_p50,
        "pause_p90_ms": pause_p90,
        "phrase_boundary_density_per_min": phrase_density,
        "overlap_proxy": overlap_proxy,
        "speech_active_ratio": speech_active_ratio,
        "pace_class": pace_class,
    }


def _quartile_wpm(words: list[dict[str, Any]], first_start_ms: float, duration_ms: float) -> list[int]:
    windows: list[list[dict[str, Any]]] = [[], [], [], []]
    quartile_ms = duration_ms / 4.0
    for w in words:
        idx = int((float(w["start_ms"]) - first_start_ms) / max(1.0, quartile_ms))
        idx = max(0, min(3, idx))
        windows[idx].append(w)

    out: list[int] = []
    for idx, bucket in enumerate(windows):
        if not bucket:
            out.append(0)
            continue
        window_start = first_start_ms + idx * quartile_ms
        window_end = window_start + quartile_ms
        speech_ms = sum(max(0.0, float(w["end_ms"]) - float(w["start_ms"])) for w in bucket)
        minutes = max(1e-6, speech_ms / 60000.0, (window_end - window_start) / 60000.0 * 0.5)
        out.append(int(round(len(bucket) / minutes)))
    return out


def _overlap_proxy(segments: list[Any]) -> float:
    parsed: list[tuple[float, float]] = []
    for seg in segments:
        if not isinstance(seg, dict):
            continue
        try:
            parsed.append((float(seg.get("start_time", 0.0)) * 1000.0, float(seg.get("end_time", 0.0)) * 1000.0))
        except (TypeError, ValueError):
            continue
    parsed.sort(key=lambda x: x[0])
    if len(parsed) < 2:
        return 0.0
    overlaps = 0
    for idx in range(1, len(parsed)):
        prev = parsed[idx - 1]
        cur = parsed[idx]
        if cur[0] < prev[1]:
            overlaps += 1
    return round(overlaps / max(1, len(parsed) - 1), 3)


def _pace_class(global_wpm: int, pause_p50_ms: int, overlap_proxy: float, speech_active_ratio: float) -> str:
    if global_wpm >= 165 and (pause_p50_ms < 320 or overlap_proxy >= 0.2):
        return "dense"
    if global_wpm >= 145 or (pause_p50_ms < 420 and speech_active_ratio >= 0.72):
        return "brisk"
    if global_wpm <= 115 and pause_p50_ms >= 650:
        return "calm"
    return "conversational"


def _derive_energy_profile(wav_path: Path) -> dict[str, Any]:
    with wave.open(str(wav_path), "rb") as wf:
        frames = wf.getnframes()
        rate = wf.getframerate()
        channels = wf.getnchannels()
        sample_width = wf.getsampwidth()
        raw = wf.readframes(frames)

    if sample_width not in (1, 2, 4):
        raise ValueError(f"Unsupported PCM sample width: {sample_width}")

    dtype = {1: np.uint8, 2: np.int16, 4: np.int32}[sample_width]
    samples = np.frombuffer(raw, dtype=dtype)
    if sample_width == 1:
        samples = samples.astype(np.int16) - 128
    if channels > 1:
        samples = samples.reshape(-1, channels).mean(axis=1)
    samples = samples.astype(np.float64)

    peak = float(np.max(np.abs(samples))) if samples.size else 1.0
    if peak <= 0:
        peak = 1.0

    win_size = max(1, int(rate * 0.4))
    usable = (samples.size // win_size) * win_size
    windows = samples[:usable].reshape(-1, win_size) if usable > 0 else np.empty((0, win_size))
    if windows.size == 0:
        dbfs = np.array([-80.0])
    else:
        rms = np.sqrt(np.mean(np.square(windows), axis=1))
        dbfs = 20.0 * np.log10(np.maximum(rms / peak, 1e-8))

    p10 = float(np.percentile(dbfs, 10, method="linear"))
    p50 = float(np.percentile(dbfs, 50, method="linear"))
    p90 = float(np.percentile(dbfs, 90, method="linear"))
    silence_ratio = float(np.mean(dbfs <= -45.0))

    room_timbre_hint = _room_timbre_hint(samples, rate)
    return {
        "loudness_p10_lufs": round(p10, 2),
        "loudness_p50_lufs": round(p50, 2),
        "loudness_p90_lufs": round(p90, 2),
        "dynamic_range_db": round(max(0.0, p90 - p10), 2),
        "silence_ratio": round(silence_ratio, 3),
        "room_timbre_hint": room_timbre_hint,
    }


def _room_timbre_hint(samples: np.ndarray, sample_rate: int) -> str:
    if samples.size == 0:
        return "dry_close_mic_neutral_mid"
    n = min(samples.size, sample_rate * 20)
    clip = samples[:n]
    spectrum = np.fft.rfft(clip)
    freqs = np.fft.rfftfreq(n, d=1.0 / sample_rate)
    power = np.abs(spectrum)
    low = float(np.sum(power[(freqs >= 20) & (freqs < 250)]))
    mid = float(np.sum(power[(freqs >= 250) & (freqs < 2000)]))
    high = float(np.sum(power[(freqs >= 2000) & (freqs < 7000)]))
    total = max(1e-6, low + mid + high)
    low_r, mid_r, high_r = low / total, mid / total, high / total
    if low_r > 0.42:
        return "dry_close_mic_warm_low_mid"
    if high_r > 0.32:
        return "dry_close_mic_bright_presence"
    if mid_r > 0.55:
        return "dry_close_mic_neutral_mid"
    return "roomy_neutral_broadband"


def _derive_source_music_risk(pacing: dict[str, Any], energy: dict[str, Any]) -> str:
    silence_ratio = float(energy.get("silence_ratio", 0.0))
    phrase_density = float(pacing.get("phrase_boundary_density_per_min", 0.0))
    if silence_ratio < 0.08 and phrase_density > 7.5:
        return "high"
    if silence_ratio < 0.14:
        return "medium"
    return "low"


def _derive_mix_contract(pacing: dict[str, Any], source_music_risk: str) -> dict[str, Any]:
    pace = pacing.get("pace_class", "conversational")
    # Audible beds under dialogue — constant level plus EQ carve, never drowning speech.
    if pace == "dense":
        bed_range = [-16, -12]
        duck = 14
        max_stingers = 1
    elif pace == "brisk":
        bed_range = [-16, -12]
        duck = 12
        max_stingers = 2
    elif pace == "calm":
        bed_range = [-16, -12]
        duck = 12
        max_stingers = 3
    else:
        bed_range = [-16, -12]
        duck = 12
        max_stingers = 2

    if source_music_risk == "high":
        underscore_policy = "skip"
    elif source_music_risk == "medium":
        underscore_policy = "sparse"
    else:
        underscore_policy = "normal"

    return {
        "bed_level_db_range": bed_range,
        "duck_under_speech_db": duck,
        "stinger_max_per_minute": max_stingers,
        "midrange_policy": "keep_stinger_energy_below_4khz_under_speech",
        "rhythmic_presence_default": "none",
        "tempo_feel_bpm": _tempo_feel_bpm(pacing, underscore_policy),
        "underscore_policy": underscore_policy,
    }


def _tempo_feel_bpm(pacing: dict[str, Any], underscore_policy: str) -> int | None:
    if underscore_policy == "skip":
        return None
    pace = pacing.get("pace_class")
    if pace == "dense":
        return 108
    if pace == "brisk":
        return 96
    return None


def _derive_prosody_summary(pacing: dict[str, Any], ctx: RunContext | None = None) -> dict[str, str]:
    pace = pacing.get("pace_class", "conversational")
    animation = {
        "dense": "high_energy_fast_turntaking",
        "brisk": "animated_conversational",
        "calm": "slow_reflective",
    }.get(pace, "conversational_not_theatrical")

    if ctx is not None:
        from interview_mux.interview_spine import SPINE_PATH

        if ctx.artifact_exists(SPINE_PATH):
            spine = ctx.read_json(SPINE_PATH)
            f0s = [
                float(w["features"]["f0_median_hz"])
                for w in (spine.get("windows") or [])
                if isinstance(w, dict)
                and isinstance((w.get("features") or {}).get("f0_median_hz"), (int, float))
            ]
            if f0s:
                med = float(np.median(f0s))
                q25, q75 = np.percentile(f0s, [25, 75])
                f0_band = "low" if med < 120 else "high" if med > 200 else "mid"
                spread = float(q75 - q25)
                f0_variability = "high" if spread > 40 else "low" if spread < 15 else "moderate"
                return {
                    "f0_band": f0_band,
                    "f0_variability": f0_variability,
                    "animation": animation,
                }

    return {
        "f0_band": "mid",
        "f0_variability": "moderate",
        "animation": animation,
    }


def _derive_prompt_tokens(mix_contract: dict[str, Any], pacing: dict[str, Any], energy: dict[str, Any]) -> dict[str, str]:
    density_hint = (
        f"{mix_contract.get('underscore_policy')} beds; "
        f"pace={pacing.get('pace_class')} "
        f"speech_active_ratio={pacing.get('speech_active_ratio')}"
    )
    timbre = energy.get("room_timbre_hint", "dry_close_mic_neutral_mid")
    return {
        "bed": (
            f"{timbre}, loopable, no pulse, no melody hook, "
            "designed for heavy ducking under close-mic speech"
        ),
        "stinger": "single soft mid-register rise-fall under 1.8s, no percussion, decay to silence",
        "avoid": "trailer whoosh, drum loop, vocal formants, cartoon SFX",
        "density": density_hint,
    }


def _placement_hints(pacing: dict[str, Any]) -> dict[str, Any]:
    base_pause = 400
    if pacing.get("pace_class") == "dense":
        base_pause = 550
    elif pacing.get("pace_class") == "calm":
        base_pause = 320
    return {
        "stinger_min_pause_after_speech_ms": base_pause,
        "bed_fade_in_ms": 700,
        "bed_fade_out_ms": 2200,
        "prefer_stinger_after_pause_tail": True,
    }


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _sha256_json(doc: dict[str, Any]) -> str:
    payload = json.dumps(doc, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()

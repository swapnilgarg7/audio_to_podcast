from __future__ import annotations

from typing import Any

from interview_mux.stage_input_helpers import attach_disfluency_context
from interview_mux.llm_specialists import maybe_run_post_stage_specialists
from interview_mux.run_context import RunContext
from interview_mux.stage_enrichment import (
    compact_manifest_for_volley,
    compact_value_features_summary,
    emphasis_regions_for_segments,
)
from interview_mux.operator_trace import logged_step
from interview_mux.artifact_completeness import make_stage_persist
from interview_mux.artifact_repairs import enrich_narrative_plan_for_persist
from interview_mux.production_profile import prompt_variant
from interview_mux.source_topology import attach_adaptation_to_payload
from interview_mux.stages.analysis_stage import run_flow_llm_stage
from interview_mux.stage_completion import heal_or_refuse_mark


def run_topic_coverage(ctx: RunContext) -> None:
    from interview_mux.talking_points_authority import try_deterministic_coverage
    from interview_mux.artifact_writes import write_validated_artifact
    from interview_mux.coherence import maybe_run_coherence_analysis
    from interview_mux.coherence.duration_gate import coherence_activated
    from interview_mux.coherence.paths import COHERENCE_REPORT_PATH

    if coherence_activated(ctx) and not ctx.artifact_exists(COHERENCE_REPORT_PATH):
        maybe_run_coherence_analysis(ctx, phase="post_reanchor")

    det = try_deterministic_coverage(ctx)
    if det is not None:
        write_validated_artifact(
            ctx,
            "master/coverage_audit.json",
            det,
            merge_from_disk=False,
            stage_key="topic_coverage_audit",
        )
        ctx.log(
            "topic_coverage_audit: deterministic from talking points + ideal cuts "
            f"(score={det.get('coverage_score')})",
            level="info",
            stage="topic_coverage_audit",
        )
        from interview_mux.delivery_guardrails import seed_stage_complete
        if not seed_stage_complete(ctx, "topic_coverage_audit"):
            heal_or_refuse_mark(ctx, "topic_coverage_audit", force=True)
    else:

        def build_input(c: RunContext) -> dict:
            manifest = c.read_json("segments/manifest.json") if c.artifact_exists("segments/manifest.json") else {}
            payload = {
                "content_brief": c.read_json("understanding/content_brief.json"),
                "segments": compact_manifest_for_volley(manifest if isinstance(manifest, dict) else {}, text_max=100),
                "emphasis_regions": emphasis_regions_for_segments(c),
            }
            vf = compact_value_features_summary(c)
            if vf:
                payload["value_features_summary"] = vf
            from interview_mux.coherence import attach_coherence_summary

            attach_coherence_summary(payload, c, "topic_coverage_audit")
            return attach_disfluency_context(
                __import__("interview_mux.delivery_brief", fromlist=["attach_delivery_brief_to_payload"]).attach_delivery_brief_to_payload(
                    c, attach_adaptation_to_payload(c, payload)
                ),
                c,
            )

        persist = make_stage_persist("master/coverage_audit.json", "topic_coverage_audit")

        with logged_step("topic_coverage_audit/llm_stage", ctx=ctx, stage="topic_coverage_audit"):
            # CSP-05 / TCA: soft-fail LLM must not auto-heal hollow; assert then heal.
            run_flow_llm_stage(
                ctx,
                "topic_coverage_audit",
                prompt_variant("selection/topic-coverage-audit.system.txt", ctx),
                build_input,
                persist,
                auto_complete=False,
            )
            from interview_mux.openai_primary_honesty import ensure_openai_primary_complete
            from interview_mux.llm_simple import StageError

            try:
                ensure_openai_primary_complete(ctx, "topic_coverage_audit")
            except Exception as exc:
                if isinstance(exc, StageError):
                    # Q5B: soft stub + heal (advisory coverage — do not stall Full-auto).
                    stub = {
                        "version": 1,
                        "coverage_score": 0.0,
                        "topic_mappings": [],
                        "missing_coverage": [],
                        "source": "stub",
                        "llm_failed": True,
                        "notes": [f"tca_soft_stub:{str(exc)[:160]}"],
                        "_meta": {"producer_stage": "topic_coverage_audit"},
                    }
                    write_validated_artifact(
                        ctx,
                        "master/coverage_audit.json",
                        stub,
                        merge_from_disk=False,
                        stage_key="topic_coverage_audit",
                    )
                    ctx.log(
                        f"topic_coverage_audit: soft stub after hollow LLM ({exc})",
                        level="warning",
                        stage="topic_coverage_audit",
                    )
                    from interview_mux.delivery_guardrails import seed_stage_complete
                    if not seed_stage_complete(ctx, "topic_coverage_audit"):
                        heal_or_refuse_mark(ctx, "topic_coverage_audit", force=True)
                else:
                    from interview_mux.openai_primary_honesty import hollow_openai_reason

                    raise StageError(
                        "topic_coverage_audit",
                        hollow_openai_reason(
                            "topic_coverage_audit",
                            str(exc)[:200] or "llm_soft_fail_hollow",
                        ),
                    ) from exc
    from interview_mux.delivery_guardrails import seed_stage_complete

    if seed_stage_complete(ctx, "topic_coverage_audit"):
        regions = emphasis_regions_for_segments(ctx)
        ctx.log(
            f"topic_coverage_audit: emphasis_regions count={len(regions)}",
            level="info",
            stage="topic_coverage_audit",
        )

    def _coverage_payload(c: RunContext) -> dict:
        manifest = c.read_json("segments/manifest.json") if c.artifact_exists("segments/manifest.json") else {}
        payload = {
            "content_brief": c.read_json("understanding/content_brief.json")
            if c.artifact_exists("understanding/content_brief.json")
            else {},
            "segments": compact_manifest_for_volley(manifest if isinstance(manifest, dict) else {}, text_max=100),
            "emphasis_regions": emphasis_regions_for_segments(c),
        }
        return payload

    with logged_step("topic_coverage_audit/post_specialists", ctx=ctx, stage="topic_coverage_audit"):
        maybe_run_post_stage_specialists(ctx, "topic_coverage_audit", _coverage_payload(ctx))
    if seed_stage_complete(ctx, "topic_coverage_audit"):
        with logged_step("topic_coverage_audit/coherence", ctx=ctx, stage="topic_coverage_audit"):
            from interview_mux.coherence import maybe_run_coherence_analysis

            maybe_run_coherence_analysis(ctx, phase="post_coverage")


def run_narrative_arc(ctx: RunContext) -> None:
    """Adaptive ladder R0–R7: never hollow-stamp; synthesize on honest LLM failure."""
    from interview_mux.talking_points_authority import (
        synthesize_narrative_from_coverage,
        try_deterministic_narrative,
    )
    from interview_mux.artifact_writes import write_validated_artifact
    from interview_mux.llm_simple import StageError

    stage = "narrative_arc_plan"

    def _has_substrate() -> bool:
        if ctx.artifact_exists("segments/manifest.json"):
            man = ctx.read_json("segments/manifest.json")
            if isinstance(man, dict) and any(
                isinstance(s, dict) and s.get("segment_id")
                for s in (man.get("segments") or [])
            ):
                return True
        if ctx.artifact_exists("master/coverage_audit.json"):
            cov = ctx.read_json("master/coverage_audit.json")
            if isinstance(cov, dict) and (cov.get("topic_mappings") or []):
                return True
        try:
            from interview_mux.talking_points_authority import MATERIALIZED_REL, TALKING_POINTS_REL

            if ctx.artifact_exists(TALKING_POINTS_REL) and ctx.artifact_exists(MATERIALIZED_REL):
                mat = ctx.read_json(MATERIALIZED_REL)
                if isinstance(mat, dict) and (mat.get("cuts") or []):
                    return True
        except Exception:
            pass
        return False

    def _structure_hard_integrity_bad() -> bool:
        if not ctx.artifact_exists("understanding/episode_structure.json"):
            return False
        es = (__import__("interview_mux.episode_structure", fromlist=["load_episode_structure"]).load_episode_structure(ctx) or {})
        if not isinstance(es, dict):
            return True
        integ = es.get("integrity") if isinstance(es.get("integrity"), dict) else {}
        if integ.get("ok") is False:
            flags = [str(f) for f in (integ.get("flags") or [])]
            hard = [
                f
                for f in flags
                if not f.startswith("orphan_answer:")
                and not f.startswith("advisory:")
            ]
            return bool(hard)
        order = es.get("segment_order") or []
        return not bool(order)

    def _maybe_repair_structure() -> None:
        if not _structure_hard_integrity_bad():
            return
        try:
            from interview_mux.episode_structure import build_episode_structure, persist_structure

            doc = build_episode_structure(ctx, refresh=True)
            persist_structure(ctx, doc, stage=stage)
            ctx.log(
                "narrative_arc_plan: rebuilt episode_structure (hard integrity)",
                level="info",
                stage=stage,
            )
        except Exception as exc:
            ctx.log(
                f"narrative_arc_plan: structure rebuild failed ({exc})",
                level="warning",
                stage=stage,
            )

    def _persist_plan(plan: dict, *, source: str) -> bool:
        enriched = enrich_narrative_plan_for_persist(ctx, plan)
        from interview_mux.artifact_repairs import repair_narrative_plan

        repaired, _notes = repair_narrative_plan(ctx, enriched)
        chapters = repaired.get("chapters") if isinstance(repaired, dict) else None
        if not isinstance(chapters, list) or not chapters:
            return False
        usable = False
        for ch in chapters:
            if isinstance(ch, dict) and (
                (ch.get("segment_ids") or [])
                or ch.get("suggested_open_segment_id")
            ):
                usable = True
                break
        if not usable:
            return False
        meta = dict(repaired.get("_meta") or {})
        meta["source"] = source
        repaired["_meta"] = meta
        write_validated_artifact(
            ctx,
            "master/narrative_plan.json",
            repaired,
            merge_from_disk=False,
            stage_key=stage,
        )
        ctx.log(
            f"narrative_arc_plan: persisted ({source}, "
            f"{len(repaired.get('chapters') or [])} chapters)",
            level="info",
            stage=stage,
        )
        return True

    def _salvage_or_halt(*, why: str) -> None:
        salvaged = synthesize_narrative_from_coverage(ctx)
        if salvaged is not None and _persist_plan(
            salvaged, source=str((salvaged.get("_meta") or {}).get("source") or "synthesize")
        ):
            return
        if not _has_substrate():
            raise StageError(
                stage,
                "narrative_arc_exhausted:missing_synthesize_substrate "
                f"({why})",
            )
        raise StageError(
            stage,
            f"narrative_arc_exhausted:could_not_synthesize ({why})",
        )

    # R0 — substrate
    if not _has_substrate():
        raise StageError(
            stage,
            "narrative_arc_exhausted:missing_synthesize_substrate",
        )

    # R1 — structure repair
    _maybe_repair_structure()
    structure_bad = _structure_hard_integrity_bad()

    # R2 — prefer / force deterministic
    det = try_deterministic_narrative(ctx)
    if det is not None and _persist_plan(det, source="talking_points_authority"):
        return
    if structure_bad and det is None:
        ctx.log(
            "narrative_arc_plan: hard integrity still bad — skip LLM, salvage",
            level="warning",
            stage=stage,
        )
        _salvage_or_halt(why="structure_integrity")
        return

    # R3 — LLM attempt 1 full packet. R4 — attempt 2 with changed/degraded
    # packet (drop structure; manifest order + coverage scaffold). R5 salvage.
    degraded: dict[str, bool] = {"use": bool(structure_bad)}

    def build_input(c: RunContext) -> dict:
        manifest = (
            c.read_json("segments/manifest.json")
            if c.artifact_exists("segments/manifest.json")
            else {}
        )
        payload: dict = {
            "content_brief": c.read_json("understanding/content_brief.json"),
            "coverage_audit": c.read_json("master/coverage_audit.json"),
            "segments": compact_manifest_for_volley(
                manifest if isinstance(manifest, dict) else {}, text_max=100
            ),
            "emphasis_regions": emphasis_regions_for_segments(c),
        }
        vf = compact_value_features_summary(c)
        if vf:
            payload["value_features_summary"] = vf
        from interview_mux.coherence import attach_coherence_summary

        attach_coherence_summary(payload, c, "narrative_arc_plan")
        from interview_mux.delivery_brief import attach_delivery_brief_to_payload
        from interview_mux.episode_structure import attach_episode_structure_to_payload

        payload = attach_disfluency_context(
            attach_delivery_brief_to_payload(
                c, attach_adaptation_to_payload(c, payload)
            ),
            c,
        )
        if degraded["use"]:
            payload.pop("episode_structure", None)
            order: list[str] = []
            if isinstance(manifest, dict):
                order = [
                    str(s.get("segment_id"))
                    for s in (manifest.get("segments") or [])
                    if isinstance(s, dict) and s.get("segment_id")
                ]
            payload["segment_order"] = order
            payload["segment_order_count"] = len(order)
            payload["narrative_packet_mode"] = "degraded_no_structure"
            # Full must-keep ids when available (R4 scaffold).
            try:
                for rel, key in (
                    ("understanding/ideal_cuts_selection_seed.json", "must_keep_segment_ids"),
                    ("analysis/low_conf_must_keep.json", "must_keep_segment_ids"),
                ):
                    if not c.artifact_exists(rel):
                        continue
                    doc = c.read_json(rel)
                    if isinstance(doc, dict) and doc.get(key):
                        payload["must_keep_segment_ids"] = [
                            str(x) for x in (doc.get(key) or []) if x
                        ]
                        break
            except Exception:
                pass
            return payload
        return attach_episode_structure_to_payload(c, payload)

    def _persist_narrative(c: RunContext, artifacts: dict) -> None:
        enriched = enrich_narrative_plan_for_persist(c, artifacts)
        from interview_mux.artifact_repairs import repair_narrative_plan

        repaired, _ = repair_narrative_plan(c, enriched)
        if not (repaired.get("chapters") or []):
            raise RuntimeError("narrative_arc_plan: empty chapters after repair")
        write = make_stage_persist("master/narrative_plan.json", "narrative_arc_plan")
        write(c, repaired)

    def _llm_once() -> None:
        run_flow_llm_stage(
            ctx,
            stage,
            prompt_variant("selection/narrative-arc-plan.system.txt", ctx),
            build_input,
            _persist_narrative,
            auto_complete=False,
        )
        if not ctx.artifact_exists("master/narrative_plan.json"):
            raise StageError(stage, "LLM completed without narrative_plan")
        raw = ctx.read_json("master/narrative_plan.json")
        if not isinstance(raw, dict) or not (raw.get("chapters") or []):
            raise StageError(stage, "LLM narrative_plan empty chapters")

    with logged_step("narrative_arc_plan/llm_stage", ctx=ctx, stage=stage):
        try:
            # R3
            _llm_once()
            return
        except Exception as first_exc:
            # R4 — changed packet (force degraded even if structure looked ok)
            if not degraded["use"]:
                degraded["use"] = True
                ctx.log(
                    f"narrative_arc_plan: R3 failed ({first_exc}); "
                    "R4 degraded packet retry",
                    level="warning",
                    stage=stage,
                )
                try:
                    _llm_once()
                    return
                except Exception as second_exc:
                    ctx.log(
                        f"narrative_arc_plan: R4 failed ({second_exc}); synthesizing",
                        level="warning",
                        stage=stage,
                    )
                    _salvage_or_halt(why=str(second_exc)[:160])
                    return
            ctx.log(
                f"narrative_arc_plan: LLM path failed ({first_exc}); synthesizing",
                level="warning",
                stage=stage,
            )
            _salvage_or_halt(why=str(first_exc)[:160])


def run_nugget_corpus_mine(ctx: RunContext) -> None:
    """Flagship mine of grounded nuggets from the full tape (kept + excluded)."""
    from interview_mux.nugget_layup import (
        CORPUS_REL,
        build_corpus_mine_input,
        nugget_layup_enabled,
    )

    if not nugget_layup_enabled():
        ctx.write_json(CORPUS_REL, {"nuggets": [], "warnings": ["nugget_layup_disabled"]})
        from interview_mux.delivery_guardrails import seed_stage_complete
        if not seed_stage_complete(ctx, "nugget_corpus_mine"):
            heal_or_refuse_mark(ctx, "nugget_corpus_mine", force=True)
        return

    persist = make_stage_persist(CORPUS_REL, "nugget_corpus_mine")

    def persist_corpus(c: RunContext, artifacts: dict) -> None:
        from interview_mux.media_ip_cta import strip_never_touch_nuggets
        from interview_mux.nugget_layup import normalize_corpus_tape_status

        doc = artifacts if isinstance(artifacts, dict) else {}
        # The tape decides what aired (ISSUES 191): a nugget spoken between
        # segments or in the part a cut trims off stays open for VO.
        doc = normalize_corpus_tape_status(c, doc)
        persist(c, strip_never_touch_nuggets(c, doc))

    # NCM-B2: auto_complete=False so empty corpus cannot hollow-stamp via mark_done;
    # heal_or_raise refuses when enabled mine yields zero nuggets.
    with logged_step("nugget_corpus_mine/llm_stage", ctx=ctx, stage="nugget_corpus_mine"):
        run_flow_llm_stage(
            ctx,
            "nugget_corpus_mine",
            prompt_variant("nugget_layup/nugget-corpus-mine.system.txt", ctx),
            build_corpus_mine_input,
            persist_corpus,
            auto_complete=False,
        )
    from interview_mux.stage_completion import heal_or_raise

    heal_or_raise(ctx, "nugget_corpus_mine")


def commit_layup_cta_selection(
    ctx: RunContext,
    previous: dict[str, Any] | None,
    pruned: dict[str, Any] | None,
) -> dict[str, Any] | None:
    """Persist CTA prune on the selection bus (ranking/sanitize owner — S7).

    Soft freeze: land editorial CTA omit via End-A packaging (media_ip_cta).
    Hard freeze: skip — no silent CTA order rewrite after VO seat freeze.
    Layup compose no longer calls this; kept for sanitize/ranking + unit tests.
    """
    if not isinstance(previous, dict) or not isinstance(pruned, dict):
        return None
    if pruned.get("ordered_segment_ids") is None:
        return None
    prev_ids = [str(s) for s in (previous.get("ordered_segment_ids") or []) if s]
    new_ids = [str(s) for s in (pruned.get("ordered_segment_ids") or []) if s]
    if new_ids == prev_ids:
        return None
    try:
        from interview_mux.seat_authority import hard_freeze_active

        if hard_freeze_active(ctx):
            ctx.log(
                "selection_order_sanitize: CTA selection commit skipped — hard freeze "
                "(no silent CTA order rewrite after VO freeze)",
                level="warning",
                stage="selection_order_sanitize",
            )
            return None
    except Exception:
        pass
    # Belt: restore floor-anchor natives if prune starved hosted VO targets.
    try:
        from interview_mux.media_ip_cta import restore_floor_anchor_natives

        pruned = restore_floor_anchor_natives(ctx, prev_ids, pruned)
        new_ids = [str(s) for s in (pruned.get("ordered_segment_ids") or []) if s]
        if new_ids == prev_ids:
            return None
    except Exception:
        pass
    from interview_mux.air_order_boundary import commit_selection_or_refuse

    return commit_selection_or_refuse(
        ctx,
        pruned,
        producer="selection_order_sanitize",
        stage_key="selection_order_sanitize",
        checkpoint_mode="detect",
    )


def _clear_layup_compose_qc_pending(plan: dict[str, Any]) -> dict[str, Any]:
    from interview_mux.nugget_layup import clear_compose_qc_pending

    return clear_compose_qc_pending(plan)


def _stamp_layup_compose_qc_pending(
    plan: dict[str, Any], *, errors: list[str] | None = None
) -> dict[str, Any]:
    from interview_mux.nugget_layup import stamp_compose_qc_pending

    return stamp_compose_qc_pending(plan, errors=errors)


def _heal_nugget_layup_compose_if_complete(ctx: RunContext) -> None:
    """Mark done only when artifacts are complete; else leave incomplete (NLC-B1).

    Land Honesty: never early-return on bare is_done — heal may unmark hollow.
    """
    # Seat already-active hosted framing lines before the floor incompleteness
    # check (pre/soft only — hard freeze uses i10 WAV paperwork elsewhere).
    try:
        from interview_mux.seat_authority import hard_freeze_active

        if not hard_freeze_active(ctx):
            from interview_mux.vo_contract import ensure_hosted_framing_vo_seats

            ensure_hosted_framing_vo_seats(ctx)
    except Exception as exc:
        try:
            ctx.log(
                f"nugget_layup_compose: hosted floor reseat skipped: {exc}",
                level="warning",
                stage="nugget_layup_compose",
            )
        except Exception:
            pass
    try:
        from interview_mux.hosted_vo_authority import (
            floor_snapshot,
            identify_hosted_vo_floor,
            reconcile_escalations,
        )

        identify_hosted_vo_floor(ctx, stage_id="nugget_layup_compose", persist=True)
        snap = floor_snapshot(ctx, stage_id="nugget_layup_compose", persist=True)
        reconcile_escalations(ctx, snap)
    except Exception:
        pass
    try:
        from interview_mux.stage_completion import (
            StageArtifactsIncompleteError,
            assert_stage_artifacts_complete,
            heal_or_raise,
        )

        assert_stage_artifacts_complete(ctx, "nugget_layup_compose")
        heal_or_raise(ctx, "nugget_layup_compose")
    except StageArtifactsIncompleteError as exc:
        ctx.log(
            f"nugget_layup_compose not marked done — {exc.reason}",
            level="warning",
            stage="nugget_layup_compose",
        )


def _stamp_layup_compose_shards_pending(
    plan: dict[str, Any],
    *,
    shard_index: int,
    shard_total: int,
) -> dict[str, Any]:
    """Mark intermediate shard merges so mid-crash cannot hollow-complete (NLC-B1)."""
    out = dict(plan) if isinstance(plan, dict) else {}
    meta = dict(out.get("_meta") or {}) if isinstance(out.get("_meta"), dict) else {}
    meta["compose_shards_pending"] = True
    meta["compose_shard_index"] = int(shard_index)
    meta["compose_shard_total"] = int(shard_total)
    out["_meta"] = meta
    return out


def _clear_layup_compose_shards_pending(plan: dict[str, Any]) -> dict[str, Any]:
    out = dict(plan) if isinstance(plan, dict) else {}
    meta = out.get("_meta")
    if isinstance(meta, dict):
        meta = dict(meta)
        meta.pop("compose_shards_pending", None)
        meta.pop("compose_shard_index", None)
        meta.pop("compose_shard_total", None)
        out["_meta"] = meta
    return out


def run_nugget_layup_compose(ctx: RunContext) -> None:
    """Flagship per-native lay-up plan → authoritative gap_report before-VO lines."""
    from interview_mux.nugget_layup import (
        PLAN_REL,
        assert_gap_report_layup_authority,
        assert_layup_fresh_vs_selection,
        assert_layup_qc_or_raise,
        build_layup_compose_input,
        commit_layup_gap_authority,
        degraded_layup_cfg,
        evaluate_layup_qc,
        merge_layup_plan_parts,
        nugget_layup_cfg,
        nugget_layup_enabled,
        repair_or_skip_spoken_copy_layups,
    )
    from interview_mux.operator_trace import log_step

    if not nugget_layup_enabled():
        ctx.write_json(
            PLAN_REL,
            {
                "ordered_segment_ids": [],
                "layups": [],
                "warnings": ["nugget_layup_disabled"],
            },
        )
        _heal_nugget_layup_compose_if_complete(ctx)
        return

    # S7: layup never mutates master/selection.json — ranking + sanitize own
    # never_touch / CTA omit. Read-only never-touch skip still runs in plan QC.

    persist_plan = make_stage_persist(PLAN_REL, "nugget_layup_compose")
    deg = degraded_layup_cfg()
    cfg = nugget_layup_cfg()
    degraded_retry_used = {"n": 0}
    batch_size = max(4, int(cfg.get("compose_batch_max_natives") or 32))

    def persist(c: RunContext, artifacts: dict) -> None:
        """P1: heal/QC in memory first; publish gap only after QC would pass."""
        from interview_mux.nugget_layup import (
            ensure_deterministic_floor_before_refuse,
            gap_report_write_lock,
            invalidate_vo_after_layup_rewrite,
            normalize_layup_talking_point_ledger,
            prepare_layup_plan_for_persist,
            prior_gap_line_fingerprints,
            stamp_sparse_or_empty_corpus_exits,
            strip_model_order_lock,
        )

        doc = dict(artifacts) if isinstance(artifacts, dict) else {}
        if "ordered_segment_ids" not in doc:
            sel = (
                c.read_json("master/selection.json")
                if c.artifact_exists("master/selection.json")
                else {}
            )
            doc["ordered_segment_ids"] = list((sel or {}).get("ordered_segment_ids") or [])

        doc = strip_model_order_lock(doc)
        doc = normalize_layup_talking_point_ledger(c, doc)
        from interview_mux.nugget_layup import heal_layup_analysis_fields

        doc, analysis_heals = heal_layup_analysis_fields(c, doc)
        if analysis_heals:
            c.log(
                "nugget_layup_compose: healed missing/canned unlock analysis "
                f"({len(analysis_heals)} row(s))",
                level="warning",
                stage="nugget_layup_compose",
            )
        doc, copy_repairs = repair_or_skip_spoken_copy_layups(c, doc)
        if copy_repairs:
            c.log(
                "nugget_layup_compose: repaired/skipped unsafe spoken copy "
                f"({len(copy_repairs)} row(s))",
                level="warning",
                stage="nugget_layup_compose",
            )
        # Selection is the only order_lock authority — stamp before freshness.
        doc = prepare_layup_plan_for_persist(c, doc)
        doc = _clear_layup_compose_shards_pending(doc)
        doc = _clear_layup_compose_qc_pending(doc)
        assert_layup_fresh_vs_selection(c, doc)

        # P4: empty/sparse corpus deterministic exits before QC loops.
        doc, sparse_exit_notes = stamp_sparse_or_empty_corpus_exits(c, doc)
        if sparse_exit_notes:
            c.log(
                "nugget_layup_compose: sparse/empty corpus exits "
                f"({len(sparse_exit_notes)} row(s))",
                level="warning",
                stage="nugget_layup_compose",
            )

        qc = evaluate_layup_qc(c, doc)
        # Layer-4: one dedicated degraded regenerate (before any gap publish).
        if (
            not qc.get("ok")
            and deg.get("enabled", True)
            and deg.get("extra_degraded_regenerate", True)
            and degraded_retry_used["n"] < 1
            and any(
                "invented_island" in e or "canned_air" in e or "thin_layup" in e
                for e in (qc.get("errors") or [])
            )
        ):
            degraded_retry_used["n"] += 1
            from interview_mux.llm_simple import StageError

            raise StageError(
                "nugget_layup_compose",
                "Degraded layup QC failed — regenerate with work-around doctrine "
                "(no invent / no canned / spine-first unlock). Errors: "
                + "; ".join((qc.get("errors") or [])[:4]),
            )

        # S8: one deterministic heal pass then refuse — no materialize↔recover↔park↔spine ladder.
        if not qc.get("ok"):
            doc, floor_notes = ensure_deterministic_floor_before_refuse(c, doc)
            if floor_notes:
                doc = prepare_layup_plan_for_persist(c, doc)
                qc = evaluate_layup_qc(c, doc)
                c.log(
                    "nugget_layup_compose: deterministic floor prep "
                    + "; ".join(str(n) for n in floor_notes[-10:]),
                    level="warning",
                    stage="nugget_layup_compose",
                )
            # Persist prep can re-skip rows the floor pass aired (never-touch CTA
            # targets) and reopen their nuggets. When open high-salience nuggets
            # are all that is left, park them on the orientation rather than
            # refuse (exec_010 attempt 2).
            if not qc.get("ok") and all(
                str(e).startswith("open_high_salience_nuggets=") for e in (qc.get("errors") or [])
            ):
                from interview_mux.nugget_layup import park_open_high_salience_on_orientation

                doc, park_notes = park_open_high_salience_on_orientation(c, doc)
                if park_notes:
                    qc = evaluate_layup_qc(c, doc)
                    c.log(
                        "nugget_layup_compose: parked open high-salience nuggets on the "
                        "orientation after persist prep: " + ", ".join(park_notes[:8]),
                        level="warning",
                        stage="nugget_layup_compose",
                    )

        if not qc.get("ok"):
            # P1: do not publish dirty authority — stamp qc_pending on plan only.
            pending = _stamp_layup_compose_qc_pending(
                doc, errors=[str(e) for e in (qc.get("errors") or [])]
            )
            persist_plan(c, pending)
            assert_layup_qc_or_raise(c, qc)
            return

        prior_fps = prior_gap_line_fingerprints(c)
        doc = _clear_layup_compose_qc_pending(doc)
        doc = _clear_layup_compose_shards_pending(doc)
        with gap_report_write_lock(c):
            persist_plan(c, doc)
            # Commit plan immediately so NLC-B1 shard stamps cannot survive a
            # pending-only write + flush race (exec_13177 layup ×3 thrash).
            try:
                from interview_mux.write_staging import write_committed_json

                write_committed_json(
                    c, PLAN_REL, doc, stage_key="nugget_layup_compose"
                )
            except Exception as commit_exc:
                c.log(
                    f"nugget_layup_compose: committed plan mirror failed: {commit_exc}",
                    level="warning",
                    stage="nugget_layup_compose",
                )
            report = commit_layup_gap_authority(c, doc)
            assert_layup_qc_or_raise(c, qc)
            assert_gap_report_layup_authority(c, report)
            invalidate_vo_after_layup_rewrite(
                c, prior_fps=prior_fps, new_report=report
            )
            from interview_mux.vo_contract import sync_vo_contract_after_layup

            remaining = sync_vo_contract_after_layup(c)
            if remaining:
                raise RuntimeError(f"VO contract drift after layup: {remaining[0]}")

    def build_input(c: RunContext, *, target_ids: list[str] | None = None) -> dict:
        packet = build_layup_compose_input(c, target_segment_ids=target_ids)
        if degraded_retry_used["n"]:
            packet["degraded_regenerate"] = True
            packet["degraded_doctrine_addendum"] = (
                "REGENERATE: work around missing/incomprehensible spans using "
                "comprehensible_text + corpus only. Never invent unclear lexicon. "
                "Keep a thinner but concrete forward_unlock. No canned hinges."
            )
        packet["min_layup_words"] = int(cfg["min_layup_words"])
        return packet

    with logged_step("nugget_layup_compose/llm_stage", ctx=ctx, stage="nugget_layup_compose"):
        ordered = []
        if ctx.artifact_exists("master/selection.json"):
            sel = ctx.read_json("master/selection.json")
            if isinstance(sel, dict):
                ordered = [str(x) for x in (sel.get("ordered_segment_ids") or []) if x]
        # Do NOT wipe PLAN_REL to an empty compose_restart document. Concurrent QC /
        # G-Framing incompleteness readers (and forensics heal) treat empty layups as
        # coverage=0 and can thrash while shards are still running. Shard merges
        # overwrite progressively; already_aired walks prior rows when present.
        if len(ordered) <= batch_size:
            run_flow_llm_stage(
                ctx,
                "nugget_layup_compose",
                prompt_variant("nugget_layup/nugget-layup-compose.system.txt", ctx),
                lambda c: build_input(c),
                persist,
            )
            return

        batches = [
            ordered[i : i + batch_size]
            for i in range(0, len(ordered), batch_size)
        ]
        log_step(
            f"nugget_layup_compose proactive batch: {len(ordered)} natives → "
            f"{len(batches)} shard(s) of ≤{batch_size}",
            ctx=ctx,
            stage="nugget_layup_compose",
            level="action",
            detail={
                "action_id": "nugget_layup_compose.proactive_batch",
                "batch_size": batch_size,
                "batches": len(batches),
                "natives": len(ordered),
            },
        )
        parts: list[dict[str, Any]] = []
        prompt_rel = prompt_variant("nugget_layup/nugget-layup-compose.system.txt", ctx)
        for bi, batch_ids in enumerate(batches):
            shard_box: dict[str, Any] = {"doc": {}}

            def build_batch(
                c: RunContext,
                _ids: list[str] = list(batch_ids),
                _i: int = bi,
                _total: int = len(batches),
            ) -> dict:
                packet = build_input(c, target_ids=_ids)
                packet["_layup_compose_shard"] = {
                    "index": _i + 1,
                    "total": _total,
                    "target_segment_ids": list(_ids),
                }
                packet["compose_instruction"] = (
                    f"Compose lay-ups ONLY for the {len(_ids)} natives in this shard "
                    f"({_i + 1}/{_total}). Look up claim/evidence in nugget_corpus. "
                    "Honor already_aired_nugget_ids from earlier air-order natives."
                )
                return packet

            def persist_shard(c: RunContext, artifacts: dict) -> None:
                from interview_mux.nugget_layup import prepare_layup_plan_for_persist

                doc = dict(artifacts) if isinstance(artifacts, dict) else {}
                # Intermediate write so later shards see claimed nuggets via prior_plan.
                # ordered_segment_ids stays shard-local here; final merge restores full order.
                # NLC-B1: stamp compose_shards_pending so mid-crash cannot hollow-done.
                merged_so_far = merge_layup_plan_parts(
                    parts + [doc],
                    ordered_segment_ids=ordered,
                )
                pending = _stamp_layup_compose_shards_pending(
                    prepare_layup_plan_for_persist(c, merged_so_far),
                    shard_index=bi + 1,
                    shard_total=len(batches),
                )
                c.write_json(PLAN_REL, pending)
                shard_box["doc"] = doc

            log_step(
                f"nugget_layup_compose shard {bi + 1}/{len(batches)} "
                f"({len(batch_ids)} natives)",
                ctx=ctx,
                stage="nugget_layup_compose",
                level="action",
                detail={
                    "action_id": "nugget_layup_compose.shard",
                    "shard": bi + 1,
                    "total": len(batches),
                    "natives": len(batch_ids),
                },
            )
            run_flow_llm_stage(
                ctx,
                "nugget_layup_compose",
                prompt_rel,
                build_batch,
                persist_shard,
                auto_complete=False,
            )
            parts.append(shard_box["doc"] if isinstance(shard_box.get("doc"), dict) else {})

        merged = merge_layup_plan_parts(parts, ordered_segment_ids=ordered)
        log_step(
            f"nugget_layup_compose batched complete ({len(merged.get('layups') or [])} layups "
            f"across {len(batches)} shard(s))",
            ctx=ctx,
            stage="nugget_layup_compose",
            level="success",
            detail={
                "action_id": "nugget_layup_compose.proactive_batch_complete",
                "layups": len(merged.get("layups") or []),
                "batches": len(batches),
            },
        )
        persist(ctx, merged)
        _heal_nugget_layup_compose_if_complete(ctx)

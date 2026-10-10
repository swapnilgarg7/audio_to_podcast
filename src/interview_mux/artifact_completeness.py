"""Semantic completeness, gap-fill context, and incremental artifact merge."""

from __future__ import annotations

import copy
from dataclasses import dataclass
from typing import Any, Callable

from interview_mux.coverage_limits import gap_fill_cap
from interview_mux.prompt_validation import (
    STAGE_ARTIFACT_DISK_PATHS,
    validate_artifact_write,
    validate_stage_artifacts,
)
from interview_mux.run_context import RunContext

GapRule = Callable[[dict[str, Any] | None], list[str]]

# Bytes-only producer artifacts. Images belong here: publish/cover.jpg fell through
# to the JSON reader, whose UnicodeDecodeError (a ValueError) was swallowed into
# status="pending" — mark_done(episode_cover_generate) then refused forever and the
# ship walk re-billed three cover generations per round (exec_11871).
BINARY_ARTIFACT_SUFFIXES = (
    ".wav",
    ".mp3",
    ".m4a",
    ".aac",
    ".flac",
    ".ogg",
    ".jpg",
    ".jpeg",
    ".png",
    ".webp",
)
_BINARY_ARTIFACT_SUFFIXES = BINARY_ARTIFACT_SUFFIXES


def _binary_artifact_status(ctx: RunContext, rel_path: str) -> str:
    from interview_mux.write_staging import resolve_read_path

    path = resolve_read_path(ctx, rel_path)
    try:
        return "complete" if path.is_file() and path.stat().st_size > 1024 else "partial"
    except OSError:
        return "pending"


def _safe_read_json_for_status(ctx: RunContext, rel_path: str) -> Any | None:
    """Read artifact JSON for status checks; None when missing or mid-write corrupt."""
    if str(rel_path or "").lower().endswith(_BINARY_ARTIFACT_SUFFIXES):
        return None
    try:
        return ctx.read_json(rel_path)
    except (FileNotFoundError, OSError, ValueError, TypeError):
        # ValueError covers json.JSONDecodeError (empty / partial writes during promote).
        return None

@dataclass(frozen=True)
class Gap:
    path: str
    reason: str

GAP_FILL_PROTECTED_PATHS: frozenset[str] = frozenset(
    {
        "thesis",
        "topics",
        "topics[].segment_ids",
        "topics[].name",
        "topics[].summary",
        "ordered_segment_ids",
        "segments",
        "segments[].segment_id",
        "segments[].type",
        "boundaries",
        "speakers",
        "speakers[].role",
        "speakers[].speaker_id",
        "evaluations",
        "gaps",
        "topic_relationships",
    }
)

def _is_protected_gap_path(path: str) -> bool:
    if path in GAP_FILL_PROTECTED_PATHS:
        return True
    for pat in GAP_FILL_PROTECTED_PATHS:
        if "[]" in pat:
            prefix = pat.split("[")[0]
            if path.startswith(prefix):
                return True
    return False

def _filter_protected_skip_fields(skip_fields: list[str]) -> list[str]:
    return [f for f in skip_fields if not _is_protected_gap_path(f)]

def _non_empty_str(val: Any) -> bool:
    return isinstance(val, str) and bool(val.strip())

def _gaps_analysis_state(data: dict[str, Any] | None) -> list[str]:
    if not data:
        return ["(root)"]
    gaps: list[str] = []
    if not (data.get("themes") or []):
        gaps.append("themes")
    narrative = data.get("narrative") or {}
    if not _non_empty_str(narrative.get("thesis")):
        gaps.append("narrative.thesis")
    ident = data.get("interview_identity") or {}
    if not _non_empty_str(ident.get("one_line_summary")):
        gaps.append("interview_identity.one_line_summary")
    style = data.get("style") or {}
    if not _non_empty_str(style.get("tone")):
        gaps.append("style.tone")
    if not _non_empty_str(style.get("tone_class")):
        gaps.append("style.tone_class")
    if not _non_empty_str(style.get("format_class")):
        gaps.append("style.format_class")
    return gaps

def _gaps_analysis_state_speakers_only(data: dict[str, Any] | None) -> list[str]:
    """Incremental analysis_state check after speaker_roles memory merge."""
    if not data:
        return ["speakers"]
    return _gaps_speakers({"speakers": data.get("speakers") or []})

def _gaps_content_brief(data: dict[str, Any] | None) -> list[str]:
    if not data:
        return ["thesis", "topics"]
    gaps: list[str] = []
    if not _non_empty_str(data.get("thesis")):
        gaps.append("thesis")
    topics = data.get("topics") or []
    if not topics:
        gaps.append("topics")
    else:
        for i, t in enumerate(topics):
            if isinstance(t, dict) and not _non_empty_str(t.get("summary")):
                gaps.append(f"topics[{i}].summary")
    return gaps

def _gaps_content_brief_reanchor(data: dict[str, Any] | None) -> list[str]:
    gaps = _gaps_content_brief(data)
    if not data:
        return gaps
    for i, t in enumerate(data.get("topics") or []):
        if isinstance(t, dict) and not (t.get("segment_ids") or []):
            gaps.append(f"topics[{i}].segment_ids")
    if not (data.get("topic_relationships") or []):
        gaps.append("topic_relationships")
    return gaps

def _gaps_speakers(data: dict[str, Any] | None) -> list[str]:
    if not data:
        return ["speakers"]
    speakers = data.get("speakers") if "speakers" in data else data
    if not isinstance(speakers, list) or not speakers:
        return ["speakers"]
    gaps: list[str] = []
    for i, sp in enumerate(speakers):
        if not isinstance(sp, dict):
            gaps.append(f"speakers[{i}]")
            continue
        if not sp.get("role"):
            gaps.append(f"speakers[{i}].role")
    if speakers and all(
        isinstance(sp, dict) and str(sp.get("role", "")).strip().lower() == "unknown"
        for sp in speakers
    ):
        gaps.append("speakers.all_unknown_roles")
    conflict = data.get("role_tape_conflict") if isinstance(data, dict) else None
    if isinstance(conflict, dict) and conflict.get("blocking"):
        gaps.append("role_tape_conflict")
    return gaps

def _gaps_sound_design_plan(data: dict[str, Any] | None) -> list[str]:
    if not data:
        return ["coherence"]
    gaps: list[str] = []
    coherence = data.get("coherence") or {}
    if not _non_empty_str(coherence.get("sonic_identity")):
        gaps.append("coherence.sonic_identity")
    # Early palette LLM may be deferred to sound_design_plan — empty palettes are OK.
    deferred = bool(coherence.get("deferred_early_palettes"))
    if not deferred:
        try:
            from interview_mux.config import merged_config

            sd_cfg = merged_config().get("sound_design") or {}
            deferred = not bool(sd_cfg.get("early_palettes_llm", False))
        except Exception:
            deferred = False
    if not deferred and not (data.get("palettes") or []):
        gaps.append("palettes")
    return gaps

def _gaps_investigation_queue(data: dict[str, Any] | None) -> list[str]:
    if not data:
        return []
    return []

def _gaps_generic_nonempty(data: dict[str, Any] | None) -> list[str]:
    if not data:
        return ["(root)"]
    return []


def _gaps_manifest(data: dict[str, Any] | None) -> list[str]:
    if not data:
        return ["segments"]
    segs = data.get("segments") or []
    if not segs:
        return ["segments"]
    return []


def _gaps_boundaries(data: dict[str, Any] | None) -> list[str]:
    if not data:
        return ["boundaries"]
    boundaries = data.get("boundaries")
    if not isinstance(boundaries, list) or not boundaries:
        return ["boundaries"]
    return []


def _gaps_selection(data: dict[str, Any] | None) -> list[str]:
    if not data:
        return ["ordered_segment_ids"]
    ids = data.get("ordered_segment_ids")
    if not isinstance(ids, list) or not ids:
        return ["ordered_segment_ids"]
    return []


def _gaps_narrative_plan(data: dict[str, Any] | None) -> list[str]:
    if not data:
        return ["beats"]
    beats = data.get("beats") or data.get("arc") or data.get("chapters")
    if isinstance(beats, list) and beats:
        return []
    if _non_empty_str(data.get("thesis") or data.get("summary")):
        return []
    return ["beats"]


def _gaps_coverage_audit(data: dict[str, Any] | None) -> list[str]:
    if not data:
        return ["findings"]
    findings = data.get("findings") or data.get("topics") or data.get("coverage")
    if isinstance(findings, list) and findings:
        return []
    if isinstance(findings, dict) and findings:
        return []
    # Canon LLM / OF-01 shape: topic_mappings (+ coverage_score), not legacy findings.
    mappings = data.get("topic_mappings")
    if isinstance(mappings, list) and mappings:
        return []
    if data.get("coverage_score") is not None and isinstance(data.get("missing_coverage"), list):
        return []
    return ["findings"]


def _gaps_gap_evaluations(data: dict[str, Any] | None) -> list[str]:
    """Hollow [] refuse — empty evaluations OK only when explicitly skipped/self-explanatory."""
    if not data:
        return ["evaluations"]
    ev = data.get("evaluations")
    if not isinstance(ev, list):
        return ["evaluations"]
    if ev:
        return []
    # Allowlist: intentional empty (skip / all self-explanatory).
    if data.get("skipped") or data.get("empty_ok") or data.get("all_self_explanatory"):
        return []
    meta = data.get("_meta") if isinstance(data.get("_meta"), dict) else {}
    if str(meta.get("empty_allowlist") or "") in {"skip", "self_explanatory", "optimal_questions"}:
        return []
    return ["evaluations"]


def _gaps_gap_report(data: dict[str, Any] | None) -> list[str]:
    """interviewer_lines may be [] (A-02 allowlist: skip / optimal_questions / no gaps)."""
    if not data:
        return ["interviewer_lines"]
    if "interviewer_lines" not in data:
        return ["interviewer_lines"]
    if not isinstance(data.get("interviewer_lines"), list):
        return ["interviewer_lines"]
    return []


def _gaps_transitions(data: dict[str, Any] | None) -> list[str]:
    if not data:
        return ["transitions"]
    rows = data.get("transitions")
    if not isinstance(rows, list):
        return ["transitions"]
    if rows:
        return []
    # Allowlist: selection < 2 → no junctions expected.
    if data.get("empty_ok") or data.get("selection_count", 99) < 2:
        return []
    meta = data.get("_meta") if isinstance(data.get("_meta"), dict) else {}
    if meta.get("empty_allowlist") == "selection_lt_2":
        return []
    return ["transitions"]


def _gaps_research_rollup(data: dict[str, Any] | None) -> list[str]:
    """File presence is enough for early wave; thin meaning checked late via stage_completion."""
    if not data:
        return ["fields"]
    if not isinstance(data.get("fields"), dict) and not data.get("waves"):
        return ["fields"]
    return []


def _gaps_mastering_plan(data: dict[str, Any] | None) -> list[str]:
    if not data:
        return ["plan_status"]
    status = str(data.get("plan_status") or "")
    if not status:
        return ["plan_status"]
    return []


def _gaps_mmaudio_qa(data: dict[str, Any] | None) -> list[str]:
    if not data:
        return ["assets"]
    assets = data.get("assets")
    if not isinstance(assets, list):
        return ["assets"]
    # Empty list used to count as complete and left music-only runs with a
    # committed mmaudio_qa.json that never described the theme wavs on disk.
    if not assets:
        return ["assets"]
    return []

STAGE_GAP_RULES: dict[str, GapRule] = {
    "content_brief_reanchor": _gaps_content_brief_reanchor,
    "optimal_questions": lambda d: [],  # A-02 allowlist: empty gap_report OK
}

STAGED_ANALYSIS_STATE_GAP_RULES: dict[str, GapRule] = {
    "speaker_roles": _gaps_analysis_state_speakers_only,
}

ARTIFACT_COMPLETENESS_RULES: dict[str, GapRule] = {
    "understanding/analysis_state.json": _gaps_analysis_state,
    "understanding/content_brief.json": _gaps_content_brief,
    "understanding/speakers.json": _gaps_speakers,
    "understanding/sound_design_plan.json": _gaps_sound_design_plan,
    "understanding/investigation_queue.json": _gaps_investigation_queue,
    "understanding/gap_evaluations.json": _gaps_gap_evaluations,
    "understanding/gap_report.json": _gaps_gap_report,
    "understanding/delivery_brief.json": _gaps_generic_nonempty,
    "segments/boundaries.json": _gaps_boundaries,
    "segments/manifest.json": _gaps_manifest,
    "master/coverage_audit.json": _gaps_coverage_audit,
    "master/narrative_plan.json": _gaps_narrative_plan,
    "master/selection.json": _gaps_selection,
    "master/transitions.json": _gaps_transitions,
    "master/podcast_sfx_brief.json": _gaps_generic_nonempty,
    "show_notes/show_description.json": _gaps_generic_nonempty,
    "sound_design/sfx_prompts.json": _gaps_generic_nonempty,
    "sound_design/mmaudio_qa.json": _gaps_mmaudio_qa,
    "mastering/research/rollup.json": _gaps_research_rollup,
    "mastering/mastering_plan.json": _gaps_mastering_plan,
}

def _gap_rule_for(rel_path: str, stage_key: str | None = None) -> GapRule | None:
    if stage_key and stage_key in STAGE_GAP_RULES:
        rel_for_stage = STAGE_ARTIFACT_DISK_PATHS.get(stage_key)
        if rel_for_stage == rel_path:
            return STAGE_GAP_RULES[stage_key]
    return ARTIFACT_COMPLETENESS_RULES.get(rel_path)

def compute_gaps(
    rel_path: str,
    data: dict[str, Any] | None,
    *,
    stage_key: str | None = None,
    ctx: RunContext | None = None,
) -> list[Gap]:
    if stage_key:
        from interview_mux.sufficiency_engine import (
            evaluate,
            findings_to_gap_paths,
            sufficiency_enabled,
        )

        if sufficiency_enabled():
            raw = evaluate(stage_key, data, ctx)
            findings = raw.get("findings", raw) if isinstance(raw, dict) else raw
            if findings:
                return [Gap(path=p, reason="incomplete") for p in findings_to_gap_paths(findings)]
    rule = _gap_rule_for(rel_path, stage_key)
    if not rule:
        return []
    return [Gap(path=p, reason="incomplete") for p in rule(data)]

def compute_staged_write_gaps(
    rel_path: str,
    data: dict[str, Any] | None,
    *,
    stage_id: str,
) -> list[Gap]:
    """
    Semantic gap checks when flushing staged writes.
    Bundled sidecar artifacts (e.g. analysis_state during speaker_roles) use
    stage-appropriate rules instead of full downstream completeness.
    """
    producer = STAGE_ARTIFACT_DISK_PATHS.get(stage_id)
    if rel_path == "understanding/analysis_state.json" and producer != rel_path:
        rule = STAGED_ANALYSIS_STATE_GAP_RULES.get(stage_id)
        if rule is not None:
            return [Gap(path=p, reason="incomplete") for p in rule(data)]
        return []
    return compute_gaps(rel_path, data, stage_key=stage_id)

def _status_stage_key(rel_path: str, ctx: RunContext) -> str | None:
    if rel_path == "understanding/content_brief.json" and ctx.is_done("content_brief_reanchor"):
        return "content_brief_reanchor"
    return None


def artifact_status_for_stage(
    rel_path: str,
    ctx: RunContext,
    consumer_stage_id: str,
) -> str:
    """pending | partial | complete — semantic gaps scoped to the consuming stage."""
    if not ctx.artifact_exists(rel_path):
        return "pending"
    if rel_path.endswith(".txt") or rel_path.endswith(_BINARY_ARTIFACT_SUFFIXES):
        from interview_mux.write_staging import resolve_read_path

        if rel_path.endswith(".txt"):
            p = resolve_read_path(ctx, rel_path)
            try:
                return "complete" if p.is_file() and p.stat().st_size > 0 else "partial"
            except OSError:
                return "pending"
        return _binary_artifact_status(ctx, rel_path)
    raw = _safe_read_json_for_status(ctx, rel_path)
    if raw is None:
        return "pending"
    data = raw if isinstance(raw, dict) else None
    from interview_mux.llm_output_resilience import artifact_resilience_partial

    if artifact_resilience_partial(data):
        return "partial"
    schema_errors = validate_artifact_write(rel_path, data) if data else ["missing"]
    semantic = compute_gaps(rel_path, data, stage_key=consumer_stage_id, ctx=ctx)
    if rel_path == "sound_design/mmaudio_qa.json" and data:
        semantic.extend([Gap(path=p, reason="incomplete") for p in _mmaudio_qa_wav_parity_gaps(ctx, data)])
    if schema_errors or semantic:
        return "partial"
    return "complete"


def artifact_status(rel_path: str, ctx: RunContext) -> str:
    """pending | partial | complete"""
    if not ctx.artifact_exists(rel_path):
        return "pending"
    if rel_path.endswith(".txt") or rel_path.endswith(_BINARY_ARTIFACT_SUFFIXES):
        from interview_mux.write_staging import resolve_read_path

        if rel_path.endswith(".txt"):
            p = resolve_read_path(ctx, rel_path)
            try:
                return "complete" if p.is_file() and p.stat().st_size > 0 else "partial"
            except OSError:
                return "pending"
        return _binary_artifact_status(ctx, rel_path)
    raw = _safe_read_json_for_status(ctx, rel_path)
    if raw is None:
        return "pending"
    data = raw if isinstance(raw, dict) else None
    from interview_mux.llm_output_resilience import artifact_resilience_partial

    if artifact_resilience_partial(data):
        return "partial"
    schema_errors = validate_artifact_write(rel_path, data) if data else ["missing"]
    semantic = compute_gaps(rel_path, data, stage_key=_status_stage_key(rel_path, ctx), ctx=ctx)
    if rel_path == "sound_design/mmaudio_qa.json" and data:
        semantic.extend([Gap(path=p, reason="incomplete") for p in _mmaudio_qa_wav_parity_gaps(ctx, data)])
    if schema_errors or semantic:
        return "partial"
    return "complete"

def _mmaudio_qa_wav_parity_gaps(ctx: RunContext, data: dict[str, Any]) -> list[str]:
    out: list[str] = []
    assets = data.get("assets") if isinstance(data.get("assets"), list) else []
    qa_ids = {
        str(row.get("asset_id"))
        for row in assets
        if isinstance(row, dict) and row.get("asset_id")
    }
    try:
        from interview_mux.mmaudio_asset_qa import sound_design_asset_wav_ids

        wav_ids = sound_design_asset_wav_ids(ctx)
    except Exception:
        wav_ids = {p.stem for p in ctx.final_path("sound_design", "assets").glob("*.wav")}
    for aid in sorted(wav_ids - qa_ids):
        out.append(f"assets_missing_qa:{aid}")
    for aid in sorted(qa_ids - wav_ids):
        out.append(f"qa_missing_wav:{aid}")
    return out

def artifact_ready_for_review(rel_path: str, ctx: RunContext) -> bool:
    """True when artifact exists and passes schema + semantic completeness."""
    return artifact_status(rel_path, ctx) == "complete"

def analysis_profile_ready_for_review(ctx: RunContext) -> bool:
    """True after understanding analysis populated the interview profile."""
    from interview_mux.llm_flow_hardening import ANALYSIS_READY_ARTIFACT_PATHS, flow_hardening_enabled
    from interview_mux.stages import gaps

    if not gaps.gap_compose_stage_done(ctx):
        return False
    if flow_hardening_enabled():
        for rel in ANALYSIS_READY_ARTIFACT_PATHS:
            if artifact_status(rel, ctx) != "complete":
                return False
    if not ctx.artifact_exists("understanding/analysis_state.json"):
        return False
    raw = ctx.read_json("understanding/analysis_state.json")
    if not isinstance(raw, dict):
        return False
    completion = raw.get("completion") or {}
    if completion.get("analysis_ready"):
        return True
    return artifact_ready_for_review("understanding/analysis_state.json", ctx)

def story_board_ready_for_gui(ctx: RunContext) -> bool:
    """True when Story board panel has meaningful workspace content."""
    if not ctx.is_done("content_context"):
        return False
    return ctx.artifact_exists("understanding/content_brief.json")

def timeline_ready_for_gui(ctx: RunContext) -> bool:
    """True when NLE timeline has classified segments."""
    if not ctx.artifact_exists("segments/manifest.json"):
        return False
    manifest = ctx.read_json("segments/manifest.json")
    if not isinstance(manifest, dict):
        return False
    segments = manifest.get("segments")
    return isinstance(segments, list) and len(segments) > 0

def _deep_merge(
    base: dict[str, Any], patch: dict[str, Any], *, replace_lists: bool = False
) -> dict[str, Any]:
    out = copy.deepcopy(base)
    for key, val in patch.items():
        if val is None:
            continue
        if key in out and isinstance(out[key], dict) and isinstance(val, dict):
            out[key] = _deep_merge(out[key], val, replace_lists=replace_lists)
        elif key in out and isinstance(out[key], list) and isinstance(val, list):
            if replace_lists:
                out[key] = copy.deepcopy(val)
                continue
            if not val:
                continue
            if key in ("themes", "major_questions", "entities", "hypotheses", "open_questions"):
                from interview_mux.analysis_memory import merge_memory_updates

                merged, _ = merge_memory_updates({key: out.get(key, [])}, {key: val})
                out[key] = merged.get(key, out.get(key))
            else:
                out[key] = val if patch.get(f"{key}_replace") else out[key] + [
                    x for x in val if x not in out[key]
                ]
        else:
            out[key] = copy.deepcopy(val)
    return out

def merge_artifact(
    rel_path: str,
    existing: dict[str, Any] | None,
    patch: dict[str, Any],
    *,
    stage_key: str | None = None,
    preserve_operator: bool = True,
    replace_lists: bool = False,
    full_replace: bool = False,
) -> dict[str, Any]:
    """Merge a patch over the on-disk doc.

    ``replace_lists``: a stage's fresh output is complete, so its lists (an
    empty one included) replace the disk lists instead of being unioned with
    them. ``full_replace``: the doc is a single-owner judgement; only ``_meta``
    survives from disk. See ISSUES 162.
    """
    if not existing:
        return copy.deepcopy(patch)
    if not patch:
        return copy.deepcopy(existing)
    if full_replace:
        out = copy.deepcopy(patch)
        if isinstance(existing.get("_meta"), dict):
            meta = copy.deepcopy(existing["_meta"])
            if isinstance(patch.get("_meta"), dict):
                meta = _deep_merge(meta, patch["_meta"])
            out["_meta"] = meta
        return out

    if rel_path == "understanding/analysis_state.json" and preserve_operator:
        verified = bool((existing.get("meta") or {}).get("operator_verified"))
        if verified:
            protected = ("themes", "major_questions", "narrative", "style")
            patch = {k: v for k, v in patch.items() if k not in protected}

    if rel_path == "segments/manifest.json" and "segments" in patch:
        ex_segs = {s.get("segment_id"): s for s in (existing.get("segments") or []) if isinstance(s, dict)}
        for seg in patch.get("segments") or []:
            if isinstance(seg, dict) and seg.get("segment_id"):
                sid = seg["segment_id"]
                if sid in ex_segs:
                    ex_segs[sid] = {**ex_segs[sid], **seg}
                else:
                    ex_segs[sid] = seg
        from interview_mux.segment_timeline import sort_segments_by_start_ms

        merged_segments = sort_segments_by_start_ms(list(ex_segs.values()))
        return {**existing, "segments": merged_segments}

    if rel_path == "understanding/sound_design_plan.json":
        return _deep_merge(existing, patch, replace_lists=replace_lists)

    if rel_path == "understanding/speakers.json" and "speakers" in patch:
        ex_by_id: dict[str, dict[str, Any]] = {}
        for sp in existing.get("speakers") or []:
            if isinstance(sp, dict) and sp.get("speaker_id"):
                ex_by_id[str(sp["speaker_id"])] = copy.deepcopy(sp)
        for sp in patch.get("speakers") or []:
            if isinstance(sp, dict) and sp.get("speaker_id"):
                sid = str(sp["speaker_id"])
                if sid in ex_by_id:
                    ex_by_id[sid] = {**ex_by_id[sid], **sp}
                else:
                    ex_by_id[sid] = copy.deepcopy(sp)
        return {**existing, "speakers": list(ex_by_id.values())}

    return _deep_merge(existing, patch, replace_lists=replace_lists)

def _kept_split_child_ids(
    ctx: RunContext,
    manifest_by_id: dict[str, dict[str, Any]],
    contract_ids: set[str],
) -> set[str]:
    """NLE/CTA recut children that must stay in the manifest candidate pool."""
    kept: set[str] = set()
    for sid, row in manifest_by_id.items():
        parent = str((row or {}).get("parent_id") or "")
        if parent and (not contract_ids or parent in contract_ids):
            kept.add(str(sid))
    try:
        from interview_mux.nle_state import load_nle

        overrides = (load_nle(ctx).get("segment_overrides") or {})
        for sid, ov in overrides.items():
            if not isinstance(ov, dict):
                continue
            parent = str(ov.get("parent_id") or "")
            if parent and (not contract_ids or parent in contract_ids) and ov.get("start_ms") is not None:
                kept.add(str(sid))
    except Exception:
        pass
    try:
        from interview_mux.media_ip_cta import admitted_story_segment_ids

        kept |= admitted_story_segment_ids(ctx)
    except Exception:
        pass
    return {s for s in kept if s}


def _split_child_row_from_parent(
    ctx: RunContext,
    child_id: str,
    manifest_by_id: dict[str, dict[str, Any]],
) -> dict[str, Any] | None:
    try:
        from interview_mux.nle_state import load_nle

        ov = (load_nle(ctx).get("segment_overrides") or {}).get(child_id) or {}
    except Exception:
        ov = {}
    if not isinstance(ov, dict) or ov.get("start_ms") is None:
        return None
    parent = manifest_by_id.get(str(ov.get("parent_id") or "")) or {}
    try:
        start = int(ov.get("start_ms") or 0)
        end = int(ov.get("end_ms") or 0)
    except (TypeError, ValueError):
        return None
    if end <= start:
        return None
    return {
        "segment_id": child_id,
        "start_ms": start,
        "end_ms": end,
        "speaker_id": str(parent.get("speaker_id") or "spk_unknown"),
        "speaker_role": str(parent.get("speaker_role") or "unknown"),
        "type": str(parent.get("type") or "interviewee_answer"),
        "topic_tags": list(parent.get("topic_tags") or []),
        "text": str(ov.get("label") or parent.get("text") or ""),
        "parent_id": str(ov.get("parent_id") or ""),
    }


def aligned_manifest_segments(
    boundary_rows: list[dict[str, Any]],
    existing_segments: list[Any],
) -> list[dict[str, Any]]:
    """Manifest rows in boundary order. Keep a saved row when its id still exists."""
    existing: dict[str, dict[str, Any]] = {}
    for seg in existing_segments:
        if isinstance(seg, dict) and seg.get("segment_id"):
            existing[str(seg["segment_id"])] = dict(seg)
    aligned: list[dict[str, Any]] = []
    for row in boundary_rows:
        if not isinstance(row, dict) or not row.get("segment_id"):
            continue
        sid = str(row["segment_id"])
        seg = dict(existing.get(sid) or {"segment_id": sid})
        seg["segment_id"] = sid
        if row.get("start_ms") is not None:
            seg["start_ms"] = int(row["start_ms"])
        if row.get("end_ms") is not None:
            seg["end_ms"] = int(row["end_ms"])
        if row.get("speaker_id"):
            seg["speaker_id"] = str(row["speaker_id"])
        aligned.append(seg)
    return aligned


def align_manifest_ids_to_boundaries(ctx: RunContext) -> bool:
    """Rewrite the manifest so its ids are exactly the saved boundary ids.

    A renumber that leaves the old manifest makes the classification check
    refuse the stage. Missing manifest with boundaries on disk is the same
    repair: build the rows from the boundaries.
    """
    if not ctx.artifact_exists("segments/boundaries.json"):
        return False
    raw = ctx.read_json("segments/boundaries.json")
    if not isinstance(raw, dict):
        return False
    rows = [r for r in (raw.get("boundaries") or []) if isinstance(r, dict)]
    if not rows:
        return False
    manifest: dict[str, Any] = {"version": 1, "segments": []}
    if ctx.artifact_exists("segments/manifest.json"):
        loaded = ctx.read_json("segments/manifest.json")
        if isinstance(loaded, dict):
            manifest = dict(loaded)
    existing = manifest.get("segments") if isinstance(manifest.get("segments"), list) else []
    aligned = aligned_manifest_segments(rows, existing)
    # NLE/CTA split children live in the manifest only, never in the
    # boundaries, and each sits inside its parent's span. Rebuilding from the
    # boundaries alone deleted them, so ranking's selection named ids the
    # manifest no longer had and every flush was refused.
    boundary_ids = {str(s.get("segment_id")) for s in aligned if s.get("segment_id")}
    by_existing = {
        str(s.get("segment_id")): s
        for s in existing
        if isinstance(s, dict) and s.get("segment_id")
    }
    kept_children = _kept_split_child_ids(ctx, by_existing, boundary_ids)
    children = [
        dict(by_existing[sid])
        for sid in sorted(kept_children)
        if sid in by_existing and sid not in boundary_ids
    ]
    old_ids = {
        str(s.get("segment_id"))
        for s in existing
        if isinstance(s, dict) and s.get("segment_id")
    }
    new_ids = {str(s.get("segment_id")) for s in aligned + children if s.get("segment_id")}
    if old_ids == new_ids and existing:
        return False
    if existing and len(aligned) + len(children) < len(existing):
        def _mid_covered(seg: dict[str, Any]) -> bool:
            try:
                mid = (int(seg.get("start_ms") or 0) + int(seg.get("end_ms") or 0)) // 2
            except (TypeError, ValueError):
                return True
            for row in rows:
                try:
                    if int(row.get("start_ms") or 0) <= mid <= int(row.get("end_ms") or 0):
                        return True
                except (TypeError, ValueError):
                    continue
            return False

        uncovered = [
            seg
            for seg in existing
            if isinstance(seg, dict)
            and seg.get("segment_id")
            and str(seg.get("segment_id")) not in new_ids
            and not _mid_covered(seg)
        ]
        if uncovered:
            return False
        def _span(rows: list[Any]) -> tuple[int, int] | None:
            starts: list[int] = []
            ends: list[int] = []
            for row in rows:
                if not isinstance(row, dict):
                    continue
                try:
                    starts.append(int(row.get("start_ms") or 0))
                    ends.append(int(row.get("end_ms") or 0))
                except (TypeError, ValueError):
                    continue
            if not starts:
                return None
            return min(starts), max(ends)

        boundary_span = _span(rows)
        manifest_span = _span(existing)
        if boundary_span and manifest_span:
            if boundary_span[1] < manifest_span[1] - 2000 or boundary_span[0] > manifest_span[0] + 2000:
                return False
    manifest["segments"] = aligned
    from interview_mux.artifact_repairs import repair_manifest_segments
    from interview_mux.write_staging import write_committed_json

    repaired, _notes = repair_manifest_segments(ctx, manifest)
    if children:
        # After the repair: its overlap trim drops any row inside its parent.
        repaired = dict(repaired)
        repaired["segments"] = sorted(
            list(repaired.get("segments") or []) + children,
            key=lambda s: (int(s.get("start_ms") or 0), int(s.get("end_ms") or 0)),
        )
    write_committed_json(
        ctx,
        "segments/manifest.json",
        repaired,
        stage_key="segment_classification",
    )
    return True


_RETIRED_LIST_KEYS = frozenset(
    {
        "segment_ids",
        "evidence_segment_ids",
        "bound_segment_ids",
        "segment_order",
    }
)


def _segment_token(value: Any) -> bool:
    return isinstance(value, str) and value.startswith("seg_")


def strip_retired_segment_ids(node: Any, manifest_ids: set[str]) -> Any:
    """Drop segment ids that are not on the saved manifest. Leave other fields."""
    if isinstance(node, list):
        kept: list[Any] = []
        for item in node:
            if (
                isinstance(item, dict)
                and _segment_token(item.get("segment_id"))
                and str(item.get("segment_id")) not in manifest_ids
            ):
                continue
            kept.append(strip_retired_segment_ids(item, manifest_ids))
        return kept
    if not isinstance(node, dict):
        return node
    out: dict[str, Any] = {}
    for key, value in node.items():
        if key == "cue_slots" and isinstance(value, list):
            kept = []
            for slot in value:
                if (
                    isinstance(slot, dict)
                    and _segment_token(slot.get("segment_id"))
                    and str(slot.get("segment_id")) not in manifest_ids
                ):
                    continue
                kept.append(strip_retired_segment_ids(slot, manifest_ids))
            out[key] = kept
            continue
        if key in _RETIRED_LIST_KEYS and isinstance(value, list):
            out[key] = [
                item
                for item in value
                if not _segment_token(item) or str(item) in manifest_ids
            ]
            continue
        if key == "segment_id" and _segment_token(value) and str(value) not in manifest_ids:
            out[key] = ""
            continue
        if isinstance(value, (dict, list)):
            out[key] = strip_retired_segment_ids(value, manifest_ids)
        else:
            out[key] = value
    return out


def drop_retired_segment_refs(ctx: RunContext) -> list[str]:
    """Remove retired segment ids from the files a flush compares to the manifest."""
    if not ctx.artifact_exists("segments/manifest.json"):
        return []
    manifest = ctx.read_json("segments/manifest.json")
    if not isinstance(manifest, dict):
        return []
    manifest_ids = {
        str(s.get("segment_id"))
        for s in (manifest.get("segments") or [])
        if isinstance(s, dict) and s.get("segment_id")
    }
    if not manifest_ids:
        return []
    targets = (
        ("understanding/content_brief.json", "content_brief_reanchor"),
        ("master/narrative_plan.json", "narrative_arc_plan"),
        ("understanding/soundscape_policy.json", "soundscape_policy_build"),
        ("understanding/episode_structure.json", "episode_structure_compose"),
        ("understanding/gap_evaluations.json", "missing_framing"),
        ("master/coverage_audit.json", "topic_coverage_audit"),
    )
    from interview_mux.write_staging import write_committed_json

    updated: list[str] = []
    for rel, stage_key in targets:
        if not ctx.artifact_exists(rel):
            continue
        try:
            doc = ctx.read_json(rel)
        except Exception:
            continue
        if not isinstance(doc, dict):
            continue
        stripped = strip_retired_segment_ids(doc, manifest_ids)
        if stripped == doc:
            continue
        try:
            write_committed_json(ctx, rel, stripped, stage_key=stage_key)
            updated.append(rel)
        except Exception:
            try:
                ctx.log(
                    f"retired segment ids left in {rel}",
                    level="warning",
                    stage=stage_key,
                )
            except Exception:
                pass
    return updated


def hydrate_manifest_from_boundaries(ctx: RunContext, manifest: dict[str, Any]) -> dict[str, Any]:
    """Fill timeline fields on manifest segments from segments/boundaries.json."""
    if not isinstance(manifest, dict):
        return manifest
    segs = manifest.get("segments")
    if not isinstance(segs, list):
        return manifest

    from interview_mux.segment_timeline_standard import contract_ordered_segment_ids, segmentation_cfg

    boundary_doc: dict[str, Any] | None = None
    if ctx.artifact_exists("segments/boundaries.json"):
        raw = ctx.read_json("segments/boundaries.json")
        if isinstance(raw, dict):
            boundary_doc = raw

    boundary_by_id: dict[str, dict[str, Any]] = {}
    if boundary_doc:
        for row in boundary_doc.get("boundaries") or []:
            if isinstance(row, dict) and row.get("segment_id"):
                boundary_by_id[str(row["segment_id"])] = row

    contract_ids = contract_ordered_segment_ids(boundary_doc)
    if not contract_ids and not segs:
        return manifest
    if not boundary_by_id:
        return manifest

    words: list[dict[str, Any]] = []
    if ctx.artifact_exists("transcript/full.json"):
        tr = ctx.read_json("transcript/full.json")
        if isinstance(tr, dict):
            words = [w for w in (tr.get("words") or []) if isinstance(w, dict)]

    speakers_by_id: dict[str, str] = {}
    if ctx.artifact_exists("understanding/speakers.json"):
        sp_doc = ctx.read_json("understanding/speakers.json")
        if isinstance(sp_doc, dict):
            for sp in sp_doc.get("speakers") or []:
                if isinstance(sp, dict) and sp.get("speaker_id"):
                    speakers_by_id[str(sp["speaker_id"])] = str(sp.get("role") or "unknown")

    manifest_by_id = {
        str(seg.get("segment_id")): dict(seg)
        for seg in segs
        if isinstance(seg, dict) and seg.get("segment_id")
    }
    seg_cfg = segmentation_cfg()
    order = contract_ids or sorted(manifest_by_id.keys())
    hydrated: list[dict[str, Any]] = []
    for sid in order:
        out = dict(manifest_by_id.get(sid) or {"segment_id": sid})
        out["segment_id"] = sid
        boundary = boundary_by_id.get(sid)
        if not boundary:
            hydrated.append(out)
            continue
        if boundary.get("start_ms") is not None:
            out["start_ms"] = int(boundary["start_ms"])
        if boundary.get("end_ms") is not None:
            out["end_ms"] = int(boundary["end_ms"])
        if boundary.get("speaker_id"):
            out["speaker_id"] = str(boundary["speaker_id"])
        speaker_id = str(out.get("speaker_id") or "")
        if speaker_id:
            out["speaker_role"] = speakers_by_id.get(speaker_id, out.get("speaker_role") or "unknown")
        if words and out.get("start_ms") is not None and out.get("end_ms") is not None:
            start_ms = int(out["start_ms"])
            end_ms = int(out["end_ms"])
            span = [
                w
                for w in words
                if int(w.get("start_ms", 0)) < end_ms and int(w.get("end_ms", 0)) > start_ms
            ]
            text = " ".join(str(w.get("text", "")) for w in span if w.get("text"))
            if text:
                out["text"] = text
        # Boundary-only stubs must still satisfy manifest schema required fields.
        if "topic_tags" not in out or out.get("topic_tags") is None:
            out["topic_tags"] = []
        if not out.get("speaker_id"):
            out["speaker_id"] = "spk_unknown"
        if not out.get("speaker_role") or str(out.get("speaker_role")) not in {
            "interviewer",
            "interviewee",
            "unknown",
        }:
            out["speaker_role"] = speakers_by_id.get(str(out.get("speaker_id") or ""), "unknown")
        if not out.get("type"):
            role = str(out.get("speaker_role") or "unknown").lower()
            out["type"] = (
                "interviewer_question"
                if role in {"interviewer", "host", "moderator", "co_host", "frame"}
                else "interviewee_answer"
            )
        hydrated.append(out)

    from interview_mux.segment_timeline import sort_segments_by_start_ms

    kept_children = _kept_split_child_ids(ctx, manifest_by_id, set(contract_ids or []))
    seen = {str(row.get("segment_id") or "") for row in hydrated}
    for sid in kept_children:
        if sid in seen:
            continue
        extra = manifest_by_id.get(sid)
        if not isinstance(extra, dict):
            extra = _split_child_row_from_parent(ctx, sid, manifest_by_id)
        if isinstance(extra, dict):
            hydrated.append(dict(extra))
            seen.add(sid)

    if seg_cfg.get("drop_orphan_manifest_rows", True) and contract_ids:
        allowed = set(contract_ids) | kept_children
        hydrated = [row for row in hydrated if str(row.get("segment_id")) in allowed]

    return {**manifest, "segments": sort_segments_by_start_ms(hydrated)}



def build_gap_fill_context(ctx: RunContext, stage_key: str) -> dict[str, Any] | None:
    from interview_mux.null_field_policy import null_acknowledged_paths

    rel = STAGE_ARTIFACT_DISK_PATHS.get(stage_key)
    if not rel:
        return None
    existing: dict[str, Any] | None = None
    if ctx.artifact_exists(rel):
        raw = ctx.read_json(rel)
        if isinstance(raw, dict):
            existing = raw
    gaps = compute_gaps(rel, existing, stage_key=stage_key)
    schema_errors = validate_artifact_write(rel, existing) if existing else []
    stage_errors = []
    if existing and stage_key:
        stage_errors = validate_stage_artifacts(stage_key, existing)

    all_gaps = list({g.path for g in gaps})
    for e in schema_errors + stage_errors:
        all_gaps.append(e.split(":")[0] if ":" in e else e)
    if existing:
        ack = set(null_acknowledged_paths(existing))
        all_gaps = [g for g in all_gaps if g not in ack and not any(g.startswith(a) for a in ack)]

    if not existing and not all_gaps:
        all_gaps = ["(root)"]

    skip_fields: list[str] = []
    if existing:
        skip_fields.extend(null_acknowledged_paths(existing))
        rule = _gap_rule_for(rel, stage_key)
        if rule:
            complete_paths = set()
            probe = copy.deepcopy(existing)
            for g in gaps:
                pass
            for key in list(existing.keys()):
                trial = copy.deepcopy(existing)
                if key in trial:
                    del trial[key]
                if rule(trial) == rule(existing):
                    skip_fields.append(key)

    if existing and not all_gaps:
        return {
            "artifact_path": rel,
            "existing": existing,
            "gaps": [],
            "skip_fields": _filter_protected_skip_fields(list(existing.keys())),
            "instructions": "Artifact is complete; return empty artifacts unless correcting errors.",
        }

    return {
        "artifact_path": rel,
        "existing": existing,
        "gaps": all_gaps[: gap_fill_cap(len(all_gaps))],
        "skip_fields": _filter_protected_skip_fields(skip_fields[:32]),
        "instructions": (
            "Only fill listed gaps. Do not overwrite skip_fields or satisfied keys in existing. "
            "Return patch-only artifacts when existing is non-null."
        ),
    }


def should_run_stage_for_artifact(ctx: RunContext, stage_key: str) -> bool:
    rel = STAGE_ARTIFACT_DISK_PATHS.get(stage_key)
    if not rel:
        return True
    if not ctx.artifact_exists(rel):
        return True
    raw = ctx.read_json(rel)
    if not isinstance(raw, dict):
        return True
    from interview_mux.llm_output_resilience import artifact_resilience_partial

    if artifact_resilience_partial(raw):
        return True
    if validate_artifact_write(rel, raw):
        return True
    if compute_gaps(rel, raw, stage_key=stage_key):
        return True
    stage_errors = validate_stage_artifacts(stage_key, raw)
    return bool(stage_errors)

def stage_keys_for_artifact_path(rel_path: str) -> list[str]:
    return [k for k, p in STAGE_ARTIFACT_DISK_PATHS.items() if p == rel_path]


def preferred_fill_stage(rel_path: str, ctx: RunContext) -> str | None:
    """LLM stage to re-run when filling gaps on ``rel_path``.

    Shared artifacts (content_brief, boundaries) have an early writer and a later
    re-writer. After the later contract is in force, fill-gaps must not rewind to
    the early producer.
    """
    keys = stage_keys_for_artifact_path(rel_path)
    if not keys:
        return None
    if rel_path == "understanding/content_brief.json":
        if ctx.artifact_exists("segments/manifest.json"):
            return "content_brief_reanchor"
        return "content_context"
    return keys[-1] if ctx.is_done(keys[-1]) else keys[0]

#: Single-owner verdict reports: a new pass replaces the old one wholesale
#: (only ``_meta`` carries over), so a cleared complaint or a stale repair note
#: cannot survive a re-audit (exec_016, ISSUES 162).
FULL_REPLACE_STAGE_ARTIFACTS: frozenset[tuple[str, str]] = frozenset(
    {
        ("master/edl_narrative_audit.json", "edl_narrative_audit"),
        ("master/coverage_audit.json", "topic_coverage_audit"),
    }
)

#: Persists that keep the list union. boundary_topic_resplit shares
#: boundaries.json with boundary_detection and has no row floor yet, so a reply
#: carrying only the split rows must not drop the untouched ones.
UNION_LIST_STAGE_ARTIFACTS: frozenset[tuple[str, str]] = frozenset(
    {("segments/boundaries.json", "boundary_topic_resplit")}
)


def make_stage_persist(
    rel_path: str,
    stage_key: str,
    *,
    transform: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
) -> Callable[[RunContext, dict[str, Any]], None]:
    """Persist one attempt's complete output over the on-disk doc.

    Lists from the attempt replace the disk lists. The union used to keep a
    failed attempt's chapters next to the retry's (two ch_05 in one
    narrative plan) and a cleared audit complaint next to the re-audit that
    cleared it, so the verdict stayed "fail" and the commit barrier refused
    every pass to the thrash cap (exec_016, ISSUES 162).
    """
    from interview_mux.artifact_writes import write_validated_artifact

    key = (rel_path, stage_key)
    full_replace = key in FULL_REPLACE_STAGE_ARTIFACTS
    replace_lists = key not in UNION_LIST_STAGE_ARTIFACTS

    def persist(ctx: RunContext, artifacts: dict[str, Any]) -> None:
        data = transform(artifacts) if transform else artifacts
        write_validated_artifact(
            ctx,
            rel_path,
            data,
            merge_from_disk=True,
            stage_key=stage_key,
            replace_lists=replace_lists,
            full_replace=full_replace,
        )

    return persist

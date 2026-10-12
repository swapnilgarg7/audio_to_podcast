"""Nugget Layup System — flagship full-tape mining → per-native pre-VO lay-ups.

Authoritative planner for contentful ``before`` synthetic VO. Publishes into
``understanding/gap_report.json`` for G1 synthesis and EDL placement.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from interview_mux.config import merged_config
from interview_mux.run_context import RunContext

CORPUS_REL = "understanding/nugget_corpus.json"
PLAN_REL = "understanding/nugget_layup_plan.json"
GAP_REL = "understanding/gap_report.json"
GAP_DRAFT_REL = "understanding/gap_report.draft.json"
GAP_WRITE_LOCK_REL = "understanding/.gap_report.write.lock"
QC_REL = "understanding/nugget_layup_qc.json"
MASKS_REL = "understanding/native_comprehension_masks.json"
LAYUP_FLOOR_UNSAT_REL = "operator/escalations/nugget_layup_compose.json"
# Craft QC tokens that H1 spine/skip may heal without another LLM round.
CRAFT_SPINE_ERROR_MARKERS = frozenset(
    {
        "invented_island",
        "canned_air",
        "thin_layup",
        "restates_target",
        "spoken_copy",
        "insufficient_analysis",
    }
)
COMPREHENSION_INDEX_REL = "understanding/nugget_comprehension_index.json"
LAYUP_CANDIDATES_REL = "understanding/layup_candidates.json"
LAYUP_CANDIDATES_ARCHIVE = "understanding/.archived/layup_candidates"
LAYUP_AIR_ADVISORIES_META_KEY = "layup_air_advisories"

# LLM analysis fields that make a lay-up a *constructed* next-native setup
# instead of a generic hinge. Required on every non-skip row.
ANALYSIS_FIELDS = ("target_beat", "listener_need_entering_T", "forward_unlock")
REQUIRED_ANALYSIS_FIELDS = ("target_beat", "listener_need_entering_T")

# Body lines that may legitimately survive publish under layup authority.
AUTHORITY_BODY_ORIGINS = frozenset(
    {"nugget_layup", "operator", "high_gap_vo_fill", "vo_line_adjudicate"}
)

_SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+")
_WORD = re.compile(r"[A-Za-z0-9₹$%]+")
_UNLOCK_STOPWORDS = frozenset(
    {
        "a", "after", "an", "and", "are", "as", "at", "be", "been", "by", "come",
        "comes", "did", "do", "does", "for", "from", "had", "happen", "happens",
        "how", "in", "is", "it", "its", "made", "make", "next", "of", "on",
        "once", "point", "shape", "so", "story", "that", "the", "then", "there",
        "this", "to", "was", "were", "what", "when", "where", "which", "with",
    }
)
_GENERIC_UNLOCK_STARTS = (
    "what changed",
    "what happened",
    "what comes",
    "what follows",
    "what shifted",
    "what did that",
    "how did that",
    "where does this",
    "coming up",
    "stepping back",
)


def nugget_layup_cfg(cfg: dict[str, Any] | None = None) -> dict[str, Any]:
    root = cfg if isinstance(cfg, dict) else merged_config()
    analysis = root.get("analysis") if isinstance(root.get("analysis"), dict) else {}
    block = analysis.get("nugget_layup") if isinstance(analysis.get("nugget_layup"), dict) else {}
    return {
        "enabled": bool(block.get("enabled", True)),
        "require_layup_per_native": bool(block.get("require_layup_per_native", True)),
        "min_layup_coverage": float(block.get("min_layup_coverage", 0.70)),
        "min_nugget_air_coverage": float(block.get("min_nugget_air_coverage", 0.85)),
        # NLC-B2 aspirational: 0.85 is a goal; structural accounting stays hard.
        "air_coverage_aspirational": bool(block.get("air_coverage_aspirational", True)),
        # S4: best-of-≤2 only — no long candidate archive / oscillation thrash.
        "air_coverage_max_attempts": max(1, min(2, int(block.get("air_coverage_max_attempts") or 2))),
        "catastrophic_nugget_air_coverage": float(
            block.get("catastrophic_nugget_air_coverage", 0.0)
        ),
        "min_layup_words": int(block.get("min_layup_words", 18)),
        "max_layup_words": int(block.get("max_layup_words", 90)),
        "prefer_excluded_nuggets": bool(block.get("prefer_excluded_nuggets", True)),
        "segment_text_max_chars": int(block.get("segment_text_max_chars", 4000)),
        # Full-tape mining (ISSUES 191): words between segments and words a
        # cut trims off a kept segment stay in the running for synthetic VO.
        "full_tape_spans": bool(block.get("full_tape_spans", True)),
        "between_span_split_gap_ms": int(block.get("between_span_split_gap_ms", 1500)),
        "between_span_max_chars": int(block.get("between_span_max_chars", 4000)),
        "cut_words_before_max_chars": int(block.get("cut_words_before_max_chars", 600)),
        "tape_window_boost": float(block.get("tape_window_boost", 0.6)),
        "prior_close_excerpt_chars": int(block.get("prior_close_excerpt_chars", 480)),
        "max_open_nuggets_per_target": int(block.get("max_open_nuggets_per_target", 8)),
        # Ranked open-nugget rows are pointers into top-level nugget_corpus (claims live once).
        "slim_open_nuggets": bool(block.get("slim_open_nuggets", True)),
        # Flagship compose shards when the air order is long (input + output token budgets).
        "compose_batch_max_natives": int(block.get("compose_batch_max_natives", 32)),
        "require_analysis_fields": bool(block.get("require_analysis_fields", True)),
        "ban_canned_air": bool(block.get("ban_canned_air", True)),
        "unique_nuggets_across_layups": bool(
            block.get("unique_nuggets_across_layups", False)
        ),
        "max_cross_layup_overlap": float(block.get("max_cross_layup_overlap", 0.6)),
        "max_target_restate_overlap": float(block.get("max_target_restate_overlap", 0.75)),
        "suppress_placeholder_seams_when_layup": bool(
            block.get("suppress_placeholder_seams_when_layup", True)
        ),
        "demote_synthetic_framing_content": bool(
            block.get("demote_synthetic_framing_content", True)
        ),
        "authoritative_gap_report": bool(block.get("authoritative_gap_report", True)),
        # Open coverage is a goal, not a stop (ISSUES 185): default advisory.
        "block_on_open_must_keep": bool(block.get("block_on_open_must_keep", False)),
        "block_on_open_high_salience": bool(block.get("block_on_open_high_salience", False)),
        "honor_information_package_dense_budget": bool(
            block.get("honor_information_package_dense_budget", True)
        ),
        "degraded_layup": {
            "enabled": True,
            "comprehensible_min_token_conf": 0.85,
            "grace_min_layup_words": 12,
            "allow_thinner_forward_unlock": True,
            "extra_degraded_regenerate": True,
            "exclude_unclear_from_llm_verbatim": True,
            "include_unclear_span_summaries": True,
            **(
                block.get("degraded_layup")
                if isinstance(block.get("degraded_layup"), dict)
                else {}
            ),
        },
    }


def nugget_layup_enabled(cfg: dict[str, Any] | None = None) -> bool:
    return bool(nugget_layup_cfg(cfg).get("enabled", True))


def _ordered_ids(ctx: RunContext) -> list[str]:
    if not ctx.artifact_exists("master/selection.json"):
        return []
    sel = ctx.read_json("master/selection.json")
    if not isinstance(sel, dict):
        return []
    return [str(x) for x in (sel.get("ordered_segment_ids") or []) if x]


def _manifest_segments(ctx: RunContext) -> list[dict[str, Any]]:
    if not ctx.artifact_exists("segments/manifest.json"):
        return []
    man = ctx.read_json("segments/manifest.json")
    if not isinstance(man, dict):
        return []
    rows = man.get("segments") or []
    return [r for r in rows if isinstance(r, dict)]


def _clip_text(text: str, max_chars: int) -> str:
    t = " ".join(str(text or "").split())
    if len(t) <= max_chars:
        return t
    return t[: max(0, max_chars - 1)].rstrip() + "…"


def _clip_tail(text: str, max_chars: int) -> str:
    t = " ".join(str(text or "").split())
    if len(t) <= max_chars:
        return t
    return "…" + t[-max(0, max_chars - 1) :].lstrip()


def _norm(text: str) -> str:
    return " ".join(str(text or "").strip().split())


def _tokens(text: str) -> set[str]:
    return {w.casefold() for w in _WORD.findall(_norm(text)) if len(w) > 2}


def _overlap(a: set[str], b: set[str]) -> float:
    if len(a) < 4 or len(b) < 4:
        return 0.0
    return len(a & b) / max(1, min(len(a), len(b)))


def layup_freshness_errors(
    ctx: RunContext,
    plan: dict[str, Any] | None = None,
) -> list[str]:
    """Reasons the on-disk lay-up plan no longer matches the committed air order."""
    if not nugget_layup_enabled():
        return []
    if plan is None:
        plan = ctx.read_json(PLAN_REL) if ctx.artifact_exists(PLAN_REL) else {}
    if not isinstance(plan, dict) or not (plan.get("layups") or plan.get("ordered_segment_ids")):
        return []
    errors: list[str] = []
    meta = plan.get("_meta") if isinstance(plan.get("_meta"), dict) else {}
    if meta.get("stale"):
        errors.append(
            "nugget_layup_plan marked stale "
            f"({meta.get('stale_reason') or 'upstream fix'}) — re-run nugget_layup_compose"
        )
    selection = _ordered_ids(ctx)
    planned = [str(x) for x in (plan.get("ordered_segment_ids") or []) if x]
    if selection and planned != selection:
        missing = [sid for sid in selection if sid not in set(planned)]
        extra = [sid for sid in planned if sid not in set(selection)]
        # Extras (dropped bumper/outro children) are as stale as missing natives.
        # Matching the selection *set* while keeping a longer order must fail closed.
        errors.append(
            "nugget_layup_plan ordered_segment_ids do not match master/selection.json "
            f"(missing={missing[:8]}, stale={extra[:8]})"
        )
    # Order-lock content mismatch even when lists somehow match. A revision
    # that differs while the content hash and the id list agree is paperwork,
    # not staleness: the selection order moved and came back (sanitize bumps
    # the revision on every pass) and the plan was built for exactly this
    # order. Treating it as stale re-dispatched compose until the invoke cap
    # while the hard freeze refused the restamp (ISSUES 190).
    try:
        from interview_mux.order_hash import get_order_lock, order_locks_same_content

        if ctx.artifact_exists("master/selection.json"):
            sel_doc = ctx.read_json("master/selection.json")
            if isinstance(sel_doc, dict) and get_order_lock(sel_doc) and get_order_lock(plan):
                if not order_locks_same_content(sel_doc, plan):
                    errors.append(
                        "nugget_layup_plan order_lock diverges from selection "
                        f"(sel_rev={(get_order_lock(sel_doc) or {}).get('revision')}, "
                        f"plan_rev={(get_order_lock(plan) or {}).get('revision')})"
                    )
    except Exception:
        pass
    return errors


def layup_rows_off_order(
    ctx: RunContext,
    plan: dict[str, Any] | None = None,
    *,
    rebindable_only: bool = False,
) -> list[str]:
    """Lay-up targets that are no longer on the committed air order.

    Not a freshness error (the one-writer admit already copies the selection
    ids onto the plan on write, so the id list can agree while a row still
    aims at a retired id); it is the reconcile trigger that rebinds the row.
    With ``rebindable_only`` only targets whose tape overlaps a live native
    count: those are the rows ``adopt_layup_plan_to_selection`` can move. A
    retired id with no live tape stays for the sanitizer to drop.
    """
    if plan is None:
        plan = ctx.read_json(PLAN_REL) if ctx.artifact_exists(PLAN_REL) else {}
    if not isinstance(plan, dict):
        return []
    ordered = _ordered_ids(ctx)
    live = set(ordered)
    if not live:
        return []
    windows = _segment_windows(ctx) if rebindable_only else {}
    out: list[str] = []
    for row in plan.get("layups") or []:
        if not isinstance(row, dict):
            continue
        tid = str(row.get("target_segment_id") or "").strip()
        if not tid or tid in live or tid in out:
            continue
        if rebindable_only:
            tw = windows.get(tid)
            if not tw or not any(
                windows.get(sid) and _overlap_ms(tw[0], tw[1], windows[sid][0], windows[sid][1]) > 0
                for sid in ordered
            ):
                continue
        out.append(tid)
    return out


def layup_plan_lock_revision_drift(ctx: RunContext, plan: dict[str, Any] | None = None) -> bool:
    """True when the plan lock matches the selection order but not its revision."""
    if plan is None:
        plan = ctx.read_json(PLAN_REL) if ctx.artifact_exists(PLAN_REL) else {}
    if not isinstance(plan, dict) or not ctx.artifact_exists("master/selection.json"):
        return False
    try:
        from interview_mux.order_hash import order_lock_revision_only_drift

        sel_doc = ctx.read_json("master/selection.json")
        return bool(isinstance(sel_doc, dict) and order_lock_revision_only_drift(sel_doc, plan))
    except Exception:
        return False


def restamp_layup_plan_order_lock(ctx: RunContext, *, stage: str = "nugget_layup_compose") -> bool:
    """Copy the selection lock onto a plan that differs only by revision.

    A courtesy write: refused under ownership or freeze it is skipped quietly
    (the plan is fresh either way, see ``layup_freshness_errors``). Returns
    True when the restamp landed.
    """
    if not ctx.artifact_exists(PLAN_REL):
        return False
    try:
        plan = ctx.read_json(PLAN_REL)
    except Exception:
        return False
    if not isinstance(plan, dict) or not layup_plan_lock_revision_drift(ctx, plan):
        return False
    stamped = attach_selection_order_lock(ctx, plan)
    try:
        ctx.write_json(PLAN_REL, stamped, skip_handoff=True, stage_key=stage, optional=True)
    except Exception:
        return False
    try:
        again = ctx.read_json(PLAN_REL)
    except Exception:
        return False
    return not layup_plan_lock_revision_drift(ctx, again if isinstance(again, dict) else None)


def assert_layup_fresh_vs_selection(
    ctx: RunContext,
    plan: dict[str, Any] | None = None,
    *,
    stage: str = "nugget_layup_compose",
) -> None:
    """Fail closed rather than publish a lay-up plan built for a different order."""
    errors = layup_freshness_errors(ctx, plan)
    if not errors:
        return
    from interview_mux.loud_fail import raise_loud_failure

    raise_loud_failure(
        ctx,
        "Nugget layup plan is stale vs selection: " + "; ".join(errors[:4]),
        stage=stage,
        reason="nugget_layup_plan_stale",
        detail={"errors": errors[:8]},
    )


_CANNED_AIR_CACHE: frozenset[str] | None = None


def canned_air_phrases() -> frozenset[str]:
    """Normalized deterministic hinges that must never ship as lay-up air copy."""
    global _CANNED_AIR_CACHE
    if _CANNED_AIR_CACHE is None:
        from interview_mux.seam_glue import (
            CANNED_BRIDGE_TEXT,
            FORWARD_HINGES,
            GENERIC_RELATIVE_HINGES,
            REVERSE_HINGES,
        )

        raw = {
            CANNED_BRIDGE_TEXT,
            *FORWARD_HINGES,
            *REVERSE_HINGES,
            *GENERIC_RELATIVE_HINGES,
        }
        _CANNED_AIR_CACHE = frozenset(_canned_key(p) for p in raw if p)
    return _CANNED_AIR_CACHE


def _canned_key(text: str) -> str:
    return re.sub(r"[^a-z0-9 ]+", "", _norm(text).casefold()).strip()


def _is_generic_unlock(sentence: str) -> bool:
    key = _canned_key(sentence)
    if not key.startswith(_GENERIC_UNLOCK_STARTS):
        return False
    content = [w for w in key.split() if w not in _UNLOCK_STOPWORDS]
    return len(content) <= 3


def canned_air_violations(text: str) -> list[str]:
    """Canned hinge / generic-unlock sentences inside a lay-up line."""
    clean = _norm(text)
    if not clean:
        return []
    try:
        from interview_mux.media_ip_cta import is_lets_hear_hinge
    except Exception:
        is_lets_hear_hinge = lambda _t: False  # noqa: E731
    menu = canned_air_phrases()
    violations: list[str] = []
    for sentence in _SENTENCE_SPLIT.split(clean):
        key = _canned_key(sentence)
        if not key:
            continue
        if is_lets_hear_hinge(sentence):
            continue
        if key in menu:
            violations.append(f"canned_hinge:{sentence.strip()}")
        elif _is_generic_unlock(sentence):
            violations.append(f"generic_unlock:{sentence.strip()}")
    return list(dict.fromkeys(violations))


def build_corpus_mine_input(ctx: RunContext) -> dict[str, Any]:
    """Compact full-tape packet for flagship corpus mining."""
    cfg = nugget_layup_cfg()
    max_chars = int(cfg["segment_text_max_chars"])
    ordered = _ordered_ids(ctx)
    oset = set(ordered)
    excluded_meta: dict[str, dict[str, Any]] = {}
    if ctx.artifact_exists("master/selection.json"):
        sel = ctx.read_json("master/selection.json")
        if isinstance(sel, dict):
            for ex in sel.get("excluded_segment_ids") or []:
                if isinstance(ex, dict):
                    sid = str(ex.get("segment_id") or "")
                    if sid:
                        excluded_meta[sid] = {
                            "reason": str(ex.get("reason") or "excluded"),
                            "why_dropped": str(
                                ex.get("why_dropped") or ex.get("reason") or "excluded"
                            ),
                            "recovery_value": str(
                                ex.get("recovery_value")
                                or ex.get("salience")
                                or "medium"
                            ),
                        }
                else:
                    sid = str(ex or "")
                    if sid:
                        excluded_meta[sid] = {
                            "reason": "excluded",
                            "why_dropped": "excluded_from_selection",
                            "recovery_value": "medium",
                        }
    segments: list[dict[str, Any]] = []
    for row in _manifest_segments(ctx):
        sid = str(row.get("segment_id") or "")
        if not sid:
            continue
        full_text = _norm(str(row.get("text") or ""))
        seg: dict[str, Any] = {
            "segment_id": sid,
            "speaker_id": row.get("speaker_id"),
            "speaker_role": row.get("speaker_role"),
            "start_ms": row.get("start_ms"),
            "end_ms": row.get("end_ms"),
            "type": row.get("type"),
            "in_selection": sid in oset,
            "text": _clip_text(full_text, max_chars),
        }
        # No word on tape is lost to the packet cap: the overflow rides along.
        if len(full_text) > max_chars:
            seg["text_continued"] = full_text[max(0, max_chars - 1) :].lstrip()
        if sid in excluded_meta:
            seg["why_dropped"] = excluded_meta[sid]["why_dropped"]
            seg["recovery_value"] = excluded_meta[sid]["recovery_value"]
        try:
            from interview_mux.media_ip_cta import never_touch_segment_ids

            if sid in never_touch_segment_ids(ctx):
                seg["never_touch_cta"] = True
                seg["recovery_value"] = "none"
        except Exception:
            pass
        segments.append(seg)
    between_spans: list[dict[str, Any]] = []
    if cfg.get("full_tape_spans", True):
        words = _load_transcript_words(ctx)
        if words:
            manifest_rows = _manifest_segments(ctx)
            windows = segment_air_windows(ctx)
            for seg in segments:
                sid = str(seg.get("segment_id") or "")
                if not seg.get("in_selection"):
                    continue
                slab = _row_slab(next((r for r in manifest_rows if str(r.get("segment_id") or "") == sid), None))
                window = windows.get(sid)
                if slab is None or window is None:
                    continue
                seg["air_start_ms"], seg["air_end_ms"] = window
                off_air = off_air_text_for_segment(words, slab, window)
                if off_air:
                    seg["off_air_text"] = _clip_text(off_air, int(cfg["between_span_max_chars"]))
            between_spans = build_between_segment_spans(
                words,
                manifest_rows,
                split_gap_ms=int(cfg["between_span_split_gap_ms"]),
                max_chars=int(cfg["between_span_max_chars"]),
            )
    talking_points = (
        ctx.read_json("understanding/talking_points.json")
        if ctx.artifact_exists("understanding/talking_points.json")
        else {}
    )
    ideal_cuts = (
        ctx.read_json("understanding/ideal_cuts.json")
        if ctx.artifact_exists("understanding/ideal_cuts.json")
        else {}
    )
    gap_evals = (
        ctx.read_json("understanding/gap_evaluations.json")
        if ctx.artifact_exists("understanding/gap_evaluations.json")
        else {}
    )
    content_brief = (
        ctx.read_json("understanding/content_brief.json")
        if ctx.artifact_exists("understanding/content_brief.json")
        else {}
    )
    payload = {
        "ordered_segment_ids": ordered,
        "segments": segments,
        "talking_points": talking_points if isinstance(talking_points, dict) else {},
        "ideal_cuts": ideal_cuts if isinstance(ideal_cuts, dict) else {},
        "gap_evaluations_hint": _compact_gap_evals(gap_evals if isinstance(gap_evals, dict) else {}),
        "content_brief": {
            "thesis": (content_brief or {}).get("thesis") if isinstance(content_brief, dict) else None,
            "topics": (content_brief or {}).get("topics") if isinstance(content_brief, dict) else [],
            "key_claims": (content_brief or {}).get("key_claims") if isinstance(content_brief, dict) else [],
        },
        "prefer_excluded_nuggets": bool(cfg["prefer_excluded_nuggets"]),
        "partial_evidence_doctrine": (
            "Prefer high-confidence spans for evidence_quote. When evidence sits in a "
            "low-conf cluster, set evidence_partial:true and quote only comprehensible tokens."
        ),
        "between_segment_spans": between_spans,
        "full_tape_doctrine": (
            "Every spoken line on the tape is in play. Selection only decides which "
            "recordings air natively; a cut only decides where one native clip ends "
            "and the next begins. Words in between_segment_spans (never inside any "
            "segment) and words in a kept segment's off_air_text (outside its "
            "air_start_ms..air_end_ms window) are NOT heard by the listener: mine them "
            "as in_selection:false nuggets with source_start_ms/source_end_ms and never "
            "mark them already_aired_in_selection. A between-segment nugget has "
            "source_segment_ids: []."
        ),
    }
    try:
        from interview_mux.media_ip_cta import attach_to_mine_input

        return attach_to_mine_input(ctx, payload)
    except Exception:
        return payload


def _compact_gap_evals(doc: dict[str, Any]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for row in doc.get("evaluations") or []:
        if not isinstance(row, dict):
            continue
        out.append(
            {
                "segment_id": row.get("segment_id"),
                "gap_type": row.get("gap_type"),
                "severity": row.get("severity"),
                "recommended_framing": row.get("recommended_framing"),
                "listener_confusion": _clip_text(str(row.get("listener_confusion") or ""), 180),
            }
        )
        if len(out) >= 80:
            break
    return out


def _salience_weight(nugget: dict[str, Any]) -> float:
    return {"critical": 1.0, "high": 0.8, "medium": 0.4, "low": 0.1}.get(
        str(nugget.get("salience") or "").lower(), 0.3
    )


def rank_open_nuggets_for_target(
    target_text: str,
    nuggets: list[dict[str, Any]],
    *,
    exclude_ids: set[str] | frozenset[str] | None = None,
    limit: int = 8,
    prefer_excluded: bool = True,
    slim: bool = False,
    tape_window: tuple[int, int] | None = None,
    tape_window_boost: float = 0.6,
) -> list[dict[str, Any]]:
    """Open corpus nuggets ranked for one upcoming native (excluded tape first).

    When ``slim`` is true, ranked rows are pointers only (``nugget_id`` + scores);
    look up ``text_claim`` / ``evidence_quote`` in the top-level ``nugget_corpus``.
    ``tape_window`` is the source span the cut dropped right before this native
    (prior native's air end to this native's air start): a nugget spoken there
    is the words the listener would have heard at this seam, so it ranks up.
    """
    skip = {str(x) for x in (exclude_ids or set())}
    target_tokens = _tokens(target_text)
    scored: list[tuple[float, dict[str, Any]]] = []
    for nug in nuggets:
        if not isinstance(nug, dict):
            continue
        nid = str(nug.get("nugget_id") or "")
        if not nid or nid in skip:
            continue
        if nug.get("already_aired_in_selection"):
            continue
        claim = f"{nug.get('text_claim') or ''} {nug.get('evidence_quote') or ''}"
        relevance = _overlap(target_tokens, _tokens(claim))
        score = relevance * 2.0 + _salience_weight(nug)
        if prefer_excluded and not nug.get("in_selection"):
            score += 0.5
        at_seam = False
        if tape_window is not None:
            span = _nugget_span(nug)
            if span is not None and _overlap_ms(span[0], span[1], tape_window[0], tape_window[1]) > 0:
                at_seam = True
                score += float(tape_window_boost)
        row: dict[str, Any] = {
            "nugget_id": nid,
            "salience": nug.get("salience"),
            "in_selection": bool(nug.get("in_selection")),
            "relevance_to_target": round(relevance, 3),
        }
        if at_seam:
            row["cut_at_this_seam"] = True
        if not slim:
            row["text_claim"] = _clip_text(str(nug.get("text_claim") or ""), 240)
            row["evidence_quote"] = _clip_text(str(nug.get("evidence_quote") or ""), 180)
            row["talking_point_ids"] = [
                str(x) for x in (nug.get("talking_point_ids") or []) if x
            ]
        scored.append((score, row))
    scored.sort(key=lambda row: (-row[0], row[1]["nugget_id"]))
    return [row[1] for row in scored[: max(0, limit)]]


def compact_nugget_corpus_for_compose(corpus: dict[str, Any]) -> dict[str, Any]:
    """Keep claim/evidence fields the compose prompt needs; drop mine-only noise."""
    nuggets_out: list[dict[str, Any]] = []
    for nug in (corpus.get("nuggets") or []) if isinstance(corpus, dict) else []:
        if not isinstance(nug, dict):
            continue
        nid = str(nug.get("nugget_id") or "")
        if not nid:
            continue
        nuggets_out.append(
            {
                "nugget_id": nid,
                "text_claim": _clip_text(str(nug.get("text_claim") or ""), 240),
                "evidence_quote": _clip_text(str(nug.get("evidence_quote") or ""), 180),
                "salience": nug.get("salience"),
                "in_selection": bool(nug.get("in_selection")),
                "talking_point_ids": [
                    str(x) for x in (nug.get("talking_point_ids") or []) if x
                ],
                "source_segment_ids": [
                    str(x) for x in (nug.get("source_segment_ids") or []) if x
                ][:6],
                "supports_targets": [
                    str(x) for x in (nug.get("supports_targets") or []) if x
                ][:8],
                "speakability": nug.get("speakability"),
            }
        )
    return {"nuggets": nuggets_out, "version": (corpus or {}).get("version", 1)}


def merge_layup_plan_parts(
    parts: list[dict[str, Any]],
    *,
    ordered_segment_ids: list[str],
) -> dict[str, Any]:
    """Merge sharded ``nugget_layup_compose`` artifacts into one plan."""
    layups_by_tid: dict[str, dict[str, Any]] = {}
    discharged_tp: list[str] = []
    open_tp: list[str] = []
    discharged_nug: list[str] = []
    open_high: list[str] = []
    waived: list[Any] = []
    warnings: list[Any] = []
    for part in parts:
        if not isinstance(part, dict):
            continue
        for row in part.get("layups") or []:
            if not isinstance(row, dict):
                continue
            tid = str(row.get("target_segment_id") or "").strip()
            if not tid or tid in layups_by_tid:
                continue
            layups_by_tid[tid] = row
        for key, bucket in (
            ("discharged_talking_point_ids", discharged_tp),
            ("open_talking_point_ids", open_tp),
            ("discharged_nugget_ids", discharged_nug),
            ("open_high_salience_nugget_ids", open_high),
        ):
            for raw in part.get(key) or []:
                sid = str(raw or "")
                if sid and sid not in bucket:
                    bucket.append(sid)
        for raw in part.get("waived_nugget_ids") or []:
            waived.append(raw)
        for raw in part.get("warnings") or []:
            warnings.append(raw)
    # Prefer last shard's open ledgers (they see the most already-aired state).
    if parts:
        last = parts[-1] if isinstance(parts[-1], dict) else {}
        if isinstance(last.get("open_talking_point_ids"), list):
            open_tp = [str(x) for x in last["open_talking_point_ids"] if x]
        if isinstance(last.get("open_high_salience_nugget_ids"), list):
            open_high = [str(x) for x in last["open_high_salience_nugget_ids"] if x]
    layups = [layups_by_tid[sid] for sid in ordered_segment_ids if sid in layups_by_tid]
    return {
        "ordered_segment_ids": list(ordered_segment_ids),
        "layups": layups,
        "discharged_talking_point_ids": discharged_tp,
        "open_talking_point_ids": open_tp,
        "discharged_nugget_ids": discharged_nug,
        "open_high_salience_nugget_ids": open_high,
        "waived_nugget_ids": waived,
        "warnings": warnings,
    }


def dedupe_layup_rows_by_target(plan: dict[str, Any] | None) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Keep one deterministic layup row per native target before QC or publish.

    LLM retries and shard merges can emit both a skip and a thin non-skip row for
    one target.  Prefer a contentful row with construction analysis, but never
    publish both: duplicate rows make the spoken-copy corpus self-collide.
    """
    out = dict(plan) if isinstance(plan, dict) else {}
    rows = [dict(row) for row in (out.get("layups") or []) if isinstance(row, dict)]
    chosen: dict[str, dict[str, Any]] = {}
    notes: list[dict[str, Any]] = []

    def score(row: dict[str, Any]) -> tuple[int, int, int]:
        text = str(row.get("text") or "").strip()
        fields = sum(bool(str(row.get(key) or "").strip()) for key in ANALYSIS_FIELDS)
        return (
            int(not row.get("skip") and bool(text)),
            fields,
            len(text.split()),
        )

    for row in rows:
        target = str(row.get("target_segment_id") or "").strip()
        if not target:
            continue
        current = chosen.get(target)
        if current is None:
            chosen[target] = row
            continue
        if score(row) > score(current):
            kept, dropped = row, current
            chosen[target] = row
        else:
            kept, dropped = current, row
        notes.append(
            {
                "action": "dedupe_layup_target",
                "target_segment_id": target,
                "kept_line_id": kept.get("line_id"),
                "dropped_line_id": dropped.get("line_id"),
            }
        )

    ordered = [str(s) for s in (out.get("ordered_segment_ids") or []) if s]
    order_index = {sid: index for index, sid in enumerate(ordered)}
    out["layups"] = sorted(
        chosen.values(),
        key=lambda row: (
            order_index.get(str(row.get("target_segment_id") or ""), len(order_index)),
            str(row.get("target_segment_id") or ""),
        ),
    )
    if notes:
        out["layup_dedupe_notes"] = notes
    return out, notes


def degraded_layup_cfg(cfg: dict[str, Any] | None = None) -> dict[str, Any]:
    block = nugget_layup_cfg(cfg).get("degraded_layup") or {}
    return block if isinstance(block, dict) else {}


def _load_transcript_words(ctx: RunContext) -> list[dict[str, Any]]:
    if not ctx.artifact_exists("transcript/full.json"):
        return []
    try:
        doc = ctx.read_json("transcript/full.json")
    except Exception:
        return []
    rows = (doc.get("words") or []) if isinstance(doc, dict) else []
    words = [w for w in rows if isinstance(w, dict) and (w.get("text") or w.get("word"))]
    words.sort(key=lambda w: (int(w.get("start_ms") or 0), int(w.get("end_ms") or 0)))
    return words


def _word_bounds(w: dict[str, Any]) -> tuple[int, int] | None:
    try:
        start = int(w.get("start_ms") or 0)
        end = int(w.get("end_ms") or start)
    except (TypeError, ValueError):
        return None
    return start, max(start, end)


def _word_text(w: dict[str, Any]) -> str:
    return str(w.get("text") or w.get("word") or "").strip()


def transcript_words_text(words: list[dict[str, Any]], start_ms: int, end_ms: int) -> str:
    """Transcript words whose start lies in ``[start_ms, end_ms)``, joined."""
    out: list[str] = []
    for w in words:
        b = _word_bounds(w)
        if b is None:
            continue
        if int(start_ms) <= b[0] < int(end_ms):
            t = _word_text(w)
            if t:
                out.append(t)
    return _norm(" ".join(out))


def _row_slab(row: dict[str, Any] | None) -> tuple[int, int] | None:
    if not isinstance(row, dict):
        return None
    try:
        start = int(row.get("start_ms") or 0)
        end = int(row.get("end_ms") or 0)
    except (TypeError, ValueError):
        return None
    return (start, end) if end > start else None


def _nugget_span(nug: dict[str, Any] | None) -> tuple[int, int] | None:
    if not isinstance(nug, dict):
        return None
    try:
        s0 = nug.get("source_start_ms")
        e0 = nug.get("source_end_ms")
        if s0 is None or e0 is None:
            return None
        start, end = int(s0), int(e0)
    except (TypeError, ValueError):
        return None
    return (start, end) if end > start else None


def segment_air_windows(ctx: RunContext) -> dict[str, tuple[int, int]]:
    """Best estimate of the tape each manifest segment airs: ideal cut ∩ slab.

    The EDL settles exact bounds later; at mine/compose time the ideal-cut
    window is the aired part and the rest of the slab is off-air tape. Without
    a cut the whole slab airs.
    """
    out: dict[str, tuple[int, int]] = {}
    cuts: Any = None
    try:
        from interview_mux.ideal_cuts import load_air_bound_inputs, overlapping_ideal_window

        cuts, _words = load_air_bound_inputs(ctx)
    except Exception:
        cuts = None
        overlapping_ideal_window = None  # type: ignore[assignment]
    for row in _manifest_segments(ctx):
        sid = str(row.get("segment_id") or "").strip()
        slab = _row_slab(row)
        if not sid or slab is None:
            continue
        window = slab
        if cuts is not None and overlapping_ideal_window is not None:
            try:
                ideal = overlapping_ideal_window(
                    source_start_ms=slab[0],
                    source_end_ms=slab[1],
                    cuts_doc=cuts,
                    segment_id=sid,
                )
            except Exception:
                ideal = None
            if ideal is not None:
                clipped = (max(slab[0], int(ideal[0])), min(slab[1], int(ideal[1])))
                if clipped[1] - clipped[0] >= 1000:
                    window = clipped
        out[sid] = window
    return out


def off_air_text_for_segment(
    words: list[dict[str, Any]],
    slab: tuple[int, int],
    window: tuple[int, int],
) -> str:
    """Words of a kept segment that its air window leaves on the tape."""
    if window[0] <= slab[0] and window[1] >= slab[1]:
        return ""
    head = transcript_words_text(words, slab[0], max(slab[0], window[0]))
    tail = transcript_words_text(words, min(slab[1], window[1]), slab[1])
    parts = [p for p in (head, tail) if p]
    return " … ".join(parts)


def build_between_segment_spans(
    words: list[dict[str, Any]],
    segments: list[dict[str, Any]],
    *,
    split_gap_ms: int = 1500,
    max_chars: int = 4000,
) -> list[dict[str, Any]]:
    """Runs of transcript words that sit inside no manifest segment at all.

    Each run is a candidate nugget source with tape neighbours named, so the
    miner can place a fact between the segments it fell between (ISSUES 191).
    Runs split on a pause longer than ``split_gap_ms`` or a speaker change.
    """
    slabs: list[tuple[int, int, str]] = []
    for row in segments:
        slab = _row_slab(row)
        sid = str((row or {}).get("segment_id") or "")
        if slab is not None and sid:
            slabs.append((slab[0], slab[1], sid))
    slabs.sort()

    def _covered(mid: int) -> bool:
        return any(s0 <= mid < s1 for s0, s1, _ in slabs)

    runs: list[list[dict[str, Any]]] = []
    current: list[dict[str, Any]] = []
    last_end: int | None = None
    last_speaker: str | None = None
    for w in words:
        b = _word_bounds(w)
        if b is None or not _word_text(w):
            continue
        mid = (b[0] + b[1]) // 2
        if _covered(mid):
            if current:
                runs.append(current)
                current = []
            last_end = None
            last_speaker = None
            continue
        speaker = str(w.get("speaker_id") or "") or None
        if current and (
            (last_end is not None and b[0] - last_end > int(split_gap_ms))
            or (speaker and last_speaker and speaker != last_speaker)
        ):
            runs.append(current)
            current = []
        current.append(w)
        last_end = b[1]
        last_speaker = speaker or last_speaker
    if current:
        runs.append(current)

    out: list[dict[str, Any]] = []
    for run in runs:
        start = _word_bounds(run[0])[0]  # type: ignore[index]
        end = _word_bounds(run[-1])[1]  # type: ignore[index]
        text = _norm(" ".join(_word_text(w) for w in run))
        if not _WORD.search(text):
            continue
        speakers: dict[str, int] = {}
        for w in run:
            spk = str(w.get("speaker_id") or "")
            if spk:
                speakers[spk] = speakers.get(spk, 0) + 1
        prev_sid = ""
        next_sid = ""
        for s0, s1, sid in slabs:
            if s1 <= start:
                prev_sid = sid
            elif s0 >= end and not next_sid:
                next_sid = sid
        span: dict[str, Any] = {
            "span_id": f"gap_{start}_{end}",
            "start_ms": start,
            "end_ms": end,
            "speaker_id": max(speakers, key=speakers.get) if speakers else None,
            "word_count": len(run),
            "prev_segment_id": prev_sid or None,
            "next_segment_id": next_sid or None,
            "in_selection": False,
            "text": _clip_text(text, max_chars),
        }
        if len(text) > max_chars:
            span["text_continued"] = text[max(0, max_chars - 1) :].lstrip()
        out.append(span)
    return out


def normalize_corpus_tape_status(ctx: RunContext, corpus: dict[str, Any] | None) -> dict[str, Any]:
    """Deterministic air status for mined nuggets from their tape span.

    The miner guesses ``in_selection`` / ``already_aired_in_selection`` from
    segment ids; the tape decides. A nugget whose span lies outside every
    selected segment's air window (between segments, or in the part a cut
    trims off) is not heard on air, so it stays open for a voiceover insert.
    Empty ``source_segment_ids`` are filled from the manifest slabs the span
    overlaps so id-keyed consumers keep working (ISSUES 191).
    """
    out = dict(corpus) if isinstance(corpus, dict) else {"nuggets": []}
    nuggets = [n for n in (out.get("nuggets") or []) if isinstance(n, dict)]
    if not nuggets:
        return out
    ordered = set(_ordered_ids(ctx))
    windows = segment_air_windows(ctx)
    slabs: dict[str, tuple[int, int]] = {}
    for row in _manifest_segments(ctx):
        sid = str(row.get("segment_id") or "").strip()
        slab = _row_slab(row)
        if sid and slab is not None:
            slabs[sid] = slab
    fixed: list[dict[str, Any]] = []
    recovered = 0
    for nug in nuggets:
        row = dict(nug)
        span = _nugget_span(row)
        sources = [str(x) for x in (row.get("source_segment_ids") or []) if x]
        if span is None:
            if sources and slabs:
                known = [s for s in sources if s in slabs]
                if known:
                    row["in_selection"] = any(s in ordered for s in known)
            fixed.append(row)
            continue
        touching = sorted(
            (
                (_overlap_ms(span[0], span[1], slab[0], slab[1]), sid)
                for sid, slab in slabs.items()
            ),
            key=lambda t: (-t[0], t[1]),
        )
        touching = [sid for ov, sid in touching if ov > 0]
        if not sources and touching:
            row["source_segment_ids"] = touching[:4]
            sources = touching[:4]
        heard_ms = 0
        for sid in sources or touching:
            if sid not in ordered:
                continue
            window = windows.get(sid) or slabs.get(sid)
            if window is None:
                continue
            heard_ms = max(heard_ms, _overlap_ms(span[0], span[1], window[0], window[1]))
        span_len = max(1, span[1] - span[0])
        heard = heard_ms >= min(1000, span_len // 2) and heard_ms > 0
        if not touching:
            row["source_kind"] = "between_segments"
        elif heard:
            row["source_kind"] = "segment"
        else:
            row["source_kind"] = "off_air_span" if any(s in ordered for s in touching) else "segment"
        if heard:
            row["in_selection"] = True
        else:
            if row.get("in_selection") or row.get("already_aired_in_selection"):
                recovered += 1
            row["in_selection"] = False
            row["already_aired_in_selection"] = False
        fixed.append(row)
    out["nuggets"] = fixed
    if recovered:
        warnings = list(out.get("warnings") or [])
        warnings.append(f"tape_status_reopened_off_air_nuggets:{recovered}")
        out["warnings"] = warnings
    return out


def _word_conf_value(w: dict[str, Any]) -> float | None:
    value = w.get("confidence")
    if value is None:
        value = w.get("conf")
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def build_comprehension_mask_for_span(
    words: list[dict[str, Any]],
    *,
    start_ms: int,
    end_ms: int,
    min_conf: float = 0.85,
) -> dict[str, Any]:
    """Deterministic spine + unclear spans for one native time window."""
    span = [
        w
        for w in words
        if int(w.get("start_ms") or 0) < int(end_ms) and int(w.get("end_ms") or 0) > int(start_ms)
    ]
    comprehensible: list[str] = []
    unclear: list[dict[str, Any]] = []
    unclear_run: list[dict[str, Any]] = []
    high = 0
    low = 0

    def _flush_unclear() -> None:
        nonlocal unclear_run
        if not unclear_run:
            return
        s0 = int(unclear_run[0].get("start_ms") or 0)
        e0 = int(unclear_run[-1].get("end_ms") or s0)
        unclear.append(
            {
                "start_ms": s0,
                "end_ms": e0,
                "placeholder": "[unclear_lexicon]",
                "low_ratio": 1.0,
                "approx_ms": max(0, e0 - s0),
            }
        )
        unclear_run = []

    for w in span:
        conf = _word_conf_value(w)
        text = str(w.get("text") or w.get("word") or "").strip()
        if not text:
            continue
        # Null confidence → mid → treat as comprehensible for spine.
        ok = conf is None or conf >= float(min_conf)
        if ok:
            high += 1
            _flush_unclear()
            comprehensible.append(text)
        else:
            low += 1
            unclear_run.append(w)
    _flush_unclear()

    total = max(1, high + low)
    high_ratio = high / total
    if low == 0:
        quality = "clear"
    elif high_ratio < 0.45:
        quality = "heavily_degraded"
    else:
        quality = "degraded_lexicon_island"

    return {
        "comprehensible_text": " ".join(comprehensible).strip(),
        "unclear_spans": unclear,
        "transcript_quality": quality,
        "high_conf_char_ratio": round(high_ratio, 4),
        "unclear_span_count": len(unclear),
        "word_count": high + low,
        "low_word_count": low,
    }


def build_native_comprehension_masks(ctx: RunContext) -> dict[str, Any]:
    """Layer 2 — per-native comprehensible spine masks written before compose."""
    deg = degraded_layup_cfg()
    min_conf = float(deg.get("comprehensible_min_token_conf") or 0.85)
    words = _load_transcript_words(ctx)
    must_keep: set[str] = set()
    if ctx.artifact_exists("analysis/low_conf_must_keep.json"):
        try:
            mk = ctx.read_json("analysis/low_conf_must_keep.json")
            if isinstance(mk, dict):
                must_keep = {str(x) for x in (mk.get("must_keep_segment_ids") or []) if x}
        except Exception:
            must_keep = set()

    natives: dict[str, Any] = {}
    for row in _manifest_segments(ctx):
        sid = str(row.get("segment_id") or "")
        if not sid:
            continue
        mask = build_comprehension_mask_for_span(
            words,
            start_ms=int(row.get("start_ms") or 0),
            end_ms=int(row.get("end_ms") or 0),
            min_conf=min_conf,
        )
        fused = bool(row.get("fused_from"))
        if sid in must_keep and mask["transcript_quality"] == "clear" and fused:
            mask["transcript_quality"] = "degraded_lexicon_island"
        mask["segment_id"] = sid
        mask["in_low_conf_must_keep"] = sid in must_keep
        mask["fused_from"] = list(row.get("fused_from") or []) if fused else []
        natives[sid] = mask

    doc = {"version": 1, "natives": natives, "generated_for": "nugget_layup_compose"}
    ctx.write_json(MASKS_REL, doc)

    index = {
        "version": 1,
        "by_segment": {
            sid: {
                "high_conf_char_ratio": row.get("high_conf_char_ratio"),
                "unclear_span_count": row.get("unclear_span_count"),
                "transcript_quality": row.get("transcript_quality"),
            }
            for sid, row in natives.items()
        },
    }
    ctx.write_json(COMPREHENSION_INDEX_REL, index)
    return doc


def _mask_for(masks: dict[str, Any], segment_id: str) -> dict[str, Any]:
    natives = masks.get("natives") if isinstance(masks, dict) else {}
    row = (natives or {}).get(segment_id) if isinstance(natives, dict) else None
    return row if isinstance(row, dict) else {}


def is_degraded_target(mask: dict[str, Any]) -> bool:
    q = str(mask.get("transcript_quality") or "clear")
    return q in ("degraded_lexicon_island", "heavily_degraded") or bool(
        mask.get("in_low_conf_must_keep")
    ) or bool(mask.get("fused_from"))


def _seam_reason(prev_row: dict[str, Any] | None, row: dict[str, Any]) -> str:
    """Why the listener needs help entering this native (edit-order seam shape)."""
    from interview_mux.seam_glue import CHAPTER_SCALE_GAP_MS

    if not prev_row:
        return "episode_open"
    try:
        gap = int(row.get("start_ms") or 0) - int(prev_row.get("end_ms") or 0)
    except (TypeError, ValueError):
        return "unknown"
    if abs(gap) >= CHAPTER_SCALE_GAP_MS:
        return "chapter_scale_reverse_jump" if gap < 0 else "chapter_scale_forward_jump"
    if gap < -2500:
        return "reverse_jump"
    if gap > 2500:
        return "forward_jump"
    return "source_contiguous"


def _nuggets_spoken_by(
    nuggets: list[dict[str, Any]],
    segment_id: str,
    *,
    air_window: tuple[int, int] | None = None,
) -> list[dict[str, Any]]:
    """Nuggets this native speaks on air.

    A nugget sourced from the segment but spoken in the part the cut trims off
    (outside ``air_window``) is not heard, so it is not discharged (ISSUES 191).
    """
    out: list[dict[str, Any]] = []
    for nug in nuggets:
        if not isinstance(nug, dict):
            continue
        sources = {str(x) for x in (nug.get("source_segment_ids") or []) if x}
        if not segment_id or segment_id not in sources:
            continue
        if air_window is not None:
            span = _nugget_span(nug)
            if span is not None and _overlap_ms(span[0], span[1], air_window[0], air_window[1]) <= 0:
                continue
        out.append(nug)
    return out


def build_layup_compose_input(
    ctx: RunContext,
    *,
    target_segment_ids: list[str] | None = None,
) -> dict[str, Any]:
    """Packet for per-native lay-up composition.

    Each native carries the construction material for the *next* clip: the prior
    native's closing words, the full upcoming text, the seam that created the
    need, and the still-open corpus nuggets ranked for that beat. The
    ``already_aired_*`` fields walk the air order so no fact is claimed twice.

    When ``target_segment_ids`` is set, only those natives are included (shard),
    but ``already_aired_*`` still walks the full air order so uniqueness holds.
    """
    cfg = nugget_layup_cfg()
    deg = degraded_layup_cfg(cfg)
    max_chars = int(cfg["segment_text_max_chars"])
    slim_open = bool(cfg.get("slim_open_nuggets", True))
    ordered = _ordered_ids(ctx)
    want = {str(x) for x in target_segment_ids} if target_segment_ids is not None else None
    by_id = {
        str(r.get("segment_id")): r
        for r in _manifest_segments(ctx)
        if r.get("segment_id")
    }
    masks = build_native_comprehension_masks(ctx) if deg.get("enabled", True) else {"natives": {}}
    corpus = ctx.read_json(CORPUS_REL) if ctx.artifact_exists(CORPUS_REL) else {"nuggets": []}
    corpus = corpus if isinstance(corpus, dict) else {"nuggets": []}
    nuggets = [n for n in (corpus.get("nuggets") or []) if isinstance(n, dict)]
    try:
        from interview_mux.media_ip_cta import (
            never_touch_segment_ids,
            never_touch_source_intervals,
            nugget_from_never_touch,
        )

        nt_ids = never_touch_segment_ids(ctx)
        try:
            nt_intervals = never_touch_source_intervals(ctx)
        except Exception:
            nt_intervals = []
        nuggets = [
            n
            for n in nuggets
            if not nugget_from_never_touch(ctx, n, banned_ids=nt_ids, intervals=nt_intervals)
        ]
    except Exception:
        pass
    air_windows = segment_air_windows(ctx) if cfg.get("full_tape_spans", True) else {}
    tape_words = _load_transcript_words(ctx) if cfg.get("full_tape_spans", True) else []
    prior_plan = ctx.read_json(PLAN_REL) if ctx.artifact_exists(PLAN_REL) else {}
    prior_by_target = {
        str(r.get("target_segment_id") or ""): r
        for r in ((prior_plan.get("layups") or []) if isinstance(prior_plan, dict) else [])
        if isinstance(r, dict) and r.get("target_segment_id")
    }
    opening_owned = _opening_owned_targets(ctx)
    from interview_mux.gap_vo_prior_context import (
        _chapter_title_for,
        load_ordered_and_segments,
    )
    from interview_mux.vo_delivery_card import next_beat_card

    _, _, chapters = load_ordered_and_segments(ctx)
    gap_need_by_sid: dict[str, str] = {}
    if ctx.artifact_exists("understanding/gap_evaluations.json"):
        try:
            ge = ctx.read_json("understanding/gap_evaluations.json")
            for row in _compact_gap_evals(ge if isinstance(ge, dict) else {}):
                sid = str(row.get("segment_id") or "")
                need = str(row.get("listener_confusion") or row.get("recommended_framing") or "").strip()
                if sid and need:
                    gap_need_by_sid[sid] = need
        except Exception:
            gap_need_by_sid = {}

    natives: list[dict[str, Any]] = []
    aired_ids: list[str] = []
    claimed_facts: list[str] = []
    for index, sid in enumerate(ordered):
        row = by_id.get(sid) or {}
        prev_row = by_id.get(ordered[index - 1]) if index else None
        mask = _mask_for(masks, sid)
        prior_mask = _mask_for(masks, ordered[index - 1]) if index else {}
        degraded = is_degraded_target(mask) or is_degraded_target(prior_mask)
        spine = str(mask.get("comprehensible_text") or "")
        verbatim = spine if (degraded and deg.get("exclude_unclear_from_llm_verbatim", True)) else str(
            row.get("text") or ""
        )
        unclear_summary = []
        if deg.get("include_unclear_span_summaries", True):
            for span in mask.get("unclear_spans") or []:
                if not isinstance(span, dict):
                    continue
                ms = int(span.get("approx_ms") or 0)
                unclear_summary.append(
                    {
                        "approx_sec": round(ms / 1000.0, 2),
                        "placeholder": span.get("placeholder") or "[unclear_lexicon]",
                    }
                )
        include_row = want is None or sid in want
        # The tape the cut dropped right before this native: from the prior
        # native's air end to this native's air start, tape-forward only.
        tape_window: tuple[int, int] | None = None
        if index and air_windows:
            prev_w = air_windows.get(ordered[index - 1])
            this_w = air_windows.get(sid)
            if prev_w and this_w and prev_w[1] < this_w[0]:
                tape_window = (prev_w[1], this_w[0])
        if include_row:
            comp = _clip_text(spine, max_chars)
            text = _clip_text(verbatim, max_chars if not degraded else max(max_chars, 2200))
            handoff = gap_need_by_sid.get(sid) or ""
            if not handoff:
                handoff = _seam_reason(prev_row, row)
            native_row: dict[str, Any] = {
                "segment_id": sid,
                "air_index": index,
                "speaker_id": row.get("speaker_id"),
                "type": row.get("type"),
                "start_ms": row.get("start_ms"),
                "end_ms": row.get("end_ms"),
                "text": text,
                "comprehensible_text": comp,
                "unclear_spans": unclear_summary,
                "transcript_quality": mask.get("transcript_quality") or "clear",
                "degraded_lexicon_island": degraded,
                "degraded_path": degraded,
                "fused_from": list(row.get("fused_from") or []),
                "prior_segment_id": ordered[index - 1] if index else None,
                "prior_closing_excerpt": _clip_tail(
                    str(
                        (prior_mask.get("comprehensible_text") if prior_mask else None)
                        or (prev_row or {}).get("text")
                        or ""
                    ),
                    int(cfg["prior_close_excerpt_chars"]),
                ),
                "seam_reason": _seam_reason(prev_row, row),
                "handoff_need": _clip_text(handoff, 220),
                "opening_owner": sid in opening_owned,
                "already_aired_nugget_ids": list(aired_ids),
                "already_claimed_facts": [
                    _clip_text(f, 120) for f in claimed_facts[-24:]
                ],
                "open_nuggets_ranked": rank_open_nuggets_for_target(
                    spine or str(row.get("text") or ""),
                    nuggets,
                    exclude_ids=set(aired_ids),
                    limit=int(cfg["max_open_nuggets_per_target"]),
                    prefer_excluded=bool(cfg["prefer_excluded_nuggets"]),
                    slim=slim_open,
                    tape_window=tape_window,
                    tape_window_boost=float(cfg.get("tape_window_boost", 0.6)),
                ),
                "work_around_doctrine": (
                    "Orient and unlock from comprehensible spine + corpus only; "
                    "never invent unclear lexicon tokens; no on-air apology about audio."
                    if degraded
                    else None
                ),
            }
            card = next_beat_card(
                chapter_title=_chapter_title_for(sid, chapters),
                listener_confusion=gap_need_by_sid.get(sid),
                handoff_need=handoff,
            )
            if card:
                native_row["next_beat_card"] = card
            # Avoid triplicating the same string when the clip is clear.
            if degraded or text != comp:
                native_row["high_conf_text"] = comp
            if tape_window is not None and tape_words:
                cut_words = transcript_words_text(tape_words, tape_window[0], tape_window[1])
                if cut_words:
                    native_row["cut_words_before"] = _clip_tail(
                        cut_words, int(cfg.get("cut_words_before_max_chars", 600))
                    )
                    native_row["cut_tape_window_ms"] = [tape_window[0], tape_window[1]]
            natives.append(native_row)
        # Facts this native speaks itself, plus anything a prior compose pass
        # already assigned here, are discharged for every later target.
        for nug in _nuggets_spoken_by(nuggets, sid, air_window=air_windows.get(sid)):
            nid = str(nug.get("nugget_id") or "")
            if nid and nid not in aired_ids:
                aired_ids.append(nid)
                claim = _clip_text(str(nug.get("text_claim") or ""), 180)
                if claim:
                    claimed_facts.append(claim)
        prior_row = prior_by_target.get(sid) or {}
        for nid in row_nugget_ids(prior_row):
            if nid and nid not in aired_ids:
                aired_ids.append(nid)

    talking_points = (
        ctx.read_json("understanding/talking_points.json")
        if ctx.artifact_exists("understanding/talking_points.json")
        else {}
    )
    dense_targets: dict[str, Any] = {}
    dense_max = int(cfg["max_layup_words"])
    if cfg.get("honor_information_package_dense_budget", True):
        try:
            from interview_mux.information_packages import (
                dense_targets_from_plan,
                information_packages_cfg,
                packages_affect_air,
            )

            if packages_affect_air() and ctx.artifact_exists("mastering/mastering_plan.json"):
                mp = ctx.read_json("mastering/mastering_plan.json")
                dense_targets = dense_targets_from_plan(mp if isinstance(mp, dict) else {})
                dense_max = int(
                    information_packages_cfg().get("dense_max_layup_words") or max(dense_max, 140)
                )
        except Exception:
            dense_targets = {}
    scope_ids = [str(x) for x in (target_segment_ids or ordered) if x]
    payload = {
        "ordered_segment_ids": scope_ids,
        "full_ordered_segment_ids": list(ordered),
        "opening_owned_segment_ids": sorted(opening_owned),
        "natives": natives,
        "nugget_corpus": compact_nugget_corpus_for_compose(
            corpus if isinstance(corpus, dict) else {"nuggets": []}
        ),
        "talking_points": talking_points if isinstance(talking_points, dict) else {},
        "min_layup_words": int(cfg["min_layup_words"]),
        "grace_min_layup_words": int(deg.get("grace_min_layup_words") or 12),
        "max_layup_words": int(cfg["max_layup_words"]),
        "dense_max_layup_words": dense_max,
        "information_package_dense_targets": dense_targets,
        "require_layup_per_native": bool(cfg["require_layup_per_native"]),
        "prefer_excluded_nuggets": bool(cfg["prefer_excluded_nuggets"]),
        "required_analysis_fields": (
            list(REQUIRED_ANALYSIS_FIELDS) if cfg["require_analysis_fields"] else []
        ),
        "banned_air_phrases": sorted(canned_air_phrases()) if cfg["ban_canned_air"] else [],
        "degraded_layup": deg,
        "slim_open_nuggets": slim_open,
        "typed_skip_required_fields": [
            "skip_reason_code",
            "evidence_refs",
            "value_forgone",
            "compensating_path",
            "revisit_if",
            "decision_confidence",
            "owner_stage",
        ],
        "uniqueness_rule": (
            "Each nugget_id may be claimed by at most one lay-up. Honor each "
            "native's already_aired_nugget_ids / already_claimed_facts. "
            "Look up claim/evidence text in nugget_corpus by nugget_id when "
            "open_nuggets_ranked rows are slim pointers."
        ),
        "degraded_doctrine": (
            "When degraded_lexicon_island is true: work around missing context; "
            "convey what IS known from comprehensible_text + corpus; never invent "
            "unclear tokens; keep air text speakable (no bracket placeholders)."
        ),
        "airable_copy_doctrine": (
            "Every lay-up text is spoken on air by the host voice. Write what the "
            "host says to the listener, never notes about the tape: no role labels "
            "(the host, the guest, the interviewer, our speaker), no he/she/him/her "
            "(use the name once or they), no 'X explains/says/adds that'. State the "
            "fact, then the forward cue."
        ),
        "order_lock_note": (
            "Do not emit order_lock or order_content_hash — code stamps them "
            "from master/selection.json after compose."
        ),
    }
    try:
        from interview_mux.speaker_delivery_plan import episode_vo_identity

        payload["episode_vo_identity"] = episode_vo_identity(ctx)
        payload["vo_shape_lock"] = payload["episode_vo_identity"].get("vo_shape")
        payload["vo_shape_rule"] = (
            "Write every lay-up in episode_vo_identity.vo_shape. Do not switch "
            "person or clone character between natives."
        )
    except Exception:
        pass
    try:
        from interview_mux.media_ip_cta import attach_to_compose_input

        return attach_to_compose_input(ctx, payload)
    except Exception:
        return payload


def _word_count(text: str) -> int:
    return len([w for w in str(text or "").split() if w])


def row_nugget_ids(row: dict[str, Any]) -> list[str]:
    """Nuggets a lay-up row claims (``nugget_ids`` ∪ analysis ``selected_nugget_ids``)."""
    ids: list[str] = []
    for key in ("nugget_ids", "selected_nugget_ids"):
        for raw in (row.get(key) or []) if isinstance(row, dict) else []:
            nid = str(raw or "")
            if nid and nid not in ids:
                ids.append(nid)
    return ids


def row_is_aired(row: dict[str, Any] | None) -> bool:
    """True when a plan row will publish as listener-facing before-VO."""
    if not isinstance(row, dict) or row.get("skip"):
        return False
    return bool(str(row.get("text") or "").strip())


def aired_nugget_ids(plan: dict[str, Any] | None) -> set[str]:
    """Nugget ids spent on non-skip aired rows only — skips do not discharge."""
    ids: set[str] = set()
    for row in (plan or {}).get("layups") or []:
        if row_is_aired(row):
            ids.update(row_nugget_ids(row))
    return ids


def waived_nugget_ids_from_sources(
    *sources: list[str] | set[str] | dict[str, Any] | None,
) -> set[str]:
    """Normalize waived nugget ids from plan rows, omit ledger, or explicit lists."""
    out: set[str] = set()
    for src in sources:
        if src is None:
            continue
        if isinstance(src, (list, set, frozenset)):
            for raw in src:
                if isinstance(raw, dict):
                    nid = str(raw.get("nugget_id") or "")
                else:
                    nid = str(raw or "")
                if nid:
                    out.add(nid)
            continue
        if isinstance(src, dict):
            for raw in src.get("waived_nugget_ids") or []:
                if isinstance(raw, dict):
                    nid = str(raw.get("nugget_id") or "")
                else:
                    nid = str(raw or "")
                if nid:
                    out.add(nid)
    return out


def eligible_nugget_ids(
    corpus: dict[str, Any] | None,
    waived: set[str] | None = None,
) -> set[str]:
    """Corpus nuggets minus waived and already native in selection."""
    waived_set = waived or set()
    eligible: set[str] = set()
    for nug in (corpus or {}).get("nuggets") or []:
        if not isinstance(nug, dict):
            continue
        nid = str(nug.get("nugget_id") or "")
        if not nid or nug.get("already_aired_in_selection"):
            continue
        if nid in waived_set:
            continue
        eligible.add(nid)
    return eligible


def evaluate_nugget_air_coverage(
    body_plan: dict[str, Any],
    intro_ids: list[str] | None,
    waived: list[str] | set[str] | None,
    corpus: dict[str, Any],
    *,
    hard: bool = False,
    min_coverage: float | None = None,
    aspirational: bool | None = None,
) -> dict[str, Any]:
    """Body + intro nugget air coverage vs eligible corpus.

    NLC-B2 / Workstream B: ``min_nugget_air_coverage`` (0.85) is an aspirational
    goal when ``air_coverage_aspirational`` is true — under-goal coverage is a
    warning (advisory), not a hard error. Hard refuse only when coverage falls
    below ``catastrophic_nugget_air_coverage``, or when callers still set
    ``air_coverage_aspirational: false`` (legacy hard floor). Structural
    unaccounted open high-salience is enforced separately in ``evaluate_layup_qc``.

    Compose QC uses ``hard=True``. Soft warn remains available for callers that
    pass ``hard=False`` (e.g. exploratory coverage probes).
    """
    cfg = nugget_layup_cfg()
    floor = float(
        min_coverage if min_coverage is not None else cfg.get("min_nugget_air_coverage", 0.85)
    )
    aspirational_on = (
        bool(cfg.get("air_coverage_aspirational", True))
        if aspirational is None
        else bool(aspirational)
    )
    catastrophic = float(cfg.get("catastrophic_nugget_air_coverage") or 0.0)
    plan = body_plan if isinstance(body_plan, dict) else {}
    doc = corpus if isinstance(corpus, dict) else {}
    waived_set = waived_nugget_ids_from_sources(waived, plan)
    eligible = eligible_nugget_ids(doc, waived_set)
    body_aired = aired_nugget_ids(plan)
    intro_aired = {str(x) for x in (intro_ids or []) if x}
    aired = body_aired | intro_aired
    coverage = (len(aired & eligible) / len(eligible)) if eligible else 1.0
    open_ids = sorted(eligible - aired)
    warnings: list[str] = []
    errors: list[str] = []
    if eligible and catastrophic > 0 and coverage + 1e-9 < catastrophic:
        errors.append(
            f"nugget_air_coverage={coverage:.3f} below "
            f"catastrophic_nugget_air_coverage={catastrophic}"
        )
    elif eligible and coverage + 1e-9 < floor:
        msg = (
            f"nugget_air_coverage={coverage:.3f} below "
            f"min_nugget_air_coverage={floor}"
        )
        # Aspirational: goal miss is advisory even under hard=True. Legacy:
        # hard=True restores the prior hard 0.85 refuse.
        if hard and not aspirational_on:
            errors.append(msg)
        else:
            warnings.append(msg)
    return {
        "version": 1,
        "nugget_air_coverage": round(coverage, 4),
        "eligible_nugget_count": len(eligible),
        "aired_nugget_count": len(aired & eligible),
        "body_aired_nugget_ids": sorted(body_aired & eligible),
        "intro_aired_nugget_ids": sorted(intro_aired & eligible),
        "open_nugget_ids": open_ids,
        "waived_nugget_ids": sorted(waived_set),
        "min_nugget_air_coverage": floor,
        "catastrophic_nugget_air_coverage": catastrophic,
        "air_coverage_aspirational": aspirational_on,
        "warnings": warnings,
        "errors": errors,
        "ok": not errors,
    }


def _corpus_high_salience_ids(corpus: dict[str, Any] | None) -> set[str]:
    ids: set[str] = set()
    for nug in (corpus or {}).get("nuggets") or []:
        if not isinstance(nug, dict):
            continue
        if str(nug.get("salience") or "") not in {"high", "critical"}:
            continue
        nid = str(nug.get("nugget_id") or "")
        if nid:
            ids.add(nid)
    return ids


def _nugget_claim_text(nugget: dict[str, Any] | None) -> str:
    if not isinstance(nugget, dict):
        return ""
    return str(
        nugget.get("text_claim")
        or nugget.get("text")
        or nugget.get("claim")
        or nugget.get("summary")
        or ""
    ).strip()


def _nugget_spoken_claim(nugget: dict[str, Any] | None) -> str:
    """A nugget claim as it may be spoken: claims are notes about the tape, not air copy."""
    from interview_mux.spoken_copy_guard import scrub_spoken_register

    return scrub_spoken_register(_nugget_claim_text(nugget))


def _nugget_preview_ok(
    row: dict[str, Any],
    text: str,
    target_text: str,
    *,
    overlap: float,
    corpus: dict[str, Any] | None = None,
) -> bool:
    """True when VO↔T overlap is a nugget preview, not a verbatim dump of T."""
    nids = row_nugget_ids(row)
    if not nids:
        return False
    # Near-verbatim copy of the upcoming native is never a preview.
    if overlap >= 0.92:
        return False
    nug_by_id = {
        str(n.get("nugget_id") or ""): n
        for n in ((corpus or {}).get("nuggets") or [])
        if isinstance(n, dict) and n.get("nugget_id")
    }
    target_tokens = _tokens(target_text)
    extra = set()
    excluded = False
    for nid in nids:
        nug = nug_by_id.get(nid) or {}
        extra |= _tokens(_nugget_claim_text(nug) + " " + str(nug.get("evidence_quote") or ""))
        if nug.get("in_selection") is False:
            excluded = True
    if excluded or extra - target_tokens:
        return True
    # Claims already live in T, but the line still spends corpus ids as a hinge-in.
    return bool(nids) and overlap < 0.92


def _count_active_synthetic_lines(lines: list[Any] | None) -> int:
    """Count non-skipped synthesize/record host lines (G-Framing floor helper)."""
    n = 0
    for ln in lines or []:
        if not isinstance(ln, dict) or ln.get("skipped_optional"):
            continue
        raw = ln.get("delivery")
        if raw is None:
            delivery = "synthesize"
        else:
            delivery = str(raw).strip().lower()
            if not delivery:
                continue
        if delivery in {"synthesize", "chatterbox", "record", "mlx_audio"}:
            n += 1
    return n


def layup_line_from_row(row: dict[str, Any]) -> dict[str, Any] | None:
    """Convert a plan layup row into a gap_report interviewer_line (or None if skip)."""
    if not isinstance(row, dict):
        return None
    if row.get("skip"):
        return None
    text = str(row.get("text") or "").strip()
    from interview_mux.spoken_copy_guard import dedupe_sentences

    text = dedupe_sentences(text)
    tid = str(row.get("target_segment_id") or "").strip()
    if not text or not tid:
        return None
    lid = str(row.get("line_id") or "").strip() or f"vo_layup_{tid}"
    nugget_ids = row_nugget_ids(row)
    tp_ids = [str(x) for x in (row.get("talking_point_ids") or []) if x]
    detail_budget = str(row.get("detail_budget") or "").strip() or None
    line = {
        "line_id": lid,
        "gap_type": "nugget_layup",
        "line_category": "extracted_context",
        "text": text,
        "targets_segment_id": tid,
        "placement": "before",
        "delivery": "synthesize",
        "supports_segment_ids": [tid],
        "rationale": str(row.get("why_relevant_to_target") or "nugget_layup").strip(),
        "origin": "nugget_layup",
        "nugget_ids": nugget_ids,
        "recovery_of_talking_point_ids": tp_ids,
        "forward_cue_ok": bool(row.get("forward_cue_ok", True)),
        "word_count": int(row.get("word_count") or _word_count(text)),
        "severity": "high" if tp_ids or nugget_ids else "medium",
    }
    if detail_budget:
        line["detail_budget"] = detail_budget
    if row.get("information_package_id"):
        line["information_package_id"] = row.get("information_package_id")
    if row.get("cta_cover"):
        line["cta_cover"] = True
        line["cta_cover_regenerate"] = True
        line["clone_adjacency_exempt"] = True
        if row.get("vo_shape"):
            line["vo_shape"] = row.get("vo_shape")
    elif row.get("clone_adjacency_exempt"):
        line["clone_adjacency_exempt"] = True
    return line


def _opening_owned_targets(ctx: RunContext) -> set[str]:
    """Segment ids whose before-VO slot is owned by episode orientation."""
    owned: set[str] = set()
    ordered = _ordered_ids(ctx)
    if ordered:
        owned.add(str(ordered[0]))
    try:
        from interview_mux.opening_orientation import native_cold_open_segment_id

        hook = native_cold_open_segment_id(ctx, ordered)
        if hook:
            owned.add(str(hook))
    except Exception:
        pass
    return {sid for sid in owned if sid}


def _opening_layup_skip_spec(ctx: RunContext, target_segment_id: str) -> tuple[str, str]:
    """Skip code + compensating path for an opening-owned before-VO slot.

    When the native open already greets / introduces, orientation is omitted
    (``native_open_self_orients``). Claiming ``opening_orientation_owns_target``
    in that case leaves omit-ledger ``suppress`` pointing at a missing
    episode-orientation line and fails post-master air-contract QC.
    """
    try:
        from interview_mux.opening_orientation import native_open_already_orients

        if native_open_already_orients(
            ctx, _ordered_ids(ctx), target_segment_id=target_segment_id
        ):
            return "episode_open_native_self_orients", "native_self_orients"
    except Exception:
        pass
    return "opening_orientation_owns_target", "opening_orientation"


_COVERAGE_EXEMPT_SKIP_REASONS = frozenset(
    {
        "spoken_copy_unhealable",
        "opening_orientation_owns_target",
        "clone_voice_adjacency",
        "merged_clone_adjacency",
        "media_ip_cta_hole",
        "never_touch_cta",
        "skip_omit_unseat",
    }
)

# Closed skip codes that may count toward coverage when typed fields are present
# (or soft-migrated from known exempt codes).
JUSTIFIED_SKIP_REASON_CODES = frozenset(
    {
        "spoken_copy_unhealable",
        "opening_orientation_owns_target",
        "clone_voice_adjacency",
        "merged_clone_adjacency",
        "media_ip_cta_hole",
        "never_touch_cta",
        "episode_open_native_self_orients",
        "self_explanatory_native",
        "native_self_orients",
        "no_unrecovered_high_salience",
        "listener_already_oriented",
        "superseded_by_dense_package",
        "operator_waive",
        "compensated_by_prior_layup",
        "closing_credits",
        "outro_self_sufficient",
        "native_audio_self_orients",
        "non_editorial_outro",
        "no_eligible_unspent_nugget",
        "skip_omit_unseat",
    }
)

_DEFAULT_COMPENSATING_PATHS = {
    "spoken_copy_unhealable": "omit_unsafe_spoken_copy",
    "opening_orientation_owns_target": "opening_orientation",
    "clone_voice_adjacency": "clone_voice_policy",
    "merged_clone_adjacency": "merged_into_non_clone_target",
    "media_ip_cta_hole": "media_ip_cta_omit",
    "never_touch_cta": "media_ip_cta_omit",
    "episode_open_native_self_orients": "native_self_orients",
    "self_explanatory_native": "native_self_orients",
    "native_self_orients": "native_self_orients",
    "native_audio_self_orients": "native_self_orients",
    "no_unrecovered_high_salience": "no_open_high_salience_need",
    "listener_already_oriented": "prior_layup_or_native",
    "superseded_by_dense_package": "information_package_dense",
    "operator_waive": "operator_waive",
    "compensated_by_prior_layup": "prior_layup",
    "closing_credits": "native_credits_self_contained",
    "outro_self_sufficient": "native_credits_self_contained",
    "non_editorial_outro": "native_credits_self_contained",
    "no_eligible_unspent_nugget": "no_open_high_salience_need",
    "skip_omit_unseat": "omit_wins_unseat",
}


def stamp_typed_skip(
    row: dict[str, Any],
    *,
    reason_code: str,
    evidence_refs: list[str] | None = None,
    value_forgone: list[str] | None = None,
    compensating_path: str | None = None,
    revisit_if: list[str] | None = None,
    decision_confidence: float = 0.85,
    owner_stage: str = "nugget_layup_compose",
) -> dict[str, Any]:
    """Stamp a skip row with full decision metadata (mutates and returns ``row``)."""
    row["skip"] = True
    row["text"] = ""
    row["word_count"] = 0
    row["skip_reason_code"] = reason_code
    refs = list(evidence_refs or [])
    tid = str(row.get("target_segment_id") or "")
    if tid and f"target:{tid}" not in refs:
        refs.append(f"target:{tid}")
    if reason_code and f"skip_reason:{reason_code}" not in refs:
        refs.append(f"skip_reason:{reason_code}")
    row["evidence_refs"] = refs
    forgone = list(value_forgone) if value_forgone is not None else list(
        row.get("value_forgone") or []
    )
    if not forgone:
        forgone = [str(x) for x in row_nugget_ids(row) if x]
    row["value_forgone"] = forgone
    path = compensating_path or _DEFAULT_COMPENSATING_PATHS.get(reason_code) or "typed_skip"
    row["compensating_path"] = path
    row["revisit_if"] = list(
        revisit_if
        if revisit_if is not None
        else (row.get("revisit_if") or ["selection_change", "new_grounded_copy"])
    )
    row["decision_confidence"] = float(decision_confidence)
    row["owner_stage"] = owner_stage
    return row


def is_justified_skip_row(
    row: dict[str, Any] | None,
    *,
    soft_migrate: bool = True,
) -> bool:
    """True when a skip row is intentional listener coverage (not a bare hole).

    Requires a known reason code and a compensating path. Known exempt codes
    soft-migrate without requiring previously stamped typed fields.
    """
    if not isinstance(row, dict) or not row.get("skip"):
        return False
    reason = str(row.get("skip_reason_code") or "").strip()
    if not reason or reason == "empty_or_skip":
        return False
    if reason not in JUSTIFIED_SKIP_REASON_CODES:
        return False
    path = str(row.get("compensating_path") or "").strip()
    if path:
        return True
    if soft_migrate and reason in _COVERAGE_EXEMPT_SKIP_REASONS:
        return True
    if soft_migrate and reason in _DEFAULT_COMPENSATING_PATHS:
        return True
    return False


def coverage_exempt_target_ids(
    ctx: RunContext,
    plan: dict[str, Any] | None = None,
) -> set[str]:
    """Targets that do not count against layup coverage denominator."""
    exempt = set(_opening_owned_targets(ctx))
    doc = plan if isinstance(plan, dict) else (
        ctx.read_json(PLAN_REL) if ctx.artifact_exists(PLAN_REL) else {}
    )
    for row in (doc.get("layups") or []) if isinstance(doc, dict) else []:
        if not isinstance(row, dict):
            continue
        tid = str(row.get("target_segment_id") or "")
        if not tid:
            continue
        if is_justified_skip_row(row, soft_migrate=True):
            exempt.add(tid)
    try:
        from interview_mux.air_script import VO_SEAT_MOVES, load_air_script
        from interview_mux.mastering_plan_loader import load_plan_raw

        mastering = load_plan_raw(ctx)
        script = load_air_script(mastering)
        if script and str(script.get("pass") or "") == "pass_b":
            for beat in script.get("beats") or []:
                if not isinstance(beat, dict):
                    continue
                if str(beat.get("montage_move") or "") in VO_SEAT_MOVES:
                    continue
                sid = str(beat.get("segment_id") or "")
                if sid:
                    exempt.add(sid)
    except Exception:
        pass
    return exempt


def uncovered_high_value_forgone(
    ctx: RunContext,
    plan: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """High-salience value_forgone claims with no compensating path / discharge."""
    if plan is None:
        plan = ctx.read_json(PLAN_REL) if ctx.artifact_exists(PLAN_REL) else {}
    plan = plan if isinstance(plan, dict) else {}
    corpus = ctx.read_json(CORPUS_REL) if ctx.artifact_exists(CORPUS_REL) else {}
    nug_by_id = {
        str(n.get("nugget_id") or ""): n
        for n in ((corpus.get("nuggets") or []) if isinstance(corpus, dict) else [])
        if isinstance(n, dict) and n.get("nugget_id")
    }
    discharged = {str(x) for x in (plan.get("discharged_nugget_ids") or []) if x}
    waived = {
        str(x.get("nugget_id") or x)
        for x in (plan.get("waived_nugget_ids") or [])
        if isinstance(x, (dict, str))
    }
    claimed: set[str] = set()
    for row in plan.get("layups") or []:
        if isinstance(row, dict) and not row.get("skip"):
            claimed.update(row_nugget_ids(row))
    out: list[dict[str, Any]] = []
    for row in plan.get("layups") or []:
        if not isinstance(row, dict) or not row.get("skip"):
            continue
        if is_justified_skip_row(row, soft_migrate=True) and str(
            row.get("compensating_path") or ""
        ).strip():
            continue
        for nid in [str(x) for x in (row.get("value_forgone") or row_nugget_ids(row)) if x]:
            if nid in discharged or nid in waived or nid in claimed:
                continue
            nug = nug_by_id.get(nid) or {}
            sal = str(nug.get("salience") or "")
            if sal not in ("high", "critical") and nid not in {
                str(x) for x in (plan.get("open_high_salience_nugget_ids") or []) if x
            }:
                continue
            out.append(
                {
                    "nugget_id": nid,
                    "target_segment_id": row.get("target_segment_id"),
                    "salience": sal or "high",
                    "skip_reason_code": row.get("skip_reason_code"),
                }
            )
    return out


def strip_model_order_lock(plan: dict[str, Any] | None) -> dict[str, Any]:
    """Discard any LLM-authored selection lock before stamping from selection."""
    out = dict(plan) if isinstance(plan, dict) else {}
    out.pop("order_lock", None)
    out.pop("order_content_hash", None)
    meta = out.get("_meta")
    if isinstance(meta, dict):
        meta = dict(meta)
        meta.pop("stale", None)
        meta.pop("stale_reason", None)
        out["_meta"] = meta
    return out


def _strip_trailing_canned_unlock(text: str) -> str:
    """Drop a trailing canned/generic unlock sentence from spoken lay-up text."""
    clean = _norm(text)
    if not clean:
        return ""
    parts = [p.strip() for p in _SENTENCE_SPLIT.split(clean) if p.strip()]
    if not parts:
        return clean
    if canned_air_violations(parts[-1]):
        parts = parts[:-1]
    return " ".join(parts).strip()


def derive_forward_unlock(
    row: dict[str, Any],
    *,
    target_text: str = "",
) -> str:
    """Build a concrete, non-canned forward_unlock from beat / listener need / target."""
    existing = str(row.get("forward_unlock") or "").strip()
    try:
        from interview_mux.media_ip_cta import is_lets_hear_hinge
    except Exception:
        is_lets_hear_hinge = lambda _t: False  # noqa: E731
    if existing and is_lets_hear_hinge(existing):
        return existing
    if (
        existing
        and not canned_air_violations(existing)
        and not _is_generic_unlock(existing)
    ):
        return existing if existing.endswith("?") else existing.rstrip(".!") + "?"

    beat = str(row.get("target_beat") or "").strip()
    listener = str(row.get("listener_need_entering_T") or "").strip()
    seed = beat or listener
    if seed:
        stem = seed.rstrip(".!")
        low = stem[:1].lower() + stem[1:] if stem else stem
        if stem.lower().startswith(("why ", "how ", "what ", "when ", "where ", "which ")):
            candidate = stem if stem.endswith("?") else f"{stem}?"
        else:
            candidate = f"Why does {low} matter for what follows?"
        if not canned_air_violations(candidate) and not _is_generic_unlock(candidate):
            return candidate

    from interview_mux.gap_vo_prior_context import _target_aware_forward_cues

    for cue in _target_aware_forward_cues(
        target_text or beat,
        category=str(row.get("line_category") or "extracted_context"),
    ):
        cue = " ".join(str(cue or "").split()).strip()
        if not cue:
            continue
        if not cue.endswith("?"):
            cue = cue.rstrip(".!") + "?"
        if canned_air_violations(cue) or _is_generic_unlock(cue):
            continue
        return cue
    return "Why does that beat change what the listener hears next?"


def heal_layup_analysis_fields(
    ctx: RunContext,
    plan: dict[str, Any] | None,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Fill missing analysis fields and replace banned generic unlocks before QC.

    LLM/schema paths often omit ``forward_unlock`` while appending canned
    ``What comes next?`` to ``text``. That combination hard-fails craft QC and
    cannot be fixed by re-mining alone.
    """
    out = dict(plan) if isinstance(plan, dict) else {"layups": []}
    layups = [dict(r) for r in (out.get("layups") or []) if isinstance(r, dict)]
    by_id = {
        str(row.get("segment_id") or ""): row
        for row in _manifest_segments(ctx)
        if isinstance(row, dict) and row.get("segment_id")
    }
    notes: list[dict[str, Any]] = []
    for row in layups:
        if row.get("skip") or not str(row.get("text") or "").strip():
            continue
        tid = str(row.get("target_segment_id") or "")
        target_text = str((by_id.get(tid) or {}).get("text") or "")
        before = str(row.get("text") or "").strip()
        stripped = _strip_trailing_canned_unlock(before)
        unlock = derive_forward_unlock(row, target_text=target_text)
        changed = False
        if unlock != str(row.get("forward_unlock") or "").strip():
            row["forward_unlock"] = unlock
            changed = True
        if not str(row.get("listener_need_entering_T") or "").strip():
            beat = str(row.get("target_beat") or "").strip()
            if beat:
                row["listener_need_entering_T"] = (
                    f"The prior stretch left this unresolved: {beat.rstrip('.')}."
                )
                changed = True
        body = stripped or _strip_trailing_canned_unlock(before)
        if not body:
            body = str(row.get("setup_from_nuggets") or row.get("target_beat") or "").strip()
        try:
            from interview_mux.media_ip_cta import is_lets_hear_hinge
        except Exception:
            is_lets_hear_hinge = lambda _t: False  # noqa: E731
        # Keep free-form spoken copy. Only strip banned trailing unlocks; never
        # force-append a forward-unlock / "let's hear…" hinge.
        if body and canned_air_violations(body):
            text = _strip_trailing_canned_unlock(body).strip() or body
            parts = [p.strip() for p in _SENTENCE_SPLIT.split(text) if p.strip()]
            if parts and canned_air_violations(parts[-1]):
                text = " ".join(parts[:-1]).strip() or text
        else:
            text = body
        # Preserve intentional let's-hear hinges when the model already wrote them.
        if body and is_lets_hear_hinge(body):
            text = body
        text = " ".join(text.split()).strip()
        if text and text != before:
            row["text"] = text
            row["word_count"] = _word_count(text)
            row["forward_cue_ok"] = True
            changed = True
        if changed:
            notes.append(
                {
                    "action": "heal_layup_analysis_fields",
                    "target_segment_id": tid,
                    "forward_unlock": unlock,
                }
            )
    out["layups"] = layups
    return out, notes


def _next_non_clone_target(
    ordered: list[str],
    by_id: dict[str, dict[str, Any]],
    *,
    after: str,
    voice: str,
) -> str:
    """First later native whose diarized speaker is not the clone voice."""
    try:
        start = ordered.index(after) + 1
    except ValueError:
        return ""
    for candidate in ordered[start:]:
        speaker = str((by_id.get(candidate) or {}).get("speaker_id") or "").strip()
        if speaker and speaker != voice:
            return candidate
    return ""


def _merge_nugget_layup_into(
    dest: dict[str, Any],
    source: dict[str, Any],
    nug_by_id: dict[str, dict[str, Any]],
) -> None:
    """Fold source nugget claims/setup into an existing dest layup (mutates dest)."""
    ids = list(dict.fromkeys([*row_nugget_ids(dest), *row_nugget_ids(source)]))
    dest["nugget_ids"] = ids
    dest["selected_nugget_ids"] = ids
    src_setup = str(source.get("setup_from_nuggets") or "").strip()
    dst_setup = str(dest.get("setup_from_nuggets") or "").strip()
    if src_setup and src_setup.casefold() not in dst_setup.casefold():
        dest["setup_from_nuggets"] = " ".join(
            p for p in (dst_setup, src_setup) if p
        ).strip()
    dest_text = str(dest.get("text") or "").strip()
    dest_fold = dest_text.casefold()
    extra: list[str] = []
    for nid in row_nugget_ids(source):
        bit = _nugget_claim_text(nug_by_id.get(nid))
        if bit and bit.casefold() not in dest_fold:
            extra.append(bit.rstrip(".") + ".")
    if extra:
        body = " ".join(extra)
        dest["text"] = f"{body} {dest_text}".strip() if dest_text else body
        dest["word_count"] = _word_count(str(dest.get("text") or ""))
    tps = list(
        dict.fromkeys(
            [
                *[str(x) for x in (dest.get("talking_point_ids") or []) if x],
                *[str(x) for x in (source.get("talking_point_ids") or []) if x],
            ]
        )
    )
    if tps:
        dest["talking_point_ids"] = tps


def apply_clone_voice_adjacency_skips(
    ctx: RunContext, plan: dict[str, Any] | None = None
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Keep clone-self-talk off air: skip generic lines, relocate nugget recoveries.

    Generic interviewer-target VO is a typed skip. Nugget-grounded lines retarget
    to the next non-clone native, or merge into that dest if it already has a layup.
    Proven excluded-tape cut-recovery stays on the clone speaker (EDL-exempt).
    """
    out, notes = dedupe_layup_rows_by_target(plan)
    try:
        from interview_mux.media_ip_cta import apply_cover_policy

        out, cover_notes = apply_cover_policy(ctx, out)
        notes.extend(cover_notes)
        from interview_mux.media_ip_cta import heal_on_air_cta_residue

        healed = heal_on_air_cta_residue(ctx)
        if healed.get("ordered_segment_ids"):
            out["ordered_segment_ids"] = list(healed.get("ordered_segment_ids") or [])
    except Exception:
        pass
    try:
        from interview_mux.gap_framing import is_cut_recovery_vo
        from interview_mux.source_topology import pickup_eligible_speaker_id
    except Exception:
        return out, notes

    voice = str(pickup_eligible_speaker_id(ctx) or "").strip()
    if not voice:
        return out, notes

    by_id = {
        str(row.get("segment_id") or ""): row
        for row in _manifest_segments(ctx)
        if isinstance(row, dict) and row.get("segment_id")
    }
    ordered = _ordered_ids(ctx)
    corpus = ctx.read_json(CORPUS_REL) if ctx.artifact_exists(CORPUS_REL) else {}
    corpus = corpus if isinstance(corpus, dict) else {}
    nug_by_id = {
        str(n.get("nugget_id") or ""): n
        for n in (corpus.get("nuggets") or [])
        if isinstance(n, dict) and n.get("nugget_id")
    }

    def _probe(row: dict[str, Any], tid: str) -> dict[str, Any]:
        return {
            "origin": "nugget_layup",
            "delivery": str(row.get("delivery") or "synthesize"),
            "nugget_ids": row_nugget_ids(row),
            "targets_segment_id": tid,
            "replaces_source_segments": list(row.get("replaces_source_segments") or []),
            "supports_segment_ids": list(row.get("supports_segment_ids") or []),
        }

    def _row_by_target(tid: str) -> dict[str, Any] | None:
        for candidate in out.get("layups") or []:
            if isinstance(candidate, dict) and str(candidate.get("target_segment_id") or "") == tid:
                return candidate
        return None

    for row in list(out.get("layups") or []):
        if not isinstance(row, dict) or row.get("skip"):
            continue
        if row.get("cta_cover") or row.get("clone_adjacency_exempt"):
            continue
        tid = str(row.get("target_segment_id") or "").strip()
        if not tid:
            continue
        target_speaker = str((by_id.get(tid) or {}).get("speaker_id") or "").strip()
        if not target_speaker or target_speaker != voice:
            continue
        if is_cut_recovery_vo(
            _probe(row, tid), ordered_segment_ids=ordered, nugget_corpus=corpus
        ):
            continue
        nids = row_nugget_ids(row)
        dest = _next_non_clone_target(ordered, by_id, after=tid, voice=voice)
        if nids and dest:
            dest_row = _row_by_target(dest)
            if dest_row is not None and row_is_aired(dest_row):
                _merge_nugget_layup_into(dest_row, row, nug_by_id)
                stamp_typed_skip(
                    row,
                    reason_code="merged_clone_adjacency",
                    evidence_refs=[
                        f"target:{tid}",
                        f"merged_into:{dest}",
                        f"clone_speaker:{voice}",
                    ],
                    value_forgone=nids,
                    compensating_path="merged_into_non_clone_target",
                    revisit_if=["clone_speaker_change"],
                    decision_confidence=0.9,
                    owner_stage="nugget_layup_compose",
                )
                notes.append(
                    {
                        "action": "merge_clone_voice_adjacency",
                        "target_segment_id": tid,
                        "merged_into": dest,
                        "line_id": row.get("line_id"),
                        "clone_speaker_id": voice,
                    }
                )
                continue
            old_tid = tid
            old_lid = str(row.get("line_id") or f"vo_layup_{old_tid}")
            row["target_segment_id"] = dest
            row["line_id"] = f"vo_layup_{dest}"
            hole = {
                "target_segment_id": old_tid,
                "line_id": old_lid if old_lid != row["line_id"] else f"vo_layup_{old_tid}",
            }
            stamp_typed_skip(
                hole,
                reason_code="compensated_by_prior_layup",
                evidence_refs=[
                    f"target:{old_tid}",
                    f"retarget_to:{dest}",
                    f"clone_speaker:{voice}",
                ],
                value_forgone=[],
                compensating_path="prior_layup",
                revisit_if=["clone_speaker_change"],
                decision_confidence=0.9,
                owner_stage="nugget_layup_compose",
            )
            layups_list = out.setdefault("layups", [])
            if isinstance(layups_list, list):
                layups_list.append(hole)
            notes.append(
                {
                    "action": "retarget_clone_voice_adjacency",
                    "from": old_tid,
                    "to": dest,
                    "line_id": row.get("line_id"),
                    "clone_speaker_id": voice,
                }
            )
            if dest_row is not None and dest_row is not row and dest_row.get("skip"):
                stamp_typed_skip(
                    dest_row,
                    reason_code="compensated_by_prior_layup",
                    evidence_refs=[f"target:{dest}", f"retarget_from:{tid}"],
                    value_forgone=row_nugget_ids(dest_row),
                    compensating_path="prior_layup",
                    revisit_if=["clone_speaker_change"],
                    decision_confidence=0.85,
                    owner_stage="nugget_layup_compose",
                )
            continue
        stamp_typed_skip(
            row,
            reason_code="clone_voice_adjacency",
            evidence_refs=[
                f"target:{tid}",
                f"clone_speaker:{voice}",
                "clone_voice_policy:before_slot_abuts_source",
            ],
            value_forgone=nids,
            compensating_path="clone_voice_policy",
            revisit_if=["clone_speaker_change", "cut_recovery_nuggets"],
            decision_confidence=0.95,
            owner_stage="nugget_layup_compose",
        )
        notes.append(
            {
                "action": "skip_clone_voice_adjacency",
                "target_segment_id": tid,
                "line_id": row.get("line_id"),
                "clone_speaker_id": voice,
            }
        )
    out, dedupe_notes = dedupe_layup_rows_by_target(out)
    notes.extend(dedupe_notes)
    return out, notes


def _speaker_role_map(ctx: RunContext) -> dict[str, str]:
    """Map speaker_id → role from speakers.json when available."""
    roles: dict[str, str] = {}
    if not ctx.artifact_exists("understanding/speakers.json"):
        return roles
    doc = ctx.read_json("understanding/speakers.json")
    if not isinstance(doc, dict):
        return roles
    for sp in doc.get("speakers") or []:
        if not isinstance(sp, dict):
            continue
        sid = str(sp.get("speaker_id") or "").strip()
        if sid:
            roles[sid] = str(sp.get("role") or "").strip().lower()
    return roles


def _segment_role(row: dict[str, Any] | None, role_map: dict[str, str]) -> str:
    if not isinstance(row, dict):
        return ""
    direct = str(row.get("speaker_role") or row.get("role") or "").strip().lower()
    if direct:
        return direct
    sid = str(row.get("speaker_id") or "").strip()
    return role_map.get(sid, "")


def _clear_native_interviewer_handoff(
    prior: dict[str, Any] | None,
    target: dict[str, Any] | None,
    *,
    role_map: dict[str, str],
) -> bool:
    """True when prior interviewer already cues the next guest answer."""
    from interview_mux.conversation_context import role_is_content, role_is_frame

    prior_role = _segment_role(prior, role_map)
    target_role = _segment_role(target, role_map)
    if not (role_is_frame(prior_role) and role_is_content(target_role)):
        return False
    prior_text = str((prior or {}).get("text") or (prior or {}).get("text_excerpt") or "").strip()
    # Strong signal: interviewer question → guest answer.
    if prior_text.rstrip().endswith("?"):
        return True
    # Softer: role change alone still means the native seam is conversational.
    return len(prior_text.split()) >= 4


def apply_clear_native_handoff_skips(
    ctx: RunContext, plan: dict[str, Any] | None = None
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Skip non-recovery VO when native interviewer→guest handoff is already clear."""
    out, notes = apply_clone_voice_adjacency_skips(ctx, plan)
    try:
        from interview_mux.gap_framing import is_cut_recovery_vo
    except Exception:
        return out, notes

    by_id = {
        str(row.get("segment_id") or ""): row
        for row in _manifest_segments(ctx)
        if isinstance(row, dict) and row.get("segment_id")
    }
    ordered = _ordered_ids(ctx)
    position = {sid: i for i, sid in enumerate(ordered)}
    role_map = _speaker_role_map(ctx)
    corpus = ctx.read_json(CORPUS_REL) if ctx.artifact_exists(CORPUS_REL) else {}
    corpus = corpus if isinstance(corpus, dict) else {}

    for row in out.get("layups") or []:
        if not isinstance(row, dict) or row.get("skip"):
            continue
        if row.get("cta_cover"):
            continue
        tid = str(row.get("target_segment_id") or "").strip()
        if not tid or tid not in position or position[tid] <= 0:
            continue
        prior_id = ordered[position[tid] - 1]
        prior = by_id.get(prior_id)
        target = by_id.get(tid)
        if not _clear_native_interviewer_handoff(prior, target, role_map=role_map):
            continue
        probe = {
            "origin": "nugget_layup",
            "delivery": str(row.get("delivery") or "synthesize"),
            "nugget_ids": row_nugget_ids(row),
            "targets_segment_id": tid,
            "replaces_source_segments": list(row.get("replaces_source_segments") or []),
            "supports_segment_ids": list(row.get("supports_segment_ids") or []),
        }
        if is_cut_recovery_vo(
            probe, ordered_segment_ids=ordered, nugget_corpus=corpus
        ):
            continue
        # Empty setup means no recovered facts — pure framing on a clear seam.
        setup = str(row.get("setup_from_nuggets") or "").strip()
        if setup and row_nugget_ids(row):
            # Still allow recovery VO with grounded nuggets even on Q→A seams.
            continue
        stamp_typed_skip(
            row,
            reason_code="native_self_orients",
            evidence_refs=[
                f"target:{tid}",
                f"prior:{prior_id}",
                "native_handoff:interviewer_to_guest",
            ],
            value_forgone=row_nugget_ids(row),
            compensating_path="native_self_orients",
            revisit_if=["excluded_nugget_recovery", "prior_role_change"],
            decision_confidence=0.9,
            owner_stage="nugget_layup_compose",
        )
        notes.append(
            {
                "action": "skip_clear_native_handoff",
                "target_segment_id": tid,
                "prior_segment_id": prior_id,
                "line_id": row.get("line_id"),
            }
        )
    return out, notes


def stamp_valueless_skips(
    ctx: RunContext, plan: dict[str, Any] | None = None
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Stamp skip holes that carry no recoverable listener value.

    Shrinks the coverage denominator to real VO candidates. Never pastes
    analysis fields into spoken text and never lowers ``min_layup_coverage``.
    """
    out = plan if isinstance(plan, dict) else (
        ctx.read_json(PLAN_REL) if ctx.artifact_exists(PLAN_REL) else {}
    )
    out = dict(out) if isinstance(out, dict) else {"layups": []}
    notes: list[dict[str, Any]] = []
    sparse = False
    try:
        from interview_mux.source_topology import vo_posture_is_sparse_omit

        sparse = vo_posture_is_sparse_omit(ctx)
    except Exception:
        sparse = False
    corpus = ctx.read_json(CORPUS_REL) if ctx.artifact_exists(CORPUS_REL) else {}
    high_ids: set[str] = set()
    for nug in (corpus.get("nuggets") or []) if isinstance(corpus, dict) else []:
        if not isinstance(nug, dict):
            continue
        nid = str(nug.get("nugget_id") or "")
        if nid and str(nug.get("salience") or "") in {"high", "critical"}:
            high_ids.add(nid)

    def _stamp(row: dict[str, Any], reason: str) -> None:
        tid = str(row.get("target_segment_id") or "")
        stamp_typed_skip(
            row,
            reason_code=reason,
            evidence_refs=[
                f"target:{tid}",
                f"skip_reason:{reason}",
                "valueless_skip:stamp",
            ],
            value_forgone=row_nugget_ids(row),
            compensating_path=_DEFAULT_COMPENSATING_PATHS.get(reason) or "typed_skip",
            revisit_if=["selection_change", "new_grounded_copy"],
            decision_confidence=0.82,
            owner_stage="nugget_layup_compose",
        )
        notes.append(
            {
                "action": "stamp_valueless_skip",
                "target_segment_id": tid,
                "reason_code": reason,
                "line_id": row.get("line_id"),
            }
        )

    for row in out.get("layups") or []:
        if not isinstance(row, dict) or not row.get("skip"):
            continue
        if is_justified_skip_row(row, soft_migrate=True):
            continue
        nids = row_nugget_ids(row)
        has_high = any(nid in high_ids for nid in nids)
        if not nids or not has_high:
            _stamp(
                row,
                "no_unrecovered_high_salience" if not nids else "self_explanatory_native",
            )

    if sparse:
        for row in out.get("layups") or []:
            if not isinstance(row, dict) or not row.get("skip"):
                continue
            if is_justified_skip_row(row, soft_migrate=True):
                continue
            if any(nid in high_ids for nid in row_nugget_ids(row)):
                continue
            _stamp(row, "self_explanatory_native")
    return out, notes


def skip_never_touch_cta_layups(
    ctx: RunContext, plan: dict[str, Any] | None = None
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Host-skip lay-ups that reuse omitted CTA / credits wording.

    QC currently hard-fails the whole compose on ``never_touch_cta[seg_X]``.
    The LLM often rotates the implicated native, so a stamp-valueless retry
    never converges. Skipping the overlapping row is the same omit the
    never-touch ledger already decided — not a quality waiver.
    """
    out = plan if isinstance(plan, dict) else (
        ctx.read_json(PLAN_REL) if ctx.artifact_exists(PLAN_REL) else {}
    )
    out = dict(out) if isinstance(out, dict) else {"layups": []}
    notes: list[dict[str, Any]] = []
    try:
        from interview_mux.media_ip_cta import (
            air_overlaps_never_touch,
            never_touch_segment_ids,
        )
    except Exception:
        return out, notes

    banned_ids = never_touch_segment_ids(ctx)
    for row in out.get("layups") or []:
        if not isinstance(row, dict) or row.get("skip"):
            continue
        if row.get("cta_cover"):
            continue
        tid = str(row.get("target_segment_id") or "").strip()
        text = str(row.get("text") or "").strip()
        hits_id = bool(tid and tid in banned_ids)
        hits_text = bool(text) and air_overlaps_never_touch(ctx, text)
        if not hits_id and not hits_text:
            continue
        stamp_typed_skip(
            row,
            reason_code="never_touch_cta",
            evidence_refs=[
                f"target:{tid}",
                "skip_reason:never_touch_cta",
                "never_touch:wording" if hits_text else "never_touch:target_id",
            ],
            value_forgone=row_nugget_ids(row),
            compensating_path="media_ip_cta_omit",
            revisit_if=["selection_change", "new_grounded_copy"],
            decision_confidence=0.95,
            owner_stage="nugget_layup_compose",
        )
        notes.append(
            {
                "action": "skip_never_touch_cta",
                "target_segment_id": tid,
                "reason_code": "never_touch_cta",
                "line_id": row.get("line_id"),
                "by_target_id": hits_id,
                "by_wording": hits_text,
            }
        )
    if notes:
        try:
            ctx.log(
                "nugget_layup_compose: skipped never-touch CTA layups "
                f"({len(notes)} row(s): "
                + ",".join(str(n.get("target_segment_id") or "") for n in notes[:8])
                + ")",
                level="warning",
                stage="nugget_layup_compose",
            )
        except Exception:
            pass
    return out, notes


def analysis_fields_leaked_into_text(row: dict[str, Any]) -> list[str]:
    """Return analysis field names whose prose was pasted into spoken text."""
    text = str(row.get("text") or "").strip()
    if not text:
        return []
    text_fold = text.casefold()
    leaked: list[str] = []
    for field in ("target_beat", "listener_need_entering_T"):
        val = str(row.get(field) or "").strip()
        if len(val) < 20:
            continue
        if val.casefold() in text_fold:
            leaked.append(field)
    setup = str(row.get("setup_from_nuggets") or "").strip()
    if setup and len(setup) >= 20 and setup.casefold() != text.casefold():
        # setup_from_nuggets is allowed as the body of air copy when distinct.
        pass
    return leaked


def prepare_layup_plan_for_persist(ctx: RunContext, plan: dict[str, Any] | None) -> dict[str, Any]:
    """Normalize, strip model lock, attach selection lock — before freshness assert."""
    doc = strip_model_order_lock(plan)
    doc = normalize_layup_talking_point_ledger(ctx, doc)
    doc, _notes = heal_layup_analysis_fields(ctx, doc)
    doc, _clone_notes = apply_clear_native_handoff_skips(ctx, doc)
    doc, _cta_notes = skip_never_touch_cta_layups(ctx, doc)
    doc, _skip_notes = stamp_valueless_skips(ctx, doc)
    return attach_selection_order_lock(ctx, doc)


def repair_or_skip_spoken_copy_layups(
    ctx: RunContext, plan: dict[str, Any] | None = None
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Use grounded recovery copy, otherwise explicitly skip unsafe layups.

    A target beat commonly paraphrases the following native verbatim.  It is
    useful planning metadata but is not safe listener-facing copy by itself.
    Never leave that row required after it fails the spoken-copy guard: G1 and
    EDL would both be unable to make progress.
    """
    out, notes = apply_clear_native_handoff_skips(ctx, plan)
    corpus = ctx.read_json(CORPUS_REL) if ctx.artifact_exists(CORPUS_REL) else {}
    nuggets = {
        str(nugget.get("nugget_id") or ""): nugget
        for nugget in (corpus.get("nuggets") or []) if isinstance(nugget, dict)
    }
    by_id = {
        str(row.get("segment_id") or ""): row
        for row in _manifest_segments(ctx)
        if isinstance(row, dict) and row.get("segment_id")
    }
    from interview_mux.spoken_copy_guard import (
        is_register_violation,
        scrub_spoken_register,
        spoken_copy_violations,
    )
    from interview_mux.gap_vo_prior_context import (
        _target_aware_forward_cues,
        last_sentence_restates_target,
        repair_last_sentence_layup,
        vo_target_overlap_ratio,
    )
    from interview_mux.opening_orientation import orientation_copy_unusable

    opening_targets = _opening_owned_targets(ctx)

    def _scrub_edit_structure(text: str) -> str:
        """Drop edit-unit nouns that trip spoken_edit_structure_ref."""
        scrubbed = re.sub(
            r"\b(?:the|this|that|our)\s+clips?'?s?\b",
            "this moment",
            text,
            flags=re.IGNORECASE,
        )
        scrubbed = re.sub(
            r"\b(?:the|this|that|our)\s+segments?'?s?\b",
            "this stretch",
            scrubbed,
            flags=re.IGNORECASE,
        )
        return " ".join(scrubbed.split()).strip()

    seen: list[str] = []
    for row in out.get("layups") or []:
        if not isinstance(row, dict) or row.get("skip"):
            continue
        target = str(row.get("target_segment_id") or "")
        if target and target in opening_targets:
            skip_code, skip_path = _opening_layup_skip_spec(ctx, target)
            stamp_typed_skip(
                row,
                reason_code=skip_code,
                evidence_refs=[
                    f"target:{target}",
                    "opening_orientation:owns_before_slot",
                ],
                value_forgone=row_nugget_ids(row),
                compensating_path=skip_path,
                revisit_if=["orientation_disabled", "opening_slot_freed"],
                decision_confidence=0.95,
                owner_stage="nugget_layup_compose",
            )
            notes.append(
                {
                    "action": "skip_unhealable_spoken_copy_layup",
                    "target_segment_id": target,
                    "line_id": row.get("line_id"),
                    "violations": ["opening_adjacency_with_orientation"],
                }
            )
            continue
        target_text = str((by_id.get(target) or {}).get("text") or "")
        text = str(row.get("text") or "").strip()
        leaked = analysis_fields_leaked_into_text(row)
        setup = str(row.get("setup_from_nuggets") or "").strip()
        if orientation_copy_unusable(text):
            core = setup or str(row.get("target_beat") or "").strip()
            if core:
                core = " ".join(core.split()).strip()
                if core[-1:] not in ".!":
                    core = core.rstrip("?") + "."
                rewritten = f"{core} Let's hear what that means for what follows."
                row["text"] = rewritten
                row["word_count"] = _word_count(rewritten)
                notes.append(
                    {
                        "action": "rewrite_meta_question_layup",
                        "target_segment_id": target,
                        "line_id": row.get("line_id"),
                    }
                )
                text = rewritten
        if leaked:
            unlock_early = str(row.get("forward_unlock") or "").strip()
            recover_bits = []
            if setup:
                recover_bits.append(
                    " ".join(bit for bit in (setup, unlock_early) if bit).strip()
                )
                recover_bits.append(setup)
            recovered_early = ""
            for candidate in recover_bits:
                candidate = " ".join(candidate.split()).strip()
                if not candidate or len(candidate.split()) < 6:
                    continue
                if candidate[-1:] not in ".!?":
                    candidate += "?"
                probe = dict(row)
                probe["text"] = candidate
                if analysis_fields_leaked_into_text(probe):
                    continue
                if spoken_copy_violations(
                    candidate,
                    evidence={
                        "target_excerpt": target_text,
                        "before_excerpt": "",
                        "after_topic": str(row.get("target_beat") or ""),
                        "strict_grounding": False,
                    },
                    seen_texts=seen,
                ):
                    continue
                recovered_early = candidate
                break
            if recovered_early:
                row["text"] = recovered_early
                row["word_count"] = _word_count(recovered_early)
                row["spoken_copy_recovered"] = True
                text = recovered_early
                notes.append(
                    {
                        "action": "repair_spoken_copy_layup",
                        "target_segment_id": target,
                        "line_id": row.get("line_id"),
                        "from": "analysis_leak_scrub",
                    }
                )
            else:
                stamp_typed_skip(
                    row,
                    reason_code="spoken_copy_unhealable",
                    evidence_refs=[
                        f"target:{target}",
                        *[f"analysis_leak:{f}" for f in leaked],
                    ],
                    value_forgone=row_nugget_ids(row),
                    compensating_path="omit_unsafe_spoken_copy",
                    revisit_if=[
                        "grounded_recovery_copy",
                        "rewrite_without_planner_fields",
                    ],
                    decision_confidence=0.95,
                    owner_stage="nugget_layup_compose",
                )
                row["spoken_copy_violations"] = [
                    "spoken_planner_meta",
                    *[f"analysis_leak:{f}" for f in leaked],
                ]
                notes.append(
                    {
                        "action": "skip_unhealable_spoken_copy_layup",
                        "target_segment_id": target,
                        "line_id": row.get("line_id"),
                        "violations": row["spoken_copy_violations"],
                    }
                )
                continue
        if text and "spoken_edit_structure_ref" in (
            spoken_copy_violations(
                text,
                evidence={
                    "target_excerpt": target_text,
                    "before_excerpt": str(row.get("listener_need_entering_T") or ""),
                    "after_topic": str(row.get("target_beat") or ""),
                    "strict_grounding": False,
                },
                seen_texts=seen,
            )
        ):
            scrubbed = _scrub_edit_structure(text)
            if scrubbed and scrubbed != text:
                text = scrubbed
                row["text"] = scrubbed
                row["word_count"] = _word_count(scrubbed)
        evidence = {
            "target_excerpt": target_text,
            "before_excerpt": str(row.get("listener_need_entering_T") or ""),
            "after_topic": str(row.get("target_beat") or ""),
            "strict_grounding": False,
        }
        violations = spoken_copy_violations(text, evidence=evidence, seen_texts=seen)
        if text and any(is_register_violation(v) for v in violations):
            # Role labels, gendered pronouns and name attribution are wording,
            # not substance: scrub the row's own copy first so its nugget body
            # survives, instead of skipping it and reopening the nugget for a
            # recovery that pastes the same note-style claim back (client run:
            # "The host recap adds that…", "Who is he…", stage refused).
            scrubbed = scrub_spoken_register(text)
            if scrubbed and scrubbed != text:
                text = scrubbed
                row["text"] = scrubbed
                row["word_count"] = _word_count(scrubbed)
                notes.append(
                    {
                        "action": "repair_spoken_copy_layup",
                        "target_segment_id": target,
                        "line_id": row.get("line_id"),
                        "from": "register_scrub",
                    }
                )
                violations = spoken_copy_violations(text, evidence=evidence, seen_texts=seen)
        # vo_value restatement can fail delivery even when spoken_copy_guard's
        # coarser restatement check is quiet. Forward-unlock endings are optional.
        soft_bad = False
        overlap = (
            vo_target_overlap_ratio(text, target_text) if text and target_text else 0.0
        )
        preview_ok = _nugget_preview_ok(
            row, text, target_text, overlap=overlap, corpus={"nuggets": list(nuggets.values())}
        )
        if text and target_text and overlap > 0.75 and not preview_ok:
            soft_bad = True
            if "spoken_next_clip_restatement" not in violations:
                violations = list(violations) + ["spoken_next_clip_restatement"]
        # Last-sentence restatement can fire with full-text overlap well below 0.75
        # (exec_11130 vo_layup_seg_038) — still soft-heal via repair_last_sentence_layup.
        last_restates = bool(
            text and target_text and last_sentence_restates_target(text, target_text)
        )
        if last_restates:
            soft_bad = True
            if "spoken_next_clip_restatement" not in violations:
                violations = list(violations) + ["spoken_next_clip_restatement"]
        if not violations and not soft_bad:
            seen.append(text)
            continue

        unlock = str(row.get("forward_unlock") or "").strip()
        from interview_mux.spoken_copy_guard import (
            _strip_name_attribution_clause,
            is_register_violation,
            topic_forward_recovery_candidates,
        )

        setup = _strip_name_attribution_clause(str(row.get("setup_from_nuggets") or "").strip())
        nugget_bits = [
            scrub_spoken_register(
                _strip_name_attribution_clause(
                    str((nuggets.get(nid) or {}).get("text_claim") or "").strip()
                )
            )
            for nid in row_nugget_ids(row)
        ]
        nugget_grounded = bool(row_nugget_ids(row) or any(nugget_bits))
        restates = bool(
            (target_text and text and overlap > 0.75 and not preview_ok) or last_restates
        )
        # Nugget bodies keep their claims; only wipe seed when a non-nugget line
        # restates T. Never recover by pasting analysis fields.
        seed_for_repair = "" if (restates and not nugget_grounded) else text
        candidates: list[str] = []
        if last_restates:
            candidates.append(
                repair_last_sentence_layup(
                    text,
                    target_text=target_text,
                    category=str(row.get("line_category") or "extracted_context"),
                    target_segment_id=target,
                )
            )
        if nugget_grounded:
            body = setup or " ".join(bit for bit in nugget_bits[:2] if bit).strip() or seed_for_repair
            if body:
                candidates.append(body)
            candidates.append(" ".join(bit for bit in nugget_bits[:2] if bit).strip())
            if setup:
                candidates.append(setup)
            if seed_for_repair:
                candidates.insert(0, seed_for_repair)
        else:
            candidates = candidates + [
                " ".join(bit for bit in nugget_bits[:2] if bit).strip(),
                seed_for_repair,
                repair_last_sentence_layup(
                    seed_for_repair,
                    target_text=target_text,
                    category=str(row.get("line_category") or "extracted_context"),
                    target_segment_id=target,
                ),
            ]
            if setup and setup.casefold() not in {
                str(row.get(f) or "").strip().casefold() for f in ANALYSIS_FIELDS
            }:
                candidates.insert(0, setup)
            if restates:
                candidates.extend(
                    _target_aware_forward_cues(
                        target_text,
                        category=str(row.get("line_category") or "extracted_context"),
                    )
                )
        if any(is_register_violation(v) for v in violations):
            reg_candidates = topic_forward_recovery_candidates(setup, unlock)
            candidates = reg_candidates + [c for c in candidates if c not in reg_candidates]
        recovered = ""
        for candidate in candidates:
            candidate = " ".join(candidate.split()).strip()
            if candidate and candidate[-1:] not in ".!?":
                candidate += "."
            if not candidate:
                continue
            if spoken_copy_violations(candidate, evidence=evidence, seen_texts=seen):
                continue
            if canned_air_violations(candidate) or _is_generic_unlock(candidate):
                continue
            cand_overlap = (
                vo_target_overlap_ratio(candidate, target_text) if target_text else 0.0
            )
            if target_text and cand_overlap > 0.75:
                probe_preview = dict(row)
                probe_preview["text"] = candidate
                if not _nugget_preview_ok(
                    probe_preview,
                    candidate,
                    target_text,
                    overlap=cand_overlap,
                    corpus={"nuggets": list(nuggets.values())},
                ):
                    continue
            # Reject recovery that still embeds planner analysis prose.
            probe = dict(row)
            probe["text"] = candidate
            if analysis_fields_leaked_into_text(probe):
                continue
            # Cue-only hinges are not a salvage when the row had nugget claims.
            if nugget_grounded:
                body_ok = any(
                    bit and bit.casefold() in candidate.casefold()
                    for bit in (*nugget_bits[:2], setup)
                    if bit
                )
                if not body_ok and seed_for_repair:
                    # Keep original nugget body if the candidate dropped it.
                    continue
                if not body_ok:
                    continue
            recovered = candidate
            break
        if recovered:
            row["text"] = recovered
            row["word_count"] = _word_count(recovered)
            row["spoken_copy_recovered"] = True
            row["forward_cue_ok"] = True
            notes.append(
                {
                    "action": "repair_spoken_copy_layup",
                    "target_segment_id": target,
                    "line_id": row.get("line_id"),
                }
            )
            seen.append(recovered)
            continue

        stamp_typed_skip(
            row,
            reason_code="spoken_copy_unhealable",
            evidence_refs=[
                f"target:{target}",
                *[f"spoken_copy:{v}" for v in violations[:6]],
            ],
            value_forgone=row_nugget_ids(row),
            compensating_path="omit_unsafe_spoken_copy",
            revisit_if=["grounded_recovery_copy", "target_text_change"],
            decision_confidence=0.9,
            owner_stage="nugget_layup_compose",
        )
        row["spoken_copy_violations"] = violations
        notes.append(
            {
                "action": "skip_unhealable_spoken_copy_layup",
                "target_segment_id": target,
                "line_id": row.get("line_id"),
                "violations": violations,
            }
        )
    return out, notes


def dedupe_gap_report_nugget_claims(
    gap_report: dict[str, Any] | None,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Keep the first owner of each nugget_id across interviewer_lines.

    Dense-package injection can stamp the same package nuggets onto an empty
    layup row that later rows already claim explicitly — strip later/dup claims.
    """
    report = dict(gap_report) if isinstance(gap_report, dict) else {}
    lines = [ln for ln in (report.get("interviewer_lines") or []) if isinstance(ln, dict)]
    owner: dict[str, str] = {}
    notes: list[dict[str, Any]] = []
    out_lines: list[dict[str, Any]] = []
    for ln in lines:
        lid = str(ln.get("line_id") or ln.get("targets_segment_id") or "")
        nids = [str(x) for x in (ln.get("nugget_ids") or []) if x]
        if not nids:
            out_lines.append(ln)
            continue
        kept: list[str] = []
        dropped: list[str] = []
        for nid in nids:
            if nid in owner and owner[nid] != lid:
                dropped.append(nid)
                continue
            owner.setdefault(nid, lid)
            kept.append(nid)
        if dropped:
            ln = dict(ln)
            ln["nugget_ids"] = kept
            notes.append(
                {
                    "action": "dedupe_nugget_claim",
                    "line_id": lid,
                    "dropped_nugget_ids": dropped,
                    "kept_by": {nid: owner[nid] for nid in dropped},
                }
            )
        out_lines.append(ln)
    if not notes:
        return report, []
    report["interviewer_lines"] = out_lines
    return report, notes


def _segment_windows(ctx: RunContext) -> dict[str, tuple[int, int]]:
    """Live segment id → (start_ms, end_ms) from manifest, hitch keepers, or boundaries."""
    out: dict[str, tuple[int, int]] = {}
    for rel in (
        "segments/manifest.json",
        "mastering/chapter_close_hitch/hitch_keepers.json",
        "segments/boundaries.json",
    ):
        if not ctx.artifact_exists(rel):
            continue
        try:
            doc = ctx.read_json(rel)
        except Exception:
            continue
        rows: list[Any]
        if isinstance(doc, dict):
            rows = list(
                doc.get("segments")
                or doc.get("keepers")
                or doc.get("boundaries")
                or []
            )
        else:
            rows = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            sid = str(row.get("segment_id") or "").strip()
            if not sid or sid in out:
                continue
            start = int(row.get("start_ms") or 0)
            end = int(row.get("end_ms") or start)
            if end > start:
                out[sid] = (start, end)
    return out


def _overlap_ms(a0: int, a1: int, b0: int, b1: int) -> int:
    return max(0, min(a1, b1) - max(a0, b0))


def adopt_layup_plan_to_selection(
    ctx: RunContext,
    mapping: dict[str, str] | None = None,
    remap_doc: dict[str, Any] | None = None,
    *,
    persist: bool = True,
    stage: str = "nugget_layup_compose",
    publish_gap: bool = True,
) -> dict[str, Any]:
    """Rewrite layup ids onto the live selection without remine.

    ``publish_gap=False`` fits the plan without republishing the gap body
    (selection-dependents reconcile, ISSUES 113: the omit stamps for retired
    ids are the sanitizer's, and the gap body is frozen).

    Displaced rows rebind onto the earliest overlapping live native. Extra hitch
    split children skip (no new VO, no compose). Satisfies
    ``assert_layup_fresh_vs_selection``.
    """
    if not nugget_layup_enabled():
        return {"ok": True, "skipped": True}
    if not ctx.artifact_exists(PLAN_REL):
        return {"ok": True, "no_plan": True}
    try:
        plan = ctx.read_json(PLAN_REL)
    except Exception:
        return {"ok": False, "error": "unreadable_plan"}
    if not isinstance(plan, dict):
        return {"ok": False, "error": "invalid_plan"}

    prior_producer = str(
        ((plan.get("_meta") or {}) if isinstance(plan.get("_meta"), dict) else {}).get(
            "producer_stage"
        )
        or ""
    ).strip()

    map_ids = {
        str(k): str(v)
        for k, v in (mapping or {}).items()
        if k and v
    }
    if not map_ids and isinstance(remap_doc, dict):
        map_ids = {
            str(k): str(v)
            for k, v in (remap_doc.get("old_to_new") or {}).items()
            if k and v
        }
    if map_ids:
        from interview_mux.segment_id_remap import apply_segment_id_map

        plan = apply_segment_id_map(plan, map_ids)

    selection = _ordered_ids(ctx)
    if not selection:
        return {"ok": True, "no_selection": True}

    layups = [dict(r) for r in (plan.get("layups") or []) if isinstance(r, dict)]
    windows = _segment_windows(ctx)
    by_target: dict[str, dict[str, Any]] = {}
    for row in layups:
        tid = str(row.get("target_segment_id") or "").strip()
        if tid and tid not in by_target:
            by_target[tid] = row

    inherited: list[str] = []
    skipped_ids: list[str] = []
    rebound: list[str] = []

    # Rebind rows whose target left the air order onto the earliest overlapping native.
    for row in list(layups):
        tid = str(row.get("target_segment_id") or "").strip()
        if not tid or tid in selection:
            continue
        tw = windows.get(tid)
        best: tuple[int, int, str] | None = None
        for sid in selection:
            sw = windows.get(sid)
            if not tw or not sw:
                continue
            ov = _overlap_ms(tw[0], tw[1], sw[0], sw[1])
            if ov <= 0:
                continue
            start = sw[0]
            if best is None or start < best[1] or (start == best[1] and ov > best[0]):
                best = (ov, start, sid)
        if best is None:
            continue
        dest = best[2]
        if dest in by_target and dest != tid:
            continue
        old_tid = tid
        row["target_segment_id"] = dest
        lid = str(row.get("line_id") or "")
        if lid and old_tid in lid:
            row["line_id"] = lid.replace(old_tid, dest)
        else:
            row["line_id"] = f"vo_layup_{dest}"
        row["adopted_from_target"] = old_tid
        by_target.pop(old_tid, None)
        by_target[dest] = row
        rebound.append(dest)
        inherited.append(dest)

    for sid in selection:
        if sid in by_target:
            continue
        skip_row = {
            "target_segment_id": sid,
            "line_id": f"vo_layup_{sid}",
        }
        stamp_typed_skip(
            skip_row,
            reason_code="hitch_split_no_inherit",
            compensating_path="native_self_orients",
            owner_stage=stage,
        )
        layups.append(skip_row)
        by_target[sid] = skip_row
        skipped_ids.append(sid)

    plan = dict(plan)
    plan["layups"] = layups
    plan = attach_selection_order_lock(ctx, plan)
    meta = dict(plan.get("_meta") or {}) if isinstance(plan.get("_meta"), dict) else {}
    meta["adopted_to_selection"] = True
    meta["adopted_inherited"] = inherited
    meta["adopted_skipped"] = skipped_ids
    meta["adopted_rebound"] = rebound
    # S2/S3: hitch integrity adopt must not claim layup ownership on the plan.
    if prior_producer and stage == "chapter_close_hitch":
        meta["producer_stage"] = prior_producer
    plan["_meta"] = meta
    if persist:
        write_kw: dict[str, Any] = {"skip_handoff": True, "stage_key": stage}
        if stage != "nugget_layup_compose" or map_ids:
            # Cousin writers (e.g. hitch) must declare segment_id_remap under freeze.
            write_kw["mutation_class"] = "segment_id_remap"
        ctx.write_json(PLAN_REL, plan, **write_kw)
        # S3: hitch must not republish gap body — owned stages own that land.
        if stage == "chapter_close_hitch" or not publish_gap:
            return {
                "ok": True,
                "inherited": inherited,
                "skipped": skipped_ids,
                "rebound": rebound,
                "gap_publish": False,
            }
        try:
            commit_layup_gap_authority(ctx, plan)
        except Exception as exc:
            return {
                "ok": False,
                "error": str(exc)[:240],
                "inherited": inherited,
                "skipped": skipped_ids,
                "rebound": rebound,
            }
    return {
        "ok": True,
        "inherited": inherited,
        "skipped": skipped_ids,
        "rebound": rebound,
    }


def attach_selection_order_lock(ctx: RunContext, plan: dict[str, Any]) -> dict[str, Any]:
    """Copy selection air order + order_lock onto a layup plan document."""
    out = dict(plan)
    ordered = _ordered_ids(ctx)
    if ordered:
        out["ordered_segment_ids"] = list(ordered)
    if not ctx.artifact_exists("master/selection.json"):
        return out
    try:
        from interview_mux.order_hash import copy_order_lock, get_order_lock

        sel = ctx.read_json("master/selection.json")
        if isinstance(sel, dict) and (get_order_lock(sel) or sel.get("ordered_segment_ids")):
            out = copy_order_lock(sel, out)
    except Exception:
        pass
    return out


def _compose_shards_block_gap_publish(plan: dict[str, Any] | None) -> bool:
    """True when mid-shard plan must not stamp gap_report (S1)."""
    if not isinstance(plan, dict):
        return False
    meta = plan.get("_meta") if isinstance(plan.get("_meta"), dict) else {}
    if not meta.get("compose_shards_pending"):
        return False
    idx = meta.get("compose_shard_index")
    total = meta.get("compose_shard_total")
    layups = plan.get("layups")
    final_shard_done = (
        idx is not None
        and total is not None
        and int(idx) >= int(total)
        and isinstance(layups, list)
        and len(layups) > 0
    )
    return not final_shard_done


def commit_layup_gap_authority(
    ctx: RunContext,
    plan: dict[str, Any] | None = None,
    *,
    allow_mid_shard: bool = False,
) -> dict[str, Any]:
    """Sole layup → gap_report publish + hosted-floor path (S1).

    Compose may QC-complete the plan without calling this. Mid-shard pending
    plans must not hollow-stamp gap_report. Callers that need single-flight
    should wrap with ``gap_report_write_lock`` (do not nest locks here).
    """
    if plan is None:
        plan = ctx.read_json(PLAN_REL) if ctx.artifact_exists(PLAN_REL) else {}
    if not isinstance(plan, dict):
        plan = {}
    if not allow_mid_shard and _compose_shards_block_gap_publish(plan):
        from interview_mux.loud_fail import raise_loud_failure

        meta = plan.get("_meta") if isinstance(plan.get("_meta"), dict) else {}
        raise_loud_failure(
            ctx,
            "layup gap publish refused while compose_shards_pending "
            f"(shard {meta.get('compose_shard_index')}/{meta.get('compose_shard_total')})",
            stage="nugget_layup_compose",
            reason="layup_gap_publish_refused_shards_pending",
        )
    return publish_layup_plan_to_gap_report(ctx, plan)


def ensure_layup_gap_authority(ctx: RunContext) -> dict[str, Any] | None:
    """Republish nugget layup lines into gap_report when EDL VO lacks script authority."""
    if not ctx.artifact_exists(PLAN_REL):
        return None
    gap: dict[str, Any] = {}
    if ctx.artifact_exists(GAP_REL):
        loaded = ctx.read_json(GAP_REL)
        gap = loaded if isinstance(loaded, dict) else {}
    lines = [ln for ln in (gap.get("interviewer_lines") or []) if isinstance(ln, dict)]
    needs_publish = not lines
    if not needs_publish and ctx.artifact_exists("master/edl.json"):
        edl = ctx.read_json("master/edl.json")
        aired = {
            str(c.get("line_id") or "")
            for c in ((edl or {}).get("clips") or [])
            if isinstance(c, dict)
            and str(c.get("type") or "") == "vo_pickup"
            and c.get("line_id")
        }
        gap_ids = {str(ln.get("line_id") or "") for ln in lines if ln.get("line_id")}
        needs_publish = bool(aired - gap_ids)
    if not needs_publish:
        return gap if gap else None
    return commit_layup_gap_authority(ctx)


def layup_claimed_air_missing_high_gap(ctx: RunContext) -> list[str]:
    """S5: high-gap segs the plan claimed as aired but gap_report still lacks a line.

    Framing owns "never had a line." Layup only owes when it claimed air for that
    target and the authoritative gap body is still empty for it.
    """
    if not ctx.artifact_exists(PLAN_REL) or not ctx.artifact_exists(GAP_REL):
        return []
    if not ctx.artifact_exists("understanding/gap_evaluations.json"):
        return []
    try:
        from interview_mux.deterministic_lint import _lint_optimal_questions
        from interview_mux.gap_fill_eligibility import gap_fill_was_skipped

        if gap_fill_was_skipped(ctx):
            return []
        report = ctx.read_json(GAP_REL)
        if not isinstance(report, dict):
            return []
        errs = _lint_optimal_questions(report, ctx)
    except Exception:
        return []
    dirty = [
        str(e)
        for e in errs
        if "has no interviewer line" in str(e)
    ]
    if not dirty:
        return []
    # Extract segment ids from lint prose ("… segment seg_014 has no …").
    missing_ids: set[str] = set()
    for msg in dirty:
        for token in str(msg).replace(",", " ").split():
            if token.startswith("seg_"):
                missing_ids.add(token.strip(".:;"))
    if not missing_ids:
        return []
    plan = ctx.read_json(PLAN_REL)
    if not isinstance(plan, dict):
        return []
    claimed: list[str] = []
    for row in plan.get("layups") or []:
        if not isinstance(row, dict) or not row_is_aired(row):
            continue
        tid = str(row.get("target_segment_id") or "").strip()
        if tid and tid in missing_ids:
            claimed.append(tid)
    return claimed


def _scrub_foreign_before_vo_for_hollow_preserve(
    report: dict[str, Any],
    *,
    min_active: int | None = None,
    ideal: int | None = None,
    live_targets: set[str] | None = None,
    open_talking_point_ids: set[str] | None = None,
    open_nugget_ids: set[str] | None = None,
) -> dict[str, Any]:
    """Rank-to-budget keep when hollow-preserving under layup authority.

    Replaces mass foreign scrub: score contentful before-VO (including
    ``gap_framing_compose``), keep up to ``ideal`` (at least ``min_active``),
    adopt winners into ``nugget_layup`` origin. Durable CTA / waive rows stay out.
    """
    from interview_mux.hosted_vo_authority import rank_to_budget_select
    from interview_mux.opening_orientation import is_episode_orientation

    if not isinstance(report, dict):
        return report
    lines = [ln for ln in (report.get("interviewer_lines") or []) if isinstance(ln, dict)]
    need_n = int(min_active or 0)
    ideal_n = max(need_n, int(ideal if ideal is not None else need_n))
    if ideal_n <= 0 and need_n <= 0:
        return report
    kept, pruned, sel_meta = rank_to_budget_select(
        lines,
        need=need_n,
        ideal=ideal_n,
        live_targets=live_targets,
        open_talking_point_ids=open_talking_point_ids,
        open_nugget_ids=open_nugget_ids,
    )
    # Preserve any non-before authority rows ranker skipped (after/bed pins).
    kept_ids = {str(ln.get("line_id") or "") for ln in kept if ln.get("line_id")}
    for ln in lines:
        lid = str(ln.get("line_id") or "")
        if lid and lid in kept_ids:
            continue
        if is_episode_orientation(ln):
            continue
        placement = str(ln.get("placement") or "").strip()
        origin = str(ln.get("origin") or "").strip()
        if placement == "before":
            continue
        if origin in AUTHORITY_BODY_ORIGINS:
            kept.append(dict(ln))
            if lid:
                kept_ids.add(lid)
    if kept == lines and not pruned:
        return report
    out = dict(report)
    out["interviewer_lines"] = kept
    out["nugget_layup_authority"] = True
    meta = dict(out.get("_meta") or {}) if isinstance(out.get("_meta"), dict) else {}
    meta["hollow_preserve_rank_to_budget"] = sel_meta
    meta["hollow_preserve_pruned"] = len(pruned)
    if meta:
        out["_meta"] = meta
    return out


def _selection_order_content_hash(ctx: RunContext) -> str:
    try:
        if not ctx.artifact_exists("master/selection.json"):
            return ""
        sel = ctx.read_json("master/selection.json")
        if not isinstance(sel, dict):
            return ""
        h = str(sel.get("order_content_hash") or "").strip()
        if h:
            return h
        ordered = [str(s) for s in (sel.get("ordered_segment_ids") or []) if s]
        if not ordered:
            return ""
        return hashlib.sha256("|".join(ordered).encode("utf-8")).hexdigest()[:16]
    except Exception:
        return ""


def _draft_pool_lines(
    ctx: RunContext,
    *,
    live_targets: set[str] | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Load hash-fresh draft interviewer_lines for rank-to-budget adopt."""
    meta: dict[str, Any] = {
        "draft_present": False,
        "draft_fresh": False,
        "stale_draft_ignored": False,
        "draft_line_count": 0,
    }
    if not ctx.artifact_exists(GAP_DRAFT_REL):
        return [], meta
    try:
        draft = ctx.read_json(GAP_DRAFT_REL)
    except Exception:
        return [], meta
    if not isinstance(draft, dict):
        return [], meta
    meta["draft_present"] = True
    cur_hash = _selection_order_content_hash(ctx)
    draft_meta = draft.get("_meta") if isinstance(draft.get("_meta"), dict) else {}
    draft_hash = str(
        draft_meta.get("selection_order_content_hash")
        or draft.get("selection_order_content_hash")
        or draft_meta.get("selection_order_hash")
        or ""
    ).strip()
    # Missing stamp: treat as fresh only when selection has no hash yet.
    if cur_hash and draft_hash and draft_hash != cur_hash:
        meta["stale_draft_ignored"] = True
        ctx.log(
            "nugget_layup: stale draft pool ignored "
            f"(draft_hash={draft_hash} selection_hash={cur_hash})",
            level="warning",
            stage="nugget_layup_compose",
            action_id="gap_vo.stale_draft_pool_ignored",
        )
        return [], meta
    meta["draft_fresh"] = True
    live = live_targets or set(_ordered_ids(ctx))
    out: list[dict[str, Any]] = []
    for ln in draft.get("interviewer_lines") or []:
        if not isinstance(ln, dict):
            continue
        if not str(ln.get("text") or "").strip():
            continue
        tid = str(ln.get("targets_segment_id") or "").strip()
        if live and tid and tid not in live:
            continue
        out.append(dict(ln))
    meta["draft_line_count"] = len(out)
    return out, meta


def _plan_recoverable_pool_lines(
    plan: dict[str, Any] | None,
    *,
    live_targets: set[str] | None = None,
) -> list[dict[str, Any]]:
    """Plan rows with recoverable text (incl. prefer-native skips) as adopt pool."""
    from interview_mux.hosted_vo_authority import PREFER_NATIVE_SKIP_CODES

    live = live_targets or set()
    out: list[dict[str, Any]] = []
    for row in (plan or {}).get("layups") or []:
        if not isinstance(row, dict):
            continue
        text = str(row.get("text") or "").strip()
        tid = str(row.get("target_segment_id") or "").strip()
        if not text or not tid:
            continue
        if live and tid not in live:
            continue
        code = str(row.get("skip_reason_code") or "").strip()
        if row.get("skip") and code and code not in PREFER_NATIVE_SKIP_CODES:
            # Durable / non-prefer skips stay out of the pool.
            continue
        lid = str(row.get("line_id") or "").strip() or f"vo_layup_{tid}"
        out.append(
            {
                "line_id": lid,
                "origin": "nugget_layup",
                "gap_type": "nugget_layup",
                "line_category": "extracted_context",
                "text": text,
                "targets_segment_id": tid,
                "placement": "before",
                "delivery": "synthesize",
                "nugget_ids": list(row.get("nugget_ids") or []),
                "skip_reason_code": code or None,
            }
        )
    return out


def _build_rank_to_budget_pool(
    ctx: RunContext,
    *,
    prior_lines: list[dict[str, Any]],
    plan: dict[str, Any] | None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Union live prior + hash-fresh draft + plan recoverable text; dedupe."""
    from interview_mux.hosted_vo_authority import RANK_ADOPTABLE_ORIGINS
    from interview_mux.opening_orientation import is_episode_orientation

    live = set(_ordered_ids(ctx))
    draft_lines, draft_meta = _draft_pool_lines(ctx, live_targets=live)
    plan_lines = _plan_recoverable_pool_lines(plan, live_targets=live)
    pool: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    seen_targets: set[str] = set()
    source_counts = {
        "prior": 0,
        "draft": 0,
        "plan": 0,
    }

    def _admit(ln: dict[str, Any], source: str) -> None:
        if is_episode_orientation(ln):
            return
        text = str(ln.get("text") or "").strip()
        if not text:
            return
        origin = str(ln.get("origin") or "").strip()
        if origin and origin not in RANK_ADOPTABLE_ORIGINS:
            # Draft framing compose is the primary revive source.
            if origin != "gap_framing_compose" and source != "draft":
                return
        lid = str(ln.get("line_id") or "").strip()
        tid = str(ln.get("targets_segment_id") or "").strip()
        if lid and lid in seen_ids:
            return
        if tid and tid in seen_targets:
            return
        if live and tid and tid not in live:
            return
        pool.append(dict(ln))
        if lid:
            seen_ids.add(lid)
        if tid:
            seen_targets.add(tid)
        source_counts[source] = int(source_counts.get(source) or 0) + 1

    for ln in prior_lines:
        if isinstance(ln, dict):
            _admit(ln, "prior")
    for ln in draft_lines:
        _admit(ln, "draft")
    for ln in plan_lines:
        _admit(ln, "plan")

    meta = {
        **draft_meta,
        "pool_source_counts": source_counts,
        "pool_size": len(pool),
    }
    return pool, meta


def _framing_floor_topup(
    ctx: RunContext,
    *,
    candidate_lines: list[dict[str, Any]],
    prior_lines: list[dict[str, Any]],
    seen_targets: set[str],
    need: int,
    plan: dict[str, Any] | None = None,
) -> tuple[list[dict[str, Any]], list[str], dict[str, Any] | None]:
    """Rank-to-budget adopt toward ``need`` from prior/draft/plan pool (no invent)."""
    from interview_mux.hosted_vo_authority import apply_rank_to_budget_fill

    notes: list[str] = []
    pool, pool_meta = _build_rank_to_budget_pool(
        ctx, prior_lines=prior_lines, plan=plan
    )
    if pool_meta.get("stale_draft_ignored"):
        notes.append("stale_draft_ignored")
    pool_n = len(pool)
    natives = len(_ordered_ids(ctx))
    effective_need = max(0, int(need))
    if natives > 0:
        effective_need = min(effective_need, natives)
    if pool_n > 0:
        effective_need = min(effective_need, pool_n + _count_active_synthetic_lines(candidate_lines))
    filled, fill_meta, plan_out = apply_rank_to_budget_fill(
        ctx,
        keep_lines=list(candidate_lines or []),
        pool_lines=pool,
        plan=plan if isinstance(plan, dict) else None,
        seen_targets=set(seen_targets or set()),
        fill_to="need",
        need_override=effective_need,
    )
    adopted = list(fill_meta.get("adopted_line_ids") or [])
    if adopted:
        notes.append(f"adopted={len(adopted)}")
    fill_meta = dict(fill_meta)
    fill_meta["pool"] = pool_meta
    fill_meta["effective_need"] = effective_need
    return filled, notes, plan_out if isinstance(plan_out, dict) else (
        plan if isinstance(plan, dict) else None
    )


def publish_layup_plan_to_gap_report(
    ctx: RunContext,
    plan: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Merge lay-up before-VO into gap_report while preserving episode orientation."""
    from interview_mux.opening_orientation import (
        ensure_episode_orientation,
        is_episode_orientation,
        orientation_omitted,
    )
    from interview_mux.artifact_writes import write_validated_artifact

    if plan is None:
        plan = ctx.read_json(PLAN_REL) if ctx.artifact_exists(PLAN_REL) else {}
    if not isinstance(plan, dict):
        plan = {}
    plan, _ = apply_clone_voice_adjacency_skips(ctx, plan)
    plan, _ = dedupe_layup_rows_by_target(plan)
    plan = attach_selection_order_lock(ctx, plan)
    # Publishing a plan built for a different air order silently mis-times every
    # before-VO — refuse instead.
    assert_layup_fresh_vs_selection(ctx, plan)

    existing = ctx.read_json(GAP_REL) if ctx.artifact_exists(GAP_REL) else {}
    if not isinstance(existing, dict):
        existing = {}
    prior_lines = [ln for ln in (existing.get("interviewer_lines") or []) if isinstance(ln, dict)]
    orientation = [
        ln
        for ln in prior_lines
        if is_episode_orientation(ln)
        and str(ln.get("text") or "").strip()
        and str(ln.get("targets_segment_id") or "").strip()
        and str(ln.get("placement") or "").strip()
    ]

    body: list[dict[str, Any]] = []
    seen_targets: set[str] = set()
    dense_meta: dict[str, dict[str, Any]] = {}
    try:
        from interview_mux.information_packages import (
            dense_targets_from_plan,
            packages_affect_air,
        )

        if packages_affect_air() and ctx.artifact_exists("mastering/mastering_plan.json"):
            mp = ctx.read_json("mastering/mastering_plan.json")
            dense_meta = dense_targets_from_plan(mp if isinstance(mp, dict) else {})
    except Exception:
        dense_meta = {}

    # Explicit plan claims win over dense-package backfill so package-shared
    # nugget lists cannot collide with later layups that already own them.
    claimed_nugs: set[str] = set()
    for row in plan.get("layups") or []:
        if isinstance(row, dict) and row_is_aired(row):
            claimed_nugs.update(row_nugget_ids(row))

    # Orientation already owns the opening handoff into the first native. A
    # second before-VO on that same target is the opening-adjacency conflict
    # that edl_narrative_audit rejects (orientation + opening layup both fire
    # before clip one). Suppress the duplicate layup row at publish time.
    orientation_targets = _opening_owned_targets(ctx)

    for row in plan.get("layups") or []:
        if not isinstance(row, dict):
            continue
        row_d = row
        tid = str(row_d.get("target_segment_id") or "").strip()
        if tid and tid in orientation_targets:
            skip_code, skip_path = _opening_layup_skip_spec(ctx, tid)
            stamp_typed_skip(
                row_d,
                reason_code=skip_code,
                evidence_refs=[
                    f"target:{tid}",
                    "opening_orientation:owns_before_slot",
                ],
                compensating_path=skip_path,
                revisit_if=["orientation_disabled", "opening_slot_freed"],
                decision_confidence=0.95,
            )
            continue
        if tid and tid in dense_meta:
            row_d["detail_budget"] = "dense"
            row_d["information_package_id"] = dense_meta[tid].get("package_id")
            # Prefer unclaimed package-cited nuggets when the layup row is empty.
            if not row_nugget_ids(row_d) and dense_meta[tid].get("nugget_ids"):
                injected = [
                    str(n)
                    for n in (dense_meta[tid].get("nugget_ids") or [])
                    if n and str(n) not in claimed_nugs
                ]
                if injected:
                    row_d["nugget_ids"] = injected
                    claimed_nugs.update(injected)
        line = layup_line_from_row(row_d)
        if not line:
            continue
        tid = str(line["targets_segment_id"])
        if tid in seen_targets:
            continue
        if tid in orientation_targets:
            continue
        seen_targets.add(tid)
        try:
            from interview_mux.speaker_delivery_plan import stamp_episode_vo_identity

            line = stamp_episode_vo_identity(ctx, line)
        except Exception:
            pass
        body.append(line)

    # Keep non-orientation operator pins and mix-time high-gap fills that
    # are not superseded by a layup before-slot on the same target.
    for ln in prior_lines:
        if is_episode_orientation(ln):
            continue
        origin = str(ln.get("origin") or "")
        lid = str(ln.get("line_id") or "")
        keep = origin in {"operator", "high_gap_vo_fill"} or lid.startswith("vo_fill_")
        if not keep:
            continue
        tid = str(ln.get("targets_segment_id") or "")
        if tid and tid not in seen_targets:
            body.append(ln)
            seen_targets.add(tid)

    candidate_lines = orientation + body
    # G-Framing Yes floor: adopt toward need from draft-backed pool; under-floor
    # is advisory-only (never loud-blocks the master).
    floor_adopt_meta: dict[str, Any] | None = None
    try:
        from interview_mux.gap_fill_eligibility import (
            hosted_framing_requires_synthetic_vo,
            min_synthetic_vo_lines,
        )

        if hosted_framing_requires_synthetic_vo(ctx):
            need = min_synthetic_vo_lines(ctx)
            from interview_mux.hosted_vo_authority import vo_budget_bands

            _need_b, _ideal_n, _max_b = vo_budget_bands(ctx)
            need = max(int(need), int(_need_b))
            active_new = _count_active_synthetic_lines(candidate_lines)
            if active_new < need:
                sel_hash = _selection_order_content_hash(ctx)
                existing_meta = (
                    existing.get("_meta")
                    if isinstance(existing.get("_meta"), dict)
                    else {}
                )
                prior_adopt = (
                    existing_meta.get("rank_to_budget_adopt")
                    if isinstance(existing_meta.get("rank_to_budget_adopt"), dict)
                    else {}
                )
                already_adopted = bool(
                    sel_hash
                    and str(prior_adopt.get("order_content_hash") or "") == sel_hash
                )
                warnings = [
                    str(w)
                    for w in ((plan.get("warnings") or []) if isinstance(plan, dict) else [])
                ]
                hollow_plan = (
                    (not (plan.get("layups") or []))
                    or any("compose_restart" in w for w in warnings)
                    or active_new == 0
                )
                prior_active = _count_active_synthetic_lines(prior_lines)
                if hollow_plan and prior_active >= need and not already_adopted:
                    # Prefer a prior body that already meets the floor over a hollow plan.
                    ctx.log(
                        "nugget_layup: hollow plan under G-Framing Yes "
                        f"(active={active_new} < {need}; preserving prior {prior_active})",
                        level="warning",
                        stage="nugget_layup_compose",
                    )
                    return existing
                if already_adopted:
                    floor_adopt_meta = {
                        "skipped": "already_adopted_this_selection",
                        "order_content_hash": sel_hash,
                        "active_before": active_new,
                        "need": need,
                    }
                    ctx.log(
                        "nugget_layup: skip re-adopt under floor "
                        f"(active={active_new} < {need}; hash={sel_hash})",
                        level="info",
                        stage="nugget_layup_compose",
                        action_id="gap_vo.rank_to_budget_adopt_skip_thrash",
                    )
                else:
                    filled, adopt_notes, plan_out = _framing_floor_topup(
                        ctx,
                        candidate_lines=candidate_lines,
                        prior_lines=prior_lines,
                        seen_targets=seen_targets,
                        need=need,
                        plan=plan,
                    )
                    candidate_lines = filled
                    if isinstance(plan_out, dict):
                        plan = plan_out
                    active_new = _count_active_synthetic_lines(candidate_lines)
                    floor_adopt_meta = {
                        "order_content_hash": sel_hash,
                        "need": need,
                        "active_after": active_new,
                        "notes": adopt_notes,
                    }
                    ctx.log(
                        "nugget_layup: rank-to-budget adopt under floor "
                        f"(active={active_new} need={need} notes={adopt_notes})",
                        level="info",
                        stage="nugget_layup_compose",
                        action_id="gap_vo.rank_to_budget_adopt",
                        detail=floor_adopt_meta,
                    )
                if active_new < need:
                    # Advisory continue — never loud-block master on count floor.
                    raise_hosted_vo_floor_unsatisfiable(
                        ctx,
                        need=need,
                        active=active_new,
                        eligible_nuggets=eligible_nugget_count_for_floor(ctx),
                    )
    except Exception as _floor_exc:
        from interview_mux.loud_fail import LoudStageFailure

        # Count-floor path must not abort publish; log and continue with best body.
        if isinstance(_floor_exc, LoudStageFailure):
            ctx.log(
                f"nugget_layup: floor advisory swallowed loud failure: {_floor_exc}",
                level="warning",
                stage="nugget_layup_compose",
            )
        else:
            ctx.log(
                f"nugget_layup: under-floor adopt skipped: {_floor_exc}",
                level="warning",
                stage="nugget_layup_compose",
            )

    report = {
        **{k: v for k, v in existing.items() if k not in ("interviewer_lines", "_meta")},
        "interviewer_lines": candidate_lines,
        "nugget_layup_authority": True,
    }
    if floor_adopt_meta:
        _rmeta = dict(report.get("_meta") or {}) if isinstance(report.get("_meta"), dict) else {}
        _rmeta["rank_to_budget_adopt"] = floor_adopt_meta
        report["_meta"] = _rmeta
    report, _dedupe_notes = dedupe_gap_report_nugget_claims(report)
    ordered = _ordered_ids(ctx)
    orient_nugget_ids = [
        str(x) for x in (plan.get("orientation_nugget_recovery_ids") or []) if x
    ]
    report, _notes = ensure_episode_orientation(
        ctx, report, ordered, orientation_nugget_ids=orient_nugget_ids or None
    )
    if orientation_omitted(report):
        for row in plan.get("layups") or []:
            if not isinstance(row, dict):
                continue
            tid = str(row.get("target_segment_id") or "").strip()
            if tid and tid in orientation_targets:
                stamp_typed_skip(
                    row,
                    reason_code="episode_open_native_self_orients",
                    evidence_refs=[
                        f"target:{tid}",
                        "opening_orientation:native_open_self_orients",
                    ],
                    compensating_path="native_self_orients",
                    revisit_if=["orientation_disabled", "opening_slot_freed"],
                    decision_confidence=0.95,
                )
        try:
            ctx.write_json(PLAN_REL, plan, skip_handoff=True)
        except Exception:
            pass
    if ctx.artifact_exists("segments/manifest.json"):
        from interview_mux.gap_framing import avoid_clone_voice_adjacency
        from interview_mux.source_topology import pickup_eligible_speaker_id

        manifest = ctx.read_json("segments/manifest.json")
        segments_by_id = {
            str(segment.get("segment_id")): segment
            for segment in ((manifest if isinstance(manifest, dict) else {}).get("segments") or [])
            if isinstance(segment, dict) and segment.get("segment_id")
        }
        corpus = (
            ctx.read_json(CORPUS_REL) if ctx.artifact_exists(CORPUS_REL) else {}
        )
        report, clone_notes = avoid_clone_voice_adjacency(
            report,
            segments_by_id,
            ordered_segment_ids=ordered,
            clone_speaker_id=pickup_eligible_speaker_id(ctx),
            nugget_corpus=corpus if isinstance(corpus, dict) else {},
        )
        if clone_notes:
            ctx.log(
                f"nugget_layup: adjusted {len(clone_notes)} clone-adjacent VO line(s)",
                level="info",
                stage="nugget_layup_compose",
            )
    if floor_adopt_meta:
        _rmeta = dict(report.get("_meta") or {}) if isinstance(report.get("_meta"), dict) else {}
        _rmeta["rank_to_budget_adopt"] = floor_adopt_meta
        report["_meta"] = _rmeta
        try:
            if isinstance(plan, dict) and (
                floor_adopt_meta.get("notes") or floor_adopt_meta.get("active_after") is not None
            ):
                ctx.write_json(PLAN_REL, plan, skip_handoff=True, stage_key="nugget_layup_compose")
        except Exception:
            pass
    write_validated_artifact(
        ctx,
        GAP_REL,
        report,
        merge_from_disk=False,
        stage_key="nugget_layup_compose",
    )
    # Hard-freeze: write_validated_artifact may skip-write. Land via End-A so
    # narrative remutate recompose can republish authority (exec_13177).
    try:
        from interview_mux.seat_authority import (
            freeze_active,
            persist_frozen_seat_doc,
        )

        if freeze_active(ctx):
            persist_frozen_seat_doc(
                ctx,
                GAP_REL,
                report,
                reason="nugget_layup_gap_publish",
                stage_key="nugget_layup_compose",
                skip_handoff=True,
            )
    except Exception as freeze_exc:
        ctx.log(
            f"nugget_layup gap End-A persist: {freeze_exc}",
            level="warning",
            stage="nugget_layup_compose",
        )
    try:
        from interview_mux.omit_ledger import rebuild_and_write_omit_ledger

        rebuild_and_write_omit_ledger(ctx, plan=plan, gap_report=report)
    except Exception as exc:
        ctx.log(
            f"omit_ledger rebuild failed (non-fatal): {exc}",
            level="warning",
            stage="nugget_layup_compose",
        )
    return report


def waive_nuggets_for_skipped_vo_lines(
    ctx: RunContext,
    *,
    skipped_line_ids: list[str],
    gap_report: dict[str, Any] | None = None,
) -> list[str]:
    """Discharge nuggets tied to operator-skipped VO (G1 skip-optional with force).

    When the operator explicitly skips synthesis, surfaced nuggets on those lines
    are waived — they will not block layup QC or downstream delivery.
    """
    skip_set = {str(x) for x in skipped_line_ids if x}
    if not skip_set:
        return []
    if gap_report is None:
        gap_report = (
            ctx.read_json(GAP_REL) if ctx.artifact_exists(GAP_REL) else {}
        )
    if not isinstance(gap_report, dict):
        return []
    waived: list[str] = []
    for line in gap_report.get("interviewer_lines") or []:
        if not isinstance(line, dict):
            continue
        lid = str(line.get("line_id") or line.get("targets_segment_id") or "")
        if lid not in skip_set:
            continue
        for nid in row_nugget_ids(line):
            if nid and nid not in waived:
                waived.append(nid)
    if not waived:
        return []
    plan: dict[str, Any] = {}
    if ctx.artifact_exists(PLAN_REL):
        raw = ctx.read_json(PLAN_REL)
        if isinstance(raw, dict):
            plan = raw
    prev = [
        str(x.get("nugget_id") or x)
        for x in (plan.get("waived_nugget_ids") or [])
        if x
    ]
    merged = list(dict.fromkeys([*prev, *waived]))
    plan["waived_nugget_ids"] = [{"nugget_id": nid} for nid in merged]
    open_high = [
        str(x)
        for x in (plan.get("open_high_salience_nugget_ids") or [])
        if str(x) not in set(waived)
    ]
    plan["open_high_salience_nugget_ids"] = open_high
    orient = [
        str(x)
        for x in (plan.get("orientation_nugget_recovery_ids") or [])
        if str(x) not in set(waived)
    ]
    plan["orientation_nugget_recovery_ids"] = orient
    ctx.write_json(PLAN_REL, plan, skip_handoff=True)
    ctx.log(
        f"G1 skip: waived {len(waived)} nugget(s) on skipped VO lines",
        level="info",
        stage="g1_vo_pickup",
        detail={"waived_nugget_ids": waived[:12]},
    )
    return waived


def gap_has_layup_before(gap_report: dict[str, Any] | None, segment_id: str) -> bool:
    """True when a contentful before-VO (layup or framing) targets this segment."""
    if not isinstance(gap_report, dict) or not segment_id:
        return False
    for ln in gap_report.get("interviewer_lines") or []:
        if not isinstance(ln, dict):
            continue
        if ln.get("skipped_optional"):
            continue
        if str(ln.get("placement") or "") != "before":
            continue
        if str(ln.get("targets_segment_id") or "") != segment_id:
            continue
        if not str(ln.get("text") or "").strip():
            continue
        delivery = str(ln.get("delivery") or "").lower()
        if delivery and delivery not in {"record", "synthesize"}:
            continue
        # Orientation is not a per-native layup substitute for seam suppression
        # when it targets a different open segment — still counts for its target.
        return True
    return False


def gap_report_has_layup_authority(gap_report: dict[str, Any] | None) -> bool:
    """True when the published gap_report is owned by the Nugget Layup System."""
    if not isinstance(gap_report, dict):
        return False
    if not nugget_layup_enabled():
        return False
    if not nugget_layup_cfg().get("authoritative_gap_report", True):
        return False
    return bool(gap_report.get("nugget_layup_authority"))


def _body_lines(gap_report: dict[str, Any]) -> list[dict[str, Any]]:
    from interview_mux.opening_orientation import is_episode_orientation

    return [
        ln
        for ln in (gap_report.get("interviewer_lines") or [])
        if isinstance(ln, dict) and not is_episode_orientation(ln)
    ]


def lint_gap_report_layup_authority(
    ctx: RunContext,
    gap_report: dict[str, Any] | None = None,
) -> list[str]:
    """Authority lint: only lay-up copy in the body, at coverage, with no canned air."""
    if gap_report is None:
        gap_report = ctx.read_json(GAP_REL) if ctx.artifact_exists(GAP_REL) else {}
    if not gap_report_has_layup_authority(gap_report if isinstance(gap_report, dict) else None):
        return []
    cfg = nugget_layup_cfg()
    report = gap_report if isinstance(gap_report, dict) else {}
    body = _body_lines(report)
    errors: list[str] = []

    # The ``before`` slot is the lay-up slot; other placements may still carry
    # deterministic density seeds.
    foreign = sorted(
        {
            str(ln.get("origin") or "unknown")
            for ln in body
            if str(ln.get("placement") or "") == "before"
            and str(ln.get("origin") or "unknown") not in AUTHORITY_BODY_ORIGINS
        }
    )
    if foreign:
        errors.append(
            "gap_report before-VO written outside the layup publish path "
            f"(origins={foreign[:6]})"
        )

    ordered = _ordered_ids(ctx)
    exempt = coverage_exempt_target_ids(ctx)
    covered = {
        str(ln.get("targets_segment_id") or "")
        for ln in body
        if str(ln.get("placement") or "") == "before"
        and str(ln.get("text") or "").strip()
        and (
            str(ln.get("origin") or "") in AUTHORITY_BODY_ORIGINS
            or str(ln.get("line_id") or "").startswith("vo_layup_")
        )
    }
    eligible = [sid for sid in ordered if sid not in exempt]
    if eligible:
        coverage = len(covered & set(eligible)) / len(eligible)
        floor = float(cfg["min_layup_coverage"])
        if coverage + 1e-9 < floor:
            try:
                from interview_mux.floor_progress import layup_coverage_aspirational

                if layup_coverage_aspirational(ctx):
                    # Advisory only — authority lint must not hard-fail coverage.
                    pass
                else:
                    errors.append(
                        f"gap_report layup coverage={coverage:.3f} below min_layup_coverage={floor}"
                    )
            except Exception:
                errors.append(
                    f"gap_report layup coverage={coverage:.3f} below min_layup_coverage={floor}"
                )

    if cfg["ban_canned_air"]:
        for ln in body:
            violations = canned_air_violations(str(ln.get("text") or ""))
            if violations:
                errors.append(
                    f"canned air on {ln.get('line_id') or ln.get('targets_segment_id')}: "
                    + "; ".join(violations[:2])
                )

    if cfg["unique_nuggets_across_layups"]:
        owner: dict[str, str] = {}
        for ln in body:
            lid = str(ln.get("line_id") or ln.get("targets_segment_id") or "")
            for nid in [str(x) for x in (ln.get("nugget_ids") or []) if x]:
                if nid in owner and owner[nid] != lid:
                    errors.append(f"nugget {nid} claimed by {owner[nid]} and {lid}")
                else:
                    owner.setdefault(nid, lid)
    return list(dict.fromkeys(errors))


def assert_gap_report_layup_authority(
    ctx: RunContext,
    gap_report: dict[str, Any] | None = None,
    *,
    stage: str = "nugget_layup_compose",
) -> None:
    errors = lint_gap_report_layup_authority(ctx, gap_report)
    if not errors:
        return
    from interview_mux.loud_fail import raise_loud_failure

    raise_loud_failure(
        ctx,
        "Nugget layup authority violated in gap_report: " + "; ".join(errors[:4]),
        stage=stage,
        reason="nugget_layup_authority_violated",
        detail={"errors": errors[:8]},
    )


def restore_layup_lines(
    ctx: RunContext,
    gap_report: dict[str, Any] | None,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Re-inject lay-up before-VO that another writer dropped from the body.

    Under authority the publish path owns body ``interviewer_lines``; a recompose
    or rebase that rewrites the artifact must not be able to wipe recovery copy.
    """
    report = dict(gap_report) if isinstance(gap_report, dict) else {}
    if not nugget_layup_enabled() or not nugget_layup_cfg().get("authoritative_gap_report", True):
        return report, []
    if not ctx.artifact_exists(PLAN_REL):
        return report, []
    plan = ctx.read_json(PLAN_REL)
    if not isinstance(plan, dict) or not plan.get("layups"):
        return report, []
    if layup_freshness_errors(ctx, plan):
        return report, []

    lines = [ln for ln in (report.get("interviewer_lines") or []) if isinstance(ln, dict)]
    have = {
        str(ln.get("targets_segment_id") or "")
        for ln in lines
        if str(ln.get("origin") or "") == "nugget_layup"
        and str(ln.get("text") or "").strip()
    }
    seen_texts = [
        str(ln.get("text") or "")
        for ln in lines
        if str(ln.get("text") or "").strip() and not ln.get("skipped_optional")
    ]
    ordered = set(_ordered_ids(ctx))
    opening_targets = _opening_owned_targets(ctx)
    restored: list[dict[str, Any]] = []
    from interview_mux.spoken_copy_guard import spoken_copy_violations

    for row in plan.get("layups") or []:
        line = layup_line_from_row(row if isinstance(row, dict) else {})
        if not line:
            continue
        tid = str(line["targets_segment_id"])
        if tid in opening_targets:
            continue
        if isinstance(row, dict) and is_justified_skip_row(row, soft_migrate=True):
            continue
        if tid in have or (ordered and tid not in ordered):
            continue
        text = str(line.get("text") or "").strip()
        if spoken_copy_violations(text, evidence={}, seen_texts=seen_texts):
            continue
        have.add(tid)
        lines.append(line)
        seen_texts.append(text)
        restored.append({"action": "restore_nugget_layup_line", "line_id": line["line_id"]})
    if not restored:
        return report, []
    report["interviewer_lines"] = lines
    report["nugget_layup_authority"] = True
    return report, restored


def normalize_layup_talking_point_ledger(
    ctx: RunContext, plan: dict[str, Any]
) -> dict[str, Any]:
    """Reconcile discharged/open talking-point ids before QC.

    LLMs often leave should_keep ids in ``open_talking_point_ids`` or list the
    same must_keep id in both open and discharged. Layup attachments win.
    """
    out = dict(plan) if isinstance(plan, dict) else {}
    layups = [r for r in (out.get("layups") or []) if isinstance(r, dict)]
    layup_tps = {
        str(x)
        for row in layups
        for x in (row.get("talking_point_ids") or [])
        if x
    }
    discharged = {
        str(x) for x in (out.get("discharged_talking_point_ids") or []) if x
    } | layup_tps
    must_keep_ids: set[str] = set()
    if ctx.artifact_exists("understanding/talking_points.json"):
        tp_doc = ctx.read_json("understanding/talking_points.json")
        for tp in (tp_doc.get("talking_points") or []) if isinstance(tp_doc, dict) else []:
            if not isinstance(tp, dict):
                continue
            if str(tp.get("importance") or "") != "must_keep":
                continue
            tpid = str(tp.get("talking_point_id") or "")
            if tpid:
                must_keep_ids.add(tpid)
    open_ids = []
    seen: set[str] = set()
    for raw in out.get("open_talking_point_ids") or []:
        tpid = str(raw or "")
        if not tpid or tpid in seen:
            continue
        seen.add(tpid)
        if tpid in must_keep_ids and tpid not in discharged:
            open_ids.append(tpid)
    for tpid in sorted(must_keep_ids):
        if tpid not in discharged and tpid not in seen:
            open_ids.append(tpid)
            seen.add(tpid)
    out["discharged_talking_point_ids"] = sorted(discharged)
    out["open_talking_point_ids"] = open_ids
    return out


def _talking_point_evidence_text(tp: dict[str, Any]) -> str:
    parts = [str(tp.get("title") or ""), str(tp.get("why_it_matters") or "")]
    for quote in tp.get("evidence_quotes") or []:
        parts.append(str(quote or ""))
    return " ".join(parts)


def _text_covers_talking_point(text: str, tp: dict[str, Any]) -> bool:
    hay = _norm(text).casefold()
    if not hay:
        return False
    for quote in tp.get("evidence_quotes") or []:
        qn = _norm(str(quote or "")).casefold()
        if len(qn) >= 6 and qn in hay:
            return True
    title = _norm(str(tp.get("title") or "")).casefold()
    if len(title) >= 8 and title in hay:
        return True
    return _overlap(_tokens(hay), _tokens(_talking_point_evidence_text(tp))) >= 0.35


def _unskip_row_with_nuggets(
    row: dict[str, Any],
    nids: list[str],
    nug_by_id: dict[str, dict[str, Any]],
    *,
    target_text: str = "",
) -> None:
    """Turn a skip (or empty) row into aired copy from nugget claims + a short cue."""
    ids = list(dict.fromkeys([*row_nugget_ids(row), *nids]))
    row["nugget_ids"] = ids
    row["selected_nugget_ids"] = ids
    setup = str(row.get("setup_from_nuggets") or "").strip()
    bits = [_nugget_spoken_claim(nug_by_id.get(nid)) for nid in ids]
    bits = [b.rstrip(".") + "." for b in bits if b]
    body = setup or " ".join(bits[:2])
    if not str(row.get("setup_from_nuggets") or "").strip() and bits:
        row["setup_from_nuggets"] = " ".join(bits[:2])
    unlock = derive_forward_unlock(row, target_text=target_text)
    if unlock and canned_air_violations(unlock):
        unlock = ""
    text = " ".join(p for p in (body, unlock) if p).strip()
    text = " ".join(text.split())
    if text and text[-1:] not in ".!?":
        text = text.rstrip(".") + "."
        if unlock:
            text = f"{body.rstrip('.!?')}. {unlock}".strip()
            text = " ".join(text.split())
    row["skip"] = False
    row.pop("skip_reason_code", None)
    row.pop("compensating_path", None)
    row["text"] = text
    row["word_count"] = _word_count(text)
    row["forward_cue_ok"] = True
    row["recovered_open_high_salience"] = True
    if not str(row.get("target_beat") or "").strip() and target_text:
        row["target_beat"] = _clip_text(target_text, 180)
    if not str(row.get("listener_need_entering_T") or "").strip():
        row["listener_need_entering_T"] = "Recover unaired high-salience fact before this beat."
    if not str(row.get("forward_unlock") or "").strip() and unlock:
        row["forward_unlock"] = unlock
    if not str(row.get("line_id") or "").strip():
        tid = str(row.get("target_segment_id") or "").strip()
        if tid:
            row["line_id"] = f"vo_layup_{tid}"


def recover_open_high_salience_nuggets(
    ctx: RunContext,
    plan: dict[str, Any],
) -> tuple[dict[str, Any], list[str]]:
    """Air or retarget leftover high/critical nuggets even under sparse_omit.

    Skip rows do not discharge. Attach to an existing aired layup when possible,
    otherwise unskip the best native (prefer non-clone speaker) and materialize
    from corpus claims.
    """
    out = dict(plan) if isinstance(plan, dict) else {}
    corpus = ctx.read_json(CORPUS_REL) if ctx.artifact_exists(CORPUS_REL) else {}
    corpus = corpus if isinstance(corpus, dict) else {}
    nug_by_id = {
        str(n.get("nugget_id") or ""): n
        for n in (corpus.get("nuggets") or [])
        if isinstance(n, dict) and n.get("nugget_id")
    }
    cfg = nugget_layup_cfg()
    waived = {
        str(x.get("nugget_id") or x)
        for x in (out.get("waived_nugget_ids") or [])
        if isinstance(x, (dict, str))
    }
    aired = aired_nugget_ids(out)
    opening = _opening_owned_targets(ctx)
    try:
        from interview_mux.media_ip_cta import never_touch_segment_ids

        # A row on a never-touch CTA target is skipped again when the plan is
        # prepared for persist, which reopens the nugget after QC looked clean
        # (exec_010: nug_003 -> seg_007, nug_011 -> seg_032, both re-skipped).
        opening = set(opening) | set(never_touch_segment_ids(ctx) or [])
    except Exception:
        pass
    ordered = [str(x) for x in (out.get("ordered_segment_ids") or _ordered_ids(ctx)) if x]
    by_id = {
        str(row.get("segment_id") or ""): row
        for row in _manifest_segments(ctx)
        if isinstance(row, dict) and row.get("segment_id")
    }
    voice = ""
    try:
        from interview_mux.source_topology import pickup_eligible_speaker_id

        voice = str(pickup_eligible_speaker_id(ctx) or "").strip()
    except Exception:
        voice = ""

    open_ids: list[str] = []
    for nug in corpus.get("nuggets") or []:
        if not isinstance(nug, dict):
            continue
        nid = str(nug.get("nugget_id") or "")
        if not nid or nid in aired or nid in waived:
            continue
        if nug.get("already_aired_in_selection"):
            continue
        if str(nug.get("salience") or "") not in {"high", "critical"}:
            continue
        if nug.get("in_selection") and cfg.get("prefer_excluded_nuggets"):
            # Prefer excluded tape; still recover in-selection high if it was listed open.
            listed = nid in {
                str(x) for x in (out.get("open_high_salience_nugget_ids") or []) if x
            }
            if not listed:
                continue
        open_ids.append(nid)

    notes: list[str] = []
    if not open_ids:
        out["open_high_salience_nugget_ids"] = []
        out["discharged_nugget_ids"] = sorted(aired_nugget_ids(out))
        return out, notes

    layups = [r for r in (out.get("layups") or []) if isinstance(r, dict)]
    by_target = {
        str(r.get("target_segment_id") or ""): r
        for r in layups
        if r.get("target_segment_id")
    }

    def _best_target(nid: str, nug: dict[str, Any]) -> str:
        for row in layups:
            held = set(row_nugget_ids(row)) | {
                str(x) for x in (row.get("value_forgone") or []) if x
            }
            tid = str(row.get("target_segment_id") or "")
            if nid in held and tid and tid not in opening:
                return tid
        best_tid, best_score = "", -1.0
        for sid in ordered:
            if sid in opening:
                continue
            text = str((by_id.get(sid) or {}).get("text") or "")
            ranked = rank_open_nuggets_for_target(
                text,
                [nug],
                exclude_ids=set(),
                limit=1,
                prefer_excluded=bool(cfg.get("prefer_excluded_nuggets", True)),
            )
            score = float((ranked[0].get("relevance_to_target") or 0) if ranked else 0)
            score += _salience_weight(nug)
            speaker = str((by_id.get(sid) or {}).get("speaker_id") or "")
            if voice and speaker == voice:
                score -= 0.25
            if score > best_score:
                best_score, best_tid = score, sid
        return best_tid

    remaining: list[str] = []
    for nid in open_ids:
        nug = nug_by_id.get(nid) or {}
        tid = _best_target(nid, nug)
        if not tid:
            remaining.append(nid)
            continue
        row = by_target.get(tid)
        if row is None:
            row = {"target_segment_id": tid, "line_id": f"vo_layup_{tid}", "skip": True}
            layups.append(row)
            by_target[tid] = row
        target_text = str((by_id.get(tid) or {}).get("text") or "")
        if row_is_aired(row):
            ids = list(dict.fromkeys([*row_nugget_ids(row), nid]))
            row["nugget_ids"] = ids
            row["selected_nugget_ids"] = ids
            bit = _nugget_spoken_claim(nug)
            text = str(row.get("text") or "")
            if bit and bit.casefold() not in text.casefold():
                row["text"] = f"{bit.rstrip('.')}. {text}".strip()
                row["word_count"] = _word_count(row["text"])
            notes.append(f"attached:{nid}:{tid}")
            continue
        _unskip_row_with_nuggets(row, [nid], nug_by_id, target_text=target_text)
        if not str(row.get("text") or "").strip():
            remaining.append(nid)
            continue
        notes.append(f"unskipped:{nid}:{tid}")

    orientation_recovery: list[str] = []
    for nid in remaining:
        if nid in aired_nugget_ids(out):
            continue
        orientation_recovery.append(nid)
    if orientation_recovery:
        prev_orient = [
            str(x) for x in (out.get("orientation_nugget_recovery_ids") or []) if x
        ]
        out["orientation_nugget_recovery_ids"] = list(
            dict.fromkeys([*prev_orient, *orientation_recovery])
        )
        for nid in orientation_recovery:
            notes.append(f"orientation_recovery:{nid}")

    out["layups"] = layups
    out["discharged_nugget_ids"] = sorted(aired_nugget_ids(out))
    orient_assigned = {
        str(x) for x in (out.get("orientation_nugget_recovery_ids") or []) if x
    }
    still_open = [
        nid
        for nid in remaining
        if nid not in aired_nugget_ids(out) and nid not in orient_assigned
    ]
    out["open_high_salience_nugget_ids"] = still_open
    return out, notes


def park_open_high_salience_on_orientation(
    ctx: RunContext,
    plan: dict[str, Any],
) -> tuple[dict[str, Any], list[str]]:
    """Last-resort: park unaired high-salience nuggets onto episode orientation.

    Used when body unskip + spoken-copy heal cannot keep corpus claims on-air
    without generic filler. Orientation embed recovers the facts without
    inventing new body hinges.
    """
    out = dict(plan) if isinstance(plan, dict) else {}
    qc = evaluate_layup_qc(ctx, out)
    open_ids = [str(x) for x in (qc.get("open_high_salience_nugget_ids") or []) if x]
    if not open_ids:
        return out, []
    prev = [str(x) for x in (out.get("orientation_nugget_recovery_ids") or []) if x]
    merged = list(dict.fromkeys([*prev, *open_ids]))
    out["orientation_nugget_recovery_ids"] = merged
    out["open_high_salience_nugget_ids"] = []
    notes = [f"orientation_park:{nid}" for nid in open_ids]
    # Keep skip rows' value_forgone so omit ledger / audit can see the trade.
    for row in out.get("layups") or []:
        if not isinstance(row, dict) or not row.get("skip"):
            continue
        held = set(row_nugget_ids(row)) | {
            str(x) for x in (row.get("value_forgone") or []) if x
        }
        park_hit = [nid for nid in open_ids if nid in held]
        if not park_hit:
            continue
        forgone = [str(x) for x in (row.get("value_forgone") or []) if x]
        row["value_forgone"] = list(dict.fromkeys([*forgone, *park_hit]))
    return out, notes


def recover_open_must_keep_talking_points(
    ctx: RunContext,
    plan: dict[str, Any],
) -> tuple[dict[str, Any], list[str]]:
    """Attach or discharge leftover must_keep talking points already on tape.

    LLMs often leave must_keep ids open even when selected natives or existing
    layup copy already carry the thesis. Attach the id onto a matching layup
    row, or discharge when selected native text overlaps title/quotes. Do not
    invent new VO.
    """
    out = dict(plan) if isinstance(plan, dict) else {}
    out = normalize_layup_talking_point_ledger(ctx, out)
    open_ids = [str(x) for x in (out.get("open_talking_point_ids") or []) if x]
    if not open_ids:
        return out, []
    tp_by_id: dict[str, dict[str, Any]] = {}
    if ctx.artifact_exists("understanding/talking_points.json"):
        tp_doc = ctx.read_json("understanding/talking_points.json")
        for row in (tp_doc.get("talking_points") or []) if isinstance(tp_doc, dict) else []:
            if not isinstance(row, dict):
                continue
            tpid = str(row.get("talking_point_id") or "")
            if tpid:
                tp_by_id[tpid] = row
    layups = [r for r in (out.get("layups") or []) if isinstance(r, dict)]
    ordered = [str(x) for x in (out.get("ordered_segment_ids") or _ordered_ids(ctx)) if x]
    ordered_set = set(ordered)
    segs_by_id: dict[str, dict[str, Any]] = {}
    if ctx.artifact_exists("segments/manifest.json"):
        man = ctx.read_json("segments/manifest.json")
        if isinstance(man, dict):
            for row in man.get("segments") or []:
                if isinstance(row, dict) and row.get("segment_id"):
                    segs_by_id[str(row["segment_id"])] = row
    native_blob = " ".join(
        str((segs_by_id.get(sid) or {}).get("text") or "") for sid in ordered
    )
    notes: list[str] = []
    discharged = {str(x) for x in (out.get("discharged_talking_point_ids") or []) if x}
    remaining: list[str] = []
    for tpid in open_ids:
        tp = tp_by_id.get(tpid) or {}
        bound = {str(x) for x in (tp.get("segment_ids") or []) if x} & ordered_set
        attached = False
        for row in layups:
            if row.get("skip"):
                continue
            tid = str(row.get("target_segment_id") or "")
            blob = " ".join(
                str(row.get(k) or "")
                for k in ("text", "forward_unlock", "target_beat", "setup_from_nuggets")
            )
            if tid in bound or _text_covers_talking_point(blob, tp):
                ids = [str(x) for x in (row.get("talking_point_ids") or []) if x]
                if tpid not in ids:
                    ids.append(tpid)
                    row["talking_point_ids"] = ids
                discharged.add(tpid)
                attached = True
                notes.append(f"attached:{tpid}:{tid or 'layup'}")
                break
        if attached:
            continue
        if bound or _text_covers_talking_point(native_blob, tp):
            discharged.add(tpid)
            notes.append(f"already_on_tape:{tpid}")
            continue
        remaining.append(tpid)
    out["layups"] = layups
    out["discharged_talking_point_ids"] = sorted(discharged)
    out["open_talking_point_ids"] = remaining
    return normalize_layup_talking_point_ledger(ctx, out), notes


def evaluate_layup_qc(
    ctx: RunContext,
    plan: dict[str, Any] | None = None,
    corpus: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Coverage / recovery QC for the lay-up plan."""
    cfg = nugget_layup_cfg()
    if plan is None:
        plan = ctx.read_json(PLAN_REL) if ctx.artifact_exists(PLAN_REL) else {}
    if corpus is None:
        corpus = ctx.read_json(CORPUS_REL) if ctx.artifact_exists(CORPUS_REL) else {}
    plan, _ = dedupe_layup_rows_by_target(plan)
    plan = normalize_layup_talking_point_ledger(ctx, plan if isinstance(plan, dict) else {})
    corpus = corpus if isinstance(corpus, dict) else {}

    ordered = [str(x) for x in (plan.get("ordered_segment_ids") or _ordered_ids(ctx)) if x]
    layups = [r for r in (plan.get("layups") or []) if isinstance(r, dict)]
    by_target = {str(r.get("target_segment_id") or ""): r for r in layups if r.get("target_segment_id")}

    present = 0
    skips: list[dict[str, Any]] = []
    missing: list[str] = []
    for sid in ordered:
        row = by_target.get(sid)
        if row is None:
            missing.append(sid)
            continue
        if row.get("skip") or not str(row.get("text") or "").strip():
            skips.append(
                {
                    "segment_id": sid,
                    "skip_reason_code": row.get("skip_reason_code") or "empty_or_skip",
                }
            )
            continue
        present += 1

    eligible = [
        sid
        for sid in ordered
        if sid not in coverage_exempt_target_ids(ctx, plan)
    ]
    coverage = (present / len(eligible)) if eligible else 1.0
    open_must = [str(x) for x in (plan.get("open_talking_point_ids") or []) if x]

    aired = aired_nugget_ids(plan)
    orient_assigned = {
        str(x) for x in (plan.get("orientation_nugget_recovery_ids") or []) if x
    }
    high_in_corpus = _corpus_high_salience_ids(corpus)
    open_high = [
        str(x)
        for x in (plan.get("open_high_salience_nugget_ids") or [])
        if x and str(x) in high_in_corpus and str(x) not in aired
    ]
    for nug in corpus.get("nuggets") or []:
        if not isinstance(nug, dict):
            continue
        if nug.get("already_aired_in_selection"):
            continue
        sal = str(nug.get("salience") or "")
        if sal not in ("high", "critical"):
            continue
        if nug.get("in_selection") and not cfg.get("prefer_excluded_nuggets"):
            continue
        nid = str(nug.get("nugget_id") or "")
        if not nid:
            continue
        discharged_n = {str(x) for x in (plan.get("discharged_nugget_ids") or []) if x}
        waived = {
            str(x.get("nugget_id") or x)
            for x in (plan.get("waived_nugget_ids") or [])
            if isinstance(x, (dict, str))
        }
        assigned = False
        for row in layups:
            if row_is_aired(row) and nid in row_nugget_ids(row):
                assigned = True
                break
        discharged_aired = discharged_n & aired_nugget_ids(plan)
        if not assigned and nid not in discharged_aired and nid not in waived and nid not in open_high:
            if nid in orient_assigned:
                continue
            if not nug.get("in_selection"):
                open_high.append(nid)

    errors: list[str] = []
    warnings: list[str] = []
    coverage_advisory = False
    try:
        from interview_mux.floor_progress import layup_coverage_aspirational

        cov_asp = layup_coverage_aspirational(ctx)
    except Exception:
        cov_asp = True
    if eligible and coverage + 1e-9 < float(cfg["min_layup_coverage"]):
        msg = (
            f"layup_coverage={coverage:.3f} below min_layup_coverage={cfg['min_layup_coverage']}"
        )
        if cov_asp:
            warnings.append(msg)
            coverage_advisory = True
        else:
            errors.append(msg)
    missing_required = [sid for sid in missing if sid not in set(ordered) - set(eligible)]
    if missing_required and cfg.get("require_layup_per_native"):
        if cov_asp:
            warnings.append(f"missing_layup_rows={missing_required[:12]}")
            coverage_advisory = True
        else:
            errors.append(f"missing_layup_rows={missing_required[:12]}")
    if open_must and cfg.get("block_on_open_must_keep"):
        errors.append(f"open_must_keep_talking_points={open_must[:12]}")
    if open_high and cfg.get("block_on_open_high_salience"):
        errors.append(f"open_high_salience_nuggets={open_high[:12]}")

    craft = evaluate_layup_craft(ctx, layups, cfg=cfg)
    errors.extend(craft["errors"])

    # NLC-B2 / Workstream B: ``min_nugget_air_coverage`` (0.85) is aspirational when
    # ``air_coverage_aspirational`` is true — under-goal coverage is advisory.
    # Structural refuse: unaccounted open high-salience (above) or catastrophic floor.
    # Orientation-parked nuggets are recovered via episode orientation embed —
    # credit them as intro air so park can satisfy coverage math (otherwise
    # recover→park↔recompose hash-oscillates at a discrete 10/12=0.833 floor miss).
    orient_ids = sorted(orient_assigned)
    nugget_cov = evaluate_nugget_air_coverage(plan, orient_ids, None, corpus, hard=True)
    warnings.extend(list(craft.get("warnings") or []))
    warnings.extend(nugget_cov.get("warnings") or [])
    errors.extend(nugget_cov.get("errors") or [])
    if nugget_cov.get("warnings") and nugget_cov.get("air_coverage_aspirational"):
        # Surface goal-miss as an advisory flag for G-Publish / pick-best ledger.
        warnings.append("nugget_air_coverage_aspirational_goal_miss")

    return {
        "version": 1,
        "ordered_count": len(ordered),
        "layup_present_count": present,
        "layup_coverage": round(coverage, 4),
        "nugget_air_coverage": nugget_cov.get("nugget_air_coverage"),
        "open_nugget_ids": nugget_cov.get("open_nugget_ids") or [],
        "skips": skips,
        "missing_targets": missing,
        "open_must_keep_talking_point_ids": open_must,
        "open_high_salience_nugget_ids": open_high,
        "canned_air_lines": craft["canned_air_lines"],
        "insufficient_analysis_targets": craft["insufficient_analysis_targets"],
        "duplicate_nugget_ids": craft["duplicate_nugget_ids"],
        "air_coverage_aspirational": nugget_cov.get("air_coverage_aspirational"),
        "air_coverage_advisory": bool(
            nugget_cov.get("air_coverage_aspirational")
            and any("min_nugget_air_coverage" in str(w) for w in (nugget_cov.get("warnings") or []))
        ),
        "layup_coverage_advisory": coverage_advisory,
        "warnings": warnings,
        "errors": errors,
        "ok": not errors,
    }


def evaluate_layup_craft(
    ctx: RunContext,
    layups: list[dict[str, Any]],
    *,
    cfg: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Per-line construction QC: analysis depth, canned air, restates, uniqueness."""
    settings = cfg or nugget_layup_cfg()
    deg = settings.get("degraded_layup") if isinstance(settings.get("degraded_layup"), dict) else {}
    by_id = {
        str(r.get("segment_id")): r
        for r in _manifest_segments(ctx)
        if r.get("segment_id")
    }
    masks = {}
    if ctx.artifact_exists(MASKS_REL):
        try:
            loaded = ctx.read_json(MASKS_REL)
            if isinstance(loaded, dict):
                masks = loaded
        except Exception:
            masks = {}
    errors: list[str] = []
    warnings: list[str] = []
    canned_lines: list[dict[str, Any]] = []
    thin: list[str] = []
    duplicates: list[str] = []
    invented: list[str] = []
    owner: dict[str, str] = {}
    texts: list[tuple[str, set[str]]] = []
    seen_air_texts: list[str] = []
    grace_floor = int(deg.get("grace_min_layup_words") or 12)

    for row in layups:
        tid = str(row.get("target_segment_id") or "")
        text = _norm(str(row.get("text") or ""))
        if row.get("skip") or not text:
            continue
        mask = _mask_for(masks, tid)
        degraded = is_degraded_target(mask) or bool(row.get("degraded_lexicon_island"))
        if settings["require_analysis_fields"]:
            required = ("target_beat",) if degraded else REQUIRED_ANALYSIS_FIELDS
            absent = [f for f in required if not str(row.get(f) or "").strip()]
            soft_absent = []
            if degraded:
                soft_absent = [
                    f for f in REQUIRED_ANALYSIS_FIELDS
                    if f not in required and not str(row.get(f) or "").strip()
                ]
            if absent:
                thin.append(tid)
                errors.append(f"insufficient_analysis[{tid}]: missing {','.join(absent)}")
            elif soft_absent:
                warnings.append(f"soft_analysis[{tid}]: missing {','.join(soft_absent)}")
        if settings["ban_canned_air"]:
            violations = canned_air_violations(text)
            if violations:
                canned_lines.append({"target_segment_id": tid, "violations": violations})
                errors.append(f"canned_air[{tid}]: " + "; ".join(violations[:2]))
        try:
            from interview_mux.media_ip_cta import air_overlaps_never_touch

            if air_overlaps_never_touch(ctx, text):
                errors.append(f"never_touch_cta[{tid}]: lay-up reuses dropped CTA wording")
        except Exception:
            pass
        from interview_mux.spoken_copy_guard import spoken_copy_violations

        target_text = str(
            mask.get("comprehensible_text")
            or (by_id.get(tid) or {}).get("text")
            or ""
        )
        copy_hits = spoken_copy_violations(
            text,
            evidence={
                "target_excerpt": target_text,
                "before_excerpt": str(row.get("listener_need_entering_T") or ""),
                "after_topic": str(row.get("target_beat") or ""),
                "strict_grounding": False,
            },
            seen_texts=seen_air_texts,
        )
        if copy_hits:
            errors.append(f"spoken_copy[{tid}]: " + ", ".join(copy_hits[:3]))
        # Invented island claim: air text contains bracket placeholders / "unclear audio".
        low = text.casefold()
        if "[unclear" in low or "unclear audio" in low or "garbled" in low:
            invented.append(tid)
            errors.append(f"invented_island_claim[{tid}]: air text must not narrate unclear tokens")
        words = _word_count(text)
        # Grace floor only on degraded targets (clear targets keep the historical
        # compose budget without a hard craft fail on short fixtures).
        if degraded and words < grace_floor:
            errors.append(f"thin_layup[{tid}]: words={words} below grace_floor={grace_floor}")
        if target_text:
            restate = _overlap(_tokens(text), _tokens(target_text))
            if restate >= float(settings["max_target_restate_overlap"]):
                corpus = {}
                if ctx.artifact_exists(CORPUS_REL):
                    loaded = ctx.read_json(CORPUS_REL)
                    corpus = loaded if isinstance(loaded, dict) else {}
                if not _nugget_preview_ok(
                    row, text, target_text, overlap=restate, corpus=corpus
                ):
                    errors.append(f"restates_target[{tid}]: overlap={restate:.2f}")
        for nid in row_nugget_ids(row):
            if nid in owner and owner[nid] != tid:
                duplicates.append(nid)
                warnings.append(
                    f"duplicate_nugget[{nid}]: claimed by {owner[nid]} and {tid}"
                )
                if settings["unique_nuggets_across_layups"]:
                    errors.append(
                        f"duplicate_nugget[{nid}]: claimed by {owner[nid]} and {tid}"
                    )
            else:
                owner.setdefault(nid, tid)
        tokens = _tokens(text)
        for other_tid, other_tokens in texts:
            overlap = _overlap(tokens, other_tokens)
            if overlap >= float(settings["max_cross_layup_overlap"]):
                errors.append(
                    f"cross_layup_overlap[{other_tid}->{tid}]: overlap={overlap:.2f}"
                )
        texts.append((tid, tokens))
        seen_air_texts.append(text)

    return {
        "errors": list(dict.fromkeys(errors)),
        "warnings": list(dict.fromkeys(warnings)),
        "canned_air_lines": canned_lines,
        "insufficient_analysis_targets": sorted(set(thin)),
        "duplicate_nugget_ids": sorted(set(duplicates)),
        "invented_island_targets": sorted(set(invented)),
    }


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def air_coverage_aspirational_enabled(cfg: dict[str, Any] | None = None) -> bool:
    return bool(nugget_layup_cfg(cfg).get("air_coverage_aspirational", True))


def air_coverage_max_attempts(cfg: dict[str, Any] | None = None) -> int:
    """S4: hard-cap at 2 (best-of-≤2); ignore higher config."""
    try:
        return max(1, min(2, int(nugget_layup_cfg(cfg).get("air_coverage_max_attempts") or 2)))
    except (TypeError, ValueError):
        return 2


def _plan_content_hash(plan: dict[str, Any]) -> str:
    payload = json.dumps(plan, sort_keys=True, default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def _layup_candidate_rank(qc: dict[str, Any]) -> tuple[float, int, int, float]:
    """Higher is better: coverage, then fewer open_high, fewer craft errors, layup_coverage."""
    coverage = float(qc.get("nugget_air_coverage") or 0.0)
    open_high = len(qc.get("open_high_salience_nugget_ids") or [])
    craft_errs = sum(
        1
        for e in (qc.get("errors") or [])
        if "open_high_salience" not in str(e) and "nugget_air_coverage" not in str(e)
    )
    layup_cov = float(qc.get("layup_coverage") or 0.0)
    return (coverage, -open_high, -craft_errs, layup_cov)


def load_layup_candidates_doc(ctx: RunContext) -> dict[str, Any]:
    if not ctx.artifact_exists(LAYUP_CANDIDATES_REL):
        return {"version": 1, "attempts": 0, "candidates": []}
    try:
        doc = ctx.read_json(LAYUP_CANDIDATES_REL)
        return doc if isinstance(doc, dict) else {"version": 1, "attempts": 0, "candidates": []}
    except Exception:
        return {"version": 1, "attempts": 0, "candidates": []}


def layup_air_attempts_exhausted(ctx: RunContext) -> bool:
    doc = load_layup_candidates_doc(ctx)
    return int(doc.get("attempts") or 0) >= air_coverage_max_attempts()


def register_layup_candidate(
    ctx: RunContext,
    *,
    plan: dict[str, Any] | None = None,
    qc: dict[str, Any] | None = None,
    label: str | None = None,
) -> dict[str, Any]:
    """Archive a layup plan snapshot (S4: keep at most 2 candidates)."""
    if plan is None:
        plan = ctx.read_json(PLAN_REL) if ctx.artifact_exists(PLAN_REL) else {}
    plan = plan if isinstance(plan, dict) else {}
    if qc is None:
        qc = evaluate_layup_qc(ctx, plan)
    qc = qc if isinstance(qc, dict) else {}
    doc = load_layup_candidates_doc(ctx)
    candidates = list(doc.get("candidates") or []) if isinstance(doc.get("candidates"), list) else []
    attempts = int(doc.get("attempts") or 0) + 1
    attempt_id = (
        f"layup_{attempts}_{datetime.now(timezone.utc).strftime('%H%M%S')}_"
        f"{_plan_content_hash(plan)}"
    )
    archive_rel = f"{LAYUP_CANDIDATES_ARCHIVE}/{attempt_id}"
    archive_dir = Path(ctx.run_dir) / archive_rel
    archive_dir.mkdir(parents=True, exist_ok=True)
    plan_path = archive_dir / "nugget_layup_plan.json"
    plan_path.write_text(json.dumps(plan, indent=2, default=str), encoding="utf-8")
    qc_path = archive_dir / "nugget_layup_qc.json"
    qc_path.write_text(json.dumps(qc, indent=2, default=str), encoding="utf-8")
    rank = _layup_candidate_rank(qc)
    open_high = [str(x) for x in (qc.get("open_high_salience_nugget_ids") or []) if x]
    catastrophic = float(nugget_layup_cfg().get("catastrophic_nugget_air_coverage") or 0.0)
    coverage = float(qc.get("nugget_air_coverage") or 0.0)
    catastrophic_ok = (not open_high) and (
        catastrophic <= 0 or coverage + 1e-9 >= catastrophic
    )
    entry = {
        "attempt_id": attempt_id,
        "label": label or "layup",
        "registered_at": _now_iso(),
        "plan_hash": _plan_content_hash(plan),
        "nugget_air_coverage": coverage,
        "layup_coverage": qc.get("layup_coverage"),
        "open_high_salience_nugget_ids": open_high,
        "orientation_nugget_recovery_ids": list(
            plan.get("orientation_nugget_recovery_ids") or []
        ),
        "craft_error_count": -rank[2],
        "rank_tuple": list(rank),
        "rank_score": coverage,
        "archive_rel": archive_rel,
        "catastrophic_ok": catastrophic_ok,
        "qc_ok": bool(qc.get("ok")),
        "air_coverage_advisory": bool(qc.get("air_coverage_advisory")),
    }
    candidates.append(entry)
    doc["attempts"] = attempts
    # S4: retain only the last 2 attempts (best-of-≤2).
    doc["candidates"] = candidates[-2:]
    doc["updated_at"] = _now_iso()
    ctx.write_json(LAYUP_CANDIDATES_REL, doc, skip_handoff=True, stage_key="nugget_layup_compose")
    return entry


def select_best_layup_candidate(ctx: RunContext) -> dict[str, Any] | None:
    doc = load_layup_candidates_doc(ctx)
    candidates = [c for c in (doc.get("candidates") or []) if isinstance(c, dict)]
    if not candidates:
        return None
    viable = [c for c in candidates if c.get("catastrophic_ok")]
    pool = viable or candidates
    return max(
        pool,
        key=lambda c: tuple(c.get("rank_tuple") or (float(c.get("rank_score") or 0.0),)),
    )


def apply_best_layup_candidate(ctx: RunContext) -> dict[str, Any]:
    """Restore best of ≤2 archived plans as the live layup plan (S4)."""
    best = select_best_layup_candidate(ctx)
    if not best:
        return {"ok": False, "reason": "no_candidates"}
    archive_rel = str(best.get("archive_rel") or "")
    archive_dir = Path(ctx.run_dir) / archive_rel
    plan_src = archive_dir / "nugget_layup_plan.json"
    if not plan_src.is_file():
        return {"ok": False, "reason": "archive_missing", "attempt_id": best.get("attempt_id")}
    try:
        plan = json.loads(plan_src.read_text(encoding="utf-8"))
    except Exception:
        return {"ok": False, "reason": "archive_unreadable", "attempt_id": best.get("attempt_id")}
    if not isinstance(plan, dict):
        return {"ok": False, "reason": "archive_invalid", "attempt_id": best.get("attempt_id")}
    # Clear mid-shard stamps so S1 publish guard allows the restore.
    meta = dict(plan.get("_meta") or {}) if isinstance(plan.get("_meta"), dict) else {}
    meta.pop("compose_shards_pending", None)
    meta.pop("compose_qc_pending", None)
    plan["_meta"] = meta
    ctx.write_json(PLAN_REL, plan, stage_key="nugget_layup_compose")
    try:
        commit_layup_gap_authority(ctx, plan)
    except Exception:
        pass
    record_layup_air_advisories(
        ctx,
        gate_id="layup_air_pick_best",
        detail={
            "picked_attempt_id": best.get("attempt_id"),
            "nugget_air_coverage": best.get("nugget_air_coverage"),
            "plan_hash": best.get("plan_hash"),
            "catastrophic_ok": best.get("catastrophic_ok"),
        },
        aspirational_proceeded=True,
    )
    ctx.log(
        f"nugget_layup: applied best air-coverage candidate {best.get('attempt_id')} "
        f"coverage={best.get('nugget_air_coverage')}",
        level="warning",
        stage="nugget_layup_compose",
    )
    return {"ok": True, "candidate": best}


def record_layup_air_advisories(
    ctx: RunContext,
    *,
    gate_id: str,
    detail: dict[str, Any] | None = None,
    aspirational_proceeded: bool = False,
) -> None:
    entry = {
        "gate_id": gate_id,
        "detail": dict(detail or {}),
        "at": _now_iso(),
        "aspirational_proceeded": bool(aspirational_proceeded),
    }

    def patch(meta: dict[str, Any]) -> None:
        advisories = list(meta.get(LAYUP_AIR_ADVISORIES_META_KEY) or [])
        advisories.append(entry)
        meta[LAYUP_AIR_ADVISORIES_META_KEY] = advisories[-20:]
        if aspirational_proceeded:
            meta["layup_air_aspirational_proceeded"] = True
        qc = meta.get("qc_summaries") if isinstance(meta.get("qc_summaries"), dict) else {}
        qc["nugget_air_aspirational"] = {
            "enabled": air_coverage_aspirational_enabled(),
            "aspirational_proceeded": bool(meta.get("layup_air_aspirational_proceeded")),
            "advisory_count": len(advisories),
        }
        meta["qc_summaries"] = qc

    try:
        ctx.mutate_run_meta(patch)
    except Exception:
        pass
    try:
        from interview_mux.floor_progress import record_floor_advisory

        record_floor_advisory(
            ctx,
            gate_id=str(gate_id),
            detail=dict(detail or {}),
            aspirational_proceeded=aspirational_proceeded,
            mirror_quality=True,
        )
    except Exception:
        pass


def try_pick_best_layup_on_oscillation(ctx: RunContext) -> dict[str, Any]:
    """S4: oscillation thrash hook retired — best-of-≤2 lives in compose QC only."""
    return {"ok": False, "reason": "oscillation_pick_disabled_s4"}


#: Row-level lay-up QC findings, written as ``kind[target_segment_id]: ...``.
#: A row with one becomes a typed skip instead of failing the stage (ISSUES 185).
_ROW_QC_KINDS = frozenset(
    {
        "insufficient_analysis",
        "canned_air",
        "never_touch_cta",
        "spoken_copy",
        "invented_island_claim",
        "thin_layup",
        "restates_target",
    }
)


def skip_rows_failing_layup_qc(ctx: RunContext, qc: dict[str, Any]) -> int:
    """Stamp a typed skip on every lay-up row a row-level QC finding names.

    Lay-up air is optional; a row whose copy is canned, thin, ungrounded or
    reuses CTA wording is dropped from air with a justified reason instead of
    stopping nugget_layup_compose (144, 157, 135, 171 were this stop).
    """
    import re

    targets: dict[str, str] = {}
    for err in qc.get("errors") or []:
        m = re.match(r"^([a-z_]+)\[([^\]]+)\]", str(err))
        if m and m.group(1) in _ROW_QC_KINDS:
            targets.setdefault(m.group(2), m.group(1))
    if not targets or not ctx.artifact_exists(PLAN_REL):
        return 0
    plan = ctx.read_json(PLAN_REL)
    if not isinstance(plan, dict):
        return 0
    stamped = 0
    for row in plan.get("layups") or []:
        if not isinstance(row, dict) or row.get("skip"):
            continue
        tid = str(row.get("target_segment_id") or "")
        kind = targets.get(tid)
        if not kind:
            continue
        stamp_typed_skip(
            row,
            reason_code="never_touch_cta" if kind == "never_touch_cta" else "spoken_copy_unhealable",
            evidence_refs=[f"layup_qc:{kind}"],
            compensating_path="typed_skip",
        )
        stamped += 1
    if not stamped:
        return 0
    try:
        from interview_mux.artifact_writes import write_validated_artifact

        write_validated_artifact(
            ctx, PLAN_REL, plan, merge_from_disk=False, stage_key="nugget_layup_compose"
        )
    except Exception as exc:
        ctx.log(
            f"nugget_layup: could not persist QC typed skips ({exc}); leaving plan as is",
            level="warning",
            stage="nugget_layup_compose",
        )
        return 0
    ctx.log(
        f"nugget_layup: {stamped} lay-up row(s) failing QC skipped with a typed reason "
        "(advisory): " + ", ".join(f"{k}:{v}" for k, v in list(targets.items())[:8]),
        level="warning",
        stage="nugget_layup_compose",
    )
    return stamped


def assert_layup_qc_or_raise(ctx: RunContext, qc: dict[str, Any]) -> None:
    from interview_mux.artifact_writes import write_validated_artifact

    if not qc.get("ok") and skip_rows_failing_layup_qc(ctx, qc):
        qc = evaluate_layup_qc(ctx)

    # QC artifact may lack a dedicated schema validator — write via ctx.
    try:
        write_validated_artifact(
            ctx,
            QC_REL,
            qc,
            merge_from_disk=False,
            stage_key="nugget_layup_compose",
        )
    except Exception:
        ctx.write_json(QC_REL, qc)

    cfg = nugget_layup_cfg()
    aspirational = bool(cfg.get("air_coverage_aspirational", True))
    plan = ctx.read_json(PLAN_REL) if ctx.artifact_exists(PLAN_REL) else {}
    if aspirational and isinstance(plan, dict) and plan.get("layups") is not None:
        try:
            register_layup_candidate(ctx, plan=plan, qc=qc, label="compose_qc")
        except Exception:
            pass

    if qc.get("ok"):
        if aspirational and qc.get("air_coverage_advisory"):
            record_layup_air_advisories(
                ctx,
                gate_id="nugget_air_coverage_goal_miss",
                detail={
                    "nugget_air_coverage": qc.get("nugget_air_coverage"),
                    "min_nugget_air_coverage": cfg.get("min_nugget_air_coverage"),
                    "open_high_salience_nugget_ids": qc.get("open_high_salience_nugget_ids") or [],
                },
                aspirational_proceeded=True,
            )
            ctx.log(
                "nugget_layup_compose: aspirational air-coverage goal miss "
                f"(coverage={qc.get('nugget_air_coverage')}; "
                f"goal={cfg.get('min_nugget_air_coverage')}) — proceeding with advisory",
                level="warning",
                stage="nugget_layup_compose",
            )
        if qc.get("layup_coverage_advisory"):
            record_layup_air_advisories(
                ctx,
                gate_id="min_layup_coverage",
                detail={
                    "layup_coverage": qc.get("layup_coverage"),
                    "min_layup_coverage": cfg.get("min_layup_coverage"),
                    "warnings": [
                        w
                        for w in (qc.get("warnings") or [])
                        if "layup_coverage" in str(w)
                        or "open_high_salience" in str(w)
                        or "open_must_keep" in str(w)
                        or "missing_layup" in str(w)
                    ][:8],
                },
                aspirational_proceeded=True,
            )
        return

    # Structural hard fail — but if aspirational + attempts exhausted and we have
    # a viable archived candidate (accounted / catastrophic_ok), pick-best instead.
    if aspirational and layup_air_attempts_exhausted(ctx):
        applied = apply_best_layup_candidate(ctx)
        if applied.get("ok"):
            winner_qc = evaluate_layup_qc(ctx)
            try:
                write_validated_artifact(
                    ctx,
                    QC_REL,
                    winner_qc,
                    merge_from_disk=False,
                    stage_key="nugget_layup_compose",
                )
            except Exception:
                ctx.write_json(QC_REL, winner_qc)
            if winner_qc.get("ok"):
                return
    from interview_mux.loud_fail import raise_loud_failure

    raise_loud_failure(
        ctx,
        "Nugget layup QC failed: " + "; ".join(str(e) for e in (qc.get("errors") or [])[:8]),
        stage="nugget_layup_compose",
        reason="nugget_layup_qc_failed",
    )

def materialize_over_skipped_layups(
    ctx: RunContext,
    plan: dict[str, Any] | None = None,
    *,
    target_coverage: float | None = None,
) -> tuple[dict[str, Any], list[str]]:
    """Turn analysis-rich skip rows into spoken before-VO when LLM over-skipped.

    Uses forward_unlock / target_beat / corpus nuggets already attached to the
    row (or open must-keep talking points) so coverage can recover without canned hinges.
    """
    cfg = nugget_layup_cfg()
    floor = float(target_coverage if target_coverage is not None else cfg["min_layup_coverage"])
    notes: list[str] = []
    if plan is None:
        plan = ctx.read_json(PLAN_REL) if ctx.artifact_exists(PLAN_REL) else {}
    plan = normalize_layup_talking_point_ledger(ctx, plan if isinstance(plan, dict) else {})
    layups = [r for r in (plan.get("layups") or []) if isinstance(r, dict)]
    if not layups:
        return plan, ["no_layups"]

    corpus = ctx.read_json(CORPUS_REL) if ctx.artifact_exists(CORPUS_REL) else {}
    nug_by_id = {
        str(n.get("nugget_id") or ""): n
        for n in (corpus.get("nuggets") or [])
        if isinstance(n, dict) and n.get("nugget_id")
    }
    tp_doc = (
        ctx.read_json("understanding/talking_points.json")
        if ctx.artifact_exists("understanding/talking_points.json")
        else {}
    )
    tp_by_id = {
        str(tp.get("id") or tp.get("talking_point_id") or ""): tp
        for tp in (tp_doc.get("talking_points") or [])
        if isinstance(tp, dict)
    }

    ordered = [str(x) for x in (plan.get("ordered_segment_ids") or []) if x]
    by_target = {str(r.get("target_segment_id") or ""): r for r in layups if r.get("target_segment_id")}
    exempt = coverage_exempt_target_ids(ctx, plan)
    present = sum(
        1
        for sid in ordered
        if sid not in exempt
        and (row := by_target.get(sid))
        and not row.get("skip")
        and str(row.get("text") or "").strip()
    )
    eligible_n = max(1, len([sid for sid in ordered if sid not in exempt])) if ordered else 1
    need = max(0, int(math.ceil(floor * eligible_n - 1e-12)) - present)
    open_tps = [str(x) for x in (plan.get("open_talking_point_ids") or []) if x]
    claimed_nugs: set[str] = set()
    for row in layups:
        for nid in row_nugget_ids(row):
            claimed_nugs.add(nid)

    filled = 0
    for row in layups:
        if filled >= need and not open_tps:
            break
        if not row.get("skip") and str(row.get("text") or "").strip():
            continue
        tid = str(row.get("target_segment_id") or "")
        reason = str(row.get("skip_reason_code") or "")
        if is_justified_skip_row(row, soft_migrate=True):
            notes.append(f"preserve_justified_skip:{tid}:{reason or 'typed'}")
            continue
        if reason in {
            "spoken_copy_unhealable",
            "opening_orientation_owns_target",
            "clone_voice_adjacency",
            "merged_clone_adjacency",
            "media_ip_cta_hole",
            "never_touch_cta",
        }:
            notes.append(f"preserve_unhealable_skip:{tid}:{reason}")
            continue
        if reason == "opening_orientation_owns_target":
            notes.append(f"preserve_opening_owned_skip:{tid}")
            continue
        if reason == "episode_open_native_self_orients" and filled >= need:
            continue
        unlock = str(row.get("forward_unlock") or "").strip()
        beat = str(row.get("target_beat") or "").strip()
        setup = str(row.get("setup_from_nuggets") or "").strip()
        assigned_tps = [str(x) for x in (row.get("talking_point_ids") or []) if x]
        if not assigned_tps and open_tps:
            assigned_tps = [open_tps.pop(0)]
            row["talking_point_ids"] = list(
                dict.fromkeys([*(row.get("talking_point_ids") or []), *assigned_tps])
            )
        nug_bits: list[str] = []
        for nid in row_nugget_ids(row):
            nug = nug_by_id.get(nid)
            if not isinstance(nug, dict):
                continue
            bit = str(
                nug.get("text")
                or nug.get("text_claim")
                or nug.get("claim")
                or nug.get("summary")
                or ""
            ).strip()
            if bit:
                nug_bits.append(bit.rstrip(".") + ".")
                claimed_nugs.add(nid)
        if not nug_bits:
            for tpid in assigned_tps:
                tp = tp_by_id.get(tpid) or {}
                bit = str(tp.get("text") or tp.get("point") or tp.get("summary") or "").strip()
                if bit:
                    nug_bits.append(bit.rstrip(".") + ".")
                    break
            if not nug_bits:
                for nug in corpus.get("nuggets") or []:
                    if not isinstance(nug, dict):
                        continue
                    nid = str(nug.get("nugget_id") or "")
                    if not nid or nid in claimed_nugs:
                        continue
                    covers = {
                        str(x)
                        for x in (
                            nug.get("talking_point_ids")
                            or nug.get("covers_talking_point_ids")
                            or []
                        )
                        if x
                    }
                    if assigned_tps and covers.isdisjoint(assigned_tps):
                        continue
                    bit = str(
                        nug.get("text")
                        or nug.get("text_claim")
                        or nug.get("claim")
                        or nug.get("summary")
                        or ""
                    ).strip()
                    if not bit:
                        continue
                    nug_bits.append(bit.rstrip(".") + ".")
                    row["selected_nugget_ids"] = list(
                        dict.fromkeys([*(row.get("selected_nugget_ids") or []), nid])
                    )
                    row["nugget_ids"] = list(dict.fromkeys([*(row.get("nugget_ids") or []), nid]))
                    claimed_nugs.add(nid)
                    break
        if not row_nugget_ids(row) and not nug_bits:
            notes.append(f"skip_no_grounded_nugget:{tid}")
            continue
        # Spoken copy from grounded nuggets/setup only — not analysis-field paste.
        parts = [p for p in (setup, *nug_bits[:2]) if p]
        text = " ".join(parts).strip()
        text = " ".join(text.split())
        if len(text.split()) < int(cfg.get("min_layup_words") or 18):
            if beat and beat.lower() not in text.lower():
                text = f"{text} {beat}".strip() if text else beat
            text = " ".join(text.split())
        if len(text.split()) < 8:
            notes.append(f"skip_unmaterializable:{tid}")
            continue
        probe = dict(row)
        probe["skip"] = False
        probe["text"] = text
        craft = evaluate_layup_craft(ctx, [probe], cfg=cfg)
        if craft.get("errors"):
            notes.append(f"skip_craft_fail:{tid}")
            continue
        # Free-form ending OK — keep grounded nugget/setup body without forcing
        # a forward-unlock question or "Let's hear…" hinge.
        if unlock and not str(row.get("forward_unlock") or "").strip():
            row["forward_unlock"] = unlock
        row["skip"] = False
        row.pop("skip_reason_code", None)
        row["text"] = text
        row["word_count"] = _word_count(text)
        row["forward_cue_ok"] = True
        row["materialized_from_skip"] = True
        filled += 1
        notes.append(f"materialized:{tid}")

    discharged = {str(x) for x in (plan.get("discharged_talking_point_ids") or []) if x}
    for row in layups:
        if row.get("skip") or not str(row.get("text") or "").strip():
            continue
        for tpid in row.get("talking_point_ids") or []:
            if tpid:
                discharged.add(str(tpid))
    plan["discharged_talking_point_ids"] = sorted(discharged)
    plan["layups"] = layups
    plan = normalize_layup_talking_point_ledger(ctx, plan)
    notes.append(f"filled={filled}")
    return plan, notes


# ---------------------------------------------------------------------------
# P1–P8 hardening: QC-before-publish, craft spine, floor hold, gap single-flight
# ---------------------------------------------------------------------------


def stamp_compose_qc_pending(plan: dict[str, Any], *, errors: list[str] | None = None) -> dict[str, Any]:
    """Mark plan so mid-QC failure cannot hollow-complete (mirrors NLC-B1 shards)."""
    out = dict(plan) if isinstance(plan, dict) else {}
    meta = dict(out.get("_meta") or {}) if isinstance(out.get("_meta"), dict) else {}
    meta["compose_qc_pending"] = True
    if errors:
        meta["compose_qc_errors"] = [str(e) for e in errors[:12]]
    out["_meta"] = meta
    return out


def clear_compose_qc_pending(plan: dict[str, Any]) -> dict[str, Any]:
    out = dict(plan) if isinstance(plan, dict) else {}
    meta = out.get("_meta")
    if isinstance(meta, dict):
        meta = dict(meta)
        meta.pop("compose_qc_pending", None)
        meta.pop("compose_qc_errors", None)
        out["_meta"] = meta
    return out


def compose_qc_pending(plan: dict[str, Any] | None) -> bool:
    if not isinstance(plan, dict):
        return False
    meta = plan.get("_meta")
    return bool(isinstance(meta, dict) and meta.get("compose_qc_pending"))


@contextmanager
def gap_report_write_lock(ctx: RunContext, *, timeout_s: float = 120.0) -> Iterator[None]:
    """P8: single-flight around authoritative gap_report publish → QC assert."""
    from filelock import FileLock, Timeout

    lock_path = ctx.path(GAP_WRITE_LOCK_REL)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock = FileLock(str(lock_path), timeout=float(timeout_s))
    try:
        lock.acquire()
    except Timeout as exc:
        raise RuntimeError(
            "nugget_layup_compose: gap_report write lock timeout — "
            "another writer holds understanding/.gap_report.write.lock"
        ) from exc
    try:
        yield
    finally:
        try:
            lock.release()
        except Exception:
            pass


def _craft_error_targets(qc: dict[str, Any] | None) -> set[str]:
    """Segment ids implicated by craft QC errors healable via spine/skip."""
    out: set[str] = set()
    if not isinstance(qc, dict):
        return out
    for err in qc.get("errors") or []:
        text = str(err or "")
        if not any(m in text for m in CRAFT_SPINE_ERROR_MARKERS):
            continue
        # Patterns: canned_air[seg_001]: … / invented_island_claim[seg_001]: …
        m = re.search(r"\[([^\]]+)\]", text)
        if m:
            tid = str(m.group(1) or "").strip()
            if tid:
                out.add(tid)
    return out


def apply_craft_spine_or_skip(
    ctx: RunContext,
    plan: dict[str, Any] | None = None,
    *,
    qc: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], list[str]]:
    """H1/P7: replace craft-fail rows with corpus-grounded spine or typed skip.

    Never issues an LLM call. Prefer materialize-style grounded copy; if craft
    still fails, stamp a justified skip so QC can clear without thrash.
    """
    notes: list[str] = []
    out = dict(plan) if isinstance(plan, dict) else (
        ctx.read_json(PLAN_REL) if ctx.artifact_exists(PLAN_REL) else {}
    )
    out = dict(out) if isinstance(out, dict) else {"layups": []}
    targets = _craft_error_targets(qc)
    if not targets:
        # Fall back: any craft-like error strings without bracket — heal all aired
        # rows that currently fail craft when QC is non-ok.
        if isinstance(qc, dict) and not qc.get("ok"):
            errs = [str(e) for e in (qc.get("errors") or [])]
            if any(any(m in e for m in CRAFT_SPINE_ERROR_MARKERS) for e in errs):
                for row in out.get("layups") or []:
                    if isinstance(row, dict) and not row.get("skip"):
                        tid = str(row.get("target_segment_id") or "")
                        if tid:
                            targets.add(tid)
    if not targets:
        return out, notes

    # Reuse materialize: temporarily mark craft-bad rows as skips so materialize
    # rebuilds them from nuggets/unlock, then re-evaluate craft.
    layups = [r for r in (out.get("layups") or []) if isinstance(r, dict)]
    for row in layups:
        tid = str(row.get("target_segment_id") or "")
        if tid not in targets:
            continue
        if row.get("skip"):
            continue
        # Preserve analysis fields; clear spoken text so materialize rebuilds.
        row["_craft_spine_prior_text"] = str(row.get("text") or "")
        row["skip"] = True
        row["skip_reason_code"] = "craft_spine_rebuild"
        row["text"] = ""
        notes.append(f"craft_spine_rebuild:{tid}")
    out["layups"] = layups
    out, mat_notes = materialize_over_skipped_layups(ctx, out)
    notes.extend(str(n) for n in mat_notes if str(n).startswith("materialized:"))

    cfg = nugget_layup_cfg()
    still_bad: list[dict[str, Any]] = []
    for row in out.get("layups") or []:
        if not isinstance(row, dict):
            continue
        tid = str(row.get("target_segment_id") or "")
        if tid not in targets:
            continue
        if row.get("skip") or not str(row.get("text") or "").strip():
            still_bad.append(row)
            continue
        craft = evaluate_layup_craft(ctx, [row], cfg=cfg)
        if craft.get("errors"):
            still_bad.append(row)
    for row in still_bad:
        tid = str(row.get("target_segment_id") or "")
        stamp_typed_skip(
            row,
            reason_code="no_eligible_unspent_nugget",
            evidence_refs=[
                f"target:{tid}",
                "craft_spine:unhealable",
            ],
            compensating_path="typed_skip",
            revisit_if=["new_grounded_copy", "corpus_refresh"],
            decision_confidence=0.8,
            owner_stage="nugget_layup_compose",
        )
        row["craft_spine_skipped"] = True
        notes.append(f"craft_spine_skip:{tid}")
    out = normalize_layup_talking_point_ledger(ctx, out)
    return out, notes


def stamp_sparse_or_empty_corpus_exits(
    ctx: RunContext,
    plan: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], list[str]]:
    """P4: empty/sparse corpus → justified skips + clear open-high so QC can pass."""
    notes: list[str] = []
    out = dict(plan) if isinstance(plan, dict) else (
        ctx.read_json(PLAN_REL) if ctx.artifact_exists(PLAN_REL) else {}
    )
    out = dict(out) if isinstance(out, dict) else {"layups": [], "ordered_segment_ids": []}
    corpus = ctx.read_json(CORPUS_REL) if ctx.artifact_exists(CORPUS_REL) else {}
    nuggets = [
        n for n in ((corpus or {}).get("nuggets") or []) if isinstance(n, dict)
    ] if isinstance(corpus, dict) else []
    sparse = False
    try:
        from interview_mux.source_topology import vo_posture_is_sparse_omit

        sparse = vo_posture_is_sparse_omit(ctx)
    except Exception:
        sparse = False
    empty = not nuggets
    if not empty and not sparse:
        return out, notes

    ordered = [str(x) for x in (out.get("ordered_segment_ids") or _ordered_ids(ctx)) if x]
    by_tid = {
        str(r.get("target_segment_id") or ""): r
        for r in (out.get("layups") or [])
        if isinstance(r, dict) and r.get("target_segment_id")
    }
    layups: list[dict[str, Any]] = list(out.get("layups") or []) if isinstance(out.get("layups"), list) else []
    for tid in ordered:
        row = by_tid.get(tid)
        if row is None:
            row = {"target_segment_id": tid, "line_id": f"vo_layup_{tid}"}
            layups.append(row)
            by_tid[tid] = row
        if row.get("skip") and is_justified_skip_row(row, soft_migrate=True):
            continue
        if str(row.get("text") or "").strip() and not empty:
            # Sparse with contentful rows: leave them; only fill holes.
            continue
        stamp_typed_skip(
            row,
            reason_code="no_eligible_unspent_nugget" if empty else "self_explanatory_native",
            evidence_refs=[
                f"target:{tid}",
                "sparse_or_empty_corpus:stamp",
            ],
            compensating_path="typed_skip",
            revisit_if=["corpus_refresh", "selection_change"],
            decision_confidence=0.88,
            owner_stage="nugget_layup_compose",
        )
        notes.append(f"sparse_empty_skip:{tid}")
    out["layups"] = [r for r in layups if isinstance(r, dict)]
    out["open_high_salience_nugget_ids"] = []
    out["open_talking_point_ids"] = []
    warnings = [str(w) for w in (out.get("warnings") or []) if w]
    tag = "empty_corpus_deterministic_exit" if empty else "sparse_omit_deterministic_exit"
    if tag not in warnings:
        warnings.append(tag)
    out["warnings"] = warnings
    out = normalize_layup_talking_point_ledger(ctx, out)
    return out, notes


def stamp_hosted_vo_floor_unsatisfiable(
    ctx: RunContext,
    *,
    need: int,
    active: int,
    eligible_nuggets: int | None = None,
) -> dict[str, Any]:
    """Paperwork for true floor shortage — empty heal pin, no invent under freeze."""
    detail = {
        "status": "open",
        "reason": "hosted_vo_floor_unsatisfiable",
        "need": int(need),
        "active": int(active),
        "eligible_nugget_count": int(eligible_nuggets or 0),
        "stage": "nugget_layup_compose",
        "ts": datetime.now(timezone.utc).isoformat(),
    }
    try:
        ctx.write_json(LAYUP_FLOOR_UNSAT_REL, detail, skip_handoff=True)
    except Exception:
        try:
            ctx.write_json(LAYUP_FLOOR_UNSAT_REL, detail)
        except Exception:
            pass
    try:
        plan = ctx.read_json(PLAN_REL) if ctx.artifact_exists(PLAN_REL) else {}
        if isinstance(plan, dict):
            meta = dict(plan.get("_meta") or {}) if isinstance(plan.get("_meta"), dict) else {}
            meta["hosted_vo_floor_unsatisfiable"] = True
            meta["hosted_vo_floor_unsatisfiable_detail"] = {
                "need": int(need),
                "active": int(active),
                "eligible_nugget_count": int(eligible_nuggets or 0),
            }
            plan["_meta"] = meta
            ctx.write_json(PLAN_REL, plan, skip_handoff=True)
    except Exception:
        pass
    try:

        def _mut(meta: dict[str, Any]) -> None:
            meta["hosted_vo_floor_unsatisfiable"] = True
            meta["hosted_vo_floor_unsatisfiable_prose"] = (
                f"hosted_vo_floor_unsatisfiable: need={need} active={active} "
                "— hard freeze blocks invent; escalate once, do not recompose"
            )
            # Prefer unsatisfiable over unmet thrash pin.
            meta.pop("hosted_vo_floor_unmet", None)
            meta.pop("needs_operator", None)

        ctx.mutate_run_meta(_mut)
    except Exception:
        pass
    return detail


def raise_hosted_vo_floor_unsatisfiable(
    ctx: RunContext,
    *,
    need: int,
    active: int,
    eligible_nuggets: int | None = None,
) -> None:
    """Floor shortage: always advisory-continue (never loud-blocks the master).

    Hosted VO count floor (need=3) is a target. PARTIAL and HOLLOW_ZERO still
    stamp identity + advisories, but publish and finalize proceed.
    """
    from interview_mux.floor_progress import proceed_on_floor_miss

    cause = None
    try:
        from interview_mux.hosted_vo_authority import (
            floor_snapshot,
            reconcile_escalations,
        )

        snap = floor_snapshot(ctx, stage_id="nugget_layup_compose", persist=True)
        cause = snap.identity.cause
        reconcile_escalations(ctx, snap)
    except Exception:
        pass
    # Stamp durable advisory signal (not a ship bar). Keep unsatisfiable meta for
    # operators when hollow, without raising LoudStageFailure.
    if int(active) < 1:
        try:
            stamp_hosted_vo_floor_unsatisfiable(
                ctx,
                need=need,
                active=active,
                eligible_nuggets=eligible_nuggets,
            )
        except Exception:
            pass
    proceed_on_floor_miss(
        ctx,
        gate_id="hosted_vo_floor",
        have=int(active),
        need=int(need),
        pool_exhausted=True,
        extra={
            "eligible_nuggets": int(eligible_nuggets or 0),
            "mode": "advisory_count_floor_continue",
            "cause": cause,
            "active_synthetic": int(active),
        },
    )
    ctx.log(
        "nugget_layup_compose: hosted_vo_floor under target "
        f"(active_synthetic={active} < min={need}; "
        f"eligible_nuggets={eligible_nuggets or 0}) — advisory continue",
        level="warning",
        stage="nugget_layup_compose",
        action_id="gap_vo.floor_advisory_continue",
    )


def ensure_deterministic_floor_before_refuse(
    ctx: RunContext,
    plan: dict[str, Any],
) -> tuple[dict[str, Any], list[str]]:
    """P2: last deterministic materialize/spine pass before floor refuse."""
    notes: list[str] = []
    out = dict(plan) if isinstance(plan, dict) else {}
    sparse = False
    try:
        from interview_mux.source_topology import vo_posture_is_sparse_omit

        sparse = vo_posture_is_sparse_omit(ctx)
    except Exception:
        sparse = False
    if sparse:
        out, sparse_notes = stamp_sparse_or_empty_corpus_exits(ctx, out)
        notes.extend(sparse_notes)
        out, skip_notes = stamp_valueless_skips(ctx, out)
        notes.extend(
            f"valueless:{n.get('target_segment_id')}" for n in skip_notes if isinstance(n, dict)
        )
    else:
        out, mat_notes = materialize_over_skipped_layups(ctx, out)
        notes.extend(str(n) for n in mat_notes if str(n).startswith("materialized:"))
    # Spine any remaining craft holes so floor topup has contentful rows.
    qc = evaluate_layup_qc(ctx, out)
    if not qc.get("ok") and _craft_error_targets(qc):
        out, spine_notes = apply_craft_spine_or_skip(ctx, out, qc=qc)
        notes.extend(spine_notes)
        qc = evaluate_layup_qc(ctx, out)
    # Open high-salience nuggets were a structural refuse with no heal in this
    # pass; the recovery that attaches them to an aired layup (or unskips a
    # native to carry them) existed but was never called (exec_052 nug_009,
    # ISSUES entry 66). Still one deterministic pass, no ladder.
    if not qc.get("ok") and any(
        str(e).startswith("open_high_salience_nuggets=") for e in (qc.get("errors") or [])
    ):
        try:
            out, rec_notes = recover_open_high_salience_nuggets(ctx, out)
            notes.extend(f"recover_high:{n}" for n in rec_notes)
        except Exception as exc:
            notes.append(f"recover_high_failed:{type(exc).__name__}")
        # Copy the recovery attached or unskipped must pass the same spoken
        # guard as model copy before QC sees it; a row it cannot make airable
        # is skipped. A high-salience nugget that still has no legal line is
        # parked on the orientation (the existing last resort) rather than
        # refusing the stage on every pass until the budget runs out.
        try:
            out, heal_notes = repair_or_skip_spoken_copy_layups(ctx, out)
            notes.extend(
                f"post_recover_copy:{n.get('action')}:{n.get('target_segment_id')}"
                for n in heal_notes
                if isinstance(n, dict)
            )
        except Exception as exc:
            notes.append(f"post_recover_copy_failed:{type(exc).__name__}")
        qc = evaluate_layup_qc(ctx, out)
        if not qc.get("ok") and any(
            str(e).startswith("open_high_salience_nuggets=") for e in (qc.get("errors") or [])
        ):
            try:
                out, park_notes = park_open_high_salience_on_orientation(ctx, out)
                notes.extend(f"recover_high:{n}" for n in park_notes)
                if park_notes:
                    qc = evaluate_layup_qc(ctx, out)
            except Exception as exc:
                notes.append(f"orientation_park_failed:{type(exc).__name__}")
    # Same gap for must-keep talking points: the recovery that attaches an open
    # id to a layup already carrying it, or discharges it when selected native
    # text already covers it, was never called (exec_050 tp_001/tp_002,
    # exec_052 tp_002). It invents no VO.
    if not qc.get("ok") and any(
        str(e).startswith("open_must_keep_talking_points=") for e in (qc.get("errors") or [])
    ):
        try:
            out, tp_notes = recover_open_must_keep_talking_points(ctx, out)
            notes.extend(f"recover_tp:{n}" for n in tp_notes)
        except Exception as exc:
            notes.append(f"recover_tp_failed:{type(exc).__name__}")
    return out, notes


def prior_gap_line_fingerprints(ctx: RunContext) -> dict[str, str]:
    """line_id → normalized text for rewrite detection (P5)."""
    out: dict[str, str] = {}
    if not ctx.artifact_exists(GAP_REL):
        return out
    try:
        gap = ctx.read_json(GAP_REL)
    except Exception:
        return out
    if not isinstance(gap, dict):
        return out
    for ln in gap.get("interviewer_lines") or []:
        if not isinstance(ln, dict):
            continue
        lid = str(ln.get("line_id") or "").strip()
        if not lid:
            continue
        out[lid] = _norm(str(ln.get("text") or ""))
    return out


def invalidate_vo_after_layup_rewrite(
    ctx: RunContext,
    *,
    prior_fps: dict[str, str],
    new_report: dict[str, Any] | None,
) -> list[str]:
    """P5: drop stale WAVs / clear vo_synthesize done when layup text/ids change."""
    touched: list[str] = []
    if not isinstance(new_report, dict):
        return touched
    new_fps: dict[str, str] = {}
    for ln in new_report.get("interviewer_lines") or []:
        if not isinstance(ln, dict):
            continue
        lid = str(ln.get("line_id") or "").strip()
        if not lid:
            continue
        origin = str(ln.get("origin") or "")
        if origin and origin not in AUTHORITY_BODY_ORIGINS and not lid.startswith("vo_layup_"):
            continue
        new_fps[lid] = _norm(str(ln.get("text") or ""))
    changed = [
        lid
        for lid, text in new_fps.items()
        if prior_fps.get(lid) != text
    ]
    # Removed lines that previously had copy also need WAV purge.
    for lid, text in prior_fps.items():
        if lid.startswith("vo_layup_") and lid not in new_fps and text:
            changed.append(lid)
    changed = list(dict.fromkeys(changed))
    if not changed:
        return touched
    pickup = ctx.path("vo_pickup")
    for lid in changed:
        wav = pickup / f"{lid}.wav"
        try:
            if wav.is_file():
                wav.unlink()
                touched.append(lid)
        except Exception:
            pass
    # Mark vo_synthesize incomplete so seed order re-runs synth for touched lines.
    if touched or changed:
        try:
            from interview_mux.homunculus.agenda import unmark_stage_only

            unmark_stage_only(ctx, "vo_synthesize")
        except Exception:
            try:
                marker = ctx.final_path(".stage_done", "vo_synthesize")
                if marker.is_file():
                    marker.unlink(missing_ok=True)
            except Exception:
                pass
    if changed:
        try:
            ctx.log(
                "nugget_layup: invalidated VO binds after rewrite "
                f"({len(changed)} line(s)): {changed[:8]}",
                level="warning",
                stage="nugget_layup_compose",
                detail={"line_ids": changed[:24]},
            )
        except Exception:
            pass
    return changed


def eligible_nugget_count_for_floor(ctx: RunContext) -> int:
    corpus = ctx.read_json(CORPUS_REL) if ctx.artifact_exists(CORPUS_REL) else {}
    plan = ctx.read_json(PLAN_REL) if ctx.artifact_exists(PLAN_REL) else {}
    waived = waived_nugget_ids_from_sources(
        plan.get("waived_nugget_ids") if isinstance(plan, dict) else None,
        plan if isinstance(plan, dict) else None,
    )
    return len(eligible_nugget_ids(corpus if isinstance(corpus, dict) else {}, waived))


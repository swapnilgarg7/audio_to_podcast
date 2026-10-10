"""Per-edge boundary confidence scoring and bounded deterministic repair.

LLM proposes segment starts/ends; this module scores each edge from acoustic,
linguistic, ASR, and continuity signals, optionally nudges low-confidence edges
to a better local hinge, and emits a review queue for unrepaired suspects.
"""

from __future__ import annotations

import bisect
import re
from datetime import datetime, timezone
from typing import Any

from interview_mux.segment_timeline_standard import edge_confidence_cfg, segmentation_cfg

BOUNDARY_REVIEW_QUEUE_REL = "segments/boundary_review_queue.json"

_ELLIPSIS_RE = re.compile(r"(?:\.\.\.|…)\s*$")
_DANGLING_INTERROGATIVE_RE = re.compile(
    r"(?i)\b(?:do|does|did|is|are|was|were|can|could|would|will|should|what|how|why|when|where)\s*"
    r"(?:\.\.\.|…|,)?\s*$"
)
_TRAILING_COMMA_RE = re.compile(r",\s*$")
_FILLER_TAIL_RE = re.compile(r"(?i)\b(?:like|you know|kind of|sort of|um+|uh+)\W*$")
_PARALLEL_MAKING_RE = re.compile(r"(?i)\bmaking it\s+(\w+)\s*,?\s*$")


def grade_from_score(score: float, cfg: dict[str, Any] | None = None) -> str:
    conf = edge_confidence_cfg(cfg)
    if score >= float(conf["grade_high"]):
        return "high"
    if score >= float(conf["grade_medium"]):
        return "medium"
    if score >= float(conf["grade_low"]):
        return "low"
    return "reject"


def _words_from_transcript(transcript: dict[str, Any] | None) -> list[dict[str, Any]]:
    if not isinstance(transcript, dict):
        return []
    words = transcript.get("words") or []
    return [
        w
        for w in words
        if isinstance(w, dict)
        and w.get("start_ms") is not None
        and w.get("end_ms") is not None
        and str(w.get("text") or w.get("word") or "").strip()
    ]


def _tok(w: dict[str, Any]) -> str:
    return str(w.get("text") or w.get("word") or "").strip()


def _span_text(words: list[dict[str, Any]], start_ms: int, end_ms: int) -> str:
    parts = [
        _tok(w)
        for w in words
        if int(w.get("start_ms") or 0) >= start_ms - 20
        and int(w.get("end_ms") or 0) <= end_ms + 20
        and _tok(w)
    ]
    return " ".join(parts).strip()


def _edge_context_text(
    words: list[dict[str, Any]], t_ms: int, *, side: str, n: int = 10
) -> str:
    if side == "left":
        ws = [w for w in words if int(w.get("end_ms") or 0) <= t_ms + 40][-n:]
    else:
        ws = [w for w in words if int(w.get("start_ms") or 0) >= t_ms - 40][:n]
    return " ".join(_tok(w) for w in ws)


def _pause_across(words: list[dict[str, Any]], t_ms: int) -> int | None:
    prev = None
    nxt = None
    for w in words:
        end = int(w.get("end_ms") or 0)
        start = int(w.get("start_ms") or 0)
        if end <= t_ms + 20:
            prev = w
        if start >= t_ms - 20 and nxt is None:
            nxt = w
            break
    if prev is None or nxt is None:
        return None
    return max(0, int(nxt["start_ms"]) - int(prev["end_ms"]))


def _mean_asr_near(
    words: list[dict[str, Any]], t_ms: int, window_ms: int
) -> float | None:
    confs: list[float] = []
    for w in words:
        start = int(w.get("start_ms") or 0)
        end = int(w.get("end_ms") or 0)
        if end < t_ms - window_ms or start > t_ms + window_ms:
            continue
        c = w.get("confidence")
        if isinstance(c, (int, float)):
            confs.append(float(c))
    if not confs:
        return None
    return sum(confs) / len(confs)


def _word_aligned(words: list[dict[str, Any]], t_ms: int, tol_ms: int) -> bool:
    for w in words:
        start = int(w.get("start_ms") or 0)
        end = int(w.get("end_ms") or 0)
        if abs(start - t_ms) <= tol_ms or abs(end - t_ms) <= tol_ms:
            return True
        if start < t_ms < end:
            return False
    return True


def _mid_word(words: list[dict[str, Any]], t_ms: int) -> bool:
    for w in words:
        start = int(w.get("start_ms") or 0)
        end = int(w.get("end_ms") or 0)
        if start + 5 < t_ms < end - 5:
            return True
        if start > t_ms + 50:
            break
    return False


def _nearest_spine(
    events: list[dict[str, Any]],
    etimes: list[int],
    t_ms: int,
    radius_ms: int,
) -> dict[str, Any] | None:
    if not etimes:
        return None
    i = bisect.bisect_left(etimes, t_ms)
    best: tuple[int, dict[str, Any]] | None = None
    for j in range(max(0, i - 4), min(len(events), i + 5)):
        d = abs(int(events[j].get("time_ms") or 0) - t_ms)
        if d > radius_ms:
            continue
        if best is None or d < best[0]:
            best = (d, events[j])
    return None if best is None else best[1]


def _spine_support_score(
    ev: dict[str, Any] | None,
    *,
    dist_ms: int | None,
    align_ms: int,
    require_multi: bool,
) -> tuple[float, list[str]]:
    reasons: list[str] = []
    if ev is None or dist_ms is None:
        reasons.append("no_spine_event")
        return 0.35, reasons
    conf = float(ev.get("confidence") or 0.0)
    sources = list(ev.get("sources") or [])
    n_src = len(sources)
    dist_factor = max(0.0, 1.0 - (dist_ms / max(align_ms, 1)))
    silence_only = n_src == 1 and any("rms" in str(s) or "vad" in str(s) for s in sources)
    if silence_only and require_multi:
        # Silence valleys are supporting evidence only.
        score = 0.45 + 0.25 * conf * dist_factor
        reasons.append("silence_only_spine")
    else:
        multi_bonus = 0.15 if n_src >= 2 else 0.0
        score = min(1.0, 0.55 * conf + 0.3 * dist_factor + multi_bonus)
        if n_src >= 2:
            reasons.append("multi_source_spine")
    return score, reasons


def linguistic_end_penalty(text: str) -> tuple[float, list[str]]:
    """Return (0..1 penalty, reasons) for unsafe end text."""
    stripped = (text or "").strip()
    reasons: list[str] = []
    penalty = 0.0
    if not stripped:
        return 0.4, ["empty_end_text"]
    if _ELLIPSIS_RE.search(stripped):
        penalty = max(penalty, 0.55)
        reasons.append("ellipsis_hang")
    if _DANGLING_INTERROGATIVE_RE.search(stripped) and stripped[-1:] not in ".!?":
        penalty = max(penalty, 0.5)
        reasons.append("dangling_interrogative")
    if _TRAILING_COMMA_RE.search(stripped):
        penalty = max(penalty, 0.35)
        reasons.append("comma_hang")
    if _FILLER_TAIL_RE.search(stripped) and stripped[-1:] not in ".!?":
        penalty = max(penalty, 0.25)
        reasons.append("trailing_filler")
    return penalty, reasons


def continuation_pair_penalty(left_text: str, right_text: str) -> tuple[float, list[str]]:
    """Detect split parallel constructions across a seam."""
    reasons: list[str] = []
    left = (left_text or "").strip()
    right = (right_text or "").strip()
    m = _PARALLEL_MAKING_RE.search(left)
    if m and re.match(r"(?i)^making it\b", right):
        reasons.append("parallel_making_split")
        return 0.55, reasons
    # Same stem continuation: "actionable," | "accessible"
    if left.rstrip().endswith(",") and right:
        left_last = left.rstrip(", ").split()[-1].lower() if left.split() else ""
        right_first = right.split()[0].lower().strip(".,") if right.split() else ""
        if left_last and right_first and left_last[:4] == right_first[:4] and len(left_last) > 4:
            reasons.append("parallel_stem_split")
            return 0.4, reasons
    return 0.0, reasons


def score_edge(
    *,
    edge: str,
    t_ms: int,
    row: dict[str, Any],
    neighbor: dict[str, Any] | None,
    words: list[dict[str, Any]],
    spine_events: list[dict[str, Any]] | None = None,
    spine_times: list[int] | None = None,
    cfg: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Score a single start or end edge; returns edgeEvidence-shaped dict."""
    from interview_mux.gap_vo_prior_context import (
        clause_continues_after,
        clause_continues_before,
        is_legal_conceptual_hinge,
        is_legal_conceptual_open,
    )

    conf = edge_confidence_cfg(cfg)
    reasons: list[str] = []
    signals: dict[str, float] = {}

    pause = _pause_across(words, t_ms)
    pause_good = int(conf["pause_good_ms"])
    pause_strong = int(conf["pause_strong_ms"])
    if pause is None:
        pause_score = 0.5
        reasons.append("unknown_pause")
    elif pause >= pause_strong:
        pause_score = 1.0
    elif pause >= pause_good:
        pause_score = 0.75
    elif pause >= 150:
        pause_score = 0.45
        reasons.append("tight_pause")
    else:
        pause_score = 0.15
        reasons.append("zero_gap_or_mid_breath")
    signals["pause"] = round(pause_score, 3)

    events = spine_events or []
    etimes = spine_times if spine_times is not None else [int(e["time_ms"]) for e in events]
    align = int(conf["spine_align_ms"])
    nearest = _nearest_spine(events, etimes, t_ms, align * 2)
    dist = abs(int(nearest["time_ms"]) - t_ms) if nearest else None
    spine_score, spine_reasons = _spine_support_score(
        nearest,
        dist_ms=dist,
        align_ms=align,
        require_multi=bool(conf.get("require_multi_source_for_high", True)),
    )
    signals["spine"] = round(spine_score, 3)
    reasons.extend(spine_reasons)

    tol = int(conf["word_snap_tol_ms"])
    mid = _mid_word(words, t_ms)
    aligned = (not mid) and _word_aligned(words, t_ms, tol)
    word_score = 0.2 if mid else (1.0 if aligned else 0.55)
    signals["word_align"] = word_score
    if mid:
        reasons.append("mid_word")

    asr = _mean_asr_near(words, t_ms, int(conf["asr_window_ms"]))
    if asr is None:
        asr_score = 0.7
    else:
        asr_score = max(0.0, min(1.0, asr))
        if asr < 0.5:
            reasons.append("low_asr")
    signals["asr"] = round(asr_score, 3)

    start = int(row.get("start_ms") or 0)
    end = int(row.get("end_ms") or start)
    legal_score = 0.5
    if edge == "end":
        text = _span_text(words, max(start, end - 30_000), end)
        legal = is_legal_conceptual_hinge(
            text, words=words, end_ms=t_ms, next_pause_ms=pause
        )
        continues = clause_continues_after(words, t_ms) if words else False
        ling_pen, ling_reasons = linguistic_end_penalty(text)
        reasons.extend(ling_reasons)
        if continues:
            reasons.append("clause_continues_after")
            legal = False
        legal_score = 1.0 if legal else max(0.0, 0.35 - ling_pen)
        if neighbor is not None:
            left = _edge_context_text(words, t_ms, side="left", n=12)
            right = _edge_context_text(words, int(neighbor.get("start_ms") or t_ms), side="right", n=12)
            pair_pen, pair_reasons = continuation_pair_penalty(left, right)
            reasons.extend(pair_reasons)
            legal_score = max(0.0, legal_score - pair_pen)
    else:
        text = _span_text(words, start, min(end, start + 30_000))
        legal = is_legal_conceptual_open(
            text, words=words, start_ms=t_ms, prev_pause_ms=pause
        )
        continues = clause_continues_before(words, t_ms) if words else False
        if continues:
            reasons.append("clause_continues_before")
            legal = False
        first = text.split()[0].lower().strip(".,!?") if text.split() else ""
        if first in {"and", "but", "so", "because", "which", "that", "or", "right"} and (
            pause is None or pause < pause_good
        ):
            reasons.append("continuation_open")
            legal = False
        legal_score = 1.0 if legal else 0.25
    signals["legal_hinge"] = round(legal_score, 3)

    # Speaker id does not raise or lower a cut. Pause is ranked only after the
    # text predicate says the concept has changed.
    turn_score = 0.55
    signals["turn"] = turn_score

    overall = (
        0.22 * pause_score
        + 0.18 * spine_score
        + 0.18 * word_score
        + 0.12 * asr_score
        + 0.22 * legal_score
        + 0.08 * turn_score
    )
    # Hard caps for clear editorial failures.
    if "ellipsis_hang" in reasons or "dangling_interrogative" in reasons:
        overall = min(overall, 0.4)
    if "parallel_making_split" in reasons:
        overall = min(overall, 0.35)
    if mid:
        overall = min(overall, 0.5)
    if legal_score < 0.95:
        # A breath cannot promote a mid-sentence or same-concept edge.
        overall = min(overall, 0.4)
    else:
        from interview_mux.gap_vo_prior_context import (
            concept_boundary_rank,
            word_density_per_sec,
        )

        before = word_density_per_sec(words, t_ms - 1) if words else 0.0
        after = word_density_per_sec(words, t_ms + 1) if words else 0.0
        rank = concept_boundary_rank(
            gap_ms=pause, density_before=before, density_after=after
        )
        signals["concept_rank"] = rank
        overall = min(1.0, overall + 0.05 * rank)
    overall = max(0.0, min(1.0, overall))
    grade = grade_from_score(overall, cfg)
    # Silence-only + high legal still cannot be "high" when multi-source required.
    if (
        grade == "high"
        and bool(conf.get("require_multi_source_for_high", True))
        and "silence_only_spine" in reasons
        and "multi_source_spine" not in reasons
        and legal_score < 0.95
    ):
        grade = "medium"
        overall = min(overall, float(conf["grade_high"]) - 0.01)

    return {
        "overall": round(overall, 3),
        "grade": grade,
        "selected_ms": int(t_ms),
        "candidate_ms": int(t_ms),
        "signals": signals,
        "reasons": list(dict.fromkeys(reasons)),
        "repaired": False,
    }


def score_boundary_rows(
    rows: list[dict[str, Any]],
    *,
    transcript: dict[str, Any] | None = None,
    spine_events: list[dict[str, Any]] | None = None,
    cfg: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Annotate each boundary row with start_edge / end_edge / confidence."""
    conf = edge_confidence_cfg(cfg)
    if not conf.get("enabled", True):
        return [dict(r) for r in rows]
    words = _words_from_transcript(transcript)
    events = [e for e in (spine_events or []) if isinstance(e, dict) and e.get("time_ms") is not None]
    events.sort(key=lambda e: int(e["time_ms"]))
    etimes = [int(e["time_ms"]) for e in events]
    out: list[dict[str, Any]] = []
    for i, row in enumerate(rows):
        if not isinstance(row, dict):
            continue
        cur = dict(row)
        start = int(cur.get("start_ms") or 0)
        end = int(cur.get("end_ms") or start)
        prev = rows[i - 1] if i > 0 and isinstance(rows[i - 1], dict) else None
        nxt = rows[i + 1] if i + 1 < len(rows) and isinstance(rows[i + 1], dict) else None
        start_edge = score_edge(
            edge="start",
            t_ms=start,
            row=cur,
            neighbor=prev,
            words=words,
            spine_events=events,
            spine_times=etimes,
            cfg=cfg,
        )
        end_edge = score_edge(
            edge="end",
            t_ms=end,
            row=cur,
            neighbor=nxt,
            words=words,
            spine_events=events,
            spine_times=etimes,
            cfg=cfg,
        )
        combined = min(float(start_edge["overall"]), float(end_edge["overall"]))
        cur["start_edge"] = start_edge
        cur["end_edge"] = end_edge
        cur["confidence"] = round(combined, 3)
        cur["edge_grade"] = grade_from_score(combined, cfg)
        out.append(cur)
    return out


def _candidate_hinge_times(
    words: list[dict[str, Any]],
    *,
    center_ms: int,
    window_ms: int,
    edge: str,
) -> list[int]:
    from interview_mux.gap_vo_prior_context import (
        is_legal_conceptual_hinge,
        is_legal_conceptual_open,
    )

    lo = max(0, center_ms - window_ms)
    hi = center_ms + window_ms
    cands: list[int] = []
    for i in range(len(words) - 1):
        prev = words[i]
        nxt = words[i + 1]
        gap = int(nxt["start_ms"]) - int(prev["end_ms"])
        if edge == "end":
            t = int(prev["end_ms"])
            if not (lo <= t <= hi):
                continue
            text = " ".join(_tok(w) for w in words[max(0, i - 20) : i + 1])
            if not is_legal_conceptual_hinge(
                text, words=words, end_ms=t, next_pause_ms=gap
            ):
                continue
            cands.append(t)
        else:
            t = int(nxt["start_ms"])
            if not (lo <= t <= hi):
                continue
            text = " ".join(_tok(w) for w in words[i + 1 : i + 12])
            if not is_legal_conceptual_open(
                text, words=words, start_ms=t, prev_pause_ms=gap
            ):
                continue
            cands.append(t)
    # Always consider exact word snaps near center.
    for w in words:
        for t in (int(w["start_ms"]), int(w["end_ms"])):
            if lo <= t <= hi:
                cands.append(t)
    return sorted(set(cands))


_NUDGE_FILLERS = frozenset({"um", "uh", "umm", "uhh", "er", "erm", "ah", "hmm", "mm", "mhm"})


def _nudge_drops_speech(words: list[dict[str, Any]], lo_ms: int, hi_ms: int) -> bool:
    """True when a word other than a filler sits between the old and new edge.

    The nudge moves one side of one row only, so a shrink leaves those words
    in no segment, and the new edge opens or closes mid-sentence.
    """
    for w in words:
        try:
            mid = (int(w["start_ms"]) + int(w["end_ms"])) // 2
        except (KeyError, TypeError, ValueError):
            continue
        if lo_ms < mid < hi_ms and _tok(w).strip(".,!?;:-").lower() not in _NUDGE_FILLERS:
            return True
    return False


def repair_low_confidence_edges(
    rows: list[dict[str, Any]],
    *,
    transcript: dict[str, Any] | None = None,
    spine_events: list[dict[str, Any]] | None = None,
    cfg: dict[str, Any] | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Nudge low/reject edges to a better local hinge when score improves."""
    conf = edge_confidence_cfg(cfg)
    sc = segmentation_cfg(cfg)
    if not conf.get("enabled", True) or not conf.get("repair_enabled", True):
        return score_boundary_rows(
            rows, transcript=transcript, spine_events=spine_events, cfg=cfg
        ), []

    words = _words_from_transcript(transcript)
    scored = score_boundary_rows(
        rows, transcript=transcript, spine_events=spine_events, cfg=cfg
    )
    repair_below = float(conf["repair_below"])
    min_improve = float(conf["min_improvement"])
    window = int(conf["search_window_ms"])
    min_ms = int(sc.get("min_segment_duration_ms") or 4000)
    actions: list[dict[str, Any]] = []

    for i, row in enumerate(scored):
        start = int(row["start_ms"])
        end = int(row["end_ms"])
        for edge_key, edge_name in (("start_edge", "start"), ("end_edge", "end")):
            edge = dict(row.get(edge_key) or {})
            overall = float(edge.get("overall") or 1.0)
            if overall >= repair_below:
                continue
            t0 = int(edge.get("selected_ms") or (start if edge_name == "start" else end))
            # Bound search so we don't invade neighbor / violate min duration.
            if edge_name == "end":
                hard_lo = start + min_ms
                hard_hi = end + window
                if i + 1 < len(scored):
                    hard_hi = min(hard_hi, int(scored[i + 1]["start_ms"]) - 40)
            else:
                hard_lo = start - window
                hard_hi = end - min_ms
                if i > 0:
                    hard_lo = max(hard_lo, int(scored[i - 1]["end_ms"]) + 40)
            hard_lo = max(0, hard_lo)
            if hard_hi <= hard_lo:
                continue
            cands = [
                t
                for t in _candidate_hinge_times(
                    words, center_ms=t0, window_ms=window, edge=edge_name
                )
                if hard_lo <= t <= hard_hi
                and t != t0
                and not (
                    edge_name == "end" and t < t0 and _nudge_drops_speech(words, t, t0)
                )
                and not (
                    edge_name == "start" and t > t0 and _nudge_drops_speech(words, t0, t)
                )
            ]
            best_t = None
            best_score = overall
            best_edge: dict[str, Any] | None = None
            trial = dict(row)
            for cand in cands:
                if edge_name == "end":
                    trial["end_ms"] = cand
                else:
                    trial["start_ms"] = cand
                prev = scored[i - 1] if i > 0 else None
                nxt = scored[i + 1] if i + 1 < len(scored) else None
                trial_edge = score_edge(
                    edge=edge_name,
                    t_ms=cand,
                    row=trial,
                    neighbor=nxt if edge_name == "end" else prev,
                    words=words,
                    spine_events=spine_events,
                    cfg=cfg,
                )
                s = float(trial_edge["overall"])
                if s >= best_score + min_improve:
                    best_score = s
                    best_t = cand
                    best_edge = trial_edge
            if best_t is None or best_edge is None:
                continue
            original = t0
            if edge_name == "end":
                row["end_ms"] = best_t
            else:
                row["start_ms"] = best_t
            best_edge["repaired"] = True
            best_edge["repair_action"] = "nudge_to_legal_hinge"
            best_edge["original_ms"] = original
            best_edge["candidate_ms"] = best_t
            best_edge["selected_ms"] = best_t
            row[edge_key] = best_edge
            actions.append(
                {
                    "action": "nudge_edge",
                    "segment_id": row.get("segment_id"),
                    "edge": edge_name,
                    "from_ms": original,
                    "to_ms": best_t,
                    "before": overall,
                    "after": best_score,
                }
            )

        # Refresh combined confidence after possible repairs.
        se = row.get("start_edge") or {}
        ee = row.get("end_edge") or {}
        combined = min(float(se.get("overall") or 1.0), float(ee.get("overall") or 1.0))
        row["confidence"] = round(combined, 3)
        row["edge_grade"] = grade_from_score(combined, cfg)

    # Re-score once so neighbor-dependent signals stay consistent after nudges.
    rescored = score_boundary_rows(
        scored, transcript=transcript, spine_events=spine_events, cfg=cfg
    )
    # Preserve repair provenance from the nudge pass.
    by_id = {str(r.get("segment_id")): r for r in scored if r.get("segment_id")}
    for row in rescored:
        sid = str(row.get("segment_id") or "")
        prev = by_id.get(sid)
        if not prev:
            continue
        for ek in ("start_edge", "end_edge"):
            old = prev.get(ek) or {}
            if old.get("repaired"):
                new = dict(row.get(ek) or {})
                new["repaired"] = True
                new["repair_action"] = old.get("repair_action")
                new["original_ms"] = old.get("original_ms")
                row[ek] = new
    return rescored, actions


def build_boundary_review_queue(
    rows: list[dict[str, Any]],
    *,
    transcript: dict[str, Any] | None = None,
    cfg: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build operator review queue from unrepaired low/reject edges."""
    conf = edge_confidence_cfg(cfg)
    queue_below = float(conf["queue_below"])
    words = _words_from_transcript(transcript)
    items: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        sid = str(row.get("segment_id") or "")
        for edge_name, edge_key in (("start", "start_edge"), ("end", "end_edge")):
            edge = row.get(edge_key) or {}
            if not isinstance(edge, dict):
                continue
            overall = float(edge.get("overall") or 1.0)
            grade = str(edge.get("grade") or grade_from_score(overall, cfg))
            repaired = bool(edge.get("repaired"))
            reasons = [str(r) for r in (edge.get("reasons") or [])]
            needs = (overall < queue_below or grade in {"low", "reject"}) and not repaired
            hard = any(
                r in reasons
                for r in (
                    "ellipsis_hang",
                    "dangling_interrogative",
                    "parallel_making_split",
                    "clause_continues_after",
                    "clause_continues_before",
                    "mid_word",
                )
            )
            if not needs and not (hard and not repaired):
                continue
            t_ms = int(edge.get("selected_ms") or row.get(f"{edge_name}_ms") or 0)
            kind = "low_confidence"
            if hard:
                kind = "hanging_edge" if "hang" in " ".join(reasons) or "dangling" in " ".join(reasons) else "continuity"
            if "mid_word" in reasons:
                kind = "mid_word"
            items.append(
                {
                    "item_id": f"{sid}_{edge_name}_{t_ms}",
                    "segment_id": sid,
                    "edge": edge_name,
                    "time_ms": t_ms,
                    "original_ms": int(edge.get("original_ms") or t_ms),
                    "suggested_ms": int(edge.get("candidate_ms") or t_ms),
                    "overall": overall,
                    "grade": grade,
                    "reasons": reasons,
                    "left_text": _edge_context_text(words, t_ms, side="left"),
                    "right_text": _edge_context_text(words, t_ms, side="right"),
                    "repaired": repaired,
                    "needs_review": True,
                    "kind": kind,
                    "rank": 0,
                }
            )
    items.sort(key=lambda it: (0 if it["grade"] == "reject" else 1, float(it["overall"]), int(it["time_ms"])))
    for i, it in enumerate(items, start=1):
        it["rank"] = i
    return {
        "version": 1,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "low_confidence_threshold": queue_below,
        "item_count": len(items),
        "items": items,
    }


def score_and_repair_boundaries(
    doc: dict[str, Any],
    *,
    transcript: dict[str, Any] | None = None,
    spine_doc: dict[str, Any] | None = None,
    cfg: dict[str, Any] | None = None,
    repair: bool = True,
) -> tuple[dict[str, Any], dict[str, Any], list[dict[str, Any]]]:
    """Score (+optional repair) a boundaries document; return (doc, queue, actions)."""
    rows = [dict(r) for r in (doc.get("boundaries") or []) if isinstance(r, dict)]
    events = []
    if isinstance(spine_doc, dict):
        events = [e for e in (spine_doc.get("boundary_events") or []) if isinstance(e, dict)]
    if repair:
        scored, actions = repair_low_confidence_edges(
            rows, transcript=transcript, spine_events=events, cfg=cfg
        )
    else:
        scored = score_boundary_rows(
            rows, transcript=transcript, spine_events=events, cfg=cfg
        )
        actions = []
    out = dict(doc)
    out["boundaries"] = scored
    queue = build_boundary_review_queue(scored, transcript=transcript, cfg=cfg)
    return out, queue, actions


def apply_boundary_confidence_pass(
    ctx: Any,
    *,
    stage: str,
    repair: bool = True,
) -> dict[str, Any]:
    """Run score/repair on ctx boundaries artifact and write review queue."""
    from interview_mux.stage_coupling import publish_boundary_contract

    conf = edge_confidence_cfg()
    if not conf.get("enabled", True):
        return {"skipped": True, "reason": "disabled"}
    if not ctx.artifact_exists("segments/boundaries.json"):
        return {"skipped": True, "reason": "no_boundaries"}
    doc = ctx.read_json("segments/boundaries.json")
    if not isinstance(doc, dict):
        return {"skipped": True, "reason": "invalid_doc"}
    transcript = (
        ctx.read_json("transcript/full.json")
        if ctx.artifact_exists("transcript/full.json")
        else None
    )
    spine = (
        ctx.read_json("understanding/interview_spine.json")
        if ctx.artifact_exists("understanding/interview_spine.json")
        else None
    )
    out, queue, actions = score_and_repair_boundaries(
        doc,
        transcript=transcript if isinstance(transcript, dict) else None,
        spine_doc=spine if isinstance(spine, dict) else None,
        repair=repair and bool(conf.get("repair_enabled", True)),
    )
    out = publish_boundary_contract(out, publisher_stage=stage)
    from interview_mux.shared_path_commit import commit_boundaries_doc

    # Edge score may run under an ALLOW stage (e.g. boundary_detection) or a
    # helper stage — claim only when stage is an ownership producer.
    from interview_mux.shared_path_commit import persist_allow_stages

    claim = str(stage or "").strip() in persist_allow_stages("segments/boundaries.json")
    commit_boundaries_doc(
        ctx,
        out,
        stage_key=str(stage or "") or None,
        claim_producer=claim,
    )
    ctx.write_json(BOUNDARY_REVIEW_QUEUE_REL, queue, stage_key=stage)
    low = sum(1 for r in out.get("boundaries") or [] if str(r.get("edge_grade")) in {"low", "reject"})
    ctx.log(
        f"boundary edge confidence: repaired={len(actions)} "
        f"queue={queue.get('item_count')} low/reject_rows={low}",
        level="info",
        stage=stage,
    )
    return {
        "repaired": len(actions),
        "queue_items": int(queue.get("item_count") or 0),
        "low_reject_rows": low,
        "actions": actions[:20],
    }

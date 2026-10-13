"""Tape-time air-order integrity: detect reverse jumps and late opening clusters."""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from interview_mux.air_order_policy import (
    DEFAULT_OPENING_AIR_SLOTS,
    DEFAULT_OPENING_BODY_START_INDEX,
    DEFAULT_OPENING_WINDOW_MS,
    DEFAULT_REVERSE_JUMP_MARGIN_MS,
    policy_value,
    resolve_air_order_policy,
)
from interview_mux.config import merged_config
from interview_mux.run_context import RunContext
from interview_mux.selection_order_repair import (
    _parent_seg_id,
    resolved_source_start_ms,
)

_RECUT_FRAG_RE = re.compile(r"^(seg_\d+)([a-z]+)$", re.IGNORECASE)

INTEGRITY_REL = "master/air_order_integrity.json"
OPERATOR_LOG_REL = "operator/air_order_integrity.log.jsonl"

# Sticky exclude reasons from the opening constitution. Hard-keep / incomplete-seam
# must not restore or pull these ids back onto air.
OPENING_CONSTITUTION_EXCLUDE_REASONS: frozenset[str] = frozenset(
    {
        "opening_skipped_duplicate",
        "opening_slot_overflow",
        "late_intro_reset",
    }
)


def is_opening_constitution_exclude_reason(reason: str | None) -> bool:
    return str(reason or "").strip() in OPENING_CONSTITUTION_EXCLUDE_REASONS


def constitution_excluded_ids(selection: dict[str, Any] | None) -> set[str]:
    """Ids currently typed-excluded by the opening constitution."""
    if not isinstance(selection, dict):
        return set()
    out: set[str] = set()
    rationales = (
        selection.get("exclude_rationales")
        if isinstance(selection.get("exclude_rationales"), dict)
        else {}
    )
    for row in selection.get("excluded_segment_ids") or []:
        if isinstance(row, dict):
            sid = str(row.get("segment_id") or "").strip()
            reason = str(row.get("reason") or rationales.get(sid) or "").strip()
        else:
            sid = str(row or "").strip()
            reason = str(rationales.get(sid) or "").strip()
        if sid and is_opening_constitution_exclude_reason(reason):
            out.add(sid)
    for sid, reason in rationales.items():
        key = str(sid or "").strip()
        if key and is_opening_constitution_exclude_reason(str(reason or "")):
            out.add(key)
    return out


def air_order_integrity_cfg() -> dict[str, Any]:
    mastering = merged_config().get("mastering") or {}
    cfg = mastering.get("air_order_integrity") if isinstance(mastering, dict) else None
    return dict(cfg) if isinstance(cfg, dict) else {}


def _cfg_int(key: str, default: int) -> int:
    try:
        return int(air_order_integrity_cfg().get(key, default))
    except (TypeError, ValueError):
        return default


def _cfg_bool(key: str, default: bool) -> bool:
    val = air_order_integrity_cfg().get(key, default)
    if isinstance(val, bool):
        return val
    if isinstance(val, str):
        return val.strip().lower() in {"1", "true", "yes", "on"}
    return default


def _resolve_policy(
    ctx: RunContext | None,
    policy: dict[str, Any] | None,
    *,
    selection: dict[str, Any] | None = None,
    starts: dict[str, int] | None = None,
) -> dict[str, Any] | None:
    if policy is not None:
        return policy
    if ctx is not None:
        return resolve_air_order_policy(ctx, selection=selection, starts=starts)
    return None


def opening_window_ms(
    ctx: RunContext | None = None,
    policy: dict[str, Any] | None = None,
) -> int:
    pol = _resolve_policy(ctx, policy)
    if pol is not None:
        return policy_value(pol, "opening_window_ms", DEFAULT_OPENING_WINDOW_MS)
    return _cfg_int("opening_window_ms", DEFAULT_OPENING_WINDOW_MS)


def opening_air_slots(
    ctx: RunContext | None = None,
    policy: dict[str, Any] | None = None,
) -> int:
    pol = _resolve_policy(ctx, policy)
    if pol is not None:
        return policy_value(pol, "opening_air_slots", DEFAULT_OPENING_AIR_SLOTS)
    return _cfg_int("opening_air_slots", DEFAULT_OPENING_AIR_SLOTS)


def opening_body_start_index(
    ctx: RunContext | None = None,
    policy: dict[str, Any] | None = None,
) -> int:
    pol = _resolve_policy(ctx, policy)
    if pol is not None:
        return policy_value(pol, "opening_body_start_index", DEFAULT_OPENING_BODY_START_INDEX)
    return _cfg_int("opening_body_start_index", DEFAULT_OPENING_BODY_START_INDEX)


def reverse_jump_margin_ms(
    ctx: RunContext | None = None,
    policy: dict[str, Any] | None = None,
) -> int:
    pol = _resolve_policy(ctx, policy)
    if pol is not None:
        return policy_value(pol, "reverse_jump_margin_ms", DEFAULT_REVERSE_JUMP_MARGIN_MS)
    return _cfg_int("reverse_jump_margin_ms", DEFAULT_REVERSE_JUMP_MARGIN_MS)


def count_opening_by_family(policy: dict[str, Any] | None) -> bool:
    if policy is None:
        return _cfg_bool("count_opening_by_family", True)
    val = policy.get("count_opening_by_family")
    if isinstance(val, bool):
        return val
    return _cfg_bool("count_opening_by_family", True)


def block_ranking_on_critical() -> bool:
    return _cfg_bool("block_ranking_on_critical", False)


def invalidate_mix_on_order_change() -> bool:
    return _cfg_bool("invalidate_mix_on_order_change", False)


def block_publish_on_critical() -> bool:
    return _cfg_bool("block_publish_on_critical", True)


def invalidate_edl_on_order_change() -> bool:
    return _cfg_bool("invalidate_edl_on_order_change", True)


def invalidate_transitions_on_order_change() -> bool:
    return _cfg_bool("invalidate_transitions_on_order_change", True)


def resolved_segment_starts(ctx: RunContext) -> dict[str, int]:
    starts: dict[str, int] = {}
    for rel in ("segments/boundaries.json", "segments/segments.json", "segments/manifest.json"):
        if not ctx.artifact_exists(rel):
            continue
        try:
            doc = ctx.read_json(rel)
        except Exception:
            continue
        rows: list[Any] = []
        if isinstance(doc, dict):
            rows = list(doc.get("boundaries") or doc.get("segments") or [])
        for row in rows:
            if not isinstance(row, dict) or not row.get("segment_id"):
                continue
            try:
                starts[str(row["segment_id"])] = int(
                    row.get("start_ms") or row.get("source_start_ms") or 0
                )
            except (TypeError, ValueError):
                continue
    return starts


def resolved_segment_spans(ctx: RunContext) -> dict[str, dict[str, Any]]:
    """segment_id → {start_ms, end_ms, text, speaker_id} from boundaries/manifest."""
    spans: dict[str, dict[str, Any]] = {}
    for rel in ("segments/boundaries.json", "segments/segments.json", "segments/manifest.json"):
        if not ctx.artifact_exists(rel):
            continue
        try:
            doc = ctx.read_json(rel)
        except Exception:
            continue
        rows: list[Any] = []
        if isinstance(doc, dict):
            rows = list(doc.get("boundaries") or doc.get("segments") or [])
        for row in rows:
            if not isinstance(row, dict) or not row.get("segment_id"):
                continue
            sid = str(row["segment_id"])
            try:
                start = int(row.get("start_ms") or row.get("source_start_ms") or 0)
                end = int(row.get("end_ms") or row.get("source_end_ms") or start)
            except (TypeError, ValueError):
                continue
            spans[sid] = {
                "start_ms": start,
                "end_ms": end,
                "text": str(row.get("text") or ""),
                "speaker_id": str(row.get("speaker_id") or row.get("speaker") or ""),
            }
    return spans


def _fill_span_text_from_words(
    spans: dict[str, dict[str, Any]],
    words: list[dict[str, Any]] | None,
) -> None:
    """Mutate spans in place: fill empty text from G0 words when available."""
    if not words or not spans:
        return
    from interview_mux.gap_vo_prior_context import _word_token

    for span in spans.values():
        if str(span.get("text") or "").strip():
            continue
        try:
            start = int(span.get("start_ms") or 0)
            end = int(span.get("end_ms") or 0)
        except (TypeError, ValueError):
            continue
        toks = [
            _word_token(w)
            for w in words
            if isinstance(w, dict)
            and int(w.get("end_ms") or 0) > start
            and int(w.get("start_ms") or 0) < end
            and _word_token(w)
        ]
        if toks:
            span["text"] = " ".join(toks)


def _load_transcript_words(ctx: RunContext) -> list[dict[str, Any]]:
    for rel in (
        "operator/transcript_corrected.json",
        "transcript/full.json",
        "transcripts/full.json",
    ):
        if not ctx.artifact_exists(rel):
            continue
        try:
            doc = ctx.read_json(rel)
        except Exception:
            continue
        if isinstance(doc, dict):
            words = doc.get("words")
            if isinstance(words, list) and words:
                return [w for w in words if isinstance(w, dict)]
    return []


def incomplete_seam_pairs(
    spans: dict[str, dict[str, Any]],
    *,
    on_air: set[str] | None = None,
    words: list[dict[str, Any]] | None = None,
) -> list[tuple[str, str]]:
    """Chronological (A,B) pairs where B's open finishes A's clause (incomplete seam).

    When ``on_air`` is set, only pairs where A is on-air are returned (B may be
    off-air — caller decides whether to pull B or drop A).
    """
    from interview_mux.gap_vo_prior_context import (
        SOURCE_ADJACENT_COMPLETES_MAX_GAP_MS,
        source_adjacent_completes,
        source_adjacent_completes_at,
    )

    if not spans:
        return []
    if words:
        _fill_span_text_from_words(spans, words)
    chrono = sorted(
        spans.keys(),
        key=lambda sid: (int(spans[sid].get("start_ms") or 0), sid),
    )
    pairs: list[tuple[str, str]] = []
    for i in range(len(chrono) - 1):
        a_id = chrono[i]
        b_id = chrono[i + 1]
        if on_air is not None and a_id not in on_air:
            continue
        a = spans[a_id]
        b = spans[b_id]
        try:
            a_end = int(a.get("end_ms") or 0)
            b_start = int(b.get("start_ms") or 0)
        except (TypeError, ValueError):
            continue
        gap = b_start - a_end
        if gap > SOURCE_ADJACENT_COMPLETES_MAX_GAP_MS or gap < -500:
            continue
        a_spk = str(a.get("speaker_id") or "")
        b_spk = str(b.get("speaker_id") or "")
        same = (not a_spk or not b_spk) or a_spk == b_spk
        # Allow tight diarization flips on the completing phrase.
        if not same and gap > SOURCE_ADJACENT_COMPLETES_MAX_GAP_MS:
            continue
        prev_text = str(a.get("text") or "")
        later_text = str(b.get("text") or "")
        completes = False
        if prev_text.strip() and later_text.strip():
            completes = source_adjacent_completes(
                prev_text,
                later_text,
                max(0, gap),
                same_speaker=same or gap <= SOURCE_ADJACENT_COMPLETES_MAX_GAP_MS,
            )
        elif words:
            completes = source_adjacent_completes_at(
                words,
                a_end,
                max_gap_ms=SOURCE_ADJACENT_COMPLETES_MAX_GAP_MS,
            )
        if not completes:
            continue
        pairs.append((a_id, b_id))
    return pairs


def blank_seam_completion_ids(
    ctx: RunContext,
    selection: dict[str, Any],
    ordered: list[str],
    spans: dict[str, dict[str, Any]],
    *,
    words: list[dict[str, Any]] | None = None,
) -> set[str]:
    """Off-air seam completions that are blank or unusable: never pulled back.

    The seam repair pulled seg_063 (360 ms, excluded as blank by
    selection_order_sanitize) back behind seg_062 on every selection commit.
    Under the seat freeze that addition turned every producer's omit-only
    delta into a reorder, the gate refused the whole write, and the blank was
    dropped again (``drop_blank_under_freeze``). The overlap repair's retire of
    an absorbed id lost the same way on every attempt, so the selection kept an
    id the EDL had merged away and no EDL rebuild could match it (exec_041
    seg_078 behind seg_063; ISSUES 195).
    """
    on_air = {str(s) for s in ordered if s}
    try:
        pairs = incomplete_seam_pairs(spans, on_air=on_air, words=words)
    except Exception:
        return set()
    candidates = {str(b) for _a, b in pairs if str(b) not in on_air}
    if not candidates:
        return set()
    reasons: dict[str, str] = {}
    for row in (selection or {}).get("excluded_segment_ids") or []:
        if isinstance(row, dict):
            reasons[str(row.get("segment_id") or "")] = str(row.get("reason") or "")
    rationales = (selection or {}).get("exclude_rationales")
    if isinstance(rationales, dict):
        for sid, why in rationales.items():
            reasons.setdefault(str(sid), str(why or ""))
    out: set[str] = set()
    for sid in sorted(candidates):
        if "blank_or_unusable" in reasons.get(sid, ""):
            out.add(sid)
            continue
        try:
            from interview_mux.artifact_repairs import _segment_is_blank_or_unusable

            if _segment_is_blank_or_unusable(ctx, sid):
                out.add(sid)
        except Exception:
            pass
    return out


def repair_incomplete_seam_order(
    ordered: list[str],
    spans: dict[str, dict[str, Any]],
    *,
    words: list[dict[str, Any]] | None = None,
    blocked_ids: set[str] | None = None,
) -> tuple[list[str], list[dict[str, Any]]]:
    """Ensure incomplete-seam pairs keep A immediately before B on air.

    - If A and B are both on-air but B precedes A (or they are non-adjacent),
      move so A immediately precedes B.
    - If A is on-air and B is missing, pull B onto air right after A.
    - Never pull ``blocked_ids`` (opening constitution excludes) back onto air.
    """
    base = [str(s) for s in ordered if str(s).strip()]
    if len(base) < 1 or not spans:
        return base, []
    blocked = {str(s) for s in (blocked_ids or set()) if str(s).strip()}
    on_air = set(base)
    pairs = incomplete_seam_pairs(spans, on_air=on_air, words=words)
    if not pairs:
        return base, []
    new_order = list(base)
    actions: list[dict[str, Any]] = []
    for a_id, b_id in pairs:
        if a_id not in new_order:
            continue
        if b_id in blocked:
            continue
        if b_id not in new_order:
            # Pull completing segment onto air immediately after A.
            ia = new_order.index(a_id)
            new_order = new_order[: ia + 1] + [b_id] + new_order[ia + 1 :]
            actions.append(
                {
                    "action": "incomplete_seam_pull_completion",
                    "code": "incomplete_seam_order",
                    "after_segment_id": a_id,
                    "before_segment_id": b_id,
                }
            )
            continue
        ia = new_order.index(a_id)
        ib = new_order.index(b_id)
        if ia + 1 == ib:
            continue
        # Remove B, then place it immediately after A (recompute A's index).
        without_b = [s for s in new_order if s != b_id]
        ia2 = without_b.index(a_id)
        # If B was before A, also ensure A is not left after other material that
        # belonged between them — place B right after A.
        new_order = without_b[: ia2 + 1] + [b_id] + without_b[ia2 + 1 :]
        actions.append(
            {
                "action": "incomplete_seam_adjacency",
                "code": "incomplete_seam_order",
                "after_segment_id": a_id,
                "before_segment_id": b_id,
                "was_index_a": ia,
                "was_index_b": ib,
            }
        )
    return new_order, actions


def letter_split_family(sid: str, ordered: list[str] | None = None) -> list[str]:
    """Parent + all letter-split siblings present in ordered (or just parent+sid)."""
    parent = _parent_seg_id(sid)
    key = str(sid or "").strip()
    if not key:
        return []
    if ordered:
        stem = parent
        out = [s for s in ordered if s == stem or (_RECUT_FRAG_RE.match(s or "") and s.startswith(stem))]
        if out:
            return out
    if key == parent:
        return [key]
    if _RECUT_FRAG_RE.match(key):
        return [parent, key]
    return [key]


def pair_source_gap_ms(
    after_id: str,
    before_id: str,
    starts: dict[str, int] | None,
) -> int | None:
    if not starts:
        return None
    a = resolved_source_start_ms(after_id, starts)
    b = resolved_source_start_ms(before_id, starts)
    if a is None or b is None:
        return None
    return int(b) - int(a)


def opening_tape_segment_ids(
    ordered: list[str],
    starts: dict[str, int] | None,
    *,
    window_ms: int | None = None,
    policy: dict[str, Any] | None = None,
    ctx: RunContext | None = None,
) -> set[str]:
    window = (
        window_ms
        if window_ms is not None
        else opening_window_ms(ctx=ctx, policy=policy)
    )
    out: set[str] = set()
    if not starts:
        return out
    for sid in ordered:
        start = resolved_source_start_ms(sid, starts)
        if start is not None and int(start) < window:
            out.add(str(sid))
    return out


def family_air_positions(order: list[str]) -> dict[str, int]:
    """Air position of each id counted in parent families, not split children.

    ``opening_body_start_index`` (3) means "the fourth thing on air". An intro
    cut into eight letter children put its own fourth piece at index 3, so
    transitions and bridges inside the intro read as landing on late opening
    tape and were pruned or linted (exec_017, ISSUES 166). Every child takes
    the position of its family's first appearance.
    """
    out: dict[str, int] = {}
    family_pos: dict[str, int] = {}
    for sid in order:
        fam = _parent_seg_id(sid)
        if fam not in family_pos:
            family_pos[fam] = len(family_pos)
        out[sid] = family_pos[fam]
    return out


def opening_tape_airs_late(speech: list[str], opening_ids: set[str]) -> bool:
    """True when opening-window tape airs after the episode has moved on.

    "Late" counts the non-opening speech clips already aired, not the raw clip
    index. An intro cut into many short pieces fills the first quarter of the
    clip list on its own; by index its last pieces read as "late" although the
    episode opens with them in tape order (exec_017: seg_001d..k at indices
    0-7 of 24 set cut_integrity to 0.5, below the catastrophic floor;
    ISSUES 164). A cold-open hook before the intro stays allowed.
    """
    if not speech or not opening_ids:
        return False
    threshold = max(1, int(len(speech) * 0.25))
    aired_elsewhere = 0
    for sid in speech:
        if sid in opening_ids:
            if aired_elsewhere >= threshold:
                return True
        else:
            aired_elsewhere += 1
    return False


def _guest_first_open_established(
    ordered: list[str],
    starts: dict[str, int],
    *,
    policy: dict[str, Any] | None = None,
    ctx: RunContext | None = None,
) -> bool:
    """True when the episode opens with non-host-intro material but host intro airs later."""
    if not ordered or not starts or len(ordered) < 2:
        return False
    window = opening_window_ms(ctx=ctx, policy=policy)
    opening_parents: dict[str, int] = {}
    for sid in ordered:
        start = resolved_source_start_ms(sid, starts)
        if start is None or int(start) >= window:
            continue
        parent = _parent_seg_id(sid)
        opening_parents[parent] = min(int(opening_parents.get(parent, start)), int(start))
    if not opening_parents:
        return False
    host_parent = min(opening_parents, key=lambda p: opening_parents[p])
    first_parent = _parent_seg_id(ordered[0])
    if first_parent == host_parent:
        return False
    return any(_parent_seg_id(s) == host_parent and idx > 0 for idx, s in enumerate(ordered))


def reverse_tape_jump_violations(
    ctx: RunContext | None,
    ordered: list[str],
    *,
    starts: dict[str, int] | None = None,
    reorder_pairs: set[tuple[str, str]] | None = None,
    policy: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    if starts is None and ctx is not None:
        starts = resolved_segment_starts(ctx)
    if not starts:
        return []
    margin = reverse_jump_margin_ms(ctx=ctx, policy=policy)
    declared = reorder_pairs or set()
    violations: list[dict[str, Any]] = []
    for i in range(len(ordered) - 1):
        after_id = str(ordered[i])
        before_id = str(ordered[i + 1])
        if (after_id, before_id) in declared:
            continue
        gap = pair_source_gap_ms(after_id, before_id, starts)
        if gap is None or gap >= -margin:
            continue
        violations.append(
            {
                "code": "mid_arc_reverse_jump",
                "severity": "critical",
                "after_segment_id": after_id,
                "before_segment_id": before_id,
                "source_gap_ms": gap,
                "air_index": i + 1,
                "message": (
                    f"Reverse tape jump: {after_id} -> {before_id} "
                    f"(source_gap_ms={gap})"
                ),
            }
        )
    return violations


def _opening_family_first_indices(
    ordered: list[str],
    opening_ids: set[str],
) -> list[tuple[str, int, list[str]]]:
    """Return [(parent_id, first_air_index, fragment_ids)] sorted by first index."""
    by_parent: dict[str, list[tuple[int, str]]] = {}
    for idx, sid in enumerate(ordered):
        if sid not in opening_ids:
            continue
        parent = _parent_seg_id(sid)
        by_parent.setdefault(parent, []).append((idx, sid))
    out: list[tuple[str, int, list[str]]] = []
    for parent, rows in by_parent.items():
        rows.sort(key=lambda r: r[0])
        first_idx = rows[0][0]
        frags = [sid for _, sid in rows]
        out.append((parent, first_idx, frags))
    out.sort(key=lambda row: row[1])
    return out


def late_opening_cluster_violations(
    ctx: RunContext | None,
    ordered: list[str],
    *,
    starts: dict[str, int] | None = None,
    policy: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    if starts is None and ctx is not None:
        starts = resolved_segment_starts(ctx)
    if not starts or not ordered:
        return []
    pol = _resolve_policy(ctx, policy, starts=starts)
    opening_ids = opening_tape_segment_ids(
        ordered, starts, policy=pol, ctx=ctx
    )
    if not opening_ids:
        return []
    early_slots = opening_air_slots(ctx=ctx, policy=pol)
    violations: list[dict[str, Any]] = []
    guest_first = _guest_first_open_established(
        ordered, starts, policy=pol, ctx=ctx
    )
    by_family = count_opening_by_family(pol)

    if by_family:
        families = _opening_family_first_indices(ordered, opening_ids)
        for ordinal, (parent, first_idx, frags) in enumerate(families, start=1):
            if guest_first and first_idx >= 1:
                violations.append(
                    {
                        "code": "late_opening_cluster",
                        "severity": "critical",
                        "parent_segment_id": parent,
                        "segment_ids": frags,
                        "family_first_index": first_idx,
                        "family_ordinal": ordinal,
                        "air_index": first_idx,
                        "message": (
                            f"Opening-tape cluster {frags[:4]} after guest-first open "
                            f"at index {first_idx}"
                        ),
                    }
                )
            elif not guest_first and ordinal > early_slots:
                violations.append(
                    {
                        "code": "late_opening_cluster",
                        "severity": "critical",
                        "parent_segment_id": parent,
                        "segment_ids": frags,
                        "family_first_index": first_idx,
                        "family_ordinal": ordinal,
                        "air_index": first_idx,
                        "message": (
                            f"Opening-tape cluster {frags[:4]} airs late at index {first_idx}"
                        ),
                    }
                )
        return violations

    for idx, sid in enumerate(ordered):
        if sid not in opening_ids:
            continue
        if guest_first and idx >= 1:
            family = letter_split_family(sid, ordered)
            violations.append(
                {
                    "code": "late_opening_cluster",
                    "severity": "critical",
                    "segment_ids": family,
                    "air_index": idx,
                    "message": (
                        f"Opening-tape cluster {family[:4]} after guest-first open at index {idx}"
                    ),
                }
            )
        elif not guest_first and idx >= early_slots:
            family = letter_split_family(sid, ordered)
            violations.append(
                {
                    "code": "late_opening_cluster",
                    "severity": "critical",
                    "segment_ids": family,
                    "air_index": idx,
                    "message": (
                        f"Opening-tape cluster {family[:4]} airs late at index {idx}"
                    ),
                }
            )
    # Dedupe by family stem
    seen_stems: set[str] = set()
    deduped: list[dict[str, Any]] = []
    for v in violations:
        stem = _parent_seg_id(str((v.get("segment_ids") or [""])[0]))
        if stem in seen_stems:
            continue
        seen_stems.add(stem)
        deduped.append(v)
    return deduped


def chapter_opening_mask_violations(
    ctx: RunContext | None,
    selection: dict[str, Any],
    *,
    starts: dict[str, int] | None = None,
    policy: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Flag opening-tape segments assigned to late-body chapters."""
    if starts is None and ctx is not None:
        starts = resolved_segment_starts(ctx)
    if not starts:
        return []
    pol = _resolve_policy(ctx, policy, selection=selection, starts=starts)
    ordered = [str(s) for s in (selection.get("ordered_segment_ids") or []) if s]
    opening_ids = opening_tape_segment_ids(
        ordered, starts, policy=pol, ctx=ctx
    )
    if not opening_ids:
        return []
    chapters = selection.get("chapters") or []
    if not isinstance(chapters, list) or len(chapters) < 2:
        return []
    pos = family_air_positions(ordered)
    late_chapter_indices = set(range(max(0, len(chapters) // 2), len(chapters)))
    violations: list[dict[str, Any]] = []
    for ch_idx, ch in enumerate(chapters):
        if not isinstance(ch, dict) or ch_idx not in late_chapter_indices:
            continue
        members = [str(s) for s in (ch.get("segment_ids") or []) if str(s) in opening_ids]
        if not members:
            continue
        late_member = max(members, key=lambda s: pos.get(s, -1))
        if pos.get(late_member, 0) >= opening_body_start_index(ctx=ctx, policy=pol):
            violations.append(
                {
                    "code": "chapter_opening_mask",
                    "severity": "warn",
                    "chapter_id": ch.get("chapter_id"),
                    "segment_ids": members[:8],
                    "message": (
                        f"Opening-tape segments in late chapter "
                        f"{ch.get('chapter_id') or ch_idx}: {members[:4]}"
                    ),
                }
            )
    return violations


def collect_violations(
    ctx: RunContext,
    selection: dict[str, Any],
    *,
    starts: dict[str, int] | None = None,
    policy: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    ordered = [str(s) for s in (selection.get("ordered_segment_ids") or []) if s]
    if starts is None:
        starts = resolved_segment_starts(ctx)
    pol = _resolve_policy(ctx, policy, selection=selection, starts=starts)
    reorder_pairs: set[tuple[str, str]] = set()
    if ctx.artifact_exists("understanding/reorder_bridges.json"):
        try:
            bridges = ctx.read_json("understanding/reorder_bridges.json")
            # The file's key is "pairs"; "bridges" was never written, so every
            # declared reorder pair was ignored (ISSUES 151).
            rows = (bridges.get("pairs") or bridges.get("bridges")) if isinstance(bridges, dict) else []
            for row in rows or []:
                if not isinstance(row, dict):
                    continue
                a = str(row.get("after_segment_id") or "")
                b = str(row.get("before_segment_id") or "")
                if a and b:
                    reorder_pairs.add((a, b))
        except Exception:
            pass
    out: list[dict[str, Any]] = []
    out.extend(
        reverse_tape_jump_violations(
            ctx, ordered, starts=starts, reorder_pairs=reorder_pairs, policy=pol
        )
    )
    out.extend(
        late_opening_cluster_violations(ctx, ordered, starts=starts, policy=pol)
    )
    out.extend(
        chapter_opening_mask_violations(
            ctx, selection, starts=starts, policy=pol
        )
    )
    return out


def _exclude_segments(
    selection: dict[str, Any],
    drop_ids: list[str],
    *,
    reason: str,
) -> dict[str, Any]:
    out = dict(selection)
    drop_set = {str(s) for s in drop_ids if s}
    if not drop_set:
        return out
    ordered = [str(s) for s in (out.get("ordered_segment_ids") or []) if s and str(s) not in drop_set]
    out["ordered_segment_ids"] = ordered
    excl = list(out.get("excluded_segment_ids") or [])
    have = {
        str(r.get("segment_id") if isinstance(r, dict) else r)
        for r in excl
    }
    for sid in drop_ids:
        if sid in have:
            continue
        excl.append({"segment_id": sid, "reason": reason})
        have.add(sid)
    out["excluded_segment_ids"] = excl
    rationales = (
        dict(out.get("exclude_rationales") or {})
        if isinstance(out.get("exclude_rationales"), dict)
        else {}
    )
    for sid in drop_ids:
        rationales[sid] = reason
    out["exclude_rationales"] = rationales
    keep = set(ordered)
    for ch in out.get("chapters") or []:
        if isinstance(ch, dict):
            ch["segment_ids"] = [str(x) for x in (ch.get("segment_ids") or []) if str(x) in keep]
    return out


def intentional_cold_open_intent(
    selection: dict[str, Any],
    ctx: RunContext | None = None,
) -> str | None:
    """Return declared cold-open segment id when Mode B intent is present."""
    if not isinstance(selection, dict):
        return None
    native = str(selection.get("native_cold_open_segment_id") or "").strip()
    if native:
        return native
    cold = selection.get("cold_open")
    if isinstance(cold, dict):
        kind = str(cold.get("kind") or "").strip().lower()
        if kind and kind not in {"none", "", "null"}:
            head = str(
                cold.get("segment_id")
                or cold.get("open_segment_id")
                or (selection.get("ordered_segment_ids") or [None])[0]
                or ""
            ).strip()
            return head or None
    if ctx is not None and ctx.artifact_exists("mastering/mastering_plan.json"):
        try:
            plan = ctx.read_json("mastering/mastering_plan.json")
        except Exception:
            plan = None
        if isinstance(plan, dict):
            cold = plan.get("cold_open")
            if isinstance(cold, dict):
                kind = str(cold.get("kind") or "").strip().lower()
                if kind and kind not in {"none", "", "null"}:
                    head = str(
                        cold.get("segment_id")
                        or cold.get("open_segment_id")
                        or (selection.get("ordered_segment_ids") or [None])[0]
                        or ""
                    ).strip()
                    return head or None
    return None


def _opening_families_chrono(
    ordered: list[str],
    opening_ids: set[str],
    starts: dict[str, int],
) -> list[tuple[str, list[str]]]:
    """[(parent, frags_on_air sorted by source start)] earliest family first."""
    by_parent: dict[str, list[str]] = {}
    for sid in ordered:
        if sid not in opening_ids:
            continue
        parent = _parent_seg_id(sid)
        by_parent.setdefault(parent, []).append(sid)

    def _fam_start(parent: str, frags: list[str]) -> int:
        vals = [resolved_source_start_ms(s, starts) for s in frags]
        vals = [int(v) for v in vals if v is not None]
        if vals:
            return min(vals)
        parent_start = resolved_source_start_ms(parent, starts)
        return int(parent_start) if parent_start is not None else 10**12

    rows = [
        (parent, sorted(frags, key=lambda s: (resolved_source_start_ms(s, starts) or 0, s)))
        for parent, frags in by_parent.items()
    ]
    rows.sort(key=lambda row: (_fam_start(row[0], row[1]), row[0]))
    return rows


def _clear_constitution_excludes_for(
    selection: dict[str, Any],
    restored: set[str],
) -> dict[str, Any]:
    """Drop sticky constitution excludes for ids we intentionally put back on air."""
    if not restored:
        return selection
    out = dict(selection)
    excl = []
    for row in out.get("excluded_segment_ids") or []:
        sid = str(row.get("segment_id") if isinstance(row, dict) else row)
        reason = (
            str(row.get("reason") or "")
            if isinstance(row, dict)
            else str((out.get("exclude_rationales") or {}).get(sid) or "")
        )
        if sid in restored and is_opening_constitution_exclude_reason(reason):
            continue
        excl.append(row)
    out["excluded_segment_ids"] = excl
    rationales = (
        dict(out.get("exclude_rationales") or {})
        if isinstance(out.get("exclude_rationales"), dict)
        else {}
    )
    for sid in restored:
        if is_opening_constitution_exclude_reason(str(rationales.get(sid) or "")):
            rationales.pop(sid, None)
    out["exclude_rationales"] = rationales
    return out


def repair_opening_tape_integrity(
    ctx: RunContext,
    selection: dict[str, Any],
    *,
    mode: str = "prepend",
    starts: dict[str, int] | None = None,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Convergent opening projection; return (selection, actions).

    Mode A (default / accident guest-first): chronological host-first opening
    prefix capped at ``opening_air_slots``; surplus families typed-excluded as
    ``opening_slot_overflow``.

    Mode B (declared cold open): keep declared head; typed-exclude violating
    opening families (especially late earliest-tape host intro) as
    ``opening_skipped_duplicate``.

    ``drop_if_guest_first`` remains an explicit override that excludes late
    opening clusters under guest-first instead of projecting.
    """
    out = dict(selection)
    ordered = [str(s) for s in (out.get("ordered_segment_ids") or []) if s]
    if not ordered:
        return out, []
    if starts is None:
        starts = resolved_segment_starts(ctx)
    if not starts:
        return out, []
    pol = _resolve_policy(ctx, None, selection=out, starts=starts)
    actions: list[dict[str, Any]] = []
    opening_ids = opening_tape_segment_ids(
        ordered, starts, policy=pol, ctx=ctx
    )
    if not opening_ids:
        return out, []

    guest_first = _guest_first_open_established(
        ordered, starts, policy=pol, ctx=ctx
    )
    late = late_opening_cluster_violations(
        ctx, ordered, starts=starts, policy=pol
    )
    cold_head = intentional_cold_open_intent(out, ctx)
    mode_b = bool(cold_head)

    def _family_sorted(family: list[str]) -> list[str]:
        return sorted(
            family,
            key=lambda s: (
                resolved_source_start_ms(s, starts) or 0,
                ordered.index(s) if s in ordered else 0,
            ),
        )

    # Explicit legacy override: drop late clusters under guest-first.
    if mode == "drop_if_guest_first" and guest_first:
        to_drop: list[str] = []
        for v in late:
            to_drop.extend(str(s) for s in (v.get("segment_ids") or []) if s)
        if not to_drop and guest_first:
            for idx, sid in enumerate(ordered):
                if sid in opening_ids and idx >= 1:
                    to_drop.extend(letter_split_family(sid, ordered))
        if to_drop:
            out = _exclude_segments(out, to_drop, reason="opening_skipped_duplicate")
            actions.append(
                {
                    "action": "exclude_opening_cluster",
                    "reason": "opening_skipped_duplicate",
                    "ids": to_drop[:12],
                }
            )
        return out, actions

    families = _opening_families_chrono(ordered, opening_ids, starts)
    if not families:
        return out, []
    slots = max(1, int(opening_air_slots(ctx=ctx, policy=pol)))

    if mode_b:
        # Mode B: keep intentional cold-open head; exclude violating opening tape.
        head = cold_head if cold_head in ordered else ordered[0]
        head_parent = _parent_seg_id(head)
        head_family = [s for s in ordered if _parent_seg_id(s) == head_parent]
        to_drop_set: set[str] = set()
        earliest_parent = families[0][0]
        if earliest_parent != head_parent:
            for parent, frags in families:
                if parent == earliest_parent:
                    to_drop_set.update(frags)
                    actions.append(
                        {
                            "action": "exclude_opening_cluster",
                            "reason": "opening_skipped_duplicate",
                            "ids": frags[:12],
                            "mode": "cold_open",
                        }
                    )
                    break
        remaining = [
            (p, f)
            for p, f in families
            if p != head_parent and not set(f) <= to_drop_set
        ]
        keep_budget = max(0, slots - 1)
        if len(remaining) > keep_budget:
            for parent, frags in reversed(remaining[keep_budget:]):
                to_drop_set.update(frags)
                actions.append(
                    {
                        "action": "exclude_opening_cluster",
                        "reason": "opening_slot_overflow",
                        "ids": frags[:12],
                        "mode": "cold_open",
                    }
                )
        trial_order = [s for s in ordered if s not in to_drop_set]
        if trial_order and _parent_seg_id(trial_order[0]) != head_parent:
            rest = [s for s in trial_order if s not in set(head_family)]
            trial_order = _family_sorted(
                [s for s in head_family if s in trial_order]
            ) + rest
            actions.append(
                {
                    "action": "preserve_cold_open_head",
                    "ids": [head][:12],
                }
            )
        still_late = late_opening_cluster_violations(
            ctx, trial_order, starts=starts, policy=pol
        )
        for v in still_late:
            frags = [str(s) for s in (v.get("segment_ids") or []) if s]
            frags = [s for s in frags if _parent_seg_id(s) != head_parent]
            if not frags:
                continue
            to_drop_set.update(frags)
            actions.append(
                {
                    "action": "exclude_opening_cluster",
                    "reason": "opening_skipped_duplicate",
                    "ids": frags[:12],
                    "mode": "cold_open",
                }
            )
        if to_drop_set or (trial_order != ordered):
            new_order = [s for s in trial_order if s not in to_drop_set]
            if new_order and _parent_seg_id(new_order[0]) != head_parent:
                hf = [s for s in new_order if _parent_seg_id(s) == head_parent]
                rest = [s for s in new_order if s not in set(hf)]
                new_order = _family_sorted(hf) + rest
            out["ordered_segment_ids"] = new_order
            if to_drop_set:
                overflow_ids = {
                    sid
                    for act in actions
                    if act.get("reason") == "opening_slot_overflow"
                    for sid in (act.get("ids") or [])
                }
                skip_ids = [s for s in to_drop_set if s not in overflow_ids]
                over_ids = [s for s in to_drop_set if s in overflow_ids]
                if skip_ids:
                    out = _exclude_segments(
                        out, skip_ids, reason="opening_skipped_duplicate"
                    )
                if over_ids:
                    out = _exclude_segments(
                        out, over_ids, reason="opening_slot_overflow"
                    )
            kept = set(new_order)
            out = _clear_constitution_excludes_for(out, kept)
        return out, actions

    # Mode A: chronological host-first prefix; typed-exclude surplus.
    keep_families = families[:slots]
    surplus = families[slots:]
    keep_ids: list[str] = []
    for _parent, frags in keep_families:
        keep_ids.extend(frags)
    keep_set = set(keep_ids)
    drop_ids: list[str] = []
    for _parent, frags in surplus:
        drop_ids.extend(frags)
    prefix = _family_sorted(keep_ids)
    drop_set = set(drop_ids)
    body = [s for s in ordered if s not in keep_set and s not in drop_set]
    new_order = prefix + body
    changed = new_order != ordered or bool(drop_ids)

    if not changed:
        if guest_first and keep_families:
            host_parent = keep_families[0][0]
            if _parent_seg_id(ordered[0]) != host_parent:
                host_frags = [s for s in ordered if _parent_seg_id(s) == host_parent]
                rest = [s for s in ordered if s not in set(host_frags)]
                new_order = _family_sorted(host_frags) + rest
                changed = True
                actions.append(
                    {
                        "action": "prepend_opening_family",
                        "ids": _family_sorted(host_frags)[:12],
                        "mode": "host_first",
                    }
                )
        if not changed:
            # Even when order already host-first within budget, no-op.
            if not late and not guest_first:
                return out, actions
            # late with host-first head but still violations → fall through via surplus empty
            if not late:
                return out, actions

    # Always project when late or guest_first or surplus, even if order coincidentally ok.
    if late or guest_first or drop_ids or new_order != ordered:
        if prefix and (not ordered or prefix[0] != ordered[0] or new_order != ordered):
            if not any(a.get("action") == "prepend_opening_family" for a in actions):
                actions.append(
                    {
                        "action": "prepend_opening_family",
                        "ids": prefix[:12],
                        "mode": "host_first",
                    }
                )
        if drop_ids:
            actions.append(
                {
                    "action": "exclude_opening_cluster",
                    "reason": "opening_slot_overflow",
                    "ids": drop_ids[:12],
                    "mode": "host_first",
                }
            )
            out = _exclude_segments(out, drop_ids, reason="opening_slot_overflow")
        out["ordered_segment_ids"] = new_order
        out = _clear_constitution_excludes_for(out, set(prefix))

        still = late_opening_cluster_violations(
            ctx, list(out.get("ordered_segment_ids") or []), starts=starts, policy=pol
        )
        if still:
            extra: list[str] = []
            for v in still:
                extra.extend(str(s) for s in (v.get("segment_ids") or []) if s)
            if keep_families:
                host_parent = keep_families[0][0]
                extra = [s for s in extra if _parent_seg_id(s) != host_parent]
            if extra:
                out = _exclude_segments(out, extra, reason="opening_slot_overflow")
                actions.append(
                    {
                        "action": "exclude_opening_cluster",
                        "reason": "opening_slot_overflow",
                        "ids": extra[:12],
                        "mode": "host_first_sweep",
                    }
                )
                out["ordered_segment_ids"] = [
                    s
                    for s in (out.get("ordered_segment_ids") or [])
                    if s not in set(extra)
                ]
    return out, actions



# A closing line the episode is allowed to end on even when it sits earlier on
# the tape than the material before it (exec_054 seg_063 "Mohan, thank you
# very much", ISSUES entry 68).
_FAREWELL_RE = re.compile(
    r"\b(thank you (very |so )?much|thanks (so much )?for (joining|coming|being|talking|your time)"
    r"|all (our|the) best|goodbye|good bye|see you next)\b",
    flags=re.IGNORECASE,
)


def is_farewell_text(text: str) -> bool:
    """True when the closing words of ``text`` read as a sign-off."""
    words = str(text or "").split()
    return bool(words) and bool(_FAREWELL_RE.search(" ".join(words[-60:])))


def closing_segment_ids(ctx: Any, ordered: list[str]) -> set[str]:
    """The final on-air id when its own text is a farewell, else empty."""
    ids = [str(s) for s in ordered if str(s).strip()]
    if not ids or not ctx.artifact_exists("segments/manifest.json"):
        return set()
    try:
        manifest = ctx.read_json("segments/manifest.json")
    except Exception:
        return set()
    rows = (manifest or {}).get("segments") or [] if isinstance(manifest, dict) else []
    last = ids[-1]
    for row in rows:
        if isinstance(row, dict) and str(row.get("segment_id") or "") == last:
            return {last} if is_farewell_text(str(row.get("text") or "")) else set()
    return set()


def pull_mid_arc_reverse_jumps(
    ordered: list[str],
    source_start_ms: dict[str, int] | None,
    *,
    guest_first: bool | None = None,
    margin_ms: int | None = None,
    protect_final_ids: set[str] | None = None,
) -> tuple[list[str], list[str], list[str]]:
    """Scan all adjacent pairs; prepend earlier-tape families on reverse jump.

    Opening-window host-intro families are prepended (not dropped) even when
    guest/company tape already opened — prefer the native host intro on air.
    """
    base = [str(s) for s in ordered if str(s).strip()]
    if len(base) < 2 or not source_start_ms:
        return base, [], []
    if guest_first is None:
        guest_first = _guest_first_open_established(base, source_start_ms)
    _ = guest_first  # retained for call-site compatibility; no longer drops
    margin = reverse_jump_margin_ms() if margin_ms is None else margin_ms
    to_drop: list[str] = []
    moved: list[str] = []
    new_order = list(base)
    changed = True
    while changed:
        changed = False
        for i in range(len(new_order) - 1):
            after_id = new_order[i]
            before_id = new_order[i + 1]
            gap = pair_source_gap_ms(after_id, before_id, source_start_ms)
            if gap is None or gap >= -margin:
                continue
            # A farewell deliberately placed last is an ending, not a mid-arc
            # reverse jump; moving it forward left material after the goodbye.
            if (
                protect_final_ids
                and before_id in protect_final_ids
                and i + 1 == len(new_order) - 1
            ):
                continue
            family = letter_split_family(before_id, new_order)
            # Always prepend earlier-tape family before the later clip.
            rest = [s for s in new_order if s not in set(family)]
            insert_at = rest.index(after_id) if after_id in rest else 0
            family_sorted = sorted(
                family,
                key=lambda s: (
                    resolved_source_start_ms(s, source_start_ms) or 0,
                    base.index(s) if s in base else 0,
                ),
            )
            new_order = rest[:insert_at] + family_sorted + rest[insert_at:]
            for sid in family_sorted:
                if sid not in moved:
                    moved.append(sid)
            changed = True
            break
    return new_order, moved, to_drop


def repair_air_order_integrity(
    ctx: RunContext,
    selection: dict[str, Any],
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Full integrity repair pass."""
    out = dict(selection)
    actions: list[dict[str, Any]] = []
    starts = resolved_segment_starts(ctx)
    pol = _resolve_policy(ctx, None, selection=out, starts=starts)
    ordered = [str(s) for s in (out.get("ordered_segment_ids") or []) if s]
    margin = reverse_jump_margin_ms(ctx=ctx, policy=pol)
    pulled, moved, dropped = pull_mid_arc_reverse_jumps(
        ordered,
        starts or None,
        margin_ms=margin,
        protect_final_ids=closing_segment_ids(ctx, ordered),
    )
    if moved or dropped or pulled != ordered:
        if dropped:
            out = _exclude_segments(out, dropped, reason="opening_skipped_duplicate")
            actions.append({"action": "mid_arc_exclude", "ids": dropped[:12]})
        else:
            out["ordered_segment_ids"] = pulled
            actions.append({"action": "mid_arc_pull", "ids": moved[:12]})
            # Clear stale duplicate excludes for ids we prepended back on air.
            restored = set(moved)
            if restored:
                excl = []
                for row in out.get("excluded_segment_ids") or []:
                    sid = str(row.get("segment_id") if isinstance(row, dict) else row)
                    reason = (
                        str(row.get("reason") or "")
                        if isinstance(row, dict)
                        else str((out.get("exclude_rationales") or {}).get(sid) or "")
                    )
                    if sid in restored and reason == "opening_skipped_duplicate":
                        continue
                    excl.append(row)
                out["excluded_segment_ids"] = excl
                rationales = (
                    dict(out.get("exclude_rationales") or {})
                    if isinstance(out.get("exclude_rationales"), dict)
                    else {}
                )
                for sid in restored:
                    if rationales.get(sid) == "opening_skipped_duplicate":
                        rationales.pop(sid, None)
                out["exclude_rationales"] = rationales
    out, opening_actions = repair_opening_tape_integrity(ctx, out, starts=starts)
    actions.extend(opening_actions)

    # Incomplete-seam adjacency: if B finishes A's clause on tape, air must keep
    # A immediately before B (or pull B onto air). Not free-form chapter reshape.
    # Never pull opening-constitution excludes back onto air.
    blocked = constitution_excluded_ids(out)
    spans = resolved_segment_spans(ctx)
    words = _load_transcript_words(ctx)
    ordered_after = [str(s) for s in (out.get("ordered_segment_ids") or []) if s]
    # A blank completion is dropped again the moment it lands; pulling it is
    # the oscillation that refused every write under the freeze (ISSUES 195).
    blocked = set(blocked) | blank_seam_completion_ids(
        ctx, out, ordered_after, spans, words=words or None
    )
    seam_order, seam_actions = repair_incomplete_seam_order(
        ordered_after,
        spans,
        words=words or None,
        blocked_ids=blocked,
    )
    if seam_actions and seam_order != ordered_after:
        out["ordered_segment_ids"] = seam_order
        actions.extend(seam_actions)
        # Clear stale excludes for ids we pulled back — never constitution reasons.
        pulled_ids = {
            str(a.get("before_segment_id") or "")
            for a in seam_actions
            if a.get("action") == "incomplete_seam_pull_completion"
        }
        pulled_ids.discard("")
        pulled_ids -= blocked
        if pulled_ids:
            excl = []
            for row in out.get("excluded_segment_ids") or []:
                sid = str(row.get("segment_id") if isinstance(row, dict) else row)
                reason = (
                    str(row.get("reason") or "")
                    if isinstance(row, dict)
                    else str((out.get("exclude_rationales") or {}).get(sid) or "")
                )
                if sid in pulled_ids and not is_opening_constitution_exclude_reason(reason):
                    continue
                excl.append(row)
            out["excluded_segment_ids"] = excl
            rationales = (
                dict(out.get("exclude_rationales") or {})
                if isinstance(out.get("exclude_rationales"), dict)
                else {}
            )
            for sid in pulled_ids:
                if not is_opening_constitution_exclude_reason(
                    str(rationales.get(sid) or "")
                ):
                    rationales.pop(sid, None)
            out["exclude_rationales"] = rationales
        # Seam may reorder kept ids — re-project opening so criticals stay cleared.
        out, reopen = repair_opening_tape_integrity(ctx, out, starts=starts)
        actions.extend(reopen)
    return out, actions


def critical_violations(violations: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [v for v in violations if str(v.get("severity") or "").lower() == "critical"]


def write_air_order_integrity_report(
    ctx: RunContext,
    *,
    violations: list[dict[str, Any]],
    actions: list[dict[str, Any]] | None = None,
    stage: str = "full_master_ranking",
    repaired: bool = False,
    resolved_policy: dict[str, Any] | None = None,
) -> dict[str, Any]:
    critical = critical_violations(violations)
    policy = resolved_policy
    if policy is None and ctx.artifact_exists("master/selection.json"):
        try:
            sel = ctx.read_json("master/selection.json")
            policy = resolve_air_order_policy(
                ctx, selection=sel if isinstance(sel, dict) else None
            )
        except Exception:
            policy = resolve_air_order_policy(ctx)
    doc: dict[str, Any] = {
        "version": 1,
        "stage": stage,
        "repaired": bool(repaired),
        "recorded_at": datetime.now(timezone.utc).isoformat(),
        "violation_count": len(violations),
        "critical_count": len(critical),
        "violations": violations[:32],
        "actions": list(actions or [])[:32],
        "ok": len(critical) == 0,
    }
    if policy:
        doc["resolved_policy"] = policy
    ctx.write_json(INTEGRITY_REL, doc, stage_key=stage)
    # A critical finding refuses ranking only under block_ranking_on_critical
    # (off by default, ISSUES 185); otherwise it is advisory and logging it as
    # an error made a healthy run read as failed (exec_036 seg_038 -> seg_027).
    level = "error" if block_ranking_on_critical() else "warning"
    for v in critical:
        ctx.log(
            v.get("message") or str(v.get("code") or "air_order_integrity_violation"),
            level=level,
            stage=stage,
            detail=v,
        )
        _append_operator_log(ctx, v, stage=stage)
        _emit_homunculus_issue(ctx, v, stage=stage)
    return doc


def _append_operator_log(ctx: RunContext, violation: dict[str, Any], *, stage: str) -> None:
    line = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "stage": stage,
        "violation": violation,
    }
    path = ctx.path(*OPERATOR_LOG_REL.split("/"))
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(line, ensure_ascii=False) + "\n")


def _emit_homunculus_issue(ctx: RunContext, violation: dict[str, Any], *, stage: str) -> None:
    try:
        from interview_mux.homunculus.issues import ingest_catch

        ingest_catch(
            ctx,
            kind="air_order_integrity_violation",
            source=stage,
            evidence=violation,
            stage_id=stage,
        )
    except Exception:
        pass


def audit_and_report(
    ctx: RunContext,
    *,
    stage: str = "full_master_ranking",
    repair: bool = True,
) -> dict[str, Any]:
    if not ctx.artifact_exists("master/selection.json"):
        return {"ok": True, "violation_count": 0, "critical_count": 0}
    sel = ctx.read_json("master/selection.json")
    if not isinstance(sel, dict):
        return {"ok": False, "violation_count": 0, "critical_count": 0}
    actions: list[dict[str, Any]] = []
    previous = dict(sel)
    if repair:
        sel, actions = repair_air_order_integrity(ctx, sel)
        if actions:
            on_selection_order_changed(ctx, source=f"audit_and_report:{stage}", previous=previous, current=sel)
            ctx.write_json("master/selection.json", sel, stage_key=stage)
    policy = resolve_air_order_policy(ctx, selection=sel)
    violations = collect_violations(ctx, sel, policy=policy)
    return write_air_order_integrity_report(
        ctx,
        violations=violations,
        actions=actions,
        stage=stage,
        repaired=bool(actions),
        resolved_policy=policy,
    )


def on_selection_order_changed(
    ctx: RunContext,
    *,
    source: str,
    previous: dict[str, Any] | None = None,
    current: dict[str, Any] | None = None,
    id_fold: bool = False,
) -> list[str]:
    """Invalidate stale transitions/EDL when air order changes."""
    notes: list[str] = []
    if current is None and ctx.artifact_exists("master/selection.json"):
        current = ctx.read_json("master/selection.json")
    if not isinstance(current, dict):
        return notes
    prev_ids = [
        str(s) for s in ((previous or {}).get("ordered_segment_ids") or []) if s
    ]
    cur_ids = [str(s) for s in (current.get("ordered_segment_ids") or []) if s]
    if prev_ids == cur_ids:
        return notes
    # Pillar B: under seat freeze, order-change cascade must not thrash seats/timeline
    # without meta-gate allow. Refuse → keep consumers; only note seating stale.
    seat_frozen = False
    seat_allow_cascade = True
    try:
        from interview_mux.seat_authority import (
            freeze_blocks_selection_layup_invalidate,
            request_seat_rewrite,
        )

        seat_frozen = freeze_blocks_selection_layup_invalidate(ctx)
        if seat_frozen:
            decision = request_seat_rewrite(
                ctx,
                proposed_delta={"ops": [], "order_change": True, "source": source},
                reason=f"selection_order_changed:{source}",
                symptoms=["order_change"],
            )
            if not decision.get("allow"):
                notes.append("seat_freeze_blocked_layup_invalidate")
                notes.append("seat_freeze_blocked_order_cascade")
                seat_allow_cascade = False
    except Exception:
        # Fail-closed when freeze may be active / unknown
        try:
            from interview_mux.seat_authority import soft_freeze_active, hard_freeze_active

            frozen = bool(soft_freeze_active(ctx) or hard_freeze_active(ctx))
        except Exception:
            frozen = True
        if frozen:
            notes.append("seat_freeze_cascade_fail_closed")
            seat_frozen = True
            seat_allow_cascade = False
    if not seat_allow_cascade:
        # Still bump seating stale for mix honesty, but skip transitions/EDL/mix clears
        # and layup wipe — imperfect order beats thrash under freeze.
        if prev_ids != cur_ids:
            def _bump_seating(meta: dict[str, Any]) -> None:
                meta["assembly_seating_generation"] = int(
                    meta.get("assembly_seating_generation") or 0
                ) + 1
                meta["assembly_seating_stale"] = True
                meta["assembly_seating_stale_reason"] = (
                    f"order_change_frozen:{source}"
                )[:200]

            try:
                ctx.mutate_run_meta(_bump_seating)
                notes.append("assembly_seating_stale")
            except Exception:
                pass
        return notes
    if invalidate_transitions_on_order_change() and ctx.is_done("transitions"):
        marker = ctx.final_path(".stage_done", "transitions")
        if marker.is_file():
            marker.unlink()
            notes.append("cleared_stage_done:transitions")
        if ctx.artifact_exists("master/transitions.json"):
            try:
                from interview_mux.artifact_repairs import prune_reverse_jump_transitions
                from interview_mux.gap_framing import prune_transitions_outside_selection

                tr = ctx.read_json("master/transitions.json")
                if isinstance(tr, dict):
                    pruned = prune_transitions_outside_selection(tr, cur_ids)
                    pruned, rnotes = prune_reverse_jump_transitions(ctx, pruned, cur_ids)
                    if int(pruned.get("outside_selection_pruned_count") or 0) or rnotes:
                        from interview_mux.transition_vo import persist_transitions_doc

                        persist_transitions_doc(
                            ctx, pruned, stage_key="transitions"
                        )
                        notes.extend(rnotes or [])
                        notes.append("prune_reverse_jump_transitions")
            except Exception:
                pass
    if invalidate_edl_on_order_change() and ctx.is_done("edl"):
        marker = ctx.final_path(".stage_done", "edl")
        if marker.is_file():
            marker.unlink()
            notes.append("cleared_stage_done:edl")
    if invalidate_mix_on_order_change() and ctx.is_done("mix"):
        edl_path = ctx.final_path("master", "edl.json")
        asm_path = ctx.final_path("master", "assembly.wav")
        stale_mix = False
        if edl_path.is_file() and asm_path.is_file():
            try:
                stale_mix = asm_path.stat().st_mtime < edl_path.stat().st_mtime
            except OSError:
                stale_mix = True
        if stale_mix or "cleared_stage_done:edl" in notes:
            marker = ctx.final_path(".stage_done", "mix")
            if marker.is_file():
                marker.unlink()
                notes.append("cleared_stage_done:mix")
    # T6: order change always marks mix/preview seating stale (generation bump).
    if prev_ids != cur_ids:
        def _bump_seating(meta: dict[str, Any]) -> None:
            meta["assembly_seating_generation"] = int(
                meta.get("assembly_seating_generation") or 0
            ) + 1
            meta["assembly_seating_stale"] = True
            meta["assembly_seating_stale_reason"] = f"order_change:{source}"[:200]

        try:
            ctx.mutate_run_meta(_bump_seating)
            notes.append("assembly_seating_stale")
        except Exception:
            pass
        # An id fold, and junction's own exclude, keep junction's done marker.
        # Clearing it reopens the exclude and burns the invoke cap. EDL and
        # mix still rebuild from the new order.
        keep_junction = id_fold or str(source or "").startswith("junction_snip_qa")
        fold_keep = {"junction_snip_qa"} if keep_junction else set()
        for sid in ("mix", "assembly_preview", "junction_snip_qa"):
            if sid in fold_keep:
                notes.append(f"kept_stage_done:{sid}")
                continue
            marker = ctx.final_path(".stage_done", sid)
            if marker.is_file():
                marker.unlink()
                notes.append(f"cleared_stage_done:{sid}")
        try:
            from interview_mux.transition_vo import clear_transitions_pair_freeze
            from interview_mux.seat_authority import soft_freeze_active

            # Dual-freeze: do not blindly clear pair freeze under seat freeze
            if soft_freeze_active(ctx) and "seat_freeze_blocked_layup_invalidate" in notes:
                notes.append("kept_transitions_pair_freeze:seat_freeze")
            elif clear_transitions_pair_freeze(ctx):
                notes.append("cleared_transitions_pair_freeze")
        except Exception:
            pass
    if prev_ids != cur_ids and not id_fold:
        try:
            from interview_mux.delivery_guardrails import fingerprints_match_checkpoint

            fp_unchanged = fingerprints_match_checkpoint(ctx)
            src_l = str(source or "").lower()
            if "seat_freeze_blocked_layup_invalidate" in notes:
                notes.append("skipped_layup_invalidate:seat_freeze")
            elif "junction_snip_qa" in src_l:
                notes.append("skipped_layup_invalidate:junction_source")
            elif fp_unchanged:
                notes.append("skipped_layup_invalidate:fingerprint_unchanged")
            elif ctx.is_done("nugget_layup_compose") or ctx.artifact_exists(
                "understanding/nugget_layup_plan.json"
            ) or ctx.artifact_exists("mastering/nugget_layup_plan.json"):
                from interview_mux.homunculus.agenda import invalidate_downstream

                try:
                    invalidate_downstream(ctx, "nugget_layup_compose")
                    notes.append("invalidated_downstream:nugget_layup_compose")
                except RuntimeError as exc:
                    if "delivery epoch locked" in str(exc).lower():
                        lock_reason = str(exc)[:400]

                        def _mark(meta: dict[str, Any]) -> None:
                            meta["needs_operator"] = True
                            meta["needs_operator_stage"] = "delivery_epoch_unlock"
                            meta["needs_operator_reason"] = lock_reason

                        ctx.mutate_run_meta(_mark)
                        notes.append("needs_operator:delivery_epoch_unlock")
                    else:
                        raise
        except Exception:
            pass
    if notes:
        ctx.log(
            f"selection order changed ({source}): " + ", ".join(notes[:6]),
            level="warning",
            stage=source.split(":")[0] if ":" in source else source,
        )
    return notes


def lint_hard_keep_family_errors(
    ctx: RunContext,
    selection: dict[str, Any],
) -> list[str]:
    """Parent hard-keep with partial letter-split family mid-arc."""
    from interview_mux.hard_keep import hard_keep_segment_ids

    keeps = hard_keep_segment_ids(ctx, selection=selection)
    if not keeps:
        return []
    ordered = [str(s) for s in (selection.get("ordered_segment_ids") or []) if s]
    excl = {
        str(r.get("segment_id") if isinstance(r, dict) else r)
        for r in (selection.get("excluded_segment_ids") or [])
    }
    starts = resolved_segment_starts(ctx)
    pol = _resolve_policy(ctx, None, selection=selection, starts=starts)
    errors: list[str] = []
    pos = {sid: idx for idx, sid in enumerate(ordered)}
    for sid in keeps:
        if sid in ordered or sid in excl:
            continue
        family = letter_split_family(sid, ordered)
        present = [m for m in family if m in ordered]
        if not present:
            errors.append(f"hard-keep segment {sid} must appear in ordered_segment_ids")
            continue
        if len(present) < len([m for m in family if m in set(ordered) | excl]):
            late = max(present, key=lambda s: pos.get(s, 0))
            if pos.get(late, 0) >= opening_body_start_index(ctx=ctx, policy=pol):
                start = resolved_source_start_ms(late, starts)
                if start is not None and start < opening_window_ms(ctx=ctx, policy=pol):
                    errors.append(
                        f"hard-keep family partial mid-arc: {sid} -> {present[:4]}"
                    )
    return errors

"""Deterministic boundary shard merge and timeline normalization."""

from __future__ import annotations

from typing import Any

from interview_mux.config import merged_config
from interview_mux.segment_timeline import sort_segments_by_start_ms


def boundary_collate_cfg(cfg: dict[str, Any] | None = None) -> dict[str, Any]:
    analysis = (cfg or merged_config()).get("analysis") or {}
    itr = analysis.get("artifact_issue_triage") or {}
    seg = analysis.get("segmentation") or {}
    merge_default = int(seg.get("boundary_merge_threshold_ms") or itr.get("boundary_merge_threshold_ms") or 500)
    return {
        "snap_tolerance_ms": int(itr.get("boundary_snap_tolerance_ms") or 500),
        "merge_threshold_ms": merge_default,
        "coarse_partition_min_children": int(itr.get("boundary_coarse_partition_min_children") or 2),
        "coarse_coverage_ratio": float(itr.get("boundary_coarse_coverage_ratio") or 0.85),
        "min_segment_duration_ms": int(seg.get("min_segment_duration_ms") or 4000),
        "max_segment_duration_ms": seg.get("max_segment_duration_ms"),
        "granularity": str(seg.get("default_granularity") or "fine"),
    }


def _row_span(row: dict[str, Any]) -> int:
    start = row.get("start_ms")
    end = row.get("end_ms")
    if start is None or end is None:
        return 0
    return max(0, int(end) - int(start))


def _interval_union(intervals: list[tuple[int, int]]) -> list[tuple[int, int]]:
    if not intervals:
        return []
    merged: list[tuple[int, int]] = []
    for start, end in sorted(intervals):
        if end <= start:
            continue
        if not merged or start > merged[-1][1]:
            merged.append((start, end))
        else:
            prev_s, prev_e = merged[-1]
            merged[-1] = (prev_s, max(prev_e, end))
    return merged


def _coverage_ratio(parent_start: int, parent_end: int, child_intervals: list[tuple[int, int]]) -> float:
    parent_span = parent_end - parent_start
    if parent_span <= 0:
        return 0.0
    clipped: list[tuple[int, int]] = []
    for start, end in child_intervals:
        clip_start = max(start, parent_start)
        clip_end = min(end, parent_end)
        if clip_end > clip_start:
            clipped.append((clip_start, clip_end))
    covered = sum(end - start for start, end in _interval_union(clipped))
    return covered / parent_span


def _snap_bucket(value: int, tolerance: int) -> int:
    if tolerance <= 0:
        return value
    return int(round(value / tolerance) * tolerance)


def _rows_equivalent_within_tolerance(
    left: dict[str, Any],
    right: dict[str, Any],
    *,
    snap_tolerance_ms: int,
) -> bool:
    if left.get("start_ms") is None or left.get("end_ms") is None:
        return False
    if right.get("start_ms") is None or right.get("end_ms") is None:
        return False
    ls, le = int(left["start_ms"]), int(left["end_ms"])
    rs, re = int(right["start_ms"]), int(right["end_ms"])
    return (
        _snap_bucket(ls, snap_tolerance_ms) == _snap_bucket(rs, snap_tolerance_ms)
        and _snap_bucket(le, snap_tolerance_ms) == _snap_bucket(re, snap_tolerance_ms)
    )


def _prefer_boundary_row(candidate: dict[str, Any], incumbent: dict[str, Any]) -> dict[str, Any]:
    cand_span = _row_span(candidate)
    inc_span = _row_span(incumbent)
    if cand_span and inc_span:
        if cand_span < inc_span:
            return candidate
        if cand_span > inc_span:
            return incumbent
    if candidate.get("proposed_split_reason") and not incumbent.get("proposed_split_reason"):
        return candidate
    return incumbent


def _dedupe_rows_within_tolerance(
    rows: list[dict[str, Any]],
    *,
    snap_tolerance_ms: int,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    kept: list[dict[str, Any]] = []
    applied: list[dict[str, Any]] = []
    for row in sort_segments_by_start_ms(rows):
        duplicate_idx = None
        for idx, existing in enumerate(kept):
            if _rows_equivalent_within_tolerance(row, existing, snap_tolerance_ms=snap_tolerance_ms):
                duplicate_idx = idx
                break
        if duplicate_idx is None:
            kept.append(dict(row))
            continue
        preferred = _prefer_boundary_row(row, kept[duplicate_idx])
        if preferred is row:
            kept[duplicate_idx] = dict(row)
        applied.append(
            {
                "action": "dedupe_snap_tolerance",
                "segment_id": row.get("segment_id"),
                "start_ms": row.get("start_ms"),
                "end_ms": row.get("end_ms"),
            }
        )
    return kept, applied


def _drop_coarse_dominated_rows(
    rows: list[dict[str, Any]],
    *,
    snap_tolerance_ms: int,
    coarse_partition_min_children: int,
    coarse_coverage_ratio: float,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    applied: list[dict[str, Any]] = []
    kept: list[dict[str, Any]] = []
    for row in rows:
        if row.get("start_ms") is None or row.get("end_ms") is None:
            continue
        start = int(row["start_ms"])
        end = int(row["end_ms"])
        span = end - start
        if span <= 0:
            continue
        children: list[tuple[int, int]] = []
        for other in rows:
            if other is row:
                continue
            if other.get("start_ms") is None or other.get("end_ms") is None:
                continue
            other_start = int(other["start_ms"])
            other_end = int(other["end_ms"])
            other_span = other_end - other_start
            if other_span <= 0 or other_span >= span:
                continue
            if other_start >= start - snap_tolerance_ms and other_end <= end + snap_tolerance_ms:
                children.append((other_start, other_end))
        if len(children) < coarse_partition_min_children:
            kept.append(row)
            continue
        if _coverage_ratio(start, end, children) >= coarse_coverage_ratio:
            applied.append(
                {
                    "action": "drop_coarse_dominated",
                    "segment_id": row.get("segment_id"),
                    "start_ms": start,
                    "end_ms": end,
                }
            )
            continue
        kept.append(row)
    return kept, applied


def _phrase_in_span(
    words: list[dict[str, Any]] | None,
    start_ms: int,
    end_ms: int,
    *,
    head: bool = False,
) -> str:
    toks: list[str] = []
    for word in words or []:
        if not isinstance(word, dict):
            continue
        try:
            at = int(float(word.get("start_ms") or 0))
        except (TypeError, ValueError):
            continue
        if at < start_ms or at >= end_ms:
            continue
        tok = str(word.get("text") or word.get("word") or "").strip()
        if tok:
            toks.append(tok)
    # The words after a cut are its head; the words before it are its tail.
    return " ".join(toks[:16] if head else toks[-16:])


def _finished_sentence_gap(
    words: list[dict[str, Any]] | None,
    prev_end_ms: int,
    next_start_ms: int,
) -> bool:
    """True when the gap is a finished sentence, so the 80 ms split stays.

    An unfinished line may still be folded into the next segment. With no
    words on hand, the older fold rules stay in place.
    """
    if not words or next_start_ms <= prev_end_ms:
        return False
    prev = _phrase_in_span(words, max(0, prev_end_ms - 30_000), prev_end_ms + 1)
    nxt = _phrase_in_span(words, next_start_ms, next_start_ms + 30_000, head=True)
    if not prev or not nxt:
        return False
    from interview_mux.gap_vo_prior_context import concept_cut_allowed

    return concept_cut_allowed(prev, nxt)


def _snap_monotonic_timeline(
    rows: list[dict[str, Any]],
    *,
    snap_tolerance_ms: int,
    words: list[dict[str, Any]] | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    applied: list[dict[str, Any]] = []
    result: list[dict[str, Any]] = []
    prev_end = 0
    for row in sort_segments_by_start_ms(rows):
        if row.get("start_ms") is None or row.get("end_ms") is None:
            continue
        start = int(row["start_ms"])
        end = int(row["end_ms"])
        if end <= start:
            applied.append({"action": "drop_zero_length", "segment_id": row.get("segment_id")})
            continue
        if result:
            if abs(start - prev_end) <= snap_tolerance_ms:
                if _finished_sentence_gap(words, prev_end, start):
                    pass
                else:
                    if start != prev_end:
                        applied.append(
                            {
                                "action": "snap_start_to_prev_end",
                                "segment_id": row.get("segment_id"),
                                "from_ms": start,
                                "to_ms": prev_end,
                            }
                        )
                    start = prev_end
            elif start < prev_end:
                applied.append(
                    {
                        "action": "trim_overlap_to_prev_end",
                        "segment_id": row.get("segment_id"),
                        "from_ms": start,
                        "to_ms": prev_end,
                    }
                )
                start = prev_end
        if end <= start:
            applied.append({"action": "drop_zero_length_after_snap", "segment_id": row.get("segment_id")})
            continue
        normalized = dict(row)
        normalized["start_ms"] = start
        normalized["end_ms"] = end
        result.append(normalized)
        prev_end = end
    return result, applied


def _merge_micro_boundaries(
    rows: list[dict[str, Any]],
    *,
    merge_threshold_ms: int,
    min_segment_duration_ms: int = 4000,
    granularity: str = "fine",
    same_speaker_pause_ms: int = 2500,
    max_segment_duration_ms: int | None = None,
    words: list[dict[str, Any]] | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    del max_segment_duration_ms
    applied: list[dict[str, Any]] = []
    if not rows:
        return [], applied
    merged: list[dict[str, Any]] = [dict(rows[0])]
    for row in rows[1:]:
        span = _row_span(row)
        prev = merged[-1] if merged else None
        prev_spk = str((prev or {}).get("speaker_id") or "").strip()
        row_spk = str(row.get("speaker_id") or "").strip()
        pause_ms = 10_000
        if prev is not None:
            try:
                pause_ms = int(row.get("start_ms") or 0) - int(prev.get("end_ms") or 0)
            except (TypeError, ValueError):
                pause_ms = 10_000
        same_speaker_small_pause = bool(
            prev is not None
            and prev_spk
            and row_spk
            and prev_spk == row_spk
            and 0 <= pause_ms <= same_speaker_pause_ms
        )
        if granularity == "fine" and span >= min_segment_duration_ms and not same_speaker_small_pause:
            merged.append(dict(row))
            continue
        if prev is not None and _finished_sentence_gap(
            words, int(prev.get("end_ms") or 0), int(row.get("start_ms") or 0)
        ):
            merged.append(dict(row))
            continue
        if merged and (span < merge_threshold_ms or same_speaker_small_pause):
            prev = merged[-1]
            prev["end_ms"] = max(int(prev.get("end_ms", 0)), int(row.get("end_ms", 0)))
            applied.append(
                {
                    "action": "merge_same_speaker_boundary"
                    if same_speaker_small_pause
                    else "merge_micro_boundary",
                    "segment_id": row.get("segment_id"),
                }
            )
            continue
        merged.append(dict(row))
    return merged, applied


def _renumber_segment_ids(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    renumbered: list[dict[str, Any]] = []
    for idx, row in enumerate(rows, start=1):
        normalized = dict(row)
        normalized["segment_id"] = f"seg_{idx:03d}"
        renumbered.append(normalized)
    return renumbered


def _clip_rows_to_window(
    rows: list[dict[str, Any]],
    *,
    window_start: int,
    window_end: int,
) -> list[dict[str, Any]]:
    """Keep rows overlapping [window_start, window_end] and clip to the window."""
    clipped: list[dict[str, Any]] = []
    for row in rows:
        if row.get("start_ms") is None or row.get("end_ms") is None:
            continue
        start = int(row["start_ms"])
        end = int(row["end_ms"])
        if end <= window_start or start >= window_end:
            continue
        normalized = dict(row)
        normalized["start_ms"] = max(start, window_start)
        normalized["end_ms"] = min(end, window_end)
        if int(normalized["end_ms"]) > int(normalized["start_ms"]):
            clipped.append(normalized)
    return clipped


def align_boundary_rows_to_shard(
    rows: list[dict[str, Any]],
    *,
    shard_start_ms: int | None,
    shard_end_ms: int | None,
    snap_tolerance_ms: int | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Map shard-local boundary timestamps onto absolute interview tape time."""
    applied: list[dict[str, Any]] = []
    if not rows or shard_start_ms is None or shard_end_ms is None:
        return rows, applied

    snap_tol = int(snap_tolerance_ms if snap_tolerance_ms is not None else boundary_collate_cfg()["snap_tolerance_ms"])
    shard_start = int(shard_start_ms)
    shard_end = int(shard_end_ms)
    span = shard_end - shard_start
    if span <= 0:
        return rows, applied

    valid = [
        dict(row)
        for row in rows
        if isinstance(row, dict)
        and row.get("start_ms") is not None
        and row.get("end_ms") is not None
        and int(row["end_ms"]) > int(row["start_ms"])
    ]
    if not valid:
        return [], applied

    starts = [int(r["start_ms"]) for r in valid]
    ends = [int(r["end_ms"]) for r in valid]
    min_start = min(starts)
    max_end = max(ends)

    if min_start >= shard_start - snap_tol and max_end <= shard_end + snap_tol:
        return valid, applied

    if (
        min_start <= snap_tol
        and max_end <= span + snap_tol
        and shard_start > snap_tol
    ):
        shifted = [
            {
                **row,
                "start_ms": int(row["start_ms"]) + shard_start,
                "end_ms": int(row["end_ms"]) + shard_start,
            }
            for row in valid
        ]
        applied.append(
            {
                "action": "shift_relative_to_shard_start",
                "offset_ms": shard_start,
                "shard_start_ms": shard_start,
                "shard_end_ms": shard_end,
            }
        )
        return _clip_rows_to_window(shifted, window_start=shard_start, window_end=shard_end), applied

    if min_start < shard_start - snap_tol:
        filtered = _clip_rows_to_window(
            [
                row
                for row in valid
                if int(row["end_ms"]) > shard_start + snap_tol and int(row["start_ms"]) < shard_end
            ],
            window_start=shard_start,
            window_end=shard_end,
        )
        if filtered:
            applied.append(
                {
                    "action": "clip_misscoped_to_shard_window",
                    "kept": len(filtered),
                    "dropped": len(valid) - len(filtered),
                    "shard_start_ms": shard_start,
                    "shard_end_ms": shard_end,
                }
            )
            return filtered, applied
        applied.append(
            {
                "action": "reject_misscoped_no_window_overlap",
                "shard_start_ms": shard_start,
                "shard_end_ms": shard_end,
            }
        )
        return [], applied

    clipped = _clip_rows_to_window(valid, window_start=shard_start, window_end=shard_end)
    if clipped:
        applied.append(
            {
                "action": "clip_to_shard_window",
                "shard_start_ms": shard_start,
                "shard_end_ms": shard_end,
            }
        )
    return clipped, applied


def boundary_timeline_coverage_ratio(
    rows: list[dict[str, Any]],
    *,
    interview_duration_ms: int,
) -> float:
    if interview_duration_ms <= 0 or not rows:
        return 0.0
    last_end = max(int(r.get("end_ms") or 0) for r in rows if isinstance(r, dict))
    return last_end / interview_duration_ms


def boundary_coverage_errors(
    rows: list[dict[str, Any]],
    *,
    interview_duration_ms: int,
    min_ratio: float | None = None,
    min_duration_ms: int = 60_000,
) -> list[str]:
    if interview_duration_ms <= min_duration_ms:
        return []
    ratio = boundary_timeline_coverage_ratio(rows, interview_duration_ms=interview_duration_ms)
    threshold = float(
        min_ratio
        if min_ratio is not None
        else (merged_config().get("analysis") or {}).get("boundary_timeline_coverage_min_ratio", 0.85)
    )
    if ratio >= threshold:
        return []
    pct = ratio * 100
    return [
        f"boundary timeline coverage {pct:.0f}% < {threshold * 100:.0f}% of interview "
        f"(last_end_ms vs duration_ms={interview_duration_ms})"
    ]


def reject_misscoped_shard_boundaries(
    rows: list[dict[str, Any]],
    *,
    parent_start: int,
    parent_end: int,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Drop shard rows that claim the full parent span (duplicate collapse across shards)."""
    if not rows or parent_end <= parent_start:
        return rows, []
    parent_span = parent_end - parent_start
    kept: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    seen_full_span = False
    for row in rows:
        if not isinstance(row, dict):
            continue
        start = int(row.get("start_ms") or 0)
        end = int(row.get("end_ms") or 0)
        span = max(0, end - start)
        if span >= int(parent_span * 0.95) and abs(start - parent_start) <= 1000:
            if seen_full_span:
                rejected.append({**row, "_reject_reason": "misscoped_full_parent_span"})
                continue
            seen_full_span = True
        kept.append(row)
    return kept, rejected


def collect_boundary_rows(
    *,
    shard_outputs: list[dict[str, Any]] | None = None,
    collate_artifacts: dict[str, Any] | None = None,
    cfg: dict[str, Any] | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    rows: list[dict[str, Any]] = []
    applied: list[dict[str, Any]] = []
    snap_tol = int(boundary_collate_cfg(cfg)["snap_tolerance_ms"])
    for shard in shard_outputs or []:
        if not isinstance(shard, dict):
            continue
        env = shard.get("envelope")
        artifacts = env.get("artifacts") if isinstance(env, dict) else {}
        boundaries = artifacts.get("boundaries") if isinstance(artifacts, dict) else []
        if not isinstance(boundaries, list):
            continue
        shard_rows = [dict(row) for row in boundaries if isinstance(row, dict)]
        aligned, align_actions = align_boundary_rows_to_shard(
            shard_rows,
            shard_start_ms=shard.get("start_ms"),
            shard_end_ms=shard.get("end_ms"),
            snap_tolerance_ms=snap_tol,
        )
        if align_actions:
            applied.extend(align_actions)
        rows.extend(aligned)
    if isinstance(collate_artifacts, dict):
        boundaries = collate_artifacts.get("boundaries") or []
        if isinstance(boundaries, list):
            rows.extend(dict(row) for row in boundaries if isinstance(row, dict))
    return rows, applied


def normalize_boundary_timeline(
    rows: list[dict[str, Any]],
    *,
    cfg: dict[str, Any] | None = None,
    words: list[dict[str, Any]] | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Force a monotonic boundary timeline, tolerating small timestamp drift."""
    settings = boundary_collate_cfg(cfg)
    snap_tol = int(settings["snap_tolerance_ms"])
    merge_threshold = int(settings["merge_threshold_ms"])
    min_children = int(settings["coarse_partition_min_children"])
    coverage_ratio = float(settings["coarse_coverage_ratio"])
    min_seg_ms = int(settings.get("min_segment_duration_ms") or 4000)
    granularity = str(settings.get("granularity") or "fine")

    valid = [
        dict(row)
        for row in rows
        if isinstance(row, dict)
        and row.get("start_ms") is not None
        and row.get("end_ms") is not None
        and int(row["end_ms"]) > int(row["start_ms"])
    ]
    if not valid:
        return [], []

    parent_start = min(int(r["start_ms"]) for r in valid)
    parent_end = max(int(r["end_ms"]) for r in valid)
    valid, rejected = reject_misscoped_shard_boundaries(
        valid,
        parent_start=parent_start,
        parent_end=parent_end,
    )
    applied: list[dict[str, Any]] = [
        {"action": "reject_misscoped_shard", "row": r} for r in rejected
    ]
    deduped, dedupe_actions = _dedupe_rows_within_tolerance(valid, snap_tolerance_ms=snap_tol)
    applied.extend(dedupe_actions)

    filtered, dominated_actions = _drop_coarse_dominated_rows(
        deduped,
        snap_tolerance_ms=snap_tol,
        coarse_partition_min_children=min_children,
        coarse_coverage_ratio=coverage_ratio,
    )
    applied.extend(dominated_actions)

    snapped, snap_actions = _snap_monotonic_timeline(
        filtered, snap_tolerance_ms=snap_tol, words=words
    )
    applied.extend(snap_actions)

    merged, merge_actions = _merge_micro_boundaries(
        snapped,
        merge_threshold_ms=merge_threshold,
        min_segment_duration_ms=min_seg_ms,
        granularity=granularity,
        max_segment_duration_ms=int(settings.get("max_segment_duration_ms") or 0) or None,
        words=words,
    )
    applied.extend(merge_actions)

    return _renumber_segment_ids(merged), applied


def merge_shard_boundaries(
    shard_outputs: list[dict[str, Any]],
    *,
    collate_artifacts: dict[str, Any] | None = None,
    cfg: dict[str, Any] | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Union shard (and optional collate) boundaries into one monotonic timeline."""
    rows, align_actions = collect_boundary_rows(
        shard_outputs=shard_outputs,
        collate_artifacts=collate_artifacts,
        cfg=cfg,
    )
    merged_rows, timeline_actions = normalize_boundary_timeline(rows, cfg=cfg)
    return merged_rows, align_actions + timeline_actions

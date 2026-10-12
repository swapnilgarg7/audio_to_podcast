"""A before-VO never outlives the segment the EDL omits as unplayable (ISSUES 190).

The run this guards (2026-10-11): seg_038 was a 200 ms interviewee fragment
("Right") still on the air order as must-air, with a synthesized lay-up aimed
at it. The builder seated the pickup, then omitted the fragment as unplayable
and skipped the speech clip. Narrative QC refused the EDL three times for an
orphan pickup targeting a segment not in ordered speech; removal authority
refused to take the fragment off the selection; the stage never finished.
Now the pickup is taken back with its target and both are recorded on the EDL,
so publishability does not demand a seat for a line that has no segment.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from interview_mux.publishability_boundary import _check_unseated_required_vo
from interview_mux.stages.assembly import build_flow1_edl
from run_fixtures import isolated_run_ctx

SEGMENTS = {
    "seg_037": {"segment_id": "seg_037", "speaker_id": "spk_guest", "start_ms": 960_000, "end_ms": 973_000, "text": "We moved from the bar to a snack."},
    "seg_038": {"segment_id": "seg_038", "speaker_id": "spk_guest", "start_ms": 974_000, "end_ms": 974_200, "text": "Right"},
    "seg_039": {"segment_id": "seg_039", "speaker_id": "spk_guest", "start_ms": 980_000, "end_ms": 992_000, "text": "The packaging had to change with it."},
}
ORDER = ["seg_037", "seg_038", "seg_039"]
LINE_ID = "vo_layup_seg_038"


def _line(target: str = "seg_038") -> dict:
    return {
        "line_id": LINE_ID,
        "text": "Before the packaging pivot, the bar was already being eaten as a snack.",
        "targets_segment_id": target,
        "placement": "before",
        "delivery": "synthesize",
        "required": True,
        "voice_speaker_id": "spk_host",
        "origin": "nugget_layup",
    }


def _build(tmp_path: Path, *, ctx=None, order: list[str] | None = None) -> dict:
    wav = tmp_path / f"{LINE_ID}.wav"
    wav.write_bytes(b"x")
    return build_flow1_edl(
        selection={"ordered_segment_ids": list(order or ORDER)},
        segments_by_id=SEGMENTS,
        gap_report={"interviewer_lines": [_line()]},
        resolve_vo_path=lambda _line: wav,
        vo_duration_ms=lambda _path: 2500,
        ctx=ctx,
    )


def test_the_pickup_is_omitted_with_its_unplayable_target(tmp_path: Path) -> None:
    edl = _build(tmp_path)
    speech = [c["segment_id"] for c in edl["clips"] if c.get("type") == "speech"]
    assert speech == ["seg_037", "seg_039"]
    assert edl["omitted_unplayable_segment_ids"] == ["seg_038"]
    assert [c for c in edl["clips"] if c.get("type") == "vo_pickup"] == []
    assert edl["gap_placements"] == []
    assert edl["omitted_unplayable_vo_line_ids"] == [LINE_ID]
    assert edl["warnings"]["vo_lines_unseated_unplayable_target"] == [LINE_ID]
    # No orphan: every pickup target is in ordered speech (narrative QC's rule).
    for clip in edl["clips"]:
        if clip.get("type") == "vo_pickup":
            assert clip["targets_segment_id"] in speech
    # The timeline is contiguous after the rollback.
    starts = [c["timeline_start_ms"] for c in edl["clips"]]
    assert starts == sorted(starts)
    assert edl["timeline_duration_ms"] == sum(int(c.get("duration_ms") or 0) for c in edl["clips"])


def test_a_seatable_target_keeps_its_pickup(tmp_path: Path) -> None:
    edl = _build(tmp_path, order=["seg_037", "seg_039"])
    assert [c for c in edl["clips"] if c.get("type") == "vo_pickup"] == []
    assert edl["omitted_unplayable_vo_line_ids"] == []
    long_target = dict(SEGMENTS["seg_038"], end_ms=976_500)
    wav = tmp_path / f"{LINE_ID}.wav"
    edl = build_flow1_edl(
        selection={"ordered_segment_ids": ORDER},
        segments_by_id={**SEGMENTS, "seg_038": long_target},
        gap_report={"interviewer_lines": [_line()]},
        resolve_vo_path=lambda _line: wav,
        vo_duration_ms=lambda _path: 2500,
    )
    seated = [c for c in edl["clips"] if c.get("type") == "vo_pickup"]
    assert [c["targets_segment_id"] for c in seated] == ["seg_038"]
    assert edl["omitted_unplayable_segment_ids"] == []


@pytest.fixture
def ctx(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("MUX_FORENSICS", "0")
    monkeypatch.setenv("MUX_ASSETS_ROOT", str(tmp_path))
    return isolated_run_ctx(tmp_path, "before_vo_unplayable")


def test_publishability_does_not_demand_a_seat_for_the_omitted_line(ctx, tmp_path: Path) -> None:
    edl = _build(tmp_path)
    assert _check_unseated_required_vo(ctx, edl, {"interviewer_lines": [_line()]}) == []
    # Without the EDL's omission record the same unseated line is still caught.
    bare = {"clips": [], "warnings": {}}
    out = _check_unseated_required_vo(ctx, bare, {"interviewer_lines": [_line()]})
    assert [v.code for v in out] == ["unseated_required_vo"]


def test_a_keep_removal_authority_protects_gets_the_min_air_seat(ctx, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The builder judges must-air the way the writer and removal authority do."""
    import interview_mux.removal_authority as removal_authority

    monkeypatch.setattr(removal_authority, "protected_segment_ids", lambda *a, **k: {"seg_038"})
    edl = _build(tmp_path, ctx=ctx)
    speech = {c["segment_id"]: c for c in edl["clips"] if c.get("type") == "speech"}
    assert "seg_038" in speech
    assert speech["seg_038"]["duration_ms"] >= 400
    assert "hard_keep_min_air" in str(speech["seg_038"].get("air_bound_reason") or "")
    seated = [c for c in edl["clips"] if c.get("type") == "vo_pickup"]
    assert [c["targets_segment_id"] for c in seated] == ["seg_038"]
    assert edl["omitted_unplayable_segment_ids"] == []

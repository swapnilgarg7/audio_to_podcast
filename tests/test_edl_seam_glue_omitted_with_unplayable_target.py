"""Seam glue never outlives the segment it leads into (ISSUES 193).

Nicket's exec_033 (2026-10-12): the selection aired seg_025, seg_029, seg_027,
seg_028 and transitions.json carried a spoken bridge for the reverse seam
seg_029 -> seg_027 (seg_027 is earlier on the tape). The builder seated the
bridge after seg_029, then omitted seg_027 as unplayable; the bridge stayed on
the timeline pointing at a segment not in ordered speech, narrative QC refused
the EDL three times ("does not sit between adjacent speech clips"), and the
stage hit the invoke cap. seg_027 was also must-air: removal authority refused
its drop from the selection, so no rebuild could converge.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from interview_mux.air_order import restore_protected_speech_clips, strip_orphan_seam_glue
from interview_mux.edl_narrative_qc import _speech_order, _validate_transitions
from interview_mux.stages.assembly import build_flow1_edl
from run_fixtures import isolated_run_ctx, write_fixture_json

SEGMENTS = {
    "seg_025": {"segment_id": "seg_025", "speaker_id": "spk_1", "start_ms": 0, "end_ms": 10_000, "text": "We started in gyms."},
    "seg_029": {"segment_id": "seg_029", "speaker_id": "spk_1", "start_ms": 40_000, "end_ms": 52_000, "text": "The gym crowd told us what the bar was for."},
    "seg_027": {"segment_id": "seg_027", "speaker_id": "spk_1", "start_ms": 38_040, "end_ms": 38_240, "text": "Yeah."},
    "seg_028": {"segment_id": "seg_028", "speaker_id": "spk_1", "start_ms": 60_000, "end_ms": 70_000, "text": "So the promise got clearer."},
}
ORDER = ["seg_025", "seg_029", "seg_027", "seg_028"]
TRANSITIONS = {
    "transitions": [
        {
            "after_segment_id": "seg_029",
            "before_segment_id": "seg_027",
            "text": "That early gym audience pointed to a clearer product promise.",
            "type": "topic_shift",
            "voice_speaker_id": "spk_0",
            "source_gap_ms": -13_960,
        }
    ]
}


def _build(tmp_path: Path, *, segments: dict | None = None, ctx=None) -> dict:
    wav = tmp_path / "tr_seg_029_seg_027.wav"
    wav.write_bytes(b"x")
    return build_flow1_edl(
        selection={"ordered_segment_ids": list(ORDER)},
        segments_by_id=segments or SEGMENTS,
        transitions=TRANSITIONS,
        resolve_transition_path=lambda _a, _b: wav,
        vo_duration_ms=lambda _path: 2500,
        ctx=ctx,
    )


def _transition_errors(edl: dict) -> list[str]:
    errors: list[str] = []
    _validate_transitions(TRANSITIONS, edl, _speech_order(edl), errors)
    return errors


def _assert_contiguous(edl: dict) -> None:
    starts = [c["timeline_start_ms"] for c in edl["clips"]]
    assert starts == sorted(starts)
    assert edl["timeline_duration_ms"] == sum(int(c.get("duration_ms") or 0) for c in edl["clips"])


def test_control_a_seatable_target_keeps_its_bridge_between_the_two_clips(tmp_path: Path) -> None:
    long_target = dict(SEGMENTS["seg_027"], start_ms=30_000, end_ms=36_000)
    edl = _build(tmp_path, segments={**SEGMENTS, "seg_027": long_target})
    kinds = [(c["type"], c.get("segment_id") or c.get("before_segment_id")) for c in edl["clips"] if c["type"] != "silence"]
    assert kinds == [("speech", "seg_025"), ("speech", "seg_029"), ("transition", "seg_027"), ("speech", "seg_027"), ("speech", "seg_028")]
    assert _transition_errors(edl) == []


def test_the_bridge_is_omitted_with_its_unplayable_target(tmp_path: Path) -> None:
    edl = _build(tmp_path)
    assert _speech_order(edl) == ["seg_025", "seg_029", "seg_028"]
    assert edl["omitted_unplayable_segment_ids"] == ["seg_027"]
    assert [c for c in edl["clips"] if c["type"] == "transition"] == []
    assert all(str(c.get("before_segment_id") or "") != "seg_027" for c in edl["clips"])
    assert edl["warnings"]["seam_glue_unseated_unplayable_target"] == ["transition:seg_029->seg_027"]
    assert edl["warnings"]["missing_transition_audio"] == []
    _assert_contiguous(edl)
    # The exact narrative QC rule exec_033 failed on every pass.
    assert _transition_errors(edl) == []


@pytest.fixture
def ctx(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("MUX_FORENSICS", "0")
    monkeypatch.setenv("MUX_ASSETS_ROOT", str(tmp_path))
    return isolated_run_ctx(tmp_path, "seam_glue_unplayable")


def test_a_must_air_keep_with_no_room_is_omitted_with_its_bridge(ctx, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A speech clip under the minimum never passes publishability
    (never_touch_zeroed_keep, exec_041 seg_070), so a protected keep with no
    room is omitted, its bridge leaves with it, and the selection keeps it."""
    import interview_mux.removal_authority as removal_authority
    from interview_mux.publishability_boundary import _check_zero_keeps

    monkeypatch.setattr(removal_authority, "protected_segment_ids", lambda *a, **k: {"seg_027"})
    tight = {
        **SEGMENTS,
        "seg_027": dict(SEGMENTS["seg_027"], start_ms=38_040, end_ms=38_340),
        "seg_028": dict(SEGMENTS["seg_028"], start_ms=38_340, end_ms=48_000),
    }
    edl = _build(tmp_path, segments=tight, ctx=ctx)
    assert _speech_order(edl) == ["seg_025", "seg_029", "seg_028"]
    assert edl["omitted_unplayable_segment_ids"] == ["seg_027"]
    assert [c for c in edl["clips"] if c["type"] == "transition"] == []
    assert edl["warnings"]["seam_glue_unseated_unplayable_target"] == ["transition:seg_029->seg_027"]
    assert all(c["duration_ms"] >= 400 for c in edl["clips"] if c["type"] == "speech")
    assert _check_zero_keeps(ctx, edl) == []
    assert _transition_errors(edl) == []
    _assert_contiguous(edl)


def test_the_builder_judges_must_air_under_the_proposed_order_too(ctx, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Removal authority protects under either state; the builder asks both."""
    import interview_mux.removal_authority as removal_authority

    def _protected(_ctx, *, overrides=None, on_air=None):
        if on_air is not None and "seg_027" not in on_air:
            return {"seg_027"}
        return set()

    monkeypatch.setattr(removal_authority, "protected_segment_ids", _protected)
    edl = _build(tmp_path, ctx=ctx)
    speech = {c["segment_id"]: c for c in edl["clips"] if c["type"] == "speech"}
    assert "seg_027" in speech
    assert speech["seg_027"]["duration_ms"] >= 400
    assert edl["omitted_unplayable_segment_ids"] == []
    assert _transition_errors(edl) == []


def _clip(kind: str, **kw) -> dict:
    return {"type": kind, "duration_ms": 1000, "timeline_start_ms": 0, **kw}


def test_the_writer_strips_glue_that_no_longer_joins_its_clips() -> None:
    edl = {
        "clips": [
            _clip("speech", segment_id="seg_025", source_start_ms=0, source_end_ms=1000),
            _clip("speech", segment_id="seg_029", source_start_ms=40_000, source_end_ms=41_000),
            _clip("transition", after_segment_id="seg_029", before_segment_id="seg_027"),
            _clip("silence", air_kind="chapter_music_bridge", after_segment_id="seg_029", before_segment_id="seg_027"),
            _clip("speech", segment_id="seg_028", source_start_ms=60_000, source_end_ms=61_000),
            _clip("transition", after_segment_id="seg_028", before_segment_id="seg_030"),
            _clip("speech", segment_id="seg_030", source_start_ms=70_000, source_end_ms=71_000),
        ],
        "warnings": {},
    }
    removed = strip_orphan_seam_glue(edl)
    assert removed == ["transition:seg_029->seg_027", "silence:seg_029->seg_027"]
    kinds = [(c["type"], c.get("segment_id") or c.get("before_segment_id")) for c in edl["clips"]]
    assert kinds == [("speech", "seg_025"), ("speech", "seg_029"), ("speech", "seg_028"), ("transition", "seg_030"), ("speech", "seg_030")]
    assert edl["timeline_duration_ms"] == 5000
    assert edl["warnings"]["seam_glue_unseated_unplayable_target"] == sorted(removed)
    assert _transition_errors(edl) == []
    assert strip_orphan_seam_glue(edl) == []


def test_a_restored_must_air_clip_lands_after_its_own_bridge(ctx, monkeypatch: pytest.MonkeyPatch) -> None:
    import interview_mux.removal_authority as removal_authority

    monkeypatch.setattr(removal_authority, "protected_segment_ids", lambda *a, **k: {"seg_027"})
    manifest = {"segments": [dict(SEGMENTS[s]) for s in SEGMENTS]}
    manifest["segments"][2] = dict(SEGMENTS["seg_027"], start_ms=30_000, end_ms=36_000)
    write_fixture_json(ctx, "segments/manifest.json", manifest)
    write_fixture_json(ctx, "master/selection.json", {"ordered_segment_ids": list(ORDER)})
    edl = {
        "clips": [
            _clip("speech", segment_id="seg_025", source_start_ms=0, source_end_ms=10_000),
            _clip("speech", segment_id="seg_029", source_start_ms=40_000, source_end_ms=52_000),
            _clip("silence", air_kind="after_vo"),
            _clip("transition", after_segment_id="seg_029", before_segment_id="seg_027"),
            _clip("silence", air_kind="chapter_hinge"),
            _clip("speech", segment_id="seg_028", source_start_ms=60_000, source_end_ms=70_000),
        ],
        "ordered_segment_ids": ["seg_025", "seg_029", "seg_028"],
        "omitted_unplayable_segment_ids": ["seg_027"],
    }
    assert restore_protected_speech_clips(ctx, edl) == ["seg_027"]
    kinds = [(c["type"], c.get("segment_id") or c.get("before_segment_id") or c.get("air_kind")) for c in edl["clips"]]
    assert kinds == [
        ("speech", "seg_025"),
        ("speech", "seg_029"),
        ("silence", "after_vo"),
        ("transition", "seg_027"),
        ("speech", "seg_027"),
        ("silence", "chapter_hinge"),
        ("speech", "seg_028"),
    ]
    assert edl["ordered_segment_ids"] == ORDER
    assert edl["omitted_unplayable_segment_ids"] == []
    assert _transition_errors(edl) == []
    assert strip_orphan_seam_glue(edl) == []

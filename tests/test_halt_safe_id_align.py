"""Manifest ids follow saved boundaries, and retired ids leave the flush files."""

from interview_mux.artifact_completeness import (
    aligned_manifest_segments,
    strip_retired_segment_ids,
)
from interview_mux.segment_id_remap import _restore_vo_pair


def test_aligned_manifest_keeps_the_saved_row_and_drops_a_retired_id() -> None:
    rows = [
        {"segment_id": "seg_002", "start_ms": 80, "end_ms": 4000, "speaker_id": "spk_1"},
        {"segment_id": "seg_003", "start_ms": 4080, "end_ms": 9000},
    ]
    existing = [
        {"segment_id": "seg_001", "text": "old", "type": "interviewee_answer"},
        {"segment_id": "seg_002", "text": "kept", "type": "interviewee_answer"},
    ]
    aligned = aligned_manifest_segments(rows, existing)
    assert [row["segment_id"] for row in aligned] == ["seg_002", "seg_003"]
    assert aligned[0]["text"] == "kept"
    assert aligned[0]["speaker_id"] == "spk_1"
    assert aligned[1]["start_ms"] == 4080


def test_strip_retired_ids_from_brief_narrative_and_soundscape() -> None:
    live = {"seg_002"}
    brief = strip_retired_segment_ids(
        {
            "topics": [{"segment_ids": ["seg_001", "seg_002"]}],
            "key_claims": [{"evidence_segment_ids": ["seg_009"]}],
        },
        live,
    )
    assert brief["topics"][0]["segment_ids"] == ["seg_002"]
    assert brief["key_claims"][0]["evidence_segment_ids"] == []
    narrative = strip_retired_segment_ids(
        {"chapters": [{"segment_ids": ["seg_001", "seg_002"]}]},
        live,
    )
    assert narrative["chapters"][0]["segment_ids"] == ["seg_002"]
    policy = strip_retired_segment_ids(
        {"cue_slots": [{"segment_id": "seg_001"}, {"segment_id": "seg_002"}]},
        live,
    )
    assert policy["cue_slots"] == [{"segment_id": "seg_002"}]


class _Ctx:
    def __init__(self) -> None:
        self.written: dict[str, dict] = {}
        self.logs: list[str] = []

    def write_json(self, rel: str, doc: dict, **_kwargs) -> None:
        self.written[rel] = doc

    def log(self, message: str, **_kwargs) -> None:
        self.logs.append(message)


def test_vo_pair_restores_the_file_that_landed_when_its_partner_did_not() -> None:
    ctx = _Ctx()
    gap = "understanding/gap_report.json"
    plan = "mastering/mastering_plan.json"
    before = {
        gap: {"lines": [{"targets_segment_id": "seg_001"}]},
        plan: {"clips": [{"segment_id": "seg_001"}]},
    }
    updated = [gap]
    _restore_vo_pair(
        ctx,
        {"seg_001": "seg_002"},
        before,
        updated,
        stage_key="connector_fuse_pass",
    )
    assert ctx.written[gap]["lines"][0]["targets_segment_id"] == "seg_001"
    assert gap not in updated
    assert plan not in ctx.written


def test_align_keeps_split_children_that_live_only_in_the_manifest(tmp_path) -> None:
    """CTA/NLE split children are manifest rows inside the parent's span.

    Rebuilding the manifest from the boundaries alone deleted them, and the
    ranking selection that names them was refused on every flush.
    """
    import json

    from run_fixtures import init_run_meta_for_test, isolated_run_ctx

    from interview_mux.artifact_completeness import align_manifest_ids_to_boundaries

    ctx = isolated_run_ctx(tmp_path, "split_children_align")
    init_run_meta_for_test(ctx)

    def _put(rel: str, doc: dict) -> None:
        p = ctx.path(*rel.split("/"))
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(doc))

    def _row(sid: str, start: int, end: int, **extra) -> dict:
        return {
            "segment_id": sid,
            "start_ms": start,
            "end_ms": end,
            "speaker_id": "spk_1",
            "speaker_role": "interviewee",
            "type": "interviewee_answer",
            "topic_tags": [],
            "text": sid,
            **extra,
        }

    _put(
        "segments/boundaries.json",
        {
            "version": 1,
            "boundaries": [
                {"segment_id": "seg_001", "start_ms": 0, "end_ms": 10_000},
                {"segment_id": "seg_002", "start_ms": 10_080, "end_ms": 30_000},
            ],
        },
    )
    _put(
        "segments/manifest.json",
        {
            "version": 1,
            "segments": [
                _row("seg_001", 0, 10_000),
                _row("seg_002", 10_080, 30_000, split_into=["seg_002a", "seg_002b"]),
                _row("seg_002a", 10_080, 18_000, parent_id="seg_002"),
                _row("seg_002b", 18_080, 30_000, parent_id="seg_002"),
                _row("seg_009", 0, 10_000),
            ],
        },
    )
    align_manifest_ids_to_boundaries(ctx)
    ids = [s["segment_id"] for s in ctx.read_json("segments/manifest.json")["segments"]]
    assert "seg_009" not in ids
    assert {"seg_001", "seg_002", "seg_002a", "seg_002b"} <= set(ids)

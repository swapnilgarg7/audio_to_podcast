"""An EDL clip omitted as unplayable still counts as seated.

The EDL can omit a selection id (a residual overlap trimmed under 400 ms, a
never-touch microfragment) while the selection keeps it. The drift check
discounts omitted ids; the seated checks must too, or edl and mix are
re-dispatched until the invoke cap.
"""

from __future__ import annotations

import json
from pathlib import Path

from run_fixtures import init_run_meta_for_test, isolated_run_ctx

from interview_mux.order_hash import (
    order_drift_heal_action,
    order_hashes_match,
    seatable_selection_ids,
)

SEL = {"ordered_segment_ids": ["seg_001", "seg_002", "seg_003"]}
EDL = {
    "version": 1,
    "ordered_segment_ids": ["seg_001", "seg_003"],
    "omitted_unplayable_segment_ids": ["seg_002"],
    "clips": [
        {
            "type": "speech",
            "segment_id": "seg_001",
            "source_start_ms": 0,
            "source_end_ms": 5000,
            "duration_ms": 5000,
            "timeline_start_ms": 0,
        },
        {
            "type": "speech",
            "segment_id": "seg_003",
            "source_start_ms": 9000,
            "source_end_ms": 14000,
            "duration_ms": 5000,
            "timeline_start_ms": 5000,
        },
    ],
    "timeline_duration_ms": 10000,
}


def test_seatable_ids_drop_omitted_from_lock_and_selection() -> None:
    sel = dict(SEL, order_lock={"ordered_segment_ids": ["seg_001", "seg_002", "seg_003"]})
    assert seatable_selection_ids(sel, EDL) == ["seg_001", "seg_003"]
    assert seatable_selection_ids(sel, EDL, use_lock=True) == ["seg_001", "seg_003"]
    assert seatable_selection_ids(sel, None) == ["seg_001", "seg_002", "seg_003"]


def test_edl_with_omitted_clip_is_seated(tmp_path: Path) -> None:
    ctx = isolated_run_ctx(tmp_path, "omit_seated")
    init_run_meta_for_test(ctx)
    mp = ctx.path("segments", "manifest.json")
    mp.parent.mkdir(parents=True, exist_ok=True)
    mp.write_text(
        json.dumps(
            {
                "segments": [
                    {
                        "segment_id": sid,
                        "start_ms": s,
                        "end_ms": e,
                        "text": "x",
                        "type": "interviewee_answer",
                        "speaker_id": "spk_1",
                        "speaker_role": "interviewee",
                        "topic_tags": [],
                    }
                    for sid, s, e in (
                        ("seg_001", 0, 5000),
                        ("seg_002", 5000, 5300),
                        ("seg_003", 9000, 14000),
                    )
                ]
            }
        )
    )
    from interview_mux.air_order import write_live_edl, write_live_selection
    from interview_mux.homunculus.agenda import stage_outputs_present

    assert order_drift_heal_action(SEL, EDL) in {"ok", "stamp"}
    write_live_selection(ctx, dict(SEL), source="selection")
    write_live_edl(ctx, json.loads(json.dumps(EDL)), source="edl")
    assert stage_outputs_present(ctx, "edl") is True
    # verify_commitment and master_finalize compare these two; an omission
    # must not read as order drift.
    sel = ctx.read_json("master/selection.json")
    edl = ctx.read_json("master/edl.json")
    assert order_hashes_match(sel, edl) is True
    assert order_drift_heal_action(sel, edl) == "ok"

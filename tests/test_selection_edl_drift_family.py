"""The selection and the EDL never disagree in a way no rebuild can clear (ISSUES 195).

Nicket's exec_033 and exec_041 (2026-10-13) both ended at the invoke cap on
``selection_edl_order_drift``: junction's remaster refused because the EDL's
speech order did not match ``ordered_segment_ids``, the driver rebuilt the EDL
from the selection, and the same drift came straight back. Five holes, one
family: a blank seam completion pulled back on air on every commit turned
every omit-only write under the freeze into a refused reorder; the overlap
repair's retire of an absorbed id was refused and reported as landed; a
must-air keep with no reseatable tape was neither seated nor recorded; and a
must-air id removal authority put back after ``exclude_unseated`` was
recorded nowhere.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from interview_mux.run_context import RunContext
from run_fixtures import (
    isolated_run_ctx,
    minimal_manifest,
    minimal_manifest_segment,
    write_fixture_json,
)


@pytest.fixture
def ctx(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> RunContext:
    monkeypatch.setenv("MUX_FORENSICS", "0")
    monkeypatch.setenv("MUX_ASSETS_ROOT", str(tmp_path))
    return isolated_run_ctx(tmp_path, "drift_family")


def _logs(ctx: RunContext) -> list[str]:
    rows: list[str] = []
    for line in (ctx.final_path("gui_log.jsonl").read_text(encoding="utf-8").splitlines() if ctx.final_path("gui_log.jsonl").is_file() else []):
        try:
            rows.append(str(json.loads(line).get("message") or ""))
        except Exception:
            rows.append(line)
    return rows


def _speech(sid: str, s: int, e: int) -> dict:
    return {"segment_id": sid, "type": "speech", "source_start_ms": s, "source_end_ms": e, "duration_ms": e - s, "timeline_start_ms": 0}


# --- 1. a blank seam completion is never pulled back on air ---------------------


def _seam_fixture(ctx: RunContext) -> dict:
    write_fixture_json(
        ctx,
        "segments/manifest.json",
        minimal_manifest(
            minimal_manifest_segment("seg_062", start_ms=1_000, end_ms=9_000, text="The other way is a provision", speaker_id="spk_0"),
            minimal_manifest_segment("seg_063", start_ms=9_100, end_ms=9_460, text="called", speaker_id="spk_0"),
            minimal_manifest_segment("seg_064", start_ms=20_000, end_ms=40_000, text="And that is how the whole lab develops its test over time.", speaker_id="spk_0"),
        ),
    )
    sel = {
        "ordered_segment_ids": ["seg_062", "seg_064"],
        "excluded_segment_ids": [{"segment_id": "seg_063", "reason": "blank_or_unusable_answer_audio"}],
    }
    write_fixture_json(ctx, "master/selection.json", sel)
    return sel


def test_blank_seam_completion_is_blocked_from_the_pull(ctx: RunContext) -> None:
    from interview_mux.air_order_integrity import (
        blank_seam_completion_ids,
        repair_incomplete_seam_order,
        resolved_segment_spans,
    )

    sel = _seam_fixture(ctx)
    spans = resolved_segment_spans(ctx)
    ordered = ["seg_062", "seg_064"]
    # The pure repair would pull seg_063 (it completes seg_062's clause).
    pulled, actions = repair_incomplete_seam_order(ordered, spans)
    assert "seg_063" in pulled and any(a.get("action") == "incomplete_seam_pull_completion" for a in actions)
    blocked = blank_seam_completion_ids(ctx, sel, ordered, spans)
    assert blocked == {"seg_063"}
    kept, actions = repair_incomplete_seam_order(ordered, spans, blocked_ids=blocked)
    assert kept == ordered and actions == []


def test_blank_seam_completion_without_an_exclude_reason_is_still_blocked(ctx: RunContext) -> None:
    from interview_mux.air_order_integrity import blank_seam_completion_ids, resolved_segment_spans

    _seam_fixture(ctx)
    spans = resolved_segment_spans(ctx)
    # No exclude row names it blank; the manifest row (360 ms) does.
    assert blank_seam_completion_ids(ctx, {"ordered_segment_ids": ["seg_062", "seg_064"]}, ["seg_062", "seg_064"], spans) == {"seg_063"}


def test_integrity_repair_leaves_the_blank_completion_off_air(ctx: RunContext) -> None:
    from interview_mux.air_order_integrity import repair_air_order_integrity

    sel = _seam_fixture(ctx)
    out, actions = repair_air_order_integrity(ctx, sel)
    assert "seg_063" not in (out.get("ordered_segment_ids") or [])
    assert not any(a.get("action") == "incomplete_seam_pull_completion" for a in actions)


# --- 2. a union-absorbed id leaves the order even when the freeze refuses the write ---


def _absorbed_fixture(ctx: RunContext) -> None:
    write_fixture_json(
        ctx,
        "segments/manifest.json",
        minimal_manifest(
            minimal_manifest_segment("seg_075", start_ms=0, end_ms=10_000, text="We started the company with a very simple idea about people.", speaker_id="spk_0"),
            minimal_manifest_segment(
                "seg_076", start_ms=20_000, end_ms=55_000,
                text="The second thing we learned was that nobody reads the manual before they call support.",
                speaker_id="spk_0", fuse_pass_id="edl_overlap_repair", fused_from=["seg_076", "seg_078"],
            ),
            minimal_manifest_segment("seg_079", start_ms=60_000, end_ms=80_000, text="So we rewrote the manual as a set of short answers to the calls we actually got.", speaker_id="spk_0"),
        ),
    )
    write_fixture_json(
        ctx,
        "master/selection.json",
        {"ordered_segment_ids": ["seg_075", "seg_076", "seg_078", "seg_079"], "excluded_segment_ids": [], "order_lock": {"revision": 3}},
    )


def test_retire_consumed_under_freeze_drops_only_the_absorbed_id(ctx: RunContext) -> None:
    from interview_mux.air_order_boundary import _retire_consumed_under_freeze

    _absorbed_fixture(ctx)
    sel = ctx.read_json("master/selection.json")
    out = _retire_consumed_under_freeze(ctx, sel)
    assert out["ordered_segment_ids"] == ["seg_075", "seg_076", "seg_079"]
    rows = {r["segment_id"]: r["reason"] for r in out["excluded_segment_ids"]}
    assert rows == {"seg_078": "edl_overlap_repair:absorbed_by_survivor"}
    assert any("union-absorbed id(s) retired" in m for m in _logs(ctx))


def test_refused_write_under_freeze_still_retires_the_absorbed_id(ctx: RunContext, monkeypatch: pytest.MonkeyPatch) -> None:
    from interview_mux.air_order_boundary import commit_selection_mutation

    _absorbed_fixture(ctx)
    monkeypatch.setattr("interview_mux.seat_authority.soft_freeze_active", lambda c: True)
    monkeypatch.setattr("interview_mux.seat_authority.hard_freeze_active", lambda c: False)
    monkeypatch.setattr(
        "interview_mux.seat_authority.request_seat_rewrite",
        lambda c, **k: {"allow": False, "refuse_reason": "opportunity_below_threshold"},
    )
    # exec_041: a repair inside the commit re-added another id, so the
    # classifier saw no omit-only delta and the gate refused the whole write.
    monkeypatch.setattr("interview_mux.air_order_boundary._ship_blocking_omit_ids", lambda c, **k: [])
    proposed = {
        "ordered_segment_ids": ["seg_075", "seg_076", "seg_079"],
        "excluded_segment_ids": [{"segment_id": "seg_078", "reason": "edl_overlap_repair:absorbed_by_survivor"}],
        "order_lock": {"revision": 4},
    }
    out = commit_selection_mutation(
        ctx, proposed, producer="edl_overlap_repair", stage_key="edl_overlap_repair",
        checkpoint_mode="detect", skip_checkpoint=True, skip_handoff=True,
    )
    assert out["ordered_segment_ids"] == ["seg_075", "seg_076", "seg_079"], (
        "the frozen order is kept, the absorbed id still leaves"
    )
    on_disk = ctx.read_json("master/selection.json")
    assert "seg_078" not in on_disk["ordered_segment_ids"]
    assert any("seat_freeze: preserved selection order" in m for m in _logs(ctx))


def test_retire_consumed_under_freeze_is_an_end_a_core_action() -> None:
    from interview_mux.seat_authority import END_A_CORE_ACTIONS, hard_freeze_action_permitted

    assert "retire_consumed_under_freeze" in END_A_CORE_ACTIONS
    assert hard_freeze_action_permitted("retire_consumed_under_freeze")


# --- 3. the overlap repair reports what landed, not what it asked for ----------------


def test_retire_reports_a_refused_landing(ctx: RunContext, monkeypatch: pytest.MonkeyPatch) -> None:
    from interview_mux.edl_overlap_repair import retire_consumed_ids_from_selection

    _absorbed_fixture(ctx)
    # A commit path that restores the order and writes nothing.
    monkeypatch.setattr("interview_mux.air_order_boundary.commit_selection_mutation", lambda c, sel, **k: dict(sel))
    assert retire_consumed_ids_from_selection(ctx) == []
    msgs = _logs(ctx)
    assert any("still carries absorbed id(s) seg_078" in m for m in msgs)
    assert not any("retired absorbed id(s) from selection" in m for m in msgs)


def test_retire_reports_a_landing_that_took(ctx: RunContext) -> None:
    from interview_mux.edl_overlap_repair import retire_consumed_ids_from_selection

    _absorbed_fixture(ctx)
    assert retire_consumed_ids_from_selection(ctx) == ["seg_078"]
    assert "seg_078" not in ctx.read_json("master/selection.json")["ordered_segment_ids"]
    assert any("retired absorbed id(s) from selection: seg_078" in m for m in _logs(ctx))


# --- 4. a must-air keep with no reseatable tape is recorded as omitted ------------


def test_writer_records_a_must_air_keep_it_cannot_reseat(ctx: RunContext, monkeypatch: pytest.MonkeyPatch) -> None:
    import interview_mux.removal_authority as removal_authority
    from interview_mux.air_order import restore_protected_speech_clips

    write_fixture_json(
        ctx,
        "segments/manifest.json",
        minimal_manifest(
            minimal_manifest_segment("seg_031", start_ms=100_000, end_ms=120_000),
            minimal_manifest_segment("seg_032", start_ms=119_900, end_ms=120_200),
            minimal_manifest_segment("seg_034", start_ms=120_100, end_ms=140_000),
        ),
    )
    write_fixture_json(ctx, "master/selection.json", {"ordered_segment_ids": ["seg_031", "seg_032", "seg_034"]})
    monkeypatch.setattr(removal_authority, "protected_segment_ids", lambda *a, **k: {"seg_032"})
    edl = {
        "clips": [_speech("seg_031", 100_000, 120_000), _speech("seg_034", 120_100, 140_000)],
        "ordered_segment_ids": ["seg_031", "seg_034"],
    }
    assert restore_protected_speech_clips(ctx, edl, source="edl") == []
    assert edl["omitted_unplayable_segment_ids"] == ["seg_032"]
    assert [c["segment_id"] for c in edl["clips"]] == ["seg_031", "seg_034"]
    assert any("must-air keep(s) seg_032 have no playable tape" in m for m in _logs(ctx))
    # And every selection-versus-clips comparison now sees the EDL as seated.
    from interview_mux.order_hash import order_drift_heal_action

    assert order_drift_heal_action(ctx.read_json("master/selection.json"), edl) != "exclude_unseated"


def test_writer_does_not_record_a_keep_it_reseated(ctx: RunContext, monkeypatch: pytest.MonkeyPatch) -> None:
    import interview_mux.removal_authority as removal_authority
    from interview_mux.air_order import restore_protected_speech_clips

    write_fixture_json(
        ctx,
        "segments/manifest.json",
        minimal_manifest(
            minimal_manifest_segment("seg_031", start_ms=100_000, end_ms=120_000),
            minimal_manifest_segment("seg_032", start_ms=121_000, end_ms=125_000),
            minimal_manifest_segment("seg_034", start_ms=126_000, end_ms=140_000),
        ),
    )
    write_fixture_json(ctx, "master/selection.json", {"ordered_segment_ids": ["seg_031", "seg_032", "seg_034"]})
    monkeypatch.setattr(removal_authority, "protected_segment_ids", lambda *a, **k: {"seg_032"})
    edl = {"clips": [_speech("seg_031", 100_000, 120_000), _speech("seg_034", 126_000, 140_000)], "ordered_segment_ids": ["seg_031", "seg_034"]}
    assert restore_protected_speech_clips(ctx, edl, source="edl") == ["seg_032"]
    assert edl.get("omitted_unplayable_segment_ids", []) == []
    assert [c["segment_id"] for c in edl["clips"]] == ["seg_031", "seg_032", "seg_034"]


# --- 5. a removal the selection refused is recorded on the EDL ----------------------


def test_commit_records_ids_the_selection_kept_without_a_clip(ctx: RunContext) -> None:
    from interview_mux.air_order import _record_unseated_after_selection_commit

    sel = {"ordered_segment_ids": ["seg_031", "seg_032", "seg_034"], "order_lock": {"revision": 5}}
    edl = {"clips": [_speech("seg_031", 0, 10_000), _speech("seg_034", 20_000, 30_000)], "ordered_segment_ids": ["seg_031", "seg_032", "seg_034"]}
    out = _record_unseated_after_selection_commit(ctx, sel, edl, ["seg_031", "seg_034"], source="edl")
    assert out["omitted_unplayable_segment_ids"] == ["seg_032"]
    assert out["ordered_segment_ids"] == ["seg_031", "seg_034"]
    assert any("the selection keeps seg_032 with no clip to play" in m for m in _logs(ctx))
    from interview_mux.order_hash import order_drift_heal_action

    assert order_drift_heal_action(sel, out) in {"ok", "stamp"}


def test_commit_records_nothing_when_every_id_has_a_clip(ctx: RunContext) -> None:
    from interview_mux.air_order import _record_unseated_after_selection_commit

    sel = {"ordered_segment_ids": ["seg_031", "seg_034"]}
    edl = {"clips": [_speech("seg_031", 0, 10_000), _speech("seg_034", 20_000, 30_000)], "ordered_segment_ids": ["seg_031", "seg_034"]}
    out = _record_unseated_after_selection_commit(ctx, sel, edl, ["seg_031", "seg_034"], source="edl")
    assert out is edl and "omitted_unplayable_segment_ids" not in out

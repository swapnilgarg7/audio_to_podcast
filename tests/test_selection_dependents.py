"""Selection-derived documents follow every selection commit (ISSUES 113).

exec_009: an overlap union retired seg_019 from the selection under hard
freeze (revision 2 to 3); the lay-up plan kept the id and revision 2, the EDL
loader failed closed, and the heal was refused for writing under its own key.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from interview_mux.nugget_layup import PLAN_REL, layup_freshness_errors
from interview_mux.seat_authority import END_A_CORE_ACTIONS, hard_freeze_action_permitted
from interview_mux.selection_dependents import (
    END_A_REASON,
    nearest_live_anchor,
    reanchor_sound_design_cues,
    reconcile_selection_dependents,
)
from run_fixtures import isolated_run_ctx, write_fixture_json


@pytest.fixture
def ctx(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("MUX_FORENSICS", "0")
    monkeypatch.setenv("MUX_ASSETS_ROOT", str(tmp_path))
    return isolated_run_ctx(tmp_path, "sel_deps")


def _hard_freeze(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("interview_mux.seat_authority.hard_freeze_active", lambda c: True)
    monkeypatch.setattr("interview_mux.seat_authority.soft_freeze_active", lambda c: False)


def _seed(ctx, *, selection_ids: list[str], plan_ids: list[str], sel_rev: int, plan_rev: int) -> None:
    ctx.write_json(
        "master/selection.json",
        {"ordered_segment_ids": selection_ids, "order_lock": {"revision": sel_rev}},
        skip_handoff=True,
    )
    # The selection write may restamp its lock; a "fresh" plan copies what landed.
    sel_lock = dict(ctx.read_json("master/selection.json").get("order_lock") or {})
    plan_lock = sel_lock if plan_rev == sel_rev else {**sel_lock, "revision": plan_rev}
    # Planted raw: the one-writer admit on ``write_json`` copies the selection
    # ids onto the plan and drops off-air rows, which would erase the very
    # staleness these tests fit. A plan the compose owner wrote before the
    # selection moved still lists the retired id (ISSUES 190).
    write_fixture_json(
        ctx,
        PLAN_REL,
        {
            "ordered_segment_ids": plan_ids,
            "layups": [{"target_segment_id": sid, "line_id": f"vo_layup_{sid}"} for sid in plan_ids],
            "order_lock": plan_lock,
            "_meta": {"producer_stage": "nugget_layup_compose"},
        },
    )
    ctx.write_json("understanding/gap_report.json", {"interviewer_lines": []}, skip_handoff=True)


def test_the_reason_is_end_a_core_and_permitted_under_hard_freeze(ctx, monkeypatch) -> None:
    _hard_freeze(monkeypatch)
    assert END_A_REASON in END_A_CORE_ACTIONS
    assert hard_freeze_action_permitted(END_A_REASON, ctx) is True


def test_a_retired_id_and_a_bumped_revision_are_fitted_under_hard_freeze(ctx, monkeypatch) -> None:
    _hard_freeze(monkeypatch)
    _seed(ctx, selection_ids=["seg_a", "seg_b"], plan_ids=["seg_a", "seg_019", "seg_b"], sel_rev=3, plan_rev=2)
    previous = {"ordered_segment_ids": ["seg_a", "seg_019", "seg_b"], "order_lock": {"revision": 2}}
    current = ctx.read_json("master/selection.json")
    from interview_mux.nugget_layup import layup_rows_off_order

    assert layup_freshness_errors(ctx)
    assert layup_rows_off_order(ctx) == ["seg_019"]
    notes = reconcile_selection_dependents(ctx, producer="edl_overlap_repair", previous=previous, current=current)
    assert "layup_plan_adopted" in notes
    assert layup_freshness_errors(ctx) == []
    # seg_019 has no tape here, so adopt cannot rebind its row; the sanitizer
    # drops it. Only rebindable rows re-trigger the reconcile.
    assert layup_rows_off_order(ctx, rebindable_only=True) == []
    plan = ctx.read_json(PLAN_REL)
    assert plan["ordered_segment_ids"] == ["seg_a", "seg_b"]
    sel = ctx.read_json("master/selection.json")
    assert plan["order_lock"]["revision"] == sel["order_lock"]["revision"]


def test_a_lock_only_drift_is_restamped_without_an_order_change(ctx) -> None:
    from interview_mux.nugget_layup import layup_plan_lock_revision_drift

    _seed(ctx, selection_ids=["seg_a", "seg_b"], plan_ids=["seg_a", "seg_b"], sel_rev=5, plan_rev=4)
    current = ctx.read_json("master/selection.json")
    # A revision-only drift is not staleness (ISSUES 190): the plan is fresh
    # before the reconcile, and the reconcile copies the lock forward.
    assert layup_freshness_errors(ctx) == []
    assert layup_plan_lock_revision_drift(ctx) is True
    notes = reconcile_selection_dependents(ctx, producer="full_master_ranking", previous=current, current=current)
    assert "layup_plan_lock_restamped" in notes
    assert layup_plan_lock_revision_drift(ctx) is False
    assert layup_freshness_errors(ctx) == []
    plan = ctx.read_json(PLAN_REL)
    assert plan["order_lock"]["revision"] == current["order_lock"]["revision"]


def test_a_fresh_plan_is_left_alone(ctx) -> None:
    _seed(ctx, selection_ids=["seg_a", "seg_b"], plan_ids=["seg_a", "seg_b"], sel_rev=2, plan_rev=2)
    current = ctx.read_json("master/selection.json")
    before = ctx.read_json(PLAN_REL)
    assert reconcile_selection_dependents(ctx, producer="x", previous=current, current=current) == []
    assert ctx.read_json(PLAN_REL) == before


def test_a_plan_the_cascade_marked_stale_waits_for_the_recompose(ctx) -> None:
    _seed(ctx, selection_ids=["seg_a"], plan_ids=["seg_a", "seg_z"], sel_rev=3, plan_rev=2)
    plan = ctx.read_json(PLAN_REL)
    plan["_meta"]["stale"] = True
    ctx.write_json(PLAN_REL, plan, skip_handoff=True)
    current = ctx.read_json("master/selection.json")
    notes = reconcile_selection_dependents(ctx, producer="x", previous=None, current=current)
    assert not any(n.startswith("layup_plan") for n in notes)


def test_the_recovery_playbook_fits_the_plan_under_hard_freeze(ctx, monkeypatch) -> None:
    from interview_mux.recovery_controller import playbook_adopt_layup

    _hard_freeze(monkeypatch)
    _seed(ctx, selection_ids=["seg_a", "seg_b"], plan_ids=["seg_a", "seg_019", "seg_b"], sel_rev=3, plan_rev=2)
    assert playbook_adopt_layup(ctx) == [PLAN_REL]
    assert layup_freshness_errors(ctx) == []


def test_cues_anchored_on_a_retired_id_move_to_the_live_neighbour() -> None:
    prev = ["seg_a", "seg_019", "seg_b", "seg_c"]
    cur = ["seg_a", "seg_b", "seg_c"]
    assert nearest_live_anchor("seg_019", prev, cur, key="before_segment_id") == "seg_b"
    assert nearest_live_anchor("seg_019", prev, cur, key="after_segment_id") == "seg_a"
    assert nearest_live_anchor("seg_019", prev, cur, key="segment_id") == "seg_a"
    assert nearest_live_anchor("seg_zz", prev, cur, key="segment_id") == ""
    plan = {
        "flow_plans": {
            "podcast": {
                "cues": [
                    {"cue_id": "c1", "before_segment_id": "seg_019"},
                    {"cue_id": "c2", "segment_id": "seg_b"},
                    {"cue_id": "c3", "under_segment_id": "seg_019", "skip": True},
                ]
            }
        }
    }
    fixed, notes = reanchor_sound_design_cues(plan, previous_ids=prev, current_ids=cur)
    cues = fixed["flow_plans"]["podcast"]["cues"]
    assert cues[0]["before_segment_id"] == "seg_b"
    assert cues[1] == {"cue_id": "c2", "segment_id": "seg_b"}
    assert cues[2]["skip"] is True and "under_segment_id" in cues[2]
    assert notes == ["podcast:c1:seg_019->seg_b"]
    # The input document is untouched.
    assert plan["flow_plans"]["podcast"]["cues"][0]["before_segment_id"] == "seg_019"


def test_a_cue_with_no_live_neighbour_is_skipped_not_invented() -> None:
    plan = {"flow_plans": {"podcast": {"cues": [{"cue_id": "c1", "segment_id": "seg_only"}]}}}
    fixed, notes = reanchor_sound_design_cues(plan, previous_ids=["seg_only"], current_ids=["seg_new"])
    cue = fixed["flow_plans"]["podcast"]["cues"][0]
    assert cue["skip"] is True and cue["skip_reason"].startswith(END_A_REASON)
    assert notes == ["podcast:c1:seg_only->skip"]


def test_the_sound_design_plan_lands_under_its_owner_with_the_end_a_reason(ctx, monkeypatch) -> None:
    import json

    _hard_freeze(monkeypatch)
    # Straight to disk: the reconcile reads the plan, the owner commit validates it.
    sdp = ctx.final_path("understanding", "sound_design_plan.json")
    sdp.parent.mkdir(parents=True, exist_ok=True)
    sdp.write_text(
        json.dumps(
            {
                "_meta": {"producer_stage": "music_palette_compose"},
                "flow_plans": {"podcast": {"cues": [{"cue_id": "c1", "segment_id": "seg_019"}]}},
            }
        ),
        encoding="utf-8",
    )
    ctx.write_json("master/selection.json", {"ordered_segment_ids": ["seg_a", "seg_b"]}, skip_handoff=True)
    calls: list[dict] = []

    def _persist(c, rel, doc, *, reason="", **kw):
        calls.append({"rel": rel, "doc": doc, "reason": reason, "kw": kw})
        return True

    monkeypatch.setattr("interview_mux.seat_authority.persist_frozen_seat_doc", _persist)
    notes = reconcile_selection_dependents(
        ctx,
        producer="edl_overlap_repair",
        previous={"ordered_segment_ids": ["seg_a", "seg_019", "seg_b"]},
        current={"ordered_segment_ids": ["seg_a", "seg_b"]},
    )
    assert "sdp_cues_reanchored:1" in notes
    assert calls and calls[0]["rel"] == "understanding/sound_design_plan.json"
    assert calls[0]["reason"] == END_A_REASON
    assert calls[0]["kw"]["stage_key"] == "music_palette_compose"
    assert calls[0]["doc"]["flow_plans"]["podcast"]["cues"][0]["segment_id"] == "seg_a"


def test_the_selection_commit_calls_the_reconcile_after_the_write() -> None:
    import interview_mux.air_order_boundary as aob

    src = Path(aob.__file__).read_text(encoding="utf-8")
    body = src[src.find("def commit_selection_mutation") :]
    assert 0 < body.find("on_selection_order_changed(") < body.find("reconcile_selection_dependents(") < body.find("return out")


def test_overlap_repair_nle_write_names_the_remap_mutation_class(ctx, monkeypatch) -> None:
    from interview_mux import edl_overlap_repair as rep

    ctx.write_json(
        "segments/nle_edits.json",
        {"segment_overrides": {}, "sequence_order": ["seg_a", "seg_019", "seg_b"]},
        skip_handoff=True,
    )
    monkeypatch.setattr("interview_mux.removal_authority.refuse_nle_excludes", lambda c, nle, producer: nle)
    rep._update_nle(ctx, survivor="seg_a", consumed=["seg_019"], union_start=0, union_end=1000, segs={})
    nle = ctx.read_json("segments/nle_edits.json")
    assert nle["segment_overrides"]["seg_019"]["excluded"] is True
    assert nle["sequence_order"] == ["seg_a", "seg_b"]

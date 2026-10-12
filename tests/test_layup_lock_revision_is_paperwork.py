"""A lay-up plan built for the committed air order is fresh whatever its lock revision (ISSUES 190).

The run this guards (2026-10-11): master/selection.json and the lay-up plan
listed the same segments with the same content hash (8f474dd65fbafd40), but
selection sanitize had moved the lock to revision 19 while the plan still
carried 15. The freshness check read any revision mismatch as a stale plan,
compose was re-dispatched until max_invokes_per_identity, and the hard seat
freeze refused the restamp that would have ended it. Revision is paperwork;
the content hash and the id list are the evidence.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from interview_mux.nugget_layup import (
    PLAN_REL,
    layup_freshness_errors,
    layup_plan_lock_revision_drift,
    restamp_layup_plan_order_lock,
)
from interview_mux.order_hash import (
    get_order_lock,
    order_lock_revision_only_drift,
    order_locks_match,
    order_locks_same_content,
    ordered_segment_ids_hash,
)
from run_fixtures import isolated_run_ctx

ORDER = ["seg_012", "seg_027", "seg_038", "seg_034"]


def _lock(ids: list[str], revision: int, created_by: str = "selection") -> dict:
    return {
        "version": 1,
        "revision": revision,
        "authority": "master/selection.json",
        "ordered_segment_ids": list(ids),
        "order_content_hash": ordered_segment_ids_hash(ids),
        "created_by": created_by,
    }


def test_same_content_under_different_revisions_is_not_drift() -> None:
    sel = {"ordered_segment_ids": ORDER, "order_lock": _lock(ORDER, 19, "artifact_sanitize.selection")}
    plan = {"ordered_segment_ids": ORDER, "order_lock": _lock(ORDER, 15, "nugget_layup_compose")}
    assert order_locks_match(sel, plan) is False
    assert order_locks_same_content(sel, plan) is True
    assert order_lock_revision_only_drift(sel, plan) is True


def test_different_content_is_still_drift() -> None:
    moved = [ORDER[0], ORDER[2], ORDER[1], ORDER[3]]
    sel = {"ordered_segment_ids": moved, "order_lock": _lock(moved, 18)}
    plan = {"ordered_segment_ids": ORDER, "order_lock": _lock(ORDER, 15)}
    assert order_locks_same_content(sel, plan) is False
    assert order_lock_revision_only_drift(sel, plan) is False


def test_a_lock_without_ids_falls_back_to_its_hash_then_the_doc() -> None:
    sel = {"ordered_segment_ids": ORDER, "order_lock": _lock(ORDER, 3)}
    hash_only = {"ordered_segment_ids": ORDER, "order_lock": {"revision": 1, "order_content_hash": ordered_segment_ids_hash(ORDER)}}
    stale_hash = {"ordered_segment_ids": ORDER, "order_lock": {"revision": 1, "order_content_hash": "0000deadbeef0000"}}
    assert order_locks_same_content(sel, hash_only) is True
    assert order_locks_same_content(sel, stale_hash) is False


@pytest.fixture
def ctx(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("MUX_FORENSICS", "0")
    monkeypatch.setenv("MUX_ASSETS_ROOT", str(tmp_path))
    return isolated_run_ctx(tmp_path, "layup_lock_paperwork")


def _seed(ctx, *, plan_ids: list[str], plan_rev: int = 15) -> None:
    sel_path = ctx.path("master", "selection.json")
    sel_path.parent.mkdir(parents=True, exist_ok=True)
    import json

    sel_path.write_text(
        json.dumps(
            {
                "ordered_segment_ids": ORDER,
                "order_content_hash": ordered_segment_ids_hash(ORDER),
                "order_lock": _lock(ORDER, 19, "artifact_sanitize.selection"),
            }
        )
    )
    plan_path = ctx.path(*PLAN_REL.split("/"))
    plan_path.parent.mkdir(parents=True, exist_ok=True)
    plan_path.write_text(
        json.dumps(
            {
                "ordered_segment_ids": plan_ids,
                "order_content_hash": ordered_segment_ids_hash(plan_ids),
                "order_lock": _lock(plan_ids, plan_rev, "nugget_layup_compose"),
                "layups": [
                    {"target_segment_id": sid, "line_id": f"vo_layup_{sid}", "text": "x " * 20}
                    for sid in plan_ids
                ],
            }
        )
    )


def test_plan_at_an_older_revision_of_the_same_order_is_fresh(ctx) -> None:
    _seed(ctx, plan_ids=ORDER, plan_rev=15)
    assert layup_freshness_errors(ctx) == []
    assert layup_plan_lock_revision_drift(ctx) is True


def test_plan_for_another_order_is_still_stale(ctx) -> None:
    moved = [ORDER[0], ORDER[2], ORDER[1], ORDER[3]]
    _seed(ctx, plan_ids=moved, plan_rev=19)
    errors = layup_freshness_errors(ctx)
    assert errors and "do not match" in errors[0]


def test_restamp_copies_the_selection_lock_forward_when_permitted(ctx) -> None:
    _seed(ctx, plan_ids=ORDER, plan_rev=15)
    assert restamp_layup_plan_order_lock(ctx) is True
    plan = ctx.read_json(PLAN_REL)
    assert get_order_lock(plan)["revision"] == 19
    assert layup_plan_lock_revision_drift(ctx) is False
    assert layup_freshness_errors(ctx) == []


def test_reconcile_notes_the_restamp_and_never_calls_adopt(ctx, monkeypatch: pytest.MonkeyPatch) -> None:
    import interview_mux.nugget_layup as nl
    from interview_mux.selection_dependents import _reconcile_layup_plan

    _seed(ctx, plan_ids=ORDER, plan_rev=15)

    def _boom(*_a, **_k):
        raise AssertionError("adopt must not run for a fresh plan")

    monkeypatch.setattr(nl, "adopt_layup_plan_to_selection", _boom)
    notes: list[str] = []
    _reconcile_layup_plan(ctx, notes)
    assert notes and notes[0].startswith("layup_plan_lock_")
    assert layup_freshness_errors(ctx) == []


def test_omit_ledger_lock_at_another_revision_is_not_stale(ctx) -> None:
    import json

    from interview_mux.omit_ledger import OMIT_LEDGER_REL, air_contract_errors, empty_omit_ledger

    _seed(ctx, plan_ids=ORDER, plan_rev=15)
    ledger = empty_omit_ledger()
    ledger["order_lock"] = _lock(ORDER, 15, "nugget_layup_compose")
    path = ctx.path(*OMIT_LEDGER_REL.split("/"))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(ledger))
    errors = air_contract_errors(ctx, ledger=ledger, gap_report={"interviewer_lines": []}, edl={"clips": []})
    assert "omit_ledger_order_lock_stale" not in errors

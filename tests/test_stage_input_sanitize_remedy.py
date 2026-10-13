"""A sanitize block runs its own remedy stage before refusing (ISSUES 194).

exec_041 (2026-10-13): junction_snip_qa failed on an EDL/selection drift, its
recovery unmarked full_master_ranking, and ranking's input check refused the
unsanitary selection junction left behind. The remedy the error named,
selection_order_sanitize, is the stage *after* ranking, so the seed walk never
ran it: ranking was re-dispatched to the identical-failure cap and the driver
stopped with "Run the matching sanitize stage" in every error line.
"""

from __future__ import annotations

import pytest

from run_fixtures import isolated_run_ctx, mark_done_raw

import interview_mux.stage_input_checks as sic
from interview_mux.stage_input_checks import StageInputError, require_stage_inputs

UNSANITARY = (
    "selection_unsanitary: selection_needs_sanitize:drop_orphan_ref,bump_order_lock,"
    "incomplete_seam_pull_completion"
)


@pytest.fixture
def ctx(tmp_path, monkeypatch):
    monkeypatch.setenv("MUX_FORENSICS", "0")
    c = isolated_run_ctx(tmp_path, "sanitize_remedy")
    # Only the sanitary preflight speaks in these tests.
    monkeypatch.setattr(sic, "_STAGE_CHECKERS", {})
    monkeypatch.setattr(sic, "_LLM_STAGES", frozenset())
    monkeypatch.setattr(sic, "_pending_write_approval_issues", lambda c, s: [])
    monkeypatch.setattr(sic, "_vo_contract_issues", lambda c, s: [])
    monkeypatch.setattr(sic, "_try_preflight_recovery", lambda c, s, i: False)
    return c


def _preflight(state: dict, *, clears: bool):
    def _errors(ctx, stage_id):
        if stage_id != "full_master_ranking":
            return []
        if clears and state.get("ran"):
            return []
        return [UNSANITARY]

    return _errors


def test_the_named_sanitize_stage_runs_once_and_the_block_clears(ctx, monkeypatch) -> None:
    state: dict = {"ran": []}
    monkeypatch.setattr(
        "interview_mux.artifact_sanitize.preflight.sanitary_preflight_errors",
        _preflight(state, clears=True),
    )
    monkeypatch.setattr(
        "interview_mux.pipeline.run_single_stage", lambda c, s: state["ran"].append(s)
    )
    require_stage_inputs(ctx, "full_master_ranking")
    assert state["ran"] == ["selection_order_sanitize"]


def test_a_remedy_that_does_not_clear_the_block_runs_once_then_refuses(ctx, monkeypatch) -> None:
    state: dict = {"ran": []}
    monkeypatch.setattr(
        "interview_mux.artifact_sanitize.preflight.sanitary_preflight_errors",
        _preflight(state, clears=False),
    )
    monkeypatch.setattr(
        "interview_mux.pipeline.run_single_stage", lambda c, s: state["ran"].append(s)
    )
    with pytest.raises(StageInputError) as info:
        require_stage_inputs(ctx, "full_master_ranking")
    assert "selection_unsanitary" in str(info.value)
    with pytest.raises(StageInputError):
        require_stage_inputs(ctx, "full_master_ranking")
    assert state["ran"] == ["selection_order_sanitize"]


def test_a_done_marker_on_the_remedy_is_cleared_so_it_really_runs(ctx, monkeypatch) -> None:
    state: dict = {"ran": []}
    mark_done_raw(ctx, "selection_order_sanitize")
    monkeypatch.setattr(
        "interview_mux.artifact_sanitize.preflight.sanitary_preflight_errors",
        _preflight(state, clears=True),
    )
    monkeypatch.setattr(
        "interview_mux.pipeline.run_single_stage", lambda c, s: state["ran"].append(s)
    )
    require_stage_inputs(ctx, "full_master_ranking")
    assert state["ran"] == ["selection_order_sanitize"]
    assert not ctx.is_done("selection_order_sanitize")


def test_the_sanitize_stage_itself_is_never_its_own_remedy(ctx, monkeypatch) -> None:
    state: dict = {"ran": []}
    monkeypatch.setattr(
        "interview_mux.artifact_sanitize.preflight.sanitary_preflight_errors",
        lambda c, s: [UNSANITARY] if s == "selection_order_sanitize" else [],
    )
    monkeypatch.setattr(
        "interview_mux.pipeline.run_single_stage", lambda c, s: state["ran"].append(s)
    )
    with pytest.raises(StageInputError):
        require_stage_inputs(ctx, "selection_order_sanitize")
    assert state["ran"] == []


def test_a_failing_remedy_is_logged_and_the_block_is_refused_in_its_own_words(ctx, monkeypatch) -> None:
    state: dict = {"ran": []}
    monkeypatch.setattr(
        "interview_mux.artifact_sanitize.preflight.sanitary_preflight_errors",
        _preflight(state, clears=False),
    )

    def _boom(c, s):
        raise RuntimeError("sanitize_refused:selection: lattice")

    monkeypatch.setattr("interview_mux.pipeline.run_single_stage", _boom)
    with pytest.raises(StageInputError) as info:
        require_stage_inputs(ctx, "full_master_ranking")
    assert "selection_unsanitary" in str(info.value)

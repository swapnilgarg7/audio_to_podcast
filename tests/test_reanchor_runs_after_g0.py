"""content_brief_reanchor runs after G0 while its brief still lacks segment ids.

content_context writes content_brief.json with empty topics[].segment_ids and
reanchor fills them once segments exist, which is always after G0. The rewind
guard refused reanchor because the file existed, the heal refused the mark
because the file was incomplete, and framing_posture_decide waited on the seed
order until the identical-error cap (exec_034).
"""

from __future__ import annotations

from pathlib import Path

import pytest
from run_fixtures import init_run_meta_for_test, isolated_run_ctx

import interview_mux.homunculus.packer as packer
from interview_mux.homunculus.agenda import _refuse_delivery_timeline_rewind

BRIEF = {
    "thesis": "Distribution beats product for early AI startups.",
    "topics": [
        {"name": "distribution", "summary": "how the guest found users", "segment_ids": []},
        {"name": "pricing", "summary": "usage pricing lessons", "segment_ids": []},
    ],
}


def _ctx(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    ctx = isolated_run_ctx(tmp_path, "reanchor_g0")
    init_run_meta_for_test(ctx)
    monkeypatch.setattr(packer, "g0_closed", lambda _ctx: True)
    ctx.write_json("understanding/content_brief.json", dict(BRIEF), skip_handoff=True)
    return ctx


def test_reanchor_is_not_refused_while_its_brief_is_unanchored(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ctx = _ctx(tmp_path, monkeypatch)
    assert not ctx.is_done("content_brief_reanchor")
    _refuse_delivery_timeline_rewind(ctx, "content_brief_reanchor", action="run")


def test_finished_reanchor_is_still_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    ctx = _ctx(tmp_path, monkeypatch)
    (ctx.run_dir / ".stage_done").mkdir(parents=True, exist_ok=True)
    (ctx.run_dir / ".stage_done" / "content_brief_reanchor").write_text("", encoding="utf-8")
    with pytest.raises(RuntimeError, match="timeline artifacts exist after G0"):
        _refuse_delivery_timeline_rewind(ctx, "content_brief_reanchor", action="run")

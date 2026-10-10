"""A nested stage run is one invoke, however many LLM calls it makes.

boundary_topic_resplit re-runs segment_classification in-process. A long tape
classifies in shards, and each shard's call was counted against
max_invokes_per_identity (3), so shard 4 of 5 was refused (exec_034).
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from run_fixtures import init_run_meta_for_test, isolated_run_ctx

from interview_mux.homunculus.ledger import count_identity, read_ledger
from interview_mux.homunculus.loop import nested_chat_create
from interview_mux.write_staging import run_nested_staged_stage


class _Completions:
    def __init__(self) -> None:
        self.calls = 0

    def create(self, **kwargs):
        self.calls += 1
        return SimpleNamespace(id=f"r{self.calls}")


def _ctx(tmp_path: Path):
    ctx = isolated_run_ctx(tmp_path, "nested_one_invoke")
    init_run_meta_for_test(ctx)
    ctx.write_json(
        "run_meta.json",
        {**ctx.read_json("run_meta.json"), "homunculus_version": "0.2.0"},
    )
    return ctx


def test_five_shards_in_a_nested_stage_count_once(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    completions = _Completions()
    client = SimpleNamespace(chat=SimpleNamespace(completions=completions))

    def classify() -> None:
        for i in range(5):
            nested_chat_create(
                ctx,
                "segment_classification",
                client,
                {"model": "x", "messages": [{"role": "user", "content": f"shard {i}"}]},
            )

    run_nested_staged_stage(ctx, "segment_classification", classify)
    assert completions.calls == 5
    rows = [r for r in read_ledger(ctx) if r.get("identity") == "segment_classification"]
    assert [r.get("status") for r in rows if r.get("kind") == "stage"] == ["started", "done"]
    assert not [r for r in rows if r.get("kind") == "llm"]
    # Once the stage is sealed the whole nested run is one invoke.
    (ctx.run_dir / ".stage_done").mkdir(parents=True, exist_ok=True)
    (ctx.run_dir / ".stage_done" / "segment_classification").write_text("", encoding="utf-8")
    assert count_identity(ctx, "segment_classification") == 1


def test_failed_nested_stage_closes_its_row(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)

    def boom() -> None:
        raise RuntimeError("shard failed")

    try:
        run_nested_staged_stage(ctx, "segment_classification", boom)
    except RuntimeError:
        pass
    rows = [r for r in read_ledger(ctx) if r.get("kind") == "stage"]
    assert [r.get("status") for r in rows] == ["started", "failed"]
    assert count_identity(ctx, "segment_classification") == 0

"""The narrative audit's chapter relabel asks the ownership table before it writes (ISSUES 190).

Under a hard freeze the freeze policy allows narrative_metadata_align, but the
ownership table can still refuse the persist (the freeze was owned by
selection_framing_apply). The refusal used to surface as an error-level
AuthorityDenied from inside the audit's finish path; now it is a note.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from run_fixtures import isolated_run_ctx


@pytest.fixture
def ctx(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("MUX_FORENSICS", "0")
    monkeypatch.setenv("MUX_ASSETS_ROOT", str(tmp_path))
    c = isolated_run_ctx(tmp_path, "audit_relabel_refused")
    sel = c.path("master", "selection.json")
    sel.parent.mkdir(parents=True, exist_ok=True)
    sel.write_text(
        json.dumps(
            {
                "ordered_segment_ids": ["seg_001", "seg_002"],
                "chapters": [
                    {"chapter_id": "ch_2", "title": "B", "segment_ids": ["seg_002"]},
                    {"chapter_id": "ch_1", "title": "A", "segment_ids": ["seg_001"]},
                ],
            }
        )
    )
    return c


def test_refused_relabel_is_a_note_not_a_denial(ctx, monkeypatch: pytest.MonkeyPatch) -> None:
    import interview_mux.artifact_ownership as own
    import interview_mux.air_order_boundary as boundary
    import interview_mux.seat_authority as seat
    from interview_mux.audit_repair_loop import maybe_repair_after_narrative_audit

    monkeypatch.setattr(seat, "hard_freeze_active", lambda _ctx: True)
    monkeypatch.setattr(own, "freeze_write_allowed", lambda *_a, **_k: True)
    monkeypatch.setattr(own, "write_permitted", lambda *_a, **_k: (False, "hard_freeze_owner:selection_framing_apply"))

    def _boom(*_a, **_k):
        raise AssertionError("commit must not run when the table refuses")

    monkeypatch.setattr(boundary, "commit_selection_mutation", _boom)
    out = maybe_repair_after_narrative_audit(ctx, {"verdict": "fail", "blocking_issues": []})
    actions = [n.get("action") for n in out.get("repair_notes") or []]
    assert "skip_relabel_chapters_not_permitted" in actions
    assert "relabel_chapters_failed" not in actions
    log_text = "\n".join(
        json.dumps(e) for e in (ctx.read_json("run_log.json") or []) if isinstance(e, dict)
    ) if ctx.artifact_exists("run_log.json") else ""
    assert "AuthorityDenied" not in log_text

"""Tests for air-order integrity detection and repair."""

from __future__ import annotations

from run_fixtures import mark_done_raw
from interview_mux.air_order_integrity import (
    critical_violations,
    late_opening_cluster_violations,
    pull_mid_arc_reverse_jumps,
    repair_air_order_integrity,
    repair_opening_tape_integrity,
    reverse_tape_jump_violations,
    write_air_order_integrity_report,
)


def test_reverse_tape_jump_detects_exec_188_shape():
    ordered = ["seg_003", "seg_050", "seg_001c", "seg_001d", "seg_058"]
    starts = {
        "seg_001": 0,
        "seg_003": 152_000,
        "seg_050": 2_416_000,
        "seg_001c": 0,
        "seg_001d": 0,
        "seg_058": 2_500_000,
    }
    violations = reverse_tape_jump_violations(None, ordered, starts=starts)
    assert violations
    assert any(v.get("before_segment_id") == "seg_001c" for v in violations)


def test_pull_mid_arc_prepends_opening_cluster_when_guest_first():
    """Prefer native host intro: prepend opening-window family, do not drop."""
    ordered = ["seg_003", "seg_050", "seg_001c", "seg_001d"]
    starts = {
        "seg_001": 0,
        "seg_003": 152_000,
        "seg_050": 2_416_000,
        "seg_001c": 0,
        "seg_001d": 0,
    }
    pulled, moved, dropped = pull_mid_arc_reverse_jumps(
        ordered, starts, guest_first=True
    )
    assert not dropped
    assert "seg_001c" in pulled
    # Mid-arc prepends before the reverse-jump pair; opening-tape repair
    # then moves the family to episode front.
    assert pulled.index("seg_001c") < pulled.index("seg_050")
    assert moved


def test_late_opening_cluster_violation():
    ordered = ["seg_003", "seg_050", "seg_001c"]
    starts = {"seg_001": 0, "seg_003": 152_000, "seg_050": 2_416_000, "seg_001c": 0}
    violations = late_opening_cluster_violations(None, ordered, starts=starts)
    assert violations


class _FakeCtx:
    def __init__(self, starts: dict[str, int] | None = None):
        self._starts = starts or {}
        self.logs: list[tuple] = []

    def artifact_exists(self, path: str) -> bool:
        return path in {"segments/boundaries.json"}

    def read_json(self, path: str):
        if path == "segments/boundaries.json":
            return {
                "boundaries": [
                    {"segment_id": sid, "start_ms": ms} for sid, ms in self._starts.items()
                ]
            }
        return {}


def test_repair_opening_tape_guest_first_prepends_cluster():
    """Default mode prepends host-intro family instead of opening_skipped_duplicate."""
    ctx = _FakeCtx(
        {
            "seg_001": 0,
            "seg_003": 152_000,
            "seg_050": 2_416_000,
            "seg_001c": 0,
            "seg_001d": 0,
        }
    )
    selection = {
        "ordered_segment_ids": ["seg_003", "seg_050", "seg_001c", "seg_001d"],
        "excluded_segment_ids": [],
    }
    repaired, actions = repair_opening_tape_integrity(ctx, selection)
    ordered = repaired.get("ordered_segment_ids") or []
    assert "seg_001c" in ordered
    assert "seg_001d" in ordered
    assert ordered.index("seg_001c") < ordered.index("seg_003")
    excl = {
        str(r.get("segment_id") if isinstance(r, dict) else r)
        for r in (repaired.get("excluded_segment_ids") or [])
    }
    assert "seg_001c" not in excl
    assert any(a.get("action") == "prepend_opening_family" for a in actions)


def test_repair_opening_tape_guest_first_prepends_host_not_last_late_family():
    """Multiple opening families: prepend earliest host, not the last late cluster."""
    ctx = _FakeCtx(
        {
            "seg_002": 26_119,
            "seg_003": 83_780,
            "seg_003ca": 97_970,
            "seg_004": 124_840,
            "seg_005": 151_600,
            "seg_007": 168_310,
            "seg_008": 185_780,
        }
    )
    selection = {
        "ordered_segment_ids": [
            "seg_007",
            "seg_005",
            "seg_004",
            "seg_002",
            "seg_003ca",
            "seg_008",
        ],
        "excluded_segment_ids": [],
    }
    before = late_opening_cluster_violations(
        ctx, selection["ordered_segment_ids"]
    )
    assert len(before) >= 2
    repaired, actions = repair_opening_tape_integrity(ctx, selection)
    ordered = [str(s) for s in (repaired.get("ordered_segment_ids") or [])]
    assert ordered[0] == "seg_002"
    assert "seg_007" in ordered
    assert "seg_003ca" in ordered
    assert not late_opening_cluster_violations(ctx, ordered)
    assert any(a.get("action") == "prepend_opening_family" for a in actions)


def test_repair_opening_tape_drop_override_still_excludes():
    ctx = _FakeCtx(
        {
            "seg_001": 0,
            "seg_003": 152_000,
            "seg_050": 2_416_000,
            "seg_001c": 0,
            "seg_001d": 0,
        }
    )
    selection = {
        "ordered_segment_ids": ["seg_003", "seg_050", "seg_001c", "seg_001d"],
        "excluded_segment_ids": [],
    }
    repaired, actions = repair_opening_tape_integrity(
        ctx, selection, mode="drop_if_guest_first"
    )
    ordered = repaired.get("ordered_segment_ids") or []
    assert "seg_001c" not in ordered
    excl = {
        str(r.get("segment_id") if isinstance(r, dict) else r)
        for r in (repaired.get("excluded_segment_ids") or [])
    }
    assert "seg_001c" in excl
    assert actions


def test_repair_air_order_prefers_host_intro_over_company_pitch():
    """Host intro + company pitch both stay; host intro lands first."""
    ctx = _FakeCtx(
        {
            "seg_002": 26_000,
            "seg_003": 49_000,
            "seg_003b": 51_000,
            "seg_008": 200_000,
        }
    )
    selection = {
        "ordered_segment_ids": ["seg_003b", "seg_008", "seg_002"],
        "excluded_segment_ids": [
            {"segment_id": "seg_002", "reason": "opening_skipped_duplicate"},
        ],
        "exclude_rationales": {"seg_002": "opening_skipped_duplicate"},
    }
    repaired, actions = repair_air_order_integrity(ctx, selection)
    ordered = [str(x) for x in (repaired.get("ordered_segment_ids") or [])]
    assert "seg_002" in ordered
    assert "seg_003b" in ordered
    assert ordered.index("seg_002") < ordered.index("seg_003b")
    excl = {
        str(r.get("segment_id") if isinstance(r, dict) else r)
        for r in (repaired.get("excluded_segment_ids") or [])
    }
    assert "seg_002" not in excl
    assert (repaired.get("exclude_rationales") or {}).get("seg_002") != (
        "opening_skipped_duplicate"
    )
    assert actions


def test_exec_805_letter_split_family_passes():
    """Eight seg_001 letter-splits + seg_002 + seg_003 should not trip family slot cap."""
    ordered = [
        "seg_001c",
        "seg_001d",
        "seg_001e",
        "seg_001f",
        "seg_001h",
        "seg_001i",
        "seg_001j",
        "seg_001k",
        "seg_002",
        "seg_003",
        "seg_006",
    ]
    starts = {
        "seg_001": 0,
        "seg_002": 124_840,
        "seg_003": 151_660,
        "seg_006": 200_000,
    }
    policy = {
        "opening_window_ms": 180_000,
        "opening_air_slots": 6,
        "count_opening_by_family": True,
    }
    violations = late_opening_cluster_violations(
        None, ordered, starts=starts, policy=policy
    )
    assert not critical_violations(violations)


def test_late_opening_still_flags_true_late_cluster():
    ordered = ["seg_003", "seg_050", "seg_001c"]
    starts = {"seg_001": 0, "seg_003": 152_000, "seg_050": 2_416_000, "seg_001c": 0}
    policy = {"opening_window_ms": 180_000, "opening_air_slots": 6, "count_opening_by_family": True}
    violations = late_opening_cluster_violations(
        None, ordered, starts=starts, policy=policy
    )
    assert violations


def test_resolved_policy_in_report(tmp_path):
    class _Ctx:
        def __init__(self):
            self.run_dir = tmp_path
            self.logs: list[tuple] = []

        def write_json(self, rel, doc, stage_key=None):
            path = tmp_path / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            import json

            path.write_text(json.dumps(doc), encoding="utf-8")

        def path(self, *parts):
            p = tmp_path.joinpath(*parts)
            p.parent.mkdir(parents=True, exist_ok=True)
            return p

        def log(self, message, level="info", stage=None, detail=None):
            self.logs.append((message, level, stage))

        def artifact_exists(self, path: str) -> bool:
            return False

    ctx = _Ctx()
    policy = {
        "source_duration_ms": 3_600_000,
        "opening_air_slots": 6,
        "count_opening_by_family": True,
    }
    doc = write_air_order_integrity_report(
        ctx,
        violations=[],
        stage="test",
        resolved_policy=policy,
    )
    assert doc.get("resolved_policy") == policy

    # exec_036: an advisory critical finding (ranking does not block on it)
    # is a warning, not an error line that makes a healthy run read as failed.
    jump = {
        "code": "mid_arc_reverse_jump",
        "severity": "critical",
        "message": "Reverse tape jump: seg_038 -> seg_027 (source_gap_ms=-272400)",
    }
    write_air_order_integrity_report(
        ctx, violations=[jump], stage="test", resolved_policy=policy
    )
    assert [lvl for msg, lvl, _ in ctx.logs if "Reverse tape jump" in msg] == ["warning"]


def test_junction_source_skips_layup_invalidate(tmp_path, monkeypatch):
    """Junction remaster must not archive mix via layup invalidate."""
    monkeypatch.setenv("INTERVIEW_MUX_DATA_ROOT", str(tmp_path))
    from interview_mux.run_context import RunContext
    from interview_mux.air_order_integrity import on_selection_order_changed

    ctx = RunContext(create=True)
    ctx.write_json(
        "master/selection.json",
        {"ordered_segment_ids": ["seg_a", "seg_b"], "order_lock": {"revision": 1}},
        skip_handoff=True,
    )
    ctx.write_json(
        "understanding/nugget_layup_plan.json",
        {"ordered_segment_ids": ["seg_a", "seg_b"], "layups": []},
        skip_handoff=True,
    )
    mark_done_raw(ctx, "nugget_layup_compose")
    calls: list[str] = []

    def _fake_invalidate(_ctx, stage: str) -> None:
        calls.append(stage)

    monkeypatch.setattr(
        "interview_mux.homunculus.agenda.invalidate_downstream",
        _fake_invalidate,
    )
    notes = on_selection_order_changed(
        ctx,
        source="junction_snip_qa",
        previous={"ordered_segment_ids": ["seg_a", "seg_b", "seg_c"]},
        current={"ordered_segment_ids": ["seg_a", "seg_b"]},
    )
    assert "skipped_layup_invalidate:junction_source" in notes
    assert calls == []
    notes2 = on_selection_order_changed(
        ctx,
        source="full_master_ranking",
        previous={"ordered_segment_ids": ["seg_a", "seg_b", "seg_c"]},
        current={"ordered_segment_ids": ["seg_a", "seg_b"]},
    )
    assert "invalidated_downstream:nugget_layup_compose" in notes2
    assert calls == ["nugget_layup_compose"]


def test_exec_022_dense_accident_guest_first_projects_host_first():
    """Dense keep-all open: Mode A host-first prefix + overflow exclude → ok."""
    starts = {
        "seg_001": 5_699,
        "seg_002": 31_140,
        "seg_003": 41_880,
        "seg_004": 85_520,
        "seg_005": 102_740,
        "seg_006": 110_840,
        "seg_007": 134_479,
        "seg_008": 164_620,
        "seg_009": 172_560,
        "seg_012": 250_000,
    }
    ctx = _FakeCtx(starts)
    # Accident guest-first (seg_004 leads) with 7+ opening families — no cold-open flag.
    selection = {
        "ordered_segment_ids": [
            "seg_004",
            "seg_001",
            "seg_005",
            "seg_007",
            "seg_006",
            "seg_003",
            "seg_002",
            "seg_009",
            "seg_008",
            "seg_012",
        ],
        "excluded_segment_ids": [],
        "chapters": [
            {"title": "Open", "segment_ids": ["seg_004", "seg_001", "seg_005"]},
        ],
    }
    policy = {
        "opening_window_ms": 180_000,
        "opening_air_slots": 6,
        "count_opening_by_family": True,
    }
    before = late_opening_cluster_violations(
        ctx, selection["ordered_segment_ids"], starts=starts, policy=policy
    )
    assert critical_violations(before)
    repaired, actions = repair_opening_tape_integrity(ctx, selection)
    ordered = [str(s) for s in (repaired.get("ordered_segment_ids") or [])]
    assert ordered[0] == "seg_001"
    after = late_opening_cluster_violations(
        ctx, ordered, starts=starts, policy=policy
    )
    assert not critical_violations(after)
    excl = {
        str(r.get("segment_id") if isinstance(r, dict) else r)
        for r in (repaired.get("excluded_segment_ids") or [])
    }
    # Slot budget 6 → at least one opening family excluded.
    assert excl
    assert any(
        (repaired.get("exclude_rationales") or {}).get(s) == "opening_slot_overflow"
        or (
            isinstance(row, dict)
            and row.get("reason") == "opening_slot_overflow"
        )
        for s, row in [
            (
                str(r.get("segment_id") if isinstance(r, dict) else r),
                r,
            )
            for r in (repaired.get("excluded_segment_ids") or [])
        ]
    )
    # Idempotent.
    again, actions2 = repair_opening_tape_integrity(ctx, repaired)
    assert again.get("ordered_segment_ids") == ordered
    assert actions
    # Chapters pruned of excluded ids.
    ch_ids = (again.get("chapters") or [{}])[0].get("segment_ids") or []
    assert all(s in ordered for s in ch_ids)


def test_cold_open_mode_b_preserves_head_excludes_host_intro():
    starts = {
        "seg_001": 0,
        "seg_003": 50_000,
        "seg_050": 200_000,
    }
    ctx = _FakeCtx(starts)
    selection = {
        "ordered_segment_ids": ["seg_003", "seg_050", "seg_001"],
        "excluded_segment_ids": [],
        "native_cold_open_segment_id": "seg_003",
    }
    repaired, actions = repair_opening_tape_integrity(ctx, selection)
    ordered = [str(s) for s in (repaired.get("ordered_segment_ids") or [])]
    assert ordered[0] == "seg_003"
    assert "seg_001" not in ordered
    excl = {
        str(r.get("segment_id") if isinstance(r, dict) else r)
        for r in (repaired.get("excluded_segment_ids") or [])
    }
    assert "seg_001" in excl
    assert (repaired.get("exclude_rationales") or {}).get("seg_001") == (
        "opening_skipped_duplicate"
    )
    assert any(a.get("mode") == "cold_open" for a in actions)
    assert not late_opening_cluster_violations(ctx, ordered)


def test_slot_overflow_host_first_excludes_surplus():
    starts = {f"seg_{i:03d}": i * 15_000 for i in range(1, 12)}
    starts["seg_099"] = 400_000
    ctx = _FakeCtx(starts)
    opening = [f"seg_{i:03d}" for i in range(1, 10)]  # 9 opening families
    selection = {
        "ordered_segment_ids": opening + ["seg_099"],
        "excluded_segment_ids": [],
    }
    policy = {
        "opening_window_ms": 180_000,
        "opening_air_slots": 6,
        "count_opening_by_family": True,
    }
    repaired, _ = repair_opening_tape_integrity(ctx, selection)
    ordered = [str(s) for s in (repaired.get("ordered_segment_ids") or [])]
    assert ordered[0] == "seg_001"
    on_air_opening = [
        s
        for s in ordered
        if (starts.get(s) or 0) < 180_000
    ]
    assert len(on_air_opening) <= 6
    assert "seg_099" in ordered
    assert not critical_violations(
        late_opening_cluster_violations(ctx, ordered, starts=starts, policy=policy)
    )


def test_hard_keep_does_not_restore_opening_slot_overflow(tmp_path, monkeypatch):
    monkeypatch.setenv("INTERVIEW_MUX_DATA_ROOT", str(tmp_path))
    from interview_mux.run_context import RunContext
    from interview_mux.hard_keep import enforce_hard_keeps

    ctx = RunContext(create=True)
    (tmp_path / "segments").mkdir(parents=True, exist_ok=True)
    import json

    (tmp_path / "segments" / "boundaries.json").write_text(
        json.dumps(
            {
                "boundaries": [
                    {"segment_id": "seg_001", "start_ms": 0},
                    {"segment_id": "seg_007", "start_ms": 100_000},
                    {"segment_id": "seg_050", "start_ms": 500_000},
                ]
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        "interview_mux.hard_keep.hard_keep_segment_ids",
        lambda _ctx, **_k: {"seg_007", "seg_001", "seg_050"},
    )
    selection = {
        "ordered_segment_ids": ["seg_001", "seg_050"],
        "excluded_segment_ids": [
            {"segment_id": "seg_007", "reason": "opening_slot_overflow"}
        ],
        "exclude_rationales": {"seg_007": "opening_slot_overflow"},
    }
    out = enforce_hard_keeps(ctx, selection)
    ordered = out.get("ordered_segment_ids") or []
    assert "seg_007" not in ordered
    excl = {
        str(r.get("segment_id") if isinstance(r, dict) else r)
        for r in (out.get("excluded_segment_ids") or [])
    }
    assert "seg_007" in excl


def test_incomplete_seam_does_not_pull_constitution_exclude():
    from interview_mux.air_order_integrity import repair_incomplete_seam_order

    spans = {
        "seg_001": {
            "start_ms": 0,
            "end_ms": 1000,
            "text": "we call it provision",
            "speaker_id": "spk_0",
        },
        "seg_002": {
            "start_ms": 1100,
            "end_ms": 2000,
            "text": "called LDT for short",
            "speaker_id": "spk_0",
        },
    }
    ordered, actions = repair_incomplete_seam_order(
        ["seg_001"],
        spans,
        blocked_ids={"seg_002"},
    )
    assert "seg_002" not in ordered
    assert not any(a.get("action") == "incomplete_seam_pull_completion" for a in actions)


def test_late_opening_heal_pins_sanitize_not_ranking():
    from interview_mux.stage_completion import producer_pin_for_token

    assert (
        producer_pin_for_token(
            "air_order_integrity_critical:late_opening_cluster"
        )
        == "selection_order_sanitize"
    )
    assert producer_pin_for_token("late_opening_cluster") == "selection_order_sanitize"


def test_finalize_reports_residual_late_opening_without_refusing(monkeypatch, tmp_path):
    """With block_ranking_on_critical false, a residual late_opening is a warning (ISSUES 185)."""
    monkeypatch.setenv("INTERVIEW_MUX_DATA_ROOT", str(tmp_path))
    from interview_mux.run_context import RunContext
    from interview_mux.stages.selection import finalize_selection_order

    ctx = RunContext(create=True)

    def _noop_repair(_ctx, selection, **_kw):
        return selection, []

    def _critical_late(_ctx, selection, **_kw):
        return [
            {
                "code": "late_opening_cluster",
                "severity": "critical",
                "message": "Opening-tape cluster ['seg_001'] after guest-first open at index 2",
            }
        ]

    monkeypatch.setattr(
        "interview_mux.air_order_integrity.repair_air_order_integrity",
        _noop_repair,
    )
    monkeypatch.setattr(
        "interview_mux.air_order_integrity.collect_violations",
        _critical_late,
    )
    monkeypatch.setattr(
        "interview_mux.air_order_integrity.block_ranking_on_critical",
        lambda: False,
    )
    monkeypatch.setattr(
        "interview_mux.hard_keep.enforce_hard_keeps",
        lambda _ctx, sel: sel,
    )
    monkeypatch.setattr(
        "interview_mux.selection_order_repair.repair_selection_order",
        lambda sel, *a, **k: (sel, []),
    )
    artifacts = {
        "ordered_segment_ids": ["seg_003", "seg_050", "seg_001"],
        "excluded_segment_ids": [],
    }
    logged: list[str] = []
    real_log = ctx.log

    def _spy(msg, *a, **k):
        logged.append(str(msg))
        return real_log(msg, *a, **k)

    monkeypatch.setattr(ctx, "log", _spy)
    out = finalize_selection_order(
        ctx, artifacts, stage="full_master_ranking", skip_lifecycle=True
    )
    assert out["ordered_segment_ids"]
    assert any("late_opening" in m.lower() or "unresolved critical" in m for m in logged)

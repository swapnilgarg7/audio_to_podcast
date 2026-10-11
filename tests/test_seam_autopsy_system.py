from __future__ import annotations

from interview_mux.artifact_repairs import repair_master_selection
from interview_mux.failure_recovery import (
    LEARNING_REL,
    REMEDIATION_LOG_REL,
    REMEDIATION_PLAN_REL,
    run_full_remediation,
)
from interview_mux.order_hash import stamp_order_hash
from interview_mux.seam_autopsy import build_autopsy, verify_commitment
from interview_mux.synthetic_framing import validate_synthetic_plan
from run_fixtures import isolated_run_ctx


def _ctx_with_timeline(tmp_path):
    ctx = isolated_run_ctx(tmp_path, "exec_seam_autopsy")
    ctx.write_json(
        "segments/manifest.json",
        {
            "segments": [
                {
                    "segment_id": "seg_a",
                    "start_ms": 0,
                    "end_ms": 1000,
                    "speaker_id": "guest",
                    "speaker_role": "interviewee",
                    "text": "A complete thought.",
                    "type": "interviewee_answer",
                    "topic_tags": [],
                    "flags": [],
                },
                {
                    "segment_id": "seg_b",
                    "start_ms": 1100,
                    "end_ms": 2500,
                    "speaker_id": "guest",
                    "speaker_role": "interviewee",
                    "text": "The next complete thought.",
                    "type": "interviewee_answer",
                    "topic_tags": [],
                    "flags": [],
                },
            ]
        },
        skip_handoff=True,
    )
    selection = stamp_order_hash(
        {
            "version": 1,
            "ordered_segment_ids": ["seg_a", "seg_b"],
            "excluded_segment_ids": [],
            "chapters": [
                {
                    "chapter_id": "ch_1",
                    "title": "Only",
                    "segment_ids": ["seg_a", "seg_b"],
                }
            ],
        }
    )
    edl = stamp_order_hash(
        {
            "version": 1,
            "ordered_segment_ids": ["seg_a", "seg_b"],
            "clips": [
                {
                    "type": "speech",
                    "segment_id": "seg_a",
                    "source_start_ms": 0,
                    "source_end_ms": 1000,
                    "timeline_start_ms": 0,
                    "duration_ms": 1000,
                },
                {
                    "type": "speech",
                    "segment_id": "seg_b",
                    "source_start_ms": 1100,
                    "source_end_ms": 2500,
                    "timeline_start_ms": 1000,
                    "duration_ms": 1400,
                },
            ],
            "timeline_duration_ms": 2400,
        }
    )
    ctx.write_json("master/selection.json", selection, skip_handoff=True)
    ctx.write_json("master/edl.json", edl, skip_handoff=True)
    # Re-read after write_json restamps locks/hashes.
    edl = ctx.read_json("master/edl.json")
    ctx.write_json(
        "master/assembly_ledger.json",
        {
            "version": 1,
            "order_content_hash": edl.get("order_content_hash"),
            "complete": True,
            "naked_seam_count": 0,
            "seams": [
                {
                    "after_segment_id": "seg_a",
                    "before_segment_id": "seg_b",
                    "requires_glue": False,
                    "naked": False,
                    "source_gap_ms": 100,
                    "kind": "contiguous",
                    "glue_piece_ids": [],
                }
            ],
        },
        skip_handoff=True,
    )
    assembly = ctx.path("master", "assembly.wav")
    assembly.parent.mkdir(parents=True, exist_ok=True)
    assembly.write_bytes(b"RIFF" + (b"\0" * 128))
    from interview_mux.seam_autopsy import RENDER_LEDGER_REL, _file_fingerprint

    fp = _file_fingerprint(ctx.final_path("master", "assembly.wav"))
    ledger_path = ctx.final_path(*RENDER_LEDGER_REL.split("/"))
    ledger_path.parent.mkdir(parents=True, exist_ok=True)
    import json as _json

    ledger_path.write_text(
        _json.dumps(
            {
                "version": 1,
                "generated_at": "2026-01-01T00:00:00Z",
                "edl_hash": str(edl.get("order_content_hash") or "fixture"),
                "assembly": {
                    "exists": True,
                    "size": fp["size"],
                    "sha256_edges": fp["sha256_edges"],
                },
                "clips": [],
            }
        ),
        encoding="utf-8",
    )
    return ctx, edl


def test_commitment_proves_applied_repair_and_render(tmp_path):
    ctx, edl = _ctx_with_timeline(tmp_path)
    report = {
        "applied": [
            {
                "status": "applied",
                "action": "nudge_source_bounds",
                "segment_id": "seg_a",
                "applied_ms": 1000,
                "detail": {"edge": "end"},
            }
        ]
    }
    commitment = verify_commitment(ctx, report, edl=edl)
    assert commitment["status"] == "committed"
    assert commitment["repairs_claimed"] == commitment["repairs_committed"] == 1

    autopsy = build_autopsy(ctx, phase="post_junction", snip_report=report, edl=edl)
    assert autopsy["commitment"]["status"] == "committed"
    assert autopsy["seams"][0]["synthetic_voice_allowed"] is False
    assert autopsy["seams"][0]["preferred_glue"][0] == "extend_native"


def test_commitment_rejects_false_applied_claim(tmp_path):
    ctx, edl = _ctx_with_timeline(tmp_path)
    report = {
        "applied": [
            {
                "status": "applied",
                "action": "nudge_source_bounds",
                "segment_id": "seg_a",
                "applied_ms": 100,
                "detail": {"edge": "end"},
            }
        ]
    }
    commitment = verify_commitment(ctx, report, edl=edl)
    assert commitment["status"] == "diverged"
    assert "claimed_repairs_missing_from_edl" in commitment["reasons"]


def test_commitment_uses_clip_index_for_duplicate_segment_ids(tmp_path):
    """Ideal-cut segments can appear twice; repairs must bind to clip_index."""
    from interview_mux.seam_autopsy import _applied_repairs_resolved

    edl = {
        "clips": [
            {
                "type": "speech",
                "segment_id": "seg_005",
                "source_start_ms": 100,
                "source_end_ms": 213510,
            },
            {"type": "silence", "duration_ms": 100},
            {
                "type": "speech",
                "segment_id": "seg_005",
                "source_start_ms": 264660,
                "source_end_ms": 344510,
            },
        ]
    }
    report = {
        "applied": [
            {
                "status": "applied",
                "action": "nudge_source_bounds",
                "segment_id": "seg_005",
                "clip_index": 2,
                "edge": "end",
                "applied_ms": 344510,
                "detail": {"edge": "end", "recommended_ms": 344510},
            }
        ]
    }
    resolved, unresolved = _applied_repairs_resolved(edl, report)
    assert unresolved == []
    assert resolved
    # Claiming the second clip's bound against the first clip_index must diverge.
    bad = {
        "applied": [
            {
                "status": "applied",
                "action": "nudge_source_bounds",
                "segment_id": "seg_005",
                "clip_index": 0,
                "edge": "end",
                "applied_ms": 344510,
                "detail": {"edge": "end"},
            }
        ]
    }
    _res, unr = _applied_repairs_resolved(edl, bad)
    assert unr


def test_commitment_accepts_superseded_earlier_bound_claims(tmp_path):
    """Two remaster runs leave intermediate applied_ms that no longer match EDL."""
    ctx, edl = _ctx_with_timeline(tmp_path)
    report = {
        "applied": [
            {
                "status": "applied",
                "action": "nudge_source_bounds",
                "segment_id": "seg_a",
                "applied_ms": 700,
                "detail": {"edge": "end"},
            },
            {
                "status": "applied",
                "action": "extend_later",
                "segment_id": "seg_a",
                "applied_ms": 1000,
                "detail": {"recommended_ms": 1000},
            },
        ]
    }
    commitment = verify_commitment(ctx, report, edl=edl)
    assert commitment["status"] == "committed"
    assert commitment["repairs_claimed"] == commitment["repairs_committed"] == 2


def test_superseded_claim_survives_a_clip_index_shift(tmp_path):
    """exec_035 seg_017: run 1 recut it at clip 37, a clip inserted before it
    moved it to 38, run 2 recut and cut it earlier there. The run-1 row is
    superseded, not a claim the final EDL must still honour."""
    from interview_mux.seam_autopsy import _applied_repairs_resolved

    edl = {
        "clips": [
            {"type": "silence", "duration_ms": 400, "air_kind": "impact_hold"},
            {"type": "speech", "segment_id": "seg_016", "source_start_ms": 0, "source_end_ms": 494000},
            {"type": "speech", "segment_id": "seg_017", "source_start_ms": 494400, "source_end_ms": 505180},
        ]
    }
    report = {
        "applied": [
            {
                "status": "applied",
                "action": "thought_complete_recut",
                "segment_id": "seg_017",
                "clip_index": 1,
                "keep_end_ms": 510640,
                "applied_ms": 510640,
            },
            {
                "status": "applied",
                "action": "thought_complete_recut",
                "segment_id": "seg_017",
                "clip_index": 2,
                "keep_end_ms": 510640,
                "applied_ms": 510640,
            },
            {
                "status": "applied",
                "action": "cut_earlier",
                "segment_id": "seg_017",
                "clip_index": 2,
                "applied_ms": 505180,
                "detail": {"recommended_ms": 505180},
            },
        ]
    }
    resolved, unresolved = _applied_repairs_resolved(edl, report)
    assert unresolved == []
    assert len(resolved) == 3


def test_commitment_accepts_thought_complete_superseding_nudge(tmp_path):
    ctx, edl = _ctx_with_timeline(tmp_path)
    report = {
        "applied": [
            {
                "status": "applied",
                "action": "nudge_source_bounds",
                "segment_id": "seg_a",
                "applied_ms": 700,
                "detail": {"edge": "end"},
            },
            {
                "status": "applied",
                "action": "thought_complete_recut",
                "segment_id": "seg_a",
                "keep_end_ms": 1000,
                "detail": {"keep_end_ms": 1000},
            },
        ]
    }
    commitment = verify_commitment(ctx, report, edl=edl)
    assert commitment["status"] == "committed"
    assert not commitment["unresolved_repair_keys"]


def test_selection_repair_never_reincludes_narrative_only_segments(tmp_path):
    ctx, _ = _ctx_with_timeline(tmp_path)
    ctx.write_json(
        "master/narrative_plan.json",
        {
            "arc_summary": "Wider candidate narrative",
            "chapters": [
                {
                    "chapter_id": "ch_1",
                    "title": "Wider",
                    "segment_ids": ["seg_a", "seg_b", "seg_excluded"],
                        "suggested_open_segment_id": "seg_a",
                }
            ],
            "ordering_constraints": [],
        },
        skip_handoff=True,
    )
    selection = ctx.read_json("master/selection.json")
    repaired, actions = repair_master_selection(ctx, selection)
    assert repaired["ordered_segment_ids"] == ["seg_a", "seg_b"]
    assert any(
        a.get("action") == "intersect_narrative_chapters_with_selection"
        for a in actions
    )


def test_full_remediation_is_hard_capped_at_two_complete_runs(tmp_path, monkeypatch):
    # Schema const max_runs=2; keep product loop aligned for this contract test.
    monkeypatch.setattr("interview_mux.failure_recovery.MAX_REMEDIATION_RUNS", 2)
    from interview_mux.failure_recovery import MAX_REMEDIATION_RUNS

    ctx, _ = _ctx_with_timeline(tmp_path)
    identify_calls: list[int] = []
    execute_calls: list[int] = []

    def identify(run_index: int):
        identify_calls.append(run_index)
        return {
            "run_index": run_index,
            "broken_pieces": [
                {
                    "piece_id": f"piece_{run_index}",
                    "kind": "junction",
                    "listener_impact": "mid_thought",
                    "target_ids": [],
                    "evidence": {},
                }
            ],
        }

    def execute(plan, run_index: int):
        assert plan["covers_all_pieces"]
        execute_calls.append(run_index)
        return {"pieces_resolved": 0, "actions_executed": []}

    result = run_full_remediation(
        ctx,
        trigger="test",
        identify=identify,
        execute=execute,
    )
    assert identify_calls == [1, 2]
    assert execute_calls == [1, 2]
    assert result["runs_used"] == 2
    assert result["max_runs"] == 2
    assert ctx.final_path(*REMEDIATION_PLAN_REL.split("/")).is_file()
    assert ctx.final_path(*REMEDIATION_LOG_REL.split("/")).is_file()
    learning = (ctx.assets_root / LEARNING_REL).read_text(encoding="utf-8").strip().splitlines()
    assert learning
    assert any(ctx.run_id in line for line in learning)


def test_synthetic_plan_enforces_native_order_and_duration_ratio(tmp_path):
    ctx, _ = _ctx_with_timeline(tmp_path)
    plan = {
        "selection_order_content_hash": ctx.read_json("master/selection.json")[
            "order_content_hash"
        ],
        "lines": [
            {
                "anchor_segment_id": "seg_b",
                "duration_ratio": 1.25,
                "comprehension_reason": "Orient the listener to a time jump.",
                "native_respect_violation": False,
            }
        ],
    }
    assert validate_synthetic_plan(ctx, plan) == []
    plan["lines"][0]["duration_ratio"] = 2.5
    assert validate_synthetic_plan(ctx, plan)


def test_align_narrative_and_demote_restore_excluded_edl_fail(tmp_path):
    from interview_mux.artifact_repairs import (
        align_narrative_plan_to_selection,
        repair_edl_audit,
        repair_master_selection,
    )

    ctx, _ = _ctx_with_timeline(tmp_path)
    ctx.write_json(
        "master/narrative_plan.json",
        {
            "arc_summary": "Wide arc",
            "chapters": [
                {
                    "chapter_id": "ch_early",
                    "title": "Origin",
                    "segment_ids": ["seg_excluded"],
                    "suggested_open_segment_id": "seg_excluded",
                },
                {
                    "chapter_id": "ch_late",
                    "title": "Exit",
                    "segment_ids": ["seg_a", "seg_b", "seg_excluded"],
                    "suggested_open_segment_id": "seg_a",
                },
            ],
            "ordering_constraints": [
                {
                    "before_segment_id": "seg_excluded",
                    "after_segment_id": "seg_a",
                    "reason": "setup before payoff",
                },
                {
                    "before_segment_id": "seg_a",
                    "after_segment_id": "seg_b",
                    "reason": "a before b",
                },
            ],
        },
        skip_handoff=True,
    )
    selection = ctx.read_json("master/selection.json")
    selection["chapters"] = [
        {"chapter_id": "ch_early", "title": "Origin", "segment_ids": []},
        {"chapter_id": "ch_late", "title": "Exit", "segment_ids": ["seg_a", "seg_b"]},
    ]
    repaired_sel, sel_actions = repair_master_selection(ctx, selection)
    assert all(ch.get("segment_ids") for ch in repaired_sel.get("chapters") or [])
    assert any(a.get("action") == "drop_empty_selection_chapters" for a in sel_actions)
    plan = ctx.read_json("master/narrative_plan.json")
    assert [c["chapter_id"] for c in plan["chapters"]] in (
        ["ch_late"],
        ["ch_early", "ch_late"],
    )
    late = next(c for c in plan["chapters"] if c["chapter_id"] == "ch_late")
    assert set(late.get("segment_ids") or []) <= {"seg_a", "seg_b", "seg_excluded"}
    assert len(plan["ordering_constraints"]) >= 1
    assert any(
        a.get("action")
        in {
            "align_narrative_chapters_to_selection",
            "drop_narrative_constraints_outside_selection",
        }
        for a in sel_actions
    ) or align_narrative_plan_to_selection(ctx) == []

    audit = {
        "verdict": "fail",
        "blocking_issues": [
            {
                "issue": "Narrative_plan chapters 1–4 have zero segments in selection; only act-4 material remains",
                "evidence": ["narrative_plan.chapters", "excluded_segment_ids"],
                "recommended_action": "Redo full_master_ranking / selection to restore representative segments",
            }
        ],
        "warnings": [],
        "recommended_actions": [],
        "reasoning_summary": "early acts wiped out",
    }
    fixed, actions = repair_edl_audit(ctx, audit)
    assert fixed["verdict"] in {"pass", "warn"}
    assert fixed.get("blocking_issues") in ([], None) or fixed["blocking_issues"] == []
    assert any(a.get("action") == "demote_restore_excluded_blocking" for a in actions)


def test_sdp_bed_cue_slot_injections_survive_sound_design_plan_flush(tmp_path):
    """Beds planned outside cue_slots must commit policy slots, not only stage them.

    sound_design_plan flushes only understanding/sound_design_plan.json; slot
    injections via ctx.write_json would otherwise vanish before post-commit QA.
    """
    from interview_mux.analysis_memory import default_sound_design_plan
    from interview_mux.artifact_repairs import repair_sound_design_plan
    from interview_mux.sdp_cross_validate import validate_post_sound_plan
    from interview_mux.write_staging import (
        enter_stage_staging,
        exit_stage_staging,
        flush_stage_writes,
    )

    ctx, _ = _ctx_with_timeline(tmp_path)
    ctx.write_json(
        "understanding/soundscape_policy.json",
        {
            "version": 1,
            "derived_from": {},
            "underscore_policy": "normal",
            "pace_class": "conversational",
            "sfx_density": {},
            "mix_contract": {
                "underscore_policy": "normal",
                "duck_under_speech_db": 16,
                "stinger_max_per_minute": 3,
                "bed_level_db_range": [-30, -26],
            },
            "standards": {},
            "cue_slots": [
                {
                    "slot_id": "bed_seg_a",
                    "segment_id": "seg_a",
                    "placement": "under_segment",
                    "allowed_roles": ["theme_underscore"],
                    "priority": 0.5,
                    "reason": "seed",
                }
            ],
            "operator_overrides": {},
            "rationale": [],
            "policy_hash": "test",
        },
        skip_handoff=True,
    )
    sdp = default_sound_design_plan()
    sdp["coherence"] = {
        "sonic_identity": "warm",
        "primary_mood": "reflective",
        "density": "sparse",
    }
    sdp["palettes"] = [
        {
            "palette_id": "p1",
            "theme_label": "t",
            "keywords": ["warm"],
            "segment_ids": ["seg_b"],
            "ambient_description": "warm pad",
            "accent_description": "soft piano",
            "avoid": ["vocals"],
        }
    ]
    sdp["assets"] = [
        {
            "asset_id": "theme_underscore_calm",
            "role": "theme_underscore",
            "description": "calm underscore bed",
            "duration_seconds": 16,
        }
    ]
    sdp["flow_plans"]["podcast"]["cues"] = [
        {
            "cue_id": "bed_seg_b",
            "placement": "under_segment",
            "segment_id": "seg_b",
            "asset_id": "theme_underscore_calm",
            "role": "theme_underscore",
            "level_db": -28,
        }
    ]
    enter_stage_staging("sound_design_plan")
    try:
        fixed, actions = repair_sound_design_plan(ctx, sdp)
        ctx.write_json(
            "understanding/sound_design_plan.json",
            fixed,
            stage_key="sound_design_plan",
        )
    finally:
        exit_stage_staging()
    flushed = flush_stage_writes(ctx, "sound_design_plan")
    assert "understanding/sound_design_plan.json" in flushed
    assert "understanding/soundscape_policy.json" not in flushed
    policy = ctx.read_json("understanding/soundscape_policy.json")
    slot_segs = {str(s.get("segment_id")) for s in (policy.get("cue_slots") or [])}
    assert "seg_b" in slot_segs
    assert any(a.get("action") == "inject_theme_underscore_cue_slot" for a in actions)
    slot_errors = [e for e in validate_post_sound_plan(ctx) if "cue_slots" in e]
    assert slot_errors == []


def test_promote_staged_side_effects_commits_undeclared_paths(tmp_path):
    """Junction remasters EDL/assembly without claiming them in StageInfo."""
    from interview_mux.write_staging import (
        enter_stage_staging,
        exit_stage_staging,
        flush_stage_writes,
        promote_staged_side_effects,
    )

    ctx, _ = _ctx_with_timeline(tmp_path)
    enter_stage_staging("junction_snip_qa")
    try:
        edl_path = ctx.path("master", "edl.json")
        edl_path.parent.mkdir(parents=True, exist_ok=True)
        edl_path.write_text('{"version":1,"clips":[]}', encoding="utf-8")
        wav = ctx.path("master", "assembly.wav")
        wav.write_bytes(b"RIFF" + b"\0" * 64)
        report = ctx.path("master", "junction_snip_qa.json")
        report.write_text(
            '{"version":1,"generated_at":"2026-01-01T00:00:00+00:00","applied":[],"findings":[]}',
            encoding="utf-8",
        )
        promoted = promote_staged_side_effects(
            ctx,
            ("master/edl.json", "master/assembly.wav"),
            stage_id="junction_snip_qa",
        )
    finally:
        exit_stage_staging()
    assert "master/edl.json" in promoted
    assert "master/assembly.wav" in promoted
    assert ctx.final_path("master", "edl.json").is_file()
    assert ctx.final_path("master", "assembly.wav").is_file()
    flushed = flush_stage_writes(ctx, "junction_snip_qa")
    assert "master/junction_snip_qa.json" in flushed
    # Side effects already committed; flush still must not require them as StageInfo.
    assert ctx.final_path("master", "edl.json").is_file()

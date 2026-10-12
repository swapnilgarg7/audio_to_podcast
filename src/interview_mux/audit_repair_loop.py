"""Audit → narrative alignment loop for story-unsafe masters.

Selection is the air-order authority. When an EDL narrative audit fails,
align narrative metadata down to selection and demote disk-stale blockers —
never expand selection, never remutate/clear markers (ENA S7), never bind
coverage (ENA S8 — topic_coverage owns).
"""

from __future__ import annotations

from typing import Any

from interview_mux.run_context import RunContext


def _audit_fail(doc: dict[str, Any] | None) -> bool:
    if not isinstance(doc, dict):
        return False
    return str(doc.get("verdict") or "").strip().lower() == "fail"


def maybe_repair_after_narrative_audit(ctx: RunContext, artifacts: dict[str, Any]) -> dict[str, Any]:
    """Fail → one metadata align + demote. Refuse if still fail (no remutate)."""
    if not _audit_fail(artifacts):
        return artifacts

    from interview_mux.artifact_repairs import (
        align_narrative_plan_to_selection,
        repair_edl_audit,
    )

    notes: list[dict[str, Any]] = []
    if ctx.artifact_exists("master/selection.json"):
        try:
            from interview_mux.media_ip_cta import (
                heal_on_air_cta_residue,
                on_air_orphaned_cta_scrap_ids,
            )

            scraps = on_air_orphaned_cta_scrap_ids(ctx)
            if scraps:
                heal_on_air_cta_residue(ctx)
                notes.append(
                    {
                        "action": "orphaned_cta_child_omit",
                        "ids": scraps[:12],
                    }
                )
        except Exception:
            pass
    if ctx.artifact_exists("master/selection.json"):
        skip_sel_write = False
        try:
            from interview_mux.seat_authority import hard_freeze_active

            skip_sel_write = bool(hard_freeze_active(ctx))
        except Exception:
            skip_sel_write = False

        if skip_sel_write:
            notes.append(
                {
                    "action": "skip_selection_write_hard_freeze",
                    "reason": "narrative_aligns_plan_only",
                }
            )
            ctx.log(
                "edl_narrative_audit repair: skip selection rewrite under hard freeze",
                level="info",
                stage="edl_narrative_audit",
            )
            # The freeze policy allows narrative_metadata_align under the hard
            # freeze: chapter labels may still follow the locked order. Skipping
            # them left a true chapter_continuity_broken blocker that no pass
            # could clear (exec_013, ISSUES 152).
            try:
                from interview_mux.artifact_ownership import freeze_write_allowed
                from interview_mux.artifact_repairs import relabel_chapters_contiguous

                # The freeze policy row is necessary, not sufficient: the
                # ownership table can still refuse the persist (a hard freeze
                # owned by selection_framing_apply, exec on 2026-10-11, ISSUES
                # 190). Ask it first so a refusal costs a note, not an
                # error-level denial from inside the audit's finish path.
                from interview_mux.artifact_ownership import write_permitted

                may_persist = freeze_write_allowed(
                    ctx, "edl_narrative_audit", "narrative_metadata_align"
                )
                if may_persist:
                    try:
                        may_persist, deny_reason = write_permitted(
                            ctx,
                            "master/selection.json",
                            "edl_narrative_audit",
                            role="producer",
                            verb="persist",
                            mutation_class="narrative_metadata_align",
                        )
                    except Exception:
                        may_persist, deny_reason = False, "write_permitted_failed"
                    if not may_persist:
                        notes.append(
                            {
                                "action": "skip_relabel_chapters_not_permitted",
                                "reason": str(deny_reason)[:120],
                            }
                        )
                        ctx.log(
                            "edl_narrative_audit repair: chapter relabel not persisted "
                            f"({deny_reason}); the selection owner keeps the labels",
                            level="info",
                            stage="edl_narrative_audit",
                        )
                if may_persist:
                    sel_now = ctx.read_json("master/selection.json")
                    relabeled, changed = relabel_chapters_contiguous(
                        sel_now if isinstance(sel_now, dict) else {}
                    )
                    if changed:
                        from interview_mux.air_order_boundary import commit_selection_mutation

                        commit_selection_mutation(
                            ctx,
                            relabeled,
                            producer="edl_narrative_metadata_align",
                            stage_key="edl_narrative_audit",
                            checkpoint_mode="detect",
                            merge_from_disk=False,
                            write_committed=True,
                            mutation_class="narrative_metadata_align",
                        )
                        notes.append({"action": "relabel_chapters_contiguous_hard_freeze"})
            except Exception as exc:
                notes.append({"action": "relabel_chapters_failed", "error": type(exc).__name__})
            notes.extend(align_narrative_plan_to_selection(ctx))
        else:
            from interview_mux.edl_narrative_remutate import (
                apply_edl_narrative_metadata_align,
            )

            meta_out = apply_edl_narrative_metadata_align(ctx)
            for item in meta_out.get("notes") or []:
                if isinstance(item, dict):
                    notes.append(item)
                else:
                    notes.append({"action": str(item)})

    out = dict(artifacts)
    repaired_audit, audit_notes = repair_edl_audit(ctx, out)
    notes.extend(audit_notes)
    out = repaired_audit
    out["repair_attempted"] = True
    out["repair_notes"] = notes[:12]
    # ENA S7: never remutate / clear markers / repair_done re-entry.
    out.pop("remutate", None)
    out.pop("remutate_applied", None)
    if _audit_fail(out):
        ctx.log(
            "edl_narrative_audit fail after demote — leaving for operator "
            f"({len(notes)} notes; no remutate)",
            level="error",
            stage="edl_narrative_audit",
            detail=notes[:8],
        )
    else:
        ctx.log(
            f"edl_narrative_audit fail → demoted/aligned ({len(notes)} notes)",
            level="warning",
            stage="edl_narrative_audit",
            detail=notes[:8],
        )
    return out

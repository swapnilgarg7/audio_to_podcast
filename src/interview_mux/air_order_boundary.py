"""Federated air-order boundary bus: constitution checkpoints + mutation lifecycle."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Literal

from interview_mux.run_context import RunContext

CheckpointMode = Literal["detect", "repair", "block"]

_NLE_SOFTEN_CODES = frozenset(
    {"mid_arc_reverse_jump", "late_opening_cluster", "chapter_opening_mask"}
)

_PRODUCER_DETECT_ONLY = frozenset(
    {
        "edl_narrative_remutate",
        "junction_snip_qa",
        "air_order",
        "timeline_optimizer",
    }
)


@dataclass
class CheckpointResult:
    violations: list[dict[str, Any]] = field(default_factory=list)
    actions: list[dict[str, Any]] = field(default_factory=list)
    critical_remaining: bool = False
    nle_overlay_softened: bool = False
    repaired: bool = False


def nle_overlay_active(ctx: RunContext) -> bool:
    try:
        from interview_mux.nle_state import load_nle, nle_has_operator_edits

        return nle_has_operator_edits(load_nle(ctx))
    except Exception:
        return False


def _effective_mode(producer: str, mode: CheckpointMode) -> CheckpointMode:
    if producer in _PRODUCER_DETECT_ONLY and mode in {"repair", "block"}:
        return "detect"
    return mode


def _critical_for_blocking(
    violations: list[dict[str, Any]],
    *,
    nle_softened: bool,
) -> list[dict[str, Any]]:
    from interview_mux.air_order_integrity import critical_violations

    critical = critical_violations(violations)
    if not nle_softened:
        return critical
    return [v for v in critical if str(v.get("code") or "") not in _NLE_SOFTEN_CODES]


def checkpoint_air_order(
    ctx: RunContext,
    selection: dict[str, Any],
    *,
    producer: str,
    mode: CheckpointMode = "repair",
) -> tuple[dict[str, Any], CheckpointResult]:
    """Run constitution checkpoint after stage domain logic, before persist."""
    from interview_mux.air_order_integrity import (
        collect_violations,
        repair_air_order_integrity,
        write_air_order_integrity_report,
    )

    effective = _effective_mode(producer, mode)
    out = dict(selection)
    actions: list[dict[str, Any]] = []
    repaired = False
    if effective in {"repair", "block"}:
        out, actions = repair_air_order_integrity(ctx, out)
        repaired = bool(actions)
    violations = collect_violations(ctx, out)
    nle_soft = nle_overlay_active(ctx)
    critical_remaining = bool(_critical_for_blocking(violations, nle_softened=nle_soft))
    result = CheckpointResult(
        violations=violations,
        actions=actions,
        critical_remaining=critical_remaining,
        nle_overlay_softened=nle_soft,
        repaired=repaired,
    )
    write_air_order_integrity_report(
        ctx,
        violations=violations,
        actions=actions,
        stage=producer,
        repaired=repaired,
    )
    return out, result


def should_block_commit(
    result: CheckpointResult,
    *,
    producer: str,
    mode: CheckpointMode,
) -> bool:
    from interview_mux.air_order_integrity import block_ranking_on_critical

    if _effective_mode(producer, mode) == "detect":
        return False
    if not block_ranking_on_critical():
        return False
    return result.critical_remaining


def _previous_selection(ctx: RunContext) -> dict[str, Any] | None:
    if not ctx.artifact_exists("master/selection.json"):
        return None
    try:
        doc = ctx.read_json("master/selection.json")
    except Exception:
        return None
    return dict(doc) if isinstance(doc, dict) else None


def drop_blank_segments(ctx: RunContext, selection: dict[str, Any]) -> dict[str, Any]:
    """Exclude blank/unusable air ids (hard-keeps exempt; never empties the order).

    Applied once before the freeze, at selection_order_sanitize (ISSUES 115),
    and again after a refused order change under freeze.
    """
    return _drop_blank_segments_under_freeze(ctx, selection)


def _drop_blank_segments_under_freeze(
    ctx: RunContext, selection: dict[str, Any]
) -> dict[str, Any]:
    """After seat-freeze order restore, still drop blank/unusable air ids.

    Freeze protects creative order thrash — not silent blank clips that split
    chapters and block EDL narrative QC (exec_11630 seg_041).
    """
    from interview_mux.artifact_repairs import _segment_is_blank_or_unusable
    from interview_mux.artifact_repairs import reconcile_ordered_vs_excluded

    out = dict(selection)
    order = [str(s) for s in (out.get("ordered_segment_ids") or []) if s]
    try:
        from interview_mux.hard_keep import hard_keep_segment_ids

        hard_keeps = {str(s) for s in (hard_keep_segment_ids(ctx) or []) if s}
    except Exception:
        hard_keeps = set()
    blank = [
        s
        for s in order
        if _segment_is_blank_or_unusable(ctx, s) and s not in hard_keeps
    ]
    if not blank:
        return out
    kept = [s for s in order if s not in set(blank)]
    if not kept:
        return out
    out["ordered_segment_ids"] = kept
    excl = list(out.get("excluded_segment_ids") or [])
    have = {str(r.get("segment_id") if isinstance(r, dict) else r) for r in excl}
    rat = dict(out.get("exclude_rationales") or {})
    for sid in blank:
        if sid not in have:
            excl.append({"segment_id": sid, "reason": "blank_or_unusable_answer_audio"})
            have.add(sid)
        rat[sid] = str(rat.get(sid) or "blank_or_unusable_answer_audio")
    out["excluded_segment_ids"] = excl
    out["exclude_rationales"] = rat
    keep = set(kept)
    for ch in out.get("chapters") or []:
        if isinstance(ch, dict):
            ch["segment_ids"] = [
                str(x) for x in (ch.get("segment_ids") or []) if str(x) in keep
            ]
    out = reconcile_ordered_vs_excluded(out)
    try:
        from interview_mux.order_hash import bump_order_lock

        out = bump_order_lock(out, source="air_order_boundary.drop_blank_under_freeze")
    except Exception:
        pass
    return out


def _retire_consumed_under_freeze(
    ctx: RunContext, selection: dict[str, Any]
) -> dict[str, Any]:
    """After a seat-freeze order restore, a union-absorbed id still leaves air.

    A fuse / overlap union already committed the merge on the EDL and in the
    manifest; the consumed id's tape airs under its survivor. Restoring the
    id into the order strands the selection at n+1 versus the EDL, every
    rebuild re-seats it, the next mix merges it again, and the walk ends at
    the invoke cap (exec_041 seg_078, Nicket's exec_033; ISSUES 195). The
    retire is bookkeeping, not a seat opportunity, so it lands here whatever
    else the gate refused in the same write.
    """
    from interview_mux.artifact_repairs import reconcile_ordered_vs_excluded

    out = dict(selection)
    order = [str(s) for s in (out.get("ordered_segment_ids") or []) if s]
    if not order:
        return out
    try:
        from interview_mux.edl_overlap_repair import consumed_segment_ids

        consumed = {str(s) for s in consumed_segment_ids(ctx) if s} & set(order)
    except Exception:
        return out
    if not consumed:
        return out
    kept = [s for s in order if s not in consumed]
    if not kept:
        return out
    out["ordered_segment_ids"] = kept
    excl = list(out.get("excluded_segment_ids") or [])
    have = {str(r.get("segment_id") if isinstance(r, dict) else r) for r in excl}
    rat = dict(out.get("exclude_rationales") or {})
    retired = [s for s in order if s in consumed]
    for sid in retired:
        if sid not in have:
            excl.append({"segment_id": sid, "reason": "edl_overlap_repair:absorbed_by_survivor"})
            have.add(sid)
        rat[sid] = str(rat.get(sid) or "edl_overlap_repair:absorbed_by_survivor")
    out["excluded_segment_ids"] = excl
    out["exclude_rationales"] = rat
    keep = set(kept)
    for ch in out.get("chapters") or []:
        if isinstance(ch, dict):
            ch["segment_ids"] = [
                str(x) for x in (ch.get("segment_ids") or []) if str(x) in keep
            ]
    out = reconcile_ordered_vs_excluded(out)
    try:
        from interview_mux.order_hash import bump_order_lock

        out = bump_order_lock(out, source="air_order_boundary.retire_consumed_under_freeze")
    except Exception:
        pass
    try:
        ctx.log(
            "seat_freeze: union-absorbed id(s) retired from the restored order: "
            + ", ".join(retired[:8]),
            level="info",
            stage="selection",
        )
    except Exception:
        pass
    return out


_SHIP_BLOCKING_OMIT_KINDS = (
    "on_a_roll",
    "incomplete_clause",
    "chapter_bleed_incomplete",
)


_INTEGRITY_OMIT_PRODUCERS = frozenset({"edl_overlap_repair", "segment_id_remap"})


def _ship_blocking_omit_ids(
    ctx: RunContext,
    *,
    prev_ids: list[str],
    cur_ids: list[str],
    selection: dict[str, Any],
    producer: str = "",
) -> list[str]:
    """Ids seat freeze must let leave air: junction incomplete-cut omit/fuse.

    Freeze protects creative order thrash. An omit-only delta (survivors keep
    their relative order) whose ids carry a junction incomplete-cut reason is a
    ship-blocking repair: if freeze restores them, the EDL drops the clip while
    selection keeps it, the divergence rebuild puts it back, and mix refuses
    forever on a residual nobody can clear (exec_11871 seg_014 / seg_071).
    """
    prev = [str(s) for s in (prev_ids or []) if s]
    cur = [str(s) for s in (cur_ids or []) if s]
    cur_set = set(cur)
    removed = [s for s in prev if s not in cur_set]
    if not removed:
        return []
    # Any true reordering (or additions) stays the freeze owner's call.
    if [s for s in prev if s in cur_set] != cur:
        return []
    # A fuse / overlap-union pass retires a consumed id into a survivor whose span
    # already covers the tape — nothing leaves air, so freeze has nothing to
    # protect. Restoring it strands selection at n+1 vs the EDL and mix refuses on
    # `speech/selection order` forever (exec_11871 seg_073→seg_071).
    if str(producer or "") in _INTEGRITY_OMIT_PRODUCERS:
        return removed
    reasons: dict[str, str] = {}
    for row in selection.get("excluded_segment_ids") or []:
        if isinstance(row, dict):
            reasons[str(row.get("segment_id") or "")] = str(row.get("reason") or "")
    rationales = selection.get("exclude_rationales")
    if isinstance(rationales, dict):
        for sid, why in rationales.items():
            reasons.setdefault(str(sid), str(why or ""))
    exempt = [
        sid
        for sid in removed
        if reasons.get(sid, "").startswith("junction_snip_qa:")
        and any(kind in reasons.get(sid, "") for kind in _SHIP_BLOCKING_OMIT_KINDS)
    ]
    if not exempt:
        return []
    try:
        from interview_mux.junction_snip_qa import (
            live_incomplete_cut_critical_findings,
        )

        if not live_incomplete_cut_critical_findings(ctx):
            return []
    except Exception:
        return []
    return exempt


def _preserve_frozen_selection_order(
    out: dict[str, Any],
    *,
    previous: dict[str, Any] | None,
    prev_ids: list[str],
    producer: str,
    stage_key: str,
    refuse_reason: str,
    ctx: RunContext,
) -> dict[str, Any]:
    """Keep frozen air order; stamp ok only when restored order is sanitary."""
    restored = dict(out)
    restored["ordered_segment_ids"] = list(prev_ids)
    if isinstance(previous, dict):
        for key in ("order_content_hash", "order_lock", "order_lock_source"):
            if key in previous:
                restored[key] = previous.get(key)
    # Restoring prior order can leave exclude_rationales pointing at air ids.
    try:
        from interview_mux.artifact_repairs import prune_stale_exclude_rationales

        restored, _ = prune_stale_exclude_rationales(restored)
        # Drop dual membership rows so lint/excluded stay coherent under freeze.
        order_set = {str(s) for s in (restored.get("ordered_segment_ids") or []) if s}
        if isinstance(restored.get("excluded_segment_ids"), list) and order_set:
            restored["excluded_segment_ids"] = [
                row
                for row in restored["excluded_segment_ids"]
                if str(
                    row.get("segment_id") if isinstance(row, dict) else row or ""
                )
                not in order_set
            ]
            restored, _ = prune_stale_exclude_rationales(restored)
    except Exception:
        pass
    sanitary_ok = True
    sanitary_reason = ""
    try:
        from interview_mux.artifact_sanitize.selection import sanitize_master_selection
        from interview_mux.artifact_sanitize.selection import (
            _lattice_and_integrity_errs,
            _shape_sanitary_errs,
        )

        dry = sanitize_master_selection(ctx, restored)
        shape_errs = _shape_sanitary_errs(
            ctx,
            [str(s) for s in (restored.get("ordered_segment_ids") or []) if s],
            restored,
        )
        lint_errs = _lattice_and_integrity_errs(ctx, restored)
        # Dry sanitize may mutate; judge the restored order itself.
        mutating = [
            a
            for a in (dry.actions or [])
            if str((a or {}).get("action") or "")
            not in {"bump_order_lock", "prune_stale_exclude_rationales", "reconcile_ordered_vs_excluded"}
        ]
        if shape_errs or lint_errs or (not dry.ok) or mutating:
            sanitary_ok = False
            bits = list(shape_errs or []) + list(lint_errs or [])
            if not dry.ok:
                bits.extend(list(dry.errors or [])[:2])
            if mutating:
                bits.append(
                    "selection_needs_sanitize:"
                    + ",".join(str(a.get("action") or "") for a in mutating[:4])
                )
            sanitary_reason = "; ".join(bits[:4]) or "freeze_restore_unsanitary"
    except Exception:
        sanitary_ok = False
        sanitary_reason = "freeze_restore_unsanitary"
    try:
        from interview_mux.artifact_sanitize.reentry import stamp_sanitize_meta

        restored = stamp_sanitize_meta(
            restored,
            ok=sanitary_ok,
            source=f"seat_freeze_preserve:{producer}",
            actions_n=0,
            extra={
                "preserved_order": True,
                "refused": str(refuse_reason or "")[:120],
                **(
                    {"freeze_restore_unsanitary": sanitary_reason[:160]}
                    if not sanitary_ok
                    else {}
                ),
            },
            content_keys=["ordered_segment_ids", "order_content_hash"],
        )
    except Exception:
        pass
    try:
        ctx.log(
            "seat_freeze: preserved selection order "
            f"(refused:{refuse_reason or 'meta_gate'}; producer={producer}"
            + (
                f"; unsanitary:{sanitary_reason[:80]}"
                if not sanitary_ok
                else ""
            )
            + ")",
            level="warning",
            stage=stage_key or producer,
        )
    except Exception:
        pass
    return restored


_ACCOUNTABLE_CO_WRITERS = frozenset(
    {
        "selection_order_sanitize",
        "air_script_compose",
        "nugget_layup_compose",
        "artifact_sanitize.selection",
    }
)


def _accountable_co_writer_shrink(
    ctx: RunContext,
    previous: dict[str, Any] | None,
    proposed: dict[str, Any],
    *,
    producer: str,
    stage_key: str,
) -> dict[str, Any]:
    """Restore unexplained membership shrinks for sanitize/Pass A co-writers.

    Drops covered by omit ledger / CTA / exclude_rationales are allowed; bare
    silent shrink (no reason) is refused by restoring the prior id into order.
    """
    if producer not in _ACCOUNTABLE_CO_WRITERS and stage_key not in _ACCOUNTABLE_CO_WRITERS:
        return proposed
    prev_ids = [str(s) for s in ((previous or {}).get("ordered_segment_ids") or []) if s]
    cur_ids = [str(s) for s in (proposed.get("ordered_segment_ids") or []) if s]
    if not prev_ids or not cur_ids:
        return proposed
    dropped = [s for s in prev_ids if s not in set(cur_ids)]
    if not dropped:
        return proposed
    explained: set[str] = set()
    try:
        from interview_mux.omit_ledger import OMIT_LEDGER_REL

        if ctx.artifact_exists(OMIT_LEDGER_REL):
            ledger = ctx.read_json(OMIT_LEDGER_REL)
            for row in (ledger or {}).get("omits") or []:
                if isinstance(row, dict) and row.get("segment_id"):
                    explained.add(str(row["segment_id"]))
            for sid in (ledger or {}).get("omitted_segment_ids") or []:
                if sid:
                    explained.add(str(sid))
    except Exception:
        pass
    try:
        from interview_mux.media_ip_cta import never_touch_segment_ids

        explained |= {str(s) for s in never_touch_segment_ids(ctx) if s}
    except Exception:
        pass
    for row in proposed.get("excluded_segment_ids") or []:
        sid = str(row.get("segment_id") if isinstance(row, dict) else row)
        if sid:
            explained.add(sid)
    rationales = (
        proposed.get("exclude_rationales")
        if isinstance(proposed.get("exclude_rationales"), dict)
        else {}
    )
    explained |= {str(k) for k in rationales if k}
    unexplained = [s for s in dropped if s not in explained]
    if not unexplained:
        return proposed
    # Restore unexplained drops into their prior relative positions (append after
    # surviving prefix for simplicity).
    out = dict(proposed)
    restored = list(cur_ids)
    have = set(restored)
    for sid in unexplained:
        if sid not in have:
            restored.append(sid)
            have.add(sid)
    out["ordered_segment_ids"] = restored
    try:
        from interview_mux.order_hash import bump_order_lock

        out = bump_order_lock(out, source=f"{producer}:restore_silent_shrink")
    except Exception:
        pass
    try:
        ctx.log(
            f"co_writer_fingerprint: restored {len(unexplained)} silently-shrunk id(s)",
            level="warning",
            stage=stage_key or producer,
            detail={"restored": unexplained[:12]},
        )
    except Exception:
        pass
    return out


def commit_selection_mutation(
    ctx: RunContext,
    selection: dict[str, Any],
    *,
    producer: str,
    stage_key: str,
    checkpoint_mode: CheckpointMode = "repair",
    merge_from_disk: bool = False,
    write_committed: bool = False,
    skip_checkpoint: bool = False,
    skip_handoff: bool = False,
    mutation_class: str | None = None,
) -> dict[str, Any]:
    """Single write path: checkpoint, optional block, persist, lifecycle on order change."""
    from interview_mux.artifact_sanitize.one_writer import (
        admitting,
        begin_admit,
        end_admit,
    )

    nested_admit = admitting(ctx)
    if not nested_admit:
        begin_admit(ctx)
    try:
        previous = _previous_selection(ctx)
        out = dict(selection)
        # Removal authority (ISSUES 74): a segment that must air cannot leave
        # the order, whoever the producer is. Judged here, once, on every
        # selection write.
        from interview_mux.removal_authority import refuse_selection_removals

        out = refuse_selection_removals(ctx, previous, out, producer=producer)
        # §4A: co-writer silent shrink outside omit ledger is refused (restore prior
        # membership for unexplained drops; omit-ledger / CTA drops stay).
        try:
            out = _accountable_co_writer_shrink(
                ctx, previous, out, producer=producer, stage_key=stage_key
            )
        except Exception:
            pass
        result = CheckpointResult()
        if not skip_checkpoint:
            out, result = checkpoint_air_order(
                ctx, out, producer=producer, mode=checkpoint_mode
            )
            if should_block_commit(result, producer=producer, mode=checkpoint_mode):
                msgs = [
                    str(v.get("message") or v.get("code") or "")
                    for v in result.violations
                    if str(v.get("severity") or "").lower() == "critical"
                ][:3]
                raise ValueError(
                    f"air_order_boundary blocked {producer}: " + "; ".join(msgs)
                )

        # Sanitize-last: non-amplifying sanitize after any repair/checkpoint, before disk.
        from interview_mux.artifact_sanitize.selection import sanitize_master_selection
        from interview_mux.media_ip_cta import normalize_media_ip_cta_rows

        # Dig #2 residue: coerce CTA rows to selection schema before sanitize/validate.
        out = normalize_media_ip_cta_rows(out)

        sanitize_result = sanitize_master_selection(ctx, out)
        if not sanitize_result.ok:
            raise RuntimeError(
                "sanitize_refused:selection: "
                + "; ".join((sanitize_result.errors or ["unknown"])[:4])
            )
        out = sanitize_result.doc if isinstance(sanitize_result.doc, dict) else out
        # Re-normalize after sanitize in case CTA rows were re-touched.
        out = normalize_media_ip_cta_rows(out)

        # b8: under seat freeze, order-changing selection commits need meta-gate
        # allow. On refuse: preserve frozen order (no-op) — same pattern as
        # framing/omit writers. Raising here spins full-auto forever because
        # heuristic opportunity for empty-ops order_change stays below threshold.
        try:
            from interview_mux.seat_authority import (
                end_a_action_for_ship_blocking_omit,
                hard_freeze_action_permitted,
                hard_freeze_active,
                request_seat_rewrite,
                soft_freeze_active,
            )

            if soft_freeze_active(ctx) or hard_freeze_active(ctx):
                prev_ids = [
                    str(s)
                    for s in ((previous or {}).get("ordered_segment_ids") or [])
                    if s
                ]
                cur_ids = [str(s) for s in (out.get("ordered_segment_ids") or []) if s]
                # DP-A2 Option A: ship-blocking omit/integrity is a *named* End-A
                # action (single constitution). Classifier names the delta; End-A
                # permit lands the omit. Otherwise meta-gate / one-shot (else restore).
                if prev_ids and prev_ids != cur_ids:
                    classified = _ship_blocking_omit_ids(
                        ctx,
                        prev_ids=prev_ids,
                        cur_ids=cur_ids,
                        selection=out,
                        producer=producer,
                    )
                    end_a_action = end_a_action_for_ship_blocking_omit(producer)
                    # Layup / media-IP CTA: omit-only shrink under soft freeze is
                    # End-A packaging (exec_13177) — _ship_blocking_omit_ids only
                    # names junction incomplete-cut, so packaging must self-qualify.
                    cur_set = set(cur_ids)
                    omit_only = [s for s in prev_ids if s in cur_set] == cur_ids and bool(
                        [s for s in prev_ids if s not in cur_set]
                    )
                    packaging_cta = False
                    if omit_only and end_a_action:
                        try:
                            from interview_mux.seat_authority import (
                                END_A_PACKAGING_ACTIONS,
                            )

                            packaging_cta = end_a_action in END_A_PACKAGING_ACTIONS
                        except Exception:
                            packaging_cta = False
                    enda_ids = list(classified) if classified else (
                        [s for s in prev_ids if s not in cur_set] if packaging_cta else []
                    )
                    if (
                        enda_ids
                        and end_a_action
                        and hard_freeze_action_permitted(end_a_action, ctx)
                    ):
                        ctx.log(
                            "seat_freeze: End-A omit permitted "
                            f"({end_a_action}; {', '.join(enda_ids[:6])}; "
                            f"producer={producer})",
                            level="info",
                            stage=stage_key or producer,
                        )
                    else:
                        if classified:
                            ctx.log(
                                "seat_freeze: ship-blocking omit not on End-A "
                                f"(action={end_a_action or 'unmapped'}; "
                                f"{', '.join(classified[:6])}; producer={producer}); "
                                "meta-gate / one-shot required",
                                level="warning",
                                stage=stage_key or producer,
                            )
                            try:
                                from interview_mux.seat_authority import (
                                    note_ship_omit_blocked,
                                )

                                note_ship_omit_blocked(
                                    ctx,
                                    producer=producer,
                                    action=end_a_action,
                                    ids=classified,
                                )
                            except Exception:
                                pass
                        dec = request_seat_rewrite(
                            ctx,
                            proposed_delta={
                                "ops": [],
                                "order_change": True,
                                "source": producer,
                                "stage_key": stage_key,
                                "ship_omit_ids": list(enda_ids or classified or [])[:12],
                                "end_a_action": end_a_action,
                            },
                            reason=(
                                f"selection_commit:{producer}"
                                if end_a_action
                                else f"selection_commit_unmapped_omit:{producer}"
                            ),
                            symptoms=["order_change", "selection_commit", "ship_omit"],
                        )
                        if not dec.get("allow"):
                            out = _preserve_frozen_selection_order(
                                out,
                                previous=previous if isinstance(previous, dict) else None,
                                prev_ids=prev_ids,
                                producer=producer,
                                stage_key=stage_key,
                                refuse_reason=str(
                                    dec.get("refuse_reason") or "meta_gate"
                                ),
                                ctx=ctx,
                            )
                            # Blank/unusable must still leave air under freeze —
                            # otherwise chapter continuity QC sees leftover blanks
                            # as interlopers (exec_11630 seg_041).
                            out = _drop_blank_segments_under_freeze(ctx, out)
                        out = _retire_consumed_under_freeze(ctx, out)
        except Exception:
            try:
                from interview_mux.seat_authority import (
                    hard_freeze_active,
                    soft_freeze_active,
                )

                if soft_freeze_active(ctx) or hard_freeze_active(ctx):
                    prev_ids = [
                        str(s)
                        for s in ((previous or {}).get("ordered_segment_ids") or [])
                        if s
                    ]
                    cur_ids = [
                        str(s) for s in (out.get("ordered_segment_ids") or []) if s
                    ]
                    if prev_ids and prev_ids != cur_ids:
                        out = _preserve_frozen_selection_order(
                            out,
                            previous=previous if isinstance(previous, dict) else None,
                            prev_ids=prev_ids,
                            producer=producer,
                            stage_key=stage_key,
                            refuse_reason="fail_closed",
                            ctx=ctx,
                        )
                        out = _drop_blank_segments_under_freeze(ctx, out)
                        out = _retire_consumed_under_freeze(ctx, out)
            except Exception:
                pass

        try:
            from interview_mux.artifact_sanitize.reentry import sanitary_content_hash
            from interview_mux.thrash_hardening import note_authority_undo_attempt

            # S4: meta-only / order-unchanged writes must not enter the undo
            # ledger (sanitize ↔ narrative metadata_align hash oscillation).
            prev_ids_undo = [
                str(s)
                for s in ((previous or {}).get("ordered_segment_ids") or [])
                if s
            ]
            cur_ids_undo = [
                str(s) for s in (out.get("ordered_segment_ids") or []) if s
            ]
            if (not prev_ids_undo) or prev_ids_undo != cur_ids_undo:
                undo = note_authority_undo_attempt(
                    ctx,
                    artifact="master/selection.json",
                    action_class=str(producer or stage_key or "selection_commit"),
                    content_hash=sanitary_content_hash(
                        out, keys=["ordered_segment_ids", "order_content_hash"]
                    ),
                )
                if undo.get("halt"):
                    raise RuntimeError(
                        "authority_undo_thrash:master/selection.json: "
                        + str(undo.get("reason") or "oscillation")
                    )
        except RuntimeError:
            raise
        except Exception:
            pass

        mut = mutation_class
        write_sk = stage_key
        try:
            from interview_mux.artifact_ownership import SEGMENT_ID_REMAP_STAGES

            if producer in SEGMENT_ID_REMAP_STAGES:
                write_sk = producer
                mut = mut or "segment_id_remap"
            elif not mut and stage_key in SEGMENT_ID_REMAP_STAGES:
                mut = "segment_id_remap"
        except Exception:
            pass

        # Land Honesty (SHARED_PATH_PRODUCER_STAGES): selection writes go through
        # write_mirrored_json while admitting (no fingerprint_artifact), so the
        # doc must carry _meta.producer_stage or selection_order_sanitize stays
        # unpaid → incomplete-after-conductor thrash (exec_13196).
        # S4: when ordered_segment_ids are unchanged, preserve prior producer_stage
        # so metadata-align does not flip ownership vs sanitize.
        # Sanitize itself must restamp even on a no-op — ranking's producer_stage
        # is intentionally unpaid for sanitize (thin matrix) until this stage pays.
        sk_stamp = str(write_sk or stage_key or "").strip()
        if sk_stamp:
            meta = (
                dict(out.get("_meta") or {})
                if isinstance(out.get("_meta"), dict)
                else {}
            )
            prev_ids_stamp = [
                str(s)
                for s in ((previous or {}).get("ordered_segment_ids") or [])
                if s
            ]
            cur_ids_stamp = [
                str(s) for s in (out.get("ordered_segment_ids") or []) if s
            ]
            prev_meta = (
                (previous or {}).get("_meta")
                if isinstance(previous, dict)
                else None
            )
            prev_producer = ""
            if isinstance(prev_meta, dict):
                prev_producer = str(prev_meta.get("producer_stage") or "").strip()
            # An id fold (fuse, hitch, overlap) removes absorbed ids and
            # leaves the surviving order. It must not replace a paid sanitize
            # stamp with the mover, or sanitize is re-dispatched until the cap.
            id_fold = (mut or "") == "segment_id_remap"
            if sk_stamp == "selection_order_sanitize":
                meta["producer_stage"] = sk_stamp
            elif id_fold and prev_producer:
                meta["producer_stage"] = prev_producer
            elif id_fold and not prev_producer:
                # A missing stamp after sanitize has already paid must not be
                # replaced by the fuse/hitch stage. That stage is not a
                # co-producer, so sanitize would be dispatched until the cap.
                sanitize_paid = False
                try:
                    sanitize_paid = bool(ctx.is_done("selection_order_sanitize"))
                except Exception:
                    sanitize_paid = False
                meta["producer_stage"] = (
                    "selection_order_sanitize" if sanitize_paid else sk_stamp
                )
            elif (
                prev_ids_stamp
                and prev_ids_stamp == cur_ids_stamp
                and prev_producer
            ):
                meta["producer_stage"] = prev_producer
            else:
                meta["producer_stage"] = sk_stamp
            out["_meta"] = meta

        if write_committed:
            from interview_mux.artifact_ownership import AuthorityDenied
            from interview_mux.write_staging import write_committed_json

            try:
                write_committed_json(
                    ctx,
                    "master/selection.json",
                    out,
                    stage_key=write_sk,
                    mutation_class=mut,
                )
            except AuthorityDenied as exc:
                # A′′ Global Freeze: after meta-gate refuse we restored frozen
                # order — retry as epoch owner, or no-op when disk already matches.
                owner = str(getattr(exc, "suggested_owner", "") or "").strip()
                prev_o = [
                    str(s)
                    for s in ((previous or {}).get("ordered_segment_ids") or [])
                    if s
                ]
                cur_o = [str(s) for s in (out.get("ordered_segment_ids") or []) if s]
                if owner and owner != write_sk:
                    write_committed_json(
                        ctx,
                        "master/selection.json",
                        out,
                        stage_key=owner,
                        mutation_class=mut,
                    )
                elif prev_o and prev_o == cur_o:
                    try:
                        ctx.log(
                            "seat_freeze: skip selection rewrite "
                            f"(order unchanged; denied {write_sk})",
                            level="info",
                            stage=stage_key or producer,
                        )
                    except Exception:
                        pass
                else:
                    raise
        elif skip_handoff:
            ctx.write_json(
                "master/selection.json",
                out,
                stage_key=write_sk,
                skip_handoff=True,
                mutation_class=mut,
            )
        else:
            # Prefer write_committed so write_validated cannot re-amplify after sanitize.
            from interview_mux.write_staging import write_committed_json

            write_committed_json(
                ctx,
                "master/selection.json",
                out,
                stage_key=write_sk,
                mutation_class=mut,
            )

        from interview_mux.air_order_integrity import on_selection_order_changed

        on_selection_order_changed(
            ctx,
            source=producer,
            previous=previous,
            current=out,
            id_fold=(mut or "") == "segment_id_remap",
        )
        # The documents derived from the selection follow it here, under their
        # owners' keys, whoever moved it (ISSUES 113). A removal the
        # constitution permitted must not leave the lay-up plan one revision
        # behind or a cue anchored on a retired id.
        try:
            from interview_mux.selection_dependents import reconcile_selection_dependents

            reconcile_selection_dependents(
                ctx, producer=producer, previous=previous, current=out
            )
        except Exception as exc:  # noqa: BLE001 - paperwork never fails the commit
            try:
                ctx.log(
                    f"selection dependents reconcile error ({producer}): "
                    f"{type(exc).__name__}: {str(exc)[:160]}",
                    level="warning",
                    stage=str(producer or "").split(":")[0] or None,
                )
            except Exception:
                pass
        return out
    finally:
        if not nested_admit:
            end_admit(ctx)


SELECTION_COMMIT_REFUSED_REL = "operator/selection_commit_refused.json"


def stamp_selection_commit_refused(
    ctx: RunContext, *, stage_key: str, error: str
) -> None:
    """HR-2: sidecar so seed-complete cannot mark the writer after a bus refuse."""
    try:
        ctx.write_json(
            SELECTION_COMMIT_REFUSED_REL,
            {
                "version": 1,
                "active": True,
                "stage": str(stage_key or ""),
                "error": str(error or "")[:240],
            },
            skip_handoff=True,
        )
    except Exception:
        pass


def clear_selection_commit_refused(ctx: RunContext, *, stage_key: str) -> None:
    if not ctx.artifact_exists(SELECTION_COMMIT_REFUSED_REL):
        return
    try:
        doc = ctx.read_json(SELECTION_COMMIT_REFUSED_REL)
    except Exception:
        return
    if not isinstance(doc, dict):
        return
    if str(doc.get("stage") or "") != str(stage_key or ""):
        return
    if not doc.get("active"):
        return
    out = dict(doc)
    out["active"] = False
    try:
        ctx.write_json(SELECTION_COMMIT_REFUSED_REL, out, skip_handoff=True)
    except Exception:
        pass


def selection_commit_refused_reason(ctx: RunContext, stage_id: str) -> str | None:
    """Incompleteness prose when this writer last failed an on-bus selection commit."""
    sid = str(stage_id or "").strip()
    if not sid or not ctx.artifact_exists(SELECTION_COMMIT_REFUSED_REL):
        return None
    try:
        doc = ctx.read_json(SELECTION_COMMIT_REFUSED_REL)
    except Exception:
        return None
    if not isinstance(doc, dict) or not doc.get("active"):
        return None
    if str(doc.get("stage") or "") != sid:
        return None
    err = str(doc.get("error") or "commit_refused")
    return f"selection_commit_refused — resume {sid}: {err}"


def commit_selection_or_refuse(
    ctx: RunContext,
    selection: dict[str, Any],
    *,
    producer: str,
    stage_key: str,
    checkpoint_mode: CheckpointMode = "repair",
    merge_from_disk: bool = False,
    write_committed: bool = False,
    skip_checkpoint: bool = False,
    skip_handoff: bool = False,
) -> dict[str, Any]:
    """HR-2: on-bus persist or raise a pin-able refuse (never off-bus write_json)."""
    try:
        out = commit_selection_mutation(
            ctx,
            selection,
            producer=producer,
            stage_key=stage_key,
            checkpoint_mode=checkpoint_mode,
            merge_from_disk=merge_from_disk,
            write_committed=write_committed,
            skip_checkpoint=skip_checkpoint,
            skip_handoff=skip_handoff,
        )
    except Exception as exc:
        stamp_selection_commit_refused(ctx, stage_key=stage_key, error=str(exc))
        raise RuntimeError(
            f"selection_commit_refused — resume {stage_key}: {exc}"
        ) from exc
    clear_selection_commit_refused(ctx, stage_key=stage_key)
    return out


def commit_selection_via(
    ctx: RunContext,
    selection: dict[str, Any],
    *,
    producer: str,
    stage_key: str,
    checkpoint_mode: CheckpointMode,
    writer: Callable[..., Any],
    writer_kwargs: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Checkpoint + lifecycle wrapper for callers that need a custom writer."""
    previous = _previous_selection(ctx)
    out, result = checkpoint_air_order(
        ctx, dict(selection), producer=producer, mode=checkpoint_mode
    )
    if should_block_commit(result, producer=producer, mode=checkpoint_mode):
        raise ValueError(f"air_order_boundary blocked {producer}")
    kwargs = dict(writer_kwargs or {})
    writer(ctx, "master/selection.json", out, **kwargs)
    from interview_mux.air_order_integrity import on_selection_order_changed

    on_selection_order_changed(
        ctx,
        source=producer,
        previous=previous,
        current=out,
        id_fold=str((writer_kwargs or {}).get("mutation_class") or "")
        == "segment_id_remap",
    )
    return out

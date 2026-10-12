"""Authoritative omit / suppress / defer / waive ledger for layup + G1 decisions.

Append/supersede only — never mutate prior active entries in place. Writers mint
new rows via ``mint_entry`` / ``supersede_entry`` and rebuild summaries with
``build_omit_ledger``.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from interview_mux.run_context import RunContext

OMIT_LEDGER_REL = "understanding/omit_ledger.json"

DECISION_KINDS = frozenset(
    {
        "segment_exclude",
        "layup_skip",
        "nugget_waive",
        "gap_line_skip",
        "suppress",
        "defer",
        "structural_omit",
        "asset_adjust",
    }
)
DECISIONS = frozenset({"omit", "defer", "suppress", "waive"})


def empty_omit_ledger() -> dict[str, Any]:
    return {
        "version": 1,
        "order_content_hash": None,
        "order_lock": None,
        "entries": [],
        "summary": {
            "active_count": 0,
            "by_kind": {},
            "unresolved_high_salience": 0,
            "compensated_count": 0,
        },
    }


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _entry_id(kind: str, subject_id: str, seq: int) -> str:
    safe_kind = "".join(ch if ch.isalnum() or ch in "_-" else "_" for ch in kind)
    safe_subj = "".join(ch if ch.isalnum() or ch in "_-" else "_" for ch in subject_id)
    return f"omit_{safe_kind}_{safe_subj}_{seq}"


def summarize_entries(entries: list[dict[str, Any]]) -> dict[str, Any]:
    by_kind: dict[str, int] = {}
    active = 0
    compensated = 0
    unresolved_high = 0
    for row in entries:
        if not isinstance(row, dict) or not row.get("active"):
            continue
        active += 1
        kind = str(row.get("kind") or "unknown")
        by_kind[kind] = by_kind.get(kind, 0) + 1
        path = str(row.get("compensating_path") or "").strip()
        if path:
            compensated += 1
        forgone = [str(x) for x in (row.get("value_forgone") or []) if x]
        if forgone and not path:
            unresolved_high += len(forgone)
    return {
        "active_count": active,
        "by_kind": by_kind,
        "unresolved_high_salience": unresolved_high,
        "compensated_count": compensated,
    }


def mint_entry(
    *,
    kind: str,
    subject_id: str,
    decision: str,
    reason_code: str,
    owner_stage: str,
    target_segment_id: str | None = None,
    rationale: str | None = None,
    evidence_refs: list[str] | None = None,
    value_forgone: list[str] | None = None,
    compensating_path: str | None = None,
    revisit_if: list[str] | None = None,
    decision_confidence: float | None = None,
    source_artifact: str | None = None,
    replacement_ref: str | None = None,
    operator_override: bool = False,
    provenance: dict[str, Any] | None = None,
    seq: int = 1,
) -> dict[str, Any]:
    if kind not in DECISION_KINDS:
        kind = "layup_skip"
    if decision not in DECISIONS:
        decision = "omit"
    return {
        "entry_id": _entry_id(kind, subject_id, seq),
        "kind": kind,
        "subject_id": str(subject_id),
        "target_segment_id": target_segment_id,
        "decision": decision,
        "reason_code": str(reason_code or "unspecified"),
        "rationale": rationale,
        "evidence_refs": list(evidence_refs or []),
        "value_forgone": list(value_forgone or []),
        "compensating_path": compensating_path,
        "revisit_if": list(revisit_if or []),
        "decision_confidence": decision_confidence,
        "owner_stage": str(owner_stage or "unknown"),
        "source_artifact": source_artifact,
        "replacement_ref": replacement_ref,
        "active": True,
        "superseded_by": None,
        "operator_override": bool(operator_override),
        "decided_at": _now_iso(),
        "provenance": provenance or {},
    }


def supersede_entry(
    ledger: dict[str, Any],
    *,
    subject_id: str,
    kind: str | None = None,
    replacement: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Mark matching active entries inactive; optionally append ``replacement``."""
    out = dict(ledger) if isinstance(ledger, dict) else empty_omit_ledger()
    entries = [dict(e) for e in (out.get("entries") or []) if isinstance(e, dict)]
    replacement_id = str((replacement or {}).get("entry_id") or "") or None
    for row in entries:
        if not row.get("active"):
            continue
        if str(row.get("subject_id") or "") != str(subject_id):
            continue
        if kind and str(row.get("kind") or "") != kind:
            continue
        row["active"] = False
        if replacement_id:
            row["superseded_by"] = replacement_id
    if replacement and isinstance(replacement, dict):
        entries.append(dict(replacement))
    out["entries"] = entries
    out["summary"] = summarize_entries(entries)
    return out


def active_entries(
    ledger: dict[str, Any] | None,
    *,
    kind: str | None = None,
    subject_id: str | None = None,
    target_segment_id: str | None = None,
) -> list[dict[str, Any]]:
    if not isinstance(ledger, dict):
        return []
    out: list[dict[str, Any]] = []
    for row in ledger.get("entries") or []:
        if not isinstance(row, dict) or not row.get("active"):
            continue
        if kind and str(row.get("kind") or "") != kind:
            continue
        if subject_id and str(row.get("subject_id") or "") != subject_id:
            continue
        if target_segment_id and str(row.get("target_segment_id") or "") != target_segment_id:
            continue
        out.append(row)
    return out


def active_omit_for_target(
    ledger: dict[str, Any] | None, target_segment_id: str
) -> dict[str, Any] | None:
    rows = active_entries(
        ledger, kind="layup_skip", target_segment_id=str(target_segment_id)
    )
    if rows:
        return rows[-1]
    rows = active_entries(ledger, target_segment_id=str(target_segment_id))
    return rows[-1] if rows else None


def line_is_omitted(ledger: dict[str, Any] | None, line_id: str) -> bool:
    return bool(
        active_entries(ledger, kind="gap_line_skip", subject_id=str(line_id))
        or active_entries(ledger, kind="layup_skip", subject_id=str(line_id))
    )


def effective_air_contract(
    ledger: dict[str, Any] | None,
    *,
    line_id: str | None = None,
    target_segment_id: str | None = None,
) -> dict[str, Any]:
    """Return how delivery should treat a VO line / target.

    Outcomes:
    - ``required`` — no active omit; synthesize / keep
    - ``omitted`` — do not introduce VO
    - ``suppressed`` — replacement must be audible instead
    - ``deferred`` — G1-optional only
    """
    if line_id:
        for row in active_entries(ledger, subject_id=str(line_id)):
            decision = str(row.get("decision") or "omit")
            if decision == "suppress":
                return {
                    "status": "suppressed",
                    "entry": row,
                    "replacement_ref": row.get("replacement_ref"),
                }
            if decision == "defer":
                return {"status": "deferred", "entry": row}
            return {"status": "omitted", "entry": row}
    if target_segment_id:
        row = active_omit_for_target(ledger, target_segment_id)
        if row:
            decision = str(row.get("decision") or "omit")
            if decision == "suppress":
                return {
                    "status": "suppressed",
                    "entry": row,
                    "replacement_ref": row.get("replacement_ref"),
                }
            if decision == "defer":
                return {"status": "deferred", "entry": row}
            return {"status": "omitted", "entry": row}
    return {"status": "required", "entry": None}


def _wav_backed_vo_line_ids(ctx: RunContext) -> set[str]:
    """Line ids with an on-disk (or vo_synthesize pending) pickup stem."""
    found: set[str] = set()
    roots: list[Any] = []
    try:
        roots.append(ctx.final_path("vo_pickup"))
    except Exception:
        pass
    try:
        from interview_mux.write_staging import staging_root

        roots.append(staging_root(ctx, "vo_synthesize") / "vo_pickup")
    except Exception:
        pass
    for pickup in roots:
        if pickup is None:
            continue
        try:
            if not pickup.exists():
                continue
        except Exception:
            continue
        for base in (
            pickup / "matched",
            pickup / "synthesized",
            pickup / "clean",
            pickup / "normalized",
            pickup,
        ):
            try:
                if not base.is_dir():
                    continue
                for wav in base.glob("*.wav"):
                    stem = str(wav.stem or "").strip()
                    if stem:
                        found.add(stem)
            except Exception:
                continue
    return found


def reconcile_edl_with_omit_ledger(
    ctx: RunContext,
    *,
    edl: dict[str, Any] | None = None,
    ledger: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Drop audible VO clips that the omit ledger actively omits; retime EDL.

    Stale EDL can retain synthesized layup VO after a later typed skip stamps
    ``omit``. Post-master QC then fails ``omit_ledger_air_contract`` and
    ``audible_script_hash_agreement`` (missing gap script). Stripping keeps
    assembly authority aligned with the ledger without shipping canned air.
    """
    if ledger is None:
        ledger = (
            ctx.read_json(OMIT_LEDGER_REL)
            if ctx.artifact_exists(OMIT_LEDGER_REL)
            else None
        )
    if not isinstance(ledger, dict):
        return {"removed": [], "updated": False}
    if edl is None:
        if not ctx.artifact_exists("master/edl.json"):
            return {"removed": [], "updated": False}
        loaded = ctx.read_json("master/edl.json")
        edl = loaded if isinstance(loaded, dict) else None
    if not isinstance(edl, dict):
        return {"removed": [], "updated": False}

    omitted_line_ids: set[str] = set()
    omitted_targets: set[str] = set()
    protected_line_ids: set[str] = set()
    gap_report: dict[str, Any] | None = None
    if ctx.artifact_exists("understanding/gap_report.json"):
        try:
            loaded_gap = ctx.read_json("understanding/gap_report.json")
            gap_report = loaded_gap if isinstance(loaded_gap, dict) else None
        except Exception:
            gap_report = None
    if isinstance(gap_report, dict):
        try:
            from interview_mux.opening_orientation import is_episode_orientation

            orient_meta = gap_report.get("opening_orientation")
            if isinstance(orient_meta, dict) and orient_meta.get("required"):
                oid = str(orient_meta.get("line_id") or "").strip()
                if oid:
                    protected_line_ids.add(oid)
            for line in gap_report.get("interviewer_lines") or []:
                if not isinstance(line, dict) or line.get("skipped_optional"):
                    continue
                if not (is_episode_orientation(line) or bool(line.get("required"))):
                    continue
                lid = str(line.get("line_id") or "").strip()
                if lid:
                    protected_line_ids.add(lid)
        except Exception:
            pass
    # Seated air-script VO must never be purged — omit-ledger rows can lag a
    # later reseat (exec_13177: purged vo_layup_seg_003c right after synth).
    protect_load_failed = False
    try:
        from interview_mux.air_script import seated_vo_line_ids
        from interview_mux.mastering_plan_loader import load_plan_raw

        protected_line_ids |= {
            str(x) for x in seated_vo_line_ids(load_plan_raw(ctx)) if x
        }
    except Exception as exc:
        protect_load_failed = True
        try:
            ctx.log(
                f"omit_ledger: seated protect load failed — skip purge ({exc})",
                level="warning",
                stage="omit_ledger",
            )
        except Exception:
            pass
    # Seat-lag harden: also protect any line that already has a pickup WAV
    # even when mastering_plan seats have not been rewritten yet.
    try:
        protected_line_ids |= _wav_backed_vo_line_ids(ctx)
    except Exception as exc:
        protect_load_failed = True
        try:
            ctx.log(
                f"omit_ledger: wav protect load failed — skip purge ({exc})",
                level="warning",
                stage="omit_ledger",
            )
        except Exception:
            pass
    if protect_load_failed:
        return {"removed": [], "updated": False, "protect_load_failed": True}
    # Drop stale active omit rows for seated lines so line_is_omitted agrees.
    if protected_line_ids and isinstance(ledger, dict):
        try:
            mutated = ledger
            for lid in sorted(protected_line_ids):
                for row in list(active_entries(mutated, subject_id=lid)):
                    if str(row.get("decision") or "omit") != "omit":
                        continue
                    mutated = supersede_entry(
                        mutated,
                        subject_id=lid,
                        kind=str(row.get("kind") or "") or None,
                    )
            if mutated is not ledger:
                write_omit_ledger(ctx, mutated)
                ledger = mutated
        except Exception:
            pass
    for entry in active_entries(ledger):
        if str(entry.get("decision") or "omit") != "omit":
            continue
        subject = str(entry.get("subject_id") or "").strip()
        if subject:
            omitted_line_ids.add(subject)
        tid = str(entry.get("target_segment_id") or "").strip()
        if tid and str(entry.get("kind") or "") == "layup_skip":
            omitted_targets.add(tid)
            omitted_line_ids.add(f"vo_layup_{tid}")

    clips_in = [c for c in (edl.get("clips") or []) if isinstance(c, dict)]
    kept: list[dict[str, Any]] = []
    removed: list[str] = []
    for clip in clips_in:
        ctype = str(clip.get("type") or "")
        if ctype != "vo_pickup":
            kept.append(clip)
            continue
        lid = str(clip.get("line_id") or clip.get("vo_line_id") or "").strip()
        tid = str(clip.get("targets_segment_id") or "").strip()
        if lid and lid in protected_line_ids:
            kept.append(clip)
            continue
        if (lid and lid in omitted_line_ids) or (tid and tid in omitted_targets):
            removed.append(lid or tid or "vo_pickup")
            continue
        kept.append(clip)

    if not removed:
        return {"removed": [], "updated": False}

    # Purge omitted VO audio/audit so later reseat cannot revive stale takes.
    try:
        from interview_mux.vo_synthesis_audit import (
            invalidate_synthesis_entries,
        )

        purge_ids = [str(x) for x in removed if str(x).strip()]
        for lid in purge_ids:
            pickup = ctx.final_path("vo_pickup")
            for sub in ("matched", "synthesized", "clean", "normalized", ""):
                base = pickup / sub if sub else pickup
                candidate = base / f"{lid}.wav"
                if candidate.is_file():
                    try:
                        candidate.unlink()
                    except OSError:
                        pass
        if purge_ids:
            invalidate_synthesis_entries(ctx, purge_ids)
            ctx.log(
                f"omit_ledger: purged {len(purge_ids)} omitted VO take(s)",
                level="info",
                stage="omit_ledger",
                detail={"line_ids": purge_ids[:24]},
            )
    except Exception:
        pass

    removed_set = set(removed)
    gap_placements = [
        row
        for row in (edl.get("gap_placements") or [])
        if isinstance(row, dict)
        and str(row.get("line_id") or "") not in removed_set
    ]

    from interview_mux.junction_snip_qa import _recompute_timeline

    timeline_ms = _recompute_timeline(kept)
    out = dict(edl)
    out["clips"] = kept
    out["gap_placements"] = gap_placements
    out["timeline_duration_ms"] = timeline_ms
    out["vo_pickup_clip_count"] = sum(1 for c in kept if c.get("type") == "vo_pickup")
    out["transition_clip_count"] = sum(1 for c in kept if c.get("type") == "transition")
    out["silence_clip_count"] = sum(1 for c in kept if c.get("type") == "silence")
    from interview_mux.write_staging import write_committed_json

    from interview_mux.air_order import write_live_edl

    write_live_edl(ctx, out, source="omit_ledger")
    try:
        from interview_mux.assembly_ledger import write_assembly_ledger

        write_assembly_ledger(ctx, edl=out)
    except Exception:
        pass
    try:
        from interview_mux.vo_contract import clamp_hosted_seats_to_rendered_wavs

        clamp_hosted_seats_to_rendered_wavs(ctx)
    except Exception:
        pass
    ctx.log(
        f"omit_ledger: stripped {len(removed)} omitted VO clip(s) from EDL",
        level="info",
        stage="edl",
        detail={"removed": removed[:12]},
    )
    return {"removed": removed, "updated": True, "timeline_duration_ms": timeline_ms}


def air_contract_errors(
    ctx: RunContext,
    *,
    ledger: dict[str, Any] | None = None,
    gap_report: dict[str, Any] | None = None,
    edl: dict[str, Any] | None = None,
) -> list[str]:
    """Return ship-blocking violations of active omit decisions.

    This intentionally tolerates runs created before the ledger existed. Once
    present, the ledger must agree with selection authority and with the
    listener-facing gap report / EDL it governs.
    """
    if ledger is None:
        ledger = (
            ctx.read_json(OMIT_LEDGER_REL)
            if ctx.artifact_exists(OMIT_LEDGER_REL)
            else None
        )
    if not isinstance(ledger, dict):
        return []

    errors: list[str] = []
    try:
        from interview_mux.order_hash import get_order_lock, order_locks_same_content

        selection = (
            ctx.read_json("master/selection.json")
            if ctx.artifact_exists("master/selection.json")
            else {}
        )
        selection_lock = get_order_lock(selection if isinstance(selection, dict) else None)
        ledger_lock = get_order_lock(ledger)
        # Same air order under a different revision is paperwork, not a stale
        # ledger (ISSUES 190, the layup plan's twin).
        if selection_lock and ledger_lock and not order_locks_same_content(selection, ledger):
            errors.append("omit_ledger_order_lock_stale")
    except Exception:
        errors.append("omit_ledger_order_lock_unreadable")

    summary = ledger.get("summary")
    if isinstance(summary, dict) and int(summary.get("unresolved_high_salience") or 0) > 0:
        errors.append(
            "omit_ledger_unresolved_high_salience="
            + str(summary.get("unresolved_high_salience"))
        )

    if gap_report is None:
        gap_report = (
            ctx.read_json("understanding/gap_report.json")
            if ctx.artifact_exists("understanding/gap_report.json")
            else {}
        )
    lines = [
        row
        for row in ((gap_report or {}).get("interviewer_lines") or [])
        if isinstance(row, dict)
    ]
    by_line_id = {str(row.get("line_id") or ""): row for row in lines if row.get("line_id")}

    if edl is None:
        edl = ctx.read_json("master/edl.json") if ctx.artifact_exists("master/edl.json") else {}
    edl_refs = {
        str(clip.get(key) or "")
        for clip in ((edl or {}).get("clips") or [])
        if isinstance(clip, dict)
        for key in ("line_id", "vo_line_id", "source_line_id")
        if clip.get(key)
    }

    for entry in active_entries(ledger):
        subject_id = str(entry.get("subject_id") or "")
        decision = str(entry.get("decision") or "omit")
        kind = str(entry.get("kind") or "")
        if kind == "gap_line_skip":
            line = by_line_id.get(subject_id)
            if line and not line.get("skipped_optional"):
                errors.append(f"omit_ledger_gap_line_not_skipped:{subject_id}")
            if subject_id in edl_refs:
                errors.append(f"omit_ledger_gap_line_still_in_edl:{subject_id}")
        elif decision == "omit" and subject_id in edl_refs:
            errors.append(f"omit_ledger_omitted_line_still_in_edl:{subject_id}")
        elif decision == "suppress":
            replacement = str(entry.get("replacement_ref") or "")
            if not replacement:
                errors.append(f"omit_ledger_suppress_missing_replacement:{subject_id}")
            elif replacement == "episode_orientation":
                from interview_mux.opening_orientation import (
                    is_episode_orientation,
                    native_open_already_orients,
                    orientation_omitted,
                )

                has_orientation_line = any(is_episode_orientation(row) for row in lines)
                # Native-open already intros: ensure_episode_orientation omits the
                # synthetic preface. That *is* the compensating air — do not fail
                # QC because the ledger still says suppress→episode_orientation.
                native_compensates = orientation_omitted(gap_report)
                compensating = str(entry.get("compensating_path") or "")
                opening_owned = False
                if compensating in {"opening_orientation", "native_self_orients"}:
                    try:
                        from interview_mux.nugget_layup import _opening_owned_targets

                        tid = str(entry.get("target_segment_id") or "").strip()
                        opening_owned = bool(tid and tid in _opening_owned_targets(ctx))
                    except Exception:
                        opening_owned = False
                native_open = False
                try:
                    ordered = []
                    if ctx.artifact_exists("master/selection.json"):
                        sel = ctx.read_json("master/selection.json")
                        if isinstance(sel, dict):
                            ordered = [
                                str(x)
                                for x in (sel.get("ordered_segment_ids") or [])
                                if x
                            ]
                    tid = str(entry.get("target_segment_id") or "").strip()
                    native_open = native_open_already_orients(
                        ctx, ordered, target_segment_id=tid or None
                    )
                except Exception:
                    native_open = False
                if not has_orientation_line and not native_compensates:
                    # Native self-orients / opening-owned suppress is satisfied
                    # without a synthetic orientation line (gap framing on or off).
                    if compensating in {
                        "opening_orientation",
                        "native_self_orients",
                    } and (opening_owned or native_open):
                        continue
                    if native_open:
                        continue
                    errors.append(
                        f"omit_ledger_orientation_replacement_missing:{subject_id}"
                    )
    return list(dict.fromkeys(errors))


def stamp_gap_report_omit_skips(ctx: RunContext) -> int:
    """Mark gap-report lines skipped when the omit ledger already decided omit/defer."""
    if not ctx.artifact_exists("understanding/gap_report.json"):
        return 0
    try:
        from interview_mux.seat_authority import gate_seat_mutation, soft_freeze_active

        # End-A reason must match HARD_FREEZE_ALLOWLIST (`stamp_gap_omit_flags`).
        # A near-miss token here (omit_ledger_stamp_gap_skips) was soft/hard blocked
        # by rewrite cap while G1 still demanded WAV for ledger-omitted lines
        # (exec_13177 vo_g1 thrash: 003b/003c/010/018).
        if soft_freeze_active(ctx) and not gate_seat_mutation(
            ctx,
            reason="stamp_gap_omit_flags",
            symptoms=["omit_ledger"],
        ):
            return 0
    except Exception:
        try:
            from interview_mux.seat_authority import soft_freeze_active, hard_freeze_active

            if soft_freeze_active(ctx) or hard_freeze_active(ctx):
                return 0
        except Exception:
            return 0
    ledger = (
        ctx.read_json(OMIT_LEDGER_REL) if ctx.artifact_exists(OMIT_LEDGER_REL) else None
    )
    report = ctx.read_json("understanding/gap_report.json")
    if not isinstance(report, dict) or not isinstance(ledger, dict):
        return 0
    lines = [row for row in (report.get("interviewer_lines") or []) if isinstance(row, dict)]
    by_id = {str(row.get("line_id") or ""): row for row in lines if row.get("line_id")}
    candidates: list[dict[str, Any]] = []
    seen: set[int] = set()
    for entry in active_entries(ledger):
        subject = str(entry.get("subject_id") or "").strip()
        kind = str(entry.get("kind") or "")
        if kind not in {"gap_line_skip", "layup_skip"}:
            continue
        line = by_id.get(subject)
        if line is None and kind == "layup_skip":
            tid = str(entry.get("target_segment_id") or "").strip()
            if tid:
                line = by_id.get(f"vo_layup_{tid}")
        if not isinstance(line, dict) or line.get("skipped_optional"):
            continue
        marker = id(line)
        if marker in seen:
            continue
        seen.add(marker)
        candidates.append(line)
        if not line.get("skip_reason_code"):
            line["_pending_omit_reason"] = str(entry.get("reason_code") or "omit_ledger")
    if not candidates:
        return 0
    require_floor = False
    need = 0
    try:
        from interview_mux.gap_fill_eligibility import (
            hosted_framing_requires_synthetic_vo,
            min_synthetic_vo_lines,
        )

        require_floor = hosted_framing_requires_synthetic_vo(ctx)
        need = min_synthetic_vo_lines(ctx)
    except Exception:
        require_floor = False
    # Cluster C: never stamp-omit orientation under HEARD_KEEP / HOLLOW_MINT.
    try:
        from interview_mux.hosted_vo_authority import decide_orientation
        from interview_mux.opening_orientation import is_episode_orientation

        ordered: list[str] = []
        if ctx.artifact_exists("master/selection.json"):
            sel = ctx.read_json("master/selection.json")
            ordered = [
                str(x) for x in ((sel or {}).get("ordered_segment_ids") or []) if x
            ]
        gap_doc = (
            ctx.read_json("understanding/gap_report.json")
            if ctx.artifact_exists("understanding/gap_report.json")
            else {}
        )
        if isinstance(gap_doc, dict) and ordered:
            disp = decide_orientation(ctx, gap_doc, ordered)
            if disp.disposition in {"HEARD_KEEP", "HOLLOW_MINT", "KEEP_REQUIRED"}:
                candidates = [
                    c
                    for c in candidates
                    if not (
                        isinstance(c, dict) and is_episode_orientation(c)
                    )
                ]
    except Exception:
        pass
    active_now = sum(1 for row in lines if not row.get("skipped_optional"))
    if require_floor:
        max_stamp = max(0, active_now - need)
        candidates = candidates[:max_stamp]
        if not candidates:
            return 0
    stamped = 0
    for line in candidates:
        reason = str(line.pop("_pending_omit_reason", None) or "omit_ledger")
        try:
            from interview_mux.hosted_vo_authority import may_soft_omit_hosted_line

            gap_for_gate = {
                "interviewer_lines": lines,
            }
            if not may_soft_omit_hosted_line(
                ctx,
                line,
                gap_report=gap_for_gate,
                reason_code=reason if reason != "omit_ledger" else "air_script_omit_sync",
                peer_lines=lines,
            ):
                line.pop("_pending_omit_reason", None)
                continue
        except Exception:
            pass
        line["skipped_optional"] = True
        if not line.get("skip_reason_code"):
            line["skip_reason_code"] = reason
        stamped += 1
    for row in lines:
        if isinstance(row, dict):
            row.pop("_pending_omit_reason", None)
    if not stamped:
        return 0
    report["interviewer_lines"] = lines
    from interview_mux.seat_authority import persist_frozen_seat_doc

    if not persist_frozen_seat_doc(
        ctx,
        "understanding/gap_report.json",
        report,
        reason="stamp_gap_omit_flags",
        skip_handoff=True,
    ):
        return 0
    return stamped


def revive_required_opening_orientation(ctx: RunContext) -> dict[str, Any]:
    """Drop omit skips that fight a required opening_orientation still on air.

    exec_5404: ORIENTATION_ALWAYS revived ``vo_preface_*`` into the EDL while
    omit_ledger still held ``gap_line_skip`` + ``skipped_optional`` — PMQ then
    failed both ``omit_ledger_air_contract`` and ``opening_orientation_contract``.
    """
    notes: list[str] = []
    try:
        from interview_mux.seat_authority import gate_seat_mutation, soft_freeze_active

        if soft_freeze_active(ctx) and not gate_seat_mutation(
            ctx,
            reason="omit_ledger_revive_orientation",
            symptoms=["omit_ledger", "orientation"],
        ):
            return {"changed": False, "notes": ["seat_freeze_blocked_revive"]}
    except Exception:
        try:
            from interview_mux.seat_authority import soft_freeze_active, hard_freeze_active

            if soft_freeze_active(ctx) or hard_freeze_active(ctx):
                return {"changed": False, "notes": ["seat_freeze_blocked_revive_fail_closed"]}
        except Exception:
            return {"changed": False, "notes": ["seat_freeze_blocked_revive_fail_closed"]}
    if not ctx.artifact_exists("understanding/gap_report.json"):
        return {"changed": False, "notes": notes}
    try:
        gap = ctx.read_json("understanding/gap_report.json")
    except Exception:
        return {"changed": False, "notes": notes}
    if not isinstance(gap, dict):
        return {"changed": False, "notes": notes}
    meta = gap.get("opening_orientation")
    if not isinstance(meta, dict) or not meta.get("required"):
        return {"changed": False, "notes": notes}
    oid = str(meta.get("line_id") or "").strip()
    if not oid:
        return {"changed": False, "notes": notes}

    changed = False
    lines = list(gap.get("interviewer_lines") or [])
    new_lines: list[Any] = []
    for row in lines:
        if not isinstance(row, dict):
            new_lines.append(row)
            continue
        if str(row.get("line_id") or "") != oid:
            new_lines.append(row)
            continue
        updated = dict(row)
        cleared = False
        from interview_mux.opening_orientation import clear_stale_orientation_waive_stamps

        updated, stamp_cleared = clear_stale_orientation_waive_stamps(updated)
        if stamp_cleared:
            cleared = True
        if cleared:
            changed = True
            notes.append(f"cleared_gap_skip:{oid}")
        new_lines.append(updated)
    if changed:
        gap = dict(gap)
        gap["interviewer_lines"] = new_lines
        from interview_mux.seat_authority import persist_frozen_seat_doc

        persist_frozen_seat_doc(
            ctx,
            "understanding/gap_report.json",
            gap,
            reason="omit_ledger_revive_orientation",
            skip_handoff=True,
        )

    if ctx.artifact_exists(OMIT_LEDGER_REL):
        try:
            ledger = ctx.read_json(OMIT_LEDGER_REL)
        except Exception:
            ledger = None
        if isinstance(ledger, dict):
            before = len(active_entries(ledger, kind="gap_line_skip", subject_id=oid))
            if before:
                ledger = supersede_entry(ledger, subject_id=oid, kind="gap_line_skip")
                write_omit_ledger(ctx, ledger)
                changed = True
                notes.append(f"superseded_omit_skips:{before}:{oid}")

    # Soft-freeze persist_air_script_omits may no-op on fingerprint match and leave
    # required orientation in omitted_line_ids (exec_13177). Reseat here under End-A.
    try:
        from interview_mux.air_script import (
            build_vo_seats,
            filter_gap_lines_for_air_script,
            load_air_script,
        )
        from interview_mux.mastering_plan_loader import load_plan_raw
        from interview_mux.seat_authority import persist_frozen_seat_doc

        plan = load_plan_raw(ctx)
        if isinstance(plan, dict) and load_air_script(plan):
            gap_now = (
                ctx.read_json("understanding/gap_report.json")
                if ctx.artifact_exists("understanding/gap_report.json")
                else gap
            )
            filtered = filter_gap_lines_for_air_script(
                gap_now if isinstance(gap_now, dict) else gap, plan, ctx=ctx
            )
            seats = build_vo_seats(plan, filtered if isinstance(filtered, dict) else gap)
            script = dict(load_air_script(plan) or {})
            prior = script.get("vo_seats") if isinstance(script.get("vo_seats"), dict) else {}
            if seats != prior:
                script["vo_seats"] = seats
                plan_out = dict(plan)
                plan_out["air_script"] = script
                if persist_frozen_seat_doc(
                    ctx,
                    "mastering/mastering_plan.json",
                    plan_out,
                    reason="omit_ledger_revive_orientation",
                    skip_handoff=True,
                ):
                    changed = True
                    notes.append(f"reseated_orientation:{oid}")
    except Exception:
        pass
    return {"changed": changed, "notes": notes, "line_id": oid}


def sync_stale_omit_order_lock(ctx: RunContext) -> bool:
    """Rebuild omit ledger when selection order_lock drifted (paperwork, not a seat)."""
    if "omit_ledger_order_lock_stale" not in air_contract_errors(ctx):
        return False
    rebuild_and_write_omit_ledger(ctx)
    return "omit_ledger_order_lock_stale" not in air_contract_errors(ctx)


def heal_omit_ledger_air_contract(ctx: RunContext) -> dict[str, Any]:
    """Align gap report + EDL with the omit ledger before post-master QC."""
    notes: list[str] = []
    # Order-lock rebuild is paperwork against the frozen selection — not a seat
    # mutation. Seat freeze must not leave omit_ledger_order_lock_stale stuck.
    pre_errors = air_contract_errors(ctx)
    if "omit_ledger_order_lock_stale" in pre_errors:
        rebuild_and_write_omit_ledger(ctx)
        notes.append("rebuilt_stale_order_lock")
        pre_errors = air_contract_errors(ctx)
        if not pre_errors:
            return {
                "healed": True,
                "notes": notes,
                "errors": [],
                "removed": [],
            }
    # End-A: required-orientation revive is allowlisted paperwork — run before
    # the meta-gated remainder of heal so freeze cannot strand audible_count=0.
    revive_early = revive_required_opening_orientation(ctx)
    if revive_early.get("notes"):
        notes.extend(list(revive_early.get("notes") or []))
    if revive_early.get("changed"):
        pre_errors = air_contract_errors(ctx)
        if not pre_errors:
            return {
                "healed": True,
                "notes": notes,
                "errors": [],
                "removed": [],
            }
    try:
        from interview_mux.seat_authority import gate_seat_mutation, soft_freeze_active

        if soft_freeze_active(ctx) and not gate_seat_mutation(
            ctx,
            reason="omit_ledger_heal_air_contract",
            symptoms=["omit_ledger"],
        ):
            return {
                "changed": False,
                "notes": notes + ["seat_freeze_blocked_heal"],
                "errors": pre_errors,
            }
    except Exception:
        try:
            from interview_mux.seat_authority import soft_freeze_active, hard_freeze_active

            if soft_freeze_active(ctx) or hard_freeze_active(ctx):
                return {
                    "changed": False,
                    "notes": notes + ["seat_freeze_blocked_heal_fail_closed"],
                    "errors": pre_errors,
                }
        except Exception:
            return {
                "changed": False,
                "notes": notes + ["seat_freeze_blocked_heal_fail_closed"],
                "errors": pre_errors,
            }
    revive = revive_required_opening_orientation(ctx)
    errors = air_contract_errors(ctx)
    healable = {
        e
        for e in errors
        if e == "omit_ledger_order_lock_stale"
        or e.startswith("omit_ledger_gap_line_not_skipped:")
        or e.startswith("omit_ledger_gap_line_still_in_edl:")
        or e.startswith("omit_ledger_omitted_line_still_in_edl:")
        or e.startswith("omit_ledger_orientation_replacement_missing:")
    }
    if not healable and not revive.get("changed") and not notes:
        return {"healed": False, "errors": errors, "notes": list(revive.get("notes") or [])}
    notes.extend(list(revive.get("notes") or []))
    if "omit_ledger_order_lock_stale" in healable:
        rebuild_and_write_omit_ledger(ctx)
        notes.append("rebuilt_stale_order_lock")
    if any(e.startswith("omit_ledger_orientation_replacement_missing:") for e in healable):
        try:
            from interview_mux.nugget_layup import (
                GAP_REL,
                PLAN_REL,
                ensure_layup_gap_authority,
            )

            if ctx.artifact_exists(PLAN_REL):
                ensure_layup_gap_authority(ctx)
                notes.append("republished_layup_gap_authority")
            elif ctx.artifact_exists(GAP_REL):
                gap = ctx.read_json(GAP_REL)
                if isinstance(gap, dict) and not gap.get("opening_orientation"):
                    from interview_mux.opening_orientation import ensure_episode_orientation
                    from interview_mux.nugget_layup import _ordered_ids

                    ordered = _ordered_ids(ctx)
                    updated, orient_notes = ensure_episode_orientation(ctx, gap, ordered)
                    if orient_notes:
                        ctx.write_json(GAP_REL, updated)
                        notes.append("stamped_opening_orientation_meta")
        except Exception:
            pass
    # Re-run revive after orientation meta stamps / rebuilds.
    revive2 = revive_required_opening_orientation(ctx)
    notes.extend(list(revive2.get("notes") or []))
    stamped = stamp_gap_report_omit_skips(ctx)
    if stamped:
        notes.append(f"stamped_gap_skips:{stamped}")
    # Required orientation must stay audible — revive again after stamp.
    revive3 = revive_required_opening_orientation(ctx)
    notes.extend(list(revive3.get("notes") or []))
    rec = reconcile_edl_with_omit_ledger(ctx)
    removed = list(rec.get("removed") or [])
    if removed:
        notes.append(f"stripped_edl:{len(removed)}")
    remaining = air_contract_errors(ctx)
    return {
        "healed": True,
        "notes": notes,
        "errors": remaining,
        "removed": removed,
    }


def build_omit_ledger(
    ctx: RunContext,
    *,
    plan: dict[str, Any] | None = None,
    gap_report: dict[str, Any] | None = None,
    prior: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Rebuild ledger from selection excludes + layup skips + waivers + gap skips.

    Preserves operator_override / gap_line_skip entries from ``prior`` that are
    still referenced, then supersedes deterministic layup/selection rows.
    """
    from interview_mux.nugget_layup import (
        CORPUS_REL,
        GAP_REL,
        PLAN_REL,
        is_justified_skip_row,
        row_nugget_ids,
    )
    from interview_mux.order_hash import get_order_lock

    ledger = empty_omit_ledger()
    prior = prior if isinstance(prior, dict) else (
        ctx.read_json(OMIT_LEDGER_REL) if ctx.artifact_exists(OMIT_LEDGER_REL) else {}
    )
    # Carry forward operator / G1 skips that deterministic rebuild would lose.
    preserved: list[dict[str, Any]] = []
    for row in (prior.get("entries") or []) if isinstance(prior, dict) else []:
        if not isinstance(row, dict) or not row.get("active"):
            continue
        if row.get("operator_override") or str(row.get("kind") or "") == "gap_line_skip":
            preserved.append(dict(row))

    entries: list[dict[str, Any]] = list(preserved)
    seq = len(entries) + 1

    sel = (
        ctx.read_json("master/selection.json")
        if ctx.artifact_exists("master/selection.json")
        else {}
    )
    if isinstance(sel, dict):
        lock = get_order_lock(sel)
        if lock:
            ledger["order_lock"] = dict(lock)
            ledger["order_content_hash"] = lock.get("order_content_hash")
        for ex in sel.get("excluded_segment_ids") or []:
            if isinstance(ex, dict):
                sid = str(ex.get("segment_id") or "")
                reason = str(ex.get("reason") or "excluded")
            else:
                sid = str(ex or "")
                reason = "excluded"
            if not sid:
                continue
            entries.append(
                mint_entry(
                    kind="segment_exclude",
                    subject_id=sid,
                    target_segment_id=sid,
                    decision="omit",
                    reason_code=reason,
                    owner_stage="full_master_ranking",
                    source_artifact="master/selection.json",
                    evidence_refs=[f"selection:excluded:{sid}"],
                    compensating_path="nugget_layup_recovery",
                    revisit_if=["selection_reinclude"],
                    decision_confidence=0.9,
                    seq=seq,
                )
            )
            seq += 1

    if plan is None:
        plan = ctx.read_json(PLAN_REL) if ctx.artifact_exists(PLAN_REL) else {}
    plan = plan if isinstance(plan, dict) else {}
    for row in plan.get("layups") or []:
        if not isinstance(row, dict) or not row.get("skip"):
            continue
        if not is_justified_skip_row(row, soft_migrate=True):
            # Still record unjustified skips so recovery can see them, but they
            # do not count as compensated coverage.
            pass
        tid = str(row.get("target_segment_id") or "")
        lid = str(row.get("line_id") or f"vo_layup_{tid}" or tid)
        reason = str(row.get("skip_reason_code") or "empty_or_skip")
        decision = "suppress" if reason == "opening_orientation_owns_target" else "omit"
        replacement = (
            "episode_orientation"
            if reason == "opening_orientation_owns_target"
            else None
        )
        entries.append(
            mint_entry(
                kind="layup_skip",
                subject_id=lid or tid,
                target_segment_id=tid or None,
                decision=decision,
                reason_code=reason,
                owner_stage=str(row.get("owner_stage") or "nugget_layup_compose"),
                source_artifact=PLAN_REL,
                evidence_refs=[str(x) for x in (row.get("evidence_refs") or []) if x],
                value_forgone=[str(x) for x in (row.get("value_forgone") or []) if x],
                compensating_path=str(row.get("compensating_path") or "") or None,
                revisit_if=[str(x) for x in (row.get("revisit_if") or []) if x],
                decision_confidence=(
                    float(row["decision_confidence"])
                    if row.get("decision_confidence") is not None
                    else 0.85
                ),
                replacement_ref=replacement,
                rationale=str(row.get("listener_need_entering_T") or "") or None,
                seq=seq,
            )
        )
        seq += 1

    for waived in plan.get("waived_nugget_ids") or []:
        if isinstance(waived, dict):
            nid = str(waived.get("nugget_id") or "")
            reason = str(waived.get("reason") or "waived")
        else:
            nid = str(waived or "")
            reason = "waived"
        if not nid:
            continue
        entries.append(
            mint_entry(
                kind="nugget_waive",
                subject_id=nid,
                decision="waive",
                reason_code=reason,
                owner_stage="nugget_layup_compose",
                source_artifact=PLAN_REL,
                evidence_refs=[f"nugget:{nid}"],
                compensating_path="operator_or_compose_waiver",
                decision_confidence=0.8,
                seq=seq,
            )
        )
        seq += 1

    if gap_report is None:
        gap_report = ctx.read_json(GAP_REL) if ctx.artifact_exists(GAP_REL) else {}
    if isinstance(gap_report, dict):
        for ln in gap_report.get("interviewer_lines") or []:
            if not isinstance(ln, dict) or not ln.get("skipped_optional"):
                continue
            lid = str(ln.get("line_id") or "")
            if not lid:
                continue
            # Prefer preserved operator row if already present.
            if any(str(e.get("subject_id") or "") == lid for e in preserved):
                continue
            entries.append(
                mint_entry(
                    kind="gap_line_skip",
                    subject_id=lid,
                    target_segment_id=str(ln.get("targets_segment_id") or "") or None,
                    decision="defer",
                    reason_code="g1_skipped_optional",
                    owner_stage="g1_vo_pickup",
                    source_artifact=GAP_REL,
                    evidence_refs=[f"gap_report:{lid}"],
                    compensating_path="operator_skip_optional",
                    operator_override=True,
                    decision_confidence=1.0,
                    seq=seq,
                )
            )
            seq += 1

    # High-salience open nuggets without compensation surface in summary.
    corpus = ctx.read_json(CORPUS_REL) if ctx.artifact_exists(CORPUS_REL) else {}
    open_high = {str(x) for x in (plan.get("open_high_salience_nugget_ids") or []) if x}
    discharged = {str(x) for x in (plan.get("discharged_nugget_ids") or []) if x}
    claimed: set[str] = set()
    for row in plan.get("layups") or []:
        if isinstance(row, dict):
            claimed.update(row_nugget_ids(row))
    unresolved = 0
    for nug in (corpus.get("nuggets") or []) if isinstance(corpus, dict) else []:
        if not isinstance(nug, dict):
            continue
        nid = str(nug.get("nugget_id") or "")
        if not nid or nid in discharged or nid in claimed:
            continue
        sal = str(nug.get("salience") or "")
        if sal in ("high", "critical") or nid in open_high:
            if not nug.get("in_selection") or nid in open_high:
                unresolved += 1

    ledger["entries"] = entries
    summary = summarize_entries(entries)
    summary["unresolved_high_salience"] = max(
        int(summary.get("unresolved_high_salience") or 0), unresolved
    )
    ledger["summary"] = summary
    if isinstance(sel, dict) and get_order_lock(sel):
        ledger["order_lock"] = dict(get_order_lock(sel) or {})
        ledger["order_content_hash"] = (get_order_lock(sel) or {}).get("order_content_hash")
    return ledger


def write_omit_ledger(ctx: RunContext, ledger: dict[str, Any]) -> dict[str, Any]:
    from interview_mux.artifact_writes import write_validated_artifact

    doc = dict(ledger) if isinstance(ledger, dict) else empty_omit_ledger()
    doc["summary"] = summarize_entries(
        [e for e in (doc.get("entries") or []) if isinstance(e, dict)]
    )
    try:
        write_validated_artifact(
            ctx,
            OMIT_LEDGER_REL,
            doc,
            merge_from_disk=False,
            stage_key="nugget_layup_compose",
        )
    except Exception:
        ctx.write_json(OMIT_LEDGER_REL, doc)
    return doc


def rebuild_and_write_omit_ledger(
    ctx: RunContext,
    *,
    plan: dict[str, Any] | None = None,
    gap_report: dict[str, Any] | None = None,
) -> dict[str, Any]:
    prior = ctx.read_json(OMIT_LEDGER_REL) if ctx.artifact_exists(OMIT_LEDGER_REL) else {}
    ledger = build_omit_ledger(ctx, plan=plan, gap_report=gap_report, prior=prior)
    return write_omit_ledger(ctx, ledger)


def record_gap_line_skip(
    ctx: RunContext,
    *,
    line_id: str,
    target_segment_id: str | None = None,
    reason_code: str = "g1_skipped_optional",
    operator_override: bool = True,
) -> dict[str, Any]:
    prior = ctx.read_json(OMIT_LEDGER_REL) if ctx.artifact_exists(OMIT_LEDGER_REL) else empty_omit_ledger()
    entry = mint_entry(
        kind="gap_line_skip",
        subject_id=str(line_id),
        target_segment_id=target_segment_id,
        decision="defer",
        reason_code=reason_code,
        owner_stage="g1_vo_pickup",
        source_artifact="understanding/gap_report.json",
        evidence_refs=[f"gap_report:{line_id}"],
        compensating_path="operator_skip_optional",
        operator_override=operator_override,
        decision_confidence=1.0,
        seq=len(prior.get("entries") or []) + 1,
    )
    updated = supersede_entry(
        prior if isinstance(prior, dict) else empty_omit_ledger(),
        subject_id=str(line_id),
        kind="gap_line_skip",
        replacement=entry,
    )
    out = write_omit_ledger(ctx, updated)
    # Durable omit meta so ORIENTATION_ALWAYS / filter_gap_lines cannot revive a
    # G1-skipped preface without a WAV (exec_11130 audible_count=0 expected=1).
    try:
        from interview_mux.opening_orientation import (
            _omit_orientation_payload,
            is_episode_orientation,
            native_cold_open_segment_id,
        )

        if ctx.artifact_exists("understanding/gap_report.json"):
            gap = ctx.read_json("understanding/gap_report.json")
            if isinstance(gap, dict):
                line = next(
                    (
                        ln
                        for ln in (gap.get("interviewer_lines") or [])
                        if isinstance(ln, dict)
                        and str(ln.get("line_id") or "") == str(line_id)
                    ),
                    None,
                )
                if line is not None and is_episode_orientation(line):
                    ordered: list[str] = []
                    if ctx.artifact_exists("master/selection.json"):
                        sel = ctx.read_json("master/selection.json")
                        if isinstance(sel, dict):
                            ordered = [
                                str(s) for s in (sel.get("ordered_segment_ids") or []) if s
                            ]
                    first = str(ordered[0]) if ordered else str(target_segment_id or "")
                    hook = native_cold_open_segment_id(ctx, ordered) if ordered else None
                    gap = dict(gap)
                    gap["opening_orientation"] = _omit_orientation_payload(
                        first=first,
                        hook=hook,
                        reason=str(reason_code or "g1_skipped_optional"),
                    )
                    for ln in gap.get("interviewer_lines") or []:
                        if (
                            isinstance(ln, dict)
                            and str(ln.get("line_id") or "") == str(line_id)
                        ):
                            ln["skipped_optional"] = True
                            ln["air_script_omit"] = True
                    ctx.write_json(
                        "understanding/gap_report.json", gap, skip_handoff=True
                    )
    except Exception:
        pass
    return out

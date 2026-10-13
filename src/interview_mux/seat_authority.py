"""VO seat freeze + holistic review (Pillar B thrash spine endgame).

Soft freeze after air_contract_sanitize; hard freeze after vo_synthesize + WAV
parity. Post-freeze seat/omit mutations require unlock (operator / G1 red /
LLM seat-rewrite meta-gate).
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Any

from interview_mux.run_context import RunContext

SEAT_FREEZE_META_KEY = "vo_seats_freeze"

# End-A constitution — mutations legal under soft/hard seat freeze without meta-gate.
# Paperwork / shrink / orientation-landing / redundant-transition strip, plus
# named ship-blocking omit/integrity repairs.
# Packaging (CTA/sanitize) is End-A under *soft* freeze only — under hard freeze
# it must pass meta-gate (prevents silent CTA order rewrites after VO freeze).
# Never expand WAV demand (see HARD_FREEZE_FORBIDDEN_ACTIONS).
END_A_PACKAGING_ACTIONS: frozenset[str] = frozenset(
    {
        "media_ip_cta",
        "media_ip_cta_editorial_omits",
        "cta_omit",
        "cta_prune",
        "heal_on_air_cta",
        "artifact_sanitize.selection",
    }
)
END_A_CORE_ACTIONS: frozenset[str] = frozenset(
    {
        "omit_ledger_order_lock_rebuild",
        "omit_ledger_revive_orientation",
        "protect_orientation_from_omit",
        "hosted_vo_disposition_apply",
        "hosted_vo_authority_apply_orientation",
        "stamp_gap_omit_flags",
        "drop_seated_missing_from_gap",
        "clamp_hosted_seats_to_rendered_wavs",
        "drop_blank_segments_under_freeze",
        "retire_consumed_under_freeze",
        "stamp_pair_freeze",
        "trim_pair_freeze",
        "framing_dedupe",
        "normalize_omit_ids",
        "junction_incomplete_cut_omit",
        "edl_overlap_repair_omit",
        "segment_id_remap_omit",
        # Orphaned NLE children of already-excluded CTA parents (exec_002 close).
        "orphaned_cta_child_omit",
        # Progress floors: reactivate existing soft-omitted host lines (never invent).
        "revive_discarded_floor_candidate",
        # A′′ must-land under freeze (shrink / reattach / synth-fail unseat — never expand).
        "hitch_reattach_vo",
        "catastrophe_seated_bind_synth_failed",
        "air_script_gap_omit_sync",
        # Duration band clamp for SFX/MusicGen craft — no seat/omit expand.
        "sdp_duration_band_repair",
        # Late shrink/clamp/integrity under hard freeze (exec_13170 follow-through).
        "soundscape_bed_trim",
        "opening_adjacency_suppress_duplicate",
        "opening_adjacency_drop_orphan",
        "sdp_theme_outro_rebind",
        # Fill uncovered reorder seams after layup VO waive (no new hosted seats).
        "bridge_completeness_mint",
        # Place existing theme palette WAVs into SDP cues (no new hosted VO).
        "music_palette_compose",
        # Republish layup→gap_report under hard freeze (narrative remutate recompose).
        "nugget_layup_gap_publish",
        # Reseat gap-active synth lines stripped by WAV clamp so vo_synthesize can
        # close the hosted floor — does not invent copy (exec_13177).
        "reseated_active_hosted_vo_for_wav",
        # Documents derived from the selection follow a permitted removal:
        # cue re-anchor / skip and lay-up lock copy, never a new seat (ISSUES 113).
        "selection_dependents_reconcile",
    }
)
HARD_FREEZE_ALLOWLIST_ACTIONS: frozenset[str] = frozenset(
    set(END_A_CORE_ACTIONS) | set(END_A_PACKAGING_ACTIONS)
)

# Substring tokens that look like End-A but are not exact rows — refuse loudly.
END_A_NEAR_MISS_TOKENS: tuple[str, ...] = (
    "media_ip_cta",
    "cta_omit",
    "cta_prune",
    "heal_on_air_cta",
    "artifact_sanitize",
    "junction_incomplete_cut",
    "edl_overlap_repair",
    "segment_id_remap",
)


def end_a_action_for_ship_blocking_omit(producer: str) -> str:
    """Map selection-commit producer to the named End-A ship-omit action.

    Unknown producers return "" — never default to junction omit (footgun #7).
    """
    from interview_mux.mix_junction_seat import SHIP_OMIT_PRODUCER_ACTIONS

    p = str(producer or "").strip()
    return str(SHIP_OMIT_PRODUCER_ACTIONS.get(p) or "")


def end_a_near_miss_reason(reason: str) -> str:
    """If reason contains an End-A-ish token but is not an exact allowlist row."""
    exact = str(reason or "").strip()
    if not exact:
        return ""
    if exact in HARD_FREEZE_ALLOWLIST_ACTIONS:
        return ""
    low = exact.lower()
    for tok in END_A_NEAR_MISS_TOKENS:
        if tok in low:
            return f"end_a_near_miss:{tok}"
    return ""


# Explicitly illegal under hard freeze (expand / invent seats).
HARD_FREEZE_FORBIDDEN_ACTIONS: frozenset[str] = frozenset(
    {
        "protect_hosted_vo_floor_reseat",
        # Expand / coverage-gaming under hard freeze (Always-HAU End-A locked NO).
        "soundscape_bed_seed_repair",
        "optimizer_promote_sdp",
        "optimizer_promote_gap",
        "optimizer_promote_transitions",
        "theme_outro_seed",
        "seed_theme_outro",
    }
)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def hard_freeze_action_permitted(action: str, ctx: RunContext | None = None) -> bool:
    """True if ``action`` is End-A permitted for the current freeze depth.

    Packaging rows: soft freeze only. Core paperwork/ship-omit: soft or hard.
    """
    a = str(action or "").strip()
    if not a or a in HARD_FREEZE_FORBIDDEN_ACTIONS:
        return False
    if a in END_A_PACKAGING_ACTIONS:
        if ctx is not None and hard_freeze_active(ctx):
            return False
        return a in HARD_FREEZE_ALLOWLIST_ACTIONS
    return a in END_A_CORE_ACTIONS


def hard_freeze_blocks_action(ctx: RunContext, action: str) -> bool:
    """Under hard freeze, refuse unknown and forbidden actions; allow allowlist."""
    if not hard_freeze_active(ctx):
        return False
    a = str(action or "").strip()
    if a in HARD_FREEZE_FORBIDDEN_ACTIONS:
        return True
    if hard_freeze_action_permitted(a, ctx):
        return False
    return True  # fail-closed on unknown seat mutations


def _cfg_int(path: str, default: int) -> int:
    try:
        from interview_mux.config import merged_config

        cfg = merged_config() or {}
        spine = cfg.get("thrash_spine") if isinstance(cfg, dict) else {}
        if not isinstance(spine, dict):
            return default
        cur: Any = spine
        for part in path.split("."):
            if not isinstance(cur, dict) or part not in cur:
                return default
            cur = cur[part]
        return int(cur)
    except Exception:
        return default


def _cfg_float(path: str, default: float) -> float:
    try:
        from interview_mux.config import merged_config

        cfg = merged_config() or {}
        spine = cfg.get("thrash_spine") if isinstance(cfg, dict) else {}
        if not isinstance(spine, dict):
            return default
        cur: Any = spine
        for part in path.split("."):
            if not isinstance(cur, dict) or part not in cur:
                return default
            cur = cur[part]
        return float(cur)
    except Exception:
        return default


def seat_fingerprint(ctx: RunContext) -> str:
    """Hash of seated + omitted + orientation ids."""
    try:
        from interview_mux.air_script import omitted_vo_line_ids, seated_vo_line_ids

        seated = sorted(str(x) for x in (seated_vo_line_ids(ctx) or []) if x)
        omitted = sorted(str(x) for x in (omitted_vo_line_ids(ctx) or []) if x)
    except Exception:
        seated, omitted = [], []
    orientation: list[str] = []
    try:
        gap = ctx.read_json("understanding/gap_report.json") if ctx.artifact_exists("understanding/gap_report.json") else {}
        for ln in (gap or {}).get("interviewer_lines") or []:
            if not isinstance(ln, dict):
                continue
            lid = str(ln.get("line_id") or "")
            role = str(ln.get("role") or ln.get("kind") or "").lower()
            if "orient" in role or lid.startswith("vo_preface"):
                orientation.append(lid)
    except Exception:
        pass
    body = json.dumps(
        {"seated": seated, "omitted": omitted, "orientation": sorted(set(orientation))},
        sort_keys=True,
    )
    return hashlib.sha256(body.encode("utf-8")).hexdigest()[:16]


def read_seat_freeze(ctx: RunContext) -> dict[str, Any]:
    try:
        meta = ctx.read_json("run_meta.json") if ctx.artifact_exists("run_meta.json") else {}
        epoch = (meta or {}).get("delivery_epoch") if isinstance(meta, dict) else {}
        freeze = (epoch or {}).get(SEAT_FREEZE_META_KEY) if isinstance(epoch, dict) else {}
        return dict(freeze) if isinstance(freeze, dict) else {}
    except Exception:
        return {}


def soft_freeze_active(ctx: RunContext) -> bool:
    fr = read_seat_freeze(ctx)
    return bool(fr.get("soft"))


def freeze_artifact_proves_hard(fr: Any) -> bool:
    """True only when freeze artifact is hard-sealed evidence (A4 thorough SSOT).

    Never treats ``fingerprint`` alone as hard. Explicit ``hard: False`` wins
    over a contradictory ``level`` string (soft+fingerprint+level=hard → not hard).
    """
    if not isinstance(fr, dict) or not fr:
        return False
    if fr.get("hard") is False:
        return False
    if fr.get("hard") is True:
        return True
    lvl = str(fr.get("level") or fr.get("mode") or "").strip().lower()
    return lvl in {"hard", "hard_freeze"}


def hard_freeze_active(ctx: RunContext) -> bool:
    """Live hard-freeze probe — same evidence law as sticky seed (A4 footgun #7)."""
    return freeze_artifact_proves_hard(read_seat_freeze(ctx))


def stamp_soft_seat_freeze(ctx: RunContext, *, reason: str = "air_contract_sanitize") -> dict[str, Any]:
    fp = seat_fingerprint(ctx)
    row = {
        "soft": True,
        "hard": False,
        "level": "soft",
        "fingerprint": fp,
        "soft_at": _utc_now(),
        "soft_reason": str(reason or "")[:160],
        "generation": int(read_seat_freeze(ctx).get("generation") or 0),
        "rewrite_generations_post_soft": int(
            read_seat_freeze(ctx).get("rewrite_generations_post_soft") or 0
        ),
        "rewrite_generations_post_hard": int(
            read_seat_freeze(ctx).get("rewrite_generations_post_hard") or 0
        ),
    }
    prev = read_seat_freeze(ctx)
    if prev.get("hard"):
        row["hard"] = True
        row["level"] = "hard"
        row["hard_at"] = prev.get("hard_at")
        row["hard_reason"] = prev.get("hard_reason")

    def _mut(meta: dict[str, Any]) -> None:
        epoch = dict(meta.get("delivery_epoch") or {})
        epoch[SEAT_FREEZE_META_KEY] = row
        meta["delivery_epoch"] = epoch

    ctx.mutate_run_meta(_mut)
    return row


def stamp_hard_seat_freeze(ctx: RunContext, *, reason: str = "vo_synthesize") -> dict[str, Any]:
    fp = seat_fingerprint(ctx)
    prev = read_seat_freeze(ctx)
    row = {
        "soft": True,
        "hard": True,
        "level": "hard",
        "fingerprint": fp,
        "soft_at": prev.get("soft_at") or _utc_now(),
        "soft_reason": prev.get("soft_reason") or "implied",
        "hard_at": _utc_now(),
        "hard_reason": str(reason or "")[:160],
        "generation": int(prev.get("generation") or 0),
        "rewrite_generations_post_soft": int(prev.get("rewrite_generations_post_soft") or 0),
        "rewrite_generations_post_hard": int(prev.get("rewrite_generations_post_hard") or 0),
    }

    def _mut(meta: dict[str, Any]) -> None:
        epoch = dict(meta.get("delivery_epoch") or {})
        epoch[SEAT_FREEZE_META_KEY] = row
        meta["delivery_epoch"] = epoch

    ctx.mutate_run_meta(_mut)
    return row


def unlock_seat_freeze(
    ctx: RunContext,
    *,
    reason: str,
    clear_hard: bool = True,
    clear_soft: bool = False,
) -> dict[str, Any]:
    """Operator / meta-gate unlock. Prefer clearing hard only for catastrophe."""

    def _mut(meta: dict[str, Any]) -> None:
        epoch = dict(meta.get("delivery_epoch") or {})
        fr = dict(epoch.get(SEAT_FREEZE_META_KEY) or {})
        if clear_hard:
            fr["hard"] = False
            fr["hard_unlocked_at"] = _utc_now()
        if clear_soft:
            fr["soft"] = False
            fr["soft_unlocked_at"] = _utc_now()
        # A4 footgun #1: keep ``level`` honest with hard/soft bits (never leave
        # level=hard after hard unlock).
        if fr.get("hard") is True:
            fr["level"] = "hard"
        elif fr.get("soft"):
            fr["level"] = "soft"
        else:
            fr["level"] = "unlocked"
        fr["unlock_reason"] = str(reason or "")[:200]
        epoch[SEAT_FREEZE_META_KEY] = fr
        meta["delivery_epoch"] = epoch

    ctx.mutate_run_meta(_mut)
    return read_seat_freeze(ctx)


def bump_seat_rewrite_generation(ctx: RunContext) -> int:
    prev = read_seat_freeze(ctx)
    soft_n = int(prev.get("rewrite_generations_post_soft") or 0) + 1
    hard_n = int(prev.get("rewrite_generations_post_hard") or 0)
    if prev.get("hard"):
        hard_n += 1
    gen = int(prev.get("generation") or 0) + 1
    fp = seat_fingerprint(ctx)

    def _mut(meta: dict[str, Any]) -> None:
        epoch = dict(meta.get("delivery_epoch") or {})
        fr = dict(epoch.get(SEAT_FREEZE_META_KEY) or {})
        fr["generation"] = gen
        fr["rewrite_generations_post_soft"] = soft_n
        fr["rewrite_generations_post_hard"] = hard_n
        fr["fingerprint"] = fp
        fr["last_rewrite_at"] = _utc_now()
        # Re-assert soft after rewrite
        fr["soft"] = True
        epoch[SEAT_FREEZE_META_KEY] = fr
        meta["delivery_epoch"] = epoch

    ctx.mutate_run_meta(_mut)
    return gen


def seat_rewrite_budget_ok(ctx: RunContext) -> tuple[bool, str]:
    prev = read_seat_freeze(ctx)
    soft_max = _cfg_int("seat_freeze.max_rewrites_post_soft", 2)
    hard_max = _cfg_int("seat_freeze.max_rewrites_post_hard", 1)
    soft_n = int(prev.get("rewrite_generations_post_soft") or 0)
    hard_n = int(prev.get("rewrite_generations_post_hard") or 0)
    if prev.get("hard") and hard_n >= hard_max:
        return False, f"hard_rewrite_cap:{hard_n}>={hard_max}"
    if prev.get("soft") and soft_n >= soft_max:
        return False, f"soft_rewrite_cap:{soft_n}>={soft_max}"
    return True, "ok"


def _grant_one_shot_rewrite_token(ctx: RunContext, *, reason: str) -> None:
    """Allow exactly one post-freeze seat mutation (Pass B / ensure / clamp)."""

    def _mut(meta: dict[str, Any]) -> None:
        epoch = dict(meta.get("delivery_epoch") or {})
        fr = dict(epoch.get(SEAT_FREEZE_META_KEY) or {})
        fr["one_shot_rewrite"] = True
        fr["one_shot_reason"] = str(reason or "")[:160]
        fr["one_shot_at"] = _utc_now()
        epoch[SEAT_FREEZE_META_KEY] = fr
        meta["delivery_epoch"] = epoch

    try:
        ctx.mutate_run_meta(_mut)
    except Exception as exc:
        raise RuntimeError(f"one_shot_rewrite_token_grant_failed:{exc}") from exc


def consume_one_shot_rewrite_token(ctx: RunContext) -> bool:
    """Consume a granted one-shot rewrite token. True if token was present."""
    fr = read_seat_freeze(ctx)
    if not fr.get("one_shot_rewrite"):
        return False

    def _mut(meta: dict[str, Any]) -> None:
        epoch = dict(meta.get("delivery_epoch") or {})
        row = dict(epoch.get(SEAT_FREEZE_META_KEY) or {})
        row["one_shot_rewrite"] = False
        row.pop("one_shot_reason", None)
        row.pop("one_shot_at", None)
        epoch[SEAT_FREEZE_META_KEY] = row
        meta["delivery_epoch"] = epoch

    try:
        ctx.mutate_run_meta(_mut)
    except Exception:
        return False
    return True


def seat_mutation_allowed(
    ctx: RunContext,
    *,
    reason: str = "",
    require_meta_gate: bool = True,
) -> tuple[bool, str]:
    """Gate Pass B / ensure / omit / reconcile seat rewrites after freeze."""
    fr = read_seat_freeze(ctx)
    if not fr.get("soft") and not fr.get("hard"):
        return True, "unfrozen"
    if fr.get("one_shot_rewrite"):
        return True, "one_shot_token"
    # End-A allowlist — paperwork / ship-omit (any freeze); packaging soft-only.
    reason_exact = str(reason or "").strip()
    if hard_freeze_action_permitted(reason_exact, ctx):
        return True, "end_a_allowlist"
    near = end_a_near_miss_reason(reason_exact)
    if near:
        return False, near
    # Catastrophe unlocks — still subject to rewrite budget (not unlimited).
    # Hosted VO floor is NOT a catastrophe under hard freeze (ownership constitution).
    reason_l = str(reason or "").lower()
    if "hosted_vo_floor" in reason_l or "hosted_framing" in reason_l:
        if hard_freeze_active(ctx):
            return False, "hard_freeze_blocks_hosted_floor_reseat"
    catastrophe = any(
        x in reason_l
        for x in ("g1_red", "operator", "missing_seated_wav")
    ) and "catastrophe_hosted_vo_floor" not in reason_l
    # Legacy token still matched only when not under hard freeze.
    if "catastrophe" in reason_l and "hosted_vo_floor" not in reason_l:
        catastrophe = True
    if "catastrophe_hosted_vo_floor" in reason_l and not hard_freeze_active(ctx):
        catastrophe = True
    # DP-A2 Option A: no packaging substring auto-allow. CTA/sanitize must use an
    # exact End-A allowlist reason (checked above) or meta-gate / one-shot.
    ok, why = seat_rewrite_budget_ok(ctx)
    if not ok and not catastrophe:
        return False, why
    if catastrophe:
        return True, "catastrophe_or_operator"
    if not require_meta_gate:
        return True, "budget_ok"
    # Caller must have already passed meta-gate; this helper is the freeze check.
    # When require_meta_gate, refuse silent mutation — caller uses request_seat_rewrite.
    return False, "frozen_needs_meta_gate"


def request_seat_rewrite(
    ctx: RunContext,
    *,
    proposed_delta: dict[str, Any] | None = None,
    reason: str = "",
    symptoms: list[str] | None = None,
) -> dict[str, Any]:
    """LLM (or deterministic) meta-gate for post-freeze seat/omit rewrite."""
    from interview_mux.timeline_reopen_meta_gate import decide_seat_rewrite

    try:
        decision = decide_seat_rewrite(
            ctx,
            proposed_delta=proposed_delta or {},
            reason=reason,
            symptoms=list(symptoms or []),
        )
    except Exception as exc:
        return {
            "allow": False,
            "refuse_reason": f"seat_rewrite_error:{type(exc).__name__}",
            "opportunity_score": 0.0,
            "rewrite_ops": [],
        }
    if decision.get("allow"):
        unlock_seat_freeze(ctx, reason=f"meta_gate:{reason}", clear_hard=False, clear_soft=False)
        _grant_one_shot_rewrite_token(ctx, reason=str(reason or "meta_gate"))
        # Keep freeze flags; one-shot token lets Pass B/ensure/clamp proceed once.
    return decision


def apply_seat_rewrite_ops(
    ctx: RunContext,
    ops: list[dict[str, Any]],
) -> list[str]:
    """Apply minimal omit/reseat ops from meta-gate, then clamp + re-freeze."""
    notes: list[str] = []
    if not ops:
        return notes
    try:
        gap_path = "understanding/gap_report.json"
        gap = ctx.read_json(gap_path) if ctx.artifact_exists(gap_path) else {}
        lines = list((gap or {}).get("interviewer_lines") or [])
        by_id = {
            str(ln.get("line_id") or ""): ln
            for ln in lines
            if isinstance(ln, dict) and ln.get("line_id")
        }
        for op in ops:
            if not isinstance(op, dict):
                continue
            action = str(op.get("action") or "").lower()
            lid = str(op.get("line_id") or "")
            ln = by_id.get(lid)
            if not ln:
                continue
            if action in {"omit", "skip"}:
                ln["skipped_optional"] = True
                ln["air_script_omit"] = True
                notes.append(f"omit:{lid}")
            elif action in {"unomit", "revive"}:
                ln["skipped_optional"] = False
                ln["air_script_omit"] = False
                ln.pop("omit", None)
                notes.append(f"revive:{lid}")
        gap["interviewer_lines"] = lines
        ctx.write_json(gap_path, gap)
    except Exception as exc:
        notes.append(f"gap_write_err:{exc}")
    try:
        from interview_mux.vo_contract import clamp_hosted_seats_to_rendered_wavs

        clamp_hosted_seats_to_rendered_wavs(ctx)
        notes.append("clamped")
    except Exception:
        pass
    try:
        from interview_mux.artifact_sanitize.air_script import commit_air_contract

        commit_air_contract(ctx, reason="seat_rewrite_meta")
        notes.append("air_contract")
    except Exception:
        pass
    bump_seat_rewrite_generation(ctx)
    stamp_soft_seat_freeze(ctx, reason="post_meta_rewrite")
    return notes


def holistic_seat_review(ctx: RunContext) -> dict[str, Any]:
    """Deterministic seat consistency report before transitions / adjudicate / edl."""
    failures: list[str] = []
    try:
        from interview_mux.air_script import omitted_vo_line_ids, seated_vo_line_ids

        seated = set(str(x) for x in (seated_vo_line_ids(ctx) or []) if x)
        omitted = set(str(x) for x in (omitted_vo_line_ids(ctx) or []) if x)
    except Exception:
        seated, omitted = set(), set()
    both = sorted(seated & omitted)
    if both:
        failures.append(f"seated_and_omitted:{','.join(both[:8])}")

    # Gap eligibility
    try:
        from interview_mux.air_script import gap_line_air_eligible

        gap = (
            ctx.read_json("understanding/gap_report.json")
            if ctx.artifact_exists("understanding/gap_report.json")
            else {}
        )
        by_id = {
            str(ln.get("line_id") or ""): ln
            for ln in (gap or {}).get("interviewer_lines") or []
            if isinstance(ln, dict)
        }
        for lid in sorted(seated):
            ln = by_id.get(lid)
            if ln is None:
                failures.append(f"seated_missing_from_gap:{lid}")
            elif not gap_line_air_eligible(ln):
                failures.append(f"seated_not_eligible:{lid}")
    except Exception as exc:
        failures.append(f"eligibility_err:{exc}")

    # Fingerprint drift vs freeze
    fr = read_seat_freeze(ctx)
    fp = seat_fingerprint(ctx)
    if fr.get("soft") and fr.get("fingerprint") and fr["fingerprint"] != fp:
        # Not always a failure — note drift for sanitize pin
        failures.append(f"fingerprint_drift:{fr['fingerprint']}->{fp}")

    ok = not failures
    report = {
        "ok": ok,
        "failures": failures[:24],
        "fingerprint": fp,
        "seated_n": len(seated),
        "omitted_n": len(omitted),
        "resume_pin": "air_contract_sanitize" if not ok else "",
        "at": _utc_now(),
    }
    try:
        ctx.write_json("operator/holistic_seat_review.json", report, skip_handoff=True)
    except Exception:
        pass
    return report


def freeze_blocks_selection_layup_invalidate(ctx: RunContext) -> bool:
    """When soft/hard freeze holds, selection order change must not wipe Pass B."""
    return soft_freeze_active(ctx) or hard_freeze_active(ctx)


def may_rewind_to_air_script_seams(ctx: RunContext) -> bool:
    """Refuse air_script_seams rewind under hard freeze + assembly unless catastrophe."""
    if hard_freeze_active(ctx):
        try:
            asm = ctx.final_path("master", "assembly.wav")
            if asm.is_file() and asm.stat().st_size > 0:
                # Catastrophe unlocks: missing seated WAV / G1 red.
                try:
                    from interview_mux.vo_contract import seated_vo_missing_ids

                    if seated_vo_missing_ids(ctx):
                        return True
                except Exception:
                    pass
                try:
                    from interview_mux.gates import check_g1_vo

                    if check_g1_vo(ctx):
                        return True
                except Exception:
                    pass
                return False
        except Exception:
            pass
    return True


def gate_seat_mutation(
    ctx: RunContext,
    *,
    reason: str,
    symptoms: list[str] | None = None,
    proposed_delta: dict[str, Any] | None = None,
) -> bool:
    """Return True if a seat/omit mutation may proceed under soft/hard freeze.

    Unfrozen → True. Catastrophe/operator reasons → True. Otherwise require
    ``request_seat_rewrite`` allow; refuse → False (caller must no-op).

    Fail-closed: any exception while freeze holds → refuse (no silent mutate).
    """
    try:
        frozen = soft_freeze_active(ctx) or hard_freeze_active(ctx)
    except Exception:
        frozen = False
    if not frozen:
        return True
    try:
        allowed, why = seat_mutation_allowed(
            ctx, reason=reason, require_meta_gate=True
        )
        if allowed:
            if why == "one_shot_token":
                consume_one_shot_rewrite_token(ctx)
            elif why == "catastrophe_or_operator":
                try:
                    bump_seat_rewrite_generation(ctx)
                except Exception:
                    pass
            return True
        if why != "frozen_needs_meta_gate":
            return False
        dec = request_seat_rewrite(
            ctx,
            proposed_delta=proposed_delta or {"ops": [], "from": reason},
            reason=reason,
            symptoms=list(symptoms or []),
        )
        if dec.get("allow"):
            consume_one_shot_rewrite_token(ctx)
            return True
        return False
    except Exception:
        return False


def operator_seat_unlock_note(ctx: RunContext, *, reason: str) -> None:
    """Stamp operator unlock note and grant one-shot rewrite (G1 / gap CRUD)."""
    unlock_seat_freeze(
        ctx,
        reason=f"operator:{reason}",
        clear_hard=False,
        clear_soft=False,
    )
    _grant_one_shot_rewrite_token(ctx, reason=f"operator:{reason}")


def note_ship_omit_blocked(
    ctx: RunContext,
    *,
    producer: str,
    action: str,
    ids: list[str],
) -> None:
    """Durable note when classified ship-omit cannot land under End-A (footgun #7)."""

    def _mut(meta: dict[str, Any]) -> None:
        epoch = dict(meta.get("delivery_epoch") or {})
        fr = dict(epoch.get(SEAT_FREEZE_META_KEY) or {})
        fr["ship_omit_blocked_at"] = _utc_now()
        fr["ship_omit_blocked_producer"] = str(producer or "")[:80]
        fr["ship_omit_blocked_action"] = str(action or "")[:80]
        fr["ship_omit_blocked_ids"] = [str(x) for x in (ids or [])[:12]]
        epoch[SEAT_FREEZE_META_KEY] = fr
        meta["delivery_epoch"] = epoch

    try:
        ctx.mutate_run_meta(_mut)
    except Exception:
        pass


FROZEN_SEAT_DOCS: frozenset[str] = frozenset(
    {
        "understanding/gap_report.json",
        "master/transitions.json",
        "understanding/sound_design_plan.json",
    }
)

# Seat-truth artifacts beyond the three docs (fingerprint / omit stamps).
FROZEN_SEAT_TRUTH_RELS: frozenset[str] = frozenset(
    {
        "understanding/omit_ledger.json",
        "mastering/mastering_plan.json",
    }
)


def freeze_active(ctx: RunContext) -> bool:
    try:
        return bool(soft_freeze_active(ctx) or hard_freeze_active(ctx))
    except Exception:
        return False


def freeze_blocks_raw_escape(ctx: RunContext, rel: str) -> bool:
    """A′′: under freeze, ``_one_writer_raw`` must not bypass seat docs."""
    rel_n = str(rel or "").replace("\\", "/").lstrip("./")
    if rel_n not in FROZEN_SEAT_DOCS:
        return False
    return freeze_active(ctx)


_CUE_ANCHOR_KEYS = ("segment_id", "before_segment_id", "after_segment_id", "under_segment_id")


def sound_design_plan_stale_versus_selection(ctx: RunContext) -> bool:
    """True when the committed plan anchors a cue on a segment the selection dropped."""
    try:
        if not ctx.artifact_exists("understanding/sound_design_plan.json") or not ctx.artifact_exists(
            "master/selection.json"
        ):
            return False
        plan = ctx.read_json("understanding/sound_design_plan.json")
        sel = ctx.read_json("master/selection.json")
    except Exception:
        return False
    if not isinstance(plan, dict) or not isinstance(sel, dict):
        return False
    ordered = {str(s) for s in (sel.get("ordered_segment_ids") or []) if s}
    if not ordered:
        return False
    flows = plan.get("flow_plans") if isinstance(plan.get("flow_plans"), dict) else {}
    podcast = flows.get("podcast") if isinstance(flows.get("podcast"), dict) else {}
    for cue in podcast.get("cues") or []:
        if not isinstance(cue, dict) or cue.get("skip"):
            continue
        for key in _CUE_ANCHOR_KEYS:
            sid = str(cue.get(key) or "").strip()
            if sid and sid not in ordered:
                return True
    return False


def frozen_seat_write_allowed(
    ctx: RunContext,
    rel: str,
    *,
    reason: str = "",
) -> bool:
    """True when gap / transitions / SDP may persist (unfrozen, End-A, or one-shot).

    A′′ Global Freeze: no cue carve-out — unknown reasons skip-write while frozen.
    """
    rel_n = str(rel or "").replace("\\", "/").lstrip("./")
    if rel_n not in FROZEN_SEAT_DOCS:
        return True
    if not freeze_active(ctx):
        return True
    try:
        fr = read_seat_freeze(ctx)
        if isinstance(fr, dict) and fr.get("one_shot_rewrite"):
            return True
    except Exception:
        pass
    if "segment_id_remap" in str(reason or ""):
        return True
    if hard_freeze_action_permitted(reason, ctx):
        return True
    # The plan's own producer re-deriving a plan that no longer matches the
    # selection is not a seat mutation either: ranking re-ran after the plan
    # was written (exec_065: the committed plan anchored a cue on seg_007,
    # which ranking had since excluded), the stage re-ran to fix that, the
    # soft freeze skipped its write, and the pre-flush barrier refused the
    # stale cue on every pass (ISSUES 104). Soft freeze only; a hard freeze
    # means WAVs are rendered against these seats.
    # The plan's own producer landing the plan under the soft freeze is not a
    # seat mutation at all: the freeze protects seats that exist from foreign
    # rewrites, and the hard freeze (WAVs rendered against the seats) still
    # refuses. ISSUES 116: sound_design_palettes writes the file first, so the
    # stage's production was never "first production" (entry 96) and was
    # skipped as not End-A on every fresh run; the unclamped palettes plan then
    # failed the pre-flush barrier on asset count (exec_084, exec_094).
    if (
        rel_n == "understanding/sound_design_plan.json"
        and str(reason or "").strip() == "sound_design_plan"
        and not hard_freeze_active(ctx)
    ):
        try:
            why = (
                "committed plan anchors cues outside the live selection"
                if sound_design_plan_stale_versus_selection(ctx)
                else "the plan's producer may land its plan before the hard freeze"
            )
            ctx.log(
                f"seat_freeze: sound_design_plan write allowed under soft freeze ({why})",
                level="info",
                stage="sound_design_plan",
            )
        except Exception:
            pass
        return True
    # A seat doc that has never been produced is not a frozen seat: the freeze
    # protects seats that exist from foreign rewrites. air_contract_sanitize
    # stamps the soft freeze and seed order puts transitions and the sound
    # design plan after it, so their first production must land (ISSUES 96).
    try:
        if not ctx.artifact_exists(rel_n):
            ctx.log(
                f"seat_freeze: first production of {rel_n} allowed under soft freeze "
                f"(reason={reason or 'empty'})",
                level="info",
                stage=str(reason or "") or None,
            )
            return True
    except Exception:
        pass
    try:
        ctx.log(
            f"seat_freeze: skip write {rel_n} (not End-A; reason={reason or 'empty'})",
            level="warning",
            stage=str(reason or "") or None,
        )
    except Exception:
        pass
    return False


def persist_frozen_seat_doc(
    ctx: RunContext,
    rel: str,
    doc: Any,
    *,
    reason: str = "",
    **write_kw: Any,
) -> bool:
    """Write gap / transitions / SDP under freeze only for End-A (or one-shot unlock).

    Returns True if the write ran. A3-1: unknown reasons skip-write (freeze wins).
    """
    if not frozen_seat_write_allowed(ctx, rel, reason=reason):
        return False
    try:
        fr = read_seat_freeze(ctx)
        if isinstance(fr, dict) and fr.get("one_shot_rewrite"):
            consume_one_shot_rewrite_token(ctx)
    except Exception:
        pass
    rel_n = str(rel or "").replace("\\", "/").lstrip("./")
    if rel_n == "understanding/gap_report.json" and isinstance(doc, dict):
        from interview_mux.artifact_sanitize.gap_report import commit_gap_report_doc

        commit_gap_report_doc(
            ctx,
            doc,
            reason=reason,
            skip_handoff=bool(write_kw.get("skip_handoff", True)),
            stage_key=write_kw.get("stage_key"),
        )
        return True
    if rel_n == "understanding/sound_design_plan.json" and isinstance(doc, dict):
        # Must pass End-A ``reason`` into commit — write_json admits with
        # reason=stage_key|"write_json", which freeze-skips while returning Path
        # (F9 silent success under hard freeze).
        from interview_mux.artifact_sanitize.one_writer import (
            commit_sound_design_plan_doc,
        )

        before = None
        try:
            before = ctx.read_json(rel_n) if ctx.artifact_exists(rel_n) else None
        except Exception:
            before = None
        stage_key = write_kw.get("stage_key")
        if not stage_key:
            try:
                from interview_mux.write_staging import active_stage_id

                stage_key = active_stage_id()
            except Exception:
                stage_key = None
        # Ownership ALLOW owners for SDP — never use End-A reason as stage_key
        # (reason is freeze constitution; stage_key is writer identity).
        if not stage_key:
            stage_key = "sound_design_vo_finalize"
        commit_sound_design_plan_doc(
            ctx,
            doc,
            reason=reason,
            skip_handoff=bool(write_kw.get("skip_handoff", True)),
            stage_key=str(stage_key),
        )
        try:
            after = ctx.read_json(rel_n) if ctx.artifact_exists(rel_n) else None
        except Exception:
            return False
        import json

        def _flow_fp(d: Any) -> str:
            if not isinstance(d, dict):
                return ""
            try:
                return json.dumps(d.get("flow_plans"), sort_keys=True, default=str)
            except Exception:
                return ""

        want = _flow_fp(doc)
        got = _flow_fp(after)
        if want and got != want and got == _flow_fp(before):
            return False
        return True
    if rel_n == "master/transitions.json" and isinstance(doc, dict):
        from interview_mux.artifact_sanitize.one_writer import commit_transitions_doc

        commit_transitions_doc(
            ctx,
            doc,
            reason=reason,
            skip_handoff=bool(write_kw.get("skip_handoff", True)),
            stage_key=write_kw.get("stage_key"),
        )
        return True
    ctx.write_json(rel, doc, **write_kw)
    return True


def persist_gap_report_stamp(
    ctx: RunContext,
    doc: dict[str, Any],
    *,
    reason: str,
    skip_handoff: bool = True,
    mutation_class: str | None = None,
) -> bool:
    """Land a flag-only change on the gap report from any stage (ISSUES 101).

    Omit, skip, delivery and orientation stamps are required state that
    adjudicate and synthesis compare against the contract. They may come from
    stages that do not own the report, so present the report's current owner
    key (`gap_report_body_owner`) with an End-A ``reason``; the sole-writer
    guard still refuses any change to interviewer text. Returns True when the
    write ran.
    """
    from interview_mux.artifact_ownership import gap_report_body_owner

    prior = None
    try:
        if ctx.artifact_exists("understanding/gap_report.json"):
            prior = ctx.read_json("understanding/gap_report.json")
    except Exception:
        prior = None
    kw: dict[str, Any] = {"skip_handoff": skip_handoff, "stage_key": gap_report_body_owner(prior)}
    if mutation_class:
        kw["mutation_class"] = mutation_class
    return persist_frozen_seat_doc(ctx, "understanding/gap_report.json", doc, reason=reason, **kw)


def persist_frozen_seat_doc_verified(
    ctx: RunContext,
    rel: str,
    doc: Any,
    *,
    reason: str = "",
    touched_line_ids: list[str] | None = None,
    touched_cue_ids: list[str] | None = None,
    **write_kw: Any,
) -> dict[str, Any]:
    """End-A persist with re-read verify (F9). ``ok`` only if persisted and verified.

    Fingerprints:
    - gap: each ``touched_line_ids`` must show omit/skip flags (or be absent for drop).
    - SDP: re-read is a dict; if ``touched_cue_ids`` given, those cue_ids must exist.
    """
    out: dict[str, Any] = {
        "ok": False,
        "persisted": False,
        "skipped": "",
        "verified": False,
        "error": "",
    }
    rel_n = str(rel or "").replace("\\", "/").lstrip("./")
    if not frozen_seat_write_allowed(ctx, rel_n, reason=reason):
        out["skipped"] = "seat_freeze_not_enda"
        out["error"] = out["skipped"]
        return out
    try:
        persisted = bool(
            persist_frozen_seat_doc(ctx, rel_n, doc, reason=reason, **write_kw)
        )
    except Exception as exc:
        out["error"] = f"persist_failed:{exc}"[:200]
        return out
    if not persisted:
        out["skipped"] = "persist_returned_false"
        out["error"] = out["skipped"]
        return out
    out["persisted"] = True
    try:
        live = ctx.read_json(rel_n) if ctx.artifact_exists(rel_n) else None
    except Exception as exc:
        out["error"] = f"reread_failed:{exc}"[:200]
        return out
    if not isinstance(live, dict):
        out["error"] = "reread_not_dict"
        return out
    if rel_n == "understanding/gap_report.json" and touched_line_ids:
        lines: list[Any] = []
        if isinstance(live.get("lines"), list):
            lines.extend(live["lines"])
        if isinstance(live.get("interviewer_lines"), list):
            lines.extend(live["interviewer_lines"])
        by_id = {
            str(ln.get("line_id") or ""): ln
            for ln in lines
            if isinstance(ln, dict) and ln.get("line_id")
        }
        for lid in touched_line_ids:
            lid_s = str(lid or "").strip()
            if not lid_s:
                continue
            # wav filenames in drop_orphan may appear as "x.wav" — strip suffix
            key = lid_s[:-4] if lid_s.endswith(".wav") else lid_s
            row = by_id.get(key) or by_id.get(lid_s)
            if row is None:
                # drop_orphan may remove the line entirely
                continue
            omitted = bool(
                row.get("air_script_omit")
                or row.get("skipped_optional")
                or row.get("skip")
            )
            if not omitted:
                out["error"] = f"gap_omit_not_landed:{lid_s}"
                return out
    if rel_n == "understanding/sound_design_plan.json" and touched_cue_ids:
        cues: list[Any] = []
        for flow in (live.get("flow_plans") or {}).values():
            if isinstance(flow, dict):
                cues.extend(flow.get("cues") or [])
        have = {
            str(c.get("cue_id") or "")
            for c in cues
            if isinstance(c, dict) and c.get("cue_id")
        }
        for cid in touched_cue_ids:
            cid_s = str(cid or "").strip()
            if cid_s and cid_s not in have:
                out["error"] = f"sdp_cue_missing:{cid_s}"
                return out
    out["verified"] = True
    out["ok"] = True
    return out


def frozen_omitted_line_ids(ctx: RunContext) -> set[str]:
    """Omit set to treat as intentional under publishability omit_collateral."""
    if not (soft_freeze_active(ctx) or hard_freeze_active(ctx)):
        return set()
    try:
        from interview_mux.air_script import omitted_vo_line_ids

        return set(str(x) for x in (omitted_vo_line_ids(ctx) or []) if x)
    except Exception:
        return set()

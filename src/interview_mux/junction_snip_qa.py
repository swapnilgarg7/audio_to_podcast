"""Mastering Junction Snips — deterministic edge QA + O(1) thought-complete + feel LLM.

Plan 2 (v2) / S1–S5 simplify: every timeline junction is evaluated with
transcript/energy signals (zero per-edge OpenAI). Hanging native ends use
batched ``junction_thought_complete`` to recut. Feel audit is advisory-only.
Remaster is critical-incomplete + commitment only (no cosmetic / feel remaster,
no nested run_edl, no SDP rewrite, no in-stage fuse/hitch arming).
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from interview_mux.audio_timeline import snap_cut_to_word_boundary
from interview_mux.config import merged_config
from interview_mux.gap_vo_prior_context import (
    ends_complete_thought,
    is_backchannel_only_text,
    is_legal_conceptual_hinge,
    is_micro_segment,
    looks_like_impact_beat,
    prior_context_cfg,
)
from interview_mux.nle_state import load_nle, save_nle
from interview_mux.run_context import RunContext

QA_REL = "master/junction_snip_qa.json"
FEEL_REL = "master/junction_feel_audit.json"
STAGE_ID = "junction_snip_qa"

# Mix QC / coverage side effects written under junction staging during remaster.
# Must promote or flush drops them and PMQ fails planned_music_preserved / outro.
REMASTER_MIX_SIDE_EFFECTS: tuple[str, ...] = (
    "master/edl.json",
    "master/selection.json",
    "master/assembly.wav",
    "master/assembly_ledger.json",
    "master/render_ledger.json",
    "master/bridge_completeness.json",
    "master/music_cue_coverage.json",
    "master/listen_critic.json",
    "master/bed_presence_qc.json",
    "master/underbed_ab_qc.json",
    "master/listenability_contract.json",
    "sound_design/placement_adjustments.json",
    "understanding/sound_design_plan.json",
    "master/transitions/",
)
FEEL_STAGE_KEY = "junction_feel_audit"
FEEL_PROMPT = "mastering/junction-feel-audit.system.txt"


def _canonical_edl_order(edl: dict[str, Any]) -> list[str]:
    return [str(s) for s in (edl.get("ordered_segment_ids") or []) if s]


_BACKCHANNEL_RE = re.compile(
    r"^(okay|ok|yeah|yep|uh.?huh|mm+|mhm|right|sure|got it|i see)[.!?,]*$",
    re.IGNORECASE,
)

def critical_residuals_may_soften(*, meta: dict[str, Any] | None = None) -> bool:
    """CFG-01: quality waivers only — ``e2e_soft`` never softens critical residuals."""
    from interview_mux.e2e_soft import e2e_quality_waivers_enabled

    return bool(e2e_quality_waivers_enabled(meta=meta))


ALLOWED_FEEL_ACTIONS = frozenset(
    {
        "nudge_source_bounds",
        "insert_impact_hold",
        "clamp_air",
        "adjust_music_fade",
        "adjust_crossfade",
        "exclude_micro",
        "merge_micro",
        "thought_complete_recut",
        "retarget_vo_anchor",
    }
)

_EDGE_ACTION_PRIORITY = {
    "extend_later": 100,
    "thought_complete_recut": 95,
    "merge_micro": 90,
    "cut_earlier": 80,
    "exclude_micro": 70,
    "nudge_source_bounds": 40,
}
_KIND_PRIORITY = {
    "on_a_roll": 100,
    "chapter_bleed_incomplete": 95,
    "incomplete_clause": 90,
    "vo_micro": 85,
    "mid_word_start": 50,
    "mid_word_end": 50,
    "leading_silence": 20,
    "trailing_silence": 20,
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def junction_snip_cfg(cfg: dict[str, Any] | None = None) -> dict[str, Any]:
    """Shipped defaults for junction snip QA.

    JSQ-B1: ``mode`` default is ``advisory``, but critical incomplete-cut residuals
    always hard-block (EM8 in ``run_junction_snip_qa``). ``authoritative`` also
    blocks other residual families; ``off`` skips the stage.
    """
    raw = (cfg or merged_config()).get("mastering") or {}
    block = raw.get("junction_snip_qa") if isinstance(raw.get("junction_snip_qa"), dict) else {}
    defaults: dict[str, Any] = {
        # off | advisory | authoritative — see JSQ-B1 dual-meaning note above.
        "mode": "advisory",
        "micro_nudge_ms": 2500,
        "phrase_extend_max_ms": 30_000,
        "impact_hold_ms_min": 1200,
        "impact_hold_ms_max": 3500,
        "feel_audit_enabled": True,
        "thought_complete_llm_enabled": True,
        "thought_complete_max_segments": 4,
        "thought_complete_max_ms": 30_000,
        "max_remaster_rounds": 2,
        "apply_repairs": True,
        "music_soft_crossfade_ms": 180,
        "dead_air_clamp_ms": 2500,
        "pace_multipliers": {
            "sparse": 1.25,
            "fireside": 1.2,
            "balanced": 1.0,
            "dense": 0.85,
            "debate": 0.8,
        },
    }
    return {**defaults, **block}


def _pace_class(ctx: RunContext) -> str:
    if ctx.artifact_exists("understanding/source_acoustic_profile.json"):
        sap = ctx.read_json("understanding/source_acoustic_profile.json")
        if isinstance(sap, dict):
            for key in ("pace_class", "pacing_class", "delivery_pace"):
                val = str(sap.get(key) or "").strip().lower()
                if val:
                    return val
    sonic = (
        ctx.read_json("understanding/sonic_context.json")
        if ctx.artifact_exists("understanding/sonic_context.json")
        else {}
    )
    if isinstance(sonic, dict):
        scenario = sonic.get("scenario") if isinstance(sonic.get("scenario"), dict) else {}
        bucket = str(scenario.get("atlas_bucket") or "").lower()
        if bucket in {"fireside", "debate", "panel", "media_profile"}:
            return "fireside" if bucket == "fireside" else ("debate" if bucket in {"debate", "panel"} else "dense")
    return "balanced"


def _pace_mult(cfg: dict[str, Any], pace: str) -> float:
    table = cfg.get("pace_multipliers") if isinstance(cfg.get("pace_multipliers"), dict) else {}
    try:
        return float(table.get(pace) or table.get("balanced") or 1.0)
    except (TypeError, ValueError):
        return 1.0


def _segments_by_id(ctx: RunContext) -> dict[str, dict[str, Any]]:
    from interview_mux.nle_state import segments_by_id_with_nle

    return segments_by_id_with_nle(ctx)


def _transcript_words(ctx: RunContext) -> list[dict[str, Any]]:
    if not ctx.artifact_exists("transcript/full.json"):
        return []
    full = ctx.read_json("transcript/full.json")
    words = full.get("words") or []
    return [w for w in words if isinstance(w, dict)] if isinstance(words, list) else []


def _text_in_window(words: list[dict[str, Any]], start_ms: int, end_ms: int) -> str:
    parts: list[str] = []
    for w in words:
        ws = int(w.get("start_ms") or 0)
        we = int(w.get("end_ms") or 0)
        # Audio slices are [start_ms, end_ms); words touching only the boundary
        # are not audible in the clip.
        if we <= start_ms or ws >= end_ms:
            continue
        t = str(w.get("text") or w.get("word") or "").strip()
        if t:
            parts.append(t)
    return " ".join(parts)


def _chapter_id_for(segment_id: str, selection: dict[str, Any]) -> str | None:
    for ch in selection.get("chapters") or []:
        if not isinstance(ch, dict):
            continue
        ids = [str(s) for s in (ch.get("segment_ids") or [])]
        if segment_id in ids:
            return str(ch.get("chapter_id") or ch.get("id") or ch.get("title") or "")
    return None


def _speaker_of(seg: dict[str, Any] | None) -> str:
    if not isinstance(seg, dict):
        return ""
    return str(seg.get("speaker_id") or seg.get("speaker") or "")


def _clip_end_text(seg: dict[str, Any] | None, words: list[dict[str, Any]], end_ms: int) -> str:
    # The EDL bound may have moved beyond the original segment boundary during
    # remediation. Always evaluate the words actually audible at the current
    # bound; static segment text would report the same incomplete clause forever.
    audible = _text_in_window(words, max(0, end_ms - 4000), end_ms).strip()
    if audible:
        toks = audible.split()
        return " ".join(toks[-12:]) if len(toks) > 12 else audible
    if isinstance(seg, dict) and str(seg.get("text") or "").strip():
        text = str(seg.get("text") or "").strip()
        toks = text.split()
        return " ".join(toks[-12:]) if len(toks) > 12 else text
    return ""


def _on_a_roll(
    *,
    seg: dict[str, Any],
    end_ms: int,
    words: list[dict[str, Any]],
    phrase_extend_max_ms: int,
) -> bool:
    speaker = _speaker_of(seg)
    if not speaker:
        return False
    # Look ahead on source continuum for same-speaker words with little pause
    ahead = [
        w
        for w in words
        if int(w.get("start_ms") or 0) >= end_ms - 40
        and int(w.get("start_ms") or 0) <= end_ms + phrase_extend_max_ms
        and str(w.get("speaker_id") or w.get("speaker") or speaker) == speaker
    ]
    if not ahead:
        # words may lack speaker_id — fall back to any words continuing quickly
        ahead = [
            w
            for w in words
            if end_ms <= int(w.get("start_ms") or 0) <= end_ms + min(1500, phrase_extend_max_ms)
        ]
    if not ahead:
        return False
    first_start = int(ahead[0].get("start_ms") or 0)
    gap = first_start - end_ms
    return gap < 450


# Never let a phrase-extend invade the next selected speech source start.
SOURCE_OVERLAP_EPS_MS = 80


def _next_speech_source_start(
    clips: list[dict[str, Any]],
    from_index: int,
) -> int | None:
    """Earliest on-air speech start after this clip's start *on tape*.

    The next clip in air order is not the tape neighbour in a reordered
    episode: capping at it put a clip's end before its own start (no repair
    possible) or let an extend run into a tape-later on-air clip that airs
    elsewhere (ISSUES 179).
    """
    if from_index < 0 or from_index >= len(clips) or not isinstance(clips[from_index], dict):
        return None
    try:
        own_start = int(clips[from_index].get("source_start_ms") or 0)
    except (TypeError, ValueError):
        return None
    later: list[int] = []
    for j, other in enumerate(clips):
        if j == from_index or not isinstance(other, dict):
            continue
        if str(other.get("type") or "") != "speech":
            continue
        try:
            start = int(other.get("source_start_ms") or 0)
        except (TypeError, ValueError):
            continue
        if start > own_start:
            later.append(start)
    return min(later) if later else None


def _clamp_end_before_next_speech(
    end_ms: int,
    next_source_start_ms: int | None,
    *,
    eps_ms: int = SOURCE_OVERLAP_EPS_MS,
) -> int:
    if next_source_start_ms is None:
        return end_ms
    return min(end_ms, int(next_source_start_ms) - int(eps_ms))


def _pause_after_word(
    words: list[dict[str, Any]],
    word: dict[str, Any],
    *,
    index: int | None = None,
) -> int | None:
    """Inter-word pause after ``word`` (ms), or None when unknown."""
    try:
        end = int(word.get("end_ms") or 0)
    except (TypeError, ValueError):
        return None
    if index is not None and 0 <= index + 1 < len(words):
        nxt = words[index + 1]
        try:
            return max(0, int(nxt.get("start_ms") or 0) - end)
        except (TypeError, ValueError):
            return None
    for w in words:
        try:
            start = int(w.get("start_ms") or 0)
        except (TypeError, ValueError):
            continue
        if start >= end:
            return max(0, start - end)
    return None


def _find_phrase_end_ms(
    words: list[dict[str, Any]],
    from_ms: int,
    *,
    max_extend_ms: int,
    speaker: str = "",
    hard_cap_ms: int | None = None,
) -> int | None:
    """Extend to the first word end that completes a thought within the window."""
    cap = from_ms + max_extend_ms
    if hard_cap_ms is not None:
        cap = min(cap, int(hard_cap_ms))
    if cap <= from_ms:
        return None
    window = [
        w
        for w in words
        if from_ms < int(w.get("end_ms") or 0) <= cap
    ]
    del speaker
    if not window:
        return None
    accumulated: list[str] = []
    for i, w in enumerate(window):
        tok = str(w.get("text") or w.get("word") or "").strip()
        if tok:
            accumulated.append(tok)
        candidate = " ".join(accumulated)
        if is_backchannel_only_text(candidate):
            continue
        pause = _pause_after_word(window, w, index=i)
        if pause is None:
            pause = _pause_after_word(words, w)
        if is_legal_conceptual_hinge(
            candidate,
            words=words,
            end_ms=int(w.get("end_ms") or 0),
            next_pause_ms=pause,
        ):
            return int(w.get("end_ms") or 0)
    return None


def _find_last_complete_phrase_end(
    words: list[dict[str, Any]],
    end_ms: int,
    *,
    max_lookback_ms: int,
) -> int | None:
    window = [
        w
        for w in words
        if end_ms - max_lookback_ms <= int(w.get("end_ms") or 0) <= end_ms
    ]
    if not window:
        return None
    # Walk backward for a terminal-punctuation word or soft-complete boundary
    for i in range(len(window) - 1, -1, -1):
        toks = [
            str(w.get("text") or w.get("word") or "").strip()
            for w in window[: i + 1]
            if str(w.get("text") or w.get("word") or "").strip()
        ]
        text = " ".join(toks)
        if not text:
            continue
        last = toks[-1]
        pause = None
        if i + 1 < len(window):
            pause = max(
                0,
                int(window[i + 1].get("start_ms") or 0)
                - int(window[i].get("end_ms") or 0),
            )
        else:
            # Pause from end of this word to the original cut / next source word
            pause = max(0, end_ms - int(window[i].get("end_ms") or 0))
            if pause == 0:
                pause = _pause_after_word(words, window[i])
        complete = is_legal_conceptual_hinge(
            text, words=words, end_ms=int(window[i].get("end_ms") or 0), next_pause_ms=pause
        )
        if complete:
            return int(window[i].get("end_ms") or 0)
    return None


def _leading_trailing_silence(
    ctx: RunContext,
    start_ms: int,
    end_ms: int,
    *,
    search_ms: int,
) -> tuple[int | None, int | None]:
    """Return recommended start/end if leading/trailing silence valleys are better."""
    wav = None
    try:
        wav = ctx.read_path("ingest", "normalized.wav")
    except Exception:
        return None, None
    if not wav or not Path(wav).is_file():
        return None, None
    from interview_mux.audio_energy import find_silence_valley_ms, rms_at_ms

    new_start = None
    new_end = None
    try:
        # Leading silence: if head is quiet, snap start forward to valley near first energy
        head_rms = rms_at_ms(Path(wav), start_ms + 80)
        if head_rms is not None and head_rms < 0.01:
            valley = find_silence_valley_ms(Path(wav), start_ms, search_ms=min(search_ms, 800))
            # Prefer moving start later into content — search slightly ahead
            forward = find_silence_valley_ms(
                Path(wav), start_ms + min(400, search_ms // 2), search_ms=min(search_ms, 600)
            )
            if forward > start_ms:
                new_start = forward
            elif valley != start_ms:
                new_start = valley
        tail_rms = rms_at_ms(Path(wav), max(start_ms, end_ms - 80))
        if tail_rms is not None and tail_rms < 0.01:
            valley = find_silence_valley_ms(Path(wav), end_ms, search_ms=min(search_ms, 800))
            if valley < end_ms:
                new_end = valley
    except Exception:
        return None, None
    return new_start, new_end


def _phrase_action_for_incomplete(
    *,
    can_extend: bool,
    can_cut: bool,
    can_complete: bool,
    is_micro: bool,
) -> str:
    """Ladder: extend (in-clip) → thought-complete recut → cut → exclude(micros)."""
    if can_extend:
        return "extend_later"
    if can_complete:
        return "thought_complete_recut"
    if can_cut:
        return "cut_earlier"
    if is_micro:
        return "exclude_micro"
    return "cut_earlier"  # keep critical path; apply will skip without recommendation


def _start_inside_never_touch(
    source_ms: int,
    intervals: list[tuple[int, int, str]] | None,
) -> bool:
    """True when ``source_ms`` sits inside a never-touch slab (not punched hole)."""
    if not intervals:
        return False
    pos = int(source_ms)
    for nt_s, nt_e, _sid in intervals:
        if nt_s <= pos < nt_e:
            return True
    return False


def _add_incomplete_repair_ladder(
    add: Any,
    *,
    kind: str,
    sid: str,
    clip_index: int,
    end_text: str,
    extend_rec: int | None,
    cut_rec: int | None,
    can_complete: bool,
    is_micro: bool,
    evidence: str,
    extra_detail: dict[str, Any] | None = None,
) -> None:
    """Emit extend + cut geometric repairs before thought-complete escalation."""
    base_detail = dict(extra_detail or {})
    base_detail.setdefault("end_text", end_text[-80:])
    if extend_rec is not None:
        add(
            kind,
            severity="critical",
            segment_id=sid,
            clip_index=clip_index,
            action="extend_later",
            detail={**base_detail, "recommended_ms": int(extend_rec)},
            evidence=evidence,
        )
    if cut_rec is not None:
        add(
            kind,
            severity="critical",
            segment_id=sid,
            clip_index=clip_index,
            action="cut_earlier",
            detail={**base_detail, "recommended_ms": int(cut_rec)},
            evidence=evidence,
        )
    if can_complete:
        add(
            kind,
            severity="critical",
            segment_id=sid,
            clip_index=clip_index,
            action="thought_complete_recut",
            detail={
                **base_detail,
                "unrecoverable_within_clip": (
                    extend_rec is None and cut_rec is None and not is_micro
                ),
            },
            evidence=evidence,
        )
    elif extend_rec is None and cut_rec is None and not is_micro:
        add(
            kind,
            severity="critical",
            segment_id=sid,
            clip_index=clip_index,
            action="cut_earlier",
            detail={
                **base_detail,
                "recommended_ms": None,
                "unrecoverable_within_clip": True,
            },
            evidence=evidence,
        )


_INCOMPLETE_CUT_KINDS = frozenset(
    {
        "on_a_roll",
        "incomplete_clause",
        "chapter_bleed_incomplete",
    }
)


def live_incomplete_cut_critical_findings(
    ctx: RunContext,
) -> list[dict[str, Any]]:
    """Fresh detect of critical incomplete-cut residuals on the live EDL."""
    if not ctx.artifact_exists("master/edl.json"):
        return []
    try:
        edl = ctx.read_json("master/edl.json")
    except Exception:
        return []
    if not isinstance(edl, dict):
        return []
    try:
        findings = detect_junction_findings(ctx, edl)
    except Exception:
        return []
    return [
        f
        for f in findings
        if isinstance(f, dict)
        and str(f.get("severity") or "") == "critical"
        and str(f.get("kind") or "") in _INCOMPLETE_CUT_KINDS
    ]


def junction_recut_precedes_mix(ctx: RunContext) -> bool:
    """True when the junction recut ladder must run ahead of the first mix.

    SSOT facade: ``mix_junction_seat.junction_precedes_mix``. Remaster-in-flight
    is an explicit ``remaster_owner``, not bare
    unmarked mix.
    """
    from interview_mux.mix_junction_seat import junction_precedes_mix

    return bool(junction_precedes_mix(ctx))


def reconcile_junction_claim_inventory(ctx: RunContext) -> bool:
    """Rewrite stale applied stamps to match EDL; refresh autopsy. No remaster.

    Paperwork-only path for ``claimed_repairs_missing_from_edl`` when live
    incomplete-cut detect is clean. Supersedes unmatched applied rows (keeps
    evidence), aligns conflicting NLE overrides for those sids to the EDL, and
    refreshes seam autopsy commitment. Returns True when commitment is
    ``committed`` afterward (or already was).
    """
    from interview_mux.seam_autopsy import (
        _applied_repairs_resolved,
        refresh_autopsy_commitment,
        verify_commitment,
    )

    if not ctx.artifact_exists(QA_REL) or not ctx.artifact_exists("master/edl.json"):
        return False
    try:
        report = ctx.read_json(QA_REL)
        edl = ctx.read_json("master/edl.json")
    except Exception:
        return False
    if not isinstance(report, dict) or not isinstance(edl, dict):
        return False

    commitment = verify_commitment(ctx, report, edl=edl)
    if str(commitment.get("status") or "") == "committed":
        return True

    if "claimed_repairs_missing_from_edl" not in (commitment.get("reasons") or []):
        # Other diverge reasons (assembly freshness / order) — not this path.
        return False

    live = live_incomplete_cut_critical_findings(ctx)
    live_sids = {
        str(f.get("segment_id") or "")
        for f in live
        if isinstance(f, dict) and str(f.get("segment_id") or "")
    }

    applied = [dict(a) for a in (report.get("applied") or []) if isinstance(a, dict)]
    if not applied:
        return False

    _resolved, unresolved_now = _applied_repairs_resolved(edl, {"applied": applied})
    unresolved_indexes: set[int] = set()
    for key in unresolved_now:
        parts = str(key).split(":", 2)
        try:
            unresolved_indexes.add(int(parts[0]))
        except ValueError:
            continue
    if not unresolved_indexes:
        return False

    changed = False
    superseded_sids: set[str] = set()
    for index, row in enumerate(applied):
        if index not in unresolved_indexes:
            continue
        if str(row.get("status") or "") not in {"applied", "already_present"}:
            continue
        sid = str(row.get("segment_id") or "")
        if sid and sid in live_sids:
            # Live hanging cut still present — do not bless via supersede.
            continue
        row = dict(row)
        row["status"] = "superseded"
        row["supersede_reason"] = "claim_inventory_reconcile_edl_mismatch"
        applied[index] = row
        if sid:
            superseded_sids.add(sid)
        changed = True

    if not changed:
        return False

    report = dict(report)
    report["applied"] = applied
    report["claim_inventory_reconciled_at"] = _now()
    try:
        from interview_mux.write_staging import write_mirrored_json

        write_mirrored_json(ctx, QA_REL, report)
    except Exception:
        ctx.write_json(QA_REL, report, skip_handoff=True)

    # Align junction-owned NLE overrides for superseded sids to EDL (third liar).
    if superseded_sids:
        try:
            nle = load_nle(ctx)
            overrides = (
                nle.get("segment_overrides")
                if isinstance(nle, dict) and isinstance(nle.get("segment_overrides"), dict)
                else {}
            )
            speech = {
                str(c.get("segment_id") or ""): c
                for c in (edl.get("clips") or [])
                if isinstance(c, dict) and str(c.get("type") or "") == "speech"
            }
            nle_changed = False
            for sid in superseded_sids:
                clip = speech.get(sid)
                ov = overrides.get(sid) if isinstance(overrides, dict) else None
                if not isinstance(clip, dict) or not isinstance(ov, dict):
                    continue
                if ov.get("excluded"):
                    continue
                edl_start = int(clip.get("source_start_ms") or 0)
                edl_end = int(clip.get("source_end_ms") or edl_start)
                ov_start = int(ov["start_ms"]) if ov.get("start_ms") is not None else edl_start
                ov_end = int(ov["end_ms"]) if ov.get("end_ms") is not None else edl_end
                if abs(ov_start - edl_start) >= 20 or abs(ov_end - edl_end) >= 20:
                    ov = dict(ov)
                    ov["start_ms"] = edl_start
                    ov["end_ms"] = edl_end
                    ov["aligned_from_edl_at"] = _now()
                    ov["align_reason"] = "claim_inventory_reconcile"
                    overrides[sid] = ov
                    nle_changed = True
            if nle_changed and isinstance(nle, dict):
                nle = dict(nle)
                nle["segment_overrides"] = overrides
                save_nle(ctx, nle)
        except Exception:
            pass

    try:
        from interview_mux.seam_autopsy import AUTOPSY_REL, build_autopsy, write_autopsy

        if ctx.artifact_exists(AUTOPSY_REL):
            refresh_autopsy_commitment(ctx)
        else:
            write_autopsy(
                ctx,
                build_autopsy(ctx, phase="post_junction", snip_report=report, edl=edl),
            )
    except Exception:
        try:
            refresh_autopsy_commitment(ctx)
        except Exception:
            pass

    # Stamp commitment onto the QA report for Done Authority readers.
    # Paperwork success = claim inventory cleared (assembly freshness is mix-owned).
    try:
        commitment2 = verify_commitment(ctx, report, edl=edl)
        report = dict(report)
        report["commitment"] = commitment2
        try:
            from interview_mux.write_staging import write_mirrored_json

            write_mirrored_json(ctx, QA_REL, report)
        except Exception:
            ctx.write_json(QA_REL, report, skip_handoff=True)
        return "claimed_repairs_missing_from_edl" not in (
            commitment2.get("reasons") or []
        )
    except Exception:
        return False


def clear_stale_incomplete_cut_residuals(ctx: RunContext) -> bool:
    """Drop stamped incomplete-cut criticals when live detect is clean.

    Pending/committed junction QA can retain critical on_a_roll stamps after a
    noop thought_complete_recut or a later EDL heal. Those stamps poison
    publishability pre_mix even when detect_junction_findings reports none.
    """
    if not ctx.artifact_exists(QA_REL):
        return False
    try:
        report = ctx.read_json(QA_REL)
    except Exception:
        return False
    if not isinstance(report, dict):
        return False
    findings = [
        f for f in (report.get("residual_findings") or []) if isinstance(f, dict)
    ]
    stamped_incomplete = [
        f
        for f in findings
        if str(f.get("kind") or "") in _INCOMPLETE_CUT_KINDS
        and str(f.get("severity") or "") == "critical"
        and not (
            isinstance(f.get("detail"), dict)
            and bool(f.get("detail", {}).get("unrecoverable_within_clip"))
        )
    ]
    if not stamped_incomplete:
        return False
    # Only reconcile after junction claimed a heal/commit — never wipe raw findings.
    commitment = report.get("commitment") if isinstance(report.get("commitment"), dict) else {}
    if not (
        report.get("incomplete_cut_producer_heals_armed")
        or report.get("stale_incomplete_cut_reconciled")
        or str(commitment.get("status") or "") == "committed"
    ):
        return False
    live = live_incomplete_cut_critical_findings(ctx)
    if live:
        return False
    cleaned: list[dict[str, Any]] = []
    for f in findings:
        kind = str(f.get("kind") or "")
        if kind in _INCOMPLETE_CUT_KINDS and str(f.get("severity") or "") == "critical":
            row = dict(f)
            row["severity"] = "warning"
            row["stale_incomplete_cut_cleared"] = True
            cleaned.append(row)
        else:
            cleaned.append(f)
    critical_left = [
        f for f in cleaned if str(f.get("severity") or "") == "critical"
    ]
    reasons = [
        r
        for r in (report.get("blocking_reasons") or [])
        if str(r) != "critical_incomplete_cut_residuals"
    ]
    if not critical_left:
        reasons = [
            r
            for r in reasons
            if str(r) != "critical_junction_residuals_after_two_runs"
        ]
    report["residual_findings"] = cleaned
    report["critical_residual_count"] = len(critical_left)
    report["critical_residuals"] = len(critical_left)
    report["critical_count"] = len(critical_left)
    report["blocking_reasons"] = reasons
    report["stale_incomplete_cut_reconciled"] = True
    try:
        from interview_mux.delivery_guardrails import (
            DELIVERY_RESIDUALS_REL,
            bump_residual_ledger_generation,
        )

        bump_residual_ledger_generation(ctx)
        if ctx.artifact_exists(DELIVERY_RESIDUALS_REL):
            doc = ctx.read_json(DELIVERY_RESIDUALS_REL)
            if isinstance(doc, dict):
                rows = []
                for r in doc.get("residuals") or []:
                    if not isinstance(r, dict):
                        continue
                    kind = str(r.get("kind") or "")
                    if kind in _INCOMPLETE_CUT_KINDS or "incomplete_cut" in kind:
                        rr = dict(r)
                        rr["state"] = "stale"
                        rows.append(rr)
                    else:
                        rows.append(r)
                doc["residuals"] = rows
                doc["critical_count"] = sum(
                    1
                    for r in rows
                    if str(r.get("severity") or "") == "critical"
                    and str(r.get("state") or "open") == "open"
                )
                ctx.write_json(DELIVERY_RESIDUALS_REL, doc, skip_handoff=True)
    except Exception:
        pass
    try:
        from interview_mux.write_staging import write_mirrored_json

        write_mirrored_json(ctx, QA_REL, report)
    except Exception:
        try:
            ctx.write_json(QA_REL, report, skip_handoff=True)
        except Exception:
            return False
    return True


def arm_incomplete_cut_producer_heals(
    ctx: RunContext,
    *,
    report: dict[str, Any],
    residual_findings: list[dict[str, Any]],
    commitment: dict[str, Any],
) -> bool:
    """Legacy helper — S5: ``run_junction_snip_qa`` no longer arms fuse/hitch.

    Kept for recovery / tests that want to arm heals explicitly outside the stage.
    """
    incomplete = [
        f
        for f in residual_findings
        if isinstance(f, dict)
        and str(f.get("severity") or "") == "critical"
        and str(f.get("kind") or "")
        in _INCOMPLETE_CUT_KINDS
    ]
    if not incomplete:
        return False
    # Incomplete mid-clause residuals can remain after a "committed" autopsy
    # with empty unresolved keys (extend blocked by never-touch while cut lost
    # the edge-winner race). Always arm producer heals when those residuals
    # are still critical.
    report["incomplete_cut_producer_heals_armed"] = True
    armed = False
    try:
        from interview_mux.stages.low_conf_fuse_stages import (
            run_connector_fuse_pass_junction_heal,
        )

        run_connector_fuse_pass_junction_heal(ctx)
        report["connector_fuse_junction_heal"] = True
        armed = True
    except Exception as fuse_exc:  # noqa: BLE001
        report["connector_fuse_junction_heal_error"] = str(fuse_exc)[:300]
    try:
        from interview_mux.chapter_close_hitch import arm_hitch_listen_restage

        if arm_hitch_listen_restage(ctx):
            report["hitch_listen_restage"] = True
            armed = True
    except Exception as hitch_exc:  # noqa: BLE001
        report["hitch_listen_restage_error"] = str(hitch_exc)[:300]
    return armed


def _merge_candidate_for_clip(
    *,
    clips: list[dict[str, Any]],
    index: int,
    sid: str,
    src_start: int,
    src_end: int,
    speaker: str | None,
    chapter: str | None,
    selection: dict[str, Any],
    segs: dict[str, Any],
    gap_max_ms: int = 450,
    allow_cross_speaker: bool = False,
    allow_cross_chapter_gap_ms: int = 0,
) -> dict[str, Any] | None:
    """Adjacent speech within gap — prefer absorbing the incomplete close.

    ``allow_cross_chapter_gap_ms`` > 0 lets a *source-adjacent* neighbour in the
    next chapter absorb the clip. A chapter_bleed_incomplete lives exactly on a
    chapter boundary, so the same-chapter rule left it with no candidate at all
    (exec_11871 seg_014 — permanent mix refusal).
    """
    if not speaker and not allow_cross_speaker:
        return None
    candidates: list[tuple[int, dict[str, Any]]] = []
    for j in (index - 1, index + 1):
        if j < 0 or j >= len(clips):
            continue
        other = clips[j]
        if str(other.get("type") or "") != "speech":
            continue
        oid = str(other.get("segment_id") or "")
        if not oid or oid == sid:
            continue
        oseg = segs.get(oid) or {}
        other_speaker = _speaker_of(oseg if isinstance(oseg, dict) else None)
        if other_speaker != speaker and not allow_cross_speaker:
            continue
        och = _chapter_id_for(oid, selection)
        cross_chapter = bool(chapter and och and chapter != och)
        oss = int(other.get("source_start_ms") or 0)
        ose = int(other.get("source_end_ms") or oss)
        # Only an air neighbour that is also the tape neighbour in the same
        # direction can absorb this clip. A reordered neighbour read as gap 0
        # (max(0, negative)) and the union aired the unselected tape between
        # them, undoing the editorial order (ISSUES 179).
        if j == index + 1:
            if oss < src_end - SOURCE_OVERLAP_EPS_MS:
                continue
            gap = max(0, oss - src_end)
        else:
            if ose > src_start + SOURCE_OVERLAP_EPS_MS:
                continue
            gap = max(0, src_start - ose)
        if gap > gap_max_ms:
            continue
        u0, u1 = min(src_start, oss), max(src_end, ose)
        if any(
            isinstance(c, dict)
            and str(c.get("type") or "") == "speech"
            and str(c.get("segment_id") or "") not in {sid, oid}
            and int(c.get("source_start_ms") or 0) < u1
            and u0 < int(c.get("source_end_ms") or 0)
            for c in clips
        ):
            continue
        if cross_chapter and gap > int(allow_cross_chapter_gap_ms):
            continue
        candidates.append((gap, other))
    if not candidates:
        return None
    candidates.sort(key=lambda x: x[0])
    other = candidates[0][1]
    oid = str(other.get("segment_id") or "")
    oss = int(other.get("source_start_ms") or 0)
    ose = int(other.get("source_end_ms") or oss)
    cur_dur = max(0, src_end - src_start)
    oth_dur = max(0, ose - oss)
    if cur_dur <= oth_dur:
        drop_id, survivor_id = sid, oid
        new_start, new_end = min(src_start, oss), max(src_end, ose)
    else:
        drop_id, survivor_id = oid, sid
        new_start, new_end = min(src_start, oss), max(src_end, ose)
    return {
        "drop_segment_id": drop_id,
        "survivor_segment_id": survivor_id,
        "new_start_ms": new_start,
        "new_end_ms": new_end,
        "gap_ms": candidates[0][0],
    }


def detect_junction_findings(
    ctx: RunContext,
    edl: dict[str, Any],
    *,
    cfg: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Deterministic detectors for every speech junction — no OpenAI."""
    conf = cfg or junction_snip_cfg()
    pace = _pace_class(ctx)
    mult = _pace_mult(conf, pace)
    micro_nudge = int(int(conf["micro_nudge_ms"]) * mult)
    phrase_max = int(int(conf["phrase_extend_max_ms"]) * mult)
    hold_min = int(int(conf["impact_hold_ms_min"]) * mult)
    hold_max = int(int(conf["impact_hold_ms_max"]) * mult)
    dead_air_clamp = int(int(conf["dead_air_clamp_ms"]) * mult)
    hyst_ms = max(40, int(0.15 * micro_nudge))

    segs = _segments_by_id(ctx)
    words = _transcript_words(ctx)
    selection = (
        ctx.read_json("master/selection.json")
        if ctx.artifact_exists("master/selection.json")
        else {}
    )
    if not isinstance(selection, dict):
        selection = {}
    prior_cfg = prior_context_cfg()
    mix_cfg = merged_config().get("mix") or {}
    margin = int(mix_cfg.get("word_boundary_margin_ms", 50))
    max_shift = int(mix_cfg.get("word_boundary_max_shift_ms", 400))
    nle = load_nle(ctx)
    nudge_history = (
        nle.get("junction_nudge_history")
        if isinstance(nle.get("junction_nudge_history"), dict)
        else {}
    )
    overrides = (
        nle.get("segment_overrides")
        if isinstance(nle.get("segment_overrides"), dict)
        else {}
    )

    clips = [c for c in (edl.get("clips") or []) if isinstance(c, dict)]
    findings: list[dict[str, Any]] = []
    # One winner per (segment_id, edge, action). Extend and cut must coexist:
    # never-touch often no-ops extend while cut_earlier still clears "Well," /
    # mid-word hinges (forensics exec_11130 seg_003f).
    edge_winners: dict[tuple[Any, ...], dict[str, Any]] = {}

    def _edge_key(
        segment_id: str | None, action: str, detail: dict[str, Any]
    ) -> tuple[Any, ...] | None:
        if not segment_id:
            return None
        if action in {
            "extend_later",
            "cut_earlier",
            "merge_micro",
            "thought_complete_recut",
            "exclude_micro",
        }:
            return (segment_id, "end", action)
        if action == "nudge_source_bounds":
            return (segment_id, str(detail.get("edge") or "end"), action)
        return None

    def add(
        kind: str,
        *,
        severity: str = "warn",
        segment_id: str | None = None,
        clip_index: int | None = None,
        action: str,
        detail: dict[str, Any] | None = None,
        evidence: str = "",
    ) -> None:
        detail = dict(detail or {})
        # A recorded ``accepted_hanging_end`` (entry 72: no recut, no fuse,
        # omit refused for a hard keep) is the terminal outcome of the ladder
        # for that clip. Honour it here, for every incomplete-cut kind, rather
        # than in one detection branch: the chapter-bleed and incomplete-clause
        # branches never saw it and re-raised the same critical on every pass
        # (ISSUES 100, upstream exec_006 mix <-> junction). Advisory keeps the
        # finding visible in the QA report without blocking mix.
        if (
            severity == "critical"
            and kind in _INCOMPLETE_CUT_KINDS
            and segment_id
            and isinstance(overrides.get(segment_id), dict)
            and overrides[segment_id].get("accepted_hanging_end")
        ):
            severity = "advisory"
            detail["accepted_hanging_end"] = True
        # Hysteresis: suppress re-fire unless delta large or valley moved.
        if action == "nudge_source_bounds" and segment_id:
            edge = str(detail.get("edge") or "end")
            hist = nudge_history.get(f"{segment_id}:{edge}")
            if isinstance(hist, dict) and detail.get("recommended_ms") is not None:
                prev = hist.get("applied_ms")
                if prev is not None and abs(int(detail["recommended_ms"]) - int(prev)) < hyst_ms:
                    return
        row = {
            "kind": kind,
            "severity": severity,
            "segment_id": segment_id,
            "clip_index": clip_index,
            "action": action,
            "detail": detail,
            "evidence": evidence,
        }
        key = _edge_key(segment_id, action, detail)
        if key is None:
            findings.append(row)
            return
        score = _EDGE_ACTION_PRIORITY.get(action, 0) + _KIND_PRIORITY.get(kind, 0)
        if severity == "critical":
            score += 25
        prev = edge_winners.get(key)
        if prev is None or score > int(prev.get("_score") or 0):
            row["_score"] = score
            edge_winners[key] = row

    for i, clip in enumerate(clips):
        ctype = str(clip.get("type") or "")
        if ctype == "speech":
            sid = str(clip.get("segment_id") or "")
            seg = segs.get(sid) or {}
            src_start = int(clip.get("source_start_ms") or 0)
            src_end = int(clip.get("source_end_ms") or src_start)
            # Word-boundary mid-word risk
            snapped_start = snap_cut_to_word_boundary(
                src_start, words, margin_ms=0, max_shift_ms=max_shift
            )
            snapped_end = snap_cut_to_word_boundary(
                src_end, words, margin_ms=margin, max_shift_ms=max_shift
            )
            if abs(snapped_start - src_start) >= 25:
                add(
                    "mid_word_start",
                    segment_id=sid,
                    clip_index=i,
                    action="nudge_source_bounds",
                    detail={"edge": "start", "recommended_ms": snapped_start},
                    evidence=f"start {src_start} → word snap {snapped_start}",
                )
            if abs(snapped_end - src_end) >= 25:
                next_start = _next_speech_source_start(clips, i)
                snapped_end = _clamp_end_before_next_speech(snapped_end, next_start)
                if snapped_end > src_start + 300 and abs(snapped_end - src_end) >= 25:
                    ov_end = overrides.get(sid) if isinstance(overrides.get(sid), dict) else {}
                    locked_end = ov_end.get("end_ms") if isinstance(ov_end, dict) else None
                    # Intentional NLE trim (incomplete-cut phrase end) is not mid-word.
                    if locked_end is not None and abs(int(locked_end) - src_end) < 20:
                        pass
                    else:
                        add(
                            "mid_word_end",
                            segment_id=sid,
                            clip_index=i,
                            action="nudge_source_bounds",
                            detail={"edge": "end", "recommended_ms": snapped_end},
                            evidence=f"end {src_end} → word snap {snapped_end}",
                        )

            lead, trail = _leading_trailing_silence(
                ctx, src_start, src_end, search_ms=micro_nudge
            )
            if lead is not None and lead > src_start + 40:
                add(
                    "leading_silence",
                    segment_id=sid,
                    clip_index=i,
                    action="nudge_source_bounds",
                    detail={"edge": "start", "recommended_ms": lead},
                    evidence=f"leading silence valley → {lead}",
                )
            if trail is not None and trail < src_end - 40:
                # Trailing silence must not create incomplete ends.
                tentative_end_text = _clip_end_text(
                    seg if isinstance(seg, dict) else None, words, trail
                )
                if tentative_end_text and not is_legal_conceptual_hinge(
                    tentative_end_text, words=words, end_ms=trail
                ):
                    pass
                else:
                    add(
                        "trailing_silence",
                        segment_id=sid,
                        clip_index=i,
                        action="nudge_source_bounds",
                        detail={"edge": "end", "recommended_ms": trail},
                        evidence=f"trailing silence valley → {trail}",
                    )

            end_text = _clip_end_text(seg if isinstance(seg, dict) else None, words, src_end)
            from interview_mux.gap_vo_prior_context import (
                clause_continues_after,
                is_legal_conceptual_hinge,
            )

            legal = bool(end_text) and is_legal_conceptual_hinge(
                end_text, words=words, end_ms=src_end, next_pause_ms=None
            )
            continues = clause_continues_after(words, src_end, max_lookahead_ms=phrase_max)
            if is_backchannel_only_text(end_text) or is_backchannel_only_text(
                end_text.split()[-1] if end_text.split() else ""
            ):
                # A lone "okay" / "right" is an acknowledgment, not a broken sentence.
                legal = True
                continues = False
            incomplete = bool(end_text) and (not legal or continues)
            on_roll = incomplete and (
                continues
                or _on_a_roll(
                    seg=seg if isinstance(seg, dict) else {},
                    end_ms=src_end,
                    words=words,
                    phrase_extend_max_ms=phrase_max,
                )
            )
            ch = _chapter_id_for(sid, selection)
            next_sid = None
            for j in range(i + 1, len(clips)):
                if str(clips[j].get("type") or "") == "speech":
                    next_sid = str(clips[j].get("segment_id") or "")
                    break
            next_ch = _chapter_id_for(next_sid, selection) if next_sid else None
            chapter_bleed = bool(incomplete and ch and next_ch and ch != next_ch)
            speaker = _speaker_of(seg if isinstance(seg, dict) else None)
            is_micro = bool(
                is_micro_segment(seg if isinstance(seg, dict) else None, cfg=prior_cfg)
                or _BACKCHANNEL_RE.match(str((seg or {}).get("text") or "").strip())
            )
            from interview_mux.thought_complete_recut import lookahead_available

            can_complete = bool(
                incomplete
                and not chapter_bleed
                and lookahead_available(
                    clips=clips,
                    index=i,
                    words=words,
                    speaker=speaker,
                    segs=segs,
                    max_segments=max(1, int(conf.get("thought_complete_max_segments") or 4)),
                    max_ms=max(
                        phrase_max,
                        int(conf.get("thought_complete_max_ms") or phrase_max),
                    ),
                )
            )

            next_src_start = _next_speech_source_start(clips, i)
            extend_hard_cap = (
                int(next_src_start) - SOURCE_OVERLAP_EPS_MS
                if next_src_start is not None
                else None
            )

            extend_speaker = "" if continues else speaker
            if on_roll and not chapter_bleed:
                extended = _find_phrase_end_ms(
                    words,
                    src_end,
                    max_extend_ms=phrase_max,
                    speaker=extend_speaker,
                    hard_cap_ms=extend_hard_cap,
                )
                earlier = _find_last_complete_phrase_end(
                    words, src_end, max_lookback_ms=max(phrase_max, 30_000)
                )
                # A long extend is a different repair. A few dozen milliseconds
                # past the concept hinge is the short tail and still cuts back.
                if (
                    extended is not None
                    and earlier is not None
                    and src_end - int(earlier) > 80
                ):
                    earlier = None
                can_cut = bool(
                    earlier is not None
                    and earlier > src_start + 300
                    # Align with apply noop Δ (20ms): short hanging tails like
                    # "Well," / "It" often sit 40–60ms past the last complete
                    # phrase — the old 80ms floor dropped cut_earlier entirely
                    # while extend lost to never-touch (forensics exec_11130).
                    and earlier < src_end - 20
                )
                # Invasion would leave incomplete — prefer cut/merge over fake extend.
                if (
                    extended is not None
                    and extend_hard_cap is not None
                    and extended >= extend_hard_cap
                    and earlier is not None
                    and can_cut
                ):
                    extended = None
                extend_rec = None
                if extended is not None:
                    extend_rec = _clamp_end_before_next_speech(
                        int(extended), next_src_start
                    )
                    if extend_rec <= src_end + 20:
                        extend_rec = None
                cut_rec = int(earlier) if can_cut and earlier is not None else None
                # Tape trail-off: no extend, no earlier cut, and no legal close
                # within reach either side. EDL QC accepts exactly this case
                # (entry 59); flagging it here left only "omit", which the
                # selection sanitizer refuses for a hard keep, so mix could
                # never seat (exec_052 seg_060, ISSUES entry 63). Same predicate.
                tape_trail_off = bool(
                    extend_rec is None
                    and cut_rec is None
                    and isinstance(overrides.get(sid), dict)
                    and overrides[sid].get("accepted_hanging_end")
                )
                if not tape_trail_off and extend_rec is None and cut_rec is None and not can_complete:
                    try:
                        from interview_mux.edl_narrative_qc import _hinge_reachable

                        tape_trail_off = not _hinge_reachable(clip, words, src_end)
                    except Exception:
                        tape_trail_off = False
                if not tape_trail_off:
                    _add_incomplete_repair_ladder(
                        add,
                        kind="on_a_roll",
                        sid=sid,
                        clip_index=i,
                        end_text=end_text,
                        extend_rec=extend_rec,
                        cut_rec=cut_rec,
                        can_complete=can_complete,
                        is_micro=is_micro,
                        evidence=f"incomplete end {end_text[-40:]!r}; same-speaker continuum",
                    )
            elif incomplete and chapter_bleed:
                from interview_mux.chapter_close_hitch import hitch_latch_committed

                last_in_chapter = False
                if ch and sid:
                    ch_ids = []
                    for crow in selection.get("chapters") or []:
                        if not isinstance(crow, dict):
                            continue
                        if str(crow.get("chapter_id") or crow.get("id") or "") == str(ch):
                            ch_ids = [str(s) for s in (crow.get("segment_ids") or []) if s]
                            break
                    last_in_chapter = bool(ch_ids) and ch_ids[-1] == sid
                if hitch_latch_committed(ctx) and last_in_chapter:
                    extended = _find_phrase_end_ms(
                        words,
                        src_end,
                        max_extend_ms=phrase_max,
                        speaker="" if continues else speaker,
                        hard_cap_ms=extend_hard_cap,
                    )
                    earlier = _find_last_complete_phrase_end(
                        words, src_end, max_lookback_ms=max(phrase_max, 30_000)
                    )
                    can_cut = bool(
                        earlier is not None
                        and earlier > src_start + 300
                        and earlier < src_end - 20
                    )
                    action = _phrase_action_for_incomplete(
                        can_extend=extended is not None,
                        can_cut=can_cut,
                        can_complete=False,
                        is_micro=is_micro,
                    )
                    recommended = extended if extended is not None else earlier
                    add(
                        "chapter_bleed_incomplete",
                        severity="critical",
                        segment_id=sid,
                        clip_index=i,
                        action=action,
                        detail={
                            "recommended_ms": recommended,
                            "end_text": end_text[-80:],
                            "hitch_last_in_chapter": True,
                            "unrecoverable_within_clip": (
                                recommended is None and not is_micro
                            ),
                        },
                        evidence=f"incomplete at chapter hinge (hitch last): {end_text[-40:]!r}",
                    )
                else:
                    earlier = _find_last_complete_phrase_end(
                        words, src_end, max_lookback_ms=max(phrase_max, 30_000)
                    )
                    can_cut = bool(
                        earlier is not None
                        and earlier > src_start + 300
                        and earlier < src_end - 20
                    )
                    action = "cut_earlier" if can_cut else ("exclude_micro" if is_micro else "cut_earlier")
                    add(
                        "chapter_bleed_incomplete",
                        severity="critical",
                        segment_id=sid,
                        clip_index=i,
                        action=action,
                        detail={
                            "recommended_ms": earlier,
                            "end_text": end_text[-80:],
                            "unrecoverable_within_clip": not can_cut and not is_micro,
                        },
                        evidence=f"incomplete at chapter hinge: {end_text[-40:]!r}",
                    )
            elif incomplete:
                extended = _find_phrase_end_ms(
                    words,
                    src_end,
                    max_extend_ms=phrase_max,
                    speaker="" if continues else speaker,
                    hard_cap_ms=extend_hard_cap,
                )
                earlier = _find_last_complete_phrase_end(
                    words, src_end, max_lookback_ms=max(phrase_max, 30_000)
                )
                if (
                    extended is not None
                    and earlier is not None
                    and src_end - int(earlier) > 80
                ):
                    earlier = None
                can_cut = bool(
                    earlier is not None
                    and earlier > src_start + 300
                    # Align with apply noop Δ (20ms): short hanging tails like
                    # "Well," / "It" often sit 40–60ms past the last complete
                    # phrase — the old 80ms floor dropped cut_earlier entirely
                    # while extend lost to never-touch (forensics exec_11130).
                    and earlier < src_end - 20
                )
                if (
                    extended is not None
                    and extend_hard_cap is not None
                    and extended >= extend_hard_cap
                    and earlier is not None
                    and can_cut
                ):
                    extended = None
                extend_rec = None
                if extended is not None:
                    extend_rec = _clamp_end_before_next_speech(
                        int(extended), next_src_start
                    )
                    if extend_rec <= src_end + 20:
                        extend_rec = None
                cut_rec = int(earlier) if can_cut and earlier is not None else None
                _add_incomplete_repair_ladder(
                    add,
                    kind="incomplete_clause",
                    sid=sid,
                    clip_index=i,
                    end_text=end_text,
                    extend_rec=extend_rec,
                    cut_rec=cut_rec,
                    can_complete=can_complete,
                    is_micro=is_micro,
                    evidence=f"incomplete clause: {end_text[-40:]!r}",
                )

            # Impact hold: impactful speech followed soon by VO without hold
            if looks_like_impact_beat(seg if isinstance(seg, dict) else None, cfg=prior_cfg):
                has_hold = False
                next_vo = False
                for j in range(i + 1, min(i + 6, len(clips))):
                    nj = clips[j]
                    nt = str(nj.get("type") or "")
                    if nt == "silence" and str(nj.get("air_kind") or "") == "impact_hold":
                        has_hold = True
                        break
                    if nt == "vo_pickup":
                        next_vo = True
                        break
                    if nt == "speech":
                        break
                if next_vo and not has_hold:
                    hold_ms = max(hold_min, min(hold_max, int(2000 * mult)))
                    add(
                        "missing_impact_hold",
                        segment_id=sid,
                        clip_index=i,
                        action="insert_impact_hold",
                        detail={"hold_ms": hold_ms},
                        evidence="impact native close → VO without music/speech-free hold",
                    )

            # VO → micro
            if is_micro:
                prev_vo = False
                for j in range(i - 1, max(-1, i - 5), -1):
                    pt = str(clips[j].get("type") or "")
                    if pt == "vo_pickup":
                        prev_vo = True
                        break
                    if pt == "speech":
                        break
                if prev_vo:
                    add(
                        "vo_micro",
                        severity="critical",
                        segment_id=sid,
                        clip_index=i,
                        action="exclude_micro",
                        detail={},
                        evidence=f"micro/backchannel after VO: {(seg or {}).get('text', '')!r}",
                    )

        elif ctype == "silence":
            air = str(clip.get("air_kind") or "")
            dur = int(clip.get("duration_ms") or 0)
            # opening_music is a cold-open bed reservation (often ~12–14s), not pad —
            # but only when an audible cold_open WAV exists (exec_11130 hollow pad).
            if air == "opening_music":
                from interview_mux.theme_slot_integrity import hollow_opening_music_finding

                hollow = hollow_opening_music_finding(ctx)
                # The cold-open theme WAV comes from the music band. On the
                # pre-mix junction pass (entry 62) music has not run yet, so a
                # missing theme is premature, not a residual (exec_055, entry 72).
                try:
                    from interview_mux.delivery_guardrails import music_epoch_complete

                    if hollow and not music_epoch_complete(ctx):
                        hollow = None
                except Exception:
                    pass
                if hollow:
                    add(
                        "hollow_opening_music",
                        severity="critical",
                        clip_index=i,
                        action="regenerate_theme_cold_open",
                        detail=hollow.get("detail") or {},
                        evidence=str(hollow.get("evidence") or "hollow opening_music"),
                    )
                continue
            # chapter_music_bridge is intentional music-filled air (4s), not dead pad.
            if air == "chapter_music_bridge":
                continue
            if air == "impact_hold":
                continue
            if dur > dead_air_clamp:
                add(
                    "dead_air_stack",
                    clip_index=i,
                    action="clamp_air",
                    detail={"recommended_ms": dead_air_clamp, "current_ms": dur},
                    evidence=f"silence {air or 'pad'} {dur}ms > clamp {dead_air_clamp}",
                )

    for row in edge_winners.values():
        row.pop("_score", None)
        findings.append(row)

    # Music transition faults from SDP cues (hard cuts / missing soft fades)
    findings.extend(_detect_music_transition_findings(ctx, conf))

    return findings


def _detect_music_transition_findings(
    ctx: RunContext, conf: dict[str, Any]
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    if not ctx.artifact_exists("understanding/sound_design_plan.json"):
        return out
    plan = ctx.read_json("understanding/sound_design_plan.json")
    if not isinstance(plan, dict):
        return out
    soft_xf = int(conf.get("music_soft_crossfade_ms") or 180)
    music_cfg = (merged_config().get("mastering") or {}).get("music_continuity") or {}
    require_bookends = bool(music_cfg.get("require_true_bookend_anchors", True))
    cues: list[dict[str, Any]] = []
    flow = (plan.get("flow_plans") or {}).get("podcast") or {}
    if isinstance(flow, dict):
        raw = flow.get("cues") or []
        if isinstance(raw, list):
            cues = [c for c in raw if isinstance(c, dict)]
    if not cues and isinstance(plan.get("cues"), list):
        cues = [c for c in plan["cues"] if isinstance(c, dict)]
    from interview_mux.placement_qa import apply_placement_adjustments

    cues = apply_placement_adjustments(ctx, cues)
    # Contiguous under_segment_span beds already carry scene XF — skip spam.
    for cue in cues:
        aid = str(cue.get("asset_id") or "")
        if not aid:
            continue
        placement = str(cue.get("placement") or "")
        if placement == "under_segment_span":
            continue
        xf_raw = cue.get("crossfade_ms")
        role = str(cue.get("role") or "")
        effective_xf = int(xf_raw) if xf_raw is not None else 0
        # Abrupt beds / bookends into speech
        if placement in {"under_segment", "after_segment", "before_segment"} or role.startswith(
            "theme_"
        ):
            if effective_xf < soft_xf:
                severity = "warn"
                if require_bookends and (
                    "cold_open" in role or "outro" in role or placement in {"before_segment", "after_segment"}
                ):
                    severity = "critical" if effective_xf <= 0 else "warn"
                out.append(
                    {
                        "kind": "music_hard_transition",
                        "severity": severity,
                        "segment_id": cue.get("segment_id"),
                        "clip_index": None,
                        "action": "adjust_music_fade",
                        "detail": {
                            "asset_id": aid,
                            "suggested_crossfade_ms": soft_xf,
                            "placement": placement,
                            "effective_crossfade_ms": effective_xf,
                        },
                        "evidence": (
                            f"music cue {aid} missing/soft crossfade "
                            f"(effective={effective_xf}, need>={soft_xf})"
                        ),
                    }
                )
    return out


def _recompute_timeline(clips: list[dict[str, Any]]) -> int:
    from interview_mux.listenability_guards import reindex_clip_timeline

    return reindex_clip_timeline(clips)


def refuse_mix_if_live_incomplete_cuts(ctx: RunContext) -> None:
    """F5 1A/3A: mix/remaster must not render live hanging mid-thought clips."""
    live = live_incomplete_cut_critical_findings(ctx)
    if not live:
        return
    # Junction's own ladder remasters between repair rounds. Refusing there would
    # abort the recut owner before it can rescan (exec_11871 spin) — junction still
    # blocks at its terminal check (`critical_incomplete_cut_residuals`).
    if getattr(ctx, "_junction_snip_qa_inner", False):
        ctx.log(
            "junction inner remaster: rendering with "
            f"{len(live)} live incomplete-cut residual(s) — terminal check still gates ship",
            level="info",
            stage=STAGE_ID,
        )
        return
    kinds = sorted({str(f.get("kind") or "") for f in live if f.get("kind")})
    sids = [
        str(f.get("segment_id") or "")
        for f in live
        if str(f.get("segment_id") or "").strip()
    ]
    # Advisory (ISSUES 185): a hanging-clause verdict is a junction judgement;
    # junction already spent its recut budget, and refusing mix reran the same
    # detector until max_mix_cycles (61, 72, 75, 100, 131, 168).
    ctx.log(
        "mix: rendering with live incomplete-cut residual(s) (advisory): "
        + ",".join(kinds[:4] or ["incomplete_cut"]),
        level="warning",
        stage="mix",
        detail={"count": len(live), "kinds": kinds[:6], "segment_ids": sids[:8]},
    )


def _speech_clip_index(clips: list[dict[str, Any]], sid: str) -> int:
    key = str(sid or "").strip()
    if not key:
        return -1
    for i, clip in enumerate(clips):
        if str(clip.get("type") or "") == "speech" and str(clip.get("segment_id") or "") == key:
            return i
    return -1


def _apply_merge_plan(
    clips: list[dict[str, Any]],
    overrides: dict[str, Any],
    excluded: set[str],
    exclude_reasons: dict[str, str],
    plan: dict[str, Any],
    *,
    reason: str,
) -> list[dict[str, Any]]:
    """Absorb drop into survivor bounds and pull the drop clip off the EDL."""
    drop_id = str(plan.get("drop_segment_id") or "")
    survivor_id = str(plan.get("survivor_segment_id") or "")
    new_start = int(plan.get("new_start_ms") or 0)
    new_end = int(plan.get("new_end_ms") or 0)
    if not drop_id or not survivor_id or new_end <= new_start:
        return clips
    for clip in clips:
        if str(clip.get("type") or "") != "speech" or str(clip.get("segment_id") or "") != survivor_id:
            continue
        clip["source_start_ms"] = new_start
        clip["source_end_ms"] = new_end
        clip["duration_ms"] = max(0, new_end - new_start)
        ov = dict(overrides.get(survivor_id) or {})
        ov["start_ms"] = new_start
        ov["end_ms"] = new_end
        overrides[survivor_id] = ov
        break
    ov_drop = dict(overrides.get(drop_id) or {})
    ov_drop["excluded"] = True
    ov_drop["exclude_reason"] = reason
    overrides[drop_id] = ov_drop
    excluded.add(drop_id)
    exclude_reasons[drop_id] = reason
    return [
        c
        for c in clips
        if not (
            str(c.get("type") or "") == "speech" and str(c.get("segment_id") or "") == drop_id
        )
    ]


def _omit_speech_clip(
    clips: list[dict[str, Any]],
    overrides: dict[str, Any],
    excluded: set[str],
    exclude_reasons: dict[str, str],
    sid: str,
    *,
    reason: str,
) -> list[dict[str, Any]]:
    key = str(sid or "").strip()
    if not key:
        return clips
    ov = dict(overrides.get(key) or {})
    ov["excluded"] = True
    ov["exclude_reason"] = reason
    overrides[key] = ov
    excluded.add(key)
    exclude_reasons[key] = reason
    return [
        c
        for c in clips
        if not (str(c.get("type") or "") == "speech" and str(c.get("segment_id") or "") == key)
    ]



def _merge_plan_preserves_source(
    plan: dict[str, Any],
    clips: list[dict[str, Any]],
    sid: str,
) -> bool:
    """True when the fused survivor range still covers the retired clip's audio."""
    drop_id = str(plan.get("drop_segment_id") or "")
    if not drop_id:
        return False
    idx = _speech_clip_index(clips, drop_id)
    if idx < 0:
        return False
    dropped = clips[idx]
    try:
        d_start = int(dropped.get("source_start_ms") or 0)
        d_end = int(dropped.get("source_end_ms") or d_start)
        new_start = int(plan.get("new_start_ms") or 0)
        new_end = int(plan.get("new_end_ms") or 0)
    except (TypeError, ValueError):
        return False
    return new_start <= d_start and new_end >= d_end and new_end > new_start


def _fuse_or_omit_hanging_clip(
    ctx: RunContext,
    *,
    clips: list[dict[str, Any]],
    finding: dict[str, Any],
    overrides: dict[str, Any],
    excluded: set[str],
    exclude_reasons: dict[str, str],
    segs: dict[str, Any],
    selection: dict[str, Any],
    hard_keeps: set[str],
    applied: list[dict[str, Any]],
    reason_suffix: str = "noop_recut",
) -> tuple[list[dict[str, Any]], bool]:
    """Fuse a hanging clip into an EDL neighbour, else omit it (F5 2C ladder)."""
    f = finding
    sid = str(f.get("segment_id") or "")
    # The keep list from the start of the pass goes stale as this pass retires
    # tape under carriers: ask the removal authority against the in-progress
    # state before any omit (ISSUES 74).
    hard_keeps = hard_keeps | _live_protected(ctx, overrides, selection, excluded)
    idx = _speech_clip_index(clips, sid)
    changed = False
    fused = False
    if sid and idx >= 0:
        hanging = clips[idx]
        src_start = int(hanging.get("source_start_ms") or 0)
        src_end = int(hanging.get("source_end_ms") or src_start)
        seg = segs.get(sid) or {}
        plan = _merge_candidate_for_clip(
            clips=clips,
            index=idx,
            sid=sid,
            src_start=src_start,
            src_end=src_end,
            speaker=_speaker_of(seg if isinstance(seg, dict) else None),
            chapter=_chapter_id_for(sid, selection),
            selection=selection,
            segs=segs,
            gap_max_ms=24_000,
            allow_cross_speaker=True,
            # A chapter_bleed_incomplete sits on the chapter boundary itself: the
            # only neighbour that can complete the thought is in the next chapter.
            # Allow it when the two clips are source-adjacent (the boundary was
            # simply placed mid-thought) — exec_11871 seg_014 → seg_015 (50 ms).
            allow_cross_chapter_gap_ms=2_000,
        )
        if plan and sid in hard_keeps and str(plan.get("drop_segment_id") or "") == sid:
            neighbor = str(plan.get("survivor_segment_id") or "")
            if neighbor and neighbor not in hard_keeps:
                plan = {
                    **plan,
                    "drop_segment_id": neighbor,
                    "survivor_segment_id": sid,
                }
        # A fuse is a *union*: the survivor's source range grows to cover both
        # clips, so the retired id loses no audio. exec_11871 had 60/61 clips on
        # the hard-keep list, so refusing the union stranded seg_014's
        # unrecoverable chapter_bleed_incomplete forever (mix ⇄ junction spin).
        # Only the omit branch below still honours hard keeps.
        if plan and str(plan.get("drop_segment_id") or "") in hard_keeps:
            if not _merge_plan_preserves_source(plan, clips, sid):
                plan = None
        if plan:
            reason = f"junction_snip_qa:{f.get('kind') or 'on_a_roll'}:fuse_{reason_suffix}"
            clips = _apply_merge_plan(
                clips,
                overrides,
                excluded,
                exclude_reasons,
                plan,
                reason=reason,
            )
            applied.append(
                {
                    **f,
                    "status": "fused_neighbor",
                    "survivor_segment_id": plan.get("survivor_segment_id"),
                    "drop_segment_id": plan.get("drop_segment_id"),
                }
            )
            changed = True
            fused = True
    if not fused:
        speech_ids = [
            str(c.get("segment_id") or "")
            for c in clips
            if isinstance(c, dict) and c.get("type") == "speech"
        ]
        if sid and sid not in hard_keeps and idx >= 0 and speech_ids != [sid]:
            reason = f"junction_snip_qa:{f.get('kind') or 'on_a_roll'}:omit_{reason_suffix}"
            clips = _omit_speech_clip(
                clips,
                overrides,
                excluded,
                exclude_reasons,
                sid,
                reason=reason,
            )
            applied.append({**f, "status": "omitted_no_neighbor"})
            changed = True
        elif sid and sid in hard_keeps and idx >= 0:
            # No recut, no fuse, and omit is refused for a hard keep: the only
            # legal outcome is to keep the clip as is. Record that decision on
            # the NLE override so detection stops re-raising it (exec_055
            # seg_058, ISSUES entry 72). Not a severity soften: a durable,
            # logged decision tied to this clip.
            ov = dict(overrides.get(sid) or {})
            ov["accepted_hanging_end"] = "hard_keep_no_recut_no_fuse"
            overrides[sid] = ov
            applied.append({**f, "status": "accepted_hard_keep_hang"})
            changed = True
        else:
            applied.append({**f, "status": "skipped_no_recommendation"})
    return clips, changed


def apply_junction_repairs(
    ctx: RunContext,
    edl: dict[str, Any],
    findings: list[dict[str, Any]],
    *,
    cfg: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], list[dict[str, Any]], bool]:
    """Apply deterministic repairs to EDL + NLE + placement adjustments.

    Returns (edl, applied_rows, needs_remaster).
    """
    conf = cfg or junction_snip_cfg()
    if not findings:
        return edl, [], False
    if not bool(conf.get("apply_repairs", True)):
        return edl, [], False

    clips = [dict(c) for c in (edl.get("clips") or []) if isinstance(c, dict)]
    nle = load_nle(ctx)
    overrides = dict(nle.get("segment_overrides") or {})
    nudge_history = dict(
        nle.get("junction_nudge_history")
        if isinstance(nle.get("junction_nudge_history"), dict)
        else {}
    )
    applied: list[dict[str, Any]] = []
    excluded: set[str] = set()
    exclude_reasons: dict[str, str] = {}
    changed = False
    hard_keeps: set[str] = set()
    try:
        from interview_mux.hard_keep import hard_keep_segment_ids

        hard_keeps = set(hard_keep_segment_ids(ctx) or [])
    except Exception:
        hard_keeps = set()
    try:
        segs = _segments_by_id(ctx)
    except Exception:
        segs = {}
    if not isinstance(segs, dict):
        segs = {}
    selection: dict[str, Any] = {}
    if ctx.artifact_exists("master/selection.json"):
        try:
            raw_sel = ctx.read_json("master/selection.json")
            if isinstance(raw_sel, dict):
                selection = raw_sel
        except Exception:
            selection = {}
    # Process excludes first
    for f in findings:
        if f.get("action") != "exclude_micro":
            continue
        sid = str(f.get("segment_id") or "")
        if not sid or sid in excluded:
            continue
        if sid in hard_keeps or sid in _live_protected(ctx, overrides, selection, excluded):
            applied.append({**f, "status": "refused_hard_keep"})
            continue
        kind = str(f.get("kind") or "exclude_micro")
        reason = f"junction_snip_qa:{kind}"
        ov = dict(overrides.get(sid) or {})
        ov["excluded"] = True
        ov["exclude_reason"] = reason
        overrides[sid] = ov
        excluded.add(sid)
        exclude_reasons[sid] = reason
        # Remove speech clip from EDL
        clips = [
            c
            for c in clips
            if not (
                str(c.get("type") or "") == "speech" and str(c.get("segment_id") or "") == sid
            )
        ]
        applied.append({**f, "status": "applied"})
        changed = True

    # Same-speaker micro merge (absorb drop into survivor bounds)
    for f in findings:
        if f.get("action") != "merge_micro":
            continue
        detail = f.get("detail") if isinstance(f.get("detail"), dict) else {}
        drop_id = str(detail.get("drop_segment_id") or "")
        survivor_id = str(detail.get("survivor_segment_id") or "")
        if not drop_id or not survivor_id or drop_id in excluded:
            continue
        new_start = detail.get("new_start_ms")
        new_end = detail.get("new_end_ms")
        if new_start is None or new_end is None:
            applied.append({**f, "status": "skipped_no_recommendation"})
            continue
        for c in clips:
            if str(c.get("type") or "") != "speech" or str(c.get("segment_id") or "") != survivor_id:
                continue
            c["source_start_ms"] = int(new_start)
            c["source_end_ms"] = int(new_end)
            ov = dict(overrides.get(survivor_id) or {})
            ov["start_ms"] = int(new_start)
            ov["end_ms"] = int(new_end)
            overrides[survivor_id] = ov
            break
        reason = f"junction_snip_qa:{f.get('kind') or 'merge_micro'}"
        ov_drop = dict(overrides.get(drop_id) or {})
        ov_drop["excluded"] = True
        ov_drop["exclude_reason"] = reason
        overrides[drop_id] = ov_drop
        excluded.add(drop_id)
        exclude_reasons[drop_id] = reason
        clips = [
            c
            for c in clips
            if not (
                str(c.get("type") or "") == "speech" and str(c.get("segment_id") or "") == drop_id
            )
        ]
        applied.append({**f, "status": "applied", "survivor_segment_id": survivor_id})
        changed = True

    # Surgical thought-complete recut (hanging end → transcript/LLM cut, leftover independent)
    from interview_mux.thought_complete_recut import ACTION as _THOUGHT_COMPLETE, apply_thought_complete_to_clips

    for f in findings:
        if f.get("action") != _THOUGHT_COMPLETE:
            continue
        clips, overrides, did = apply_thought_complete_to_clips(
            clips,
            f,
            overrides=overrides,
            excluded=excluded,
            exclude_reasons=exclude_reasons,
        )
        if did:
            sid_tc = str(f.get("segment_id") or "")
            landed_end = None
            for c in clips:
                if (
                    isinstance(c, dict)
                    and str(c.get("type") or "") == "speech"
                    and str(c.get("segment_id") or "") == sid_tc
                ):
                    landed_end = int(c.get("source_end_ms") or 0)
                    break
            if landed_end is None:
                landed_end = (f.get("detail") or {}).get("keep_end_ms")
            applied.append(
                {
                    **f,
                    "status": "applied",
                    "keep_end_ms": landed_end,
                    "applied_ms": landed_end,
                }
            )
            changed = True
        else:
            # F5 2C: recut noop → fuse into an EDL neighbor; else omit the hang.
            clips, changed_noop = _fuse_or_omit_hanging_clip(
                ctx,
                clips=clips,
                finding=f,
                overrides=overrides,
                excluded=excluded,
                exclude_reasons=exclude_reasons,
                segs=segs,
                selection=selection,
                hard_keeps=hard_keeps,
                applied=applied,
            )
            changed = changed or changed_noop

    # Bound nudges first, then incomplete extend/cut so mid-word snaps cannot
    # overwrite a cut that landed on the last complete phrase (exec_11130).
    bound_findings = [
        f
        for f in findings
        if str(f.get("action") or "") in {"nudge_source_bounds", "extend_later", "cut_earlier"}
    ]
    bound_findings.sort(
        key=lambda f: 0 if str(f.get("action") or "") == "nudge_source_bounds" else 1
    )
    for f in bound_findings:
        action = str(f.get("action") or "")
        sid = str(f.get("segment_id") or "")
        if not sid or sid in excluded:
            continue
        detail = f.get("detail") if isinstance(f.get("detail"), dict) else {}
        recommended = detail.get("recommended_ms")
        if recommended is None:
            applied.append({**f, "status": "skipped_no_recommendation"})
            continue
        rec = int(recommended)
        edge = str(detail.get("edge") or ("end" if action != "nudge_source_bounds" else "end"))
        if action == "nudge_source_bounds":
            edge = str(detail.get("edge") or "end")
        else:
            edge = "end"

        # Respect intentional NLE trims — mid-word nudge must not reopen "Well,"/"It".
        if action == "nudge_source_bounds" and edge == "end":
            ov_lock = overrides.get(sid) if isinstance(overrides.get(sid), dict) else {}
            if "end_ms" in ov_lock and abs(rec - int(ov_lock["end_ms"])) >= 20:
                applied.append(
                    {
                        **f,
                        "status": "skipped_nle_trim_locked",
                        "locked_end_ms": int(ov_lock["end_ms"]),
                    }
                )
                continue

        # C5: cosmetic mid_word end writebacks after assembly need INTENT_EDL_EDGE allow.
        kind = str(f.get("kind") or "")
        if kind in {"mid_word_end", "mid_word_start"} and action == "nudge_source_bounds":
            try:
                asm = ctx.final_path("master", "assembly.wav")
                if asm.is_file() and asm.stat().st_size > 0:
                    from interview_mux.timeline_reopen_meta_gate import (
                        INTENT_EDL_EDGE,
                        decide_timeline_reopen,
                    )

                    gate = decide_timeline_reopen(
                        ctx,
                        intent=INTENT_EDL_EDGE,
                        detail={
                            "cosmetic_mid_word": True,
                            "segment_id": sid,
                            "edge": edge,
                            "kind": kind,
                        },
                    )
                    if not gate.get("allow"):
                        applied.append(
                            {
                                **f,
                                "status": "skipped_cosmetic_mid_word",
                                "gate": gate,
                            }
                        )
                        continue
            except Exception:
                applied.append(
                    {
                        **f,
                        "status": "skipped_cosmetic_mid_word_fail_closed",
                    }
                )
                continue

        target_idx = f.get("clip_index")
        try:
            target_idx_i = int(target_idx) if target_idx is not None else None
        except (TypeError, ValueError):
            target_idx_i = None

        matched_clip = False
        # Earlier fuses/omits in this same apply pass shift clip positions, so a
        # stamped clip_index can point past the clip (exec_055 seg_031,
        # skipped_clip_index_mismatch). When the id is unique among speech
        # clips the index is not needed to disambiguate.
        same_id = sum(
            1
            for c in clips
            if str(c.get("type") or "") == "speech" and str(c.get("segment_id") or "") == sid
        )
        if same_id == 1:
            target_idx_i = None
        for i, c in enumerate(clips):
            if str(c.get("type") or "") != "speech" or str(c.get("segment_id") or "") != sid:
                continue
            # Ideal-cut / multi-appearance segments share segment_id across clips —
            # always honor clip_index when the detector stamped one.
            if target_idx_i is not None and i != target_idx_i:
                continue
            matched_clip = True
            ss = int(c.get("source_start_ms") or 0)
            se = int(c.get("source_end_ms") or ss)
            phrase_max = int(conf.get("phrase_extend_max_ms") or 30_000)
            if edge == "start":
                # Allow small retreat for continuum; don't cross end
                new_ss = max(0, min(rec, se - 300))
                try:
                    from interview_mux.media_ip_cta import never_touch_source_intervals

                    nt_intervals = never_touch_source_intervals(ctx)
                    if _start_inside_never_touch(new_ss, nt_intervals):
                        applied.append(
                            {**f, "status": "skipped_never_touch_shoulder", "edge": edge}
                        )
                        continue
                except Exception:
                    pass
                if abs(new_ss - ss) < 20:
                    applied.append({**f, "status": "skipped_noop_bound", "edge": edge})
                    continue
                c["source_start_ms"] = new_ss
                ov = dict(overrides.get(sid) or {})
                ov["start_ms"] = new_ss
                if "end_ms" not in ov:
                    ov["end_ms"] = se
                overrides[sid] = ov
            else:
                # Allow extend beyond prior end up to phrase_max from original,
                # but never invade the next selected speech source range.
                new_se = max(ss + 300, rec)
                # Cap wild extends
                if new_se > se + phrase_max:
                    new_se = se + phrase_max
                if new_se < se - phrase_max:
                    new_se = max(ss + 300, se - phrase_max)
                next_start = _next_speech_source_start(clips, i)
                new_se = _clamp_end_before_next_speech(new_se, next_start)
                try:
                    from interview_mux.media_ip_cta import (
                        clamp_source_away_from_never_touch,
                        never_touch_source_intervals,
                    )

                    _nt_ss, new_se, _nt_notes = clamp_source_away_from_never_touch(
                        ss, new_se, never_touch_source_intervals(ctx)
                    )
                except Exception:
                    _nt_notes = []
                # Never-touch partial extends (e.g. 97920→97970 onto the NT
                # shoulder) re-open incomplete "Well," after cut_earlier landed
                # on the last complete phrase (forensics exec_11130).
                if _nt_notes and action == "extend_later":
                    applied.append(
                        {
                            **f,
                            "status": "skipped_never_touch_extend",
                            "edge": edge,
                            "notes": list(_nt_notes)[:4],
                        }
                    )
                    continue
                if new_se <= ss + 300:
                    status = (
                        "skipped_never_touch_clamp"
                        if _nt_notes
                        else "skipped_next_clip_clamp"
                    )
                    applied.append({**f, "status": status, "edge": edge})
                    continue
                if abs(new_se - se) < 20:
                    # Extend often no-ops under never-touch; cut_earlier for the
                    # same segment still applies when edge keys no longer collapse.
                    status = (
                        "skipped_never_touch_noop"
                        if _nt_notes
                        else "skipped_noop_bound"
                    )
                    applied.append({**f, "status": status, "edge": edge})
                    continue
                c["source_end_ms"] = new_se
                ov = dict(overrides.get(sid) or {})
                if "start_ms" not in ov:
                    ov["start_ms"] = ss
                ov["end_ms"] = new_se
                overrides[sid] = ov
            # Commit the *actual* written bound, not the uncapped recommendation.
            written = (
                int(c.get("source_start_ms") or 0)
                if edge == "start"
                else int(c.get("source_end_ms") or 0)
            )
            nudge_history[f"{sid}:{edge}:{i}"] = {
                "applied_ms": written,
                "kind": f.get("kind"),
                "updated_at": _now(),
            }
            applied.append(
                {
                    **f,
                    "status": "applied",
                    "applied_ms": written,
                    "edge": edge,
                    "clip_index": i,
                }
            )
            changed = True
            break
        else:
            if target_idx_i is not None and not matched_clip:
                applied.append({**f, "status": "skipped_clip_index_mismatch"})

    # exec_11871: a critical incomplete cut flagged `unrecoverable_within_clip`
    # whose in-clip repair could not land (clamped to the neighbour / no room)
    # must escalate to the same fuse-then-omit ladder the noop recut uses —
    # otherwise mix refuses forever on a residual nobody can clear.
    _resolved_ok = {"applied", "fused_neighbor", "omitted_no_neighbor"}
    for f in findings:
        if str(f.get("severity") or "") != "critical":
            continue
        if str(f.get("kind") or "") not in _INCOMPLETE_CUT_KINDS:
            continue
        detail_f = f.get("detail") if isinstance(f.get("detail"), dict) else {}
        if not detail_f.get("unrecoverable_within_clip"):
            continue
        sid = str(f.get("segment_id") or "")
        if not sid:
            continue
        statuses = {
            str(a.get("status") or "")
            for a in applied
            if isinstance(a, dict)
            and str(a.get("segment_id") or "") == sid
            and str(a.get("kind") or "") == str(f.get("kind") or "")
        }
        if statuses & _resolved_ok:
            continue
        clips, changed_esc = _fuse_or_omit_hanging_clip(
            ctx,
            clips=clips,
            finding=f,
            overrides=overrides,
            excluded=excluded,
            exclude_reasons=exclude_reasons,
            segs=segs,
            selection=selection,
            hard_keeps=hard_keeps,
            applied=applied,
        )
        changed = changed or changed_esc

    # Impact holds — insert after speech clip before next VO
    for f in findings:
        if f.get("action") != "insert_impact_hold":
            continue
        idx = f.get("clip_index")
        if idx is None:
            continue
        # Find current index by segment after prior mutations
        sid = str(f.get("segment_id") or "")
        insert_at = None
        for i, c in enumerate(clips):
            if str(c.get("type") or "") == "speech" and str(c.get("segment_id") or "") == sid:
                insert_at = i + 1
                break
        if insert_at is None:
            continue
        # Skip if hold already present
        if insert_at < len(clips):
            nxt = clips[insert_at]
            if str(nxt.get("type") or "") == "silence" and str(nxt.get("air_kind") or "") == "impact_hold":
                applied.append({**f, "status": "already_present"})
                continue
        detail = f.get("detail") if isinstance(f.get("detail"), dict) else {}
        hold_ms = int(detail.get("hold_ms") or conf.get("impact_hold_ms_min") or 1200)
        hold_ms = max(
            int(conf.get("impact_hold_ms_min") or 1200),
            min(int(conf.get("impact_hold_ms_max") or 3500), hold_ms),
        )
        clips.insert(
            insert_at,
            {
                "type": "silence",
                "air_kind": "impact_hold",
                "timeline_start_ms": 0,
                "duration_ms": hold_ms,
            },
        )
        applied.append({**f, "status": "applied", "hold_ms": hold_ms})
        changed = True

    # Clamp dead air
    for f in findings:
        if f.get("action") != "clamp_air":
            continue
        idx = f.get("clip_index")
        if idx is None or idx >= len(clips):
            continue
        # Re-find silence by scanning (indices may have shifted from holds)
        # Use original index best-effort on silence clips still over clamp
        detail = f.get("detail") if isinstance(f.get("detail"), dict) else {}
        rec = int(detail.get("recommended_ms") or conf.get("dead_air_clamp_ms") or 2500)
        for c in clips:
            if str(c.get("type") or "") != "silence":
                continue
            if str(c.get("air_kind") or "") in {
                "impact_hold",
                "opening_music",
                "chapter_music_bridge",
            }:
                continue
            dur = int(c.get("duration_ms") or 0)
            if dur > rec:
                c["duration_ms"] = rec
                changed = True
                applied.append({**f, "status": "applied", "clamped_to_ms": rec})
                break

    # Music fades → placement adjustments (only mark applied when mix would apply)
    from interview_mux.placement_qa import music_repair_would_apply

    for f in findings:
        if f.get("action") != "adjust_music_fade":
            continue
        detail = f.get("detail") if isinstance(f.get("detail"), dict) else {}
        aid = str(detail.get("asset_id") or "")
        if not aid:
            continue
        xf = int(detail.get("suggested_crossfade_ms") or conf.get("music_soft_crossfade_ms") or 180)
        adj_row = {
            "asset_id": aid,
            "action": "adjust_crossfade",
            "suggested_crossfade_ms": xf,
            "reason": "junction_snip_qa:music_hard_transition",
            "provenance": {
                "rule_id": "junction_snip_qa",
                "source_artifact": QA_REL,
                "detail": str(f.get("evidence") or ""),
            },
            "adaptive_level_source": "default",
        }
        # Persist first so music_repair_would_apply can see durable hint.
        _merge_placement_adjustments(ctx, [adj_row])
        if music_repair_would_apply(
            ctx, {**f, **adj_row, "detail": {**detail, "suggested_crossfade_ms": xf}}
        ):
            applied.append({**f, "status": "applied", "suggested_crossfade_ms": xf})
            changed = True
        else:
            applied.append(
                {
                    **f,
                    "status": "detect_only",
                    "suggested_crossfade_ms": xf,
                    "reason": "music_repair_not_mix_applicable",
                }
            )

    # Strip orphan VO targeting excluded speech (e.g. vo_micro exclude left the
    # preceding vo_pickup on the timeline).
    if excluded:
        before_n = len(clips)
        clips = [
            c
            for c in clips
            if not (
                str(c.get("type") or "") == "vo_pickup"
                and str(c.get("targets_segment_id") or "") in excluded
            )
        ]
        if len(clips) < before_n:
            changed = True
            applied.append(
                {
                    "action": "strip_orphan_vo_pickup",
                    "excluded_segment_ids": sorted(excluded),
                    "removed_clips": before_n - len(clips),
                    "status": "applied",
                }
            )
        placements = [
            p
            for p in (edl.get("gap_placements") or [])
            if isinstance(p, dict)
            and str(p.get("targets_segment_id") or "") not in excluded
        ]
        if placements != list(edl.get("gap_placements") or []):
            edl = dict(edl)
            edl["gap_placements"] = placements
            changed = True
        if ctx.artifact_exists("understanding/gap_report.json"):
            try:
                from interview_mux.gap_framing import rebase_gap_lines_to_selection
                from interview_mux.write_staging import write_committed_json

                gr = ctx.read_json("understanding/gap_report.json")
                if isinstance(gr, dict):
                    from interview_mux.segment_id_remap import gap_report_remap_owner
                    ordered_live = [
                        str(s)
                        for s in (edl.get("ordered_segment_ids") or [])
                        if str(s) not in excluded
                    ]
                    if not ordered_live and ctx.artifact_exists("master/selection.json"):
                        sel = ctx.read_json("master/selection.json")
                        if isinstance(sel, dict):
                            ordered_live = [
                                str(s)
                                for s in (sel.get("ordered_segment_ids") or [])
                                if str(s) not in excluded
                            ]
                    rebased, notes = rebase_gap_lines_to_selection(gr, ordered_live)
                    if notes:
                        write_committed_json(
                            ctx,
                            "understanding/gap_report.json",
                            rebased,
                            stage_key=gap_report_remap_owner(gr),
                            mutation_class="segment_id_remap",
                        )
                        applied.append(
                            {
                                "action": "rebase_gap_report_after_exclude",
                                "notes": notes[:12],
                                "status": "applied",
                            }
                        )
                        changed = True
            except Exception as exc:
                applied.append(
                    {
                        "action": "rebase_gap_report_after_exclude",
                        "status": "failed",
                        "error": str(exc)[:160],
                    }
                )

    # Never-touch / media-IP CTA source tape must not bleed into speech clips.
    try:
        from interview_mux.media_ip_cta import clamp_edl_speech_away_from_never_touch

        clamped_edl, nt_rows = clamp_edl_speech_away_from_never_touch(
            ctx, {"clips": clips}
        )
        if nt_rows:
            clips = [dict(c) for c in (clamped_edl.get("clips") or []) if isinstance(c, dict)]
            changed = True
            applied.append(
                {
                    "action": "clamp_never_touch_cta_bleed",
                    "status": "applied",
                    "clips": nt_rows[:12],
                }
            )
    except Exception as exc:
        applied.append(
            {
                "action": "clamp_never_touch_cta_bleed",
                "status": "failed",
                "error": str(exc)[:160],
            }
        )

    if overrides != (nle.get("segment_overrides") or {}) or nudge_history != (
        nle.get("junction_nudge_history") or {}
    ):
        nle = dict(nle)
        nle["segment_overrides"] = overrides
        nle["junction_nudge_history"] = nudge_history
        # Mark junction provenance without forcing structural cascade
        nle["junction_snip_qa"] = {"updated_at": _now(), "override_count": len(overrides)}
        save_nle(ctx, nle)
        changed = True

    timeline = _recompute_timeline(clips)
    new_edl = dict(edl)
    new_edl["clips"] = clips
    new_edl["timeline_duration_ms"] = timeline
    new_edl["silence_clip_count"] = sum(1 for c in clips if str(c.get("type") or "") == "silence")
    # Drop excluded from ordered list if present — always persist EDL when order
    # changes even if clip/override mutations did not set ``changed`` (otherwise
    # selection is updated and EDL on disk drifts).
    if excluded:
        ordered = [s for s in (new_edl.get("ordered_segment_ids") or []) if str(s) not in excluded]
        if ordered != list(new_edl.get("ordered_segment_ids") or []):
            changed = True
        # Selection is authority: bump lock on exclude, then copy onto EDL.
        _exclude_from_selection(ctx, excluded, reasons=exclude_reasons)
        new_edl["ordered_segment_ids"] = ordered
        from interview_mux.order_hash import copy_order_lock, stamp_order_hash

        if ctx.artifact_exists("master/selection.json"):
            sel = ctx.read_json("master/selection.json")
            if isinstance(sel, dict):
                new_edl = copy_order_lock(sel, stamp_order_hash(new_edl))
            else:
                new_edl = stamp_order_hash(new_edl)
        else:
            new_edl = stamp_order_hash(new_edl)
        changed = True

    if changed:
        from interview_mux.order_hash import copy_order_lock, stamp_order_hash
        from interview_mux.write_staging import write_committed_json

        # Persist bound repairs immediately — StageInfo does not claim edl/selection,
        # so a normal flush would delete them and leave commitment diverged.
        new_edl = stamp_order_hash(new_edl)
        if ctx.artifact_exists("master/selection.json"):
            sel = ctx.read_json("master/selection.json")
            if isinstance(sel, dict):
                # Selection leads: copy lock onto EDL; never rewrite selection from EDL.
                new_edl = copy_order_lock(sel, new_edl)
                from interview_mux.order_hash import assert_selection_leads_edl

                try:
                    assert_selection_leads_edl(sel, new_edl)
                except ValueError as exc:
                    ctx.log(
                        f"junction repair EDL/selection divergence (selection leads): {exc}",
                        level="warning",
                        stage=STAGE_ID,
                    )
        from interview_mux.air_order import write_live_edl

        write_live_edl(ctx, new_edl, source=STAGE_ID)

    return new_edl, applied, changed


def _live_protected(
    ctx: RunContext,
    overrides: dict[str, Any],
    selection: dict[str, Any],
    excluded: set[str],
) -> set[str]:
    """Must-air ids under the in-progress NLE overrides and air order."""
    try:
        from interview_mux.removal_authority import protected_segment_ids

        on_air = [
            str(s)
            for s in ((selection or {}).get("ordered_segment_ids") or [])
            if s and str(s) not in excluded
        ]
        return protected_segment_ids(ctx, overrides=overrides, on_air=on_air or None)
    except Exception:
        return set()


def _exclude_from_selection(
    ctx: RunContext,
    excluded: set[str],
    *,
    reasons: dict[str, str] | None = None,
) -> None:
    if not excluded or not ctx.artifact_exists("master/selection.json"):
        return
    try:
        from interview_mux.hard_keep import hard_keep_segment_ids

        keeps = hard_keep_segment_ids(ctx)
        blocked = excluded & keeps
        if blocked:
            ctx.log(
                "junction refuse exclude of hard-keep: " + ", ".join(sorted(blocked)[:8]),
                level="warning",
                stage="junction_snip_qa",
            )
            excluded = {s for s in excluded if s not in keeps}
        if not excluded:
            return
    except Exception:
        pass
    sel = ctx.read_json("master/selection.json")
    if not isinstance(sel, dict):
        return
    from interview_mux.order_hash import bump_order_lock
    from interview_mux.air_order_boundary import commit_selection_mutation

    ordered = [s for s in (sel.get("ordered_segment_ids") or []) if str(s) not in excluded]
    sel = dict(sel)
    sel["ordered_segment_ids"] = ordered
    excl_list = list(sel.get("excluded_segment_ids") or [])
    existing = {
        (e if isinstance(e, str) else str((e or {}).get("segment_id") or ""))
        for e in excl_list
    }
    for sid in excluded:
        if sid in existing:
            continue
        reason = (reasons or {}).get(sid) or "junction_snip_qa:exclude_micro"
        excl_list.append({"segment_id": sid, "reason": reason})
    sel["excluded_segment_ids"] = excl_list
    commit_selection_mutation(
        ctx,
        bump_order_lock(sel, source="junction_snip_qa:exclude"),
        producer="junction_snip_qa",
        stage_key=STAGE_ID,
        checkpoint_mode="detect",
        write_committed=True,
    )


def _merge_placement_adjustments(ctx: RunContext, rows: list[dict[str, Any]]) -> None:
    from interview_mux.placement_qa import OUTPUT_PATH, load_placement_adjustments
    from interview_mux.write_staging import write_committed_json

    doc = load_placement_adjustments(ctx)
    existing = [r for r in (doc.get("adjustments") or []) if isinstance(r, dict)]
    by_asset = {str(r.get("asset_id")): r for r in existing if r.get("asset_id")}
    for row in rows:
        aid = str(row.get("asset_id") or "")
        if not aid:
            continue
        prev = by_asset.get(aid, {})
        by_asset[aid] = {**prev, **row}
        # S4: never mirror fades into SDP — mix/QA apply placement_adjustments at read.
    out = {"version": int(doc.get("version") or 1), "adjustments": list(by_asset.values())}
    write_committed_json(ctx, OUTPUT_PATH, out, stage_key=STAGE_ID)


def _patch_sdp_cue_crossfade(ctx: RunContext, asset_id: str, crossfade_ms: int) -> None:
    """S4 no-op: music fades live only in ``placement_adjustments`` (never rewrite SDP)."""
    ctx.log(
        f"junction S4: skip SDP crossfade patch asset={asset_id} "
        f"xf={int(crossfade_ms)} — placement_adjustments only",
        level="info",
        stage=STAGE_ID,
    )
    return


def _sync_edl_speech_bounds_from_nle(
    ctx: RunContext, edl: dict[str, Any]
) -> tuple[dict[str, Any], bool]:
    """Re-seat speech clip bounds from NLE trim overrides.

    Junction repairs write both EDL clips and ``segment_overrides``. Remaster /
    mix paths can reload an older EDL while NLE keeps the cut — audible
    assembly then still ends on hanging ``Well,`` / ``It`` (exec_11130).
    """
    try:
        nle = load_nle(ctx)
    except Exception:
        return edl, False
    overrides = nle.get("segment_overrides") if isinstance(nle, dict) else None
    if not isinstance(overrides, dict) or not overrides:
        return edl, False
    clips = [dict(c) for c in (edl.get("clips") or []) if isinstance(c, dict)]
    changed = False
    for clip in clips:
        if str(clip.get("type") or "") != "speech":
            continue
        sid = str(clip.get("segment_id") or "")
        if not sid:
            continue
        ov = overrides.get(sid)
        if not isinstance(ov, dict) or ov.get("excluded"):
            continue
        ss = int(clip.get("source_start_ms") or 0)
        se = int(clip.get("source_end_ms") or ss)
        if "start_ms" in ov:
            new_ss = int(ov["start_ms"])
            if abs(new_ss - ss) >= 20:
                clip["source_start_ms"] = new_ss
                ss = new_ss
                changed = True
        if "end_ms" in ov:
            new_se = int(ov["end_ms"])
            if abs(new_se - se) >= 20 and new_se > ss + 300:
                clip["source_end_ms"] = new_se
                changed = True
    if not changed:
        return edl, False
    out = dict(edl)
    out["clips"] = clips
    out["timeline_duration_ms"] = _recompute_timeline(clips)
    return out, True


def commitment_remaster_needed(ctx: RunContext) -> bool:
    """ENDD-1: True when commitment remaster must run (missing/unseated/mtime skew)."""
    asm_path = ctx.final_path("master", "assembly.wav")
    edl_path = ctx.final_path("master", "edl.json")
    if not asm_path.is_file():
        return True
    mtime_skew = False
    if edl_path.is_file():
        try:
            mtime_skew = asm_path.stat().st_mtime_ns < edl_path.stat().st_mtime_ns
        except OSError:
            mtime_skew = True
    try:
        from interview_mux.air_order import mix_outputs_seated

        unseated = not mix_outputs_seated(ctx)
    except Exception:
        unseated = True
    return unseated or mtime_skew


def remaster_mix_only(ctx: RunContext) -> None:
    """Rebuild mix from current EDL (and placement adjustments) without wiping EDL.

    End-D: all content ``write_live_edl`` happens **before** nested ``run_mix``.
    After render/promote, only mtime polish + seat verification — no EDL rewrite.
    """
    from interview_mux.assembly_ledger import write_assembly_ledger
    from interview_mux.mix_junction_seat import remaster_session
    from interview_mux.stages import assembly
    from interview_mux.transition_vo import (
        commit_current_transition_wavs,
        restamp_edl_transition_source_paths,
    )
    from interview_mux.write_staging import promote_staged_side_effects

    with remaster_session(ctx, owner="junction"):
        refuse_mix_if_live_incomplete_cuts(ctx)
        marker = ctx.final_path(".stage_done", "mix")
        if marker.is_file():
            try:
                marker.unlink()
            except OSError:
                pass
        try:
            commit_current_transition_wavs(ctx)
            restamp_edl_transition_source_paths(ctx)
        except Exception as exc:
            ctx.log(
                f"junction remaster VO resync: {exc}",
                level="warning",
                stage=STAGE_ID,
            )
        # --- Pre-mix EDL content rewrites only (ENDD-4) ---
        if ctx.artifact_exists("master/edl.json"):
            try:
                edl_sync = ctx.read_json("master/edl.json")
                if isinstance(edl_sync, dict):
                    edl_sync, synced = _sync_edl_speech_bounds_from_nle(ctx, edl_sync)
                    if synced:
                        from interview_mux.air_order import write_live_edl

                        write_live_edl(ctx, edl_sync, source=STAGE_ID)
                        ctx.log(
                            "junction remaster: re-seated EDL bounds from NLE overrides",
                            level="info",
                            stage=STAGE_ID,
                        )
            except Exception as sync_exc:
                ctx.log(
                    f"junction remaster NLE bound sync: {sync_exc}",
                    level="warning",
                    stage=STAGE_ID,
                )
        ledger = write_assembly_ledger(ctx)
        if not ledger.get("complete", True):
            clips = []
            if ctx.artifact_exists("master/edl.json"):
                edl_now = ctx.read_json("master/edl.json")
                if isinstance(edl_now, dict):
                    clips = [
                        c for c in (edl_now.get("clips") or []) if isinstance(c, dict)
                    ]
            has_speech = any(str(c.get("type") or "") == "speech" for c in clips)
            # S1: never nested run_edl — speechless / naked ledger is refuse, not rebuild.
            if not has_speech:
                raise RuntimeError(
                    "junction remaster: no speech clips on live EDL — refuse nested "
                    f"run_edl (naked_seam_count={ledger.get('naked_seam_count')})"
                )
            raise RuntimeError(
                f"junction remaster left {ledger.get('naked_seam_count')} naked seam(s)"
            )
        if ctx.artifact_exists("master/edl.json"):
            edl_now = ctx.read_json("master/edl.json")
            if isinstance(edl_now, dict):
                from interview_mux.listenability_guards import remediate_listenability_edl

                edl_now, notes = remediate_listenability_edl(ctx, edl_now)
                if notes:
                    from interview_mux.edl_narrative_qc import validate_flow1_edl_narrative

                    if validate_flow1_edl_narrative(ctx, edl_now):
                        pass
                    else:
                        from interview_mux.air_order import write_live_edl

                        write_live_edl(ctx, edl_now, source="junction_snip_qa")
        # Remaster must run under mix write-staging — otherwise assembly/ledger land
        # in junction pending and are dropped (exec_13167: newer uncommitted pending
        # + hollow mark_done after remaster).
        from interview_mux.write_staging import run_nested_staged_stage

        run_nested_staged_stage(ctx, "mix", lambda: assembly.run_mix(ctx))
        from interview_mux.seam_autopsy import write_render_ledger

        write_render_ledger(ctx)
        # EDL/assembly are owned by edl/mix for invalidation — promote as side effects
        # so junction flush does not delete the remastered render.
        # Mix QC artifacts (coverage / listen_critic / bed presence) are written while
        # junction staging is active; StageInfo does not claim them, so flush would
        # drop them and PMQ then fails planned_music_preserved / episode_close_outro.
        promote_staged_side_effects(
            ctx,
            REMASTER_MIX_SIDE_EFFECTS,
            stage_id=STAGE_ID,
        )
        # ENDD-4: no content write_live_edl after render. Mtime polish only.
        from interview_mux.air_order import ensure_assembly_mtime_seats_edl

        ensure_assembly_mtime_seats_edl(ctx)


def _budgeted_remaster_mix(ctx: RunContext, *, path: str = "repair") -> tuple[bool, int]:
    """Gate every remaster_mix_only through gen budget + sticky oscillation halt.

    Returns ``(remastered, used_count)``. On refuse, hard-pins classified
    budget/osc exhaust (no ``needs_operator`` hang; no soft residuals for
    naked/critical paths — terminal raise_loud_failure refuses).

    End-D: ``path=commitment`` always reseats assembly — bypasses low_gain and
    remaster budget/oscillation. Cosmetic/feel paths stay gated.
    """
    from interview_mux.thrash_hardening import (
        junction_budget_exhaust_hard_pin,
        junction_remaster_budget_ok,
        note_junction_remaster,
    )

    path_l = str(path or "").lower()
    # "commitment" reseats assembly to the live EDL — never refuse low_gain
    # or budget/osc (forensics: hollow junction_done + mix unseated after write_live_edl).
    is_commitment = "commitment" in path_l
    critical = is_commitment or any(
        x in path_l
        for x in (
            "incomplete_clause",
            "on_a_roll",
            "critical",
            "naked",
        )
    )

    # Pillar C gain gate for non-critical remasters
    try:
        from interview_mux.timeline_reopen_meta_gate import (
            INTENT_JUNCTION,
            decide_timeline_reopen,
        )

        if not critical:
            gate = decide_timeline_reopen(
                ctx,
                intent=INTENT_JUNCTION,
                detail={"path": path, "incomplete_kinds": [path]},
            )
            if not gate.get("allow"):
                ctx.log(
                    f"junction_snip_qa: remaster refused low_gain ({path})",
                    level="info",
                    stage=STAGE_ID,
                )
                return False, 0
    except Exception:
        # End-D: commitment must not fail-closed on gate import/errors.
        if not critical:
            return False, 0

    ok_budget, used = junction_remaster_budget_ok(ctx)
    if not ok_budget and not is_commitment:
        try:
            from interview_mux.mix_junction_seat import abandon_remaster

            abandon_remaster(ctx, reason=f"budget_exhaust:{path}")
        except Exception:
            pass
        pin = junction_budget_exhaust_hard_pin(ctx)
        ctx.log(
            f"junction_snip_qa: remaster refused ({path}) used={used} pin={pin}",
            level="warning",
            stage=STAGE_ID,
        )
        return False, used
    if not ok_budget and is_commitment:
        ctx.log(
            f"junction_snip_qa: commitment remaster bypasses budget/osc used={used}",
            level="warning",
            stage=STAGE_ID,
        )
    # ENDD-5: defer (do not steal) while music_epoch remaster is owed.
    if is_commitment:
        try:
            from interview_mux.mix_junction_seat import (
                MusicEpochOwnsRemaster,
                junction_remaster_blocked_by_music_epoch,
            )

            if junction_remaster_blocked_by_music_epoch(ctx):
                ctx.log(
                    "junction_snip_qa: commitment remaster deferred (music_epoch owed)",
                    level="info",
                    stage=STAGE_ID,
                )
                return False, used
        except Exception:
            pass
    try:
        remaster_mix_only(ctx)
    except Exception as rem_exc:
        try:
            from interview_mux.mix_junction_seat import MusicEpochOwnsRemaster

            if isinstance(rem_exc, MusicEpochOwnsRemaster):
                ctx.log(
                    "junction_snip_qa: commitment remaster deferred (music_epoch owns)",
                    level="info",
                    stage=STAGE_ID,
                )
                return False, used
        except Exception:
            pass
        raise
    note_junction_remaster(ctx)
    # ENDD-2: commitment success requires mix_outputs_seated — never hollow True.
    if is_commitment:
        try:
            from interview_mux.air_order import mix_outputs_seated

            if not mix_outputs_seated(ctx):
                ctx.log(
                    "junction_snip_qa: commitment remaster left mix unseated — refuse",
                    level="error",
                    stage=STAGE_ID,
                )
                return False, used + 1
        except Exception:
            return False, used + 1
    return True, used + 1


def _set_g_listen_pending_after_remaster(ctx: RunContext) -> None:
    """HX-5: re-arm ``g_listen_pending`` after remaster (warn, block, block_mix).

    Skipped / cleared / ``refused_low_gain`` still do not re-arm. Full-auto
    driver skip/clear after re-arm so unattended ``block_mix`` does not deadlock.
    Optimizer remaster shares this helper. ``check_g_listen_pending`` (critic-alone)
    is unchanged.
    """
    try:
        from interview_mux.config import merged_config

        mode = str(
            ((merged_config().get("sound_design") or {}).get("g_listen_mode") or "warn")
        ).lower()
        _ = mode
        # C14: do not re-arm G-Listen after a refused_low_gain remutate decision.
        try:
            if ctx.artifact_exists("mastering/listen_delight_remutate.json"):
                rem = ctx.read_json("mastering/listen_delight_remutate.json")
                if isinstance(rem, dict) and str(rem.get("status") or "") == "refused_low_gain":
                    return
        except Exception:
            pass
        if not ctx.artifact_exists("master/listen_critic.json"):
            return
        critic = ctx.read_json("master/listen_critic.json")
        if isinstance(critic, dict) and critic.get("g_listen_recommended"):

            def _glisten(m: dict) -> None:
                # Operator already continued/skipped for this run — do not re-arm
                # (remaster thrash + e2e clear loops otherwise fight forever).
                if m.get("g_listen_skipped") or m.get("g_listen_cleared"):
                    return
                m["g_listen_pending"] = True
                if critic.get("quality_score") is not None:
                    m["g_listen_quality_score"] = critic.get("quality_score")

            ctx.mutate_run_meta(_glisten)
    except Exception as exc:
        ctx.log(
            f"junction remaster: could not refresh g_listen pending ({exc})",
            level="warning",
            stage=STAGE_ID,
        )


def build_feel_audit_context(
    ctx: RunContext,
    snip_report: dict[str, Any],
) -> dict[str, Any]:
    """Bounded context for the single feel-audit LLM call."""
    edl = ctx.read_json("master/edl.json") if ctx.artifact_exists("master/edl.json") else {}
    clips = [c for c in (edl.get("clips") or []) if isinstance(c, dict)] if isinstance(edl, dict) else []
    seams: list[dict[str, Any]] = []
    words = _transcript_words(ctx)
    segs = _segments_by_id(ctx)
    for i, clip in enumerate(clips[:80]):
        ctype = str(clip.get("type") or "")
        row: dict[str, Any] = {
            "i": i,
            "type": ctype,
            "timeline_start_ms": clip.get("timeline_start_ms"),
            "duration_ms": clip.get("duration_ms"),
        }
        if ctype == "speech":
            sid = str(clip.get("segment_id") or "")
            row["segment_id"] = sid
            seg = segs.get(sid) or {}
            text = str(seg.get("text") or "")[:160]
            row["text_head"] = text[:80]
            row["text_tail"] = text[-80:] if len(text) > 80 else text
            row["source_start_ms"] = clip.get("source_start_ms")
            row["source_end_ms"] = clip.get("source_end_ms")
            end_ms = int(clip.get("source_end_ms") or 0)
            row["end_window"] = _text_in_window(words, max(0, end_ms - 2500), end_ms)[-120:]
        elif ctype == "silence":
            row["air_kind"] = clip.get("air_kind")
        elif ctype == "vo_pickup":
            row["line_id"] = clip.get("line_id")
            row["targets_segment_id"] = clip.get("targets_segment_id")
        seams.append(row)

    residuals = [
        f
        for f in (snip_report.get("findings") or [])
        if isinstance(f, dict) and f.get("status") not in {"applied", "already_present"}
    ][:40]
    applied = [a for a in (snip_report.get("applied") or []) if isinstance(a, dict)][:40]
    return {
        "version": 1,
        "seam_sample": seams,
        "deterministic_applied": applied,
        "deterministic_residuals": residuals,
        "timeline_duration_ms": edl.get("timeline_duration_ms") if isinstance(edl, dict) else None,
        "allowed_actions": sorted(ALLOWED_FEEL_ACTIONS),
        "llm_budget": "at_most_two_calls_primary_plus_retry",
    }


def run_junction_feel_audit(
    ctx: RunContext,
    snip_report: dict[str, Any],
    *,
    cfg: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """One LLM call judging final master feel. Retry once on schema/unavailable."""
    conf = cfg or junction_snip_cfg()
    from interview_mux.seam_autopsy import _canonical_hash

    edl_doc = (
        ctx.read_json("master/edl.json")
        if ctx.artifact_exists("master/edl.json")
        else {}
    )
    edl_hash = _canonical_hash(edl_doc if isinstance(edl_doc, dict) else {})
    if ctx.artifact_exists(FEEL_REL):
        try:
            prior = ctx.read_json(FEEL_REL)
        except Exception:
            prior = None
        if isinstance(prior, dict):
            prior_hash = str(prior.get("edl_hash") or "")
            prior_verdict = str(prior.get("verdict") or "")
            if (
                prior_hash
                and prior_hash == edl_hash
                and prior_verdict
                and prior_verdict not in {"", "unavailable"}
            ):
                return prior
    if not bool(conf.get("feel_audit_enabled", True)):
        audit = {
            "version": 1,
            "skipped": True,
            "reason": "feel_audit_disabled",
            "directives": [],
            "findings": [],
            "llm_calls": 0,
            "edl_hash": edl_hash,
            "generated_at": _now(),
        }
        ctx.write_json(FEEL_REL, audit)
        return audit

    # Re-entrant: the feel audit usually runs nested inside run_junction_snip_qa,
    # which already owns the inner-ladder flag. Remember the prior state so the
    # nested exit does not strip the outer ladder's flag (exec_11871).
    _inner_flag_prior = bool(getattr(ctx, "_junction_snip_qa_inner", False))
    setattr(ctx, "_junction_snip_qa_inner", True)
    packet = build_feel_audit_context(ctx, snip_report)
    directives: list[dict[str, Any]] = []
    findings: list[dict[str, Any]] = []
    verdict = "unavailable"
    llm_calls = 0
    error: str | None = None
    allowed = {"pass", "soft_pass", "fail", "remux_suggested", "unavailable"}

    def _parse_feel_payload(envelope: Any) -> tuple[str, list[dict[str, Any]], list[dict[str, Any]], str | None]:
        local_directives: list[dict[str, Any]] = []
        local_findings: list[dict[str, Any]] = []
        local_error: str | None = None
        local_verdict = "unavailable"
        raw = envelope.get("parsed") if isinstance(envelope, dict) else None
        if not isinstance(raw, dict):
            raw = envelope.get("artifacts") if isinstance(envelope, dict) else None
            if isinstance(raw, dict) and "junction_feel_audit" in raw:
                raw = raw["junction_feel_audit"]
            elif isinstance(envelope, dict) and "verdict" in envelope:
                raw = envelope
        if isinstance(raw, dict):
            local_verdict = str(raw.get("verdict") or "unavailable")
            for d in raw.get("directives") or []:
                if not isinstance(d, dict):
                    continue
                action = str(d.get("action") or "")
                if action not in ALLOWED_FEEL_ACTIONS:
                    continue
                local_directives.append(d)
            local_findings = [f for f in (raw.get("findings") or []) if isinstance(f, dict)]
        else:
            content = ""
            if isinstance(envelope, dict):
                content = str(envelope.get("content") or envelope.get("text") or "")
            if content.strip().startswith("{"):
                try:
                    parsed = json.loads(content)
                    if isinstance(parsed, dict):
                        local_verdict = str(parsed.get("verdict") or "unavailable")
                        for d in parsed.get("directives") or []:
                            if isinstance(d, dict) and str(d.get("action") or "") in ALLOWED_FEEL_ACTIONS:
                                local_directives.append(d)
                        local_findings = [
                            f for f in (parsed.get("findings") or []) if isinstance(f, dict)
                        ]
                except json.JSONDecodeError:
                    local_error = "feel_audit_unparseable"
            else:
                local_error = "feel_audit_empty_payload"
        if local_verdict not in allowed:
            local_verdict = "unavailable"
        return local_verdict, local_directives, local_findings, local_error

    from interview_mux.homunculus.budget import LimitExhausted, mark_identity_exhausted
    from interview_mux.stages.llm_runner import run_prompt_envelope
    from interview_mux.v2.config import v2_cfg

    user_content = json.dumps(packet, indent=2, ensure_ascii=False)
    # SYN-RETRY-01: LLM volley budget = v2.llm_max_attempts (default 2).
    # Escalate tier within that budget (standard → flagship). Remaster rounds
    # and listen_delight remutate MAX_ATTEMPTS are separate non-volley budgets.
    max_attempts = max(1, int(v2_cfg().get("llm_max_attempts", 2)))
    if max_attempts == 1:
        tiers: tuple[str, ...] = ("standard",)
    else:
        tiers = tuple(["standard"] * (max_attempts - 1) + ["flagship"])
    for attempt in range(1, max_attempts + 1):
        try:
            envelope = run_prompt_envelope(
                FEEL_STAGE_KEY,
                FEEL_PROMPT,
                user_content,
                ctx=ctx,
                explicit_tier=tiers[min(attempt, len(tiers)) - 1],
                bump_tier=attempt > 1,
                record_stage_key=FEEL_STAGE_KEY,
            )
            llm_calls += 1
            verdict, directives, findings, error = _parse_feel_payload(envelope)
            if verdict != "unavailable" and not error:
                break
            if attempt < max_attempts:
                ctx.log(
                    f"junction_feel_audit retry after unavailable/schema issue "
                    f"(attempt {attempt}/{max_attempts})",
                    level="warn",
                    stage=STAGE_ID,
                )
                continue
        except LimitExhausted as exc:
            mark_identity_exhausted(ctx, FEEL_STAGE_KEY)
            error = str(exc)[:240]
            verdict = "unavailable"
            ctx.log(f"junction_feel_audit unavailable: {exc}", level="error", stage=STAGE_ID)
            break
        except Exception as exc:
            error = str(exc)[:240]
            verdict = "unavailable"
            ctx.log(f"junction_feel_audit unavailable: {exc}", level="error", stage=STAGE_ID)
            if attempt < max_attempts:
                continue
            break

    if error:
        verdict = "unavailable"

    audit = {
        "version": 1,
        "verdict": verdict,
        "findings": findings,
        "directives": directives,
        "llm_calls": llm_calls,
        "error": error,
        "remaster_round": 0,
        "edl_hash": edl_hash,
        "generated_at": _now(),
    }
    # Only clear when this call armed the flag — otherwise the outer junction
    # ladder loses its inner marker and its own commitment remaster refuses
    # itself on the very residuals it is repairing (exec_11871 spin).
    if not _inner_flag_prior and hasattr(ctx, "_junction_snip_qa_inner"):
        delattr(ctx, "_junction_snip_qa_inner")
    ctx.write_json(FEEL_REL, audit)
    return audit


def apply_feel_directives(
    ctx: RunContext,
    audit: dict[str, Any],
    *,
    cfg: dict[str, Any] | None = None,
) -> bool:
    """Map feel-audit directives onto the same repair machinery. Returns needs_remaster."""
    conf = cfg or junction_snip_cfg()
    directives = [d for d in (audit.get("directives") or []) if isinstance(d, dict)]
    if not directives:
        return False
    edl = ctx.read_json("master/edl.json") if ctx.artifact_exists("master/edl.json") else None
    if not isinstance(edl, dict):
        return False
    findings: list[dict[str, Any]] = []
    nle = load_nle(ctx)
    nudge_history = (
        nle.get("junction_nudge_history")
        if isinstance(nle.get("junction_nudge_history"), dict)
        else {}
    )
    for d in directives:
        action = str(d.get("action") or "")
        if action not in ALLOWED_FEEL_ACTIONS:
            continue
        detail = dict(d.get("detail") or {}) if isinstance(d.get("detail"), dict) else {}
        if action == "adjust_crossfade":
            action = "adjust_music_fade"
            if "suggested_crossfade_ms" not in detail:
                detail["suggested_crossfade_ms"] = int(conf.get("music_soft_crossfade_ms") or 180)
        if action == "merge_micro":
            action = "thought_complete_recut"
        severity = str(d.get("severity") or "warn")
        # Feel must not re-nudge edges already applied this stage unless critical.
        if action == "nudge_source_bounds" and severity != "critical":
            sid = str(d.get("segment_id") or "")
            edge = str(detail.get("edge") or "end")
            if sid and f"{sid}:{edge}" in nudge_history:
                continue
        findings.append(
            {
                "kind": f"feel_{action}",
                "severity": severity,
                "segment_id": d.get("segment_id"),
                "clip_index": d.get("clip_index"),
                "action": action if action != "retarget_vo_anchor" else "exclude_micro",
                "detail": detail,
                "evidence": str(d.get("evidence") or "feel_audit"),
            }
        )
    if not findings:
        return False
    from interview_mux.thought_complete_recut import enrich_thought_complete_findings

    findings, _ = enrich_thought_complete_findings(
        ctx, edl, findings, cfg=conf, allow_llm=False
    )
    _edl2, applied, changed = apply_junction_repairs(ctx, edl, findings, cfg=conf)
    audit = dict(audit)
    audit["applied_directives"] = applied
    audit["remaster_round"] = 1 if changed else 0
    ctx.write_json(FEEL_REL, audit)
    return changed


def _persist_terminal_autopsy(
    ctx: RunContext,
    *,
    report: dict[str, Any] | None = None,
    edl: dict[str, Any] | None = None,
) -> None:
    """Always leave a seam_autopsy whose commitment matches live assembly.wav.

    Mtime-only freshness is not enough: mix can rewrite assembly to a new size
    while an older autopsy file is later touched, which left seed_stage_complete
    false and thrashed junction↔finalize (forensics exec_10066).
    """
    from interview_mux.homunculus.agenda import (
        _junction_commitment_matches_assembly,
        _producer_older_than_assembly,
    )

    if (
        ctx.artifact_exists("master/seam_autopsy.json")
        and _junction_commitment_matches_assembly(ctx)
    ):
        # Commitment matches — bump mtime when the file still looks older than
        # assembly so hollow-done / seed_stage_complete stay stable.
        if _producer_older_than_assembly(ctx, "master", "seam_autopsy.json"):
            try:
                from interview_mux.seam_autopsy import refresh_autopsy_commitment

                refresh_autopsy_commitment(ctx)
            except Exception:
                try:
                    path = ctx.final_path("master", "seam_autopsy.json")
                    path.touch()
                except Exception:
                    pass
        return
    doc = edl
    if not isinstance(doc, dict) and ctx.artifact_exists("master/edl.json"):
        loaded = ctx.read_json("master/edl.json")
        doc = loaded if isinstance(loaded, dict) else {}
    if not isinstance(doc, dict):
        doc = {}
    try:
        from interview_mux.seam_autopsy import build_autopsy, write_autopsy

        write_autopsy(
            ctx,
            build_autopsy(ctx, phase="post_junction", snip_report=report or {}, edl=doc),
        )
    except Exception as exc:
        ctx.log(
            f"junction_snip_qa: terminal autopsy fallback ({exc})",
            level="warning",
            stage=STAGE_ID,
        )
        ctx.write_json(
            "master/seam_autopsy.json",
            {
                "version": 1,
                "generated_at": _now(),
                "phase": "post_junction",
                "commitment": {
                    "status": "pending",
                    "reasons": ["terminal_autopsy_fallback"],
                },
                "scores": {
                    "continuity": 0.0,
                    "finishability": 0.0,
                    "sonic_density_fit": 0.0,
                    "information_clarity": 0.0,
                    "music_completeness": 0.0,
                },
                "seams": [],
                "blocking_reasons": ["terminal_autopsy_fallback"],
            },
            skip_handoff=True,
            stage_key=STAGE_ID,
        )


def run_junction_snip_qa(ctx: RunContext) -> None:
    """Delivery stage: deterministic junction QA → remaster → one feel audit → optional remaster."""
    conf = junction_snip_cfg()
    mode = str(conf.get("mode") or "advisory").lower()
    if mode == "off":
        report = {
            "version": 1,
            "mode": "off",
            "skipped": True,
            "findings": [],
            "applied": [],
            "remaster_rounds": 0,
            "llm_calls": 0,
            "generated_at": _now(),
        }
        ctx.write_json(QA_REL, report)
        return

    if not ctx.artifact_exists("master/edl.json"):
        ctx.log("junction_snip_qa: no master/edl.json — skip", level="warning", stage=STAGE_ID)
        ctx.write_json(
            QA_REL,
            {
                "version": 1,
                "mode": mode,
                "skipped": True,
                "reason": "missing_edl",
                "findings": [],
                "applied": [],
                "remaster_rounds": 0,
                "llm_calls": 0,
                "generated_at": _now(),
            },
        )
        _persist_terminal_autopsy(ctx, report={"skipped": True, "reason": "missing_edl"})
        return

    from interview_mux.air_order import assert_consumer

    assert_consumer(ctx, STAGE_ID)
    setattr(ctx, "_junction_snip_qa_inner", True)
    edl = ctx.read_json("master/edl.json")
    if not isinstance(edl, dict):
        raise ValueError("master/edl.json is not an object")

    findings = detect_junction_findings(ctx, edl, cfg=conf)
    from interview_mux.thought_complete_recut import enrich_thought_complete_findings

    thought_llm_calls = 0
    findings, thought_llm_calls = enrich_thought_complete_findings(
        ctx, edl, findings, cfg=conf, allow_llm=False
    )
    remaster_rounds = 0
    applied: list[dict[str, Any]] = []
    max_rounds = max(1, int(conf.get("max_remaster_rounds") or 8))
    residual_findings = list(findings)

    # Bounded repair runs: batch repairs, remaster only for critical incomplete cuts,
    # then rescan. Observational repairs land on EDL/NLE; commitment remaster seats audio.
    remediation_runs: list[dict[str, Any]] = []
    current_edl = edl
    prior_applied_sig: set[tuple[str, str, int]] | None = None
    _CRITICAL_REMASTER_KINDS = frozenset(
        {
            "naked_seam",
            "incomplete_clause",
            "on_a_roll",
            "chapter_bleed_incomplete",
        }
    )
    for run_index in range(1, max_rounds + 1):
        if not residual_findings:
            break
        from interview_mux.seam_autopsy import _canonical_hash

        pre_repair_hash = _canonical_hash(current_edl)
        next_edl, run_applied, needs = apply_junction_repairs(
            ctx, current_edl, residual_findings, cfg=conf
        )
        post_repair_hash = _canonical_hash(next_edl if isinstance(next_edl, dict) else {})
        applied.extend(run_applied)
        applied_sig = {
            (
                str(a.get("segment_id") or a.get("kind") or ""),
                str(a.get("action") or ""),
                int(round(int(a.get("applied_ms") or a.get("suggested_crossfade_ms") or 0) / 40.0) * 40),
            )
            for a in run_applied
            if isinstance(a, dict) and a.get("status") == "applied"
        }
        if prior_applied_sig is not None and applied_sig and applied_sig == prior_applied_sig:
            ctx.log(
                "junction_snip_qa: oscillating repair signature — halt remaster thrash",
                level="warning",
                stage=STAGE_ID,
            )
            try:
                from interview_mux.thrash_hardening import note_junction_oscillation_halt

                note_junction_oscillation_halt(ctx)
            except Exception:
                pass
            residual_findings = detect_junction_findings(ctx, current_edl, cfg=conf)
            residual_findings, extra_llm = enrich_thought_complete_findings(
                ctx, current_edl, residual_findings, cfg=conf, allow_llm=(run_index >= 2)
            )
            thought_llm_calls += extra_llm
            break
        prior_applied_sig = applied_sig
        needs_critical = any(
            isinstance(f, dict)
            and (
                str(f.get("severity") or "") == "critical"
                or str(f.get("kind") or "") in _CRITICAL_REMASTER_KINDS
            )
            for f in residual_findings
        )
        if needs and needs_critical:
            try:
                # S1: critical incomplete-cut remasters only (no cosmetic path=repair).
                remaster_path = "repair_incomplete_clause"
                remastered, used = _budgeted_remaster_mix(ctx, path=remaster_path)
                if not remastered:
                    residual_findings = detect_junction_findings(
                        ctx, current_edl, cfg=conf
                    )
                    naked_or_critical = any(
                        isinstance(f, dict)
                        and (
                            str(f.get("severity") or "") == "critical"
                            or str(f.get("kind") or "") in _CRITICAL_REMASTER_KINDS
                        )
                        for f in residual_findings
                    )
                    budget_open = True
                    try:
                        from interview_mux.thrash_hardening import (
                            junction_remaster_budget_ok,
                        )

                        budget_open, _n = junction_remaster_budget_ok(ctx)
                    except Exception:
                        budget_open = True
                    if naked_or_critical and budget_open and int(used or 0) == 0:
                        ctx.log(
                            "junction_snip_qa: critical remaster refused "
                            f"without budget burn (path={remaster_path}) — "
                            "retry commitment seating",
                            level="warning",
                            stage=STAGE_ID,
                        )
                        remastered, used = _budgeted_remaster_mix(
                            ctx, path="commitment_incomplete_clause"
                        )
                    if not remastered and naked_or_critical:
                        ctx.log(
                            "junction_snip_qa: remaster budget exhausted "
                            f"(used={used}) — classified refuse terminate "
                            "(no e2e soft-pass for naked/critical seams; "
                            "no needs_operator hang)",
                            level="error",
                            stage=STAGE_ID,
                        )
                        break
                    if not remastered:
                        ctx.log(
                            "junction_snip_qa: critical remaster refused "
                            f"(used={used}) — halt repair rounds",
                            level="warning",
                            stage=STAGE_ID,
                        )
                        break
            except Exception as exc:
                from interview_mux.loud_fail import raise_loud_failure

                _persist_terminal_autopsy(ctx)
                raise_loud_failure(
                    ctx,
                    f"Junction remediation run {run_index} could not remaster: {exc}",
                    stage=STAGE_ID,
                    reason="junction_remaster_failed",
                    detail={"run_index": run_index, "piece_count": len(residual_findings)},
                    cause=exc,
                )
            remaster_rounds += 1
            _set_g_listen_pending_after_remaster(ctx)
        elif needs and not needs_critical:
            ctx.log(
                "junction_snip_qa S1: observational repairs on EDL/NLE only — "
                "skip remaster until commitment",
                level="info",
                stage=STAGE_ID,
            )
        current_edl = (
            ctx.read_json("master/edl.json")
            if ctx.artifact_exists("master/edl.json")
            else next_edl
        )
        residual_findings = detect_junction_findings(ctx, current_edl, cfg=conf)
        residual_findings, extra_llm = enrich_thought_complete_findings(
            ctx, current_edl, residual_findings, cfg=conf, allow_llm=(run_index >= 2)
        )
        thought_llm_calls += extra_llm
        critical_residuals = [
            f for f in residual_findings if str(f.get("severity") or "") == "critical"
        ]
        pieces = [f for f in residual_findings if isinstance(f, dict)]
        pieces_resolved = max(0, len(run_applied) - len(critical_residuals))
        actions_executed = [
            str(a.get("action") or "")
            for a in run_applied
            if isinstance(a, dict) and a.get("status") == "applied"
        ]
        row = {
            "run_index": run_index,
            "pieces_targeted": len(pieces),
            "pieces_resolved": pieces_resolved,
            "actions_executed": actions_executed,
            "residual_after": len(residual_findings),
            "critical_residual_after": len(critical_residuals),
            "edl_hash_before": pre_repair_hash,
            "edl_hash_after": post_repair_hash,
            "edl_hash_unchanged": pre_repair_hash == post_repair_hash,
            "completed_at": _now(),
        }
        remediation_runs.append(row)
        if not critical_residuals:
            break

    report = {
        "version": 1,
        "mode": mode,
        "pace_class": _pace_class(ctx),
        "findings": findings,
        "applied": applied,
        "residual_findings": residual_findings,
        "remaster_rounds": remaster_rounds,
        "remediation_runs": remediation_runs,
        "llm_calls": thought_llm_calls,
        "advisory": mode != "authoritative",
        "blocking": mode == "authoritative",
        "generated_at": _now(),
    }
    ctx.write_json(QA_REL, report)
    # Honesty SSOT: drop stale applied claims before autopsy (no remaster).
    try:
        reconcile_junction_claim_inventory(ctx)
        if ctx.artifact_exists(QA_REL):
            refreshed = ctx.read_json(QA_REL)
            if isinstance(refreshed, dict):
                report = refreshed
                applied = list(report.get("applied") or applied)
    except Exception:
        pass

    audit = run_junction_feel_audit(ctx, report, cfg=conf)
    report["llm_calls"] = thought_llm_calls + int(audit.get("llm_calls") or 0)
    # S2: feel audit is advisory-only — never apply directives / remaster.
    report["feel_advisory_only"] = True
    ctx.write_json(QA_REL, report)

    if report["llm_calls"] > 6:
        ctx.log(
            f"junction_snip_qa feel-audit calls={report['llm_calls']} (escalation ladder exhausted)",
            level="warning",
            stage=STAGE_ID,
        )

    from interview_mux.seam_autopsy import build_autopsy, enrich_ledger, write_autopsy
    from interview_mux.order_hash import (
        assert_selection_leads_edl,
        copy_order_lock,
        order_hashes_match,
        stamp_order_hash,
    )
    from interview_mux.write_staging import write_committed_json

    # Final air-order lock: EDL must match selection. Never rewrite selection from EDL.
    if isinstance(current_edl, dict):
        current_edl = stamp_order_hash(current_edl)
        if ctx.artifact_exists("master/selection.json"):
            sel = ctx.read_json("master/selection.json")
            if isinstance(sel, dict) and not order_hashes_match(sel, current_edl):
                ctx.log(
                    "junction_snip_qa: EDL diverges from selection — aligning EDL to selection lock",
                    level="warning",
                    stage=STAGE_ID,
                )
                # Align EDL ordered ids to selection (selection leads).
                current_edl = dict(current_edl)
                # Ids the EDL omitted as unplayable stay omitted.
                from interview_mux.order_hash import seatable_selection_ids

                current_edl["ordered_segment_ids"] = seatable_selection_ids(sel, current_edl)
                current_edl = copy_order_lock(sel, stamp_order_hash(current_edl))
                try:
                    assert_selection_leads_edl(sel, current_edl)
                except ValueError as exc:
                    from interview_mux.loud_fail import raise_loud_failure

                    _persist_terminal_autopsy(ctx, edl=current_edl)
                    raise_loud_failure(
                        ctx,
                        str(exc),
                        stage=STAGE_ID,
                        reason="order_lock_selection_leads",
                        cause=exc,
                    )
            elif isinstance(sel, dict):
                current_edl = copy_order_lock(sel, current_edl)
        # Persist in-memory EDL only when disk copy differs (repairs / order stamp).
        disk_edl = (
            ctx.read_json("master/edl.json")
            if ctx.artifact_exists("master/edl.json")
            else None
        )
        if not isinstance(disk_edl, dict) or _canonical_edl_order(disk_edl) != _canonical_edl_order(
            current_edl
        ) or str(disk_edl.get("order_content_hash") or "") != str(
            current_edl.get("order_content_hash") or ""
        ):
            from interview_mux.air_order import write_live_edl

            write_live_edl(ctx, current_edl, source=STAGE_ID)

        # ENDD-1: remaster when assembly missing, gen/ledger unseated, or mtime skew.
        # ENDD-2: success requires mix_outputs_seated. ENDD-5: defer if music_epoch owed.
        try:
            from interview_mux.air_order import mix_outputs_seated
            from interview_mux.mix_junction_seat import (
                junction_remaster_blocked_by_music_epoch,
            )

            needs_remaster = commitment_remaster_needed(ctx)
            if needs_remaster:
                if junction_remaster_blocked_by_music_epoch(ctx):
                    report["commitment_remaster_deferred_music_epoch"] = True
                    ctx.write_json(QA_REL, report)
                    ctx.log(
                        "junction_snip_qa: commitment remaster deferred "
                        "(music_epoch / speech_first remaster owed)",
                        level="info",
                        stage=STAGE_ID,
                    )
                else:
                    ctx.log(
                        "junction_snip_qa: remastering mix so assembly matches "
                        "current EDL (commitment seat)",
                        level="info",
                        stage=STAGE_ID,
                    )
                    remastered, _used = _budgeted_remaster_mix(ctx, path="commitment")
                    seated_ok = False
                    try:
                        seated_ok = bool(remastered and mix_outputs_seated(ctx))
                    except Exception:
                        seated_ok = False
                    if seated_ok:
                        remaster_rounds += 1
                        report["remaster_rounds"] = remaster_rounds
                        ctx.write_json(QA_REL, report)
                        if ctx.artifact_exists("master/edl.json"):
                            loaded = ctx.read_json("master/edl.json")
                            if isinstance(loaded, dict):
                                current_edl = loaded
                    else:
                        report["commitment_remaster_refused"] = True
                        ctx.write_json(QA_REL, report)
                        from interview_mux.loud_fail import raise_loud_failure

                        _persist_terminal_autopsy(ctx, edl=current_edl)
                        raise_loud_failure(
                            ctx,
                            "Junction commitment remaster refused or left mix "
                            "unseated — refuse hollow junction_done",
                            stage=STAGE_ID,
                            reason="junction_commitment_remaster_refused",
                        )
        except Exception as exc:
            from interview_mux.loud_fail import LoudStageFailure, raise_loud_failure

            if isinstance(exc, LoudStageFailure):
                raise
            _persist_terminal_autopsy(ctx, edl=current_edl)
            raise_loud_failure(
                ctx,
                f"Junction could not remaster assembly for commitment: {exc}",
                stage=STAGE_ID,
                reason="junction_commitment_remaster_failed",
                cause=exc,
            )

    autopsy = build_autopsy(
        ctx,
        phase="post_junction",
        snip_report=report,
        edl=current_edl,
    )
    write_autopsy(ctx, autopsy)
    enrich_ledger(ctx, autopsy)
    commitment = autopsy.get("commitment") if isinstance(autopsy.get("commitment"), dict) else {}
    critical_left = [
        f for f in residual_findings if str(f.get("severity") or "") == "critical"
    ]
    blocking_reasons = list(commitment.get("reasons") or [])
    if str(commitment.get("status") or "") == "diverged":
        blocking_reasons.append("junction_commitment_diverged")
    from interview_mux.seam_autopsy import _canonical_hash

    live_edl_hash = _canonical_hash(current_edl if isinstance(current_edl, dict) else {})
    commit_edl_hash = str(commitment.get("edl_hash") or "")
    if critical_left and commit_edl_hash and live_edl_hash != commit_edl_hash:
        blocking_reasons.append("junction_edl_hash_mismatch")
    # S3: never severity-soften critical residuals. Live detect + incomplete kinds gate.
    # S5: no fuse/hitch producer-heal arming — loud refuse and let recovery route.
    commit_ok = str(commitment.get("status") or "") == "committed"
    if critical_left:
        blocking_reasons.append("critical_junction_residuals_after_two_runs")
    # F5 3A: hanging mid-thought clips always hard-block, including the
    # not-committed / advisory path that only stamped after_two_runs.
    incomplete_left = [
        f
        for f in critical_left
        if isinstance(f, dict) and str(f.get("kind") or "") in _INCOMPLETE_CUT_KINDS
    ]
    if incomplete_left and "critical_incomplete_cut_residuals" not in blocking_reasons:
        blocking_reasons.append("critical_incomplete_cut_residuals")
    # unavailable after retry is a blocking quality signal unless mechanical
    # commitment already passed with no critical residuals (LLM outage must not
    # discard a remastered assembly).
    if audit.get("verdict") == "unavailable":
        report["feel_audit_unavailable"] = True
        if not (commit_ok and not critical_left):
            blocking_reasons.append("junction_feel_audit_unavailable")
        else:
            ctx.log(
                "junction_feel_audit unavailable after committed remaster — not blocking",
                level="warning",
                stage=STAGE_ID,
            )
    # EM8: incomplete_clause / mid-cut residuals always hard-block even when
    # junction mode stays advisory (mode flag is observational elsewhere).
    enforce_block = mode == "authoritative"
    if "critical_incomplete_cut_residuals" in blocking_reasons:
        enforce_block = True
    # Residual judgements (incomplete cuts, residuals after the run budget) and
    # an unavailable feel audit (an LLM outage) are advisory (ISSUES 185): the
    # junction ladder already spent its budget and a rerun reaches the same
    # verdict. Only the structural seat stays hard.
    critical_blocking = {
        # ENDD-6: commitment / remaster / assembly seat stay hard under aspirational.
        "junction_commitment_diverged",
        "junction_commitment_remaster_refused",
        "junction_commitment_remaster_failed",
        "assembly_not_rendered_from_current_edl",
    }
    if report.get("commitment_remaster_refused"):
        blocking_reasons.append("junction_commitment_remaster_refused")
    if report.get("assembly_not_rendered_from_current_edl"):
        blocking_reasons.append("assembly_not_rendered_from_current_edl")
    try:
        from interview_mux.aspirational_quality import (
            is_aspirational_enabled,
            record_quality_advisories,
            register_quality_candidate,
        )

        if is_aspirational_enabled(ctx):
            non_critical = [
                r for r in blocking_reasons if r not in critical_blocking
            ]
            if non_critical:
                register_quality_candidate(ctx, family="junction")
                record_quality_advisories(
                    ctx,
                    gate_id="junction_snip_qa",
                    failed_checks=non_critical,
                    detail={"residual": len(residual_findings)},
                )
            blocking_reasons = [
                r for r in blocking_reasons if r in critical_blocking
            ]
            enforce_block = bool(blocking_reasons)
    except Exception:
        pass
    report["commitment"] = commitment
    report["blocking_reasons"] = sorted(set(blocking_reasons))
    # B-02 Wave 9: stamp both dialects so ship / PMQ / publishability share SSOT.
    report["critical_residual_count"] = len(critical_left)
    report["critical_residuals"] = len(critical_left)
    report["critical_count"] = len(critical_left)
    ctx.write_json(QA_REL, report)

    # Surface the authoritative result in run_meta.
    meta: dict[str, Any]
    if ctx.artifact_exists("run_meta.json"):
        doc = ctx.read_json("run_meta.json")
        meta = doc if isinstance(doc, dict) else {}
    else:
        meta = {}
    qc = meta.get("qc_summaries") if isinstance(meta.get("qc_summaries"), dict) else {}
    qc["junction_snip_qa"] = {
        "passed": not bool(blocking_reasons),
        "advisory": False,
        "blocking": bool(blocking_reasons) and enforce_block,
        "findings": len(findings),
        "applied": len(applied),
        "residual_findings": len(residual_findings),
        "critical_residuals": len(critical_left),
        "remaster_rounds": remaster_rounds,
        "llm_calls": report["llm_calls"],
        "feel_verdict": audit.get("verdict"),
        "commitment_status": commitment.get("status"),
        "blocking_reasons": sorted(set(blocking_reasons)),
    }
    qc["seam_autopsy"] = {
        "passed": not bool(autopsy.get("blocking_reasons")),
        "blocking": bool(autopsy.get("blocking_reasons")) and enforce_block,
        "commitment_status": commitment.get("status"),
        "continuity": (autopsy.get("scores") or {}).get("continuity"),
        "worst_seam_count": len(autopsy.get("worst_seam_ids") or []),
    }
    meta["qc_summaries"] = qc
    ctx.write_json("run_meta.json", meta)

    if hasattr(ctx, "_junction_snip_qa_inner"):
        delattr(ctx, "_junction_snip_qa_inner")

    if blocking_reasons and enforce_block:
        from interview_mux.loud_fail import raise_loud_failure

        raise_loud_failure(
            ctx,
            "Junction quality failed after the bounded remediation budget: "
            + ", ".join(sorted(set(blocking_reasons))),
            stage=STAGE_ID,
            reason="junction_quality_blocked",
            detail={
                "remediation_runs_used": len(remediation_runs),
                "critical_residuals": len(critical_left),
                "blocking_reasons": sorted(set(blocking_reasons)),
            },
        )

    try:
        from interview_mux.publishability_boundary import checkpoint_publishability

        checkpoint_publishability(ctx, checkpoint="post_junction")
    except Exception as exc:
        from interview_mux.publishability_boundary import PublishabilityBlocked

        if isinstance(exc, PublishabilityBlocked):
            raise
        ctx.log(
            f"junction_snip_qa: publishability checkpoint skipped: {exc}",
            level="warning",
            stage=STAGE_ID,
        )

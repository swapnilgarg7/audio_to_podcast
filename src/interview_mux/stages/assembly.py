from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any, Callable

from interview_mux.gates import check_edl_narrative_qc, check_edl_qc, check_narrative_qc
from interview_mux.operator_subprocess import run_command
from interview_mux.nle_state import (
    apply_nle_to_selection,
    load_nle,
    nle_has_operator_edits,
    segments_by_id_with_nle,
)
from interview_mux.operator_trace import logged_step
from interview_mux.prompt_validation import validate_edl
from interview_mux.run_context import RunContext


def _commit_edl_gap_report(ctx: RunContext, gap_report: dict) -> None:
    """Persist gap_report during EDL staging while the owner still allows writes.

    Under hard freeze the gap-line repairs stay in memory: EDL still builds from
    the repaired document, but disk authority stays with gap_report_sanitize
    (EDL is not a gap_report producer).
    """
    from interview_mux.write_staging import write_committed_json

    try:
        from interview_mux.artifact_ownership import write_permitted

        ok, _reason = write_permitted(
            ctx,
            "understanding/gap_report.json",
            "edl",
            role="producer",
            verb="persist",
        )
    except Exception:
        ok = False
    if not ok:
        ctx.log(
            "edl: gap_report frozen — keeping repairs in memory (no disk rewrite)",
            level="info",
            stage="edl",
        )
        return

    write_committed_json(ctx, "understanding/gap_report.json", gap_report, stage_key="edl")


# How far past a segment's end the EDL may reach to finish a hanging thought.
HANGING_END_EXTEND_MAX_MS = 30_000


def extend_hanging_end_to_thought(
    words: list[dict[str, Any]],
    speech_end_ms: int,
    *,
    next_keeper_start_ms: int | None = None,
    max_extend_ms: int = HANGING_END_EXTEND_MAX_MS,
) -> int | None:
    """End of the first complete thought after ``speech_end_ms``, or None.

    Used only when a kept segment has no complete-thought point inside it
    (boundary detection cut it mid-sentence), so trimming back cannot help.
    Never runs into the next on-air segment: when that segment starts on the
    tape inside the window, the horizon stops just before it (exec_052
    seg_060 ended "this is going to be", ISSUES entry 57).
    """
    from interview_mux.edl_narrative_qc import first_qc_hinge_between

    horizon = int(speech_end_ms) + int(max_extend_ms)
    if next_keeper_start_ms is not None:
        nxt = int(next_keeper_start_ms)
        # Next on-air segment starts at or before this end (tape-adjacent, as
        # seg_059 -> seg_060 on exec_052): any extension would overlap it.
        if nxt <= int(speech_end_ms) + 40:
            return None
        horizon = min(horizon, nxt - 40)
    if horizon <= speech_end_ms:
        return None
    # Same definition of "complete" as EDL QC: an extension QC would still
    # reject is no fix (exec_052 seg_060, ISSUES entry 59).
    return first_qc_hinge_between(words, int(speech_end_ms), horizon)


def nle_committed_on_disk(
    disk_order: list[str], nle_order: list[str], disk_selection: dict[str, Any]
) -> bool:
    """True when the NLE edits are already reflected in the disk selection.

    The order is what matters. ``nle_applied`` is only stamped by
    ``apply_nle_to_selection``; a later selection writer (ranking, CTA prune)
    that lands the same order without that path drops the stamp, and edl then
    refused forever although nothing was uncommitted (exec_052, ISSUES
    entry 56). A differing order still refuses.
    """
    if nle_order != disk_order:
        return False
    return True


def _selection_stands_over_nle_order(
    ctx: RunContext, disk_order: list[str], nle_order: list[str]
) -> bool:
    """Whether edl may build from the disk order although the NLE order differs.

    Only when the two orders hold the same segments (so nothing the NLE
    excludes or splits is missing from the selection) and the run is driven
    by the engine. The refusal tells an operator to land the timeline through
    the selection owner; an engine-driven run has no operator at the
    timeline, no stage that could do it, and the selection was committed by
    stages that ran after whatever wrote the NLE order, so the halt had no
    exit (exec_102 stopped here three times, second wind included). A manual
    run keeps the refusal.
    """
    if not disk_order or set(disk_order) != set(nle_order) or len(disk_order) != len(nle_order):
        return False
    try:
        from interview_mux.automation_run import (
            automation_driver_env_enabled,
            automation_driver_run,
        )

        meta = ctx.read_json("run_meta.json") if ctx.artifact_exists("run_meta.json") else {}
        driven = bool(automation_driver_env_enabled()) or bool(
            automation_driver_run(meta if isinstance(meta, dict) else {})
        )
    except Exception:
        driven = False
    if not driven:
        return False
    moved = sum(1 for a, b in zip(disk_order, nle_order) if a != b)
    ctx.log(
        "edl: the NLE sequence order differs from the committed selection "
        f"({moved} of {len(disk_order)} positions); the selection is the air order",
        level="warning",
        stage="edl",
        detail={
            "event": "selection_stands_over_nle_order",
            "disk_head": disk_order[:8],
            "nle_head": nle_order[:8],
        },
    )
    return True


def _segment_by_id(ctx: RunContext) -> dict[str, dict]:
    return segments_by_id_with_nle(ctx)


def _wav_duration_ms(path: Path) -> int:
    proc = run_command(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            str(path),
        ],
        label=f"ffprobe duration {path.name}",
        capture_output=True,
    )
    raw = (proc.stdout or "").strip()
    if not raw:
        raise RuntimeError(f"edl: unreadable VO duration: {path}")
    try:
        seconds = float(raw)
    except ValueError as exc:
        raise RuntimeError(f"edl: unreadable VO duration: {path}") from exc
    return max(0, int(seconds * 1000))


def resolve_vo_pickup_path(ctx: RunContext, line: dict) -> Path | None:
    """Resolve pickup WAV: matched → synthesized → clean → normalized → raw.

    Rejects forbidden backends and speech-QA failures so tone stubs never enter the EDL.
    """
    pickup = ctx.final_path("vo_pickup")
    matched = pickup / "matched"
    synthesized = pickup / "synthesized"
    clean = pickup / "clean"
    normalized = pickup / "normalized"
    lid = line.get("line_id", "")
    seg = line.get("targets_segment_id", "")
    bases: list[Path] = []
    for candidate in (matched, synthesized, clean, normalized, pickup):
        if candidate.is_dir():
            bases.append(candidate)
    if not bases:
        bases = [pickup]
    from interview_mux.vo_speech_qa import backend_allowed_for_vo, vo_passes_speech_qa
    from interview_mux.vo_synthesis_audit import (
        _audited_wav_path,
        synthesis_entry_for_line,
        synthesis_entry_matches_line,
    )

    for base in bases:
        for key in (lid, seg):
            if not key:
                continue
            candidate = base / f"{key}.wav"
            if not candidate.is_file():
                continue
            entry = synthesis_entry_for_line(ctx, str(lid or seg))
            if isinstance(entry, dict):
                backend = str(entry.get("backend") or "")
                if backend and not backend_allowed_for_vo(backend):
                    continue
                if entry.get("qc_pass") is False:
                    continue
                matches, _reason = synthesis_entry_matches_line(ctx, line)
                if not matches:
                    continue
                # Prefer the sha-bound take (seated copy may still match while
                # synthesized/ was overwritten mid re-synth).
                audited = _audited_wav_path(ctx, entry, line)
                if audited is None or not audited.is_file():
                    continue
                if not vo_passes_speech_qa(audited):
                    continue
                return audited
            else:
                # No audit row: only allow explicit operator *record* takes under
                # clean/normalized/top-level pickup. Synthesize delivery must never
                # seat unaudited audio (script rewrite would otherwise look G1-green).
                delivery = str(line.get("delivery") or "synthesize").strip().lower()
                if delivery != "record" or base.name not in {
                    "clean",
                    "normalized",
                    "vo_pickup",
                }:
                    continue
                if not vo_passes_speech_qa(candidate):
                    continue
                return candidate
    return None


def _gap_line_for_vo_clip(ctx: RunContext, clip: dict) -> dict:
    """Enrich a thin EDL vo_pickup clip with gap_report fields needed for resolve.

    EDL clips often carry only line_id / stale script_hash after sanitize strips a
    null source_path. resolve_vo_pickup_path matches synthesis audit against line
    text — prefer authoritative gap script so seated WAVs heal instead of hard-fail.
    """
    lid = str(clip.get("line_id") or "").strip()
    if not lid:
        return clip
    try:
        gap = ctx.read_json("understanding/gap_report.json") or {}
    except Exception:
        return clip
    for line in gap.get("interviewer_lines") or []:
        if not isinstance(line, dict):
            continue
        if str(line.get("line_id") or "").strip() != lid:
            continue
        merged = dict(clip)
        for key in (
            "text",
            "script_hash",
            "delivery",
            "targets_segment_id",
            "speaker_id",
            "context_hash",
            "required",
        ):
            if line.get(key) is not None:
                merged[key] = line[key]
        return merged
    return clip


def heal_vo_pickup_clip_source(ctx: RunContext, clip: dict) -> Path | None:
    """Stamp source_path (+ duration_ms) when a resolvable VO WAV exists."""
    if str(clip.get("type") or "") != "vo_pickup":
        return None
    if str(clip.get("source_path") or "").strip():
        return None
    line = _gap_line_for_vo_clip(ctx, clip)
    try:
        path = resolve_vo_pickup_path(ctx, line)
    except Exception:
        return None
    if path is None or not path.is_file():
        return None
    clip["source_path"] = vo_pickup_relpath(ctx, path)
    if int(clip.get("duration_ms") or 0) <= 0:
        try:
            clip["duration_ms"] = int(_wav_duration_ms(path) or 0)
        except Exception:
            pass
    return path


def vo_clip_wav_resolvable(ctx: RunContext, clip: dict) -> bool:
    """True when an unsourced EDL vo_pickup clip has a resolvable rendered WAV."""
    if str(clip.get("type") or "") != "vo_pickup":
        return False
    try:
        line = _gap_line_for_vo_clip(ctx, clip)
        path = resolve_vo_pickup_path(ctx, line)
    except Exception:
        return False
    return path is not None and path.is_file()


def restamp_edl_vo_pickup_source_paths(ctx: RunContext) -> list[str]:
    """Bind EDL ``vo_pickup`` clips to rendered WAVs (``edl`` owns live EDL).

    ``heal_vo_pickup_clip_source`` only ever ran inside ``run_preview``, but the
    assembly_preview *input check* refuses on exactly the unsourced clips that heal
    would fix. VO rendered after the EDL was built therefore never bound: preview
    could not start, nothing else re-bound, and mix silenced every host line
    (exec_11871 — vo_layup_seg_004/047/055 → "missing VO pickup WAV — inserted
    silence"). ``run_edl`` restamps after write (vo_synthesize S1 peel).
    """
    if not ctx.artifact_exists("master/edl.json"):
        return []
    try:
        edl = ctx.read_json("master/edl.json")
    except Exception:
        return []
    if not isinstance(edl, dict):
        return []
    healed: list[str] = []
    clips: list[object] = []
    retimed: list[str] = []
    for clip in edl.get("clips") or []:
        if not isinstance(clip, dict):
            clips.append(clip)
            continue
        row = dict(clip)
        path = heal_vo_pickup_clip_source(ctx, row)
        if path is None and str(row.get("type") or "") == "vo_pickup":
            # Already bound: keep duration honest against the rendered take so a
            # placeholder length can never survive into the timeline.
            src_rel = str(row.get("source_path") or "").strip()
            if src_rel:
                try:
                    bound = ctx.read_path(src_rel)
                except Exception:
                    bound = None
                if bound is not None and bound.is_file():
                    path = bound
        if path is not None:
            try:
                dur = int(_wav_duration_ms(path) or 0)
            except Exception:
                dur = 0
            if dur > 0 and int(row.get("duration_ms") or 0) != dur:
                row["duration_ms"] = dur
                retimed.append(str(row.get("line_id") or path.name))
            if not str(clip.get("source_path") or "").strip():
                healed.append(str(row.get("line_id") or path.name))
        clips.append(row)
    if not healed and not retimed:
        return []
    out = dict(edl)
    out["clips"] = clips
    # A bound VO clip is seconds long where the placeholder was 500 ms — without
    # re-timing, its tail overlaps the next speech head (exec_11871 EDL QC:
    # "Overlapping speech: vo_layup_seg_047 … overlaps seg_047").
    try:
        from interview_mux.edl_overlap_repair import _retime_clips

        total = _retime_clips(clips)
        if total > 0:
            out["timeline_duration_ms"] = int(total)
    except Exception as exc:
        ctx.log(
            f"vo_synthesize: VO bind could not re-time the timeline ({exc})",
            level="warning",
            stage="vo_synthesize",
        )
    from interview_mux.air_order import write_live_edl

    try:
        write_live_edl(ctx, out, source="vo_synthesize_bind")
    except Exception as exc:
        ctx.log(
            f"vo_synthesize: could not bind rendered VO into the EDL ({exc})",
            level="warning",
            stage="vo_synthesize",
        )
        return []
    if healed:
        ctx.log(
            "vo_synthesize: bound rendered VO WAV(s) into the EDL: "
            + ", ".join(healed[:12]),
            level="warning",
            stage="vo_synthesize",
        )
    if retimed:
        ctx.log(
            "vo_synthesize: re-timed VO clip length from the rendered take: "
            + ", ".join(sorted(set(retimed))[:12]),
            level="warning",
            stage="vo_synthesize",
        )
    return healed or sorted(set(retimed))


def vo_pickup_relpath(ctx: RunContext, path: Path) -> str:
    try:
        rel = path.relative_to(ctx.run_dir).as_posix()
    except ValueError:
        return path.as_posix()
    # Writes during a staged stage land under .pending_writes/<stage>/… — committed
    # EDL/mix consumers must see the post-flush run-relative path.
    prefix = ".pending_writes/"
    if rel.startswith(prefix):
        rest = rel[len(prefix) :]
        if "/" in rest:
            rel = rest.split("/", 1)[1]
    return rel


def _normalize_vo_text(text: str) -> str:
    return " ".join(str(text or "").strip().lower().split())


def _same_speaker_source_contiguous(
    prev: dict | None,
    curr: dict | None,
    *,
    gap_ms: int = 2500,
) -> bool:
    if not isinstance(prev, dict) or not isinstance(curr, dict):
        return False
    prev_spk = str(prev.get("speaker_id") or "").strip()
    curr_spk = str(curr.get("speaker_id") or "").strip()
    if not prev_spk or prev_spk != curr_spk:
        return False
    try:
        prev_end = int(prev.get("end_ms") or prev.get("source_end_ms") or 0)
        curr_start = int(curr.get("start_ms") or curr.get("source_start_ms") or 0)
    except (TypeError, ValueError):
        return False
    source_gap = curr_start - prev_end
    return -gap_ms <= source_gap <= gap_ms


def _gap_lines_for_segment(
    gap_report: dict | None,
    segment_id: str,
    placement: str,
    *,
    emitted_line_ids: set[str] | None = None,
    emitted_sentence_keys: set[str] | None = None,
) -> list[dict]:
    if not gap_report:
        return []
    out: list[dict] = []
    seen_ids = emitted_line_ids if emitted_line_ids is not None else set()
    seen_sentences = (
        emitted_sentence_keys if emitted_sentence_keys is not None else set()
    )
    from interview_mux.spoken_copy_guard import sentence_keys

    for line in gap_report.get("interviewer_lines") or []:
        if line.get("skipped_optional"):
            continue
        if line.get("targets_segment_id") != segment_id:
            continue
        if line.get("placement", "before") != placement:
            continue
        if line.get("delivery") != "record" and line.get("delivery") != "synthesize":
            continue
        lid = str(line.get("line_id") or "").strip()
        if lid and lid in seen_ids:
            continue
        line_sentence_keys = sentence_keys(str(line.get("text") or ""))
        # This is deliberately global, not scoped to a target or placement.
        # A repeated spoken sentence is unacceptable even if it was authored
        # for two different native segments — fail closed rather than silent-drop.
        # A repeated spoken sentence is a quality note: the line's audio is
        # already synthesized and dropping it would leave a required line
        # unseated. EDL narrative QC logs the collision (ISSUES 185).
        if lid:
            seen_ids.add(lid)
        seen_sentences.update(line_sentence_keys)
        out.append(line)
    return out


def _transition_after_segment(
    transitions: dict | None, after_segment_id: str, before_segment_id: str
) -> dict | None:
    if not transitions:
        return None
    for item in transitions.get("transitions") or []:
        if (
            item.get("after_segment_id") == after_segment_id
            and item.get("before_segment_id") == before_segment_id
        ):
            return item
    return None


def build_flow1_edl(
    *,
    selection: dict,
    segments_by_id: dict[str, dict],
    gap_report: dict | None = None,
    transitions: dict | None = None,
    resolve_vo_path: Callable[[dict], Path | None] | None = None,
    vo_relpath: Callable[[Path], str] | None = None,
    vo_duration_ms: Callable[[Path], int] | None = None,
    resolve_transition_path: Callable[[str, str], Path | None] | None = None,
    ideal_cuts: dict | list | None = None,
    transcript_words: list | None = None,
    max_keeper_ms: int | None = None,
    normalized_wav: Path | str | None = None,
    ctx: RunContext | None = None,
    verify_pair: Callable[[Path, Path], str | None] | None = None,
) -> dict:
    """Build Flow 1 EDL: speech order from selection, gap VO placements, transition anchors.

    When ``ideal_cuts`` / ``transcript_words`` are provided, speech clips use
    tightened air bounds (ideal window + complete-thought trim) instead of the
    full coarse segment slab. ``normalized_wav`` enables acoustic silence-valley
    micro-snaps at keeper edges.
    """
    from interview_mux.listenability_guards import air_pad_ms, listenability_guards_cfg
    from interview_mux.ideal_cuts import resolve_keeper_air_bounds

    ordered = list(selection.get("ordered_segment_ids") or [])
    clips: list[dict] = []
    gap_placements: list[dict] = []
    timeline_ms = 0
    missing_vo: list[str] = []
    missing_targets: list[str] = []
    missing_segments: list[str] = []
    omitted_unplayable: list[str] = []
    # Before-VO line ids whose target the builder omitted as unplayable. The
    # pickup is taken back off the timeline with its target (ISSUES 190).
    unseated_before_vo: list[str] = []
    missing_transitions: list[str] = []
    # Seams where a cloned voice abuts the same native speaker. Recorded, never
    # suppressed: the EDL seats every line the gap report airs (ISSUES 184).
    clone_adjacency_advisory: list[str] = []
    from interview_mux.clone_adjacency_verify import CloneAdjacencySession

    clone_adj = CloneAdjacencySession(ctx=ctx, verify_pair=verify_pair)
    air_cfg = listenability_guards_cfg()
    from interview_mux.gap_vo_prior_context import coerce_word_times

    words = coerce_word_times([w for w in (transcript_words or []) if isinstance(w, dict)])
    never_touch_intervals: list[tuple[int, int, str]] = []
    if ctx is not None:
        try:
            from interview_mux.media_ip_cta import never_touch_source_intervals

            never_touch_intervals = never_touch_source_intervals(ctx)
        except Exception:
            never_touch_intervals = []
    if max_keeper_ms is None:
        try:
            from interview_mux.ideal_cuts import ideal_cuts_cfg

            max_keeper_ms = int(ideal_cuts_cfg().get("max_cut_ms") or 180_000)
        except Exception:
            max_keeper_ms = 180_000

    def _append_air(
        kind: str,
        ref_dur: int,
        *,
        required_seam_hitch: bool = False,
        clone_adjacency_hitch: bool = False,
        preserve_planned_music: bool = False,
    ) -> None:
        nonlocal timeline_ms
        pad = air_pad_ms(ref_dur, kind=kind, cfg=air_cfg)
        if pad <= 0:
            return
        clip: dict = {
            "type": "silence",
            "air_kind": kind,
            "duration_ms": pad,
            "timeline_start_ms": timeline_ms,
        }
        if required_seam_hitch:
            clip["required_seam_hitch"] = True
            clip["preserve_planned_music"] = True
        if clone_adjacency_hitch:
            clip["clone_adjacency_hitch"] = True
            clip["preserve_planned_music"] = True
        if preserve_planned_music:
            clip["preserve_planned_music"] = True
        clips.append(clip)
        timeline_ms += pad

    def _same_answer_seam(after_sid: str, before_sid: str) -> bool:
        """True when adjacent keeps continue one answer — no chapter hitch air."""
        if not words:
            return False
        after = segments_by_id.get(after_sid) or {}
        before = segments_by_id.get(before_sid) or {}
        try:
            left_end = int(after.get("end_ms") or after.get("source_end_ms") or 0)
            right_start = int(before.get("start_ms") or before.get("source_start_ms") or 0)
        except (TypeError, ValueError):
            return False
        if left_end <= 0 or right_start <= 0:
            return False
        from interview_mux.gap_vo_prior_context import same_answer_continues

        return same_answer_continues(words, left_end, right_start)

    def _is_orientation(line: dict) -> bool:
        from interview_mux.opening_orientation import is_episode_orientation

        return is_episode_orientation(line)

    def _copy_hashes(line: dict) -> dict[str, str]:
        from interview_mux.spoken_copy_guard import (
            context_hash,
            evidence_for_line,
            script_hash,
        )

        return {
            "script_hash": script_hash(str(line.get("text") or "")),
            "context_hash": context_hash(evidence_for_line(line)),
        }

    def _append_opening_music_marker(ref_dur: int) -> None:
        nonlocal timeline_ms
        from interview_mux.opening_orientation import OPENING_MUSIC_AIR_KIND

        pad = air_pad_ms(ref_dur, kind="chapter_hinge", cfg=air_cfg)
        clips.append(
            {
                "type": "silence",
                "air_kind": OPENING_MUSIC_AIR_KIND,
                "duration_ms": max(0, pad),
                "timeline_start_ms": timeline_ms,
                "preserve_planned_music": True,
            }
        )
        timeline_ms += max(0, pad)

    def _append_chapter_music_bridge(*, after_segment_id: str = "", before_segment_id: str = "") -> None:
        """Reserve fixed 4s music-filled air (not generic chapter_hinge silence)."""
        nonlocal timeline_ms
        from interview_mux.chapter_music_bridge import (
            CHAPTER_MUSIC_BRIDGE_AIR_KIND,
            bridge_duration_ms,
            chapter_music_bridge_cfg,
        )

        if not chapter_music_bridge_cfg().get("chapter_music_bridge_enable", True):
            return
        # The kept tape pause and a chapter bridge are one seam. The bridge
        # wins, including when the voiceover file is longer than its estimate.
        if clips and str(clips[-1].get("air_kind") or "") == "kept_source_gap":
            dropped = clips.pop()
            timeline_ms = max(0, timeline_ms - int(dropped.get("duration_ms") or 0))
        # One bridge per seam — skip if we just placed one.
        if clips and str(clips[-1].get("air_kind") or "") == CHAPTER_MUSIC_BRIDGE_AIR_KIND:
            return
        pad = bridge_duration_ms()
        clip: dict = {
            "type": "silence",
            "air_kind": CHAPTER_MUSIC_BRIDGE_AIR_KIND,
            "duration_ms": pad,
            "timeline_start_ms": timeline_ms,
            "preserve_planned_music": True,
            "required_seam_hitch": True,
        }
        if after_segment_id:
            clip["after_segment_id"] = after_segment_id
        if before_segment_id:
            clip["before_segment_id"] = before_segment_id
        clips.append(clip)
        timeline_ms += pad

    if gap_report:
        for line in gap_report.get("interviewer_lines") or []:
            if line.get("skipped_optional"):
                continue
            if line.get("delivery") != "record":
                continue
            target = line.get("targets_segment_id", "")
            if target and target not in ordered:
                missing_targets.append(target)

    duration_fn = vo_duration_ms or _wav_duration_ms
    emitted_line_ids: set[str] = set()
    emitted_sentence_keys: set[str] = set()
    resolved_end_by_sid: dict[str, int] = {}

    def _raw_start(o: str) -> int | None:
        row = segments_by_id.get(o)
        if not isinstance(row, dict) or row.get("start_ms") is None:
            return None
        try:
            return int(row["start_ms"])
        except (TypeError, ValueError):
            return None

    # Keepers stay disjoint on the tape, not in air order. A reordered episode
    # airs a tape-later clip before a tape-earlier one; flooring the open at
    # the previously aired clip's end pushed seg_021 (27:48) past its own end
    # onto seg_026's head (32:13) and the EDL lost it (exec_025, ISSUES 178).
    def _tape_prev_end(sid: str, start: int) -> int | None:
        ends: list[int] = []
        for o in ordered:
            s0 = _raw_start(o) if o != sid else None
            if s0 is None or s0 >= start:
                continue
            if o in resolved_end_by_sid:
                ends.append(resolved_end_by_sid[o])
            else:
                try:
                    ends.append(int(segments_by_id[o].get("end_ms") or s0))
                except (TypeError, ValueError):
                    continue
        return max(ends) if ends else None

    def _tape_next_start(sid: str, start: int) -> int | None:
        later = [
            s0
            for o in ordered
            if o != sid and (s0 := _raw_start(o)) is not None and s0 > start
        ]
        return min(later) if later else None

    chapter_of: dict[str, str] = {}

    def _note_chapters(chapters: object) -> None:
        if not isinstance(chapters, list):
            return
        for ch in chapters:
            if not isinstance(ch, dict):
                continue
            cid = str(ch.get("chapter_id") or ch.get("id") or "").strip()
            if not cid:
                continue
            for raw_sid in ch.get("segment_ids") or []:
                key = str(raw_sid).strip()
                if key and key not in chapter_of:
                    chapter_of[key] = cid

    _note_chapters(selection.get("chapters"))
    if ctx is not None:
        try:
            from interview_mux.mastering_plan_loader import load_plan_raw

            plan = load_plan_raw(ctx) or {}
            _note_chapters(plan.get("chapters") if isinstance(plan, dict) else None)
        except Exception:
            pass

    def _chapter_id_of(row: dict | None) -> str:
        if not isinstance(row, dict):
            return ""
        own = str(row.get("chapter_id") or "").strip()
        if own:
            return own
        return chapter_of.get(str(row.get("segment_id") or "").strip(), "")

    def _same_story(earlier: dict | None, later: dict | None) -> bool:
        """Same chapter or the same talking point. A topic tag is too broad."""
        if not isinstance(earlier, dict) or not isinstance(later, dict):
            return False
        chapter_a = _chapter_id_of(earlier)
        chapter_b = _chapter_id_of(later)
        if chapter_a and chapter_a == chapter_b:
            return True
        point_a = str(earlier.get("talking_point_id") or "")
        point_b = str(later.get("talking_point_id") or "")
        return bool(point_a and point_a == point_b)

    def _keeps_neighbor_wall(later: dict | None) -> bool:
        """A question stays apart so the voiceover seam is not walked into."""
        if not isinstance(later, dict):
            return False
        if str(later.get("type") or "") in {
            "interviewer_question",
            "interviewer_prompt",
            "host_turn",
        }:
            return True
        from interview_mux.gap_vo_prior_context import _opens_as_ask

        return _opens_as_ask(str(later.get("text") or ""))

    def _last_non_silence_type() -> str:
        for clip in reversed(clips):
            ctype = str(clip.get("type") or "")
            if ctype and ctype != "silence":
                return ctype
        return ""

    from interview_mux.hosted_vo_authority import edl_before_line_survives

    for idx, sid in enumerate(ordered):
        seg = segments_by_id.get(sid)
        if not seg:
            missing_segments.append(sid)
            continue

        prev_sid = ordered[idx - 1] if idx > 0 else ""
        prev_seg = segments_by_id.get(prev_sid) if prev_sid else None
        # The opening grammar belongs to the first *seated* clip, not to air
        # index 0: an omitted first fragment must not take the opening with it.
        first_seat = not any(
            isinstance(c, dict) and c.get("type") == "speech" for c in clips
        )
        skip_before_vo = _same_speaker_source_contiguous(prev_seg, seg)
        nxt = ordered[idx + 1] if idx + 1 < len(ordered) else ""
        next_before_vo = bool(
            nxt
            and _gap_lines_for_segment(
                gap_report,
                nxt,
                "before",
                emitted_line_ids=set(emitted_line_ids),
                emitted_sentence_keys=set(emitted_sentence_keys),
            )
        )

        before_lines = _gap_lines_for_segment(
            gap_report,
            sid,
            "before",
            emitted_line_ids=emitted_line_ids,
            emitted_sentence_keys=emitted_sentence_keys,
        )
        if skip_before_vo:
            # Contiguous same-speaker source usually needs no seam hinge, but
            # EDL survivors (orientation / nugget_layup / required) must still air.
            before_lines = [
                ln
                for ln in before_lines
                if edl_before_line_survives(ln, after_vo_stack=False)
            ]
        # One host turn per seam: never stack a second synthetic after VO/transition.
        # Survivors from hosted_vo_authority must still seat on later segments after
        # opening VO — otherwise WAV exists without EDL vo_pickup
        # (exec_13183: vo_audibility_drift phantom_vo for vo_layup_seg_007/037).
        if _last_non_silence_type() in {"vo_pickup", "transition"}:
            before_lines = [
                ln
                for ln in before_lines
                if edl_before_line_survives(ln, after_vo_stack=True)
            ]

        # Opening grammar pin: orientation must stay within the early audible window.
        # Straight: orientation first among before-VO. Cold open: defer hook before-VO
        # until after hook speech + opening_music + orientation.
        cold_open_hook = False
        if first_seat and gap_report:
            for _oln in gap_report.get("interviewer_lines") or []:
                if (
                    isinstance(_oln, dict)
                    and _is_orientation(_oln)
                    and not _oln.get("skipped_optional")
                    and str(_oln.get("targets_segment_id") or "") == sid
                    and str(_oln.get("placement") or "before") == "after"
                ):
                    cold_open_hook = True
                    break
        before_lines = sorted(
            before_lines, key=lambda ln: (0 if _is_orientation(ln) else 1)
        )
        deferred_before: list[dict] = []
        if cold_open_hook and before_lines:
            deferred_before = [ln for ln in before_lines if not _is_orientation(ln)]
            before_lines = [ln for ln in before_lines if _is_orientation(ln)]

        def _emit_vo_line(line: dict, *, placement: str) -> None:
            nonlocal timeline_ms
            voice_speaker_id = str(line.get("voice_speaker_id") or "").strip()
            if not voice_speaker_id and ctx is not None:
                # Episode clone lock must land on every seated synthesize clip
                # (exec_11630: orientation missing voice_speaker_id → narrative QC).
                try:
                    from interview_mux.speaker_delivery_plan import episode_vo_identity

                    voice_speaker_id = str(
                        (episode_vo_identity(ctx) or {}).get("speaker_id") or ""
                    ).strip()
                except Exception:
                    voice_speaker_id = ""
                if not voice_speaker_id:
                    try:
                        from interview_mux.source_topology import pickup_eligible_speaker_id

                        voice_speaker_id = str(
                            pickup_eligible_speaker_id(ctx) or ""
                        ).strip()
                    except Exception:
                        voice_speaker_id = ""
            target_speaker_id = str(seg.get("speaker_id") or "").strip()
            prev_speaker_id = (
                str((prev_seg or {}).get("speaker_id") or "").strip() if prev_seg else ""
            )
            id_adjacent = bool(
                voice_speaker_id
                and (
                    voice_speaker_id == target_speaker_id
                    or (placement == "before" and voice_speaker_id == prev_speaker_id)
                )
            )
            if (
                id_adjacent
                and not bool(line.get("clone_adjacency_exempt"))
                and not _is_orientation(line)
            ):
                target = dict(seg)
                target.setdefault("segment_id", sid)
                after = dict(prev_seg) if isinstance(prev_seg, dict) else None
                if after is not None:
                    after.setdefault("segment_id", prev_sid)
                vo_key = str(line.get("line_id") or sid)
                # Advisory only (ISSUES 184): the gap report decided this line
                # airs, so the EDL seats it. Dropping it here left a required
                # line with no seat and no omit, and publishability refused the
                # mix on every attempt (Nicket's exec_025, vo_question_seg_027).
                if clone_adj.decide(
                    kind="vo_pickup",
                    key=vo_key,
                    clone_voice_id=voice_speaker_id,
                    target=target,
                    after=after if placement == "before" else None,
                ):
                    clone_adjacency_advisory.append(vo_key)
            vo_path = resolve_vo_path(line) if resolve_vo_path else None
            rel: str | None = None
            dur = 0
            if vo_path is None or not vo_path.is_file():
                missing_vo.append(line.get("line_id") or sid)
                if (
                    str(line.get("delivery") or "").lower() == "synthesize"
                    or _is_orientation(line)
                    or bool(line.get("required"))
                ):
                    return
            else:
                rel = vo_relpath(vo_path) if vo_relpath else vo_path.as_posix()
                dur = duration_fn(vo_path)
            # Cold open: music marker precedes orientation VO.
            if placement == "after" and _is_orientation(line):
                est_ms = int(float(line.get("estimated_duration_sec") or 0) * 1000)
                _append_opening_music_marker(max(dur, est_ms, 1000))
            # Substantial chapter/package VO: 4s music-only before speech (not short bridges).
            elif placement == "before" and not _is_orientation(line):
                try:
                    from interview_mux.chapter_music_bridge import (
                        montage_move_for_segment,
                        vo_line_earns_bridge,
                    )
                    from interview_mux.mastering_plan_loader import load_plan_raw

                    plan = load_plan_raw(ctx) if ctx is not None else {}
                    if not isinstance(plan, dict):
                        plan = {}
                    move = montage_move_for_segment(plan, sid)
                    first_after = False
                    spine = plan.get("story_spine") if isinstance(plan.get("story_spine"), dict) else {}
                    scenes = [s for s in (spine.get("scenes") or []) if isinstance(s, dict)]
                    for si, scene in enumerate(scenes):
                        if si <= 0:
                            continue
                        segs = [str(x) for x in (scene.get("segment_ids") or [])]
                        if segs and segs[0] == sid:
                            first_after = True
                            break
                    if vo_line_earns_bridge(
                        line,
                        montage_move=move,
                        first_after_chapter_hinge=first_after,
                        measured_duration_ms=dur or None,
                    ):
                        _append_chapter_music_bridge(
                            after_segment_id=str(prev_sid or ""),
                            before_segment_id=sid,
                        )
                except Exception:
                    pass
            clip = {
                "type": "vo_pickup",
                "line_id": line.get("line_id"),
                "targets_segment_id": sid,
                "placement": placement,
                "voice_speaker_id": voice_speaker_id,
                "clone_adjacency_exempt": bool(
                    line.get("clone_adjacency_exempt") or _is_orientation(line)
                ),
                "gap_type": line.get("gap_type"),
                "line_category": line.get("line_category"),
                "episode_orientation": bool(line.get("episode_orientation")),
                "opening_sequence": line.get("opening_sequence"),
                "allow_music_bed_overlap": bool(line.get("allow_music_bed_overlap"))
                or (
                    placement == "before"
                    and bool(clips)
                    and str(clips[-1].get("air_kind") or "") == "kept_source_gap"
                ),
                **_copy_hashes(line),
                "source_path": rel,
                "duration_ms": dur,
                "timeline_start_ms": timeline_ms,
            }
            clips.append(clip)
            gap_placements.append(
                {
                    "line_id": line.get("line_id"),
                    "targets_segment_id": sid,
                    "placement": placement,
                    "timeline_start_ms": timeline_ms,
                }
            )
            timeline_ms += dur
            # Straight open: music marker follows orientation VO.
            if placement == "before" and _is_orientation(line):
                est_ms = int(float(line.get("estimated_duration_sec") or 0) * 1000)
                _append_opening_music_marker(max(dur, est_ms, 1000))
            elif dur > 0:
                _append_air("after_vo", dur)

        def _before_vo_earns_chapter_bridge() -> bool:
            if not before_lines:
                return False
            try:
                from interview_mux.chapter_music_bridge import (
                    montage_move_for_segment,
                    vo_line_earns_bridge,
                )
                from interview_mux.mastering_plan_loader import load_plan_raw

                plan = load_plan_raw(ctx) if ctx is not None else {}
                if not isinstance(plan, dict):
                    plan = {}
                move = montage_move_for_segment(plan, sid)
                first_after = False
                spine = plan.get("story_spine") if isinstance(plan.get("story_spine"), dict) else {}
                scenes = [s for s in (spine.get("scenes") or []) if isinstance(s, dict)]
                for si, scene in enumerate(scenes):
                    if si <= 0:
                        continue
                    segs = [str(x) for x in (scene.get("segment_ids") or [])]
                    if segs and segs[0] == sid:
                        first_after = True
                        break
                for line in before_lines:
                    if _is_orientation(line):
                        continue
                    measured: int | None = None
                    if resolve_vo_path is not None:
                        vo_path = resolve_vo_path(line)
                        if vo_path is not None and vo_path.is_file():
                            measured = duration_fn(vo_path)
                    if vo_line_earns_bridge(
                        line,
                        montage_move=move,
                        first_after_chapter_hinge=first_after,
                        measured_duration_ms=measured,
                    ):
                        return True
            except Exception:
                return False
            return False

        def _insert_kept_source_gap() -> None:
            nonlocal timeline_ms
            if not isinstance(prev_seg, dict) or _same_story(prev_seg, seg):
                return
            if clips and str(clips[-1].get("air_kind") or "") == "chapter_music_bridge":
                return
            if _before_vo_earns_chapter_bridge():
                return
            try:
                source_gap = int(seg.get("start_ms") or 0) - int(prev_seg.get("end_ms") or 0)
            except (TypeError, ValueError):
                source_gap = 0
            if source_gap < 8_000:
                return
            prev_end = int(prev_seg.get("end_ms") or 0)
            this_start = int(seg.get("start_ms") or 0)
            for other_id, other in segments_by_id.items():
                if other_id in {sid, prev_sid} or not isinstance(other, dict):
                    continue
                try:
                    other_start = int(other.get("start_ms") or 0)
                except (TypeError, ValueError):
                    continue
                if prev_end < other_start < this_start:
                    return
            kept = min(source_gap, 30_000)
            clips.append(
                {
                    "type": "silence",
                    "air_kind": "kept_source_gap",
                    "duration_ms": kept,
                    "timeline_start_ms": timeline_ms,
                    "preserve_planned_music": True,
                    "music_bed_preferred": True,
                }
            )
            timeline_ms += kept

        # Everything seated for this segment before its speech clip (kept tape
        # pause, chapter bridge, before-VO pickups, air pads) is provisional
        # until the speech clip itself seats. A segment the builder then omits
        # as unplayable takes those back with it; a pickup left behind targets
        # a segment that is not in ordered speech, which narrative QC refuses
        # as an orphan on every pass (exec on 2026-10-11, seg_038; ISSUES 190).
        seat_mark = (
            len(clips),
            len(gap_placements),
            timeline_ms,
            len(missing_vo),
            len(clone_adjacency_advisory),
        )

        def _unseat_before_vo(reason: str) -> None:
            nonlocal timeline_ms
            taken_back = [
                str(c.get("line_id") or "")
                for c in clips[seat_mark[0] :]
                if isinstance(c, dict) and c.get("type") == "vo_pickup"
            ]
            del clips[seat_mark[0] :]
            del gap_placements[seat_mark[1] :]
            timeline_ms = seat_mark[2]
            del missing_vo[seat_mark[3] :]
            del clone_adjacency_advisory[seat_mark[4] :]
            for lid in taken_back:
                if lid and lid not in unseated_before_vo:
                    unseated_before_vo.append(lid)
            if taken_back and ctx is not None:
                try:
                    ctx.log(
                        f"EDL: {sid} omitted as unplayable ({reason}); its before-VO "
                        f"{taken_back} is omitted with it",
                        level="warning",
                        stage="edl",
                        detail={"segment_id": sid, "line_ids": taken_back, "reason": reason},
                    )
                except Exception:
                    pass

        _insert_kept_source_gap()
        for line in before_lines:
            _emit_vo_line(line, placement="before")

        # Native-open omit: still reserve the opening-music window so theme can
        # bed under the hosts' own greeting (music → body).
        if first_seat and not cold_open_hook:
            meta = (
                (gap_report or {}).get("opening_orientation")
                if isinstance(gap_report, dict)
                else None
            )
            omitted = isinstance(meta, dict) and (
                bool(meta.get("omitted")) or meta.get("required") is False
            )
            already_opening_music = any(
                str(clip.get("air_kind") or "") == "opening_music" for clip in clips
            )
            if omitted and not already_opening_music:
                _append_opening_music_marker(
                    max(int(seg.get("end_ms") or 0) - int(seg.get("start_ms") or 0), 1000)
                )

        speech_start = int(seg["start_ms"])
        speech_end = int(seg["end_ms"])
        air_meta: dict = {}
        next_keeper_start = _tape_next_start(str(sid), speech_start)
        prev_speech_end_ms = _tape_prev_end(str(sid), speech_start)
        next_tape = None
        if next_keeper_start is not None:
            for other_id, other in segments_by_id.items():
                if other_id == sid or not isinstance(other, dict):
                    continue
                try:
                    if int(other.get("start_ms") or 0) == int(next_keeper_start):
                        next_tape = other
                        break
                except (TypeError, ValueError):
                    continue
        if ideal_cuts is not None or words:
            from interview_mux.media_ip_cta import never_touch_end_cap_ms

            nt_cap = never_touch_end_cap_ms(speech_start, never_touch_intervals)
            speech_start, speech_end = resolve_keeper_air_bounds(
                source_start_ms=speech_start,
                source_end_ms=speech_end,
                cuts_doc=ideal_cuts if isinstance(ideal_cuts, (dict, list)) else None,
                words=words or None,
                segment_id=str(sid),
                max_keep_ms=max_keeper_ms,
                next_keeper_start_ms=next_keeper_start,
                prev_keeper_end_ms=prev_speech_end_ms,
                never_touch_cap_ms=nt_cap,
                same_story=_same_story(seg, next_tape) and not _keeps_neighbor_wall(next_tape),
                meta_out=air_meta,
                wav_path=normalized_wav,
            )
        if words:
            from interview_mux.gap_vo_prior_context import is_legal_conceptual_hinge
            from interview_mux.ideal_cuts import last_complete_thought_end_ms

            span = " ".join(
                str(w.get("text") or "")
                for w in words
                if isinstance(w, dict)
                and speech_start <= int(w.get("start_ms") or 0) < speech_end
            ).strip()
            if span and not is_legal_conceptual_hinge(
                span, words=words, end_ms=speech_end
            ):
                fixed = last_complete_thought_end_ms(
                    words, start_ms=speech_start, end_ms=speech_end
                )
                if fixed is not None and fixed - speech_start >= 400:
                    speech_end = int(fixed)
                else:
                    # Nearest on-air start after this end, wherever it sits in
                    # the air order: a reordered episode can have a later tape
                    # segment on air that is not the next clip.
                    later_starts = [
                        int(segments_by_id[o]["start_ms"])
                        for o in ordered
                        if o != sid
                        and isinstance(segments_by_id.get(o), dict)
                        and segments_by_id[o].get("start_ms") is not None
                        and int(segments_by_id[o]["start_ms"]) >= speech_end
                    ]
                    nearest = min(later_starts) if later_starts else None
                    if next_keeper_start is not None and (
                        nearest is None or next_keeper_start < nearest
                    ):
                        nearest = next_keeper_start
                    extended = extend_hanging_end_to_thought(
                        words,
                        speech_end,
                        next_keeper_start_ms=nearest,
                    )
                    if extended is not None:
                        speech_end = extended
                        air_meta["air_bound_reason"] = (
                            str(air_meta.get("air_bound_reason") or "")
                            + "+hanging_end_extend"
                        ).lstrip("+")
        speech_dur = max(0, speech_end - speech_start)
        if words and speech_dur < 5000:
            from interview_mux.gap_vo_prior_context import is_legal_conceptual_hinge

            span = " ".join(
                str(w.get("text") or "")
                for w in words
                if isinstance(w, dict)
                and speech_start <= int(w.get("start_ms") or 0) < speech_end
            ).strip()
            b0 = air_meta.get("before_start_ms")
            b1 = air_meta.get("before_end_ms")
            if span and not is_legal_conceptual_hinge(
                span, words=words, end_ms=speech_end
            ):
                if b0 is not None and b1 is not None and int(b1) - int(b0) >= 2000:
                    speech_start, speech_end = int(b0), int(b1)
                    speech_dur = max(0, speech_end - speech_start)
                    air_meta["air_bound_reason"] = (
                        str(air_meta.get("air_bound_reason") or "") + "+before_window_hanging_rescue"
                    )
        if never_touch_intervals:
            from interview_mux.media_ip_cta import clamp_source_away_from_never_touch

            clamped_s, clamped_e, nt_notes = clamp_source_away_from_never_touch(
                speech_start, speech_end, never_touch_intervals
            )
            if nt_notes:
                speech_start, speech_end = clamped_s, clamped_e
                prior = str(air_meta.get("air_bound_reason") or "")
                air_meta["air_bound_reason"] = (
                    f"{prior}+never_touch_clamp" if prior else "never_touch_clamp"
                )
        speech_dur = max(0, speech_end - speech_start)
        if speech_dur < 400:
            hard_keep = False
            if ctx is not None:
                try:
                    from interview_mux.hard_keep import hard_keep_segment_ids

                    hard_keep = sid in hard_keep_segment_ids(ctx)
                except Exception:
                    hard_keep = False
                if not hard_keep:
                    # Same judge as the live-EDL writer and removal authority:
                    # a keep they protect gets the min-air seat here instead of
                    # an omit the selection then refuses (ISSUES 190).
                    try:
                        from interview_mux.removal_authority import removal_block_reason

                        hard_keep = bool(
                            removal_block_reason(ctx, sid, on_air=[str(o) for o in ordered])
                        )
                    except Exception:
                        hard_keep = False
            if not hard_keep:
                already_speech = any(
                    isinstance(c, dict) and c.get("type") == "speech" for c in clips
                )
                later_long = False
                for later_id in ordered[idx + 1 :]:
                    later = segments_by_id.get(later_id)
                    if not isinstance(later, dict):
                        continue
                    try:
                        span = int(later.get("end_ms") or 0) - int(later.get("start_ms") or 0)
                    except (TypeError, ValueError):
                        span = 0
                    if span >= 400:
                        later_long = True
                        break
                if already_speech or later_long:
                    omitted_unplayable.append(sid)
                    _unseat_before_vo("unplayable_short")
                    continue
                hard_keep = True
            # Hard-keep microfragments must stay on the speech list so narrative
            # QC matches selection (exec_13198 seg_028 "Okay.").
            raw_s = int(seg.get("start_ms") or seg.get("source_start_ms") or speech_start)
            raw_e = int(seg.get("end_ms") or seg.get("source_end_ms") or speech_end)
            if raw_e > raw_s:
                speech_start, speech_end = raw_s, raw_e
                speech_dur = raw_e - raw_s
            if speech_dur < 400:
                room_end = speech_start + 400
                nxt_id = ordered[idx + 1] if idx + 1 < len(ordered) else None
                nxt_seg = segments_by_id.get(nxt_id) if nxt_id else None
                if isinstance(nxt_seg, dict) and nxt_seg.get("start_ms") is not None:
                    try:
                        room_end = min(room_end, int(nxt_seg["start_ms"]) - 80)
                    except (TypeError, ValueError):
                        pass
                for interval in never_touch_intervals:
                    try:
                        nt0 = int(interval[0])
                        nt1 = int(interval[1])
                    except (TypeError, ValueError, IndexError):
                        continue
                    if nt0 <= speech_start < nt1:
                        room_end = speech_start
                    elif speech_start < nt0 < room_end:
                        room_end = min(room_end, nt0)
                if room_end - speech_start < 400 and nxt_id:
                    omitted_unplayable.append(sid)
                    _unseat_before_vo("unplayable_no_room")
                    continue
                speech_end = max(speech_start, room_end)
                speech_dur = speech_end - speech_start
            air_meta = dict(air_meta or {})
            air_meta["air_bound_reason"] = (
                str(air_meta.get("air_bound_reason") or "") + "+hard_keep_min_air"
            ).lstrip("+")
        if clips and str(clips[-1].get("type") or "") == "silence":
            pass
        elif any(c.get("type") == "vo_pickup" for c in clips[-3:]):
            _append_air("before_answer", speech_dur)
        speech_clip = {
            "segment_id": sid,
            "source_start_ms": speech_start,
            "source_end_ms": speech_end,
            "timeline_start_ms": timeline_ms,
            "duration_ms": speech_dur,
            "type": "speech",
        }
        if air_meta:
            speech_clip["air_bound_reason"] = air_meta.get("air_bound_reason")
            if air_meta.get("ideal_window_id"):
                speech_clip["air_bound_ideal_window_id"] = air_meta.get("ideal_window_id")
            speech_clip["air_bound_before_ms"] = [
                air_meta.get("before_start_ms"),
                air_meta.get("before_end_ms"),
            ]
            speech_clip["air_bound_after_ms"] = [
                air_meta.get("after_start_ms"),
                air_meta.get("after_end_ms"),
            ]
        clips.append(speech_clip)
        timeline_ms += speech_dur
        resolved_end_by_sid[str(sid)] = speech_end

        after_lines = _gap_lines_for_segment(
            gap_report,
            sid,
            "after",
            emitted_line_ids=emitted_line_ids,
            emitted_sentence_keys=emitted_sentence_keys,
        )
        # Prefer a before-VO layup on the next clip over an after-VO on this one.
        # Keep episode orientation — cold-open grammar requires it after the hook.
        if next_before_vo:
            after_lines = [ln for ln in after_lines if _is_orientation(ln)]
        after_lines = sorted(
            after_lines, key=lambda ln: (0 if _is_orientation(ln) else 1)
        )

        for line in after_lines:
            _emit_vo_line(line, placement="after")

        # Cold-open: hook before-VO deferred until after orientation.
        for line in deferred_before:
            _emit_vo_line(line, placement="before")

        if idx + 1 < len(ordered):
            nxt = ordered[idx + 1]
            tr = _transition_after_segment(transitions, sid, nxt)
            suppressed_transition = False
            if tr:
                from interview_mux.gap_framing import choose_seam_synthetic

                choice = choose_seam_synthetic(
                    sid,
                    nxt,
                    gap_report=gap_report,
                    transitions_doc=transitions,
                )
                if str(choice.get("kind") or "") == "layup":
                    tr = None
                elif _last_non_silence_type() == "vo_pickup":
                    tr = None
                else:
                    already_vo = any(
                        isinstance(c, dict)
                        and c.get("type") == "vo_pickup"
                        and str(c.get("targets_segment_id") or "") == str(nxt)
                        for c in clips
                    )
                    if already_vo:
                        tr = None
            if tr:
                transition_voice = str(tr.get("voice_speaker_id") or "").strip()
                after_seg = dict(segments_by_id.get(sid) or {})
                after_seg.setdefault("segment_id", sid)
                before_seg = dict(segments_by_id.get(nxt) or {})
                before_seg.setdefault("segment_id", nxt)
                # Empty voice still runs decide when a clone plan is locked so
                # unvoiced transitions next to the clone source are hitch-replaced.
                clone_voice = transition_voice
                if not clone_voice and ctx is not None:
                    try:
                        if ctx.artifact_exists("understanding/speaker_delivery_plan.json"):
                            plan = ctx.read_json(
                                "understanding/speaker_delivery_plan.json"
                            )
                            clone_voice = str(
                                (plan or {}).get("clone_speaker_id") or ""
                            ).strip()
                    except Exception:
                        clone_voice = ""
                if not clone_voice and ctx is not None:
                    try:
                        from interview_mux.source_topology import (
                            pickup_eligible_speaker_id,
                        )

                        clone_voice = str(
                            pickup_eligible_speaker_id(ctx) or ""
                        ).strip()
                    except Exception:
                        clone_voice = ""
                # Advisory only (ISSUES 184): a planned transition is seated; the
                # seam is recorded for a later quality pass, never dropped here.
                if clone_voice and clone_adj.decide(
                    kind="transition",
                    key=f"transition:{sid}->{nxt}",
                    clone_voice_id=clone_voice,
                    after=after_seg,
                    before=before_seg,
                ):
                    clone_adjacency_advisory.append(f"transition:{sid}->{nxt}")
            if tr:
                text = str(tr.get("text") or "")
                from interview_mux.spoken_copy_guard import script_hash
                from interview_mux.chapter_music_bridge import chapter_hinge_earns_bridge
                from interview_mux.seam_glue import is_chapter_scale_pair

                tr_type = str(tr.get("type") or "bridge")
                chapter_scale = tr_type == "chapter" or is_chapter_scale_pair(
                    {"after_segment_id": sid, "before_segment_id": nxt, "type": tr_type}
                )
                same_ans = _same_answer_seam(sid, nxt)
                # Music-only bridge *before* spoken hitch so VO rides under music.
                if chapter_hinge_earns_bridge(
                    transition_type=tr_type,
                    is_chapter_scale=chapter_scale,
                    same_answer=same_ans,
                ):
                    _append_chapter_music_bridge(after_segment_id=sid, before_segment_id=nxt)
                tr_path = (
                    resolve_transition_path(sid, nxt) if resolve_transition_path else None
                )
                tr_rel = None
                tr_dur = 0
                if tr_path is not None and tr_path.is_file():
                    tr_rel = vo_relpath(tr_path) if vo_relpath else tr_path.as_posix()
                    tr_dur = duration_fn(tr_path)
                elif text.strip():
                    missing_transitions.append(f"{sid}->{nxt}")
                clip_tr = {
                    "type": "transition",
                    "after_segment_id": sid,
                    "before_segment_id": nxt,
                    "text": text,
                    "transition_type": tr_type,
                    "duration_ms": tr_dur,
                    "timeline_start_ms": timeline_ms,
                    "script_hash": script_hash(text),
                }
                voice = tr.get("voice_speaker_id")
                if voice:
                    clip_tr["voice_speaker_id"] = voice
                if tr_rel:
                    clip_tr["source_path"] = tr_rel
                clips.append(clip_tr)
                timeline_ms += tr_dur
                if not same_ans and not chapter_hinge_earns_bridge(
                    transition_type=tr_type,
                    is_chapter_scale=chapter_scale,
                    same_answer=same_ans,
                ):
                    if tr_dur > 0:
                        _append_air("chapter_hinge", tr_dur)
                    else:
                        _append_air("chapter_hinge", speech_dur)
            else:
                # No spoken transition: still need audible chapter/reorder hitch
                # unless this is the same answer continuing on tape.
                needs_hitch = suppressed_transition
                chapter_scale = False
                if not needs_hitch:
                    from interview_mux.reorder_bridges import build_reorder_bridges

                    bridges = build_reorder_bridges([sid, nxt], segments_by_id)
                    needs_hitch = bool(bridges.get("pairs"))
                    try:
                        from interview_mux.seam_glue import is_chapter_scale_pair

                        for pair in bridges.get("pairs") or []:
                            if isinstance(pair, dict) and is_chapter_scale_pair(pair):
                                chapter_scale = True
                                break
                    except Exception:
                        chapter_scale = False
                same_ans = _same_answer_seam(sid, nxt)
                if needs_hitch and not same_ans:
                    from interview_mux.chapter_music_bridge import chapter_hinge_earns_bridge

                    if chapter_hinge_earns_bridge(
                        is_chapter_scale=chapter_scale,
                        same_answer=same_ans,
                    ):
                        _append_chapter_music_bridge(
                            after_segment_id=sid, before_segment_id=nxt
                        )
                    else:
                        _append_air(
                            "chapter_hinge",
                            max(int(speech_dur), 1000),
                            required_seam_hitch=True,
                            clone_adjacency_hitch=bool(suppressed_transition),
                        )

    if omitted_unplayable and ctx is not None:
        try:
            from interview_mux.media_ip_cta import _omit_unplayable_keeps_from_selection

            drop_reasons = {sid: "never_touch_unplayable" for sid in omitted_unplayable}
            _omit_unplayable_keeps_from_selection(ctx, omitted_unplayable, drop_reasons)
        except Exception:
            pass
    air_ordered = [s for s in ordered if s not in set(omitted_unplayable)]

    return {
        "version": 1,
        "ordered_segment_ids": air_ordered,
        # Recorded so EDL QC can tell a deliberate unplayable omission from a
        # mismatch when the selection commit of the drop was refused (ISSUES 151).
        "omitted_unplayable_segment_ids": sorted(set(omitted_unplayable)),
        # Before-VO lines omitted with their unplayable target (ISSUES 190).
        "omitted_unplayable_vo_line_ids": sorted(set(unseated_before_vo)),
        "clips": clips,
        "gap_placements": gap_placements,
        "timeline_duration_ms": timeline_ms,
        "gap_report_line_count": len((gap_report or {}).get("interviewer_lines") or []),
        "vo_pickup_clip_count": sum(1 for c in clips if c.get("type") == "vo_pickup"),
        "transition_clip_count": sum(1 for c in clips if c.get("type") == "transition"),
        "silence_clip_count": sum(1 for c in clips if c.get("type") == "silence"),
        "warnings": {
            "missing_vo_files": sorted(set(missing_vo)),
            "gap_targets_not_in_selection": sorted(set(missing_targets)),
            "missing_segment_lookups": sorted(set(missing_segments)),
            "missing_transition_audio": sorted(set(missing_transitions)),
            "clone_adjacency_advisory": sorted(set(clone_adjacency_advisory)),
            "vo_lines_unseated_unplayable_target": sorted(set(unseated_before_vo)),
            "clone_adjacency_id_mismatch_kept": sorted(
                set(k for k in clone_adj.kept_despite_id() if k)
            ),
        },
        "mux_scope": "full_mix",
        "_clone_adjacency_verify": clone_adj.report(),
    }


def resync_required_synthesize_wavs(ctx: RunContext, gap_report: dict) -> list[str]:
    """Re-speak active synthesize lines whose WAV is missing or hash-stale. Once.

    When gap framing / chatterbox is active, every non-skipped synthesize line is
    eligible (not only orientation). Otherwise only orientation/required.

    Must write under ``vo_synthesize`` staging and promote — synthesizing while
    EDL staging is active lands bytes in ``.pending_writes/edl/vo_pickup/``, which
    ``discard_non_owner_pending_vo_pickup`` deletes (exec_11630 missing 005/020).
    """
    from interview_mux.opening_orientation import is_episode_orientation
    from interview_mux.spoken_copy_guard import script_hash
    from interview_mux.vo_synthesis_audit import synthesis_entry_matches_line
    from interview_mux import s2s_runner
    from interview_mux.write_staging import (
        VO_PICKUP_OWNER_STAGES,
        active_stage_id,
        discard_non_owner_pending_vo_pickup,
        enter_stage_staging,
        exit_stage_staging,
        promote_owner_vo_pickup,
    )

    framing_active = False
    try:
        from interview_mux.gap_vo_gates import (
            framing_requires_nested_synth_gate,
            nested_synth_may_mint,
        )

        framing_active = framing_requires_nested_synth_gate(ctx)
    except Exception:
        # Partial-shaped fail-closed: treat as gated.
        framing_active = True

    if framing_active:
        # DP-NESTED-SYNTH A: probe errors must skip-not-mint (never fail-open).
        try:
            may_mint, skip_note = nested_synth_may_mint(ctx, for_synthesize=False)
            if not may_mint:
                # Partial/manual: do not stamp gates or SystemExit — defer WAV demand.
                ctx.log(
                    skip_note or "nested_synth_skipped:vo_path_not_ready",
                    level="warning",
                    stage="edl",
                )
                return [skip_note or "nested_synth_skipped:vo_path_not_ready"]
        except Exception as exc:
            note = f"nested_synth_skipped:probe_error:{type(exc).__name__}"
            try:
                ctx.log(note, level="warning", stage="edl")
            except Exception:
                pass
            return [note]

    from interview_mux.air_script import seated_vo_line_ids
    from interview_mux.mastering_plan_loader import load_plan_raw
    from interview_mux.vo_contract import gap_line_requires_synthesis

    plan = load_plan_raw(ctx) if ctx.artifact_exists("mastering/mastering_plan.json") else {}
    seated = seated_vo_line_ids(plan)

    notes: list[str] = []
    parent = active_stage_id()
    nested = parent not in VO_PICKUP_OWNER_STAGES
    if nested:
        enter_stage_staging("vo_synthesize")
    try:
        for line in gap_report.get("interviewer_lines") or []:
            if not isinstance(line, dict):
                continue
            if str(line.get("delivery") or "").lower() != "synthesize":
                continue
            if not gap_line_requires_synthesis(line, seated):
                continue
            if not framing_active:
                requiredish = is_episode_orientation(line) or bool(line.get("required"))
                lid = str(line.get("line_id") or "")
                if lid not in seated and not requiredish:
                    continue
            path = resolve_vo_pickup_path(ctx, line)
            matches, reason = synthesis_entry_matches_line(ctx, line)
            if path is not None and path.is_file() and matches:
                continue
            lid = str(line.get("line_id") or "")
            try:
                s2s_runner.synthesize_line(ctx, line, mode="synthesize")
                promote_owner_vo_pickup(ctx)
                discard_non_owner_pending_vo_pickup(ctx)
                notes.append(lid)
            except Exception as exc:
                # Chatterbox may write then false-fail JSON; accept stem if present.
                try:
                    promote_owner_vo_pickup(ctx)
                except Exception:
                    pass
                from interview_mux.vo_contract import _gap_row_has_pickup_stem

                if _gap_row_has_pickup_stem(ctx, line):
                    notes.append(lid)
                    continue
                raise RuntimeError(
                    f"edl: synthesize WAV stale/missing (line_id={lid} "
                    f"reason={reason} path={path} "
                    f"script_hash={script_hash(str(line.get('text') or ''))}): {exc}"
                ) from exc
            path2 = resolve_vo_pickup_path(ctx, line)
            matches2, reason2 = synthesis_entry_matches_line(ctx, line)
            # Audit match already proves script+bytes on disk (incl. pending). Do not
            # also require resolve_vo_pickup_path — that gate includes speech-QA and
            # can false-fail mid-stage while the take is still under .pending_writes
            # (exec_10066: reason=match path=None → raise → orphan 019 WAV).
            if matches2:
                continue
            if path2 is not None and path2.is_file():
                continue
            from interview_mux.vo_contract import _gap_row_has_pickup_stem

            if _gap_row_has_pickup_stem(ctx, line):
                continue
            raise RuntimeError(
                f"edl: synthesize WAV stale/missing (line_id={lid} "
                f"reason={reason2} path={path2} "
                f"script_hash={script_hash(str(line.get('text') or ''))})"
            )
    finally:
        if nested:
            if parent:
                enter_stage_staging(parent)
            else:
                exit_stage_staging()
    return notes


def _selection_write_permitted(ctx: RunContext) -> bool:
    """EDL must never rewrite a frozen selection (selection leads EDL)."""
    try:
        from interview_mux.artifact_ownership import write_permitted

        ok, _reason = write_permitted(
            ctx, "master/selection.json", "edl", role="producer", verb="persist"
        )
        return bool(ok)
    except Exception:
        return False


def _persist_selection_for_edl(ctx: RunContext, selection: dict, *, note: str) -> None:
    """Persist EDL-side selection repairs only while the owner still allows writes.

    Under hard freeze the repairs stay in memory: the EDL build consumes the
    repaired document, disk authority stays with the selection owner.
    """
    if _selection_write_permitted(ctx):
        ctx.write_json("master/selection.json", selection)
        return
    ctx.log(
        f"edl: selection frozen — keeping {note} in memory (no disk rewrite)",
        level="info",
        stage="edl",
    )


def _prepare_locked_selection(ctx: RunContext, selection: dict) -> dict:
    from interview_mux.artifact_repairs import (
        _segment_is_blank_or_unusable,
        reconcile_ordered_vs_excluded,
    )
    from interview_mux.selection_order_repair import fill_chapter_list_membership_gaps

    selection = reconcile_ordered_vs_excluded(
        selection if isinstance(selection, dict) else {}
    )
    order = [str(s) for s in (selection.get("ordered_segment_ids") or []) if s]
    try:
        from interview_mux.hard_keep import hard_keep_segment_ids

        hard_keeps = {str(s) for s in (hard_keep_segment_ids(ctx) or []) if s}
    except Exception:
        hard_keeps = set()
    # Keep hard-keep microfragments on the EDL speech list so narrative QC
    # parity matches selection (exec_13198 seg_028 "Okay.").
    cleaned = [
        s
        for s in order
        if (not _segment_is_blank_or_unusable(ctx, s)) or s in hard_keeps
    ]
    if cleaned != order:
        have = {
            str(r.get("segment_id") if isinstance(r, dict) else r)
            for r in (selection.get("excluded_segment_ids") or [])
        }
        excl = list(selection.get("excluded_segment_ids") or [])
        for sid in order:
            if sid in cleaned or sid in have:
                continue
            excl.append({"segment_id": sid, "reason": "blank_or_unusable_answer_audio"})
            have.add(sid)
        selection["ordered_segment_ids"] = cleaned
        selection["excluded_segment_ids"] = excl
        keep = set(cleaned)
        for ch in selection.get("chapters") or []:
            if isinstance(ch, dict):
                ch["segment_ids"] = [
                    str(x) for x in (ch.get("segment_ids") or []) if str(x) in keep
                ]
        selection = reconcile_ordered_vs_excluded(selection)
    # Contiguous chapter membership: absorb unassigned air-order ids into the
    # nearest chapter so edl_narrative_qc does not see split chapters (exec_11630
    # Clinical-Trial split by leftover seg_041).
    order_final = [str(s) for s in (selection.get("ordered_segment_ids") or []) if s]
    chapters = [
        dict(ch) for ch in (selection.get("chapters") or []) if isinstance(ch, dict)
    ]
    if order_final and chapters:
        filled, filled_ids = fill_chapter_list_membership_gaps(chapters, order_final)
        if filled_ids:
            selection["chapters"] = filled
            ctx.log(
                f"edl: filled chapter membership gaps {filled_ids[:8]}",
                level="info",
                stage="edl",
            )
    return selection


#: Orientation contract findings that mean the line is missing from air. The
#: rest (mission labels, wording, position, music-then-body) are quality notes
#: and are logged (ISSUES 185).
_STRUCTURAL_ORIENTATION_MARKERS = ("audible_count", "opening_orientation_count")


def _blocking_orientation_errors(ctx: RunContext, errors: list[str]) -> list[str]:
    blocking = [e for e in errors if any(m in str(e) for m in _STRUCTURAL_ORIENTATION_MARKERS)]
    advisory = [e for e in errors if e not in blocking]
    if advisory:
        try:
            ctx.log(
                "edl: opening orientation notes (advisory): " + "; ".join(advisory[:4]),
                level="warning",
                stage="edl",
            )
        except Exception:
            pass
    return blocking


def run_edl(ctx: RunContext) -> None:
    """Build ``master/edl.json`` from disk selection + seated VO/transitions.

    S1–S5 peel: no nested VO mint, no spoofed foreign writers, no orientation
    ensure/revive, no seam-glue mint, no selection blank/chapter dual-copy.
    Missing audio / incomplete glue → refuse to the upstream owner.
    """
    from interview_mux.edl_narrative_remutate import narrative_audit_blocks_edl

    if narrative_audit_blocks_edl(ctx):
        # The audit is an LLM verdict; its only remedy is a re-roll (ISSUES 185;
        # 48, 70, 120, 152, 162 were this shape). Logged, never blocking.
        ctx.log(
            "edl_narrative_audit has blocking issues (advisory) — building the EDL",
            level="warning",
            stage="edl",
        )
    check_narrative_qc(ctx, stage="edl", require_selection=True)

    soft = False
    with logged_step("edl/load_inputs", ctx=ctx, stage="edl"):
        try:
            from interview_mux.media_ip_cta import heal_nle_unplayable_keep_overrides

            heal_nle_unplayable_keep_overrides(ctx)
        except Exception:
            pass
        # Overlap-union owner retires absorbed ids (not an EDL selection rewrite).
        try:
            from interview_mux.edl_overlap_repair import (
                retire_consumed_ids_from_selection,
            )

            retire_consumed_ids_from_selection(ctx)
        except Exception as exc:
            ctx.log(
                f"edl: could not retire union-absorbed ids from selection ({exc})",
                level="warning",
                stage="edl",
            )
        # S5: disk selection is SSOT. NLE operator edits may reshape the in-memory
        # cut list for this build only — never blank/chapter/air-script dual-copy.
        disk_selection = ctx.read_json("master/selection.json")
        if not isinstance(disk_selection, dict):
            raise SystemExit("edl: master/selection.json missing or invalid")
        selection = dict(disk_selection)
        nle = load_nle(ctx)
        by_id = _segment_by_id(ctx)
        if nle_has_operator_edits(nle):
            # S5: EDL is not a selection producer. NLE must already be committed
            # onto disk selection (GUI / owner) — applying here would create
            # selection_edl_order_drift at write_live_edl.
            preview = apply_nle_to_selection(
                dict(disk_selection), nle, segments_by_id=by_id
            )
            disk_order = [
                str(s) for s in (disk_selection.get("ordered_segment_ids") or []) if s
            ]
            nle_order = [
                str(s) for s in (preview.get("ordered_segment_ids") or []) if s
            ]
            if not nle_committed_on_disk(disk_order, nle_order, disk_selection):
                if not _selection_stands_over_nle_order(ctx, disk_order, nle_order):
                    raise SystemExit(
                        "edl: NLE operator edits not committed on disk selection; "
                        "land NLE via selection owner before edl "
                        f"(disk={disk_order[:8]} nle={nle_order[:8]})"
                    )
                # Same segments, different order, nobody at the timeline: the
                # committed selection is the air order (ISSUES 130). Keep the
                # NLE's non-order overlays, drop its order.
                preview = dict(preview)
                preview["ordered_segment_ids"] = list(disk_order)
                nle_order = list(disk_order)
            selection = preview
            ctx.log(
                f"EDL: NLE already on disk selection — {len(nle_order)} segments.",
                level="info",
                stage="edl",
            )
        gap_report = (
            ctx.read_json("understanding/gap_report.json")
            if ctx.artifact_exists("understanding/gap_report.json")
            else None
        )
        from interview_mux.nugget_layup import (
            assert_gap_report_layup_authority,
            assert_layup_fresh_vs_selection,
        )

        assert_layup_fresh_vs_selection(ctx, stage="edl")
        assert_gap_report_layup_authority(
            ctx, gap_report if isinstance(gap_report, dict) else None, stage="edl"
        )
        transitions = (
            ctx.read_json("master/transitions.json")
            if ctx.artifact_exists("master/transitions.json")
            else {"transitions": []}
        )
        # S4: glue mint lives on `transitions`; EDL only refuses incomplete bridges.
        try:
            from interview_mux.bridge_completeness import (
                assert_bridges_complete,
                justified_skip_before_ids,
                missing_reorder_bridges,
            )

            bridges = (
                ctx.read_json("understanding/reorder_bridges.json")
                if ctx.artifact_exists("understanding/reorder_bridges.json")
                else {"pairs": []}
            )
            skip_before = justified_skip_before_ids(ctx)
            prior_edl = ctx.read_json("master/edl.json") if ctx.artifact_exists("master/edl.json") else None
            missing = missing_reorder_bridges(
                bridges if isinstance(bridges, dict) else {"pairs": []},
                gap_report=gap_report if isinstance(gap_report, dict) else None,
                transitions=transitions if isinstance(transitions, dict) else None,
                justified_skip_before_ids=skip_before,
                edl=prior_edl if isinstance(prior_edl, dict) else None,
            )
            from interview_mux.bridge_completeness import forbidden_bridge_pairs

            forbidden = forbidden_bridge_pairs(ctx, missing)
            missing = [m for m in missing if (m.get("after_segment_id"), m.get("before_segment_id")) not in forbidden]
            if missing:
                # Advisory (ISSUES 185): build the EDL; the seam plays unglued.
                ctx.log(
                    "edl: bridge_completeness incomplete (advisory) "
                    f"(missing={missing[:6]})",
                    level="warning",
                    stage="edl",
                )
            assert_bridges_complete(
                bridges if isinstance(bridges, dict) else {"pairs": []},
                gap_report=gap_report if isinstance(gap_report, dict) else None,
                transitions=transitions if isinstance(transitions, dict) else None,
                justified_skip_before_ids=skip_before,
                soft=False,
            )
        except SystemExit:
            raise
        except Exception as glue_exc:
            ctx.log(
                f"edl: bridge completeness preflight skipped: {glue_exc}",
                level="warning",
                stage="edl",
            )

    from interview_mux.air_order_integrity import audit_and_report

    audit_and_report(ctx, stage="edl", repair=False)

    from interview_mux.transition_vo import (
        assert_spoken_transitions_audible,
        resolve_transition_wav,
    )

    with logged_step("edl/build_edl", ctx=ctx, stage="edl"):
        from interview_mux.order_hash import copy_order_lock, stamp_order_hash
        from interview_mux.ideal_cuts import load_air_bound_inputs

        ideal_cuts_doc, transcript_words = load_air_bound_inputs(ctx)
        wav_path = None
        try:
            wav_path = ctx.read_path("ingest", "normalized.wav")
        except Exception:
            wav_path = None
        edl = build_flow1_edl(
            selection=selection,
            segments_by_id=by_id,
            gap_report=gap_report,
            transitions=transitions,
            resolve_vo_path=lambda line: resolve_vo_pickup_path(ctx, line),
            vo_relpath=lambda p: vo_pickup_relpath(ctx, p),
            resolve_transition_path=lambda a, b: resolve_transition_wav(ctx, a, b),
            ideal_cuts=ideal_cuts_doc,
            transcript_words=transcript_words,
            normalized_wav=wav_path,
            ctx=ctx,
        )
        verify_report = edl.pop("_clone_adjacency_verify", None)
        if isinstance(verify_report, dict):
            from interview_mux.clone_adjacency_verify import persist_clone_adjacency_verify

            persist_clone_adjacency_verify(ctx, verify_report)
        edl = copy_order_lock(selection, stamp_order_hash(edl))
        try:
            from interview_mux.gap_vo_gates import gap_framing_enabled
            from interview_mux.opening_orientation import (
                is_episode_orientation,
                orientation_omitted,
                validate_opening_orientation,
            )

            active_framing_lines = [
                line
                for line in ((gap_report or {}).get("interviewer_lines") or [])
                if isinstance(line, dict) and not line.get("skipped_optional")
            ]
            if gap_framing_enabled(ctx) and (
                active_framing_lines
                or orientation_omitted(gap_report if isinstance(gap_report, dict) else None)
            ):
                from interview_mux.gates import vo_gap_line_effectively_optional
                from interview_mux.spoken_copy_guard import script_hash

                missing = set((edl.get("warnings") or {}).get("missing_vo_files") or [])
                for line in active_framing_lines:
                    if not is_episode_orientation(line):
                        continue
                    # Omitted / optional orientation must not hard-fail EDL when the
                    # WAV was intentionally not seated (exec_11130 preface wipe).
                    if vo_gap_line_effectively_optional(ctx, line):
                        continue
                    lid = str(line.get("line_id") or "")
                    if lid in missing:
                        raise RuntimeError(
                            f"edl: orientation WAV stale/missing (line_id={lid} "
                            f"script_hash={script_hash(str(line.get('text') or ''))})"
                        )
                opening_errors = validate_opening_orientation(
                    gap_report=gap_report if isinstance(gap_report, dict) else None,
                    edl=edl,
                )
                opening_errors = _blocking_orientation_errors(ctx, opening_errors)
                if opening_errors:
                    raise RuntimeError(
                        "edl: opening orientation contract failed: "
                        + "; ".join(opening_errors)
                    )
        except ImportError:
            pass

    warnings = edl.get("warnings") or {}
    if warnings.get("missing_segment_lookups"):
        missing = sorted(set(warnings["missing_segment_lookups"]))
        raise RuntimeError(
            f"edl: ordered_segment_ids missing from manifest/NLE lookup: {missing}"
        )
    if warnings.get("missing_vo_files"):
        missing = sorted(set(warnings["missing_vo_files"]))
        skipped_ids: set[str] = set()
        if gap_report:
            from interview_mux.gates import vo_gap_line_effectively_optional

            for line in gap_report.get("interviewer_lines") or []:
                if not isinstance(line, dict):
                    continue
                if not (line.get("skipped_optional") or vo_gap_line_effectively_optional(ctx, line)):
                    continue
                skipped_ids.add(str(line.get("line_id") or ""))
                skipped_ids.add(str(line.get("targets_segment_id") or ""))
        missing = [mid for mid in missing if mid not in skipped_ids]
        if missing:
            raise RuntimeError(f"edl: gap VO lines missing WAV: {missing}")
    if warnings.get("gap_targets_not_in_selection"):
        ctx.log(
            f"EDL: gap targets not in selection order: "
            f"{warnings['gap_targets_not_in_selection']}",
            level="warning",
            stage="edl",
        )
    if warnings.get("vo_lines_unseated_unplayable_target"):
        ctx.log(
            "EDL: before-VO omitted with its unplayable target: "
            f"{warnings['vo_lines_unseated_unplayable_target']} "
            f"(omitted segments {edl.get('omitted_unplayable_segment_ids')})",
            level="warning",
            stage="edl",
        )

    vo_n = edl.get("vo_pickup_clip_count", 0)
    ctx.log(
        f"EDL built: {len(edl.get('clips') or [])} events, "
        f"{vo_n} vo_pickup, timeline {edl.get('timeline_duration_ms')} ms "
        f"(mix: speech + VO + SDP overlays)",
        level="success",
        stage="edl",
    )
    from interview_mux.transition_vo import lint_edl_vo_source_paths

    edl = lint_edl_vo_source_paths(ctx, edl)
    assert_spoken_transitions_audible(ctx, edl)
    check_edl_qc(ctx, stage="edl", edl=edl, strict=True, gap_report=gap_report)
    check_edl_narrative_qc(ctx, stage="edl", edl=edl)

    with logged_step("edl/validate_write", ctx=ctx, stage="edl"):
        edl_errors = validate_edl(edl)
        if edl_errors:
            for err in edl_errors:
                ctx.log(
                    f"master/edl.json: {err}",
                    level="error",
                    stage="edl",
                )
            raise SystemExit(
                f"edl: edl.json failed schema validation ({len(edl_errors)} error(s))"
            )
        from interview_mux.air_order import write_live_edl

        write_live_edl(ctx, edl, source="edl")
        # S1 (vo_synthesize peel): EDL owns live source_path bind after WAVs exist.
        try:
            from interview_mux.transition_vo import restamp_edl_transition_source_paths

            restamp_edl_transition_source_paths(ctx)
        except Exception as exc:
            ctx.log(
                f"edl: transition source restamp incomplete: {exc}",
                level="warning",
                stage="edl",
            )
        try:
            restamp_edl_vo_pickup_source_paths(ctx)
        except Exception as exc:
            ctx.log(
                f"edl: VO pickup source restamp incomplete: {exc}",
                level="warning",
                stage="edl",
            )
        try:
            from interview_mux.gap_vo_gates import gap_framing_enabled
            from interview_mux.opening_orientation import (
                orientation_omitted,
                validate_opening_orientation,
            )

            if gap_framing_enabled(ctx) and isinstance(gap_report, dict):
                active_framing_lines = [
                    line
                    for line in (gap_report.get("interviewer_lines") or [])
                    if isinstance(line, dict) and not line.get("skipped_optional")
                ]
                if active_framing_lines or orientation_omitted(gap_report):
                    opening_errors = validate_opening_orientation(
                        gap_report=gap_report,
                        edl=edl,
                    )
                    opening_errors = _blocking_orientation_errors(ctx, opening_errors)
                    if opening_errors:
                        raise RuntimeError(
                            "edl: opening orientation contract failed at stage_done: "
                            + "; ".join(opening_errors)
                        )
        except ImportError:
            pass

    with logged_step("edl/assembly_ledger", ctx=ctx, stage="edl"):
        from interview_mux.assembly_ledger import (
            assert_ledger_no_naked_seams,
            write_assembly_ledger,
        )

        ledger = write_assembly_ledger(ctx, edl=edl)
        if not soft:
            assert_ledger_no_naked_seams(ledger)
        elif ledger.get("naked_seam_count"):
            ctx.log(
                f"assembly_ledger: {ledger.get('naked_seam_count')} naked seam(s) "
                "(NLE soft — operator must repair)",
                level="warning",
                stage="edl",
            )
    try:
        from interview_mux.publishability_boundary import checkpoint_publishability

        checkpoint_publishability(ctx, checkpoint="post_edl")
    except Exception as exc:
        from interview_mux.publishability_boundary import PublishabilityBlocked

        if isinstance(exc, PublishabilityBlocked):
            raise
        ctx.log(
            f"edl: publishability checkpoint skipped: {exc}",
            level="warning",
            stage="edl",
        )
    try:
        from interview_mux.transition_vo import stamp_transitions_pair_freeze

        stamp_transitions_pair_freeze(ctx)
    except Exception as freeze_exc:
        ctx.log(
            f"edl: transitions pair freeze skipped: {freeze_exc}",
            level="warning",
            stage="edl",
        )
    try:
        from interview_mux.thrash_hardening import bump_assembly_seating_generation

        bump_assembly_seating_generation(ctx, "edl_rewrite")
    except Exception:
        pass
    # Expanded WS2 O17: never stamp edl done while G1/VO incompleteness is open.
    try:
        from interview_mux.stage_completion import (
            heal_or_refuse_mark,
            stage_artifact_incompleteness,
        )

        inc = stage_artifact_incompleteness(ctx, "edl")
        if inc and ("g1_open" in inc or "vo_synthesize incomplete" in inc):
            raise SystemExit(f"edl refuse mark_done: {inc}")
        out = heal_or_refuse_mark(ctx, "edl", force=True)
        if out.get("refused"):
            raise SystemExit(
                f"edl refuse mark_done: {out.get('reason') or 'incompleteness'}"
            )
    except SystemExit:
        raise
    except Exception:
        ctx.log(
            "edl: mark_done path failed — refusing hollow done",
            level="error",
            stage="edl",
        )
        raise
    try:
        from interview_mux.delivery_guardrails import freeze_air_order

        freeze_air_order(ctx, reason="edl_commit")
    except Exception:
        pass


def run_mix(ctx: RunContext) -> Path:
    """Flow 1 assembly mix — speech + VO + SDP overlays (canonical stage id)."""
    from interview_mux.air_order import assert_consumer
    from interview_mux.llm_flow_hardening import require_spend_artifacts_complete
    from interview_mux.sound_design import mix

    require_spend_artifacts_complete(ctx, "mix")
    assert_consumer(ctx, "mix")

    try:
        from interview_mux.junction_snip_qa import refuse_mix_if_live_incomplete_cuts

        refuse_mix_if_live_incomplete_cuts(ctx)
    except Exception as exc:
        from interview_mux.loud_fail import LoudStageFailure

        if isinstance(exc, LoudStageFailure):
            raise
        ctx.log(
            f"mix: incomplete-cut preflight skipped: {exc}",
            level="warning",
            stage="mix",
        )

    try:
        from interview_mux.publishability_boundary import checkpoint_publishability

        checkpoint_publishability(ctx, checkpoint="pre_mix")
    except Exception as exc:
        from interview_mux.publishability_boundary import PublishabilityBlocked

        if isinstance(exc, PublishabilityBlocked):
            raise
        ctx.log(
            f"mix: publishability checkpoint skipped: {exc}",
            level="warning",
            stage="mix",
        )

    from interview_mux.listen_quality import place_episode_close_cue
    from interview_mux.theme_slot_integrity import assert_theme_bookends_ready_for_mix

    # Mix may rebind outro anchors only — never invent a new outro cue.
    try:
        placed = place_episode_close_cue(ctx, allow_create=False)
        if placed:
            ctx.log(
                f"mix: rebound episode_close cue via {placed}",
                level="info",
                stage="mix",
            )
    except Exception as exc:
        ctx.log(f"mix: episode_close rebind skipped: {exc}", level="warning", stage="mix")
    assert_theme_bookends_ready_for_mix(ctx)

    # Restore or generate MMAudio QA before mix loud-fail.
    try:
        from interview_mux.delivery_recovery import ensure_mmaudio_qa_before_mix

        qa_state = ensure_mmaudio_qa_before_mix(ctx, run_if_missing=True)
        if not qa_state.get("ok"):
            from interview_mux.stage_resilience import escalate_stage_failure

            escalate_stage_failure(
                ctx,
                "mix",
                failed_invariant="mmaudio_qa_missing",
                evidence=qa_state,
            )
    except Exception:
        pass

    if ctx.artifact_exists("master/assembly_ledger.json"):
        from interview_mux.assembly_ledger import assert_ledger_no_naked_seams

        ledger = ctx.read_json("master/assembly_ledger.json")
        if isinstance(ledger, dict):
            assert_ledger_no_naked_seams(ledger)

    check_edl_qc(ctx, stage="mix", strict=False)

    # S4: refuse missing transition WAVs — do not commit/synth at mix.
    try:
        from interview_mux.transition_vo import current_transition_pairs_missing

        pre_missing = current_transition_pairs_missing(ctx)
        from interview_mux.transition_vo import current_pair_wav_usable

        playable: list[str] = []
        for pair in pre_missing:
            after_id, _, before_id = str(pair).partition("->")
            if after_id and before_id and current_pair_wav_usable(ctx, after_id, before_id):
                continue
            playable.append(pair)
        pre_missing = playable
    except Exception as exc:
        ctx.log(f"mix: transition pair preflight skipped: {exc}", level="warning", stage="mix")
        pre_missing = []
    if pre_missing:
        from interview_mux.loud_fail import raise_loud_failure

        raise_loud_failure(
            ctx,
            "mix: transition pairs missing WAV — resume vo_synthesize: "
            + ", ".join(pre_missing[:8]),
            stage="mix",
            reason="missing_transition_wav",
            detail={"missing_pairs": pre_missing[:12], "resume": "vo_synthesize"},
        )

    with logged_step("mix/render", ctx=ctx, stage="mix"):
        out = mix(ctx)
    # S5: seat assert only — HAU remaster clear / speech-first stamp at boundary.
    _seat_after_mix(ctx)
    return out


def _seat_after_mix(ctx: RunContext) -> None:
    """S5: seating bookkeeping after render — not craft heal."""
    try:
        from interview_mux.air_order import mix_outputs_seated
        from interview_mux.mix_junction_seat import (
            clear_remaster,
            note_speech_first_mix,
        )

        if mix_outputs_seated(ctx):
            clear_remaster(ctx)
            try:
                from interview_mux.delivery_guardrails import music_epoch_complete

                if not music_epoch_complete(ctx):
                    note_speech_first_mix(ctx)
            except Exception:
                note_speech_first_mix(ctx)
    except Exception:
        pass


def run_mux(ctx: RunContext) -> Path:
    """Backward-compatible alias for mix (v1 pipeline stage id)."""
    assembly = run_mix(ctx)
    ctx.mark_done("mux_flow1")
    return assembly


def run_preview(ctx: RunContext) -> Path:
    """Build Flow 1 assembly preview: speech + recorded VO pickup, no SFX."""
    edl = ctx.read_json("master/edl.json")
    # Heal VO clips that lost source_path (sanitize stripped nulls / seating race).
    healed_ids: list[str] = []
    for clip in edl.get("clips") or []:
        if not isinstance(clip, dict):
            continue
        path = heal_vo_pickup_clip_source(ctx, clip)
        if path is not None:
            healed_ids.append(str(clip.get("line_id") or path.name))
    if healed_ids:
        ctx.log(
            "assembly_preview: healed vo_pickup source_path for "
            + ", ".join(healed_ids[:12]),
            level="warning",
            stage="assembly_preview",
        )
        # C13: after assembly exists, EDL heal writeback that forces remaster needs allow.
        allow_write = True
        try:
            asm = ctx.final_path("master", "assembly.wav")
            if asm.is_file() and asm.stat().st_size > 0:
                from interview_mux.timeline_reopen_meta_gate import (
                    INTENT_PUB_SOFT_MIX,
                    decide_timeline_reopen,
                )

                gate = decide_timeline_reopen(
                    ctx,
                    intent=INTENT_PUB_SOFT_MIX,
                    detail={
                        "from_stage": "assembly_preview",
                        "healed_line_ids": healed_ids[:12],
                    },
                )
                allow_write = bool(gate.get("allow"))
                if not allow_write:
                    ctx.log(
                        f"assembly_preview: EDL heal write refused ({gate.get('refuse_reason')})",
                        level="info",
                        stage="assembly_preview",
                    )
        except Exception:
            allow_write = False  # c13 fail-closed
        if allow_write:
            try:
                from interview_mux.write_staging import write_committed_json

                write_committed_json(
                    ctx, "master/edl.json", edl, stage_key="assembly_preview"
                )
            except Exception as exc:
                ctx.log(
                    f"assembly_preview: could not persist healed EDL: {exc}",
                    level="warning",
                    stage="assembly_preview",
                )

    refuse = None
    try:
        from interview_mux.stage_completion import assembly_preview_heard_wav_refuse

        refuse = assembly_preview_heard_wav_refuse(ctx, edl if isinstance(edl, dict) else None)
    except Exception:
        refuse = None
    if refuse:
        raise RuntimeError(refuse)

    source = ctx.read_path("ingest", "normalized.wav")
    work = ctx.path("master", "_preview_clips")
    work.mkdir(parents=True, exist_ok=True)
    clip_paths: list[Path] = []

    with logged_step("assembly_preview/render_clips", ctx=ctx, stage="assembly_preview"):
        for i, clip in enumerate(edl.get("clips") or []):
            ctype = clip.get("type")
            out = work / f"clip_{i:04d}.wav"
            if ctype == "speech":
                start = max(0, float(clip.get("source_start_ms", 0)) / 1000.0)
                end = max(start, float(clip.get("source_end_ms", 0)) / 1000.0)
                run_command(
                    [
                        "ffmpeg",
                        "-y",
                        "-i",
                        str(source),
                        "-ss",
                        str(start),
                        "-to",
                        str(end),
                        "-vn",
                        "-ac",
                        "1",
                        "-ar",
                        "48000",
                        "-c:a",
                        "pcm_s16le",
                        str(out),
                    ],
                    stage="assembly_preview",
                    label=f"ffmpeg speech clip {i}",
                    capture_output=True,
                )
                clip_paths.append(out)
                continue

            if ctype == "silence":
                pad_ms = max(0, int(clip.get("duration_ms") or 0))
                if pad_ms <= 0:
                    continue
                run_command(
                    [
                        "ffmpeg",
                        "-y",
                        "-f",
                        "lavfi",
                        "-i",
                        f"anullsrc=r=48000:cl=mono",
                        "-t",
                        f"{pad_ms / 1000.0:.3f}",
                        "-c:a",
                        "pcm_s16le",
                        str(out),
                    ],
                    stage="assembly_preview",
                    label=f"ffmpeg silence clip {i}",
                    capture_output=True,
                )
                clip_paths.append(out)
                continue

            if ctype not in {"vo_pickup", "transition"}:
                continue

            src_rel = clip.get("source_path")
            if not src_rel:
                text = str(clip.get("text") or "").strip()
                try:
                    dur = int(clip.get("duration_ms") or 0)
                except (TypeError, ValueError):
                    dur = 0
                if ctype == "transition" and not text and dur <= 0:
                    continue
                raise RuntimeError(
                    "heard_wav_flow — resume vo_synthesize: "
                    f"assembly_preview: {ctype} clip {clip.get('line_id') or i} missing source_path"
                )
            vo_src = ctx.read_path(src_rel)
            if not vo_src.is_file():
                raise FileNotFoundError(f"assembly_preview: {ctype} clip missing WAV: {src_rel}")
            run_command(
                [
                    "ffmpeg",
                    "-y",
                    "-i",
                    str(vo_src),
                    "-vn",
                    "-ac",
                    "1",
                    "-ar",
                    "48000",
                    "-c:a",
                    "pcm_s16le",
                    str(out),
                ],
                stage="assembly_preview",
                label=f"ffmpeg {ctype} clip {i}",
                capture_output=True,
            )
            clip_paths.append(out)

    if not clip_paths:
        raise SystemExit(
            "assembly_preview: no renderable speech/VO clips found in edl output."
        )

    from interview_mux.audio_timeline import concat_clips_with_crossfade
    from interview_mux.config import merged_config
    from interview_mux.sound_design import load_audio

    crossfade_ms = int((merged_config().get("mix") or {}).get("crossfade_ms_assembly_preview", 80))
    with logged_step("assembly_preview/concat_export", ctx=ctx, stage="assembly_preview"):
        clips = [load_audio(p) for p in clip_paths]
        preview_audio = concat_clips_with_crossfade(clips, crossfade_ms)
        preview_path = ctx.path("master", "assembly_preview.wav")
        preview_audio.export(str(preview_path), format="wav")
    ctx.log(
        f"Assembly preview ready (speech + VO, crossfade_ms={crossfade_ms}, clips={len(clips)}) — listen before MMAudio SFX generation.",
        level="success",
        stage="assembly_preview",
        detail=str(preview_path),
    )
    marked = False
    try:
        from interview_mux.stage_completion import heal_or_refuse_mark

        out = heal_or_refuse_mark(ctx, "assembly_preview")
        if out.get("refused"):
            raise RuntimeError(
                str(out.get("reason") or "heard_wav_flow — resume vo_synthesize: preview incomplete")
            )
        marked = bool(out.get("marked"))
        if not marked:
            try:
                from interview_mux.delivery_guardrails import seed_stage_complete

                marked = bool(seed_stage_complete(ctx, "assembly_preview"))
            except Exception:
                marked = False
    except RuntimeError:
        raise
    except Exception:
        marked = False
    if not marked:
        ctx.mark_done("assembly_preview")
    return preview_path

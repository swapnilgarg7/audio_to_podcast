"""Artifact ownership constitution — single ALLOW/DENY SSOT for persist + heal.

Every persist, mark_done, unmark, pending promote, and heal resume consults this
module. Existing tables (DISK_PATHS, HOT, PRODUCER_PIN_TABLE) are drift-checked
or derived against the registry. See docs/cross-cutting/artifact-ownership.md.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any, Iterable, Literal

WriteMode = Literal[
    "one_writer",
    "air_contract",
    "staged",
    "committed_ok",
    "binary",
    "operational",
]
Role = Literal[
    "producer",
    "sanitize",
    "repair",
    "heal",
    "gui",
    "driver",
    "ops",
]
Verb = Literal[
    "persist",
    "mark_done",
    "unmark",
    "invalidate",
    "promote_pending",
    "execute",
]
Epoch = Literal[
    "pre_soft_freeze",
    "soft_freeze",
    "hard_freeze",
    "edl_sealed",
    "mix_seated",
    "junction_committed",
    "operational",
    "",
]

# Flip after Wave 0–3 canaries green. Env override for archaeology only.
_ENV_FAIL_CLOSED = "INTERVIEW_MUX_ARTIFACT_OWNERSHIP_FAIL_CLOSED"
# Default true once canaries ship in the same change-set.
_FAIL_CLOSED_DEFAULT = True

# Mechanical segment-id remap surface (mirror of
# ``segment_id_remap.SHARED_REMAP_RELS`` — parity is asserted in tests). A fuse /
# overlap-union pass must be able to retire a consumed ``seg_*`` from every doc
# that references it, in any epoch: this is referential integrity, never
# authoring. Only the walker's own stages get these ALLOW rows.
SEGMENT_ID_REMAP_PATHS: frozenset[str] = frozenset(
    {
        "understanding/content_brief.json",
        "understanding/talking_points.json",
        "understanding/ideal_cuts.json",
        "understanding/ideal_cuts_materialized.json",
        "understanding/ideal_cuts_selection_seed.json",
        "analysis/low_conf_must_keep.json",
        "analysis/high_value_speech_boosts.json",
        "analysis/high_value_speech_islands.json",
        "analysis/stt_lexicon_island_boosts.json",
        "segments/nle_edits.json",
        "operator/must_keep.json",
        "understanding/analysis_state.json",
        "understanding/investigation_queue.json",
        "understanding/context_index.json",
        "understanding/omit_ledger.json",
        "understanding/gap_report.json",
        "understanding/gap_evaluations.json",
        "understanding/nugget_layup_plan.json",
        "understanding/episode_structure.json",
        "vo_pickup/synthesis_report.json",
        "master/selection.json",
        "master/narrative_plan.json",
        "master/transitions.json",
        "master/coverage_audit.json",
        "master/edl.json",
        "transcripts/index.json",
        "analysis/low_conf_islands.json",
        "analysis/low_conf_density_ranking.json",
        "analysis/high_value_island_clusters.json",
        "analysis/vernacular_must_keep.json",
        "vernacular/resplit_report.json",
        "transcript/protected_zones.json",
        "understanding/sonic_context.json",
        "understanding/soundscape_policy.json",
        "understanding/gap_framing_plan.json",
        "understanding/sound_design_plan.json",
        "mastering/mastering_plan.json",
    }
)
# Fuse + overlap repair + chapter-close hitch all retire absorbed seg_* ids
# across SHARED remaps (hitch remaps keepers onto surviving ids — exec_13157).
SEGMENT_ID_REMAP_STAGES: tuple[str, ...] = (
    "edl_overlap_repair",
    "connector_fuse_pass",
    "connector_fuse_pass_pre_ranking",
    "chapter_close_hitch",
)

MATRIX_VERSION_META_KEY = "artifact_ownership_matrix_version"
AUTHORITY_DENIED_FP_PREFIX = "authority_denied"

# S9: gap_report interviewer_lines[].text — framing pre-authority; layup sole
# post-compose / post-authority body writer.
GAP_REPORT_REL = "understanding/gap_report.json"
GAP_REPORT_BODY_WRITERS: frozenset[str] = frozenset(
    {"gap_framing_compose", "nugget_layup_compose"}
)
GAP_REPORT_SOLE_BODY_WRITER = "nugget_layup_compose"


@dataclass(frozen=True)
class ArtifactRow:
    path: str
    producers: tuple[str, ...]
    authoritative: str
    write_mode: WriteMode = "staged"
    fields: tuple[str, ...] = ()
    heal_pin: str = ""
    end_family: str = "analysis"
    foreign_roles_pin_only: tuple[Role, ...] = (
        "heal",
        "repair",
        "sanitize",
        "gui",
        "driver",
    )


@dataclass(frozen=True)
class AllowRow:
    path: str
    stage: str = ""
    role: Role | str = "producer"
    fields: tuple[str, ...] = ()
    epochs: tuple[str, ...] = ()  # empty = any
    write_mode: WriteMode = "staged"
    allowed_mutations: tuple[str, ...] = ()
    verb: Verb = "persist"


@dataclass(frozen=True)
class DenyRow:
    path: str  # exact or glob (* and **)
    stage: str = ""
    role: Role | str = ""
    fields: tuple[str, ...] = ()
    epochs: tuple[str, ...] = ()
    verb: Verb = "persist"
    reason: str = ""


@dataclass(frozen=True)
class FreezeWritePolicyRow:
    """Named mutation classes that remain legal at each seat-freeze epoch."""

    stage: str
    mutation_class: str
    epochs: tuple[str, ...]


# Freeze policy is deliberately stage/action based: paths can be co-written, but
# the reason for a post-freeze write determines whether it is safe. Metadata
# alignment and transition shrinkage do not mint VO seats; compose/host-copy
# rewrites stop once the seat contract freezes.
FREEZE_WRITE_POLICY: tuple[FreezeWritePolicyRow, ...] = (
    FreezeWritePolicyRow(
        "edl_narrative_audit",
        "narrative_metadata_align",
        ("pre_soft_freeze", "soft_freeze", "hard_freeze"),
    ),
    FreezeWritePolicyRow(
        "edl_narrative_audit",
        "narrative_host_repair",
        ("pre_soft_freeze",),
    ),
    FreezeWritePolicyRow(
        "gap_framing_compose",
        "compose_copy",
        ("pre_soft_freeze", "soft_freeze"),
    ),
    FreezeWritePolicyRow(
        "transitions",
        "transition_dedupe",
        ("pre_soft_freeze", "soft_freeze", "hard_freeze"),
    ),
    FreezeWritePolicyRow(
        "transitions",
        "transition_repair",
        ("pre_soft_freeze", "soft_freeze", "hard_freeze"),
    ),
    # Ranking CTA recut seats split children on the manifest so lattice/orphan
    # checks see real rows (exec_13168/13170 authority_denied under ranking).
    FreezeWritePolicyRow(
        "full_master_ranking",
        "cta_child_materialize",
        ("pre_soft_freeze", "soft_freeze"),
    ),
)


class AuthorityDenied(PermissionError):
    """Illegal write / mutation under the ownership constitution."""

    def __init__(
        self,
        message: str,
        *,
        path: str = "",
        stage_key: str = "",
        role: str = "",
        epoch: str = "",
        suggested_owner: str = "",
        verb: str = "persist",
        reason: str = "",
    ) -> None:
        super().__init__(message)
        self.path = path
        self.stage_key = stage_key
        self.role = role
        self.epoch = epoch
        self.suggested_owner = suggested_owner
        self.verb = verb
        self.reason = reason

    @property
    def foreign(self) -> bool:
        """The writer has no row for this path at all (a side effect, ISSUES 127)."""
        return self.verb == "persist" and is_foreign_refusal(self.reason)

    def fingerprint(self) -> str:
        return (
            f"{AUTHORITY_DENIED_FP_PREFIX}:{self.verb}:{self.path}:"
            f"{self.stage_key}:{self.epoch}:{self.suggested_owner}"
        )


# ---------------------------------------------------------------------------
# L1 catalog — primary path per live stage (+ multi-output secondaries)
# ---------------------------------------------------------------------------

def _row(
    path: str,
    *producers: str,
    mode: WriteMode = "staged",
    heal: str = "",
    end: str = "analysis",
    fields: tuple[str, ...] = (),
) -> ArtifactRow:
    prods = tuple(str(p) for p in producers if p)
    auth = prods[-1] if prods else ""
    return ArtifactRow(
        path=path,
        producers=prods,
        authoritative=auth,
        write_mode=mode,
        fields=fields,
        heal_pin=heal or auth,
        end_family=end,
    )


# Ordered preferred rewriter is last in producers (shared paths).
_CATALOG_SEED: tuple[ArtifactRow, ...] = (
    # Prepare
    _row("preclean/isolated.wav", "audio_preclean", mode="binary", end="ops"),
    _row("preclean/provider.json", "audio_preclean", mode="operational", end="ops"),
    _row("preclean/lineage.json", "audio_preclean", mode="operational", end="ops"),
    _row("preclean/skip.json", "audio_preclean", mode="operational", end="ops"),
    _row("ingest/normalized.wav", "ingest", mode="binary", end="ops"),
    _row("transcript/full.json", "transcribe", end="ops"),
    _row("transcript/review_queue.json", "transcript_review_build", end="ops"),
    # Analysis
    _row("analysis/run_golden_facts.json", "audio_probe_build"),
    _row("understanding/interview_spine.json", "interview_spine_build"),
    _row("understanding/speakers.json", "speaker_roles"),
    _row("understanding/source_acoustic_profile.json", "source_acoustic_profile"),
    _row(
        "understanding/source_topology.json",
        "source_topology_build",
    ),
    # source_topology_build authors the base doc; missing_framing / G-Framing
    # mutates operator_overrides (gap_framing_enabled) via gap_vo_gates
    # (exec_13157 AuthorityDenied under missing_framing). chapter_close_hitch
    # may auto-confirm pickup during inner walk without spoofing MF ownership.
    _row(
        "understanding/flow_adaptation.json",
        "source_topology_build",
        "missing_framing",
        "chapter_close_hitch",
        mode="committed_ok",
    ),
    _row(
        "understanding/content_brief.json",
        "content_context",
        "content_brief_reanchor",
        mode="committed_ok",
    ),
    _row("understanding/talking_points.json", "talking_points_compose"),
    _row("understanding/ideal_cuts.json", "ideal_cuts_propose"),
    _row("understanding/ideal_cuts_materialized.json", "ideal_cuts_materialize"),
    # ideal_cuts_materialize publishes the segment contract itself when
    # ``analysis.ideal_cuts.bind_mode`` binds boundaries (default ``both``) —
    # boundary_detection then skips its LLM. That publication is stamped
    # ``_meta.segment_contract.publisher_stage=ideal_cuts_materialize`` and read
    # back by ``ideal_cuts.boundaries_already_from_ideal_cuts``, so it is a first
    # class co-producer, not a stray write. boundary_detection stays first: it is
    # the full-tape resume target when the bind is demoted.
    _row(
        "segments/boundaries.json",
        "boundary_detection",
        "ideal_cuts_materialize",
        "connector_fuse_pass",
        "connector_fuse_pass_pre_ranking",
        "boundary_topic_resplit",
        # chapter_close_hitch remints the contract after the hitch remap
        # (`chapter_close_hitch.py` persist of BOUNDARIES_REL). Keep it a
        # co-producer, not last: edl_overlap_repair stays the overlap-union
        # rewriter, and boundary_detection stays first (full-tape resume).
        "chapter_close_hitch",
        # Same overlap union pass keeps boundaries coherent with the merged row.
        "edl_overlap_repair",
        mode="committed_ok",
    ),
    # Manifest is rewritten by sanitize / fuse passes after classification.
    # Keep segment_classification last (authoritative); earlier entries are
    # co-producers (exec_11871 vernacular + connector_fuse DENY thrash).
    _row(
        "segments/manifest.json",
        "vernacular_segment_sanitize",
        "low_conf_island_scan",
        "connector_fuse_pass",
        "connector_fuse_pass_pre_ranking",
        # Delivery-time overlap union (two speech clips sharing source tape) must
        # seat its survivor row on the manifest — exec_11871 aborted junction's
        # commitment remaster on this DENY.
        "edl_overlap_repair",
        # Ranking CTA/NLE recut materializes split children so they are not
        # orphan-dropped (exec_13168/13170 authority_denied under ranking).
        "full_master_ranking",
        "segment_classification",
        end="A",
    ),
    _row("understanding/framing_posture_decision.json", "framing_posture_decide"),
    _row("vernacular/resplit_report.json", "vernacular_segment_sanitize"),
    _row("analysis/low_conf_islands.json", "low_conf_island_scan"),
    _row(
        "analysis/connector_fuse_audit.json",
        "connector_fuse_pass",
        # Pre-ranking apply_connector_fuses appends the same audit trail
        # (exec_13157 AuthorityDenied when only post_sanitize owned the path).
        "connector_fuse_pass_pre_ranking",
    ),
    # Analysis fuse owns shared rounds; pre_ranking has a dedicated SSOT (S1).
    _row(
        "analysis/connector_fuse_rounds.json",
        "connector_fuse_pass",
    ),
    _row(
        "analysis/connector_fuse_rounds_pre_ranking.json",
        "connector_fuse_pass_pre_ranking",
    ),
    _row("understanding/sonic_context.json", "sonic_context_build"),
    _row(
        "understanding/sound_design_plan.json",
        "sound_design_palettes",
        "sound_design_plan",
        # music_palette_compose seats the real music asset ids / cue arrangement
        # into the SDP body (exec_11871 authority_denied under edl_sealed).
        "music_palette_compose",
        "sfx_prompt_craft",
        "sound_design_vo_finalize",
        mode="one_writer",
        end="B",
    ),
    _row("mastering/research/routing.json", "mastering_research_routing"),
    _row("mastering/research/waves.json", "mastering_research_waves"),
    _row("mastering/research/rollup.json", "mastering_research_rollup"),
    # Per-field wave notes (preclean_lineage.json, …) under research/ — operational
    # sidecars written by mastering_research_waves (exec_11871 unknown_path).
    _row(
        "mastering/research/*.json",
        "mastering_research_routing",
        "mastering_research_waves",
        "mastering_research_rollup",
        mode="operational",
    ),
    _row("mastering/shape/agenda.json", "mastering_shape_agenda"),
    _row("mastering/shape/candidates.json", "mastering_shape_candidates"),
    _row(
        "mastering/mastering_plan.json",
        "mastering_plan_synthesize",
        "mastering_plan_confirm",
        "information_package_plan",
        "nugget_layup_compose",
        "air_script_compose",
        "air_script_seams",
        "air_contract_sanitize",
        mode="air_contract",
        end="A",
    ),
    _row(
        "understanding/gap_evaluations.json",
        "missing_framing",
        end="C",
    ),
    _row(
        "understanding/gap_report.json",
        # S9: body (interviewer_lines[].text) sole writers — framing pre-authority,
        # layup post-compose (authoritative). Omit/delivery stampers are AllowRows.
        "gap_framing_compose",
        "nugget_layup_compose",
        mode="one_writer",
        end="A",
        fields=(
            "interviewer_lines[].text",
            "interviewer_lines[].skipped_optional",
            "interviewer_lines[].air_script_omit",
            "interviewer_lines[].delivery",
            "interviewer_lines[].voice_speaker_id",
        ),
    ),
    _row(
        "understanding/omit_ledger.json",
        "nugget_layup_compose",
        "air_script_compose",
        "air_script_seams",
        "air_contract_sanitize",
        mode="air_contract",
        heal="air_contract_sanitize",
        end="A",
    ),
    _row("understanding/delivery_brief.json", "delivery_brief_build"),
    _row(
        "understanding/soundscape_policy.json",
        "soundscape_policy_build",
        # music_palette_compose injects bed cue_slots when seating real cues (SDP invent peeled).
        "music_palette_compose",
    ),
    _row("understanding/episode_structure.json", "episode_structure_compose"),
    # Compact digest of episode_structure.json (plain text, LX-03 / LLM-volley
    # attach). Three writers, all legitimate: sound_design_plan refreshes after
    # ranking, chapter_close_hitch realigns after the hitch remap, and
    # episode_structure_compose is the canonical minter — so it stays last.
    _row(
        "understanding/episode_structure_compact.txt",
        "sound_design_plan",
        "chapter_close_hitch",
        "episode_structure_compose",
        end="C",
    ),
    # Delivery
    _row("master/coverage_audit.json", "topic_coverage_audit", end="C"),
    _row("master/narrative_plan.json", "narrative_arc_plan", end="C"),
    _row("mastering/chapter_close_hitch.json", "chapter_close_hitch", end="C"),
    _row(
        "master/selection.json",
        "full_master_ranking",
        "selection_order_sanitize",
        "selection",  # legacy stage_key used by air_order_boundary / ranking helpers
        # Junction is the delivery-time cut authority: an incomplete cut that is
        # unrecoverable within its clip is resolved by fuse-into-neighbor or omit,
        # which drops the segment from selection (F5 fixtures assert this, and
        # exec_11871 stalled on the denied omit).
        # S7: nugget_layup_compose removed — CTA / never_touch omit is ranking+sanitize.
        "junction_snip_qa",
        # SFA-S2 / lattice #6: framing VO-cover excludes land here via
        # selection_framing_apply → commit_selection_mutation only.
        "selection_framing_apply",
        mode="one_writer",
        end="A",
    ),
    _row("understanding/nugget_corpus.json", "nugget_corpus_mine", end="C"),
    _row(
        "mastering/shape/information_packages_audit.json",
        "information_package_plan",
        end="C",
    ),
    _row(
        "understanding/nugget_layup_plan.json",
        "gap_framing_recompose",
        "nugget_layup_compose",
        mode="one_writer",
        end="C",
    ),
    _row("understanding/refinement_agenda.json", "refinement_agenda", end="C"),
    _row(
        "understanding/gap_framing_recompose.json",
        "gap_framing_recompose",
        end="C",
    ),
    _row(
        "understanding/selection_framing_apply.json",
        "selection_framing_apply",
        end="C",
    ),
    _row(
        "master/transitions.json",
        "transitions",
        mode="one_writer",
        end="C",
    ),
    # Disk-grounded selected-adjacency occupancy. The audit builds it immediately
    # before its LLM volley; transitions refreshes it under soft/hard seat freeze.
    _row(
        "master/seam_occupancy.json",
        "edl_narrative_audit",
        "transitions",
        end="C",
    ),
    _row(
        "master/transitions/*.wav",
        "transitions",
        "vo_synthesize",
        "edl",
        "mix",
        "junction_snip_qa",
        mode="binary",
        end="C",
    ),
    _row(
        "understanding/vo_line_adjudication.json",
        "vo_line_adjudicate",
        end="B",
    ),
    _row(
        "mastering/vo_synthesize.json",
        "vo_synthesize",
        mode="staged",
        end="B",
    ),
    _row(
        "vo_pickup/*.wav",
        "vo_synthesize",
        mode="binary",
        end="B",
    ),
    _row(
        "mastering/sound_design_vo_finalize.json",
        "sound_design_vo_finalize",
        end="B",
    ),
    _row("master/edl_narrative_audit.json", "edl_narrative_audit", end="C"),
    _row("master/edl.json", "edl", mode="one_writer", end="C"),
    _row("master/assembly_preview.wav", "assembly_preview", mode="binary", end="D"),
    # master_finalize runs the *authoritative* ship-time delight audit on the
    # committed master (`run_authoritative_listen_delight_at_ship`) — it re-scores
    # the same artifact, so it is a co-producer, not a foreign writer. Without
    # this row the ship gate could score but never record (exec_11871).
    _row(
        "mastering/listen_delight_audit.json",
        "master_finalize",
        "listen_delight_audit",
        end="F",
    ),
    _row(
        "sound_design/music_palette_compose.json",
        "music_palette_compose",
        end="D",
    ),
    _row("sound_design/sfx_prompts.json", "sfx_prompt_craft", end="D"),
    _row("sound_design/mmaudio_qa.json", "mmaudio_sfx", end="D"),
    # Generated SFX/theme bed WAVs + their manifest live under master/sfx/
    # (exec_11871 unknown_path on master/sfx/manifest.json).
    _row("master/sfx/manifest.json", "mmaudio_sfx", end="D"),
    _row("master/sfx/*.wav", "mmaudio_sfx", mode="binary", end="D"),
    _row("master/assembly.wav", "mix", mode="binary", end="D"),
    _row("master/junction_snip_qa.json", "junction_snip_qa", end="D"),
    # S3: seam autopsy SSOT is junction_snip_qa only — finalize reads commitment.
    _row("master/seam_autopsy.json", "junction_snip_qa", end="D"),
    _row("master/master.wav", "master_finalize", mode="binary", end="F"),
    _row("master/post_master_quality.json", "master_finalize", end="F"),
    _row("master/transcript.json", "master_transcript_build", end="ops"),
    # Apple ingest + plain-text sidecars of the same cue set — written straight to
    # disk by asset_transcripts.write_master_transcript_files (Path.write_text), so
    # they bind under the owner stage rather than registering as operational.
    _row("master/transcript.vtt", "master_transcript_build", mode="binary", end="ops"),
    _row("master/transcript.txt", "master_transcript_build", mode="binary", end="ops"),
    _row("publish/episode_meta.json", "episode_meta_build", end="ops"),
    _row("publish/cover_prompt.json", "episode_cover_prompt_craft", end="ops"),
    _row("publish/audio.mp3", "podcast_encode_mp3", mode="binary", end="ops"),
    _row("publish/cover.jpg", "episode_cover_generate", mode="binary", end="ops"),
    # ECG-B1: cascade writes batch_*/ + flat 0..n under staging (ctx.path);
    # without a catalog row promote/audit treat them as unknown_path.
    _row(
        "publish/cover_candidates/**",
        "episode_cover_generate",
        mode="binary",
        end="ops",
    ),
    _row("publish/package_ready.json", "podcast_publish", end="ops"),
    # Operational / homunculus
    _row("run_meta.json", "ops", mode="operational", end="ops"),
    _row(
        "mastering/homunculus/memory.json",
        "homunculus",
        mode="operational",
        end="ops",
    ),
    _row(
        "mastering/homunculus/ledger.json",
        "homunculus",
        mode="operational",
        end="ops",
    ),
    _row(
        "mastering/homunculus/admitted.json",
        "homunculus",
        mode="operational",
        end="ops",
    ),
    _row(
        "mastering/homunculus/agenda.json",
        "homunculus",
        mode="operational",
        end="ops",
    ),
    # Homunculus LLM volley packs — written under the active stage key during
    # analysis/delivery (exec_11871 speaker_roles unknown_path thrash).
    _row(
        "mastering/homunculus/volley_packs/*.json",
        "homunculus",
        mode="operational",
        end="ops",
    ),
    _row(
        "operator/forensics_errors.json",
        "ops",
        mode="operational",
        end="ops",
    ),
    _row(
        "operator/execution_status.json",
        "ops",
        mode="operational",
        end="ops",
    ),
    _row(
        "operator/driver_claim.json",
        "ops",
        mode="operational",
        end="ops",
    ),
    _row(
        "understanding/refinement_ledger.json",
        "ops",
        mode="operational",
        end="ops",
    ),
    _row(
        "master/air_order_integrity.json",
        "ops",
        mode="operational",
        end="ops",
    ),
    _row("gui_job.json", "ops", mode="operational", end="ops"),
    _row(
        "operator/identical_failures.json",
        "ops",
        mode="operational",
        end="ops",
    ),
    # Advance-past defect ledger (D1) + dispatch delta / attempt memo (§5.2-§5.3).
    _row(
        "operator/defect_ledger.json",
        "ops",
        mode="operational",
        end="ops",
    ),
    _row(
        "operator/dispatch_memo.json",
        "ops",
        mode="operational",
        end="ops",
    ),
    # Ship-path verdict written when reachability is provably severed (§5.5).
    _row(
        "operator/ship_reachability.json",
        "ops",
        mode="operational",
        end="ops",
    ),
    # Observed stage contracts (ops).
    _row(
        "operator/contract_observed.json",
        "ops",
        mode="operational",
        end="ops",
    ),
    # Analysis memory / context scaffold (ops)
    _row(
        "understanding/analysis_state.json",
        "ops",
        "analysis_profile",
        mode="operational",
        end="ops",
    ),
    _row(
        "understanding/investigation_queue.json",
        "ops",
        mode="operational",
        end="ops",
    ),
    _row(
        "understanding/context_index.json",
        "ops",
        "context_index",
        mode="operational",
        end="ops",
    ),
    _row(
        "understanding/analysis_orchestration.json",
        "ops",
        mode="operational",
        end="ops",
    ),
    # --- Secondary writers (fail-closed cutover; plan Phase 0A) ---
    # Wave 1 prepare
    _row("ingest/checksums.json", "ingest", mode="operational", end="ops"),
    _row("ingest/loudness.json", "ingest", mode="operational", end="ops"),
    _row("ingest/waveform_peaks.json", "ingest", mode="operational", end="ops"),
    _row("transcript/speakers.json", "transcribe", "speaker_roles", end="ops"),
    _row("transcript/corrections.json", "transcript_review_build", "transcribe", mode="operational", end="ops"),
    _row("transcript/diarization_repairs.json", "interview_spine_build", mode="operational", end="ops"),
    _row("transcript/protected_zones.json", "audio_probe_build", "vernacular_segment_sanitize", mode="operational", end="ops"),
    _row("transcript/speaker_flows.json", "audio_probe_build", mode="operational", end="ops"),
    _row("segments/nle_edits.json", "ops", mode="operational", end="ops"),
    _row("segments/boundary_review_queue.json", "boundary_detection", mode="operational", end="ops"),
    _row("segments/split_plan.json", "boundary_topic_resplit", mode="operational", end="ops"),
    # Wave 2 analysis secondaries
    _row("analysis/connector_fuse_locked_seams.json", "connector_fuse_pass", mode="operational"),
    _row("analysis/connector_fuse_split_island_qc.json", "connector_fuse_pass", mode="operational"),
    _row("analysis/connector_seam_packets.json", "connector_fuse_pass", mode="operational"),
    _row("analysis/connector_seam_verdicts.json", "connector_fuse_pass", mode="operational"),
    # connector_fuse_pass regroups the clusters every fuse round
    # (segment_fuse.run_high_value_cluster_fuse_rounds); scan stays the owner.
    _row(
        "analysis/high_value_island_clusters.json",
        "connector_fuse_pass",
        "low_conf_island_scan",
        mode="operational",
    ),
    _row("analysis/high_value_speech_boosts.json", "low_conf_island_scan", mode="operational"),
    _row("analysis/high_value_speech_islands.json", "low_conf_island_scan", mode="operational"),
    _row("analysis/island_cluster_structure_packets.json", "low_conf_island_scan", mode="operational"),
    _row("analysis/island_cluster_structure_verdicts.json", "low_conf_island_scan", mode="operational"),
    _row("analysis/low_conf_density_ranking.json", "low_conf_island_scan", mode="operational"),
    _row("analysis/low_conf_must_keep.json", "low_conf_island_scan", mode="operational"),
    # Soft STT lexicon islands/boosts: written by ranking (scan + OS-04) and
    # merged earlier by low_conf / high_value under low_conf_island_scan —
    # not vernacular_segment_sanitize (ghost ALLOW peeled VSS S1).
    _row(
        "analysis/stt_lexicon_island_boosts.json",
        "low_conf_island_scan",
        "full_master_ranking",
        mode="operational",
    ),
    _row("analysis/stt_lexicon_islands.json", "full_master_ranking", mode="operational"),
    _row("analysis/vernacular_must_keep.json", "vernacular_segment_sanitize", mode="operational"),
    _row("mastering/research_dossier.json", "mastering_research_rollup", mode="operational"),
    _row("mastering/shadow_diff.json", "ops", mode="operational", end="ops"),
    _row("mastering/media_ip_cta.json", "full_master_ranking", "selection_order_sanitize", end="A"),
    _row("mastering/shape/cross_critique.json", "mastering_shape_candidates", mode="operational"),
    _row("mastering/shape/diversity_report.json", "mastering_shape_candidates", mode="operational"),
    _row("mastering/shape/eval_rubric.json", "mastering_shape_agenda", mode="operational"),
    _row("mastering/shape/feasibility.json", "mastering_shape_candidates", mode="operational"),
    _row(
        "mastering/shape/information_package_candidates.json",
        "mastering_shape_candidates",
        "information_package_plan",
        mode="operational",
    ),
    _row("mastering/shape/pareto.json", "mastering_shape_candidates", mode="operational"),
    _row("mastering/shape/semantic_integrity.json", "mastering_shape_candidates", mode="operational"),
    _row("understanding/coherence_report.json", "ops", mode="operational", end="ops"),
    _row("understanding/cold_open_audition.json", "ops", mode="operational", end="ops"),
    _row("understanding/listener_outcome_trajectory.json", "ops", mode="operational", end="ops"),
    _row("understanding/native_comprehension_masks.json", "ops", mode="operational", end="ops"),
    _row("understanding/operator_clarifications.json", "ops", mode="operational", end="ops"),
    _row("understanding/pipeline_mode.json", "ops", mode="operational", end="ops"),
    # Pass-2 side artifacts — written by gate/accept/ensemble callers, not L0 agenda.
    _row(
        "understanding/refinement_cascade.json",
        "gap_framing_recompose",
        mode="operational",
        end="C",
    ),
    _row(
        "understanding/refinement_ensemble_lint.json",
        "ops",
        mode="operational",
        end="ops",
    ),
    _row(
        "understanding/refinement_plan.json",
        "gap_framing_recompose",
        "selection_framing_apply",
        mode="operational",
        end="C",
    ),
    _row("understanding/refinement_skip_copy.json", "gap_framing_recompose", mode="operational", end="C"),
    _row("understanding/source_readiness.json", "ops", mode="operational", end="ops"),
    _row("understanding/value_features.json", "ops", mode="operational", end="ops"),
    _row("vernacular/audio_tags_by_flow.json", "audio_probe_build", "vernacular_segment_sanitize", mode="operational"),
    _row("vernacular/probe_report.json", "audio_probe_build", "vernacular_segment_sanitize", mode="operational"),
    _row("analysis_complete.json", "ops", mode="operational", end="ops"),
    _row("transcripts/index.json", "ops", mode="operational", end="ops"),
    # Per-segment asset transcript sidecars: derived text mirrors kept in sync by
    # every fuse / resplit / overlap pass (asset_transcripts.sync_speech_sidecars).
    _row("transcripts/speech/*.json", "ops", mode="operational", end="ops"),
    _row("transcripts/speech/**/*.json", "ops", mode="operational", end="ops"),
    # Wave 3 gap / layup / VO
    _row("understanding/nugget_layup_qc.json", "nugget_layup_compose", mode="operational", end="C"),
    _row("understanding/nugget_allocation_plan.json", "nugget_layup_compose", mode="operational", end="C"),
    _row("understanding/nugget_intro_compose.json", "nugget_layup_compose", mode="operational", end="C"),
    _row(
        "understanding/layup_candidates.json",
        "nugget_layup_compose",
        mode="operational",
        end="C",
    ),
    _row(
        "understanding/.archived/layup_candidates/**/*.json",
        "nugget_layup_compose",
        mode="operational",
        end="C",
    ),
    _row(
        "understanding/nugget_comprehension_index.json",
        "nugget_layup_compose",
        mode="operational",
        end="C",
    ),
    _row("understanding/gap_report.draft.json", "gap_framing_compose", mode="operational", end="C"),
    _row("understanding/gap_framing_plan.json", "gap_framing_compose", mode="operational", end="C"),
    # Operator-facing VO script (plain text, surfaced by the G1 gate and the
    # gap_framing_compose workbench, and required by stage_completion before
    # optimal_questions may be marked done). Skip stub is compose-owned
    # (ensure_gap_report_skipped); optimal_questions may still write from
    # llm_output_resilience partial persist; gap_framing_compose is live author.
    _row(
        "understanding/interviewer_script.txt",
        "optimal_questions",
        "gap_framing_compose",
        end="C",
    ),
    _row("understanding/gap_vo_context_audit.json", "gap_framing_compose", mode="operational", end="C"),
    _row("understanding/gap_vo_rebudget_after_selection.json", "selection_order_sanitize", "full_master_ranking", mode="operational", end="C"),
    _row("understanding/gap_fill_skip.json", "ops", mode="operational", end="ops"),
    # Voice reference gate (GUI approve + candidate collect) — exec_11871 unknown_path.
    _row(
        "understanding/voice_reference/*.json",
        "source_topology_build",
        "missing_framing",
        "ops",
        mode="operational",
        end="ops",
    ),
    _row(
        "understanding/voice_reference/clips/**/*.wav",
        "source_topology_build",
        "missing_framing",
        "ops",
        mode="operational",
        end="ops",
    ),
    # The approved reference itself. Without these two rows the candidates and
    # the manifest survive a staged flush but the WAV the manifest points at is
    # dropped as unknown_path, leaving an approval that names a file which does
    # not exist. approve_voice_reference builds and duration-checks the WAV, so
    # the failure only appears later, as voice_reference_unusable, and nothing
    # reopens the gate because the approval stamp is already recorded.
    _row(
        "understanding/speaker_samples/*.wav",
        "source_topology_build",
        "missing_framing",
        "ops",
        mode="operational",
        end="ops",
    ),
    _row(
        "understanding/speaker_samples/*.json",
        "source_topology_build",
        "missing_framing",
        "ops",
        mode="operational",
        end="ops",
    ),
    # Per-stage LLM telemetry / volley inputs (exec_11871 missing_framing unknown_path).
    _row(
        "understanding/stage_runs/**/*.json",
        "ops",
        mode="operational",
        end="ops",
    ),
    # Direct children + nested (**/ requires an intermediate slash).
    _row(
        "mastering/evidence_packets/*.json",
        "ops",
        mode="operational",
        end="ops",
    ),
    _row(
        "mastering/evidence_packets/**/*.json",
        "ops",
        mode="operational",
        end="ops",
    ),
    _row(
        "understanding/llm_calls/missing_framing/**",
        "missing_framing",
        mode="operational",
        end="ops",
    ),
    _row(
        "understanding/stage_runs/missing_framing/**",
        "missing_framing",
        mode="operational",
        end="ops",
    ),
    _row(
        "understanding/llm_calls/gap_framing_compose/**",
        "gap_framing_compose",
        mode="operational",
        end="ops",
    ),
    _row(
        "understanding/stage_runs/gap_framing_compose/**",
        "gap_framing_compose",
        mode="operational",
        end="ops",
    ),
    _row(
        "vo_pickup/",
        "vo_synthesize",
        "vo_ingest",
        mode="operational",
        end="B",
    ),
    _row(
        "understanding/llm_calls/**/*.json",
        "ops",
        mode="operational",
        end="ops",
    ),
    _row(
        "understanding/llm_calls/**/*.md",
        "ops",
        mode="operational",
        end="ops",
    ),
    # Refinement champion seeds (exec_11871 gap_framing_compose unknown_path).
    _row(
        "understanding/refinement_champion/*.json",
        "gap_framing_compose",
        "refinement_agenda",
        "ops",
        mode="operational",
        end="ops",
    ),
    # Per-line VO transcript sidecars (exec_11871 heal/authority unknown_path).
    _row(
        "transcripts/vo/*.json",
        "nugget_layup_compose",
        "vo_synthesize",
        "ops",
        mode="operational",
        end="ops",
    ),
    # Spoken transition sidecars assembled into master VTT (exec_13161
    # master_transcript_build unknown_path under junction_committed).
    _row(
        "transcripts/transition/*.json",
        "master_transcript_build",
        "transitions",
        "ops",
        mode="operational",
        end="ops",
    ),
    _row(
        "transcripts/transition/**/*.json",
        "master_transcript_build",
        "transitions",
        "ops",
        mode="operational",
        end="ops",
    ),
    # Synthetic framing packet/plan (exec_11871 transitions unknown_path).
    _row(
        "understanding/synthetic_context_packet.json",
        "transitions",
        "synthetic_framing_plan",
        "ops",
        mode="operational",
        end="ops",
    ),
    _row(
        "understanding/synthetic_framing_plan.json",
        "transitions",
        "synthetic_framing_plan",
        end="C",
    ),
    # Refinement shadow scores (exec_11871 authority unknown_path).
    _row(
        "understanding/refinement_shadow/*.json",
        "ops",
        mode="operational",
        end="ops",
    ),
    _row(
        "understanding/reorder_bridges.json",
        "transitions",
        "selection_order_sanitize",
        "full_master_ranking",
        # EDL may refresh bridges when seating air order (exec_13159 AuthorityDenied).
        "edl",
        end="C",
    ),
    _row("vo_pickup/synthesis_report.json", "vo_synthesize", mode="operational", end="B"),
    _row("vo_pickup/timbre_match_qa.json", "vo_synthesize", mode="operational", end="B"),
    _row("master/transitions_pair_freeze.json", "transitions", end="A"),
    _row("master/clone_adjacency_verify.json", "vo_synthesize", mode="operational", end="B"),
    _row(
        "mastering/chapter_close_hitch/vo_snapshot.json",
        "chapter_close_hitch",
        mode="operational",
        end="C",
    ),
    # Wave 4 seating bus + selection/glue
    _row(
        "master/air_order.json",
        "edl",
        "mix",
        "junction_snip_qa",
        mode="one_writer",
        heal="edl",
        end="D",
    ),
    _row(
        "master/air_order_snapshot.json",
        "edl",
        "mix",
        "junction_snip_qa",
        mode="operational",
        heal="edl",
        end="D",
    ),
    _row(
        "master/render_ledger.json",
        "mix",
        "junction_snip_qa",
        mode="one_writer",
        heal="mix",
        end="D",
    ),
    _row(
        "master/assembly_ledger.json",
        "edl",
        "mix",
        "junction_snip_qa",
        mode="staged",
        heal="edl",
        end="D",
    ),
    _row("master/bridge_completeness.json", "transitions", "edl", end="C"),
    _row("master/deferred_transition_pairs.json", "transitions", end="A"),
    _row("master/story_health.json", "full_master_ranking", "selection_order_sanitize", mode="operational", end="A"),
    _row("master/rank_candidates.json", "full_master_ranking", mode="operational", end="A"),
    _row(
        "understanding/ideal_cuts_selection_seed.json",
        "full_master_ranking",
        mode="operational",
        end="A",
    ),
    _row("master/failure_review.json", "master_finalize", mode="operational", end="F"),
    _row("master/remediation_plan.json", "ops", mode="operational", end="ops"),
    _row("master/remediation_run_log.json", "ops", mode="operational", end="ops"),
    _row("master/listenability_contract.json", "mix", mode="operational", end="D"),
    _row("master/listener_scorecard.json", "master_finalize", mode="operational", end="F"),
    _row("master/narrative_plan.qc.json", "chapter_close_hitch", end="C"),
    _row(
        "master/order_reconcile.json",
        "full_master_ranking",
        "edl",
        mode="operational",
        end="C",
    ),
    _row("master/quality_candidates.json", "ops", mode="operational", end="ops"),
    _row("master/optimizer/archive.json", "ops", mode="operational", end="ops"),
    _row("master/optimizer/best_candidate.json", "ops", mode="operational", end="ops"),
    _row("master/optimizer/state.json", "ops", mode="operational", end="ops"),
    _row("master/optimizer_pending_selection.json", "ops", mode="operational", end="ops"),
    _row(
        "mastering/chapter_close_hitch/hitch_keepers.json",
        "chapter_close_hitch",
        mode="operational",
        end="C",
    ),
    _row(
        "mastering/chapter_close_hitch/intent_plan.json",
        "chapter_close_hitch",
        mode="operational",
        end="C",
    ),
    _row(
        "mastering/chapter_close_hitch/omit_ledger.json",
        "chapter_close_hitch",
        mode="operational",
        end="C",
    ),
    _row(
        "mastering/chapter_close_hitch/pre_keepers.json",
        "chapter_close_hitch",
        mode="operational",
        end="C",
    ),
    _row(
        "mastering/chapter_close_hitch/remap.json",
        "chapter_close_hitch",
        mode="operational",
        end="C",
    ),
    _row("mastering/edl_narrative_remutate.json", "edl_narrative_audit", mode="operational", end="C"),
    # Wave 5 sound / mix / junction / homunculus
    _row("sound_design/musicgen_candidates.json", "music_palette_compose", mode="operational", end="D"),
    _row("sound_design/placement_adjustments.json", "mix", mode="operational", end="D"),
    _row("sound_design/soundscape_report.json", "soundscape_policy_build", mode="operational"),
    # Mix realizes overlays and writes coverage; palette only plans cues.
    # Junction remaster must promote this as a side effect (see remaster_mix_only).
    _row("master/music_cue_coverage.json", "mix", mode="operational", end="D"),
    _row("master/bed_presence_qc.json", "mix", mode="operational", end="D"),
    _row("master/underbed_ab_qc.json", "mix", mode="operational", end="D"),
    _row("master/listen_critic.json", "mix", mode="operational", end="D"),
    _row("understanding/music_brief.json", "sound_design_plan", mode="operational", end="D"),
    _row(
        "understanding/speaker_delivery_plan.json",
        "selection_order_sanitize",
        "full_master_ranking",
        "ops",
        mode="operational",
        end="ops",
    ),
    _row("mastering/voice_clone_audit.json", "vo_synthesize", mode="operational", end="B"),
    _row("master/junction_feel_audit.json", "junction_snip_qa", mode="operational", end="D"),
    _row("master/junction_thought_complete.json", "junction_snip_qa", mode="operational", end="D"),
    _row(
        "mastering/listen_delight_remutate.json",
        "listen_delight_audit",
        mode="operational",
        end="F",
    ),
    _row("mastering/homunculus/plan.json", "homunculus", mode="operational", end="ops"),
    _row("mastering/homunculus/kb.json", "homunculus", mode="operational", end="ops"),
    _row("mastering/homunculus/persona.json", "homunculus", mode="operational", end="ops"),
    _row("mastering/homunculus/source_card.json", "homunculus", mode="operational", end="ops"),
    _row("mastering/homunculus/gate_decisions.json", "homunculus", mode="operational", end="ops"),
    _row("mastering/homunculus/prompt_stack.json", "homunculus", mode="operational", end="ops"),
    _row("mastering/homunculus/promotions.json", "homunculus", mode="operational", end="ops"),
    _row("mastering/homunculus/end_judgment.json", "homunculus", mode="operational", end="ops"),
    _row("mastering/homunculus/speaker_dossier.json", "homunculus", mode="operational", end="ops"),
    _row(
        "mastering/homunculus/identical_stage_errors.json",
        "homunculus",
        mode="operational",
        end="ops",
    ),
    _row("mastering/homunculus/limit_exhausted.json", "homunculus", mode="operational", end="ops"),
    _row(
        "mastering/homunculus/analyses/*.json",
        "homunculus",
        mode="operational",
        end="ops",
    ),
    _row(
        "mastering/homunculus/ears/**",
        "homunculus",
        mode="operational",
        end="ops",
    ),
    _row(
        "mastering/homunculus/mints/*.json",
        "homunculus",
        mode="operational",
        end="ops",
    ),
    _row(
        "mastering/homunculus/memory_probe.json",
        "homunculus",
        mode="operational",
        end="ops",
    ),
    _row("scratch/**", "ops", "homunculus", mode="operational", end="ops"),
    # Legacy G0 pack alias still read by homunculus packer/ears/speakers.
    _row("ingest/transcript.json", "transcribe", "ops", mode="operational", end="ops"),
    # Wave 6 publish
    # The local episode package written by podcast_publish (`s3_layout.episode_files`).
    # These were never cataloged, so packaging died on
    # `authority_denied:persist:publish/chapters.json … (unknown_path)` (exec_11871).
    # Written at the end of episode_cover_generate, before the G-Publish gate.
    # podcast_publish remains the authoritative owner and may restamp on ship.
    _row(
        "publish/chapters.json",
        "episode_cover_generate",
        "podcast_publish",
        mode="operational",
        end="ops",
    ),
    _row(
        "publish/episode.json",
        "episode_cover_generate",
        "podcast_publish",
        mode="operational",
        end="ops",
    ),
    _row(
        "publish/description.txt",
        "episode_cover_generate",
        "podcast_publish",
        mode="operational",
        end="ops",
    ),
    _row(
        "publish/transcript.vtt",
        "episode_cover_generate",
        "podcast_publish",
        mode="operational",
        end="ops",
    ),
    _row(
        "publish/master.wav",
        "podcast_encode_mp3",
        "podcast_publish",
        mode="binary",
        end="ops",
    ),
    _row("publish/cover_meta.json", "episode_cover_generate", mode="operational", end="ops"),
    _row("publish/cover_pick.json", "episode_cover_generate", mode="operational", end="ops"),
    _row("publish/publish_result.json", "podcast_publish", mode="operational", end="ops"),
)

# Primary path for DISK_PATHS parity (one path per live stage).
_PRIMARY_BY_STAGE: dict[str, str] = {
    "audio_preclean": "preclean/isolated.wav",
    "ingest": "ingest/normalized.wav",
    "transcribe": "transcript/full.json",
    "transcript_review_build": "transcript/review_queue.json",
    "audio_probe_build": "analysis/run_golden_facts.json",
    "source_acoustic_profile": "understanding/source_acoustic_profile.json",
    "interview_spine_build": "understanding/interview_spine.json",
    "speaker_roles": "understanding/speakers.json",
    "source_topology_build": "understanding/source_topology.json",
    "content_context": "understanding/content_brief.json",
    "talking_points_compose": "understanding/talking_points.json",
    "ideal_cuts_propose": "understanding/ideal_cuts.json",
    "ideal_cuts_materialize": "understanding/ideal_cuts_materialized.json",
    "boundary_detection": "segments/boundaries.json",
    "segment_classification": "segments/manifest.json",
    "content_brief_reanchor": "understanding/content_brief.json",
    "framing_posture_decide": "understanding/framing_posture_decision.json",
    "boundary_topic_resplit": "segments/boundaries.json",
    "vernacular_segment_sanitize": "vernacular/resplit_report.json",
    "low_conf_island_scan": "analysis/low_conf_islands.json",
    "connector_fuse_pass": "analysis/connector_fuse_audit.json",
    "connector_fuse_pass_pre_ranking": "analysis/connector_fuse_rounds_pre_ranking.json",
    "sonic_context_build": "understanding/sonic_context.json",
    "sound_design_palettes": "understanding/sound_design_plan.json",
    "mastering_research_routing": "mastering/research/routing.json",
    "mastering_research_waves": "mastering/research/waves.json",
    "mastering_research_rollup": "mastering/research/rollup.json",
    "mastering_shape_agenda": "mastering/shape/agenda.json",
    "mastering_shape_candidates": "mastering/shape/candidates.json",
    "mastering_plan_synthesize": "mastering/mastering_plan.json",
    "missing_framing": "understanding/gap_evaluations.json",
    "mastering_plan_confirm": "mastering/mastering_plan.json",
    "gap_framing_compose": "understanding/gap_report.json",
    "delivery_brief_build": "understanding/delivery_brief.json",
    "soundscape_policy_build": "understanding/soundscape_policy.json",
    "episode_structure_compose": "understanding/episode_structure.json",
    "topic_coverage_audit": "master/coverage_audit.json",
    "narrative_arc_plan": "master/narrative_plan.json",
    "chapter_close_hitch": "mastering/chapter_close_hitch.json",
    "full_master_ranking": "master/selection.json",
    "selection_order_sanitize": "master/selection.json",
    "air_script_compose": "mastering/mastering_plan.json",
    "nugget_corpus_mine": "understanding/nugget_corpus.json",
    "information_package_plan": "mastering/shape/information_packages_audit.json",
    "nugget_layup_compose": "understanding/nugget_layup_plan.json",
    "gap_report_sanitize": "understanding/gap_report.json",
    "refinement_agenda": "understanding/refinement_agenda.json",
    "gap_framing_recompose": "understanding/gap_framing_recompose.json",
    "selection_framing_apply": "understanding/selection_framing_apply.json",
    "air_script_seams": "mastering/mastering_plan.json",
    "air_contract_sanitize": "mastering/mastering_plan.json",
    "transitions": "master/transitions.json",
    "sound_design_plan": "understanding/sound_design_plan.json",
    "vo_line_adjudicate": "understanding/vo_line_adjudication.json",
    "vo_synthesize": "mastering/vo_synthesize.json",
    "sound_design_vo_finalize": "mastering/sound_design_vo_finalize.json",
    "edl_narrative_audit": "master/edl_narrative_audit.json",
    "edl": "master/edl.json",
    "assembly_preview": "master/assembly_preview.wav",
    "listen_delight_audit": "mastering/listen_delight_audit.json",
    "music_palette_compose": "sound_design/music_palette_compose.json",
    "sfx_prompt_craft": "sound_design/sfx_prompts.json",
    "mmaudio_sfx": "sound_design/mmaudio_qa.json",
    "mix": "master/assembly.wav",
    "junction_snip_qa": "master/junction_snip_qa.json",
    "master_finalize": "master/master.wav",
    "master_transcript_build": "master/transcript.json",
    "episode_meta_build": "publish/episode_meta.json",
    "episode_cover_prompt_craft": "publish/cover_prompt.json",
    "podcast_encode_mp3": "publish/audio.mp3",
    "episode_cover_generate": "publish/cover.jpg",
    "podcast_publish": "publish/package_ready.json",
}

HOT_PATHS: frozenset[str] = frozenset(
    {
        "master/selection.json",
        "understanding/gap_report.json",
        "master/edl.json",
        "master/transitions.json",
        "understanding/sound_design_plan.json",
        "understanding/nugget_layup_plan.json",
    }
)

# Production _one_writer_raw allowlist (ops scaffold only).
ONE_WRITER_RAW_ALLOWLIST: frozenset[str] = frozenset(
    {
        "analysis_memory.ensure_analysis_workspace",
    }
)

# L3 heal tokens → owner (extends PRODUCER_PIN_TABLE; no sealed default).
HEAL_TOKEN_OWNERS: dict[str, str] = {
    "hosted_vo_floor_unmet": "nugget_layup_compose",
    "hosted_vo_hollow": "nugget_layup_compose",
    "hosted_vo_wav_coverage": "vo_synthesize",
    "hosted_vo_floor_unsatisfiable": "",  # escalate once — never re-pin LLM compose
    "hosted_vo_books_agree": "nugget_layup_compose",
    "layup_compose_qc_pending": "nugget_layup_compose",
    "hosted_framing_floor_unmet": "nugget_layup_compose",
    "resume nugget_layup_compose": "nugget_layup_compose",
    "resume gap_framing_compose": "gap_framing_compose",
    "transitions_missing": "transitions",
    "missing_transitions": "transitions",
    "g1_incomplete": "vo_synthesize",
    "premature_complete": "transitions",
    "authority_denied": "",  # resolve via suggested_owner on the event
}


def catalog() -> tuple[ArtifactRow, ...]:
    return _CATALOG_SEED


def primary_path_for_stage(stage: str) -> str:
    return _PRIMARY_BY_STAGE.get(str(stage or "").strip(), "")


def disk_paths_view() -> dict[str, str]:
    """Derived STAGE_ARTIFACT_DISK_PATHS — live order stages only."""
    return dict(_PRIMARY_BY_STAGE)


def owner_of(path: str) -> str:
    owners = owners_of(path)
    return owners[-1] if owners else ""


def owners_of(path: str) -> tuple[str, ...]:
    rel = _norm_path(path)
    for row in _CATALOG_SEED:
        if _path_matches(row.path, rel):
            return row.producers
    return ()


def owners_of_field(path: str, field: str) -> tuple[str, ...]:
    rel = _norm_path(path)
    for row in _CATALOG_SEED:
        if not _path_matches(row.path, rel):
            continue
        if not row.fields or field in row.fields or _field_glob_match(field, row.fields):
            return row.producers
    return owners_of(rel)


def row_for_path(path: str) -> ArtifactRow | None:
    rel = _norm_path(path)
    for row in _CATALOG_SEED:
        if _path_matches(row.path, rel):
            return row
    return None


# ---------------------------------------------------------------------------
# ALLOW / DENY matrices
# ---------------------------------------------------------------------------

def _build_allow() -> tuple[AllowRow, ...]:
    rows: list[AllowRow] = []
    for art in _CATALOG_SEED:
        mode = "one_writer" if art.path in HOT_PATHS else art.write_mode
        for stage in art.producers:
            rows.append(
                AllowRow(
                    path=art.path,
                    stage=stage,
                    role="producer",
                    fields=art.fields,
                    write_mode=mode,
                    verb="persist",
                )
            )
            rows.append(
                AllowRow(
                    path=art.path,
                    stage=stage,
                    role="producer",
                    write_mode=mode,
                    verb="mark_done",
                )
            )
        # Heal may execute/unmark the owner only.
        pin = art.heal_pin or art.authoritative
        if pin:
            rows.append(
                AllowRow(
                    path=art.path,
                    stage=pin,
                    role="heal",
                    allowed_mutations=("unmark_owner", "execute_owner"),
                    verb="unmark",
                )
            )
            rows.append(
                AllowRow(
                    path=art.path,
                    stage=pin,
                    role="heal",
                    allowed_mutations=("execute_owner",),
                    verb="execute",
                )
            )
            rows.append(
                AllowRow(
                    path=art.path,
                    stage=pin,
                    role="driver",
                    allowed_mutations=("execute_owner",),
                    verb="execute",
                )
            )
        # Nested VO staging under EDL — ALLOW persist of VO bytes by vo_synthesize
        # even while edl holds the job lease (pending under vo_synthesize tree).
        if art.path.startswith("vo_pickup") or art.path == "mastering/vo_synthesize.json":
            rows.append(
                AllowRow(
                    path=art.path,
                    stage="vo_synthesize",
                    role="producer",
                    epochs=("hard_freeze", "edl_sealed", "mix_seated", ""),
                    write_mode="binary" if "wav" in art.path else "staged",
                    allowed_mutations=("nested_under_edl",),
                    verb="persist",
                )
            )
        # Mechanical segment-id remap after a fuse / overlap union — retire the
        # consumed id everywhere it is referenced, in any epoch (integrity only).
        if art.path in SEGMENT_ID_REMAP_PATHS:
            for remap_stage in SEGMENT_ID_REMAP_STAGES:
                rows.append(
                    AllowRow(
                        path=art.path,
                        stage=remap_stage,
                        role="producer",
                        epochs=(
                            "pre_soft_freeze",
                            "soft_freeze",
                            "hard_freeze",
                            "edl_sealed",
                            "mix_seated",
                            "junction_committed",
                            "",
                        ),
                        write_mode="staged",
                        allowed_mutations=("segment_id_remap",),
                        verb="persist",
                    )
                )
        # Sanitize End-A omit flags only under freeze epochs.
        if art.path in {
            "understanding/gap_report.json",
            "understanding/omit_ledger.json",
            "mastering/mastering_plan.json",
        }:
            rows.append(
                AllowRow(
                    path=art.path,
                    stage="air_contract_sanitize",
                    role="sanitize",
                    fields=(
                        "interviewer_lines[].skipped_optional",
                        "interviewer_lines[].air_script_omit",
                    ),
                    epochs=("soft_freeze", "hard_freeze"),
                    write_mode="air_contract",
                    allowed_mutations=(
                        "end_a_omit_stamp",
                        "orientation_revive",
                        "omit_ledger_mirror",
                    ),
                    verb="persist",
                )
            )
        # S9: gap_report omit/delivery stampers (body text = framing + layup only).
        if art.path == "understanding/gap_report.json":
            _gap_stamp_fields = (
                "interviewer_lines[].skipped_optional",
                "interviewer_lines[].air_script_omit",
                "interviewer_lines[].delivery",
                "interviewer_lines[].voice_speaker_id",
                "interviewer_lines[].targets_segment_id",
            )
            for stamp_stage in (
                "selection_framing_apply",
                "vo_line_adjudicate",
                "gap_report_sanitize",
                "vo_synthesize",
            ):
                rows.append(
                    AllowRow(
                        path=art.path,
                        stage=stamp_stage,
                        role="producer",
                        fields=_gap_stamp_fields,
                        epochs=(
                            "pre_soft_freeze",
                            "soft_freeze",
                            "hard_freeze",
                            "edl_sealed",
                            "mix_seated",
                            "junction_committed",
                            "",
                        ),
                        write_mode="one_writer",
                        verb="persist",
                    )
                )
        # GUI gate / transcript / gap CRUD / NLE / recompute / publish scaffolds
        if art.path in {
            "transcript/review_queue.json",
            "transcript/full.json",
            "run_meta.json",
            "understanding/gap_report.json",
            "understanding/gap_fill_skip.json",
            "understanding/coherence_report.json",
            "understanding/analysis_state.json",
            "understanding/investigation_queue.json",
            "understanding/context_index.json",
            "understanding/delivery_brief.json",
            "understanding/speakers.json",
            "understanding/source_acoustic_profile.json",
            "segments/nle_edits.json",
            "publish/cover_meta.json",
            "publish/cover_pick.json",
            "publish/publish_result.json",
            "publish/package_ready.json",
            "gui_job.json",
        }:
            rows.append(
                AllowRow(
                    path=art.path,
                    stage="",
                    role="gui",
                    epochs=("",),
                    write_mode="operational" if art.path in {
                        "run_meta.json",
                        "gui_job.json",
                        "understanding/gap_fill_skip.json",
                        "understanding/coherence_report.json",
                        "understanding/analysis_state.json",
                        "understanding/investigation_queue.json",
                        "understanding/context_index.json",
                        "segments/nle_edits.json",
                        "publish/cover_meta.json",
                        "publish/cover_pick.json",
                        "publish/publish_result.json",
                        "publish/package_ready.json",
                    } else "staged",
                    allowed_mutations=(
                        "g0_edit",
                        "g_framing",
                        "g1_consent",
                        "nle_edit",
                        "recompute",
                        "g_publish",
                    ),
                    verb="persist",
                )
            )
        # Ops operational
        if art.write_mode == "operational":
            rows.append(
                AllowRow(
                    path=art.path,
                    stage="ops",
                    role="ops",
                    write_mode="operational",
                    verb="persist",
                )
            )
    # Explicit GUI ALLOW for gate paths that may not be catalog primaries.
    for gui_path in (
        "transcript/review_queue.json",
        "transcript/full.json",
        "understanding/gap_report.json",
        "understanding/gap_fill_skip.json",
        "understanding/coherence_report.json",
        "understanding/analysis_state.json",
        "understanding/investigation_queue.json",
        "understanding/context_index.json",
        "understanding/delivery_brief.json",
        "understanding/speakers.json",
        "understanding/source_acoustic_profile.json",
        "segments/nle_edits.json",
        "sound_design/sfx_prompts.json",
        "publish/cover_meta.json",
        "publish/cover_pick.json",
        "publish/publish_result.json",
        "publish/package_ready.json",
        "gui_job.json",
        "run_meta.json",
    ):
        rows.append(
            AllowRow(
                path=gui_path,
                stage="",
                role="gui",
                epochs=("",),
                write_mode="operational",
                allowed_mutations=(
                    "g0_edit",
                    "g_framing",
                    "g1_consent",
                    "nle_edit",
                    "recompute",
                    "g_publish",
                    "sfx_edit",
                ),
                verb="persist",
            )
        )
    # Narrative audit may realign chapter metadata to the already-selected air
    # order without changing seats. This is the freeze-safe repair class.
    rows.append(
        AllowRow(
            path="master/selection.json",
            stage="edl_narrative_audit",
            role="producer",
            epochs=("pre_soft_freeze", "soft_freeze", "hard_freeze"),
            write_mode="one_writer",
            allowed_mutations=("narrative_metadata_align",),
            verb="persist",
        )
    )
    # Same freeze-safe class for narrative_plan chapter/constraint alignment
    # (exec_13167: hard_freeze blocked persist under narrative_arc_plan owner).
    rows.append(
        AllowRow(
            path="master/narrative_plan.json",
            stage="edl_narrative_audit",
            role="producer",
            epochs=("pre_soft_freeze", "soft_freeze", "hard_freeze"),
            write_mode="one_writer",
            allowed_mutations=("narrative_metadata_align",),
            verb="persist",
        )
    )
    # Hitch chapter adopt / QC remint — must use hitch's own stage_key + mutation
    # (never spoof narrative_arc_plan).
    rows.append(
        AllowRow(
            path="master/narrative_plan.json",
            stage="chapter_close_hitch",
            role="producer",
            epochs=("pre_soft_freeze", "soft_freeze", "hard_freeze", ""),
            write_mode="one_writer",
            allowed_mutations=("hitch_chapter_authority",),
            verb="persist",
        )
    )
    # Ranking / reconcile constraint align under freeze-safe class.
    for align_stage in ("full_master_ranking", "sound_design_plan", "edl"):
        rows.append(
            AllowRow(
                path="master/narrative_plan.json",
                stage=align_stage,
                role="producer",
                epochs=("pre_soft_freeze", "soft_freeze", "hard_freeze", ""),
                write_mode="one_writer",
                allowed_mutations=("narrative_metadata_align",),
                verb="persist",
            )
        )
    # Common operational / ledger paths (not stage primaries).
    for path in (
        "understanding/refinement_ledger.json",
        "master/air_order_integrity.json",
        "gui_job.json",
        "operator/identical_failures.json",
        "operator/forensics_errors.md",
        "operator/defect_ledger.json",
        "operator/dispatch_memo.json",
        "operator/ship_reachability.json",
        "operator/contract_observed.json",
    ):
        rows.append(
            AllowRow(
                path=path,
                stage="ops",
                role="ops",
                write_mode="operational",
                verb="persist",
            )
        )
    # Transition WAVs: producers already pass via owner_rerun. Glue stages that
    # remaster a pair into pending still need an explicit promote_pending ALLOW.
    for art in _CATALOG_SEED:
        if art.path != "master/transitions/*.wav":
            continue
        for stage in (
            "edl_narrative_audit",
            "master_finalize",
            "connector_fuse_pass",
            "connector_fuse_pass_pre_ranking",
            "edl_overlap_repair",
        ):
            rows.append(
                AllowRow(
                    path=art.path,
                    stage=stage,
                    role="producer",
                    write_mode="binary",
                    verb="promote_pending",
                )
            )
    return tuple(rows)


def _build_deny() -> tuple[DenyRow, ...]:
    return (
        # EDL / mix / finalize must never mint VO pickups or seat new gap lines.
        DenyRow(
            path="vo_pickup/*.wav",
            stage="edl",
            reason="edl_must_not_write_vo_pickup",
        ),
        DenyRow(
            path="vo_pickup/*.wav",
            stage="mix",
            reason="mix_must_not_write_vo_pickup",
        ),
        DenyRow(
            path="vo_pickup/*.wav",
            stage="master_finalize",
            reason="finalize_must_not_write_vo_pickup",
        ),
        DenyRow(
            path="vo_pickup/*.wav",
            stage="missing_framing",
            reason="missing_framing_must_not_write_vo_pickup",
        ),
        DenyRow(
            path="understanding/gap_report.json",
            stage="edl",
            fields=("interviewer_lines[].text",),
            reason="edl_must_not_rewrite_gap_copy",
        ),
        DenyRow(
            path="understanding/gap_report.json",
            stage="edl",
            fields=("interviewer_lines[].voice_speaker_id",),
            reason="edl_must_not_stamp_voice_speaker",
        ),
        # WAV-expanding hosted-floor reseat under hard freeze (action token).
        # Legitimate End-A omit stamps remain ALLOW via sanitize rows.
        DenyRow(
            path="understanding/gap_report.json",
            stage="ensure_hosted_framing_vo_seats",
            epochs=("hard_freeze",),
            reason="protect_hosted_vo_floor_reseat",
        ),
        DenyRow(
            path="understanding/gap_report.json",
            stage="catastrophe_hosted_vo_floor",
            epochs=("hard_freeze",),
            reason="catastrophe_hosted_vo_floor",
        ),
        # CTA never becomes hosted-floor seat (field hint when callers pass it).
        DenyRow(
            path="understanding/gap_report.json",
            stage="",
            fields=("origin=cta_hole_cover",),
            reason="cta_never_touch_as_floor_seat",
        ),
        # Non-owner flush of VO pending.
        DenyRow(
            path="vo_pickup/*.wav",
            stage="edl",
            verb="promote_pending",
            reason="non_owner_vo_pending_flush",
        ),
        DenyRow(
            path="vo_pickup/*",
            stage="",
            role="driver",
            verb="promote_pending",
            reason="driver_must_not_flush_foreign_vo",
        ),
        # Foreign glue / selection / plan promote (0F).
        DenyRow(
            path="understanding/gap_report.json",
            stage="edl",
            verb="promote_pending",
            reason="non_owner_gap_pending_flush",
        ),
        DenyRow(
            path="understanding/gap_report.json",
            stage="mix",
            verb="promote_pending",
            reason="non_owner_gap_pending_flush",
        ),
        DenyRow(
            path="understanding/gap_report.json",
            stage="junction_snip_qa",
            verb="promote_pending",
            reason="non_owner_gap_pending_flush",
        ),
        DenyRow(
            path="master/transitions.json",
            stage="edl",
            verb="promote_pending",
            reason="non_owner_transitions_pending_flush",
        ),
        DenyRow(
            path="master/selection.json",
            stage="edl",
            verb="promote_pending",
            reason="non_owner_selection_pending_flush",
        ),
        DenyRow(
            path="mastering/mastering_plan.json",
            stage="edl",
            verb="promote_pending",
            reason="non_owner_plan_pending_flush",
        ),
        DenyRow(
            path="understanding/reorder_bridges.json",
            stage="edl",
            verb="promote_pending",
            reason="non_owner_bridges_pending_flush",
        ),
        # GUI must not rewrite seated EDL via artifact editor.
        DenyRow(
            path="master/edl.json",
            stage="",
            role="gui",
            reason="gui_must_not_rewrite_edl",
        ),
        # Adjudicate must not unmark live VO to rewrite WAVs.
        DenyRow(
            path="mastering/vo_synthesize.json",
            stage="vo_line_adjudicate",
            verb="unmark",
            reason="adjudicate_must_not_unmark_live_vo",
        ),
        DenyRow(
            path="vo_pickup/*.wav",
            stage="vo_line_adjudicate",
            verb="unmark",
            reason="adjudicate_must_not_unmark_vo_wavs",
        ),
        # Consumer restage writing owner JSON.
        DenyRow(
            path="master/transitions.json",
            stage="edl",
            reason="edl_must_not_mint_transitions",
        ),
        DenyRow(
            path="master/transitions.json",
            stage="edl_narrative_audit",
            reason="narrative_must_not_mint_transitions",
        ),
        # Soft-complete without waiver.
        DenyRow(
            path="master/bridge_completeness.json",
            stage="",
            role="driver",
            reason="soft_complete_requires_e2e_waiver",
        ),
        # Cross-exec / escape.
        DenyRow(
            path="../**",
            stage="",
            reason="cross_exec_isolation",
        ),
    )


ALLOW: tuple[AllowRow, ...] = _build_allow()
DENY: tuple[DenyRow, ...] = _build_deny()


# ---------------------------------------------------------------------------
# Epoch + fail_closed
# ---------------------------------------------------------------------------

def fail_closed() -> bool:
    raw = os.environ.get(_ENV_FAIL_CLOSED)
    if raw is not None:
        return str(raw).strip().lower() not in {"0", "false", "no", "off"}
    return bool(_FAIL_CLOSED_DEFAULT)


def set_fail_closed_default(value: bool) -> None:
    global _FAIL_CLOSED_DEFAULT
    _FAIL_CLOSED_DEFAULT = bool(value)


def current_epoch(ctx: Any) -> str:
    """Derive the highest active epoch gate from run_meta / done markers."""
    try:
        from interview_mux.seat_authority import hard_freeze_active, soft_freeze_active

        if hard_freeze_active(ctx):
            ep = "hard_freeze"
        elif soft_freeze_active(ctx):
            ep = "soft_freeze"
        else:
            ep = "pre_soft_freeze"
    except Exception:
        ep = "pre_soft_freeze"
    try:
        if ctx.is_done("edl") and ctx.artifact_exists("master/edl.json"):
            ep = "edl_sealed"
        if ctx.is_done("mix") and ctx.artifact_exists("master/assembly.wav"):
            ep = "mix_seated"
        if ctx.is_done("junction_snip_qa"):
            ep = "junction_committed"
    except Exception:
        pass
    return ep


def freeze_write_allowed(
    ctx: Any,
    stage_key: str,
    mutation_class: str,
    *,
    epoch: str | None = None,
) -> bool:
    """Return whether a named stage mutation is legal in the active freeze epoch."""
    stage = str(stage_key or "").strip()
    mutation = str(mutation_class or "").strip()
    ep = str(epoch or current_epoch(ctx) or "pre_soft_freeze")
    for row in FREEZE_WRITE_POLICY:
        if row.stage == stage and row.mutation_class == mutation:
            return ep in row.epochs
    return False


@lru_cache(maxsize=1)
def matrix_version() -> str:
    payload = {
        "allow": [
            (a.path, a.stage, a.role, a.fields, a.epochs, a.verb, a.allowed_mutations)
            for a in ALLOW
        ],
        "deny": [
            (d.path, d.stage, d.role, d.fields, d.epochs, d.verb, d.reason)
            for d in DENY
        ],
        "freeze": [
            (p.stage, p.mutation_class, p.epochs) for p in FREEZE_WRITE_POLICY
        ],
        "primary": sorted(_PRIMARY_BY_STAGE.items()),
    }
    blob = json.dumps(payload, sort_keys=True, default=str).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()[:16]


def stamp_matrix_version(ctx: Any) -> str:
    ver = matrix_version()

    def _mut(meta: dict[str, Any]) -> None:
        meta[MATRIX_VERSION_META_KEY] = ver

    try:
        ctx.mutate_run_meta(_mut)
    except Exception:
        pass
    return ver


def check_matrix_version(ctx: Any) -> tuple[bool, str]:
    """Return (ok, message). Mismatch refuses further mutates.

    Forensics campaigns may restamp after intentional ALLOW-seed patches so the
    same run_id can continue (MUX_FORENSICS=1). Plain / soak runs stay sealed.
    """
    live = matrix_version()
    try:
        meta = ctx.read_json("run_meta.json") if ctx.artifact_exists("run_meta.json") else {}
    except Exception:
        meta = {}
    sealed = str((meta or {}).get(MATRIX_VERSION_META_KEY) or "").strip()
    if not sealed:
        return True, "unsealed"
    if sealed != live:
        try:
            from interview_mux.identical_failures import forensics_mode

            if forensics_mode():
                stamp_matrix_version(ctx)
                return True, f"forensics_restamp sealed={sealed}→{live}"
        except Exception:
            pass
        return False, f"matrix_version_mismatch sealed={sealed} live={live}"
    return True, sealed


# ---------------------------------------------------------------------------
# write_permitted / assert_write / heal_pin_for
# ---------------------------------------------------------------------------

def write_permitted(
    ctx: Any,
    path: str,
    stage_key: str | None,
    role: str = "producer",
    fields: Iterable[str] | None = None,
    verb: Verb = "persist",
    mutation_class: str | None = None,
) -> tuple[bool, str]:
    """Return (ok, reason). DENY wins over ALLOW. Unknown path fails when closed."""
    rel = _norm_path(path)
    stage = str(stage_key or "").strip()
    role_s = str(role or "producer").strip() or "producer"
    field_set = tuple(str(f) for f in (fields or ()) if f)
    mutation = str(mutation_class or "").strip()
    epoch = ""
    try:
        epoch = current_epoch(ctx) if ctx is not None else ""
    except Exception:
        epoch = ""

    # Cross-exec isolation
    if rel.startswith("../") or rel.startswith("/") or ".." in rel.split("/"):
        return False, "cross_exec_isolation"

    # Anti-spoof: hitch must not forge narrative_arc_plan stage_key on live plan.
    if verb == "persist" and stage and rel == "master/narrative_plan.json":
        try:
            from interview_mux.write_staging import active_stage_id

            active = str(active_stage_id() or "").strip()
        except Exception:
            active = ""
        if (
            active == "chapter_close_hitch"
            and stage == "narrative_arc_plan"
        ):
            return False, "stage_key_spoof:hitch_must_not_claim_narrative_arc_plan"
        if stage == "chapter_close_hitch" and mutation not in {
            "hitch_chapter_authority",
            "segment_id_remap",
        }:
            return False, "mutation_class_required:hitch_chapter_authority"

    ok_ver, ver_msg = (True, "")
    if ctx is not None and verb == "persist":
        ok_ver, ver_msg = check_matrix_version(ctx)
        if not ok_ver:
            # The version seal protects owner bodies. Telemetry / progress / claim
            # scaffolds must keep writing or the operator loses every surface that
            # would report the mismatch — exec_11871 jammed the job lock behind a
            # per-write denial storm on operator/*.
            ver_row = row_for_path(rel)
            if rel.startswith("operator/") or rel.startswith(".stage_done/") or (
                ver_row is not None and ver_row.write_mode == "operational"
            ):
                ok_ver, ver_msg = True, "operational_under_version_mismatch"
            else:
                return False, ver_msg

    # Explicit DENY first
    for d in DENY:
        if d.verb != verb:
            continue
        if not _path_matches(d.path, rel):
            continue
        if d.stage and d.stage != stage:
            continue
        if d.role and d.role != role_s:
            continue
        if d.epochs and epoch and epoch not in d.epochs:
            continue
        if d.fields:
            if not field_set:
                continue  # field-specific deny needs a field hint
            if not any(_field_glob_match(f, d.fields) for f in field_set):
                continue
        return False, d.reason or f"deny:{d.path}:{d.stage or d.role}"

    # Ops / operational paths
    row = row_for_path(rel)
    if row is None:
        # Glob catalog miss — try vo_pickup concrete
        if rel.startswith("vo_pickup/") and rel.endswith(".wav"):
            row = row_for_path("vo_pickup/*.wav")
    if row is None:
        if rel.startswith("operator/") or rel.startswith(".stage_done/"):
            return True, "operational_unregistered"
        if not fail_closed():
            return True, "unknown_path_warn"
        return False, "unknown_path"

    # Remap authority is narrower than producer authority. Every persist made by
    # a remap stage on the shared walker surface must identify the integrity-only
    # mutation class declared by its ALLOW row — except hitch chapter adopt.
    # A remap stage writing a file it produces itself is not a remap.
    if (
        verb == "persist"
        and stage in SEGMENT_ID_REMAP_STAGES
        and rel in SEGMENT_ID_REMAP_PATHS
        and stage not in row.producers
    ):
        if mutation == "hitch_chapter_authority" and rel == "master/narrative_plan.json":
            pass  # hitch chapter adopt ALLOW (not pure id remap)
        elif mutation != "segment_id_remap":
            return False, "mutation_class_required:segment_id_remap"
        elif not any(
            a.path == rel
            and a.stage == stage
            and a.verb == verb
            and mutation in a.allowed_mutations
            and (not a.epochs or not epoch or epoch in a.epochs or "" in a.epochs)
            for a in ALLOW
        ):
            return False, "mutation_class_not_allowed:segment_id_remap"

    if mutation and any(
        p.stage == stage and p.mutation_class == mutation for p in FREEZE_WRITE_POLICY
    ) and not freeze_write_allowed(ctx, stage, mutation, epoch=epoch):
        return False, f"freeze_blocks:{stage}:{mutation}:{epoch}"

    # Ops / operational paths — any caller may persist (telemetry, integrity, GUI).
    if row.write_mode == "operational":
        return True, "operational"

    # Role pin-only: heal/gui/driver cannot persist owner bodies unless ALLOW
    if role_s in {"heal", "gui", "driver"} and verb == "persist":
        if not _allow_match(rel, stage, role_s, field_set, epoch, verb, mutation):
            # Nested VO under EDL: vo_synthesize producer persist still OK
            if stage in row.producers:
                return True, "owner_rerun"
            return False, f"role_{role_s}_pin_only"

    if stage and stage in row.producers:
        # Owner rerun — still respect hard-freeze seat expansion denies above
        if row.write_mode == "operational" or role_s == "ops":
            return True, "operational"
        # Compose must not rewrite gap line copy under hard freeze (floor)
        if (
            epoch == "hard_freeze"
            and rel == "understanding/gap_report.json"
            and stage in {"gap_framing_compose", "nugget_layup_compose"}
            and field_set
            and any("text" in f for f in field_set)
        ):
            return False, "hard_freeze_blocks_compose_copy"
        return True, "owner_rerun"

    if _allow_match(rel, stage, role_s, field_set, epoch, verb, mutation):
        return True, "allow"

    if not stage and role_s == "ops":
        if row and row.write_mode == "operational":
            return True, "ops"
        # Legacy callers omit stage_key; still subject to DENY rows and
        # wrong-stage checks. Prefer stamping stage_key / active_stage.
        if not fail_closed():
            return True, "ops_warn"
        return True, "anonymous_legacy"

    if not fail_closed():
        return True, "not_allow_warn"
    suggested = _suggested_owner_for_deny(
        ctx, rel, stage=stage, epoch=epoch, row=row
    )
    return False, f"not_allow:owner={suggested}"


def parse_authority_denied_resume(
    err: str,
    *,
    ctx: Any = None,
    denied_stage: str = "",
) -> str:
    """Resume pin from ``authority_denied:verb:path:writer:epoch:suggested`` blobs.

    Never returns the denied writer when an ALLOW owner is in the message or
    ownership table (exec_13183 mix→listen_delight thrash).
    """
    text = str(err or "").strip()
    low = text.lower()
    if "authority_denied" not in low:
        return ""
    # Prefer parenthetical owner= from write_permitted reason.
    import re

    m = re.search(r"owner=([a-z0-9_]+)", low)
    if m:
        owner = m.group(1)
        if owner and owner != str(denied_stage or "").strip():
            return owner
    parts = text.split(":")
    # authority_denied : verb : path : writer : epoch : suggested …
    path = ""
    writer = str(denied_stage or "").strip()
    suggested = ""
    if len(parts) >= 6:
        path = parts[2].strip()
        writer = parts[3].strip() or writer
        suggested = parts[5].strip().split()[0].split("(")[0]
    elif len(parts) >= 4:
        path = parts[2].strip()
        writer = parts[3].strip() or writer
    if suggested and suggested not in {"", writer} and suggested.isidentifier():
        return suggested
    if path:
        pin = _suggested_owner_for_deny(ctx, path, stage=writer)
        if pin and pin != writer:
            return pin
        if pin:
            return pin
    if "listen_delight" in low:
        return "listen_delight_audit"
    return ""


def _suggested_owner_for_deny(
    ctx: Any,
    path: str,
    *,
    stage: str = "",
    epoch: str = "",
    row: ArtifactRow | None = None,
) -> str:
    """Prefer calling producer over junction for pre-junction selection denies.

    exec_13177: layup CTA commit denied with suggested_owner=junction_snip_qa
    because authoritative is last producer — heal then pinned the wrong stage.
    """
    rel = _norm_path(path)
    art = row or row_for_path(rel)
    stage_s = str(stage or "").strip()
    ep = str(epoch or "").strip()
    if not ep and ctx is not None:
        try:
            ep = current_epoch(ctx)
        except Exception:
            ep = ""

    if rel == "master/selection.json":
        if art and stage_s and stage_s in art.producers:
            return stage_s
        junction_done = False
        try:
            if ctx is not None and getattr(ctx, "is_done", None):
                junction_done = bool(ctx.is_done("junction_snip_qa"))
        except Exception:
            junction_done = False
        if ep == "junction_committed" or junction_done:
            return (art.heal_pin or art.authoritative) if art else "junction_snip_qa"
        if stage_s in {"full_master_ranking", "selection_order_sanitize", "selection"}:
            return stage_s
        if ep in {"pre_soft_freeze", "soft_freeze", ""} or not ep:
            # S7: CTA omit lives on ranking/sanitize — not layup.
            return "selection_order_sanitize"
        return (art.heal_pin or art.authoritative) if art else "selection_order_sanitize"

    if art and stage_s and stage_s in art.producers:
        return stage_s
    if art:
        return art.heal_pin or art.authoritative or (
            art.producers[-1] if art.producers else ""
        )
    return stage_s


def _gap_line_text_map(doc: Any) -> dict[str, str]:
    out: dict[str, str] = {}
    if not isinstance(doc, dict):
        return out
    for ln in doc.get("interviewer_lines") or []:
        if not isinstance(ln, dict):
            continue
        lid = str(ln.get("line_id") or "").strip()
        if not lid:
            tid = str(ln.get("targets_segment_id") or "").strip()
            lid = f"target:{tid}" if tid else ""
        if not lid:
            continue
        out[lid] = str(ln.get("text") or "")
    return out


def gap_report_body_text_changed(prior: Any, new: Any) -> bool:
    """True when interviewer_lines[].text set differs (by line_id / target)."""
    return _gap_line_text_map(prior) != _gap_line_text_map(new)


def gap_report_body_owner(prior: Any) -> str:
    """The stage that may write the gap report body right now.

    Mirrors assert_gap_report_body_sole_writer and the catalog row: after
    ``nugget_layup_authority`` layup is the sole writer; before it, compose.
    Keyed off the prior on-disk doc because that is what both checks read.
    Four stops came from legitimate repairs presenting their own stage key
    here (the recovery playbook, the hitch's id remap, the VO contract drift
    repair); this is the one answer they all need.
    """
    if isinstance(prior, dict) and prior.get("nugget_layup_authority"):
        return GAP_REPORT_SOLE_BODY_WRITER
    return "gap_framing_compose"


def assert_gap_report_body_sole_writer(
    ctx: Any,
    *,
    stage_key: str | None,
    prior: Any,
    new: Any,
    mutation_class: str | None = None,
    role: str | None = None,
) -> None:
    """S9: refuse foreign gap_report body (text) mutates.

    Pre-authority: only ``gap_framing_compose`` / ``nugget_layup_compose``.
    After ``nugget_layup_authority``: layup is the sole body writer.

    ``mutation_class=segment_id_remap`` may rewrite embedded ``seg_*`` in
    ``line_id`` / targets when spoken ``text`` values are unchanged (hitch).
    Empty ``stage_key`` may rewrite existing body only with explicit
    ``role="ops"`` / ``role="fixture"``.
    """
    mc = str(mutation_class or "").strip()
    if mc == "segment_id_remap":
        # Id-map only: same text bag under remapped keys is not authorship.
        prior_texts = sorted(_gap_line_text_map(prior).values())
        new_texts = sorted(_gap_line_text_map(new).values())
        if prior_texts == new_texts:
            return
    if not gap_report_body_text_changed(prior, new):
        return
    sk = str(stage_key or "").strip()
    role_s = str(role or "").strip()
    if not sk:
        if role_s in {"ops", "fixture"}:
            return
        # Initial persist (no prior body texts) is not a foreign rewrite.
        if not _gap_line_text_map(prior):
            return
        suggested = GAP_REPORT_SOLE_BODY_WRITER
        epoch = ""
        try:
            epoch = current_epoch(ctx) if ctx is not None else ""
        except Exception:
            epoch = ""
        exc = AuthorityDenied(
            f"authority_denied:persist:{GAP_REPORT_REL}::{epoch}:{suggested} "
            "(gap_body_writers:empty_stage_key)",
            path=GAP_REPORT_REL,
            stage_key="",
            role=role_s or "producer",
            epoch=epoch,
            suggested_owner=suggested,
            verb="persist",
        )
        _log_authority_denied(ctx, exc)
        raise exc
    authority = False
    if isinstance(prior, dict) and prior.get("nugget_layup_authority"):
        authority = True
    elif isinstance(new, dict) and new.get("nugget_layup_authority") and sk != "nugget_layup_compose":
        # Non-layup cannot mint authority + body in one write either.
        authority = True
    if authority:
        allowed = {GAP_REPORT_SOLE_BODY_WRITER}
        reason = "gap_body_sole_writer:nugget_layup_compose"
    else:
        allowed = set(GAP_REPORT_BODY_WRITERS)
        reason = "gap_body_writers:gap_framing_compose|nugget_layup_compose"
    if sk in allowed:
        return
    suggested = GAP_REPORT_SOLE_BODY_WRITER if authority else "gap_framing_compose"
    epoch = ""
    try:
        epoch = current_epoch(ctx) if ctx is not None else ""
    except Exception:
        epoch = ""
    exc = AuthorityDenied(
        f"authority_denied:persist:{GAP_REPORT_REL}:{sk}:{epoch}:{suggested} ({reason})",
        path=GAP_REPORT_REL,
        stage_key=sk,
        role="producer",
        epoch=epoch,
        suggested_owner=suggested,
        verb="persist",
    )
    _log_authority_denied(ctx, exc)
    raise exc


def assert_write(
    ctx: Any,
    path: str,
    stage_key: str | None,
    role: str = "producer",
    fields: Iterable[str] | None = None,
    verb: Verb = "persist",
    mutation_class: str | None = None,
) -> None:
    ok, reason = write_permitted(
        ctx,
        path,
        stage_key,
        role=role,
        fields=fields,
        verb=verb,
        mutation_class=mutation_class,
    )
    if ok:
        return
    row = row_for_path(path)
    epoch = ""
    try:
        epoch = current_epoch(ctx) if ctx is not None else ""
    except Exception:
        pass
    suggested = _suggested_owner_for_deny(
        ctx, path, stage=str(stage_key or ""), epoch=epoch, row=row
    )
    if not suggested:
        suggested = heal_pin_for(reason, ctx=ctx) or heal_pin_for(path, ctx=ctx)
    exc = AuthorityDenied(
        f"authority_denied:{verb}:{_norm_path(path)}:{stage_key or ''}:{epoch}:{suggested} ({reason})",
        path=_norm_path(path),
        stage_key=str(stage_key or ""),
        role=role,
        epoch=epoch,
        suggested_owner=suggested,
        verb=verb,
        reason=reason,
    )
    _log_authority_denied(ctx, exc)
    raise exc


#: Run-relative ledger of side-effect writes the ownership table skipped.
FOREIGN_WRITE_LEDGER_REL = "operator/foreign_writes_skipped.jsonl"

#: Documents with their own commit path. Their writers already catch a refusal
#: and retry as the owner or through the committed-tree writer (the live EDL,
#: the selection's single write point, the manifest and boundaries remaps,
#: the one-writer commits for transitions, the gap report and the sound
#: design plan). A silent skip would bypass those fallbacks, so an unkeyed
#: foreign write to one of these still raises.
FOREIGN_SKIP_EXEMPT: frozenset[str] = frozenset(
    {
        "master/edl.json",
        "master/selection.json",
        "master/transitions.json",
        "master/air_order.json",
        "segments/manifest.json",
        "segments/boundaries.json",
        "understanding/gap_report.json",
        "understanding/sound_design_plan.json",
    }
)


def is_foreign_refusal(reason: str) -> bool:
    """True for the table's last fall-through: the writer is neither a producer
    of the path nor named by any ALLOW row. Explicit DENY rows, freeze blocks
    and mutation-class requirements are rules about writers that do have a
    relationship with the path and are not foreign."""
    return str(reason or "").startswith("not_allow:owner=")


def skip_foreign_side_effect(
    ctx: Any,
    path: str,
    *,
    stage_key: str | None,
    role: str | None,
    mutation_class: str | None = None,
) -> bool:
    """Whether this write is a side effect the active stage has no right to (ISSUES 127).

    Shared helpers (seat sync, id remaps, orientation republish) run under
    whichever stage happens to be active and write documents that stage does
    not own. Such a write used to raise ``AuthorityDenied`` from deep inside
    the stage's finish path: the stage's real work was already on disk, the
    denial was logged at error level, and it was counted toward a no-heal
    halt against the active stage. The write was never going to land either
    way; what the refusal should cost is the write, not the stage.

    Only the inferred-writer case is softened: a caller that names a
    ``stage_key`` or a ``role`` is making an ownership claim and still gets
    the exception, so every existing fallback (retry as owner, mirrored
    write) keeps working. Returns True when the write must be skipped.
    """
    if stage_key or str(role or "").strip():
        return False
    try:
        from interview_mux.write_staging import active_stage_id

        active = str(active_stage_id() or "").strip()
    except Exception:
        return False
    if not active:
        return False
    try:
        ok, reason = write_permitted(
            ctx, path, active, role="producer", verb="persist", mutation_class=mutation_class
        )
    except Exception:
        return False
    if ok or not is_foreign_refusal(reason):
        return False
    rel = _norm_path(path)
    if rel in FOREIGN_SKIP_EXEMPT:
        return False
    owner = str(reason).split("=", 1)[-1]
    # Name the helper that made the write: the ledger is how a skipped write
    # is traced back to code (exec_011's was never identified from its log).
    callers: list[str] = []
    try:
        import traceback

        for frame in traceback.extract_stack()[:-1]:
            name = str(frame.filename).replace("\\", "/")
            if "/interview_mux/" not in name:
                continue
            base = name.rsplit("/interview_mux/", 1)[-1]
            if base in {"artifact_ownership.py", "run_context.py", "write_staging.py"}:
                continue
            callers.append(f"{base}:{frame.lineno}:{frame.name}")
        callers = callers[-5:]
    except Exception:
        callers = []
    try:
        ctx.log(
            f"side-effect write skipped: {rel} is not {active}'s to write "
            f"(owner {owner or 'unknown'}); the owner rewrites it",
            level="warning",
            stage=active,
            detail={
                "foreign_write_skipped": True,
                "path": rel,
                "owner": owner,
                "epoch": str(current_epoch(ctx) or ""),
                "callers": callers,
            },
        )
    except Exception:
        pass
    try:
        from datetime import datetime, timezone

        ledger = Path(ctx.run_dir) / FOREIGN_WRITE_LEDGER_REL
        ledger.parent.mkdir(parents=True, exist_ok=True)
        with open(ledger, "a", encoding="utf-8") as fh:
            fh.write(
                json.dumps(
                    {
                        "path": rel,
                        "stage": active,
                        "owner": owner,
                        "callers": callers,
                        "at": datetime.now(timezone.utc).isoformat(),
                    }
                )
                + "\n"
            )
    except Exception:
        pass
    return True


def refuse_and_pin(
    ctx: Any,
    *,
    path: str = "",
    stage_key: str = "",
    reason: str = "",
    verb: str = "persist",
) -> str:
    """Log DENY and return heal pin for the owner (never bare stop)."""
    pin = _suggested_owner_for_deny(
        ctx, path, stage=stage_key
    ) or heal_pin_for(reason, ctx=ctx) or heal_pin_for(path, ctx=ctx) or owner_of(path)
    exc = AuthorityDenied(
        reason or f"authority_denied:{verb}:{path}:{stage_key}",
        path=path,
        stage_key=stage_key,
        suggested_owner=pin,
        verb=verb,
    )
    _log_authority_denied(ctx, exc)
    return pin


def heal_pin_for(token_or_path: str, *, ctx: Any = None) -> str:
    """Map incompleteness token or artifact path → resume stage. No sealed default."""
    raw = str(token_or_path or "").strip()
    if not raw:
        return ""
    low = raw.lower()

    # Path → owner (selection: epoch-aware — not always junction)
    if "/" in raw or raw.endswith(".json") or raw.endswith(".wav"):
        if _norm_path(raw) == "master/selection.json":
            return _suggested_owner_for_deny(ctx, raw)
        own = owner_of(raw)
        if own:
            return own
        row = row_for_path(raw)
        if row:
            return row.heal_pin or row.authoritative

    # Named premature_complete:<class> before HEAL_TOKEN_OWNERS substring steal.
    if "premature_complete" in low:
        try:
            from interview_mux.stage_completion import premature_class_pin

            pc = premature_class_pin(raw, ctx)
            if pc is not None:
                return str(pc)
        except Exception:
            pass

    for needle, pin in HEAL_TOKEN_OWNERS.items():
        if needle and needle in low:
            # Skip bare premature_complete when a named class is present.
            if needle == "premature_complete" and (
                "vo_g1" in low
                or "g1_vo" in low
                or "premature_complete:stage:" in low
                or any(
                    f"premature_complete:{c}" in low
                    for c in (
                        "music_epoch",
                        "mix_seat",
                        "finalize_inputs",
                        "phase_a_edl",
                        "delivery_blocked",
                        "incomplete_after_conductor",
                        "mastering_shape_llm_hollow",
                        "g1_incomplete",
                    )
                )
            ):
                continue
            return pin

    # Delegate to existing producer_pin_for_token but refuse sealed defaults.
    try:
        from interview_mux.stage_completion import producer_pin_for_token

        pin = producer_pin_for_token(raw, default="", ctx=ctx)
        if pin in {"edl", "mix", "master_finalize"} and (
            "seed_order" in low or not raw
        ):
            return ""
        return str(pin or "")
    except Exception:
        return ""


def stage_enter_preflight(ctx: Any, stage_id: str) -> tuple[bool, str, str]:
    """Before enter_stage_staging: stage must be able to persist its primary."""
    sid = str(stage_id or "").strip()
    primary = primary_path_for_stage(sid)
    if not primary:
        return True, "", ""
    ok, reason = write_permitted(ctx, primary, sid, role="producer", verb="persist")
    if ok:
        return True, reason, ""
    pin = heal_pin_for(primary) or heal_pin_for(reason) or owner_of(primary)
    return False, reason, pin


def assert_may_mark_done(ctx: Any, stage: str) -> None:
    """Hollow mark_done DENY — stage must have required outputs present."""
    sid = str(stage or "").strip()
    if not sid:
        return
    # Operator gate sign-offs are intentional marker-only done stamps.
    try:
        from interview_mux.done_authority import GATE_MARKER_ONLY

        if sid in GATE_MARKER_ONLY:
            return
    except Exception:
        if sid in {
            "transcript_review",
            "g1_vo_pickup",
            "delivery_unlock",
        }:
            return
    primary = primary_path_for_stage(sid)
    try:
        from interview_mux.homunculus.agenda import (
            prepare_outputs_present,
            stage_outputs_present,
        )

        if sid in {"audio_preclean", "ingest", "transcribe"}:
            if prepare_outputs_present(ctx, sid):
                return
            raise AuthorityDenied(
                f"authority_denied:mark_done:hollow:{sid}",
                path=primary,
                stage_key=sid,
                suggested_owner=sid,
                verb="mark_done",
            )
        if stage_outputs_present(ctx, sid):
            return
        raise AuthorityDenied(
            f"authority_denied:mark_done:hollow:{sid}",
            path=primary,
            stage_key=sid,
            suggested_owner=sid,
            verb="mark_done",
        )
    except AuthorityDenied:
        raise
    except Exception:
        if primary and not ctx.artifact_exists(primary):
            if sid == "audio_preclean" and ctx.artifact_exists("preclean/skip.json"):
                return
            if sid == "air_contract_sanitize" and ctx.artifact_exists(
                "mastering/mastering_plan.json"
            ):
                return
            raise AuthorityDenied(
                f"authority_denied:mark_done:missing_primary:{sid}:{primary}",
                path=primary,
                stage_key=sid,
                suggested_owner=sid,
                verb="mark_done",
            )


def assert_execute_from_stage(ctx: Any, from_stage: str) -> str:
    """Reject empty from_stage when mid-pipeline done markers exist."""
    pin = str(from_stage or "").strip()
    if pin:
        return pin
    # Empty pin — loud fail when anything past prepare is done.
    try:
        from interview_mux.v2.config import ANALYSIS_ORDER, DELIVERY_ORDER

        mid = False
        for sid in list(ANALYSIS_ORDER) + list(DELIVERY_ORDER):
            if sid in {"audio_preclean", "ingest", "transcribe"}:
                continue
            if ctx.is_done(sid):
                mid = True
                break
        if mid:
            raise AuthorityDenied(
                "authority_denied:execute:empty_from_stage:pause_needs_operator",
                path="",
                stage_key="",
                suggested_owner="",
                verb="execute",
            )
    except AuthorityDenied:
        raise
    except Exception:
        pass
    return ""


# ---------------------------------------------------------------------------
# Telemetry helpers
# ---------------------------------------------------------------------------

def _log_authority_denied(ctx: Any, exc: AuthorityDenied) -> None:
    if ctx is None:
        return
    fp = exc.fingerprint()
    try:
        ctx.log(
            fp,
            # A writer with no row for the path is a side effect of shared
            # helper code, not a broken producer (ISSUES 127).
            level="warning" if getattr(exc, "foreign", False) else "error",
            stage=exc.stage_key or "authority",
            detail={
                "authority_denied": True,
                "path": exc.path,
                "epoch": exc.epoch,
                "suggested_pin": exc.suggested_owner,
                "verb": exc.verb,
            },
        )
    except Exception:
        pass
    try:
        from interview_mux.file_store import read_json, write_json

        rel = "operator/forensics_errors.json"
        path = ctx.final_path(rel) if hasattr(ctx, "final_path") else None
        doc: dict[str, Any] = {"errors": []}
        if path is not None and path.is_file():
            try:
                loaded = read_json(path)
                if isinstance(loaded, dict):
                    doc = loaded
            except Exception:
                pass
        errors = list(doc.get("errors") or [])
        errors.append(
            {
                "fingerprint": fp,
                "path": exc.path,
                "stage_key": exc.stage_key,
                "epoch": exc.epoch,
                "suggested_pin": exc.suggested_owner,
                "verb": exc.verb,
                "message": str(exc),
            }
        )
        doc["errors"] = errors[-200:]
        doc["last_authority_denied"] = fp
        doc["last_suggested_pin"] = exc.suggested_owner
        if path is not None:
            write_json(path, doc)
    except Exception:
        pass
    try:

        def _esr(meta: dict[str, Any]) -> None:
            # Prefer dedicated ESR file when present; also stamp run_meta.
            meta["last_authority_denied"] = fp
            meta["last_suggested_pin"] = exc.suggested_owner

        ctx.mutate_run_meta(_esr)
    except Exception:
        pass
    # No identical-failure row and no halt is stamped here (ISSUES 127). This
    # function runs when the denial is raised, before anyone knows whether the
    # caller handles it, and most callers do (retry as the owner, mirrored
    # write, skip). Counting at raise time turned two handled denials into
    # ``authority_denied_no_heal`` against whichever stage was active. A denial
    # that does fail a stage is counted where every other stage failure is:
    # in the walk's failure path, under the same cap and the same fallbacks.


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------

def _norm_path(path: str) -> str:
    rel = str(path or "").replace("\\", "/").strip()
    while rel.startswith("./"):
        rel = rel[2:]
    return rel.lstrip("/")


def _path_matches(pattern: str, rel: str) -> bool:
    pat = _norm_path(pattern)
    target = _norm_path(rel)
    if pat == "*" or pat == "**":
        return True
    if pat == target:
        return True
    if "*" not in pat and "**" not in pat:
        return False
    # Simple glob: ** / *
    rx = (
        re.escape(pat)
        .replace(r"\*\*", ".*")
        .replace(r"\*", "[^/]*")
    )
    return re.fullmatch(rx, target) is not None


def _field_glob_match(field: str, patterns: Iterable[str]) -> bool:
    f = str(field or "")
    for p in patterns:
        if f == p or (p.endswith("[]") and f.startswith(p[:-2])):
            return True
        if "*" in p:
            rx = re.escape(p).replace(r"\*", ".*")
            if re.fullmatch(rx, f):
                return True
        if p in f:
            return True
    return False


def _allow_match(
    rel: str,
    stage: str,
    role: str,
    fields: tuple[str, ...],
    epoch: str,
    verb: Verb,
    mutation: str = "",
) -> bool:
    for a in ALLOW:
        if a.verb != verb:
            continue
        if not _path_matches(a.path, rel):
            continue
        if a.stage and a.stage != stage:
            continue
        if a.role and a.role != role and not (
            role == "producer" and a.role == "producer"
        ):
            continue
        if a.epochs and epoch and epoch not in a.epochs and "" not in a.epochs:
            continue
        if a.fields and fields and not any(
            _field_glob_match(f, a.fields) for f in fields
        ):
            continue
        if a.allowed_mutations:
            if not mutation or mutation not in a.allowed_mutations:
                continue
        return True
    return False


def render_allow_deny_markdown() -> str:
    lines = [
        "# Artifact ownership — ALLOW / DENY",
        "",
        f"matrix_version: `{matrix_version()}`",
        "",
        "## Primary paths (live stages)",
        "",
        "| stage | path |",
        "|---|---|",
    ]
    for sid, path in sorted(_PRIMARY_BY_STAGE.items()):
        lines.append(f"| `{sid}` | `{path}` |")
    lines.extend(["", "## DENY (high-risk)", "", "| path | stage/role | epoch | reason |", "|---|---|---|---|"])
    for d in DENY:
        who = d.stage or d.role or "*"
        ep = ",".join(d.epochs) if d.epochs else "*"
        lines.append(f"| `{d.path}` | `{who}` | `{ep}` | {d.reason} |")
    lines.extend(
        [
            "",
            "## Notes",
            "",
            "- Owner re-execute is ALLOW; consumer re-execute never becomes owner.",
            "- Nested VO staging under EDL is ALLOW; flushing `vo_pickup` from non-owner pending is DENY.",
            "- Empty heal pin must not execute (no delivery rewind / no music_palette_compose coalesce).",
            "",
        ]
    )
    return "\n".join(lines)


__all__ = [
    "ALLOW",
    "DENY",
    "AuthorityDenied",
    "ArtifactRow",
    "AllowRow",
    "DenyRow",
    "FREEZE_WRITE_POLICY",
    "FreezeWritePolicyRow",
    "HEAL_TOKEN_OWNERS",
    "GAP_REPORT_BODY_WRITERS",
    "GAP_REPORT_SOLE_BODY_WRITER",
    "assert_gap_report_body_sole_writer",
    "gap_report_body_text_changed",
    "HOT_PATHS",
    "MATRIX_VERSION_META_KEY",
    "ONE_WRITER_RAW_ALLOWLIST",
    "assert_execute_from_stage",
    "assert_may_mark_done",
    "assert_write",
    "catalog",
    "check_matrix_version",
    "current_epoch",
    "disk_paths_view",
    "fail_closed",
    "freeze_write_allowed",
    "heal_pin_for",
    "matrix_version",
    "owner_of",
    "owners_of",
    "owners_of_field",
    "primary_path_for_stage",
    "parse_authority_denied_resume",
    "refuse_and_pin",
    "render_allow_deny_markdown",
    "row_for_path",
    "set_fail_closed_default",
    "stamp_matrix_version",
    "stage_enter_preflight",
    "write_permitted",
]

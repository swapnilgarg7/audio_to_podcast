"""Hand-derived stage dependency data, merged into contracts by the generator.

`tools/bootstrap_stage_contracts.py` **overwrites** every
`docs/cross-cutting/stage-contracts/*.yaml` on each run, and it runs as step 1 of
`scripts/verify_artifact_contract.sh`. Anything hand-edited into a contract file
is therefore reverted on the next verify. So the dependency data of plan §4.3
lives here, in the generator's input, and the YAML stays generated.

Organised by operator phase (`interview_mux.v2.phases`) because plan §3.3
populates upstream-first, one group at a time.

## Safety rule for `hard` vs `soft`

Contract `inputs` are **not** inert, contrary to plan §3.4 which claims
`inputs[].producer` is the only live lever. `artifact_lifecycle.run_phase_checks`
walks `contract.inputs`, and for every **hard** dep whose `when` holds it emits
`missing input <path> for <stage>` at `PRESTAGE` and runs `read_stale_guard`
against it. A wrongly-hard dep therefore gates a live stage — the same hazard as
`sufficiency`.

So while contracts are being populated:

* a dep is **hard** only when the stage body already refuses without it
  (`ctx.artifact_exists_required(...)`, or an unconditional read that raises);
* every other real dep is **soft**, which nothing live consumes;
* `producer` is set on both, and the `requires` edges it mints are gated by
  `MUX_CONTRACT_REQUIRES` (`artifact_dependency_graph`).

A soft row may additionally carry `correctness: true`. That marker is read by
`ship_reachability` alone — everything on the dispatch path filters on `hard` —
and it means the master would be *wrong* without the artifact even though the
stage body does not refuse. Justify one the same way hardness is justified: name
the code that degrades silently in its absence.

Evidence for each entry: `tools/extract_stage_artifact_touches.py` over the
stage's entrypoint module, cross-checked against `web/stages.py::STAGE_BY_ID`
(the promotion allowlist `flush_stage_writes` uses) and the guardrail tables in
`artifact_dependency_graph._PROPAGATION_SEEDS` / `stage_completion`.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Any

# ---------------------------------------------------------------------------
# Producer resolution
# ---------------------------------------------------------------------------
# `inputs[].producer` is what mints a `requires` edge, so a guessed producer
# routes a heal to a stage that can only be denied — the inverse of the
# i12–i54 `authority_denied` class, and pinned by
# `tests/test_contract_ownership_xcheck.py::test_declared_producer_is_a_permitted_writer_of_the_input_path`.
#
# So it is derived, never typed by hand:
#
#   1. `STAGE_ARTIFACT_DISK_PATHS` — the declared path SSOT (plan §8.7). Where
#      several stages claim one path the FIRST is the canonical minter and the
#      rest are mutators (`master/selection.json` is minted by
#      `full_master_ranking`, then re-ordered by `selection_order_sanitize`).
#   2. Otherwise the ownership catalog, when it names exactly one pipeline
#      stage — covers sidecars with no SSOT row (`preclean/lineage.json`).
#   3. Otherwise none: the artifact is operator-, GUI- or ops-supplied and has
#      no stage that could heal it.


# Paths with no `STAGE_ARTIFACT_DISK_PATHS` row that several stages may write,
# so rule 2 cannot choose. Each is resolved by reading the writer.
PRODUCER_OVERRIDES: dict[str, str] = {
    # `stages/transcript_review.py` mints and rewrites it across the G0 loop;
    # `transcribe` holds a write row only to seed an empty corrections map.
    "transcript/corrections.json": "transcript_review_build",
    # Minted alongside `master/selection.json` in the ranking pass; the later
    # owners (`selection_order_sanitize`, `transitions`) re-order in place.
    "mastering/media_ip_cta.json": "full_master_ranking",
    "understanding/reorder_bridges.json": "selection_order_sanitize",
    "understanding/speaker_delivery_plan.json": "selection_order_sanitize",
    # Minted by the interviewer-script volley; `gap_framing_recompose` and
    # `air_script_seams` rewrite it during refinement.
    "understanding/gap_framing_plan.json": "gap_framing_compose",
    # Minted by the air-script omit pass; `air_contract_sanitize` re-sanitises.
    "understanding/omit_ledger.json": "air_script_compose",
    # Co-writers exist (missing_framing / hitch confirm); topology is the mint.
    "understanding/flow_adaptation.json": "source_topology_build",
    # Junction autopsy is the heal owner; mix/EDL only rmw.
    "master/seam_autopsy.json": "junction_snip_qa",
}


@lru_cache(maxsize=1)
def _producer_index() -> dict[str, str]:
    from interview_mux.prompt_validation import STAGE_ARTIFACT_DISK_PATHS
    from interview_mux.v2.config import ANALYSIS_ORDER, DELIVERY_ORDER

    stages = set(ANALYSIS_ORDER) | set(DELIVERY_ORDER)
    index: dict[str, str] = {}
    for stage_id, rel in STAGE_ARTIFACT_DISK_PATHS.items():
        if rel not in index and stage_id in stages:
            index[rel] = stage_id
    return index


def producer_for(path: str) -> str | None:
    """Canonical producing stage for an artifact path, or None if operator-supplied."""
    from interview_mux.artifact_ownership import owners_of
    from interview_mux.v2.config import ANALYSIS_ORDER, DELIVERY_ORDER

    hit = PRODUCER_OVERRIDES.get(path) or _producer_index().get(path)
    if hit:
        return hit
    stages = set(ANALYSIS_ORDER) | set(DELIVERY_ORDER)
    owners = [o for o in owners_of(path) if o in stages]
    return owners[0] if len(owners) == 1 else None


def dep(path: str, **extra: Any) -> dict[str, Any]:
    """One `InputDep` row with its producer resolved from the path SSOT.

    Pass ``producer=None`` for a read-modify-write of the stage's own artifact:
    a self-edge is not a dependency and would make the stage its own blocker.
    """
    row: dict[str, Any] = {"path": path}
    producer = extra.pop("producer", producer_for(path))
    if producer:
        row["producer"] = producer
    row.update(extra)
    return row


# Declaring a hard input is not free. `artifact_lifecycle.run_phase_checks` is
# called from `pipeline.run_single_stage` at PRESTAGE and *raises* when a
# declared hard input is missing or stale, and `ship_reachability` reads the
# same list — a hard input on a conditionally-produced artifact turns a run that
# would have degraded gracefully into a hard failure, or a "reachable" verdict
# into a proven HALT. `when` predicates are honoured (`evaluate_when`), so a
# gated dependency is safe as long as the predicate matches the gate the stage
# itself checks.
#
# Every hard input below therefore rests on one of exactly four bases:
#
#   REFUSES   the stage body calls `artifact_exists_required` /
#             `read_artifact_path` / raises on absence itself, so hard adds no
#             failure mode that the code does not already have.
#   PREFLIGHT `llm_preflight._PREFLIGHT_CHECKERS` registers a checker for the
#             stage that errors on absence of this exact path.
#   ALWAYS    the artifact is unconditionally present before dispatch in every
#             posture and branch. Only `_TRANSCRIPT_ALWAYS_PRESENT` qualifies.
#   SEED_ORDER seed law (`_seed_prereq_block`) always requires producer P before
#             consumer C. Without a hard edge the authoritative solver can mark C
#             confident while P is still deferred (D14 topology↔content_context
#             thrash). Soft body degrade is fine; leapfrog under authority is not.
#             Applied by `_apply_seed_order_hard_promotions` below for consecutive
#             soft-underdeclare pairs (allowlisted optionals stay soft).
#
# Anything else is soft. When in doubt for *body* refuse modes, soft — but never
# leave a SEED_ORDER predecessor soft on a confident-capable consumer.
_TRANSCRIPT_ALWAYS_PRESENT = """transcript/full.json — `run_transcribe` either
writes it or raises (no branch completes without it), it sits at ANALYSIS_ORDER
position 2 ahead of every consumer, and nothing gates transcription: no posture,
operator choice or framing decision skips it. Two earlier stages
(`transcript_review_build`, `audio_probe_build`) already hard-require it through
`artifact_exists_required`, so a run missing it has failed long before here."""


def deps(*paths: str) -> list[dict[str, Any]]:
    return [dep(p) for p in paths]


def back(*paths: str) -> list[dict[str, Any]]:
    """Reads of an artifact whose only producer is *downstream* of this stage.

    Real and recorded — a re-entered stage sees artifacts a later stage already
    wrote — but the producer is deliberately omitted. A `requires` edge pointing
    forward in seed order is a phantom blocker by construction
    (`tests/test_contract_generator_roundtrip.py::test_hard_inputs_are_produced_by_an_earlier_seed_position`).
    """
    return [dep(p, producer=None) for p in paths]


# `stage_input_helpers.transcript_quality_for_ctx` reads these three and folds
# them into `payload["transcript_quality"]` for every stage that calls it. It is
# the read that `tests/test_precision_invalidation.py` pins as the near-miss:
# undeclared, cross-module, and invisible to a call-depth-0 scan of the stage
# body. Declared here so the callers' `inputs` are complete at the depth the
# precision gate reasons about.
def transcript_quality_reads() -> list[dict[str, Any]]:
    return [
        *deps("transcript/review_queue.json", "analysis/run_golden_facts.json"),
        # `audio_probes.py:100` is the writer; the catalog row names `transcribe`,
        # so pin the producer the way `vernacular_segment_sanitize` already does.
        dep("transcript/protected_zones.json", producer="audio_probe_build"),
    ]


def rmw(*paths: str) -> list[dict[str, Any]]:
    """Artifacts this stage both reads and writes.

    The producer is omitted for the same reason as `back()`: the canonical minter
    is the declaring stage itself, and a `requires` edge from a stage to itself is
    never satisfiable. The pre-state is still a real input — it decides the
    outcome — so it must stay declared, which is also what keeps `dispatch_delta`
    from stripping it as the stage's own product.
    """
    return [dep(p, producer=None) for p in paths]


# ---------------------------------------------------------------------------
# prepare
# ---------------------------------------------------------------------------
# audio_preclean's real source is `ctx.input_audio()` — outside the run dir, so
# not an artifact. `normalized_rebuild` scope re-cleans ingest output instead,
# hence the conditional soft dep.
_PREPARE: dict[str, dict[str, Any]] = {
    "audio_preclean": {
        "inputs": {
            "soft": [
                {"path": "ingest/normalized.wav", "producer": "ingest"},
            ]
        },
        # The self-skip marker (§8.1) is a real committed write —
        # `stages/audio_preclean.py:33` resolves it through `ctx.final_path` and
        # the catalog names `audio_preclean` its owner — so it belongs in
        # `outputs`, not just in `artifact_lifecycle`'s prose.
        "outputs": [{"path": "preclean/skip.json"}],
        "consumers": ["ingest", "vo_ingest", "vo_synthesize"],
    },
    "ingest": {
        "inputs": {
            "soft": [
                {"path": "preclean/isolated.wav", "producer": "audio_preclean"},
            ]
        },
        # ING-B3: ingest owns waveform_peaks (GUI load-only).
        "outputs": [
            {"path": "ingest/normalized.wav", "staging": False},
            {"path": "ingest/checksums.json"},
            {"path": "ingest/loudness.json"},
            {"path": "ingest/waveform_peaks.json"},
        ],
        "consumers": [
            "transcribe",
            "audio_probe_build",
            "transcript_review_build",
            "source_acoustic_profile",
            "source_topology_build",
            "mix",
            "master_finalize",
        ],
        # Process host (ffmpeg) — no OpenAI volley (ING-B1/B2).
        "remediation": ["full_stage_rerun"],
        "lifecycle_phases": [
            "prestage",
            "pre_call",
            "execute",
            "staged_validate",
            "committed",
            "post_commit_validate",
        ],
    },
    "transcribe": {
        # `run_transcribe` resolves ingest/normalized.wav and raises when absent.
        "inputs": {
            "hard": [
                {"path": "ingest/normalized.wav", "producer": "ingest"},
            ]
        },
        "consumers": [
            "audio_probe_build",
            "transcript_review_build",
            "source_acoustic_profile",
            "interview_spine_build",
            "speaker_roles",
            "content_context",
            "segment_classification",
            "nugget_corpus_mine",
        ],
        # Local STT host — no OpenAI volley (TR-B1/B2).
        "remediation": ["full_stage_rerun"],
        "lifecycle_phases": [
            "prestage",
            "pre_call",
            "execute",
            "staged_validate",
            "committed",
            "post_commit_validate",
        ],
    },
    "transcript_review_build": {
        "inputs": {
            "hard": [
                {"path": "transcript/full.json", "producer": "transcribe"},
                {"path": "ingest/normalized.wav", "producer": "ingest"},
            ],
            "soft": [
                {"path": "transcript/speakers.json", "producer": "transcribe"},
            ],
        },
        "consumers": ["transcript_review"],
        "remediation": ["full_stage_rerun"],
        "lifecycle_phases": [
            "prestage",
            "pre_call",
            "execute",
            "staged_validate",
            "committed",
            "post_commit_validate",
        ],
    },
    "audio_probe_build": {
        # ctx.artifact_exists_required("transcript/full.json") — hard by the body.
        # Soft ingest WAV is fail-open. Drop delivery back-reads (APB-B3) — body
        # never reads edl / topology / gap_fill_skip on the happy path.
        "inputs": {
            "hard": [
                {"path": "transcript/full.json", "producer": "transcribe"},
            ],
            "soft": [
                {"path": "ingest/normalized.wav", "producer": "ingest"},
            ],
        },
        "consumers": [
            "vernacular_segment_sanitize",
            "low_conf_island_scan",
            "boundary_detection",
            "segment_classification",
        ],
    },
}

# ---------------------------------------------------------------------------
# understand-a — transcript -> segments
# ---------------------------------------------------------------------------
# Hardness here is read straight out of the imperative encodings:
# `llm_preflight._PREFLIGHT_CHECKERS` (which artifacts a stage refuses without)
# and the stage bodies' own `raise` sites. Prefer soft when a *config* flag
# alone gates the read and `InputDep.when` cannot express it — but match body
# `raise` sites when an upstream always mints the artifact (including disabled
# stubs), as with `ideal_cuts_propose` → `ideal_cuts_materialize`.
_UNDERSTAND_A: dict[str, dict[str, Any]] = {
    "source_acoustic_profile": {
        # `read_artifact_path("ingest/normalized.wav")` goes through
        # `artifact_exists_required`, so the stage already refuses without it.
        #
        # `transcript/full.json` is hard on the `_TRANSCRIPT_ALWAYS_PRESENT`
        # basis below rather than on a refusal in this body.
        "inputs": {
            "hard": [
                {"path": "ingest/normalized.wav", "producer": "ingest"},
                {"path": "transcript/full.json", "producer": "transcribe"},
            ],
            "soft": [
                {"path": "preclean/isolated.wav", "producer": "audio_preclean"},
            ],
        },
        # readiness is ops/helper write — not SAP primary output (SAP-B4).
        "consumers": [
            "interview_spine_build",
            "boundary_detection",
            "sonic_context_build",
            "sound_design_palettes",
            "mastering_plan_synthesize",
        ],
        "remediation": ["full_stage_rerun"],
        "lifecycle_phases": [
            "prestage",
            "pre_call",
            "execute",
            "staged_validate",
            "committed",
            "post_commit_validate",
        ],
    },
    "interview_spine_build": {
        # Both requirements live behind `if not spine_enabled(): return`.
        # transcript/full.json is read-only; side write is diarization_repairs only (ISB S1–S4).
        "inputs": {
            "hard": [
                {
                    "path": "transcript/full.json",
                    "producer": "transcribe",
                    "when": {"spine_enabled": True},
                },
                {
                    "path": "understanding/source_acoustic_profile.json",
                    "producer": "source_acoustic_profile",
                    "when": {"spine_enabled": True},
                },
            ],
            "soft": [
                {"path": "ingest/normalized.wav", "producer": "ingest"},
                {"path": "preclean/isolated.wav", "producer": "audio_preclean"},
            ],
        },
        "outputs": [
            {"path": "understanding/interview_spine.json"},
            {"path": "transcript/diarization_repairs.json"},
        ],
        "consumers": ["speaker_roles", "content_context", "boundary_detection"],
        "lifecycle_phases": [
            "prestage",
            "pre_call",
            "execute",
            "staged_validate",
            "committed",
            "post_commit_validate",
        ],
    },
    "speaker_roles": {
        # Body requires transcript only; spine is optional enrichment (SR-B1).
        "inputs": {
            "hard": [
                {"path": "transcript/full.json", "producer": "transcribe"},
            ],
            "soft": [
                {
                    "path": "understanding/interview_spine.json",
                    "producer": "interview_spine_build",
                },
                {"path": "transcript/speakers.json", "producer": "transcribe"},
                dep("transcript/diarization_repairs.json"),
            ],
        },
        "consumers": [
            "content_context",
            "boundary_detection",
            "segment_classification",
            "source_topology_build",
        ],
    },
    "source_topology_build": {
        # Body hard-reads speakers + transcript (STB-B4). Soft ingest for samples.
        # Pickup auto-confirm SSOT is gap_vo_gates, not this stage (STB-B3).
        "inputs": {
            "hard": [
                {"path": "understanding/speakers.json", "producer": "speaker_roles"},
                {"path": "transcript/full.json", "producer": "transcribe"},
            ],
            "soft": [
                {"path": "ingest/normalized.wav", "producer": "ingest"},
            ],
        },
        "consumers": [
            "content_context",
            "boundary_detection",
            "segment_classification",
            "boundary_topic_resplit",
            "missing_framing",
            "narrative_arc_plan",
            "full_master_ranking",
            "sound_design_plan",
        ],
        "propagation": [
            "content_context",
            "boundary_detection",
            "segment_classification",
            "missing_framing",
            "narrative_arc_plan",
            "full_master_ranking",
            "sound_design_plan",
        ],
        "remediation": ["full_stage_rerun"],
        "lifecycle_phases": [
            "prestage",
            "pre_call",
            "execute",
            "staged_validate",
            "committed",
            "post_commit_validate",
        ],
    },
    "content_context": {
        # `_preflight_content_context` refuses on speakers + transcript.
        # SEED_ORDER: topology hard — without it authority leapfrogs the deferred
        # empty-hard topology stage (D14 thrash).
        "inputs": {
            "hard": [
                {"path": "transcript/full.json", "producer": "transcribe"},
                {"path": "understanding/speakers.json", "producer": "speaker_roles"},
                {
                    "path": "understanding/source_topology.json",
                    "producer": "source_topology_build",
                },
            ],
            "soft": [
                {"path": "ingest/normalized.wav", "producer": "ingest"},
                # review_queue comes only via transcript_quality_reads() (CC-B2).
                *back("segments/manifest.json"),
                *transcript_quality_reads(),
            ],
        },
        "consumers": [
            "talking_points_compose",
            "ideal_cuts_propose",
            "boundary_detection",
            "segment_classification",
            "content_brief_reanchor",
            "narrative_arc_plan",
            "full_master_ranking",
            "topic_coverage_audit",
        ],
    },
    "talking_points_compose": {
        # Transcript ALWAYS; content_brief SEED_ORDER (was soft under-declare).
        "inputs": {
            "hard": [
                {"path": "transcript/full.json", "producer": "transcribe"},
                {
                    "path": "understanding/content_brief.json",
                    "producer": "content_context",
                },
            ],
            "soft": [
                {"path": "understanding/speakers.json", "producer": "speaker_roles"},
                *transcript_quality_reads(),
            ],
        },
        "consumers": [
            "ideal_cuts_propose",
            "ideal_cuts_materialize",
            "mastering_shape_agenda",
            "nugget_corpus_mine",
            "nugget_layup_compose",
        ],
    },
    "ideal_cuts_propose": {
        # talking_points SEED_ORDER (was soft under-declare).
        "inputs": {
            "hard": [
                {"path": "transcript/full.json", "producer": "transcribe"},
                {
                    "path": "understanding/talking_points.json",
                    "producer": "talking_points_compose",
                },
            ],
            "soft": [
                {"path": "understanding/speakers.json", "producer": "speaker_roles"},
                {"path": "understanding/content_brief.json", "producer": "content_context"},
                *transcript_quality_reads(),
            ],
        },
        "consumers": ["ideal_cuts_materialize", "boundary_detection", "nugget_corpus_mine"],
    },
    "ideal_cuts_materialize": {
        # Body raises without `understanding/ideal_cuts.json` on the enabled
        # path (`ideal_cuts.py:run_ideal_cuts_materialize`). Propose always
        # mints that path (including enable=false placeholder), so hard matches
        # the RuntimeError and seed-order honesty (Wave 2 B1).
        "inputs": {
            "hard": [
                {"path": "understanding/ideal_cuts.json", "producer": "ideal_cuts_propose"},
            ],
            "soft": [
                {"path": "transcript/full.json", "producer": "transcribe"},
                {"path": "ingest/normalized.wav", "producer": "ingest"},
                # `refresh_selection_seed_from_boundaries` / `run_ideal_cuts_
                # materialize` read the boundaries back (`ideal_cuts.py:1001`,
                # `:1121`). Every writer of that path is downstream in seed
                # order, so `back()` omits the producer.
                *back("segments/boundaries.json"),
            ]
        },
        # Read-modify-write, and the write is the stage body's own, not a
        # helper's: `run_ideal_cuts_materialize` rewrites the boundaries it
        # materialised cuts against, and the catalog names this stage a producer
        # of the path. Undeclared it was the one blocking finding in
        # `understand-a` under `--entrypoint-only --call-depth 0`.
        "outputs": [{"path": "segments/boundaries.json"}],
        "consumers": [
            "boundary_detection",
            "segment_classification",
            "mastering_shape_agenda",
            "full_master_ranking",
        ],
    },
    "boundary_detection": {
        # BD-B1: hard = LLM build_input hard-reads (transcript/speakers/brief).
        # ideal_cuts_materialized stays soft — skip path uses bind stamp when
        # present; LLM path treats it as optional enrich (BD-B1 demote).
        # BD-B2: no review_queue / golden_facts / protected_zones (not in payload).
        "inputs": {
            "hard": [
                {"path": "transcript/full.json", "producer": "transcribe"},
                {"path": "understanding/speakers.json", "producer": "speaker_roles"},
                {
                    "path": "understanding/content_brief.json",
                    "producer": "content_context",
                },
            ],
            "soft": [
                {
                    "path": "understanding/talking_points.json",
                    "producer": "talking_points_compose",
                },
                {
                    "path": "understanding/ideal_cuts_materialized.json",
                    "producer": "ideal_cuts_materialize",
                },
                # The proposal itself, not only the materialised form —
                # `build_input` passes both (`stages/segmentation.py`).
                {"path": "understanding/ideal_cuts.json", "producer": "ideal_cuts_propose"},
                {
                    "path": "understanding/source_acoustic_profile.json",
                    "producer": "source_acoustic_profile",
                },
            ],
        },
        "consumers": [
            "segment_classification",
            "content_brief_reanchor",
            "sonic_context_build",
            "sound_design_palettes",
            "missing_framing",
        ],
        # BD-B3: drop retired optimal_questions from invalidates.
        "propagation": [
            "segment_classification",
            "content_brief_reanchor",
            "sonic_context_build",
            "sound_design_palettes",
            "missing_framing",
        ],
    },
    "segment_classification": {
        # SC-B1: hard matches SEGMENTATION_INPUT_DEPS (boundaries via
        # LLM_UPSTREAM_STAGE; transcript+speakers hard here; brief soft).
        # SC-B2: drop unused master/edl.json; keep transcript_quality_reads —
        # build_classification_payload calls transcript_quality_for_ctx.
        "inputs": {
            "hard": [
                {"path": "transcript/full.json", "producer": "transcribe"},
                {"path": "understanding/speakers.json", "producer": "speaker_roles"},
            ],
            "soft": [
                {
                    "path": "understanding/talking_points.json",
                    "producer": "talking_points_compose",
                },
                # `build_classification_payload` passes `bundle.content_brief`
                # (`segmentation_input_resolver.py:226`).
                {"path": "understanding/content_brief.json", "producer": "content_context"},
                # SEGMENTATION_INPUT_DEPS soft (resolver loads; ops/profile owned).
                *deps("understanding/analysis_state.json"),
                # Det path reads materialized cuts (`try_deterministic_classification`).
                {
                    "path": "understanding/ideal_cuts_materialized.json",
                    "producer": "ideal_cuts_materialize",
                },
                *transcript_quality_reads(),
            ],
        },
        "consumers": [
            "content_brief_reanchor",
            "vernacular_segment_sanitize",
            "low_conf_island_scan",
            "connector_fuse_pass",
            "sonic_context_build",
            "sound_design_palettes",
            "missing_framing",
        ],
        # SC-B3: drop retired optimal_questions from invalidates.
        "propagation": [
            "content_brief_reanchor",
            "sonic_context_build",
            "sound_design_palettes",
            "missing_framing",
        ],
    },
}

# ---------------------------------------------------------------------------
# understand-b — segment refinement
# ---------------------------------------------------------------------------
# Four of the six stages here short-circuit rather than refuse:
# `framing_posture_decide` returns a stub when the homunculus feature or the
# posture flag is off, `vernacular_segment_sanitize` writes a skip report when
# zones or the manifest are absent, and `low_conf_island_scan` /
# `connector_fuse_pass` return on `analysis.*.enabled=false`. None of their deps
# may be hard.
_UNDERSTAND_B: dict[str, dict[str, Any]] = {
    "content_brief_reanchor": {
        # `_preflight_content_brief_reanchor` refuses without the prior brief;
        # segments/manifest.json is the LLM_UPSTREAM_STAGE hard dep already.
        # CBR-B1: speakers hard — build_input hard-reads understanding/speakers.json.
        "inputs": {
            "hard": [
                {"path": "understanding/content_brief.json", "producer": "content_context"},
                {"path": "understanding/speakers.json", "producer": "speaker_roles"},
            ],
            "soft": [
                {"path": "segments/boundaries.json", "producer": "boundary_detection"},
            ],
        },
        "consumers": [
            "boundary_topic_resplit",
            "sonic_context_build",
            "sound_design_palettes",
            "missing_framing",
        ],
        # CBR-B2: drop retired optimal_questions from invalidates.
        "propagation": [
            "boundary_topic_resplit",
            "sonic_context_build",
            "sound_design_palettes",
            "missing_framing",
        ],
    },
    "framing_posture_decide": {
        "inputs": {
            "soft": [
                {"path": "understanding/speakers.json", "producer": "speaker_roles"},
                {
                    "path": "understanding/source_topology.json",
                    "producer": "source_topology_build",
                },
                {
                    "path": "understanding/flow_adaptation.json",
                    "producer": "source_topology_build",
                },
            ]
        },
        "consumers": ["missing_framing", "gap_framing_compose", "transitions", "vo_synthesize"],
    },
    "boundary_topic_resplit": {
        # content_brief.json arrives from LLM_UPSTREAM_STAGE=content_brief_reanchor.
        "inputs": {
            "soft": [
                {"path": "segments/boundaries.json", "producer": "boundary_detection"},
                {"path": "segments/manifest.json", "producer": "segment_classification"},
                {"path": "transcript/full.json", "producer": "transcribe"},
                {"path": "understanding/speakers.json", "producer": "speaker_roles"},
                {
                    "path": "understanding/flow_adaptation.json",
                    "producer": "source_topology_build",
                },
                # Cross-module helper reads, invisible to a call-depth-0 scan of
                # the stage body: `interview_spine.compact.attach_spine_to_payload`
                # and `source_topology.attach_adaptation_to_payload` on the LLM
                # path, `split_plan.propose_split_plan` on the way out.
                *deps(
                    "understanding/interview_spine.json",
                    "understanding/source_topology.json",
                    "understanding/ideal_cuts_materialized.json",
                ),
                # `full_master_ranking` is the only writer and sits downstream.
                *back("understanding/speaker_delivery_plan.json"),
                # Operator NLE edits — `ops`-written, no stage produces it.
                *back("segments/nle_edits.json"),
                # Own artifact: `propose_split_plan` reads the prior plan back.
                *rmw("segments/split_plan.json"),
            ]
        },
        "consumers": [
            "segment_classification",
            "vernacular_segment_sanitize",
            "low_conf_island_scan",
            "connector_fuse_pass",
            "sonic_context_build",
            "sound_design_palettes",
            "missing_framing",
        ],
    },
    "vernacular_segment_sanitize": {
        # Body skip-completes without hard inputs (HS-5). Do not empty-hard-promote
        # predecessor boundaries — unused by run_vernacular_segment_sanitize.
        "inputs": {
            "hard": [],
            "soft": [
                {
                    "path": "transcript/protected_zones.json",
                    "producer": "audio_probe_build",
                },
                {"path": "segments/manifest.json", "producer": "segment_classification"},
                # `enforcement_mode_for_ctx` / `load_must_keep_segment_ids` read
                # the golden facts (`stages/audio_probes.py:38`, `:53`), and the
                # resplit reads the flows and the audio tags (`:287`, `:289`).
                *deps("analysis/run_golden_facts.json", "transcript/speaker_flows.json"),
                # Own artifact, read for its pre-state.
                *rmw("vernacular/audio_tags_by_flow.json"),
            ]
        },
        # The resplit rewrites the protected zones it just read
        # (`stages/audio_probes.py:420`) — read-modify-write, so the path is both
        # a declared input and a declared output.
        "outputs": [{"path": "transcript/protected_zones.json"}],
        "consumers": ["low_conf_island_scan", "connector_fuse_pass", "full_master_ranking"],
        # Process host stage — no OpenAI volley.
        "remediation": ["full_stage_rerun"],
        "lifecycle_phases": [
            "prestage",
            "pre_call",
            "execute",
            "staged_validate",
            "committed",
            "post_commit_validate",
        ],
    },
    "low_conf_island_scan": {
        "inputs": {
            "soft": [
                {"path": "segments/manifest.json", "producer": "segment_classification"},
                {"path": "transcript/full.json", "producer": "transcribe"},
                {
                    "path": "analysis/vernacular_must_keep.json",
                    "producer": "vernacular_segment_sanitize",
                },
            ]
        },
        "consumers": ["connector_fuse_pass", "full_master_ranking"],
    },
    "connector_fuse_pass": {
        # Soft-only: missing manifest / disabled → persist_fuse_skip (HS-3).
        # Do not hard-require low_conf_islands (CFP-B1).
        "inputs": {
            "hard": [],
            "soft": [
                {"path": "segments/manifest.json", "producer": "segment_classification"},
                {"path": "transcript/full.json", "producer": "transcribe"},
                {
                    "path": "analysis/low_conf_islands.json",
                    "producer": "low_conf_island_scan",
                },
                {
                    "path": "analysis/low_conf_must_keep.json",
                    "producer": "low_conf_island_scan",
                },
            ]
        },
        "consumers": [
            "connector_fuse_pass_pre_ranking",
            "topic_coverage_audit",
            "narrative_arc_plan",
            "full_master_ranking",
            "nugget_corpus_mine",
            "nugget_layup_compose",
            "transitions",
            "edl",
        ],
        "remediation": ["volley_retry", "full_stage_rerun"],
    },
}

# ---------------------------------------------------------------------------
# understand-c — sonic context, mastering research, Shape
# ---------------------------------------------------------------------------
# Soft-by-default group: research/Shape stages degrade rather than refuse.
# Exception: `sonic_context_build` hard-matches `stage_input_checks`
# `_check_sonic_context_build` (brief + manifest + acoustic profile) — not the
# prior-seed fuse audit (SCB-B1 / clinic CODE_DOC_CONFLICT). Enrichment reads
# stay soft via `_read_if_dict`.
#
# * the three `mastering_research_*` stages are a presence scan — `_probe` walks
#   `FIELD_PROBES` with `ctx.artifact_exists` and writes `status:
#   skipped_or_thin` for whatever is absent. Missing upstream is the expected
#   state at Shape time (W4–W8 are thin by construction, per `SHAPE_CORE_WAVES`).
# * the three Shape stages short-circuit on `soft_gate_enabled()` and fall
#   through a heuristic ladder ending in `forced_sparse_plan`; the `except`
#   arms still write a schema-valid artifact.
#
# So a hard dep on research/Shape would gate a stage that is built never to be
# gated — `artifact_lifecycle.run_phase_checks` would refuse PRESTAGE on a tape
# where the stage is designed to produce a thin-but-valid artifact.
#
# The research probe set is derived from `mastering_research.FIELD_PROBES`
# rather than transcribed, so the contract cannot rot when a probe is added.
_RESEARCH_PROBE_EXCLUDE = frozenset(
    {
        # Recorder infrastructure, not artifacts (`contract_conformance.is_ignored`).
        "run_meta.json",
        "understanding/analysis_state.json",
        ".stage_done",
        "llm_audit.jsonl",
    }
)


def research_probe_paths() -> list[str]:
    """Every artifact the research waves probe, in `FIELD_PROBES` order."""
    from interview_mux.mastering_research import FIELD_PROBES

    seen: list[str] = []
    for probes in FIELD_PROBES.values():
        for rel in probes:
            if rel not in seen and rel not in _RESEARCH_PROBE_EXCLUDE:
                seen.append(rel)
    return seen


def _research_probe_inputs() -> list[dict[str, Any]]:
    return [dep(rel) for rel in sorted(research_probe_paths())]


# Readers of `understanding/sonic_context.json` and `understanding/sound_design_plan.json`
# — both are read by the whole sound/build/ship tail, so the consumer lists are
# long by nature rather than by over-reporting.
_SONIC_CONSUMERS = [
    "sound_design_palettes",
    "soundscape_policy_build",
    "episode_structure_compose",
    "sound_design_plan",
    "edl_narrative_audit",
    "music_palette_compose",
    "sfx_prompt_craft",
    "mix",
    "junction_snip_qa",
    "master_finalize",
]

_UNDERSTAND_C: dict[str, dict[str, Any]] = {
    "sonic_context_build": {
        # Hard = `_check_sonic_context_build` SSOT (SCB-B1). Soft = enrichment
        # via `_read_if_dict` (built_from = whatever was present).
        "inputs": {
            "hard": [
                {"path": "understanding/content_brief.json", "producer": "content_context"},
                {
                    "path": "segments/manifest.json",
                    "producer": "segment_classification",
                },
                {
                    "path": "understanding/source_acoustic_profile.json",
                    "producer": "source_acoustic_profile",
                },
            ],
            "soft": deps(
                "understanding/speakers.json",
                "understanding/value_features.json",
                "understanding/gap_report.json",
                "transcript/full.json",
                "transcript/disfluencies.json",
                "master/narrative_plan.json",
            ),
        },
        "consumers": list(_SONIC_CONSUMERS),
    },
    "sound_design_palettes": {
        # Read-modify-write: `_load_sound_design_plan` reads the plan, the stage
        # folds palettes / coherence into it and writes it back. It MUST be
        # declared as an input as well as an output or the no-delta guard strips
        # it as "the stage's own product" and refuses a legitimate re-run
        # (tests/test_contract_input_declaration_safety.py). `producer=None`
        # because the canonical producer of that path is this stage itself.
        "inputs": {
            "soft": [
                dep("understanding/sound_design_plan.json", producer=None),
                *deps(
                    "understanding/sonic_context.json",
                    "understanding/content_brief.json",
                    "understanding/source_acoustic_profile.json",
                    "understanding/value_features.json",
                    "understanding/flow_adaptation.json",
                    "understanding/source_topology.json",
                    "segments/manifest.json",
                ),
            ]
        },
        "consumers": [c for c in _SONIC_CONSUMERS if c != "sound_design_palettes"]
        + ["delivery_brief_build"],
    },
    "mastering_research_routing": {
        # `_routing_user_payload` runs only when `mastering.research.llm.enabled`
        # (default false), and it probes the whole catalog via
        # `_probe_presence_map`. Config-gated, so soft — `InputDep.when` has no
        # config predicate and a hard dep would gate the disabled branch too.
        # Body never hard-requires SDP (stub path) — keep hard:[] (CSP-02).
        "inputs": {"soft": _research_probe_inputs()},
        # Nothing reads `mastering/research/routing.json`: `run_research_rollup`
        # re-runs the routing rather than reading its artifact.
        "consumers": [],
    },
    "mastering_research_waves": {
        # `_probe` is unconditional here — every field report records which of
        # its `FIELD_PROBES` exist. Body never reads routing.json (MRW-B1) —
        # keep hard:[] (allowlisted empty-hard; do not empty-hard-promote
        # prior-seed routing).
        "inputs": {"soft": _research_probe_inputs()},
        "consumers": ["mastering_research_rollup"],
    },
    "mastering_research_rollup": {
        # `run_research_rollup` calls routing and every wave itself, then reads
        # the per-field reports back out of `mastering/research/`. Body never
        # requires waves.json (re-probes) — hard:[] (MRRoll soft/hard align).
        "inputs": {"soft": _research_probe_inputs()},
        # MRRoll-B2: match RESEARCH_CONSUMER_STAGES (Shape quartet + missing_framing + gap compose).
        "consumers": [
            "mastering_shape_agenda",
            "mastering_shape_candidates",
            "mastering_plan_synthesize",
            "mastering_plan_confirm",
            "missing_framing",
            "gap_framing_compose",
        ],
        "outputs": [
            {"path": "glob:mastering/research/*.json"},
            {"path": "mastering/research/routing.json"},
        ],
    },
    "mastering_shape_agenda": {
        # `_style_hints` + `_talking_points_bound` + `_shape_llm_user_payload`
        # + `compile_shape_evidence`.
        "inputs": {
            "soft": deps(
                "mastering/research_dossier.json",
                "understanding/content_brief.json",
                "understanding/source_topology.json",
                "understanding/talking_points.json",
                "understanding/ideal_cuts_materialized.json",
                "understanding/gap_evaluations.json",
                "understanding/gap_report.json",
                "master/selection.json",
                "segments/manifest.json",
                "mastering/mastering_plan.json",
                "mastering/shape/candidates.json",
            )
        },
        "consumers": ["mastering_shape_candidates"],
        "outputs": [
            {"path": "mastering/shape/eval_rubric.json"},
            {"path": "glob:mastering/evidence_packets/*.json"},
        ],
        "lifecycle_phases": [
            "prestage",
            "pre_call",
            "execute",
            "staged_validate",
            "committed",
            "post_commit_validate",
        ],
    },
    "mastering_shape_candidates": {
        "inputs": {
            "soft": deps(
                "mastering/shape/agenda.json",
                "mastering/research_dossier.json",
                "understanding/content_brief.json",
                "understanding/source_topology.json",
                "understanding/gap_evaluations.json",
                "understanding/gap_report.json",
                "master/selection.json",
                "segments/manifest.json",
                "mastering/mastering_plan.json",
            )
        },
        "consumers": ["mastering_plan_synthesize"],
        "outputs": [
            # `mastering.shape.soft_gate.skip_diversity` defaults true, so this
            # is written only on the diversity branch.
            {"path": "mastering/shape/diversity_report.json"},
            {"path": "glob:mastering/evidence_packets/*.json"},
        ],
    },
    "mastering_plan_synthesize": {
        # `_shape_llm_user_payload` + `compile_shape_evidence` +
        # `shape_order_emit.attach_shape_order`.
        #
        # gap_evaluations / gap_report are soft + forward: missing_framing and
        # gap_framing_compose run *after* synthesize in seed order (Pass1
        # provisional). Absence is expected; Pass2 confirm re-scores with gaps.
        # candidates soft here → promoted hard by _SEED_ORDER_PROMOTE; body still
        # fail-opens to forced_sparse when empty (MPS contract honesty).
        "inputs": {
            "soft": deps(
                "mastering/shape/candidates.json",
                "mastering/shape/agenda.json",
                "mastering/research_dossier.json",
                "understanding/content_brief.json",
                "understanding/source_topology.json",
                "understanding/gap_evaluations.json",
                "understanding/gap_report.json",
                "understanding/episode_structure.json",
                "master/selection.json",
                "master/narrative_plan.json",
                "segments/manifest.json",
            )
        },
        # Every downstream stage that binds narrative_mode / montage grammar.
        "consumers": [
            "missing_framing",
            "mastering_plan_confirm",
            "gap_framing_compose",
            "delivery_brief_build",
            "soundscape_policy_build",
            "episode_structure_compose",
            "narrative_arc_plan",
            "chapter_close_hitch",
            "full_master_ranking",
            "air_script_compose",
            "nugget_corpus_mine",
            "information_package_plan",
            "nugget_layup_compose",
            "refinement_agenda",
            "gap_framing_recompose",
            "selection_framing_apply",
            "air_script_seams",
            "air_contract_sanitize",
            "transitions",
            "sound_design_plan",
            "vo_synthesize",
            "edl_narrative_audit",
            "edl",
            "listen_delight_audit",
            "music_palette_compose",
            "mix",
            "junction_snip_qa",
            "master_finalize",
        ],
        "outputs": [{"path": "glob:mastering/evidence_packets/*.json"}],
    },
}

# ---------------------------------------------------------------------------
# fill_gaps — gap evaluation, framing script, delivery/soundscape/episode briefs
# ---------------------------------------------------------------------------
# Hardness comes from `llm_preflight._PREFLIGHT_CHECKERS` /
# `_FLOW_UPSTREAM_ARTIFACTS` — the table that decides which artifacts a stage
# refuses without. Only `missing_framing` has an entry in this group.
#
# Four of the six stages read their own artifact and write it back, which is the
# trap that stranded `speaker_roles` and `vernacular_segment_sanitize`: declaring
# the artifact as an output alone lets the no-delta guard strip it as the
# stage's own product and then refuse a legitimate re-run. Each is declared as
# BOTH, with `producer=None` where this stage is the canonical minter.
#
# `mastering/mastering_plan.json` is consumed by essentially the whole delivery
# tail, so `mastering_plan_confirm` shares `_PLAN_CONSUMERS` with
# `mastering_plan_synthesize`.
_PLAN_CONSUMERS = list(_UNDERSTAND_C["mastering_plan_synthesize"]["consumers"])

_FILL_GAPS: dict[str, dict[str, Any]] = {
    "missing_framing": {
        # `_preflight_missing_framing` refuses on both, and on an empty manifest.
        # `segments/boundaries.json` already arrives as the LLM_UPSTREAM_STAGE dep.
        "inputs": {
            "hard": deps(
                "understanding/content_brief.json",
                "segments/manifest.json",
            ),
            "soft": [
                dep("understanding/gap_evaluations.json", producer=None),
                *deps(
                    "transcript/full.json",
                    "understanding/speakers.json",
                    "understanding/source_topology.json",
                    "understanding/talking_points.json",
                    "understanding/flow_adaptation.json",
                    "understanding/value_features.json",
                    "understanding/gap_report.json",
                    "understanding/speaker_delivery_plan.json",
                    "mastering/mastering_plan.json",
                    "master/selection.json",
                    "master/narrative_plan.json",
                ),
            ],
        },
        "consumers": [
            "mastering_plan_confirm",
            "gap_framing_compose",
            "air_script_compose",
            "nugget_layup_compose",
            "refinement_agenda",
            "gap_framing_recompose",
            "air_script_seams",
            "edl",
        ],
    },
    "mastering_plan_confirm": {
        # Pass2 of the Shape soft gate: re-reads the provisional plan it is about
        # to overwrite, re-scores it against the gap evidence, writes it back.
        "inputs": {
            "soft": [
                dep("mastering/mastering_plan.json", producer="mastering_plan_synthesize"),
                *deps(
                    "mastering/research_dossier.json",
                    "understanding/gap_evaluations.json",
                    "understanding/gap_report.json",
                    "understanding/content_brief.json",
                    "understanding/source_topology.json",
                    "understanding/episode_structure.json",
                    "master/selection.json",
                    "master/narrative_plan.json",
                    "segments/manifest.json",
                ),
            ]
        },
        # MPC-B2: drop upstream missing_framing (seed: synthesize → missing_framing → confirm).
        "consumers": [
            c
            for c in _PLAN_CONSUMERS
            if c not in {"mastering_plan_confirm", "missing_framing"}
        ],
        "outputs": [
            {"path": "mastering/shadow_diff.json"},
            {"path": "glob:mastering/evidence_packets/*.json"},
        ],
    },
    "gap_framing_compose": {
        # No preflight entry: the stage no-ops under layup authority or a seat
        # freeze rather than refusing, so nothing beyond the LLM_UPSTREAM dep on
        # `understanding/gap_evaluations.json` is hard.
        "inputs": {
            "soft": [
                dep("understanding/gap_report.json", producer=None),
                *deps(
                    "understanding/nugget_layup_plan.json",
                    "understanding/nugget_corpus.json",
                    "understanding/content_brief.json",
                    "understanding/delivery_brief.json",
                    "understanding/episode_structure.json",
                    "understanding/speakers.json",
                    "understanding/source_topology.json",
                    "understanding/talking_points.json",
                    "understanding/flow_adaptation.json",
                    "understanding/value_features.json",
                    "understanding/reorder_bridges.json",
                    "understanding/speaker_delivery_plan.json",
                    "segments/manifest.json",
                    "segments/boundaries.json",
                    "master/selection.json",
                    "master/narrative_plan.json",
                    "mastering/mastering_plan.json",
                ),
            ]
        },
        "consumers": [
            "mastering_plan_confirm",
            "delivery_brief_build",
            "chapter_close_hitch",
            "full_master_ranking",
            "nugget_corpus_mine",
            "nugget_layup_compose",
            "gap_report_sanitize",
            "gap_framing_recompose",
            "selection_framing_apply",
            "air_script_seams",
            "air_contract_sanitize",
            "transitions",
            "sound_design_plan",
            "vo_line_adjudicate",
            "vo_synthesize",
            "sound_design_vo_finalize",
            "edl",
            "assembly_preview",
            "listen_delight_audit",
            "mix",
            "junction_snip_qa",
            "master_finalize",
            "master_transcript_build",
            "podcast_publish",
        ],
        "outputs": [
            {"path": "understanding/gap_framing_plan.json"},
            {"path": "understanding/gap_vo_context_audit.json"},
        ],
    },
    "delivery_brief_build": {
        "inputs": {
            "soft": [
                dep("understanding/delivery_brief.json", producer=None),
                *deps(
                    "understanding/content_brief.json",
                    "understanding/speakers.json",
                    "understanding/source_topology.json",
                    "understanding/flow_adaptation.json",
                    "understanding/sound_design_plan.json",
                    "mastering/mastering_plan.json",
                    "master/selection.json",
                    "segments/manifest.json",
                    "transcript/full.json",
                    "ingest/checksums.json",
                ),
            ]
        },
        # gap_framing_compose seeds *before* this stage; compose may soft-read an
        # existing brief on re-entry, but it is not a downstream consumer of a
        # first-pass brief write (circular vs ANALYSIS_ORDER).
        "consumers": [
            "soundscape_policy_build",
            "full_master_ranking",
            "transitions",
            "sound_design_plan",
            "listen_delight_audit",
            "music_palette_compose",
            "mix",
        ],
    },
    "soundscape_policy_build": {
        "inputs": {
            "soft": [
                dep("understanding/soundscape_policy.json", producer=None),
                *deps(
                    "understanding/sonic_context.json",
                    "understanding/sound_design_plan.json",
                    "understanding/delivery_brief.json",
                    "understanding/source_acoustic_profile.json",
                    "mastering/mastering_plan.json",
                    "master/selection.json",
                    "master/narrative_plan.json",
                    "segments/manifest.json",
                ),
            ]
        },
        "consumers": ["sound_design_plan", "music_palette_compose", "sfx_prompt_craft"],
    },
    "episode_structure_compose": {
        # The last three are a cross-module helper read no stage-body scan can
        # see: `run_episode_structure_compose` calls
        # `analysis_memory.update_completion_from_analysis`, which walks
        # `llm_flow_hardening.ANALYSIS_READY_ARTIFACT_PATHS` through
        # `artifact_completeness.artifact_status` — a content read that parses
        # and schema-validates each one, not an existence probe.
        "inputs": {
            "soft": deps(
                "understanding/content_brief.json",
                "understanding/speakers.json",
                "understanding/sonic_context.json",
                "understanding/sound_design_plan.json",
                "understanding/source_acoustic_profile.json",
                "mastering/mastering_plan.json",
                "master/selection.json",
                "segments/manifest.json",
                "segments/boundaries.json",
                "transcript/full.json",
                "understanding/gap_evaluations.json",
                "understanding/gap_report.json",
                "understanding/delivery_brief.json",
            )
        },
        "outputs": [{"path": "understanding/episode_structure_compact.txt"}],
        # Seed order places synthesize/confirm/gap_compose *before* this stage;
        # those soft-reads are re-entry/forward edges, not first-pass consumers
        # (ESC-B2 — same circular cleanup as delivery_brief ↔ gap_compose).
        "consumers": [
            "narrative_arc_plan",
            "chapter_close_hitch",
            "full_master_ranking",
            "gap_framing_recompose",
            "selection_framing_apply",
            "air_script_seams",
            "transitions",
            "sound_design_plan",
            "edl",
            "mix",
            "junction_snip_qa",
        ],
    },
}

# ---------------------------------------------------------------------------
# plan_rank — coverage, narrative, ranking, layups, air contract, transitions
# ---------------------------------------------------------------------------
# Hardness SSOT for ranking is `stage_input_checks._check_full_master_ranking`
# (FMR-B1). Other plan_rank stages still track `llm_preflight`:
#
#   narrative_arc_plan   <- master/coverage_audit.json
#   full_master_ranking  <- narrative_plan + manifest + gap_report
#   transitions          <- master/selection.json
#
# `topic_coverage_audit` runs `_preflight_pre_delivery`, which asserts every
# `ANALYSIS_READY_ARTIFACT_PATHS` entry is `complete` — a *status* test, not an
# existence test. A hard contract dep is an existence test that additionally
# arms `artifact_lifecycle.read_stale_guard`, so promoting those to hard would
# add a refusal the stage does not have today. They stay soft.
#
# This group is where read-modify-write is the norm rather than the exception:
# `master/selection.json` is minted by `full_master_ranking` and then rewritten
# by `selection_order_sanitize`, `mastering/mastering_plan.json` by four air
# stages, `understanding/gap_report.json` by three, and
# `connector_fuse_pass_pre_ranking` re-fuses `segments/manifest.json` in place.
# Every one of those is declared as input AND output; see the module docstring
# of `tests/test_contract_input_declaration_safety.py` for why omitting the
# input strands the stage.
_SELECTION_CONSUMERS = [
    "missing_framing",
    "mastering_plan_confirm",
    "gap_framing_compose",
    "delivery_brief_build",
    "soundscape_policy_build",
    "episode_structure_compose",
    "chapter_close_hitch",
    "selection_order_sanitize",
    "air_script_compose",
    "nugget_corpus_mine",
    "information_package_plan",
    "nugget_layup_compose",
    "gap_report_sanitize",
    "gap_framing_recompose",
    "selection_framing_apply",
    "air_script_seams",
    "air_contract_sanitize",
    "transitions",
    "sound_design_plan",
    "sound_design_vo_finalize",
    "edl_narrative_audit",
    "edl",
    "listen_delight_audit",
    "music_palette_compose",
    "sfx_prompt_craft",
    "mix",
    "junction_snip_qa",
    "master_finalize",
    "podcast_publish",
]

_PLAN_RANK: dict[str, dict[str, Any]] = {
    "topic_coverage_audit": {
        "inputs": {
            "soft": [
                dep("master/coverage_audit.json", producer=None),
                *deps(
                    "understanding/content_brief.json",
                    "understanding/source_topology.json",
                    "understanding/flow_adaptation.json",
                    "understanding/value_features.json",
                    "understanding/speakers.json",
                    "understanding/gap_evaluations.json",
                    "understanding/gap_report.json",
                    "understanding/interview_spine.json",
                    "understanding/sound_design_plan.json",
                    "segments/manifest.json",
                    "segments/boundaries.json",
                    "transcript/full.json",
                    "transcript/review_queue.json",
                ),
                # Recorded (`MUX_CONTRACT_RECORD=1`): skip flags and the ranking
                # sidecar, neither of which has an upstream producer here.
                *back(
                    "analysis_complete.json",
                    "understanding/gap_fill_skip.json",
                    "understanding/speaker_delivery_plan.json",
                ),
            ]
        },
        # TCA: pin ADG `_PROPAGATION_SEEDS` + contract invalidates.
        "propagation": [
            "narrative_arc_plan",
            "connector_fuse_pass_pre_ranking",
            "full_master_ranking",
            "nugget_corpus_mine",
            "nugget_layup_compose",
            "transitions",
        ],
        "consumers": [
            "narrative_arc_plan",
            "full_master_ranking",
            "refinement_agenda",
            "gap_framing_recompose",
            "selection_framing_apply",
            "transitions",
            "edl_narrative_audit",
            "junction_snip_qa",
        ],
    },
    "narrative_arc_plan": {
        # coverage_audit hard via LLM_UPSTREAM_STAGE. NAP-B1: content_brief hard
        # to match `_check_narrative_arc_plan` (was soft under-declare).
        "inputs": {
            "hard": [
                {
                    "path": "understanding/content_brief.json",
                    "producer": "content_context",
                },
            ],
            "soft": [
                dep("master/narrative_plan.json", producer=None),
                *deps(
                    "mastering/mastering_plan.json",
                    "understanding/episode_structure.json",
                    "understanding/source_topology.json",
                    "understanding/flow_adaptation.json",
                    "understanding/value_features.json",
                    "segments/manifest.json",
                    "segments/boundaries.json",
                ),
            ]
        },
        "consumers": [
            "mastering_plan_synthesize",
            "mastering_plan_confirm",
            "missing_framing",
            "gap_framing_compose",
            "soundscape_policy_build",
            "chapter_close_hitch",
            "full_master_ranking",
            "air_script_compose",
            "gap_framing_recompose",
            "transitions",
            "sound_design_plan",
            "edl_narrative_audit",
            "edl",
            "music_palette_compose",
            "sfx_prompt_craft",
            "mix",
            "junction_snip_qa",
            "master_finalize",
            "podcast_publish",
        ],
    },
    "chapter_close_hitch": {
        # narrative_plan / manifest / transcript are already hard from _EXTRA_INPUTS.
        "inputs": {
            "soft": [
                dep("mastering/chapter_close_hitch.json", producer=None),
                *deps(
                    "master/selection.json",
                    "mastering/mastering_plan.json",
                    "understanding/content_brief.json",
                    "understanding/episode_structure.json",
                    "understanding/flow_adaptation.json",
                    "understanding/gap_report.json",
                    "understanding/ideal_cuts_materialized.json",
                    "understanding/nugget_corpus.json",
                    "understanding/nugget_layup_plan.json",
                    "understanding/omit_ledger.json",
                    "understanding/source_topology.json",
                    "understanding/speaker_delivery_plan.json",
                    "understanding/speakers.json",
                    "understanding/talking_points.json",
                    "segments/boundaries.json",
                    # `resolve_keeper_air_bounds` measures the source audio
                    # (`chapter_close_hitch.py:1755`).
                    "ingest/normalized.wav",
                ),
            ]
        },
        "outputs": [
            {"path": "mastering/chapter_close_hitch/hitch_keepers.json"},
            {"path": "mastering/chapter_close_hitch/pre_keepers.json"},
            {"path": "mastering/chapter_close_hitch/omit_ledger.json"},
            {"path": "mastering/chapter_close_hitch/vo_snapshot.json"},
            {"path": "master/narrative_plan.qc.json"},
            {"path": "understanding/episode_structure_compact.txt"},
            {"path": "segments/boundaries.json"},
        ],
        # Process host — no OpenAI volley (CCH-B1). StageInfo openai=none.
        "lifecycle_phases": [
            "prestage",
            "pre_call",
            "execute",
            "staged_validate",
            "committed",
            "post_commit_validate",
        ],
    },
    "connector_fuse_pass_pre_ranking": {
        # S5: manifest is hard (admit refuse when missing). Hitch / islands soft.
        "inputs": {
            "hard": [
                dep("segments/manifest.json", producer="segment_classification"),
            ],
            "soft": [
                *deps(
                    "transcript/full.json",
                    "analysis/low_conf_islands.json",
                    "analysis/connector_fuse_audit.json",
                    "mastering/chapter_close_hitch.json",
                ),
            ]
        },
        "consumers": ["full_master_ranking"],
        "remediation": ["volley_retry", "full_stage_rerun"],
    },
    "full_master_ranking": {
        # FMR-B1: hard matches `_check_full_master_ranking` (narrative +
        # manifest + gap). Soft trimmed (S5): no corpus/layup/transcript/transitions/
        # golden_facts sprawl; seed cuts + gap plan + mastering_plan retained.
        "inputs": {
            "hard": deps(
                "master/narrative_plan.json",
                "segments/manifest.json",
                "understanding/gap_report.json",
            ),
            "soft": [
                dep("master/selection.json", producer=None),
                *deps(
                    "analysis/connector_fuse_rounds_pre_ranking.json",
                    "master/coverage_audit.json",
                    "mastering/mastering_plan.json",
                    "mastering/media_ip_cta.json",
                    "segments/boundaries.json",
                    "segments/nle_edits.json",
                    "transcript/review_queue.json",
                    "understanding/content_brief.json",
                    "understanding/delivery_brief.json",
                    "understanding/episode_structure.json",
                    "understanding/flow_adaptation.json",
                    "understanding/gap_framing_plan.json",
                    "understanding/ideal_cuts.json",
                    "understanding/ideal_cuts_selection_seed.json",
                    "understanding/interview_spine.json",
                    "understanding/source_acoustic_profile.json",
                    "understanding/source_topology.json",
                    "understanding/speakers.json",
                    "understanding/talking_points.json",
                    "analysis/high_value_speech_boosts.json",
                    "analysis/high_value_speech_islands.json",
                ),
            ],
        },
        "consumers": list(_SELECTION_CONSUMERS),
        "outputs": [
            {"path": "master/rank_candidates.json"},
            {"path": "master/story_health.json"},
            {"path": "mastering/media_ip_cta.json"},
            {"path": "analysis/stt_lexicon_islands.json"},
        ],
    },
    "selection_order_sanitize": {
        "inputs": {
            "soft": [
                dep("master/selection.json", producer="full_master_ranking"),
                *deps(
                    "mastering/media_ip_cta.json",
                    "segments/manifest.json",
                    "segments/nle_edits.json",
                    "understanding/ideal_cuts.json",
                    "understanding/talking_points.json",
                ),
                dep("understanding/reorder_bridges.json", producer="selection_order_sanitize"),
            ]
        },
        "consumers": [c for c in _SELECTION_CONSUMERS if c != "selection_order_sanitize"],
        "outputs": [
            {"path": "understanding/reorder_bridges.json"},
            {"path": "understanding/speaker_delivery_plan.json"},
            {"path": "master/story_health.json"},
        ],
        "lifecycle_phases": [
            "prestage",
            "pre_call",
            "execute",
            "staged_validate",
            "committed",
            "post_commit_validate",
        ],
    },
    "air_script_compose": {
        # selection + mastering_plan already hard from _EXTRA_INPUTS.
        # ASC-B1: deterministic Pass A host — process tier via _PROCESS_STAGES;
        # no OpenAI volley (StageInfo openai=()).
        "inputs": {
            "soft": deps(
                "analysis/high_value_speech_islands.json",
                "master/narrative_plan.json",
                "segments/manifest.json",
                "segments/nle_edits.json",
                "understanding/gap_evaluations.json",
                "understanding/gap_fill_skip.json",
                "understanding/ideal_cuts.json",
                "understanding/source_acoustic_profile.json",
                "understanding/source_topology.json",
                "understanding/talking_points.json",
            )
        },
        "outputs": [{"path": "understanding/omit_ledger.json"}],
        "remediation": ["full_stage_rerun"],
        "lifecycle_phases": [
            "prestage",
            "pre_call",
            "execute",
            "staged_validate",
            "committed",
            "post_commit_validate",
        ],
    },
    "nugget_corpus_mine": {
        # NCM-B3: mastering_plan soft — body soft-admits (build_corpus_mine_input
        # never refuses on plan); hard PRESTAGE would FULL_AUTO_REGRESSION_RISK.
        # NCM-B1: do not claim nugget_layup_qc — ownership = nugget_layup_compose.
        # selection stays hard via bootstrap _EXTRA_INPUTS.
        "inputs": {
            "soft": [
                dep("understanding/nugget_corpus.json", producer=None),
                # Full tape (ISSUES 191): words between segments and words a
                # cut trims off are offered to the miner; absent file = no spans.
                dep("transcript/full.json", producer="transcribe"),
                dep("mastering/mastering_plan.json", producer="air_script_compose"),
                *deps(
                    "mastering/media_ip_cta.json",
                    "understanding/flow_adaptation.json",
                    "understanding/gap_report.json",
                    "understanding/native_comprehension_masks.json",
                    "understanding/nugget_layup_plan.json",
                    "understanding/source_topology.json",
                    "understanding/speakers.json",
                ),
            ]
        },
    },
    "information_package_plan": {
        # Folds information packages into the mastering plan in place.
        # Deterministic scoring host — no OpenAI volley (IPP-B1). StageInfo openai=().
        # Full-auto defaults: mode=commit_music_vo (mutates mastering_plan) — IPP-B3.
        "inputs": {
            "soft": [
                dep("mastering/mastering_plan.json", producer="mastering_plan_synthesize"),
                *deps("master/selection.json", "segments/manifest.json"),
            ]
        },
        "consumers": ["nugget_layup_compose", "air_script_seams", "edl"],
        "lifecycle_phases": [
            "prestage",
            "pre_call",
            "execute",
            "staged_validate",
            "committed",
            "post_commit_validate",
        ],
    },
    "nugget_layup_compose": {
        # NLC-B3: hard = selection+corpus via bootstrap _EXTRA_INPUTS +
        # _SKIP_LLM_UPSTREAM_HARD (do not hard-gate on IP audit; shadow mode
        # soft-admits). Audit + plan/gap reads stay soft enrichment.
        "inputs": {
            "soft": [
                dep(
                    "mastering/shape/information_packages_audit.json",
                    producer="information_package_plan",
                ),
                dep("understanding/nugget_layup_plan.json", producer=None),
                dep("understanding/gap_report.json", producer="gap_framing_compose"),
                dep("mastering/mastering_plan.json", producer="mastering_plan_synthesize"),
                *deps(
                    "analysis/low_conf_must_keep.json",
                    "mastering/media_ip_cta.json",
                    "transcript/full.json",
                    "understanding/content_brief.json",
                    "understanding/episode_structure.json",
                    "understanding/flow_adaptation.json",
                    "understanding/gap_evaluations.json",
                    "understanding/ideal_cuts.json",
                    "understanding/interview_spine.json",
                    "understanding/native_comprehension_masks.json",
                    "understanding/source_topology.json",
                    "understanding/speakers.json",
                    "understanding/value_features.json",
                ),
            ]
        },
        "outputs": [
            {"path": "mastering/mastering_plan.json"},
            {"path": "understanding/native_comprehension_masks.json"},
            {"path": "understanding/nugget_comprehension_index.json"},
            {"path": "understanding/nugget_layup_qc.json"},
        ],
    },
    "gap_report_sanitize": {
        # GRS-B1: body stubs missing report then sanitize — never hard-gates on
        # nugget_layup_plan (empty-hard seed predecessor was a false claim).
        # GRS-B3: hash-change marker cascade = artifact_sanitize.invalidate
        # `_SANITIZE_CASCADE` for gap_report.json (VO/EDL markers only).
        "inputs": {
            "hard": [],
            "soft": [
                dep("understanding/gap_report.json", producer="gap_framing_compose"),
                *deps("master/selection.json"),
            ]
        },
        "propagation": [
            "vo_line_adjudicate",
            "vo_synthesize",
            "edl_narrative_audit",
            "edl",
            "assembly_preview",
        ],
        "consumers": [
            # GRS-B1: drop upstream nugget_layup_compose (producer, not consumer).
            "gap_framing_recompose",
            "selection_framing_apply",
            "air_script_seams",
            "air_contract_sanitize",
            "transitions",
            "vo_line_adjudicate",
            "vo_synthesize",
            "edl",
            "mix",
            "master_finalize",
        ],
        "lifecycle_phases": [
            "prestage",
            "pre_call",
            "execute",
            "staged_validate",
            "committed",
            "post_commit_validate",
        ],
    },
    "refinement_agenda": {
        # RA-B1: body never reads gap_report — empty-hard seed predecessor was a
        # false PRESTAGE claim. Keep gap soft for seed adjacency honesty only.
        "inputs": {
            "hard": [],
            "soft": [
                dep("understanding/gap_report.json", producer="gap_framing_compose"),
                *deps(
                    "master/coverage_audit.json",
                    "mastering/mastering_plan.json",
                    "understanding/gap_evaluations.json",
                    "understanding/source_acoustic_profile.json",
                    "understanding/source_topology.json",
                ),
            ]
        },
        "consumers": [
            "gap_framing_recompose",
            "selection_framing_apply",
        ],
    },
    "gap_framing_recompose": {
        # GFR-B1: Full-auto defaults = layup-authority thin adapter (no OpenAI).
        # Agenda is soft — seat-freeze / authority paths skip without it.
        # Empty-hard seed predecessor + SEED_ORDER_PROMOTE were false PRESTAGE claims.
        "inputs": {
            "hard": [],
            "soft": [
                dep("understanding/gap_report.json", producer="gap_framing_compose"),
                dep("understanding/gap_framing_plan.json", producer="gap_framing_compose"),
                dep("understanding/nugget_layup_plan.json", producer="nugget_layup_compose"),
                dep(
                    "understanding/refinement_agenda.json",
                    producer="refinement_agenda",
                ),
                *deps(
                    "master/coverage_audit.json",
                    "master/narrative_plan.json",
                    "master/selection.json",
                    "mastering/mastering_plan.json",
                    "segments/manifest.json",
                    "understanding/comprehension_risks.json",
                    "understanding/content_brief.json",
                    "understanding/episode_structure.json",
                    "understanding/gap_evaluations.json",
                    "understanding/nugget_corpus.json",
                ),
            ]
        },
        # Ownership-honest secondaries (skip stub + shared layup plan adopt).
        # Do not claim compose-owned plan/draft/audit or ops cold-open/trajectory.
        "outputs": [
            {"path": "understanding/nugget_layup_plan.json"},
            {"path": "understanding/refinement_skip_copy.json"},
        ],
        "consumers": [
            "selection_framing_apply",
            "air_script_seams",
            "air_contract_sanitize",
            "transitions",
            "vo_line_adjudicate",
            "vo_synthesize",
            "edl",
            "mix",
            "master_finalize",
        ],
        "lifecycle_phases": [
            "prestage",
            "pre_call",
            "execute",
            "staged_validate",
            "committed",
            "post_commit_validate",
        ],
        "remediation": ["full_stage_rerun"],
    },
    "selection_framing_apply": {
        # SFA-B1: deterministic framing apply — no OpenAI. Body writes APPLY
        # sidecar + optional gap_report rebase; never draft/plan/skip_copy.
        "inputs": {
            "hard": [
                dep("master/selection.json", producer="full_master_ranking"),
                dep("understanding/gap_report.json", producer="gap_framing_compose"),
            ],
            "soft": [
                *deps(
                    "master/coverage_audit.json",
                    "mastering/mastering_plan.json",
                    "segments/manifest.json",
                    "understanding/content_brief.json",
                    "understanding/episode_structure.json",
                    "understanding/flow_adaptation.json",
                    "understanding/gap_framing_plan.json",
                    "understanding/nugget_corpus.json",
                    "understanding/nugget_layup_plan.json",
                    "understanding/refinement_agenda.json",
                    "understanding/source_topology.json",
                    "understanding/speaker_delivery_plan.json",
                    "understanding/speakers.json",
                ),
            ]
        },
        "outputs": [
            {"path": "understanding/gap_report.json"},
        ],
        "lifecycle_phases": [
            "prestage",
            "pre_call",
            "execute",
            "staged_validate",
            "committed",
            "post_commit_validate",
        ],
        "remediation": ["full_stage_rerun"],
    },
    "air_script_seams": {
        # mastering_plan / gap_report / nugget_layup_plan already hard.
        # ASS-B1: deterministic Pass B host — process tier via _PROCESS_STAGES;
        # no OpenAI volley (StageInfo openai=()). gap_framing_plan write is IN_CODE
        # (compose_pass_b → build_gap_framing_plan).
        "inputs": {
            "soft": deps(
                "analysis/high_value_speech_islands.json",
                "master/selection.json",
                "segments/manifest.json",
                "segments/nle_edits.json",
                "transcript/full.json",
                "understanding/episode_structure.json",
                "understanding/gap_evaluations.json",
                "understanding/gap_fill_skip.json",
                "understanding/source_acoustic_profile.json",
                "understanding/source_topology.json",
            )
        },
        "outputs": [{"path": "understanding/gap_framing_plan.json"}],
        "remediation": ["full_stage_rerun"],
        "lifecycle_phases": [
            "prestage",
            "pre_call",
            "execute",
            "staged_validate",
            "committed",
            "post_commit_validate",
        ],
    },
    "air_contract_sanitize": {
        # ACS-B2: deterministic commit/heal/soft-freeze host — no OpenAI volley.
        # Drop llm_execute lifecycle claim noise (CODE_DOC_CONFLICT → IN_CODE).
        "inputs": {
            "soft": [
                dep("mastering/mastering_plan.json", producer="air_script_seams"),
                dep("understanding/omit_ledger.json", producer="air_script_compose"),
                dep("understanding/gap_report.json", producer="gap_framing_compose"),
                *deps(
                    "master/selection.json",
                    "segments/manifest.json",
                    "understanding/gap_fill_skip.json",
                    "understanding/source_topology.json",
                ),
            ]
        },
        "consumers": [
            "transitions",
            "vo_synthesize",
            "edl",
            "mix",
            "junction_snip_qa",
            "master_finalize",
        ],
        "outputs": [{"path": "understanding/omit_ledger.json"}],
        "lifecycle_phases": [
            "prestage",
            "pre_call",
            "execute",
            "staged_validate",
            "committed",
            "post_commit_validate",
        ],
        "remediation": ["full_stage_rerun"],
    },
    "transitions": {
        # Transitions: hard = selection + content_brief (and
        # gap_report) match `build_input` unconditional `read_json` + preflight.
        # Layup stays LLM_UPSTREAM hard; mastering_plan stays SEED_ORDER promote.
        "inputs": {
            "hard": [
                *deps("master/selection.json"),
                {
                    "path": "understanding/content_brief.json",
                    "producer": "content_context",
                },
                {
                    "path": "understanding/gap_report.json",
                    "producer": "gap_framing_compose",
                },
            ],
            "soft": [
                dep("master/transitions.json", producer=None),
                dep("understanding/synthetic_framing_plan.json", producer=None),
                dep("understanding/reorder_bridges.json", producer="full_master_ranking"),
                dep(
                    "understanding/speaker_delivery_plan.json",
                    producer="full_master_ranking",
                ),
                *deps(
                    "master/coverage_audit.json",
                    "master/narrative_plan.json",
                    "master/transitions_pair_freeze.json",
                    "mastering/mastering_plan.json",
                    "mastering/media_ip_cta.json",
                    "segments/manifest.json",
                    "segments/boundaries.json",
                    "segments/nle_edits.json",
                    "understanding/delivery_brief.json",
                    "understanding/episode_structure.json",
                    "understanding/flow_adaptation.json",
                    "understanding/ideal_cuts.json",
                    "understanding/interview_spine.json",
                    "understanding/nugget_corpus.json",
                    "understanding/nugget_layup_plan.json",
                    "understanding/source_topology.json",
                    "understanding/speakers.json",
                    "understanding/talking_points.json",
                ),
            ],
        },
        # Transitions: pin ADG `_PROPAGATION_SEEDS` + contract.
        "propagation": [
            "sound_design_plan",
            "vo_line_adjudicate",
            "vo_synthesize",
            "edl",
        ],
        "consumers": [
            "full_master_ranking",
            "sound_design_plan",
            "vo_synthesize",
            "edl",
            "mix",
            "junction_snip_qa",
            "master_finalize",
            "master_transcript_build",
            "podcast_publish",
        ],
        "outputs": [
            {"path": "master/deferred_transition_pairs.json"},
            {"path": "master/transitions_pair_freeze.json"},
            {"path": "understanding/reorder_bridges.json"},
            {"path": "master/bridge_completeness.json"},
        ],
    },
}

# ---------------------------------------------------------------------------
# sound
# ---------------------------------------------------------------------------
# Two stages, and the recorded run (`MUX_CONTRACT_RECORD=1`) covers the big one:
# `sound_design_plan` showed 13 undeclared reads, every one of which is visible
# in `run_sound_design_plan`'s `build_input`.

_SOUND: dict[str, dict[str, Any]] = {
    "sound_design_plan": {
        # `build_input` reads the whole delivery picture, but each read is either
        # `read_json` (empty dict when absent) or a `load_*` helper that returns
        # falsey and is then skipped by an `if`. Nothing here gates the stage, so
        # nothing here is hard. `master/transitions.json` stays hard because the
        # registry already declared it and the stage is seeded after `transitions`.
        #
        # `order_reconcile` and the `refresh_*` helpers run first and pull in the
        # EDL, the mastering plan and the structure/policy artifacts — all three
        # wrapped in try/except with an explicit fail-open log.
        "inputs": {
            "soft": [
                *deps(
                    "segments/manifest.json",
                    "master/selection.json",
                    "master/narrative_plan.json",
                    "understanding/gap_report.json",
                    "understanding/content_brief.json",
                    "understanding/content_brief_reanchor.json",
                    "understanding/delivery_brief.json",
                    "understanding/episode_structure.json",
                    "understanding/sonic_context.json",
                    "understanding/soundscape_policy.json",
                    "understanding/source_acoustic_profile.json",
                    "understanding/speakers.json",
                    "understanding/analysis_state.json",
                    "mastering/mastering_plan.json",
                ),
                *back("master/edl.json"),
            ],
        },
        # `persist` calls `_load_sound_design_plan(c)` and merges into it, so the
        # plan is a read-modify-write — declared as an output already, which
        # `evaluate` counts as readable.
        "outputs": [
            {"path": "understanding/music_brief.json"},
            {"path": "understanding/episode_structure_compact.txt"},
        ],
        # Seed order: sound_design_palettes is analysis *before* this stage and
        # seeds the shared path; it is not a first-pass consumer of a delivery
        # SDP write (SDP-B2 — CODE_DOC_CONFLICT vs DELIVERY_ORDER).
        "consumers": ["sfx_prompt_craft", "mmaudio_sfx", "music_palette_compose"],
    },
    "vo_line_adjudicate": {
        # Skip-stub off-paths still run PRESTAGE. Layup + gap_report exist by this
        # seed position; refusing without them is louder than a hollow skip-done.
        "inputs": {
            "hard": [
                dep("understanding/nugget_layup_plan.json", producer="nugget_layup_compose"),
                dep("understanding/gap_report.json", producer="gap_framing_compose"),
            ],
            "soft": deps(
                "understanding/nugget_corpus.json",
                "understanding/omit_ledger.json",
                "understanding/native_comprehension_masks.json",
                "understanding/content_brief.json",
            ),
        },
        # S1–S3: primary is adjudication.json only (advisory). Omit stamps may
        # rewrite gap_report under stamp ALLOW; allocation / intro peeled.
        "outputs": [],
        "consumers": ["vo_synthesize"],
    },
}

# ---------------------------------------------------------------------------
# build
# ---------------------------------------------------------------------------
# Four of these are AUDIO_MUTATING (`edl`, `mix`, `junction_snip_qa`, and
# `master_finalize` in `ship`), so the hard-input bar is at its strictest here:
# a wrong hard input does not degrade a mix, it refuses one.
#
# `FALLBACK_HARD_INPUTS` in `dispatch_delta` is *not* a safe source of hard
# inputs. It feeds the no-delta guard, which merely tolerates a missing path,
# whereas `run_phase_checks` raises on one. It lists `master/assembly.wav` for
# `junction_snip_qa` — see the note on that stage for why declaring that as a
# contract hard input would re-create an exec_11871 halt.

_BUILD: dict[str, dict[str, Any]] = {
    "vo_synthesize": {
        # Renders seated lines; a missing gap report is a hollow synth, not a skip.
        # Soft: seats / omit ledger / speech QA (S7 docs soft seats/audit/ledger).
        "inputs": {
            "hard": [
                dep("understanding/gap_report.json", producer="gap_framing_compose"),
                dep("master/transitions.json", producer="transitions"),
            ],
            "soft": [
                dep("mastering/mastering_plan.json", producer="mastering_plan_synthesize"),
                dep("understanding/omit_ledger.json", producer="air_contract_sanitize"),
                dep("mastering/vo_speech_qa.json", producer=None),
            ],
        },
        "consumers": ["sound_design_vo_finalize", "edl_narrative_audit", "edl"],
        "lifecycle_phases": [
            "prestage",
            "pre_call",
            "execute",
            "staged_validate",
            "committed",
            "post_commit_validate",
        ],
    },
    "sound_design_vo_finalize": {
        # Measures seated WAVs after synth; skip-done without the synth artifact
        # is the hollow path Finding 4 exists to close.
        # Body also refuses without SDP (`no_sound_design_plan`) — hard so PRESTAGE
        # matches that honesty (SDVF-B2; was vo_synthesize-only CODE_DOC_CONFLICT).
        # SDVF-B3 (8B): invalidate downstream only — no reverse VO adjudicate/synth edges.
        "inputs": {
            "hard": [
                dep("mastering/vo_synthesize.json", producer="vo_synthesize"),
                dep("understanding/sound_design_plan.json", producer="sound_design_plan"),
            ],
        },
        "propagation": ["edl_narrative_audit", "edl"],
    },
    "edl_narrative_audit": {
        # ENA-B2: hard matches `build_input` hard-reads + `_preflight_edl_narrative_audit`
        # (was SDP-only via LLM_UPSTREAM — CODE_DOC_CONFLICT). SDP/transitions/gap
        # are `_optional_json` → soft. Consumers = edl (was self via ARTIFACTS_REGISTRY).
        # Bootstrap `_SKIP_LLM_UPSTREAM_HARD` so SDP is not re-injected as hard.
        # ENA-B3 (8B): drop reverse invalidate of vo_synthesize (thrash).
        "inputs": {
            "hard": [
                {"path": "understanding/content_brief.json", "producer": "content_context"},
                {"path": "master/coverage_audit.json", "producer": "topic_coverage_audit"},
                {"path": "master/narrative_plan.json", "producer": "narrative_arc_plan"},
                {"path": "master/selection.json", "producer": "full_master_ranking"},
            ],
            "soft": [
                dep("understanding/sound_design_plan.json", producer="sound_design_plan"),
                *deps(
                    "master/transitions.json",
                    "understanding/gap_report.json",
                    "segments/nle_edits.json",
                    "mastering/mastering_plan.json",
                ),
            ],
        },
        "consumers": ["edl"],
        "propagation": ["edl"],
    },
    "edl": {
        # REFUSES: `check_narrative_qc(ctx, stage="edl", require_selection=True)`
        # raises SystemExit for a missing content brief, coverage audit or
        # selection, and `narrative_qc.strict` is true in the shipped config.
        # `validate_flow1_narrative` returns early on the brief, so the order of
        # these three is the order the gate reports them in.
        # EDL-B2: soft SDP producer is delivery `sound_design_plan` (not analysis
        # `sound_design_palettes` first-claimer) — matches heal authority + ENA.
        "inputs": {
            "hard": [
                {"path": "understanding/content_brief.json", "producer": "content_context"},
                {"path": "master/coverage_audit.json", "producer": "topic_coverage_audit"},
                {"path": "master/selection.json", "producer": "full_master_ranking"},
            ],
            "soft": [
                # `run_edl` substitutes `{"transitions": []}` when the file is
                # absent, so the EDL is built with no seam grammar at all rather
                # than refusing — soft for dispatch, required for correctness.
                dep("master/transitions.json", correctness=True),
                *deps(
                    "master/narrative_plan.json",
                    "master/edl_narrative_audit.json",
                    "segments/manifest.json",
                    "understanding/gap_report.json",
                    "understanding/omit_ledger.json",
                    "understanding/nugget_layup_plan.json",
                    "mastering/mastering_plan.json",
                    "mastering/media_ip_cta.json",
                ),
                dep(
                    "understanding/sound_design_plan.json",
                    producer="sound_design_plan",
                ),
            ],
        },
        "consumers": [
            "assembly_preview",
            "mix",
            "junction_snip_qa",
            "master_finalize",
            "master_transcript_build",
            "podcast_publish",
        ],
        "lifecycle_phases": [
            "prestage",
            "pre_call",
            "execute",
            "staged_validate",
            "committed",
            "post_commit_validate",
        ],
    },
    "assembly_preview": {
        # Preview without an EDL is an empty WAV, not a refusal in the body —
        # PRESTAGE records the missing hard input instead of shipping a silent
        # blank preview. Seeded after `edl`.
        # AP-B2: selection is unused by run_preview (clips come from edl) —
        # keep soft so PRESTAGE does not refuse on ranking alone.
        "inputs": {
            "hard": [
                dep("master/edl.json", producer="edl"),
            ],
            "soft": [
                dep("master/selection.json", producer="full_master_ranking"),
                *deps(
                    "ingest/normalized.wav",
                ),
            ],
        },
        "consumers": ["junction_snip_qa", "mix"],
    },
    "junction_snip_qa": {
        # `FALLBACK_HARD_INPUTS` lists `master/assembly.wav` here, but
        # `junction_recut_precedes_mix` exists precisely because this stage must
        # be able to run *before* the first mix: "The ladder needs only
        # master/edl.json — the remaster it drives is what mints assembly.wav."
        # Declaring the assembly hard would refuse the stage at PRESTAGE in exactly
        # the pre-mix recut posture it was added to serve, re-creating the
        # exec_11871 mix <-> junction_snip_qa ping-pong as a hard stop.
        #
        # The EDL is now hard: without it the stage cannot adjudicate air order.
        # JSQ-B4: selection stays soft+correctness (matches `_check`; assert_consumer
        # returns early when selection is absent — never PRESTAGE-hard).
        "inputs": {
            "hard": [
                dep("master/edl.json", producer="edl"),
            ],
            "soft": [
                # Ownership makes `junction_snip_qa` authoritative for the air
                # order, the assembly ledger and the render ledger: it reads each
                # one and promotes it back, so they are read-modify-write.
                *rmw(
                    "master/assembly_ledger.json",
                    "master/air_order.json",
                    "master/render_ledger.json",
                ),
                dep("master/selection.json", correctness=True),
                *deps(
                "master/assembly.wav",
                "segments/manifest.json",
                "understanding/gap_report.json",
                "understanding/sonic_context.json",
                "understanding/source_acoustic_profile.json",
                "understanding/sound_design_plan.json",
                "mastering/media_ip_cta.json",
                ),
            ],
        },
        "consumers": ["mix", "master_finalize"],
    },
    "mix": {
        # MIX-B1: hard matches `_check_mix` — tape + selection + edl + SDP.
        # Body refuses without those four (`StageInputIssue`). PRESTAGE is
        # lenient (`missing_hard_input` refuse, not crash), so hard EDL no longer
        # hard-stops the junction_recut_precedes_mix seed flip (MIX-B2): live
        # incomplete cuts sit on an existing EDL; mix then refuses incomplete_cut
        # and the agenda seeds junction first.
        #
        # MIX-B5: deterministic placement/critic host — no OpenAI volley.
        "lifecycle_phases": [
            "prestage",
            "pre_call",
            "execute",
            "staged_validate",
            "committed",
            "post_commit_validate",
        ],
        "inputs": {
            "hard": [
                {"path": "ingest/normalized.wav", "producer": "ingest"},
                {"path": "master/selection.json", "producer": "full_master_ranking"},
                {"path": "master/edl.json", "producer": "edl"},
                {
                    "path": "understanding/sound_design_plan.json",
                    "producer": "sound_design_plan",
                },
            ],
            "soft": [
                *deps(
                    "master/transitions.json",
                    "master/junction_snip_qa.json",
                    "master/seam_autopsy.json",
                    "understanding/omit_ledger.json",
                    "understanding/gap_report.json",
                    "sound_design/mmaudio_qa.json",
                ),
            ],
        },
        "consumers": ["junction_snip_qa", "master_finalize", "listen_delight_audit"],
    },
    "listen_delight_audit": {
        # Seeded *before* mix, so assembly.wav cannot be hard (permanent PRESTAGE
        # refusal on the first walk). EDL + selection exist by this seed position.
        "inputs": {
            "hard": [
                dep("master/edl.json", producer="edl"),
                dep("master/selection.json", producer="full_master_ranking"),
            ],
            "soft": deps(
                "master/assembly.wav",
                "understanding/sound_design_plan.json",
                "understanding/delivery_brief.json",
            ),
        },
        "consumers": ["master_finalize"],
        "lifecycle_phases": [
            "prestage",
            "pre_call",
            "execute",
            "staged_validate",
            "committed",
            "post_commit_validate",
        ],
    },
    "music_palette_compose": {
        # Owns cue placement on the SDP after the EDL exists (the `sound_design_plan`
        # note says so explicitly), so the plan is a read-modify-write: already a
        # declared output, and declared as an input here too. Hard: a palette
        # composed against an absent plan is a hollow cue list.
        # MPC-B2: producer is delivery `sound_design_plan` (not analysis palettes
        # scaffold); body refuses without SDP before LLM.
        "inputs": {
            "hard": [
                dep(
                    "understanding/sound_design_plan.json",
                    producer="sound_design_plan",
                ),
            ],
            "soft": deps(
                "understanding/music_brief.json",
                "master/edl.json",
                "master/selection.json",
                "understanding/episode_structure.json",
            ),
        },
        # MPC-B3: sfx_prompt_craft soft-reads the SDP cue seat after compose.
        "consumers": ["sfx_prompt_craft", "mmaudio_sfx", "mix"],
    },
    "sfx_prompt_craft": {
        # SPC-B1: consumers = mmaudio_sfx (was self via ARTIFACTS_REGISTRY).
        "consumers": ["mmaudio_sfx"],
    },
    "mmaudio_sfx": {
        # MusicGen-first local gen host — no OpenAI volley (MSFX-B1).
        # Drop llm_execute lifecycle claim noise (CODE_DOC_CONFLICT → IN_CODE).
        "lifecycle_phases": [
            "prestage",
            "pre_call",
            "execute",
            "staged_validate",
            "committed",
            "post_commit_validate",
        ],
    },
}

# ---------------------------------------------------------------------------
# ship
# ---------------------------------------------------------------------------

_SHIP: dict[str, dict[str, Any]] = {
    "master_finalize": {
        # REFUSES: `master_wav(ctx, "master/assembly.wav", "master/master.wav")`
        # opens the assembly and raises `FileNotFoundError(assembly)` when it is
        # not a file. There is no branch of this stage that produces a master
        # without one, and unlike `junction_snip_qa` there is no pre-mix posture —
        # `master_finalize` is seeded at DELIVERY_ORDER 30, after `mix` at 28.
        #
        # This is the declaration `ship_reachability` needs: the master is
        # reachable exactly when the assembly is.
        #
        # MF-B4: process host (loudnorm + PMQ/delight nested) — drop llm_execute
        # lifecycle claim noise (CODE_DOC_CONFLICT → IN_CODE).
        "lifecycle_phases": [
            "prestage",
            "pre_call",
            "execute",
            "staged_validate",
            "committed",
            "post_commit_validate",
        ],
        "inputs": {
            "hard": [
                {"path": "master/assembly.wav", "producer": "mix"},
                {"path": "master/selection.json", "producer": "full_master_ranking"},
            ],
            "soft": [
                # Junction is the heal owner; finalize only rereads/restamps.
                dep("master/seam_autopsy.json", producer="junction_snip_qa"),
                # Not correctness-marked: `render_ledger_exists` sits in
                # `RUBRIC_PMQ_CHECKS`, i.e. advisory under the default
                # aspirational policy, so by the project's own contract a
                # missing render ledger does not block publish.
                {"path": "master/render_ledger.json", "producer": "junction_snip_qa"},
                dep("master/edl.json", correctness=True),
                *deps(
                    "master/junction_snip_qa.json",
                    "master/transitions.json",
                    "understanding/omit_ledger.json",
                    "understanding/gap_report.json",
                    "understanding/sound_design_plan.json",
                    "mastering/mastering_plan.json",
                    "mastering/listen_delight_audit.json",
                    "mastering/chapter_close_hitch.json",
                ),
            ],
        },
        "consumers": ["master_transcript_build", "podcast_encode_mp3", "podcast_publish"],
    },
    "podcast_encode_mp3": {
        # REFUSES, and says so in the message it raises:
        # `raise FileNotFoundError("master/master.wav missing — run master_finalize first")`.
        "inputs": {
            "hard": [
                {"path": "master/master.wav", "producer": "master_finalize"},
            ],
        },
        "consumers": ["podcast_publish"],
    },
    "master_transcript_build": {
        # EMB-B3: meta does not read transcript pack — invalidate podcast_publish only.
        "propagation": ["podcast_publish"],
    },
    "episode_meta_build": {
        # Title/description from the locked selection; skip-done without it is a
        # hollow publish packet. EMB-B2: body refuses without selection.
        "inputs": {
            "hard": [
                {"path": "master/selection.json", "producer": "full_master_ranking"},
            ],
            "soft": deps(
                "understanding/content_brief.json",
                "master/narrative_plan.json",
            ),
        },
        "consumers": ["episode_cover_prompt_craft", "episode_cover_generate", "podcast_publish"],
    },
    "episode_cover_prompt_craft": {
        # ECPC-B2: body soft-harvests motifs (harvest_motif_context) — never
        # refuses without episode_meta. LLM_UPSTREAM hard meta was a false
        # PRESTAGE claim (CODE_DOC_CONFLICT → IN_CODE).
        "inputs": {
            "hard": [],
            "soft": [
                {"path": "publish/episode_meta.json", "producer": "episode_meta_build"},
                *deps(
                    "understanding/content_brief.json",
                    "master/selection.json",
                ),
            ],
        },
        "consumers": ["episode_cover_generate"],
    },
    "episode_cover_generate": {
        # ECG-B2: OpenAI Images+Vision host — tier via ALL_LLM_STAGES → llm_full
        # (bootstrap); keep llm_execute lifecycle (default). Hard cover_prompt
        # file from craft; rejected/empty content soft-harvests in body.
        "inputs": {
            "hard": [
                {"path": "publish/cover_prompt.json", "producer": "episode_cover_prompt_craft"},
            ],
            "soft": deps(
                "publish/episode_meta.json",
                "understanding/content_brief.json",
                "understanding/speakers.json",
                "master/selection.json",
            ),
        },
        "consumers": ["podcast_publish"],
    },
    "podcast_publish": {
        # Clinic B3: body finalizes only when mp3+cover+VTT exist (FileNotFound
        # after materialize / min-size). Cover/VTT also SEED_ORDER-upstream
        # (encode / cover generate / transcript). Skip path does not walk
        # PRESTAGE hard checks the same way — writes ready:false skipped (B1).
        # Clinic B5: drop unused soft (transcript.json, segments, gap, speakers).
        "inputs": {
            "hard": [
                {"path": "master/master.wav", "producer": "master_finalize"},
                {"path": "publish/audio.mp3", "producer": "podcast_encode_mp3"},
                {"path": "publish/cover.jpg", "producer": "episode_cover_generate"},
                {"path": "master/transcript.vtt", "producer": "master_transcript_build"},
            ],
            "soft": deps(
                "master/edl.json",
                "master/selection.json",
                "master/narrative_plan.json",
                "publish/episode_meta.json",
                "publish/cover_meta.json",
            ),
        },
        "consumers": [],
        "lifecycle_phases": [
            "prestage",
            "pre_call",
            "execute",
            "staged_validate",
            "committed",
            "post_commit_validate",
        ],
    },
}

GROUP_DEPS: dict[str, dict[str, dict[str, Any]]] = {
    "prepare": _PREPARE,
    "understand-a": _UNDERSTAND_A,
    "understand-b": _UNDERSTAND_B,
    "understand-c": _UNDERSTAND_C,
    "fill_gaps": _FILL_GAPS,
    "plan_rank": _PLAN_RANK,
    "sound": _SOUND,
    "build": _BUILD,
    "ship": _SHIP,
}


# ---------------------------------------------------------------------------
# SEED_ORDER promotions (applied at import — keeps body tables readable)
# ---------------------------------------------------------------------------
# Consecutive soft-underdeclare pairs to promote (producer → consumer → path).
# Allowlisted optionals (preclean→ingest, ideal_cuts gated, mmaudio→mix,
# mix→junction assembly) are omitted on purpose.
_SEED_ORDER_PROMOTE: tuple[tuple[str, str, str], ...] = (
    ("speaker_roles", "source_topology_build", "understanding/speakers.json"),
    # ideal_cuts_materialize→boundary_detection: materialize soft (BD-B1) —
    # LLM path optional; skip-when-bound still gates on published boundaries.
    # connector_fuse_pass: low_conf_islands stay soft — fuse skip-completes without
    # them (CFP-B1); body gates on manifest/enabled.
    ("sonic_context_build", "sound_design_palettes", "understanding/sonic_context.json"),
    ("mastering_shape_agenda", "mastering_shape_candidates", "mastering/shape/agenda.json"),
    ("mastering_shape_candidates", "mastering_plan_synthesize", "mastering/shape/candidates.json"),
    ("mastering_plan_synthesize", "missing_framing", "mastering/mastering_plan.json"),
    ("missing_framing", "mastering_plan_confirm", "understanding/gap_evaluations.json"),
    ("mastering_plan_confirm", "gap_framing_compose", "mastering/mastering_plan.json"),
    ("delivery_brief_build", "soundscape_policy_build", "understanding/delivery_brief.json"),
    # chapter_close_hitch→connector_fuse_pass_pre_ranking hitch stays soft —
    # fuse skip-completes without hitch latch (pre_ranking CFP-B1); body gates
    # on manifest/enabled (match connector_fuse_pass).
    ("full_master_ranking", "selection_order_sanitize", "master/selection.json"),
    # refinement_agenda→gap_framing_recompose agenda stays soft (GFR-B1) —
    # authority / seat-freeze paths complete without agenda.
    ("air_script_seams", "air_contract_sanitize", "mastering/mastering_plan.json"),
    ("air_contract_sanitize", "transitions", "mastering/mastering_plan.json"),
    ("edl_narrative_audit", "edl", "master/edl_narrative_audit.json"),
    ("junction_snip_qa", "master_finalize", "master/junction_snip_qa.json"),
)

# Mid-pipeline empty-hard stages: hard-require the previous seed stage primary.
# framing_posture_decide / ideal_cuts_materialize stay allowlisted empty.
_EMPTY_HARD_SEED_PREDECESSOR: tuple[str, ...] = (
    # source_topology_build: hard speakers+transcript set explicitly (STB-B4).
    # vernacular_segment_sanitize intentionally hard:[] — skip-complete without
    # boundaries (VSS-B1); allowlisted in audit_seed_contract_alignment.
    "low_conf_island_scan",
    # connector_fuse_pass intentionally hard:[] — skip-complete without islands
    # (CFP-B1); allowlisted.
    # connector_fuse_pass_pre_ranking intentionally hard:[] — skip-complete
    # without hitch (pre_ranking CFP-B1); allowlisted.
    # sonic_context_build: hard set explicitly to brief+manifest+acoustic
    # (SCB-B1); do not empty-hard-promote prior-seed fuse audit.
    # mastering_research_routing/waves/rollup intentionally hard:[] — probes
    # and stub routing never require prior-seed SDP/routing/waves (CSP-02 /
    # MRW-B1); allowlisted in audit_seed_contract_alignment.
    "mastering_shape_agenda",
    "mastering_shape_candidates",
    "mastering_plan_synthesize",
    "mastering_plan_confirm",
    "soundscape_policy_build",
    "episode_structure_compose",
    "selection_order_sanitize",
    # gap_report_sanitize intentionally hard:[] — stubs missing report then
    # sanitize; never require layup_plan (GRS-B1); allowlisted.
    # refinement_agenda intentionally hard:[] — body ignores gap_report (RA-B1);
    # allowlisted + consecutive soft vs gap_report_sanitize.
    # gap_framing_recompose intentionally hard:[] — authority/freeze skip without
    # agenda (GFR-B1); allowlisted + consecutive soft vs refinement_agenda.
    # episode_cover_prompt_craft intentionally hard:[] — soft-harvest meta
    # (ECPC-B2); allowlisted + consecutive soft vs episode_meta_build.
    "air_contract_sanitize",
)

# Air-order correctness softs that must become hard.
# mix←edl promoted to hard under MIX-B1 (`_check_mix`); not listed here.
# JSQ-B4: junction←selection stays soft+correctness — `_check_junction_snip_qa`
# never requires selection (assert_consumer returns early when absent).
_CORRECTNESS_TO_HARD: tuple[tuple[str, str], ...] = (
    ("edl", "master/transitions.json"),
    ("master_finalize", "master/edl.json"),
)


def _stage_inputs(stage_id: str) -> dict[str, Any] | None:
    for group in GROUP_DEPS.values():
        if stage_id in group:
            inputs = group[stage_id].setdefault("inputs", {})
            inputs.setdefault("hard", [])
            inputs.setdefault("soft", [])
            return inputs
    return None


def _move_soft_to_hard(inputs: dict[str, Any], path: str, *, producer: str | None = None) -> None:
    hard = list(inputs.get("hard") or [])
    soft = list(inputs.get("soft") or [])
    if any(isinstance(d, dict) and d.get("path") == path for d in hard):
        inputs["soft"] = [d for d in soft if not (isinstance(d, dict) and d.get("path") == path)]
        return
    moved = None
    kept_soft: list[Any] = []
    for d in soft:
        if isinstance(d, dict) and d.get("path") == path:
            moved = dict(d)
            moved.pop("correctness", None)
            if producer and not moved.get("producer"):
                moved["producer"] = producer
            continue
        kept_soft.append(d)
    if moved is None:
        moved = {"path": path}
        if producer:
            moved["producer"] = producer
    hard.append(moved)
    inputs["hard"] = hard
    inputs["soft"] = kept_soft


def _apply_seed_order_hard_promotions() -> None:
    """Promote SEED_ORDER / air-order edges so authority cannot leapfrog seed law."""
    try:
        from interview_mux.artifact_ownership import primary_path_for_stage
        from interview_mux.v2.config import ANALYSIS_ORDER, DELIVERY_ORDER
    except Exception:
        return

    order = list(ANALYSIS_ORDER) + list(DELIVERY_ORDER)
    index = {sid: i for i, sid in enumerate(order)}

    for producer, consumer, path in _SEED_ORDER_PROMOTE:
        inputs = _stage_inputs(consumer)
        if inputs is None:
            continue
        _move_soft_to_hard(inputs, path, producer=producer)

    for consumer in _EMPTY_HARD_SEED_PREDECESSOR:
        inputs = _stage_inputs(consumer)
        if inputs is None:
            continue
        if any(isinstance(d, dict) and d.get("path") for d in (inputs.get("hard") or [])):
            continue
        i = index.get(consumer)
        if i is None or i < 1:
            continue
        producer = order[i - 1]
        primary = primary_path_for_stage(producer)
        if not primary:
            continue
        _move_soft_to_hard(inputs, primary, producer=producer)

    for consumer, path in _CORRECTNESS_TO_HARD:
        inputs = _stage_inputs(consumer)
        if inputs is None:
            continue
        _move_soft_to_hard(inputs, path)


_apply_seed_order_hard_promotions()


def deps_for(stage_id: str) -> dict[str, Any]:
    for group in GROUP_DEPS.values():
        if stage_id in group:
            return group[stage_id]
    return {}


def populated_stage_ids() -> list[str]:
    return sorted({sid for group in GROUP_DEPS.values() for sid in group})


def populated_groups() -> list[str]:
    return sorted(GROUP_DEPS)


__all__ = ["GROUP_DEPS", "deps_for", "populated_groups", "populated_stage_ids"]

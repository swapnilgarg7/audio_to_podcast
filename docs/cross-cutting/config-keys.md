# Config keys — `app.defaults.json` and overrides

**Runtime versions:** Python packages and CLI tools — [anchored-toolchain.md](./anchored-toolchain.md). OpenAI model IDs — [model-routing.md](./model-routing.md).

Authoritative defaults live in **`config/app.defaults.json`**. At runtime, `interview_mux.config.merged_config()` merges **`config/secrets/secrets.env`** (never commit secrets). This doc lists **meaningful keys**, what uses them, and **what breaks if wrong**.

**Per-machine overrides — `config/app.local.json` (gitignored).** `load_defaults()` deep-merges this file over `app.defaults.json` before secrets are applied, so only the keys that differ need to appear. `app.defaults.json` is committed and shared across machines, so anything host-specific — where the heavy runtime venvs and model weights live, which model tier the local GPU can hold — belongs here instead. See [windows-cuda-setup.md](./windows-cuda-setup.md) for a worked example.

Resolution order (highest wins): CLI flags → `secrets.env` → `app.local.json` → `app.defaults.json`.

---

## Launcher environment (not in `app.defaults.json`)

| Variable | Default | Used by | If wrong |
|----------|---------|---------|----------|
| `MUX_PRESERVE_SESSION` | `0` | `./scripts/run.sh` | `1` keeps `ASSETS/.gui/application_state.json` (and legacy session files) across this launch; default clears session for a fresh Start tab |
| `MUX_SKIP_ASSETS_CLEANUP` | `0` | `./scripts/run.sh` | `1` skips `assets_ephemeral_cleanup` (session files, `.gui/sessions/*`, stale locks inside exec_*; never deletes execution dirs) |
| `MUX_MIRROR_OPERATOR_ERRORS` | `1` | `run.sh`, pipeline stderr mirror | `0` hides terminal mirror of operator errors |
| `MUX_RUN_MODE` | interactive / `manual` | `./scripts/run.sh` | `manual` opens GUI (pick Manual/Full-auto on Start); `full-auto` detaches bounded automation (heal/remutate/re-execute, no silent quality waivers, publish + S3, `operator/EXECUTION_REPORT.md`). TTY prompts when unset. `--full-auto` is the same. |
| `MUX_INPUT_AUDIO` | picker | Full-auto / `full_auto_driver` | Relative path directly under `ASSETS/input/` (e.g. `ASSETS/input/interview.mp3`); required for non-TTY Full-auto. `--input` is the same. Non-WAV files are converted to sibling PCM WAV before stages run. |
| `MUX_FULL_AUTO` | `0` | `./scripts/run.sh` / GUI Start | `1` enables Full-auto soft stack (heal/remutate/re-execute, soft waivers, publish + S3). Regular `./scripts/run.sh` (this unset) stops leftover Full-auto daemons before serve; this flag skips that stop so Full-auto can recycle/resume its stack. |
| `MUX_BABA_E2E` | `0` | legacy | Alias for `MUX_FULL_AUTO` (accepted once during rename transition) |
| `MUX_NO_BROWSER` | `0` / auto on Full-auto | `./scripts/run.sh` | `1` passes `--no-browser` to serve |
| `MUX_DETACH_SERVE` | `0` / auto on Full-auto | `./scripts/run.sh` | `1` starts serve in its own session so Full-auto survives shell exit |
| `MUX_KEEPALIVE` | `0` | `./scripts/run.sh`, GUI Full-auto, `full_auto_daemon_launch.py` | `1` starts the crash-restart watchdog (`full_auto_keepalive_loop.py`). Off by default so killing the GUI cannot resurrect serve. Equivalent: `--keepalive` or `python tools/full_auto_daemon_launch.py keepalive` |
| `MUX_SKIP_PRECLEAN` | unset | `full_auto_driver.py`, `full_auto_daemon_launch.py` | `1` dismisses `audio_preclean` before ingest (writes `preclean/skip.json`; no DeepFilterNet). Unset / `0` keeps the Full-auto default accept. Set at launch and on every driver restart so keepalive cannot re-accept. |

---

## GUI session persistence (`ASSETS/.gui/active_execution.json`)

Written by `PUT /api/session/active` into `application_state.json` (legacy `active_execution.json` synced). Restored on `GET /api/session` during a **single** `./scripts/run.sh` process (browser refresh). Each new `./scripts/run.sh` launch clears session files by default; use `MUX_PRESERVE_SESSION=1` to keep the pointer across that launch.

| Field | Values | Purpose |
|-------|--------|---------|
| `run_id` | execution id | Active run pointer |
| `selected_stage_id` | stage id | Pipeline sidebar focus |
| `active_tab` | `start` \| `executions` \| `pipeline` \| `logs` | Main tab |
| `pipeline_sub_tab` | `stage` \| `story` \| `timeline` \| … | Pipeline tool row |
| `activity_log_tab` | `live` \| `step` \| `all` | Inline activity panel tab |
| `activity_log_collapsed` | bool | Collapse Pipeline activity column |
| `source_locked` | bool | Session source lock (default true when run active) |

No new `journey_ui.*` keys were added for the activity panel — tab/collapse state uses session fields above.

---

## Top-level

| Key | Used by | If wrong |
|-----|---------|----------|
| `assets_root` | `RunContext`, GUI assets list (`GET /api/assets`) | Wrong folder for audio discovery |
| `input_audio_path` | CLI / tools **default** when no run exists yet | Headless analysis points at missing file; **GUI operators use asset picker instead** — [assets-and-executions.md](./assets-and-executions.md) |
| `data_root` | Legacy runs `data/run_NNN` | Legacy paths broken |
| `executions_root` | New runs under `ASSETS/executions/...` | Runs created outside expected tree; resume breaks |
| `sample_rate` | Ingest / mastering expectation | Wrong SR → Transcribe or mux issues |
| `flow1_target_lufs` / `flow2_target_lufs` | Mastering targets (when enforced) | Wrong loudness “sound” |
| `target_lufs` | Preferred alias for podcast master LUFS (falls back to `flow1_target_lufs`) | Wrong loudness |
| `ingest` | Source format + loudness stabilize after optional preclean | Quiet/hot source stays unleveled through STT/review |
| `master` | Final safety limiter before loudness normalization | Disabled or unsafe settings reduce peak protection |
| `creative_delivery` | Selection trim requirements and exclusion floors | Selection can over-trim or retain low-value material |
| `local_chatterbox` | Local Chatterbox VO: `enabled`, `model_id`, `device` (`auto`→MPS), `timeout_sec`, `fail_open` | Gap VO synthesis unavailable or stalls |
| `gui_job` | Background job stall thresholds (`stall_threshold_sec` default **1200**, `subprocess_stall_threshold_sec` default **7200**) | False stall warnings or late detection |
| `show_description_min_words` / `show_description_max_words` / `show_description_target_words` | Flow 3 schema band + editorial target (defaults **150** / **250** / **200**) | Blurb fails validation or drifts from product spec |
| `g1_5_preview_pickup` | `gates_tbiy.py`, G1.5 post-preview pickup panel (→ Mastering Realization preview gate) | When `enabled`, blocks SFX until post-preview VO re-recorded |
| `production_profiles` | `production_profile.py`, legacy TBIY/documentary profile (hints under Mastering Process) | Wrong profile → incorrect gate/lint until cutover |
| `production_style` | Sound design + mix defaults; Mastering Process treats as *hint* only | Style mismatch vs operator intent |
| `source_topology` | `source_topology.py`, topology + pickup speaker; feeds research Wave 2 | Missing topology breaks pickup speaker rules |
| `flow_adaptation.recovery_policy` | Written by `source_topology_build`; consumed at compose / EDL / mix and by `recovery_controller` | Wrong `vo_posture` force-airs balanced interviews or skips needed framing; `synth_ladder` gates Chatterbox→mlx QC retry without flipping `gap_vo.auto_fallback_on_qc_fail` |
| `mastering` | Mastering Process + quality-hardening gates — see [`mastering.*`](#mastering) below | Gates skipped or blocking at the wrong time |
| `web_port` | `serve` / `run.sh` | GUI on wrong port / collision |
| `web.api_consent_persist` | `POST /api/session/api-consent`, GUI | When `true` (default), grants written to `ASSETS/.gui/api_consent.json` for convenience across `./scripts/run.sh` relaunches |
| `journey_ui.enabled` | GUI phase sidebar, journey snapshot | When `false`, flat stage list (legacy UI); meta still written |
| `journey_ui.phase_sidebar` | `PipelineStepList` phase grouping | When `false`, flat numbered step list |
| `journey_ui.story_board` | — | **Inert.** Story Board tab removed; key ignored |

Pre-clean offers appear inline via `PrecleanOfferCard` on matching stages and the operator checkpoint modal. The legacy **Audio quality** drawer and `GET …/audio-quality` endpoint were removed.
| `journey_ui.express_flow1` | Express delivery CTAs in journey kernel | When `false`, hides express shortcuts |
| `journey_ui.journey_log_filter` | Logs tab journey-scoped filter | When `false`, standard log filters only |
| `journey_ui.require_preview_listen` | Polish CTA gating after `assembly_preview` | When `true`, requires `POST …/milestones/preview-listened` before polish execute |
| `journey_ui.require_handoff_between_stages` | — | **Inert.** Handoff acks removed (`custom_run_handoff.py` deleted); key ignored |
| `journey_ui.enable_stage_reuse_offers` | `stage_execution_reuse`, pipeline, GUI | When `true` (default), blocks execute until reuse decision when candidates exist; when `false`, UI still lists offers but does not block (CLI: `--no-reuse-offers`) — [stage-execution-reuse.md](../workflows/stage-execution-reuse.md) |
| `journey_ui.stage_reuse_lookback_executions` | `stage_execution_reuse`, session lineage | How many prior executions (by `execution_number`, newest first) to scan for hash-matched reuse offers (default `5`) — [stage-execution-reuse.md](../workflows/stage-execution-reuse.md) |
| `journey_ui.require_write_approval_per_stage` | `write_staging`, pipeline | Still read by `write_staging.py`, but v2 sets `v2.auto_commit_artifacts: true` and the `WriteApprovalPanel` / `pending-writes` API were removed — leave at the v2 default so writes go straight to final paths |
| `journey_ui.auto_advance_pipeline` | GUI stage continuation | When `false` (shipped default), after save/checkpoint the GUI **focuses** the next runnable stage and shows *Ready for … — use Run when you want to start*; operator runs each stage explicitly. Set `true` for unattended auto-run between stages. |
| `journey_ui.full_autopilot` | — | **Inert.** Autopilot and the Stage Decision Wizard were removed; no code reads this key |
| `journey_ui.first_try_mode` | `first_try.py`, gates, write staging | Cold-start friction collapse (default **true**). Kept in v2. |
| `journey_ui.defer_write_approval_until` | `write_staging`, journey `_blocking` | `phase_end` (default under first_try): keep `.pending_writes/` without mid-phase pause. Only meaningful if write staging is re-enabled; v2 auto-commits. |
| `journey_ui.segmentation_unified_review` | `write_staging`, `first_try`, GUI `SegmentationReviewPanel` | When `true` (default), `boundary_detection` stages without write-approval pause; `segment_classification` offers paired review/save for boundaries + manifest. |
| `journey_ui.preclean_auto_dismiss_when_green` | `source_readiness` | When true, auto-dismiss preclean offer on green readiness (never auto-accept). |
| `journey_ui.batch_save_phases` | batch Save API/GUI | Default `["analysis","delivery"]`. |
| `transcript_review.auto_complete_when_clean` | `transcript_review` | Auto-complete G0 when zero `needs_review` chunks. |
| `transcript_review.low_confidence_threshold` | `transcript_review` | Chunks below this confidence get `needs_review` (default `0.85`). |
| `analysis.g1.blocking_severities` | `gates.check_g1_vo` | Severities that require VO WAV (default `high`,`critical`). |
| `analysis.first_try.artifact_issue_triage.*` | `first_try.py`, `artifact_root_cause.py` | Optional first_try triage overrides (extra attempt, lower confidence). |
| `sound_design.one_regen_on_fail` | `sfx_mmaudio` | One regen before placeholder under first_try. |
| `sound_design.first_try_allow_placeholder_mix` | mix / mmaudio | Allow silent SFX placeholders instead of hard-fail. |
| `mix.completeness_gate.hard_fail_missing_blocking_vo` | `mix_completeness` | Hard-fail missing blocking VO (default true). |
| `mix.completeness_gate.hard_fail_empty_speech` | `mix_completeness` | Hard-fail empty speech (default true). |
| `mix.completeness_gate.soft_fail_sfx_placeholder` | `mix_completeness` | Warn on SFX placeholders (default true). |
| `mix.missing_vo_retry_once` | `sound_design.mix` | Last-chance generate-once for missing seated VO (default true). |
| `autopilot_enabled` / `operator.autopilot_enabled` | — | **Inert.** Autopilot removed; no code reads these keys |
| `g1_5_require_prompt_approval` | `sfx_prompt_review`, `sfx_mmaudio`, GUI `/sfx-prompts` | When `true` (shipped default), blocks MMAudio SFX until prompts approved. Full-auto auto-approves (incl. soft completeness warnings); first_try auto-approves only when QA green; Partial/manual wait for GUI when warnings present |
| `g1_5_require_music_listen` | `music_listen_review`, `mix`, GUI music listen | Default `false`: automated candidate selection + underbed A/B QC gate mix; set `true` to additionally require operator listening |
| `narrative_qc.strict` | `gates.check_narrative_qc`, `selection`, `assembly` | When `true`, blocks `full_master_ranking` / `edl` on topic/chapter failures for manual/partial (production default `true`). **FMR-B2:** Full-auto softens fail to advisory continue without flipping the config flag |
| `edl_qc.strict` | `gates.check_edl_qc`, `assembly`, `tools/validate_edl.py` | When `true`, blocks invalid EDL timeline mechanics before mix/export |
| `edl_narrative_qc.strict` | `gates.check_edl_narrative_qc`, `assembly`, `tools/validate_narrative.py --include-edl` | When `true`, blocks `edl` when final EDL breaks coverage, chapter continuity, ordering constraints, transitions, gap placements, or flagship audit findings |
| `edl_narrative_qc.require_synthesized_vo` | `edl_narrative_qc._validate_gap_placements` | When `true`, requires synthesized gap lines to have WAV on vo_pickup clips (default `false`) |
| `edl_narrative_qc.require_framing_before_impact` | `edl_narrative_qc._validate_framing_before_impact` | When `true`, each impact block primary segment must have preceding framing VO in EDL order |
| `analysis.gap_framing.min_vo_insert_ratio` | `artifact_repairs._enforce_min_vo_insert_ratio`, compose `vo_line_budget` | Hard density floor vs selected speech (default **0** — off; do not force mid-monologue VO) |
| `analysis.gap_framing.target_vo_insert_ratio` | compose `vo_line_budget`, delivery_brief | Soft seam-coverage aim (default **0.08** — sparse; omit when native handoff is enough) |
| `analysis.gap_framing.interviewer_question_max_words` | `gap_framing`, compose/ranking prompts | Max words for `framing_question` lines (default **60**) |
| `analysis.gap_framing.max_exclusion_ratio` | `framing_coverage_guard` | Cap on framing-driven exclusions vs manifest size (default **0.15**) |
| `analysis.gap_framing.never_exclude_primary_impact` | `framing_coverage_guard` | Block excluding sole primary impact segment (default **true**) |
| `analysis.gap_framing.require_topic_survival` | `framing_coverage_guard` | Block exclusions that zero out a brief topic (default **true**) |
| `analysis.gap_framing.prior_native_context.enabled` | `gap_vo_prior_context`, compose/recompose, density seeds | Attach prior ordered native segment to VO LLM inputs (default **true**) |
| `analysis.gap_framing.prior_native_context.volley_turns_enabled` | `llm_simple` + `gap_vo_prior_context` | Prepend sequential user/assistant prior-beat turns before gap framing compose (default **true**) |
| `analysis.gap_framing.prior_native_context.rewrite_density_seeds` | `artifact_repairs._enforce_min_vo_insert_ratio` | Rewrite interruptive density stock using prior beat (default **true**) |
| `analysis.gap_framing.prior_native_context.relocate_micro_targets` | `gap_vo_prior_context` | Move density VO off micro backchannels onto next substantive segment (default **true**) |
| `analysis.gap_framing.vo_value_gate.enabled` | `vo_value_violations`, `deterministic_lint` | Enforce conversation-partner VO quality (rationale + no-restate) (default **true**) |
| `analysis.gap_framing.vo_value_gate.restate_overlap_max` | `vo_value_violations` | Max VO↔next-clip content-token overlap before fail (default **0.75**; single soft ceiling for all line categories) |
| `analysis.gap_framing.vo_value_gate.allow_summary_overlap_max` | `vo_value_violations` | Alias kept for config compat; same **0.75** soft ceiling (no separate summary limit) |
| `analysis.gap_framing.vo_value_gate.require_rationale` | `vo_value_violations` | Require non-empty `rationale` on each interviewer line (default **true**) |
| `analysis.gap_framing.vo_value_gate.require_forward_cue` | `vo_value_violations` | Last sentence of every VO must unlock the next beat (default **true**) |
| `analysis.gap_framing.vo_value_gate.require_cold_open_layup` | `vo_value_violations` | Preface / first-segment last sentence must cue the actual first native clip (default **true**). Does **not** force a synthetic preface: `ensure_episode_orientation` omits when native hosts already intro. |
| `analysis.nugget_layup.enabled` | `nugget_layup`, corpus/layup stages | Master switch for Nugget Layup System (default **true**) |
| `analysis.nugget_layup.require_layup_per_native` | `evaluate_layup_qc` | Require a plan row per ordered native (default **true**) |
| `analysis.nugget_layup.min_layup_coverage` | `evaluate_layup_qc` | Secondary row-density floor: min fraction of natives with non-skip lay-up text (default **0.70**; authoritative nugget metric is `min_nugget_air_coverage`) |
| `analysis.nugget_layup.min_layup_words` / `max_layup_words` | layup compose prompt budgets | Word bounds for each before-VO (defaults **18** / **90**) |
| `analysis.nugget_layup.prefer_excluded_nuggets` | corpus/layup prompts | Prefer recovering off-air facts (default **true**) |
| `analysis.nugget_layup.segment_text_max_chars` | `build_corpus_mine_input`, `build_layup_compose_input` | Chars of native text handed to the LLM — full upcoming clip, not a stub (default **1500**) |
| `analysis.nugget_layup.prior_close_excerpt_chars` | `build_layup_compose_input` | Tail of the previous native handed to compose as the seam's starting point (default **480**) |
| `analysis.nugget_layup.max_open_nuggets_per_target` | `rank_open_nuggets_for_target` | Open corpus nuggets ranked per target (default **8**) |
| `analysis.nugget_layup.slim_open_nuggets` | `build_layup_compose_input` | Ranked rows are id/score pointers; claims live once in `nugget_corpus` (default **true**) |
| `analysis.nugget_layup.compose_batch_max_natives` | `run_nugget_layup_compose` | Shard compose when air order exceeds this many natives (default **32**) |
| `analysis.nugget_layup.require_analysis_fields` | `evaluate_layup_craft` | Require `target_beat` / `listener_need_entering_T` / `forward_unlock` per line (default **true**) |
| `analysis.nugget_layup.ban_canned_air` | `evaluate_layup_craft`, `lint_gap_report_layup_authority`, `seam_glue.mint_missing_transitions` | Reject hinge-menu / generic-unlock copy as air under layup authority (default **true**) |
| `analysis.nugget_layup.unique_nuggets_across_layups` | `evaluate_layup_craft` | When **true**, one nugget may be claimed by one lay-up only (hard fail). Default **false**: warn + dedupe on publish/EDL |
| `analysis.nugget_layup.max_cross_layup_overlap` | `evaluate_layup_craft` | Max token overlap between two lay-up lines (default **0.6**) |
| `analysis.nugget_layup.max_target_restate_overlap` | `evaluate_layup_craft` | Max token overlap between a lay-up and the clip it introduces (default **0.75**) |
| `analysis.nugget_layup.suppress_placeholder_seams_when_layup` | `seam_glue` | Skip canned seam mint when before-VO layup exists (default **true**) |
| `analysis.nugget_layup.demote_synthetic_framing_content` | `synthetic_framing` | Skip contentful synthetic framing LLM under layup authority (default **true**) |
| `analysis.nugget_layup.authoritative_gap_report` | `gap_framing_recompose` | Recompose becomes thin adapter when layup plan exists (default **true**) |
| `analysis.nugget_layup.block_on_open_must_keep` | `assert_layup_qc_or_raise` | Fail compose when must_keep TPs remain open (default **true**) |
| `analysis.nugget_layup.block_on_open_high_salience` | `evaluate_layup_qc` | Fail compose when high/critical corpus nuggets remain unaired (default **true**). Skip rows do not discharge. |
| `analysis.nugget_layup.degraded_layup.*` | spine mask + craft QC | Grace floors / unclear-span policy for lexicon islands (default enabled) |
| `analysis.low_conf_selection.enabled` | `low_conf_island_scan` | Master switch for density ladder + top-decile must_keep (default **true**) |
| `analysis.low_conf_selection.top_percentile` | `write_low_conf_must_keep` | Hard-include fraction of natives (default **0.10**) |
| `analysis.low_conf_selection.enforcement_mode` | `authoritative_low_conf_must_keep_ids` | `authoritative` (default) or soft |
| `analysis.low_conf_selection.cluster.*` | `scan_low_conf_islands` | Ladder thresholds (loose sprinkle, windows, soft density) |
| `analysis.high_value_speech_islands.enabled` | `scan_high_value_speech_islands` | Volume-gated STT-skip / multi low-conf detector (default **true**) |
| `analysis.high_value_speech_islands.min_gap_ms` | STT-skip energy spans | Default **2000** — word-gap with speech RMS |
| `analysis.high_value_speech_islands.min_cluster_ms` / `min_cluster_words` | low-conf cluster promote | Default **1500** / **3** |
| `analysis.high_value_speech_islands.level_ratio_min` / `max` | volume gate vs tape median RMS | Default **0.55** / **1.45** |
| `analysis.high_value_speech_islands.ranking_boost` / `importance_score` | pack + ranking priors | Default **0.85** / **0.92** |
| `analysis.high_value_speech_islands.cluster_join_max_gap_ms` | `group_high_value_island_clusters` | Max gap to join L islands into one multi-cluster (default **8000**) |
| `analysis.high_value_speech_islands.flow_break_min_high_conf_ms` | cluster separate | Long comprehensible H break (default **12000**) |
| `analysis.high_value_speech_islands.flow_break_min_high_conf_segments` | cluster separate | With topic/subtopic change, **1** intervening H segment is enough |
| `analysis.high_value_speech_islands.multi_cluster_min_islands` | density class | Default **2** → economy structure path |
| `analysis.high_value_speech_islands.max_island_cluster_rounds` | `run_high_value_cluster_fuse_rounds` | Default **32** |
| `analysis.high_value_speech_islands.per_cluster_fuse` | connector fuse HV path | Default **true** |
| `analysis.high_value_speech_islands.island_cluster_structure.*` | `island_cluster_structure_adjudicate` | `llm_tier=economy`, fail-open, lock_forced_seams, max_block_chars |
| `analysis.connector_fuse.enabled` | `connector_fuse_pass` | Seam LLM fuse loop (default **true**) |
| `analysis.connector_fuse.max_fuses_per_pass` / `max_fuse_rounds` | `run_connector_fuse_pass` | **0** = no episode-wide count budget; incomplete-thought fuses still run to fixed point or oscillation halt |
| `analysis.connector_fuse.incomplete_thought_only` | adjudicate + apply | Default **true** — fuse unfinished sentences only; same-speaker complete ideas stay independent |
| `analysis.connector_fuse.prefer_stay_when_uncertain` | adjudicate | Default **true** — low-confidence complete seams stay apart |
| `analysis.connector_fuse.prefer_fuse_when_hint_and_uncertain` | LLM policy | Default **true** — still prefer fuse when hanging / continuer-open / island-straddle hints fire |
| `analysis.connector_fuse.uncertain_confidence_floor` | adjudicate | Default **0.55** — below this, complete-thought fuses are refused |
| `analysis.connector_fuse.tail_words` / `head_words` / `llm_batch_size` | seam packets | Defaults **16** / **16** / **16** |
| `analysis.connector_fuse.close_excerpt_max_chars` | seam packets | Default **480** — earlier close shown to the seam LLM |
| `analysis.connector_fuse.allow_cross_speaker_fuse` | adjudicate | Default **false** for editorial same-topic fuses. Incomplete-thought / hanging-setup fuses may still cross speakers on a tight gap. |
| `analysis.connector_fuse.llm_tier` | `connector_seam_adjudicate` | Default **economy** |
| `analysis.connector_fuse.max_seam_gap_ms` | seam short-circuit / apply | Default **8000**; large gaps stay independent unless island-straddle / high-value force |
| `analysis.connector_fuse.same_topic_score_floor` | apply fuse / high-value neighbor | Default **0.15**; refuse fuse when declared topics barely overlap (high-value force bypasses) |
| `analysis.connector_fuse.max_fused_duration_ms` | apply fuse | Default **25000** — cap for non-incomplete editorial fuses so one speaker run cannot collapse into a slab |
| `analysis.connector_fuse.max_fused_members` | apply fuse | Default **3** — member cap for non-incomplete editorial fuses |
| `analysis.connector_fuse.allow_high_value_bridge` | `plan_cluster_fuses` | Default **false** — high-value islands attach to **one** neighbor; do not glue left+right |
| `analysis.gap_vo.min_reference_sec` | `voice_reference.approve_voice_reference` | Hard reject collated reference shorter than N seconds (default **3.0**) |
| `analysis.gap_vo.fail_open` | `s2s_runner`, `chatterbox_runner` | Chatterbox → mlx-audio fallback on synthesis failure (default **false** — hard-stop) |
| `analysis.gap_vo.fallback_to_manual_on_failure` | `synthesis_fallback` | After Chatterbox + mlx fail, switch lines to `delivery: record` and continue (default **false** — hard-stop; set **true** for legacy degrade) |
| `analysis.gap_vo.timbre_match.enabled` | `timbre_match`, G1 `/match` endpoint | Enables deterministic spectral/loudness matching of an operator take; never synthesizes replacement words |
| `analysis.gap_vo.timbre_match.max_eq_db` | `timbre_match` | Clamps the reference-derived EQ correction (default **6 dB**) |
| `analysis.gap_vo.post_synthesis_qc` | `vo_synthesis_audit.record_synthesis` | Duration QC + speech QA; `max_ms_per_word` / `min_ms_per_word` hard-fail TTS stutter vs script |
| `analysis.gap_vo.adjudicate_before_synth` | `vo_line_adjudicate` | Require smart adjudicate before `vo_synthesize` on homunculus 0.1.0+ (default **true**) |
| `analysis.gap_vo.adjudicate_fail_open` | `vo_line_adjudicate` | On nugget air coverage below floor after adjudicate+intro: warn+continue when **true** (default **true**); loud_fail when false |
| `analysis.gap_vo.adjudicate_batch_size` | `vo_line_adjudicate` | Lines per economy adjudicate volley (default **5**) |
| `analysis.gap_vo.adjudicate_llm_tier` | `vo_line_adjudicate` | OpenAI tier for body adjudicate batches (default **economy**) |
| `analysis.gap_vo.adjudicate_flow_threshold` | `vo_line_adjudicate` | Pre-score below → LLM adjudicate (default **0.55**) |
| `analysis.gap_vo.full_resynth_on_adjudicate_change` | `vo_line_adjudicate`, `vo_synthesis_audit` | 1A — nuke synth WAVs on adjudicate mutation (default **true**) |
| `analysis.gap_vo.intro_compose_llm_tier` | `nugget_intro_compose` | Flagship tier for intro preface LLM (default **flagship**) |
| `analysis.nugget_layup.min_nugget_air_coverage` | `evaluate_nugget_air_coverage`, `nugget_intro_compose`, `vo_line_adjudicate`, `nugget_allocation_plan.json` | Body + intro combined nugget air **goal** (default **0.85**). When `air_coverage_aspirational` is true (default), under-goal coverage is advisory at compose/adjudicate; structural refuse is unaccounted open high-salience or `catastrophic_nugget_air_coverage`. Eligible = corpus nuggets − waived − already native in selection. |
| `analysis.nugget_layup.air_coverage_aspirational` | `evaluate_nugget_air_coverage`, `evaluate_layup_qc`, pick-best | Soften 0.85 to goal + best-of-N (default **true**). Set **false** to restore prior hard floor. |
| `analysis.nugget_layup.air_coverage_max_attempts` | layup candidate ledger | Max compose/heal candidates before pick-best (default **3**) |
| `analysis.nugget_layup.catastrophic_nugget_air_coverage` | `evaluate_nugget_air_coverage` | Hard refuse below this floor even when aspirational (default **0.0** = disabled) |
| `analysis.gap_vo.auto_fallback_on_qc_fail` | `vo_synthesis_audit.qc_failed`, `s2s_runner` | Global Chatterbox→mlx retry after QC fail (default **false**). Topology `recovery_policy.synth_ladder=chatterbox_then_mlx_qc` may enable the same retry **per run** without flipping this charter default |
| `v2.lint_blocking` | — | **Documented only** on v2 simple path; defaults `false` — see [reliability-charter.md](./reliability-charter.md) |
| `v2.cross_validate_blocking` | — | **Documented only** on v2 simple path; defaults `false` |
| `show_notes_qc.strict` | `gates.check_show_notes_qc` | **Inert.** Flow 3 publishing was removed, so no stage produces a show description and the gate is never called |
| `value_analysis.enabled` | `tools/run_value_spike.py`, `tools/extract_value_features.py`, gap volleys | Master switch for deterministic value features + investigation triggers (production default `true`) |
| `value_analysis.spike_scoring` | `run_value_spike.py` | Spike scorecard aggregation when master enabled |
| `value_analysis.transcript_features` | `extract_value_features.py --profile transcript` | Transcript-derived metrics artifact |
| `value_analysis.audio_features` | `extract_value_features.py --profile audio` | Audio-derived metrics (normalized.wav) |
| `value_analysis.auto_extract_after_content_context` | `understanding.run_content_context` | When master + this flag on, writes `understanding/value_features.json` after successful `content_context` (default **on** in shipped `app.defaults.json`) |
| `transcript_review.sort_mode` | `stages/transcript_review.run_transcript_review_build` | G0 queue order: `salience` (default, H-G0-01) or `confidence` (legacy ascending) |
| `analysis.specialists.comprehension_risk_threshold` | `llm_specialists._process_specialist_investigations` | Minimum `risk_score` before `gap_unresolved` investigation from `comprehension_risk_blind` (default **0.7**) |
| `interview_spine.enabled` | `interview_spine_stage.run_interview_spine_build` | Master switch for time-aligned comprehension spine (default **on**) |
| `interview_spine.clap_enabled` | `interview_spine/clap_index.py` | Build CLAP sidecar `understanding/interview_spine/embeddings.npz`; fail-open when MMAudio venv missing |
| `interview_spine.prosody_enabled` | `interview_spine/features.py`, SAP `prosody_summary` | Per-window F0 via librosa pyin when available |
| `interview_spine.window_sec_default` | `interview_spine/windows.py` | Default window length (seconds) for conversational pace |
| `interview_spine.window_sec_dense` | `interview_spine/windows.py` | Window length when SAP `pace_class` is `dense` |
| `interview_spine.window_sec_calm` | `interview_spine/windows.py` | Window length when SAP `pace_class` is `calm` |
| `interview_spine.hop_sec` | `interview_spine/windows.py` | Hop for subdividing long spans |
| `interview_spine.boundary_fusion_min_sources` | `interview_spine/boundaries.py` | Minimum fused sources to keep boundary events (default `1`) |
| `interview_spine.clap_model_id` | `tools/clap_embed_window.py` | CLAP model id for retrieval embeddings |
| `interview_spine.clap_timeout_sec` | `interview_spine/clap_index.py` | Subprocess timeout per window embed |
| `interview_spine.ssl_enabled` | — | **Opt-in only** — Wav2Vec/SSL merge path; default **false** (H-ING-01 gate preserved) |
| `interview_spine.flow2_quotability_enabled` | `stage_enrichment.quotability_signals` | Fuse spine boundary events into Flow 2 quotability proxy |
| `value_analysis.orc03_enabled` | `coherence/config.py` | Master sub-flag for H-ORC-03 long-run coherence (requires `value_analysis.enabled`) |
| `coherence.enabled` | `coherence/analyze.py` | Master switch for coherence report + investigations |
| `coherence.min_duration_ms` | `coherence/duration_gate.py` | Activation threshold (default **1800000** = 30 minutes) |
| `coherence.max_investigations_per_run` | `coherence/investigations.py` | Cap enqueue per run |
| `coherence.max_risks_in_memory` | `coherence/memory_sync.py` | Cap `analysis_state.coherence_risks[]` |
| `coherence.topic_drift_threshold` | `coherence/analyze.py` | Minimum `drift_score` for topic_drift risk |
| `coherence.claim_contradiction_threshold` | `coherence/claim_contradiction.py` | Minimum confidence for contradiction risk |
| `coherence.missing_callback_threshold` | `coherence/missing_callback.py` | Minimum confidence for missing callback risk |
| `coherence.require_acoustic_novelty` | `coherence/analyze.py` | Require novelty gate for topic_drift |
| `coherence.novelty_min_delta` | `coherence/novelty.py`, `analyze.py` | Adjacent-window novelty minimum |
| `coherence.theme_alignment_min` | `coherence/theme_alignment.py` | Token overlap floor for theme match |
| `coherence.blocking_claim_contradiction` | `coherence/claim_contradiction.py` | High-confidence contradictions block `analysis_ready` |
| `coherence.replace_stub_topic_shift_hints` | `interview_spine/boundaries.py`, `value_analysis/extract.py` | Disable speaker-turn-only stub when full coherence on |
| *(implicit)* ORC-02 quality flags cap | `value_analysis/extract.py` | Max **5** `quality_trajectory_flags` enqueued per run (not a config key) |
| *(implicit)* ORC-02 spine cap | `value_analysis/extract.py` `_spine_orchestration_investigations` | Max **6** spine boundary investigations per run (not a config key) |
| `models.<stage_key>` | `get_model()` → OpenAI calls | Wrong model: cost/quality drift; unknown name → API errors |

**Secrets override (not in JSON):** `INPUT_AUDIO_PATH` in `secrets.env` replaces `input_audio_path` for **CLI/automation only**. Not required for GUI: operators pick WAVs under `ASSETS/` — see [assets-and-executions.md](./assets-and-executions.md).

**Optional secrets (fallback):** `OPENAI_MODEL` used when a stage key is missing from tier resolution.

### `master` — final safety and loudness chain

| Key | Default | Used by | If wrong |
|-----|---------|---------|----------|
| `master.safety_limiter_enabled` | `true` | `stages/mastering.py` | Disables the pre-loudnorm peak safety stage |
| `master.safety_limiter_limit_db` | `-1.0` | FFmpeg `alimiter` | Too low over-compresses; above 0 is rejected |
| `master.safety_limiter_attack_ms` | `5` | FFmpeg `alimiter` | Outside FFmpeg's 0.1–80 ms range is rejected |
| `master.safety_limiter_release_ms` | `50` | FFmpeg `alimiter` | Outside FFmpeg's 1–8000 ms range is rejected |

### `ingest` — source format + loudness stabilize

Runs on the ingest ffmpeg pass **after** optional `audio_preclean` (uses `preclean/isolated.wav` when present). Default on: **upward-only** soft boost (`acompressor` mode=`upward`) then `loudnorm` to −18 LUFS headroom so quiet captures are enjoyable for STT/review **without ducking louder syllables**. After full-source preclean, skip the upward stage (loudnorm only) so residual hiss is not lifted. `master_finalize` still targets podcast −16 LUFS.

| Key | Default | Used by | If wrong |
|-----|---------|---------|----------|
| `ingest.loudness_stabilize.enabled` | `true` | `stages/ingest.py`, `source_loudness.py` | `false` keeps format-only ingest (quiet/hot source unchanged) |
| `ingest.loudness_stabilize.target_lufs` | `-18.0` | FFmpeg `loudnorm` I= | Too hot leaves no headroom for master; too quiet hurts listenability |
| `ingest.loudness_stabilize.true_peak_dbtp` | `-1.5` | FFmpeg `loudnorm` TP= | Outside −9…0 rejected |
| `ingest.loudness_stabilize.lra` | `11.0` | FFmpeg `loudnorm` LRA= | Outside 1…50 rejected |
| `ingest.loudness_stabilize.dual_mono` | `true` | FFmpeg `loudnorm` | Mono podcast on stereo meters reads wrong without it |
| `ingest.loudness_stabilize.dynaudnorm` | `true` | Within-file soft leveling before loudnorm | `false` only sets integrated LUFS (within-file wander remains) |
| `ingest.loudness_stabilize.dynaudnorm_mode` | `upward_only` | `upward_only` = soft boost only; `classic` = bidirectional `dynaudnorm` | `classic` can mid-word duck loud syllables |
| `ingest.loudness_stabilize.upward_threshold` | `0.125` | Linear amp (~−18 dBFS); below = boost | Too high over-lifts mid speech; too low leaves quiet sources soft |
| `ingest.loudness_stabilize.upward_ratio` | `3.0` | Upward compression ratio | Outside 1–20 rejected |
| `ingest.loudness_stabilize.upward_attack_ms` | `50.0` | Upward attack | Outside 0.01–2000 rejected |
| `ingest.loudness_stabilize.upward_release_ms` | `300.0` | Upward release | Outside 0.01–9000 rejected |
| `ingest.loudness_stabilize.upward_knee` | `2.5` | Soft knee | Outside 1–8 rejected |
| `ingest.loudness_stabilize.skip_upward_after_preclean` | `true` | Skip `acompressor`/`dynaudnorm` when ingesting `preclean/isolated.wav` | `false` re-lifts residual hiss after DeepFilterNet |
| `ingest.loudness_stabilize.dynaudnorm_frame_ms` | `500` | Classic `dynaudnorm` f= only | Outside 10–8000 rejected |
| `ingest.loudness_stabilize.dynaudnorm_gausssize` | `31` | Classic `dynaudnorm` g= only | Must be odd and ≥ 3 |
| `ingest.loudness_stabilize.dynaudnorm_peak` | `0.95` | Classic `dynaudnorm` p= only | Outside (0, 1] rejected |
| `ingest.loudness_stabilize.dynaudnorm_maxgain` | `10.0` | Classic `dynaudnorm` m= only | Cap on boost for very quiet sources (1–100) |

Artifact: `ingest/loudness.json` (filter lineage). See [ingest README](../pipeline/ingest/README.md).

---

## `models` — runtime (BUILD-073)

Resolved by `get_model(stage_key)` in `src/interview_mux/config.py`:

1. If `models.<stage_key>` is a **string** → use that API ID directly (per-stage override).
2. Else `model_registry.resolve_model(stage_key, task_kind)` using `models.tiers` + `models.stages`.
3. Fallback: `OPENAI_MODEL` secret, then `gpt-4o-mini`.

Flat string overrides in `app.defaults.json` remain the escape hatch when you need an explicit API ID for one stage.

Tier guidance: [llm-stage-model-matrix.md](./llm-stage-model-matrix.md). API ID registry: [model-routing.md](./model-routing.md#model-tier-registry). Committed defaults use **flagship** for all `STAGE_ARTIFACT_SCHEMAS` stages — [artifact-generation-and-validation.md](./artifact-generation-and-validation.md).

| Key | Purpose |
|-----|---------|
| `models.tiers.<economy\|standard\|flagship>` | Maps tier alias → API ID |
| `models.stages.<stage_key>.tier` | Default tier for `task_kind=primary` |
| `models.stages.<stage_key>.severity` | `low` \| `medium` \| `high` — drives collate floor (when set) |
| `models.<stage_key>` (string) | **Override:** explicit API ID wins over tier lookup |

**Optional secrets (tier overrides):**

| Key | Effect |
|-----|--------|
| `OPENAI_TIER_ECONOMY` | Override economy tier API ID |
| `OPENAI_TIER_STANDARD` | Override standard tier API ID |
| `OPENAI_TIER_FLAGSHIP` | Override flagship tier API ID |
| `OPENAI_MODEL` | Fallback when stage missing (unchanged) |

`task_kind` (`primary`, `arbiter`, `shard`, `collate`) is **not** a config key — it is resolved in code.

---

## `structure`

Deterministic episode structure composer — [episode-structure-catalog.md](./episode-structure-catalog.md).

| Key | Default | If wrong |
|-----|---------|----------|
| `structure.enabled` | `true` | Compose/refresh skipped; consumers see empty plan |
| `structure.strict_slots` | `false` | When `true`, SDP cues must map to emitted slots only |
| `structure.max_dynamic_slots` | `24` | Density safety for pack-unlocked DYN components |
| `structure.hook_reel_enabled` | `true` | Cold-open may `repeat_allowed` once |
| `structure.require_payoff` | `false` | **Locked false** — do not force payoff |
| `structure.require_outro` | `false` | **Locked false** — do not force outro |

Artifact: `understanding/episode_structure.json`. Stage: `episode_structure_compose` (off local quality allowlist).

---

## `local_llm`

On-device MLX framing before OpenAI. Shipped in `config/app.defaults.json` (`enabled: true` by default on macOS).

| Key | Default | If wrong |
|-----|---------|----------|
| `local_llm.enabled` | `true` | No local pass when `false`; OpenAI-only volleys |
| `local_llm.model_id` | `mlx-community/Llama-3.2-3B-Instruct-4bit` | Fallback when no `selection.json` (bootstrap writes selection) |
| `local_llm.models_dir` | `ASSETS/local_llm/models` | Download script and runner disagree on path |
| `local_llm.max_volley_turns` | `2` | OpenAI volley bloat; higher API cost |
| `local_llm.max_tokens` | `768` | Truncated framer JSON → forced escalation |
| `local_llm.escalate_on_parse_error` | `true` | `false` risks skipping OpenAI on bad local output |
| `local_llm.min_confidence` | `0.6` | Local framing below threshold → escalate to OpenAI |
| `local_llm.skip_openai_primary_when_local_satisfied` | `false` | When `true`, may skip OpenAI primary on high-confidence local output (P0–P2 stages always escalate) |
| `local_llm.apply_to_specialists` | `true` | Local volley before economy specialist passes |
| `local_llm.apply_to_shards` | `true` | Local volley before shard calls |
| `local_llm.apply_to_collate` | `true` | Local volley before collate calls |
| `local_llm.capability.enabled` | `true` | Quality capability router (allowlisted stages only) |
| `local_llm.capability.max_local_steps` | `3` | Hard cap local steps per stage attempt |
| `local_llm.capability.max_local_retries_per_cap` | `0` | Locked zero — fail-fast; no retry storms |
| `local_llm.capability.planner_fanout_k` | `3` | Economy planner runs only when predicted fanout ≥ K |
| `local_llm.capability.max_enabled_caps` | `4` | Stage-1 calibration ceiling |
| `local_llm.capability.lx03_min_verify_rate` | `0.85` | Gate LX-03 digest compressor enable |
| `local_llm.capability.lx04_min_agreement` | `0.95` | Gate LX-04 escalate advisory enable |
| `local_llm.capability.lx05_min_verify_rate` | `0.85` | Gate LX-05 shard packet prep enable |
| `local_llm.framer_allowlist` | `[]` | Empty → code `QUALITY_LOCAL_ALLOWLIST`; non-empty overrides |
| `local_llm.fail_open` | `true` | Framer verify errors proceed OpenAI-only when `true` |
| `local_llm.request_timeout_sec` | `120` | Local framer subprocess timeout |

Capability calibration writes `ASSETS/local_llm/capability_manifest.json` via `scripts/calibrate_local_llm.py` (bootstrap after model select).

**Operator path (recommended):** do **not** set a local model in `secrets.env`. Run `./scripts/bootstrap_venv.sh` once — llmfit writes `ASSETS/local_llm/selection.json`, downloads weights (reused when present), then `calibrate_local_llm.py` writes `capability_manifest.json`. Launch with `./scripts/run.sh` thereafter.

| Key / artifact | Effect |
|----------------|--------|
| `ASSETS/local_llm/selection.json` | Canonical picked `model_id` + `context_length` |
| `LOCAL_LLM_REFRESH=1` | Bootstrap env only — force llmfit re-selection |
| `LOCAL_LLM_MODEL_ID` | Advanced secret override (not in template); prefer bootstrap |

**llmfit selection:** `scripts/select_local_llm.py` picks largest context among `mlx-community/*` models with fit `perfect`/`good` and quality ≥ 45 (`MIN_QUALITY_SCORE` in `local_llm_selection.py`).

Setup: [SETUP.md](../../SETUP.md) § Local LLM.

---

## `disfluency_extract` / `disfluency_restore` — removed

The G0.5 disfluency gate is gone: `stages/disfluency.py`, the `disfluency_extract` / `disfluency_review` stages, `DisfluencyReviewPanel`, and the `/disfluency-review` API routes were all deleted. Any `disfluency_*` keys left in a local `app.defaults.json` are inert. See [../v2/drop-manifest.md](../v2/drop-manifest.md).

---

## Legacy note — flat-only config

Older docs described only a flat `models.<stage_key>` map. That still works, but **`models.tiers` + `models.stages` are the preferred shape** in `config/app.defaults.json`.

---

## `analysis.sufficiency`

Semantic completeness gates on committed LLM artifacts — `sufficiency_engine.py`, `artifact_lifecycle.build_outputs_view()`.

| Key | Default | If wrong |
|-----|---------|----------|
| `analysis.sufficiency.enabled` | `true` | Sufficiency badges hidden; downstream may proceed on thin artifacts |
| `analysis.sufficiency.default_blocking_tier` | `progression` | Wrong tier → blocks stage progression at wrong severity |
| `analysis.sufficiency.per_stage_overrides` | `{}` | Per-stage tier overrides ignored |

---

## `analysis.remediation_orchestrator` — removed

Micro-gap fill and upstream rerun orchestration. `micro_gap_fill.py` and the remediation orchestrator were deleted; a blocked stage hard-stops instead. The namespace no longer ships in [`config/app.defaults.json`](../../config/app.defaults.json), and the matching `OM-MG-*` rows are gone from the [LLM interaction catalog](./llm-interaction-catalog.md). It stays optional in [app_config.schema.json](./json-schemas/app_config.schema.json) so pre-v2 operator config files still validate.

| Removed key | Old default |
|-------------|-------------|
| `analysis.remediation_orchestrator.enabled` | `true` |
| `analysis.remediation_orchestrator.max_micro_gap_fill_per_stage` | `2` |
| `analysis.remediation_orchestrator.max_upstream_reruns` | `2` |
| `analysis.adaptation_loop.max_micro_gap_fill_calls` | `2` |

---

## `analysis.gap_fill`

Binary eligibility gate for `missing_framing` / `gap_framing_compose` / G1 VO — `gap_fill_eligibility.py`, `stages/gaps.py`.

| Key | Default | If wrong |
|-----|---------|----------|
| `analysis.gap_fill.enabled` | `true` | Eligibility never evaluated; gap stages always run |
| `analysis.gap_fill.default_framing_enabled` | `true` | G-Framing recommends No; product default is Yes + voice-cloned least-spoken host |
| `analysis.gap_fill.require_explicit_opt_in` | `true` | Framing decision may be treated as settled without operator confirm |
| `analysis.gap_fill.auto_accept_defaults` | `false` | Set `true` (or `INTERVIEW_MUX_AUTO_ACCEPT_GATES=1`) for unattended/E2E to apply Yes / cloned host / Chatterbox without human input. Homunculus **0.2.0** can still auto-Yes G-Framing via `recommended_framing_action` even when this is false and the env is unset. |
| `analysis.gap_fill.auto_skip_when_ineligible` | `false` | When false (default, **KEEP**), ineligible framing with G-Framing Yes hard-stops; set true only for legacy silent skip |
| `analysis.gap_fill.frame_confidence_min` | `0.65` | Clone **auto-approve** floor (voice-ref / consent). Does not skip G-Framing eligibility. Below this, Homunculus still auto-Yes but will not auto-approve a non-frame or low-confidence clone. |
| `analysis.gap_fill.min_synthetic_vo_lines` | `3` | Post-layup / EDL / G1 ship bar for hosted 1:1 with G-Framing Yes (capped by native count). Not enforced at `gap_framing_compose`. Panels / sparse-host are auto-Yes without this floor. |
| `analysis.gap_fill.hide_gui_stages_when_skipped` | `false` | If `true`, skipped gap stages are hidden from the step list (legacy v2 behavior; Refinement Pass keeps the full step list always visible) |
| `analysis.gap_fill.sealed_ratio_max` | `0.15` | CAP-seal fraction that triggers one bounded rescue micro-volley |
| `analysis.gap_fill.sealed_ratio_hard_max` | `0.35` | After rescue, seal fraction above this is operator-STOP (`sealed_ratio_hard`) |
| `analysis.gap_fill.sealed_ratio_rescue_max_ids` | `32` | Max segment ids re-volleyed in sealed_ratio / sealed_vs_risk rescue |

---

## `analysis.refinement_passes`

[Refinement Pass](./refinement-passes.md) — L0 agenda, L1 gate, CFI ledger cap, blacklist/whitelist, succession/mutex. `refinement_catalog.py`, `refinement_gate.py`, `refinement_ledger.py`.

| Key | Default | If wrong |
|-----|---------|----------|
| `analysis.refinement_passes.enabled` | `true` | L1 always skips; every Pass 2 stage becomes a deterministic no-op |
| `analysis.refinement_passes.full_auto` | `true` | Reserved — no approval-modal path is implemented; always full auto today |
| `analysis.refinement_passes.max_second_runs_per_cfi` | `1` | Raising this allows more than one refinement call per Canonical Function Identity per run (anti-loop cap) |
| `analysis.refinement_passes.priors.enabled` | `"soft"` | `false` disables prior-biased L0 eligible-class expansion; priors never remove eligibility either way |
| `analysis.refinement_passes.shadow_score.enabled` | `true` | Disables the informational skip-vs-draft score written when a pass is skipped |
| `analysis.refinement_passes.blacklist.*` | see `refinement_catalog._DEFAULT_BLACKLIST_STAGES` | Blacklist always wins over whitelist — removing a stage here can let non-refinable stages (e.g. `mix`) be gated as refinements |
| `analysis.refinement_passes.whitelist.pass_ids` | 7 catalog pass ids | Only listed pass ids can ever `activate`; a pass id missing here always skips with `reason_code: not_whitelisted` |
| `analysis.refinement_passes.succession.unlocks` / `.mutex` / `.priority` | see catalog defaults | Governs pass ordering — wrong unlock rules can leave `ranking_refine` / `transitions_refine` / `sdp_intent_refine` permanently locked |
| `analysis.refinement_passes.policy_packs.*` | see `refinement_policy.POLICY_PACKS` | Per-tape-character eligible-class defaults for L0; overrides merge onto (not replace) the built-in packs |

---

## `analysis.artifact_lifecycle`

Fingerprinting, stale reads, reuse validation — `artifact_lifecycle.py`.

| Key | Default | If wrong |
|-----|---------|----------|
| `analysis.artifact_lifecycle.fingerprint_enabled` | `true` | No content fingerprints on commit |
| `analysis.artifact_lifecycle.post_commit_validate` | `true` | Invalid JSON may persist after approve |
| `analysis.artifact_lifecycle.read_stale_guard` | `true` | Stale artifact reads not blocked |
| `analysis.artifact_lifecycle.reuse_validate` | `true` | Reuse copies skip schema checks |

---

## `analysis.artifact_contract`

Stage contract verification on CI and optional runtime checks.

| Key | Default | If wrong |
|-----|---------|----------|
| `analysis.artifact_contract.enabled` | `true` | Contract drift undetected |
| `analysis.artifact_contract.contracts_dir` | `docs/cross-cutting/stage-contracts` | Wrong contract path |
| `analysis.artifact_contract.verify_on_ci` | `true` | CI skips contract verification |

---

## `analysis.downstream_probe` — removed

Probed downstream consumers when upstream artifacts changed. `downstream_probe.py` and `downstream_requirements.py` were deleted in v2; downstream impact is covered by artifact lifecycle staleness (`analysis.artifact_lifecycle.read_stale_guard`) instead. The namespace no longer ships in [`config/app.defaults.json`](../../config/app.defaults.json). It stays optional in [app_config.schema.json](./json-schemas/app_config.schema.json) so pre-v2 operator config files still validate.

| Removed key | Old default |
|-------------|-------------|
| `analysis.downstream_probe.enabled` | `true` |
| `analysis.downstream_probe.blocking_tier` | `progression` |

---

## `analysis.llm_call_records`

Every OpenAI call via `run_prompt_envelope` (when `ctx` is set). Spec: [llm-call-record-framework.md](./llm-call-record-framework.md).

| Key | Default | If wrong |
|-----|---------|----------|
| `enabled` | `true` | No per-call files; only `stage_runs` attempt summaries |
| `write_markdown_sidecar` | `true` | No `.md` copy-paste files next to JSON records |

## `analysis.structured_outputs`

Strict OpenAI `json_schema` + post-call verification for every `run_prompt_envelope` call. Catalog: [llm-interaction-catalog.md](./llm-interaction-catalog.md).

| Key | Default | If wrong |
|-----|---------|----------|
| `enabled` | `true` | Falls back to `json_object` only |
| `strict` | `true` | OpenAI `json_schema.strict` flag |
| `fail_on_verify_error` | `true` | Invalid responses raise after parse |
| `allow_json_object_fallback` | `false` | On schema compose failure, use loose JSON mode |
| `api_schema_tier` | `full` | Reserved for partial schema tiers |
| `log_verification_to_gui` | `true` | Operator log `llm.verification_failed` events |

## `local_llm.structured_outputs`

Schema appendix in MLX prompts + `verify_llm_response` after `generate_local_chat`.

| Key | Default | If wrong |
|-----|---------|----------|
| `enabled` | `true` | No schema block in local system prompt |
| `strict` | `true` | Prompt cites strict min-example shape |
| `fail_open_on_verify` | `true` | ITR/framer keep fail-open on verify errors (set `false` to hard-fail) |

Export: `python tools/export_llm_calls.py --run-id <exec_*>`.

---

## `analysis.max_iterations_per_stage` / `max_volley_retries` / `max_queue_drains_per_stage` — inert

These configured the pre-v2 orchestrator loop, volley retries, and investigation-queue drains. `analysis_orchestrator.py`, `llm_stage_routing.py`, and `attempt_budget.py` were deleted; `llm_simple.py` now hard-caps every stage at **2** attempts with no queue drain. The keys are ignored.

---

## `analysis.context.*`

Consumed by analysis stage builders and `transcript_shards` proactive batching. Defaults in `config/app.defaults.json` (shipped: `proactive_decompose_chars` 72000, `segment_text_max_chars` 400). See [long-interview-chunking.md](../workflows/long-interview-chunking.md).

| Key | If wrong |
|-----|----------|
| `transcript_excerpt_chars` | Legacy / unused in v2 builders |
| `transcript_full_chars` | Legacy alias; prefer `proactive_decompose_chars` for full-tape shard threshold |
| `speaker_roles_sample_chars` | Speaker role inference sees too little of long interviews |
| `max_transcript_shards` | Long transcripts split into too few/many shard calls |
| `max_shard_batches` | Legacy alias — prefer `analysis.coverage_limits.shard_batch_ceiling` |
| `content_brief_reanchor_min_coverage_ratio` | Legacy alias — prefer `analysis.coverage_limits.reanchor_min_coverage_ratio` |
| `proactive_decompose_chars` | `content_context` / `talking_points_compose` stay single-pass too long (context blow) or shard too early |
| `transcript_shard_overlap_ratio` | Boundary talking points / topics lost between shards (too low) or duplicate spend (too high) |
| `segment_text_max_chars` | Gap / compact manifest text unreadable / over-truncated |
| `max_segments_in_context` | Documented ceiling; not all stages wire this selector |
| `max_segments_in_gap_pass` | Soft ceiling; prefer `proactive_decompose_gap_segments` for batch size |
| `proactive_decompose_gap_segments` | `missing_framing` batches too large (context blow) or too many shard calls |
| `max_gap_evaluations` | Some segments never evaluated in one pass |
| `max_stage_data_chars` | Huge payloads rejected or truncated by model host |
| `interviewer_sample_lines` | Transitions stage lacks tone reference |

## `analysis.ideal_cuts.*`

Talking-points-first cut authority — `ideal_cuts.py`, stages `talking_points_compose` → `ideal_cuts_propose` → `ideal_cuts_materialize`.

| Key | Default | Meaning |
|-----|---------|---------|
| `enable` | `true` | Run the holistic talking-points / ideal-cuts path |
| `bind_mode` | `both` | `off` (artifacts only) · `seed_ranking` · `boundaries` · `both` |
| `min_cut_ms` / `max_cut_ms` | `2500` / `180000` | Clamp snapped native windows |
| `word_snap_margin_ms` / `word_snap_max_shift_ms` | `0` / `150` | Tight snap of LLM times to transcript word edges (no large free shift) |
| `semantic_edge_buffer_ms` | `5000` | Lookback/lookahead budget when auto-fixing illegal opens/ends |
| `acoustic_edge_refine` | `true` | After word pins, micro-nudge into silence valleys on `ingest/normalized.wav` |
| `acoustic_search_ms` | `120` | ±search window for silence-valley micro-snap (never across neighbor words) |
| `pause_midpoint_end` | `true` | When a keep ends on a finished word and the next word starts later, park the cut at mid-pause (protects word release through mix crossfades) |
| `pause_midpoint_min_gap_ms` | `80` | Minimum post-word gap before mid-pause placement applies |
| `pause_midpoint_max_pad_ms` | `1000` | Cap post-word air at this many ms after the word end (midpoint of longer gaps is clamped) |
| `skip_boundary_llm_when_bound` | `true` | When materialize published boundaries, skip `boundary_detection` LLM |
| `skip_topic_resplit_when_bound` | `true` | Skip `boundary_topic_resplit` when ideal-cuts boundaries are authoritative |
| `skip_classification_llm_when_bound` | `true` | Skip `segment_classification` LLM; build manifest from cuts + speakers |
| `prefer_seed_over_ranking` | `true` | Prefer ideal-cuts selection seed in `full_master_ranking` |
| `prefer_seed_over_shape` | `true` | Ideal-cuts air order beats Shape segment order when both bind |
| `min_span_coverage_ratio` | `0.45` | Propose retry + skip deterministic TP narrative when cut span is early-only |
| `overlap_map_min_ms` | `500` | Min overlap when remapping cuts onto real boundaries |

## `analysis.talking_points_authority.*`

Deterministic coverage / narrative / classification from talking points + ideal cuts (skip redundant LLMs).

| Key | Default | Meaning |
|-----|---------|---------|
| `deterministic_coverage` | `true` | Synthesize `master/coverage_audit.json` without LLM when cuts are bound |
| `deterministic_narrative` | `true` | Synthesize `master/narrative_plan.json` without LLM when cuts are bound |
| `deterministic_classification` | `true` | Synthesize `segments/manifest.json` without LLM when ideal-cut boundaries are bound |

## `analysis.truncation_integrity.*`

Consumed by `truncation_policy` + LLM routing gateways. Defaults in `config/app.defaults.json`.

| Key | When it matters |
|-----|-----------------|
| `context_cap_boost_steps` | Rebuild multipliers after truncated volley detected (default `[1,2,4,8,16]`) |
| `field_truncation_clear_floor_chars` | Minimum segment/transcript clip floor during clear-field rebuild (default `8000`) |
| `max_escalation_rounds_per_call` | Caps how many boost rebuild rounds run before hard-block |
| `framer_digest_limits` | Local framer digest size per stage before `…[digest truncated]` |

See [truncation-integrity.md](truncation-integrity.md).

## `analysis.safe_pruning.*`

One-shot packer after a flagship `context_length` API error. Applies to every OpenAI chat call.

| Key | Default | Meaning |
|-----|---------|---------|
| `enabled` | `true` | Master switch |
| `extract_tier` | `economy` | Model tier for per-chunk extract (never flagship) |
| `economy_chunk_char_budget` | `80000` | Max chars per extract chunk |
| `flagship_input_token_budget` | `900000` | Packed volley must estimate under this (`chars/4`) or hard-stop |
| `max_chunks` | `24` | More chunks than this → `SafePruneExhausted` |

See [truncation-integrity.md](truncation-integrity.md) and [model-routing.md](model-routing.md).

---

## `analysis.context_index.*`

Volley Q&A memory — `context_resolver.py`.

| Key | Default | Purpose |
|-----|---------|---------|
| `enabled` | `true` | Master switch for index read/write |
| `sync_plans_on_ensure` | `true` | Refresh `stage_plans` from code `STAGE_PLANS` on workspace ensure |
| `write_on_accept` | `true` | Append `volley_entries` on arbiter-accept merge |
| `prefer_index_over_legacy_summaries` | `false` | When `true`, `build_message_volley` uses index entries instead of legacy `_format_*` fallbacks only |

Rollout: ship with `prefer_index_over_legacy_summaries: false` (dual-write); enable after backfill smoke on real runs.

---

## `analysis.flow_hardening`

Fail-closed LLM stage progression. Implemented in `llm_flow_hardening.py`, `llm_preflight.py`, `artifact_cross_validate.py`.

| Key | Default | Purpose |
|-----|---------|---------|
| `enabled` | `true` | Master switch (`false` = legacy always `mark_done`) |
| `strict_critical_stages` | `true` | `SystemExit` on critical LLM stage failure |
| `preflight_enabled` | `true` | Deterministic checks before OpenAI |
| `cross_validate_enabled` | `true` | Cross-artifact checks at segmentation boundaries |
| `investigation_dedupe` | `true` | Dedupe open investigations by kind+stage+target |
| `inner_retry_require_delta` | `true` | Stop inner retries when volley/errors unchanged |
| `stuck_signature_threshold` | `2` | Identical attempt signatures in a row → stage treated as stuck |
| `block_partial_segment_classification` | `true` | Block partial persist when segment classification obligation lints fail |
| `block_partial_on_quality_fail` | `true` | Critical LLM stages never stage write-approvable partials after lint/schema/truncation/accept failure — audit sidecar only |
| `spend_block_stages` | see defaults | Stages that require complete upstream SDP/craft before API spend |
| `block_mix_without_sfx_when_enabled` | `true` | When `true`, block `mix` if SFX assets missing; set `false` for dry-mix debugging without generated WAVs |
| `clarification_before_halt` | `true` | Run ITR and set `needs_clarification` instead of hard halt when artifacts are repairable |

When `enabled`, `pipeline.py` calls `maybe_require_upstream_llm_progress` before each LLM stage so upstream `.stage_done` and producer artifacts must be complete.

**Spend block:** `spend_block_stages` lists stage ids checked by `llm_flow_hardening.require_spend_prerequisites()` — default `sfx_prompt_craft`, `mmaudio_sfx`, `mix`. If upstream `sound_design_plan.json` or craft artifacts are incomplete, the stage is blocked with no MMAudio generation. Override list only for dev; production should keep defaults.

**Loop policy:** Every LLM stage gets at most **2** attempts in `llm_simple.py`, then hard-stops. The pre-v2 attempt-budget, shard/collate, and arbiter-verdict keys (`max_primary_attempts_per_stage`, `max_arbiter_rejects_per_stage`, `shard_min_success_ratio`, `max_investigation_reruns_per_kind`, `halt_on_schema_errors_with_accept`) are inert.

## `analysis.segmentation`

Timeline authority + boundary/classification hardening — `segment_timeline_standard.py`, `segmentation_input_resolver.py`.

| Key | Default | Purpose |
|-----|---------|---------|
| `reject_invalid_shards` | `true` | Exclude boundary shards that fail timeline validation after normalize |
| `deterministic_collate_authoritative` | `true` | Deterministic `merge_shard_boundaries` wins over LLM collate rows |
| `deterministic_classification_collate_authoritative` | `true` | Contract-ordered deterministic segment collate wins over LLM collate |
| `block_invalid_boundary_commit` | `true` | Refuse commit when `segment_contract.timeline_valid` is false |
| `block_partial_classification` | `true` | Block staging manifest with coverage/type lint failures |
| `fabricate_missing_segments` | `true` | Fabricate missing manifest rows from boundaries before ITR wizard |
| `require_type_diversity` | `true` | Enforce type diversity when interviewer role exists |
| `enforce_field_parity` | `true` | Assert manifest/boundary field parity at collate, hydrate, paired save |
| `drop_orphan_manifest_rows` | `true` | Drop manifest rows not in `segment_contract` on hydrate |
| `boundary_proactive_decompose_pace_classes` | `["calm","brisk"]` | Pace classes eligible for proactive boundary decompose |
| `default_granularity` | `"fine"` | Global segmentation granularity (`fine` \| `standard` \| `coarse`) |
| `max_segment_duration_ms` | `null` | Optional force-split for spans longer than this (unset = no cap) |
| `min_segment_duration_ms` | `8000` | Floor to prevent word-level slivers / mid-sentence fragments |
| `split_backchannels` | `true` | Deterministic split of brief interviewer turns during answers |
| `backchannel_max_words` | `8` | Max words for a splittable backchannel turn |
| `prefer_topic_splits` | `true` | Prefer `topic_shift` over pause-only splits in enrich pass |
| `boundary_merge_threshold_ms` | `200` | Micro-boundary merge floor in fine mode (via `boundary_collate_cfg`) |
| `micro_segment_lint_max` | `400` | Base boundary-count lint ceiling (scales with duration on long interviews) |
| `min_bed_segment_ms_fine` | `6000` | Minimum segment duration for ambient bed slots in fine mode |
| `reject_coarse_fallback` | `true` | Hard-stop on invalid boundary ranges or coarse time-boxed fallback maps — not on isolated over-max segments when the timeline is otherwise fine-grained |

## `analysis.duration_policy`

Duration tiers and scaled loop budgets — `interview_duration_policy.py`. Tiers: **short** &lt;15m, **medium** 15m–1h, **long** &gt;1h.

| Key | Default | Purpose |
|-----|---------|---------|
| `short_max_ms` | `900000` (15m) | Upper bound for short tier; topic-anchor strictness aligns via `prompt_thresholds.content_context_topic_anchor_min_duration_ms` |
| `medium_max_ms` | `3600000` (1h) | Upper bound for medium tier |
| `very_long_min_ms` | `7200000` (2h) | Interviews at or above this use `shard_calls_very_long` |
| `primary_attempts_base` | `6` | Primary routing cap for short interviews |
| `primary_attempts_per_30min_above_short` | `1` | Added to base for each 30m of audio above `short_max_ms` |
| `primary_attempts_cap` | `16` | Hard cap (e.g. ~4h interview → 13 attempts before cap) |
| `shard_calls_base` | `24` | Max per-segment shard calls per stage cycle (&lt;2h) |
| `shard_calls_very_long` | `48` | Shard cap for interviews ≥ `very_long_min_ms` |
| `boundary_micro_segment_min_ms` | `900000` | Minimum duration before boundary micro-segment explosion lint applies |

**Arbiter rubric optional fields** (per-stage JSON under `docs/prompts/_shared/arbiter-rubrics/`):

| Field | Default | Purpose |
|-------|---------|---------|
| `min_segment_coverage_ratio` | `0.85` (or `1.0` when manifest &lt;5 segments) | Threshold for generic `segment_coverage_ratio` lint on decompose-eligible stages |

---

## `analysis.llm_resilience`

Partial persist and sanitize when primary output fails lint/schema — `llm_output_resilience.py`.

| Key | Default | Purpose |
|-----|---------|---------|
| `progression_mode` | `strict` | Stage progression requires acceptance-clean artifacts (`degraded_continue` is deprecated) |
| `partial_persist_enabled` | `true` | Write sanitized partial artifacts to staging for repair input (does not mark stages done). Critical stages with `block_partial_on_quality_fail` still skip this on lint/accept failure |
| `record_stripped_fields` | `true` | Write `attempt_NNN_resilience.json` sidecars |
| `min_artifact_mass` | per-stage | Minimum keys required for partial persist (`content_context`: thesis + topics) |

---

## `analysis.artifact_issue_triage`

Artifact Issue Triage & Remediation (ITR). Implemented in `artifact_issue_triage.py`, `artifact_auto_resolve.py`, `artifact_repairs.py`, `issue_severity_rules.py`.

| Key | Default | Purpose |
|-----|---------|---------|
| `enabled` | `true` | Master switch for triage pipeline |
| `auto_repair_minor` | `true` | Deterministic repairs for minor/null issues |
| `auto_repair_noise` | `true` | Enqueue noise issues as non-blocking investigations |
| `local_llm_for_important` | `true` | Local MLX option generation for important ambiguities |
| `pre_cross_validate_repair` | `true` | Triage before cross-artifact gates |
| `max_resolution_rounds` | `3` | Resolution loop iterations per stage |
| `max_operator_prompts_per_stage` | `8` | Cap LLM-generated option sets |
| `allow_promote_with_open_investigations` | `true` | Allow save when only non-blocking investigations remain |
| `allow_partial_then_repair` | `true` | Allow partial segment persist when post-repair reaches coverage |
| `revalidate_downstream_on_segment_fix` | `true` | Invalidate downstream summaries after manifest repair |
| `segment_overlap_policy` | `drop_duplicate_then_llm_pick` | Overlap handling strategy |
| `record_repairs_in_artifact_meta` | `true` | Append `_meta.repairs[]` audit entries |
| `max_upstream_reruns_per_run` | `2` | Cap operator-initiated upstream reruns per run |
| `max_downstream_auto_continue` | `1` | After upstream rerun completes, auto-continue downstream stages (max count) |
| `boundary_merge_threshold_ms` | `500` | Merge adjacent micro-boundaries below this span in `repair_boundaries` |
| `boundary_snap_tolerance_ms` | `500` | Snap nearby boundary timestamps when collating shards or repairing timeline drift |
| `boundary_coarse_partition_min_children` | `2` | Drop a coarse boundary when at least this many finer child boundaries cover its span |
| `boundary_coarse_coverage_ratio` | `0.85` | Minimum child coverage required before dropping a dominated coarse boundary |
| `require_propagation_before_segment_approve` | `true` | Block write approval when downstream cross-validate fails after segment/boundary fix |
| `upstream_rerun_invalidate_downstream` | `true` | Invalidate downstream summaries when upstream rerun is executed |
| `auto_resolve_min_confidence` | `0.70` | Minimum option confidence for Fix all auto-pick |
| `auto_resolve_confidence_gap` | `0.15` | Required gap between top two options for auto-pick |
| `auto_resolve_chain_downstream` | `true` | After boundary fix, chain `segment_classification` when safe |
| `max_auto_resolve_attempts_per_stage` | `2` | Cap Fix all attempts per stage (oscillation guard) |
| `auto_advance_after_itr_clear` | `false` (shipped default) | When `true`, Fix all / ITR clear may chain-execute the next stage; keep `false` with manual progression (`journey_ui.auto_advance_pipeline: false`) |
| `min_segments_after_auto_resolve` | `1` | Block destructive fix-all that empties manifest |
| `max_segments_deleted_per_fix_all` | `0.10` | Max fraction of segments deletable in one Fix all pass |
| `auto_resolve_max_issues_per_pass` | `50` | Cap issues processed per Fix all invocation |
| `in_run_auto_resolve` | `true` | Run auto-resolve inside stage finalize after each LLM stage |
| `risk_based_force_advance` | `true` | Classify open issues via `issue_risk_assessment.py` (delete vs repair vs pass) |
| `auto_resolve_in_run_mode` | `autopilot` | Mode string passed to in-run auto-resolve (`autopilot` or `manual`). Names a resolve strategy only — the autopilot journey runner was removed. |

---

## `analysis.llm_null_policy`

Explicit JSON `null` for unavailable optional fields — `null_field_policy.py`.

| Key | Default | Purpose |
|-----|---------|---------|
| `enabled` | `true` | Master switch for null acknowledgment and volley exclusion |
| `allow_unavailable_reason` | `true` | Prompt allows optional `_unavailable_reason` on null fields |
| `hard_stop_on_critical_null` | `true` | Block persist when critical fields are null |
| `exclude_from_volleys` | `true` | Omit null-acknowledged paths from shaped volley input |

---

## `analysis.holistic_fabrication` — removed

Global LLM-backed fallback for blocked stages. `holistic_fabrication.py` was deleted and v2 is fail-closed: a blocked stage hard-stops. The namespace no longer ships in [`config/app.defaults.json`](../../config/app.defaults.json). Per-field fabrication survives under [`analysis.llm_null_policy`](#analysisllm_null_policy) (`fabricate_*`, catalog id `OM-F01`).

| Removed key | Old default |
|-------------|-------------|
| `enabled` | `true` |
| `llm_enabled` | `true` |
| `deterministic_first` | `true` |
| `model_tier` | `economy` |
| `max_calls_per_stage_attempt` | `2` |
| `max_calls_per_run` | `24` |
| `override_arbiter_on_clear` | `true` |
| `allow_upstream_patches` | `true` |
| `stages` | `"*"` |

---

## `analysis.specialists.enabled`

When `true` (shipped default), runs economy-tier specialist passes after `missing_framing` (pre), `segment_classification`, `topic_coverage_audit`, and `full_master_ranking` (post); enqueues investigations when thresholds are met. Omit `pilot_stages` to run all mapped stages globally.

| Key | Default | Purpose |
|-----|---------|---------|
| `comprehension_risk_threshold` | `0.7` | Minimum `risk_score` from `comprehension_risk_blind` specialist before enqueueing a `comprehension_risk` investigation |

---

## `analysis.stt_lexicon_islands`

Soft prefer-include for mid-run STT weakness (domain lexicon, code-switch/Spanglish, passion). Systematic group evaluation before `full_master_ranking`; **boost-only** (never demotes other segments). Module: `stt_lexicon_islands.py`.

| Key | Default | Purpose |
|-----|---------|---------|
| `enabled` | `true` | Master switch for scan + pre-specialist + guards |
| `low_confidence_threshold` | `0.85` | Word confidence below this starts an island (falls back to `audio_probes.low_confidence_threshold`) |
| `high_confidence_threshold` | `0.9` | Pad words must meet this confidence |
| `min_pad_words` | `3` | Bilateral high-conf pad length for lexicon islands |
| `min_pad_words_passion` | `2` | Looser pad when passion/affect corroboration is present |
| `max_pad_gap_ms` | `600` | Max gap between pad words |
| `max_island_words` / `max_island_ms` | `12` / `8000` | Cap island size (avoid whole-turn boosts) |
| `max_candidates_for_llm` | `40` | Cost cap for `stt_lexicon_island_verify` |
| `verify_importance_min` | `0.65` | LLM `importance_score` floor before emitting a prior |
| `soft_boost_strength` | `0.15` | Scales importance into soft_boost |
| `auto_pack_protect_min_boost` | `0.65` | Importance floor for duration soft-protect |
| `passion_boost_multiplier` / `vernacular_boost_multiplier` | `1.25` | Multipliers when probe class matches |
| `sibling_cohesion_enabled` | `true` | Keep vernacular special siblings together when one is boosted |

Artifacts: `analysis/stt_lexicon_islands.json`, `analysis/stt_lexicon_island_boosts.json`. Specialist: OS-04 `stt_lexicon_island_verify` (pre on `full_master_ranking`).

---

## `analysis.prompt_examples`

Few-shot example injection into system prompts via `stages/llm_runner.py` → `load_compact_examples()`.

| Key | Default | Purpose |
|-----|---------|---------|
| `enabled` | `true` | Master switch; when `false`, no example packs appended |
| `mode` | `full` (shipped) | `compact` — per-stage char cap (`COMPACT_EXAMPLE_MAX_CHARS_BY_STAGE`); `full` — entire example `.md` file |
| `stages` | *(omit = built-in list)* | Optional allowlist; when set, only listed `stage_key`s receive examples |

**Built-in stages** (when `stages` omitted): all keys in `STAGE_EXAMPLE_FILES` — includes P0/P1 stages and sound-design packs when example files exist. Narrower runtime default than the full reference list in [prompts/README.md](../prompts/README.md).

**If wrong:** `compact` truncates mid-pattern → model misses bad-example guardrails; `full` on very long packs increases token cost but improves quality-first runs (shipped default). Unknown `mode` falls back to `compact`.

---

## Stage enrichment inputs (`stage_enrichment.py`)

Optional compact keys in shaped `stage_input` (when artifacts exist):

| Key | Stages | Source |
|-----|--------|--------|
| `pause_ladder_hints` | `boundary_detection` | Transcript word gaps |
| `emphasis_regions` | `topic_coverage_audit`, `narrative_arc_plan` | `source_acoustic_profile` + segments |
| `quotability_signals` | — | **Inert.** Consumed only by the removed `highlight_selection` (Flow 2) stage |
| `value_features_summary` | Delivery + boundary stages | `understanding/value_features.json` (opt-in extract) |
| `comprehension_risks` | `missing_framing` | Specialist pass output (when enabled) |

---

---

## `analysis.coverage_limits.*`

Unified ratio policy for analysis integrity vs delivery compression — `coverage_limits.py`.

| Key | Default | Used by | If wrong |
|-----|---------|---------|----------|
| `analysis_timeline_min_coverage_ratio` | `0.85` | boundary/spine lint | Analysis blind to tail of long interviews |
| `reanchor_min_coverage_ratio` | `0.55` | `content_brief_reanchor` lint | Re-anchor gate too strict/loose |
| `delivery_output_min_ratio_of_source` | `0.10` | delivery brief / QC | Master shorter than product floor |
| `delivery_output_ideal_ratio_of_source` | `0.65` | delivery brief | Ideal duration band misaligned |
| `delivery_output_max_ratio_of_source` | `1.5` | delivery brief / QC | Master longer than 1.5× source |
| `shard_target_duration_ms` | `120000` | shard planning | Shards too large/small for long interviews |
| `shard_batch_max_ratio` | `1.0` | collate/decompose | Late timeline segments dropped |
| `gap_fill_max_ratio` | `0.20` | micro-gap-fill | Over-synthetic remediation |
| `fabricate_max_ratio_per_call` | `0.20` | fabricate ladder | Too much invented content per attempt |
| `volley_spread_quartile_min_ratio` | `0.25` | volley compact | Head-biased context padding |
| `gap_evaluations_max_ratio` | `1.0` | gap volley | Gap pass drops tail segments |
| `gap_pass_segments_max_ratio` | `1.0` | gap volley | Gap segment sample head-only |
| `context_selector.enabled` | `false` | `context_selector.py` | Economy select path off (shadow log only) |

### `analysis.coverage_limits.soft_progression.*`

Non-blocking progression policy — soft caps, adaptive coverage floors, partial shard collate. Default **enabled**.

| Key | Default | Used by | If wrong |
|-----|---------|---------|----------|
| `enabled` | `true` | lint partition, mix/master gates | Coverage lint blocks long interviews again |
| `lint_coverage_floor_ratio` | `0.15` | `segment_coverage_ratio` lint | Below-floor coverage still hard-fails |
| `segment_coverage_adaptive` | `true` | moving min ratio by manifest size | Fixed 0.85 bar on long interviews |
| `shard_min_success_floor_ratio` | `0.35` | shard collate | Partial shard sets blocked |
| `pre_master_soft_fail` | `true` | `master_finalize` | Missing SFX/listen blocks export |

Non-blocking lint substrings (coverage, orphan refs, truncation decompose, confidence, post_listen, missing WAV) are logged as warnings and do not block arbiter accept or partial persist when `enabled` is true.

---

## `analysis.context_selector.*`

Economy catalog → select → hydrate for volley padding. Default **disabled** with shadow logging.

| Key | Default | If wrong |
|-----|---------|----------|
| `enabled` | `false` | Premature select without operator rollout |
| `shadow_log` | `true` | No visibility into would-be selection |
| `min_catalog_items` | `4` | Selector skipped on short manifests |

---

## `analysis.delivery_brief`

Deterministic adaptive soft targets after `optimal_questions` — [delivery-quality-preservation-matrix.md](./delivery-quality-preservation-matrix.md).

| Key | Default | Used by | If wrong |
|-----|---------|---------|----------|
| `enabled` | `true` | `delivery_brief_build` | No brief → delivery preflight fails when hardening on |
| `ideal_fraction_of_source` | `0.65` | duration band derivation | Episode ideal too short/long vs source |
| `min_ratio_of_source` | `0.10` | duration floor | Master may not compress below 10% without override |
| `max_ratio_of_source` | `1.5` | duration ceiling | Masters above 1.5× source blocked at ship |
| `min_duration_sec` | `600` | clamp | Floor too aggressive for short interviews |
| `max_duration_sec` | `7200` | clamp | Cap blocks long masters |
| `question_budget_max` | `0` (uncapped) | soft guidance only when >0 | Prefer `creative_delivery.listenability_guards` host_vo coverage ratios |
| `enforce_duration` | `true` | ranking cross-validate + post-master | Soft duration band becomes hard fail; selection below brief.min×`selection_brief_min_ratio` (default `0.65`), below source min-ratio, or above source max-ratio blocks ship |
| `selection_brief_min_ratio` | `0.65` | `selection_duration_ship_ok`, ranking cross-validate, nugget_retention scoring | Hard ship envelope vs `brief.target_duration_sec.min` |

---

## `creative_delivery.listenability_guards`

Percentage-band QC for conversation, beds, stingers, and intentional air. **No numbered hard caps** on interviewer lines / beds / stingers.

| Key | Default | Meaning |
|-----|---------|---------|
| `host_vo_coverage_min_ratio` / `max` | `0.12` / `0.85` | Share of selected segments near a host VO/transition |
| `host_vo_duration_min_ratio` / `max` | `0.04` / `0.45` | Host vs total speech duration |
| `host_vo_quartile_presence_min_ratio` | `0.5` | Quartiles with host presence |
| `bed_coverage_min_ratio` / `max` | `0.40` / `0.85` | Selection duration under beds |
| `bed_quartile_presence_min_ratio` | `0.5` | Quartiles with a bed |
| `hinge_stinger_coverage_min_ratio` / `max` | `0.3` / `1.0` | Chapter/topic hinges with punctuator |
| `intentional_air_min_ratio` / `max` | `0.01` / `0.12` | Explicit silence pads in EDL |
| `gap_eval_scored_min_ratio` | `0.95` | Scored gap evaluations completeness |
| `fail_closed` | `true` | Mix raises on listenability fail |

`bed_coverage_max_ratio` (`0.85`) and `hinge_stinger_coverage_min_ratio` (`0.3`) — bed-heavy passages (up to ~85%) and lighter hinge-punctuation density are both legitimate; the old `0.55` ceiling under-filled mid/late show.

**Retention / pack-to-target policy:** product **soft ideal** is **~65% of source** for the final master (selection is the pre-mix proxy). Prefer **shorter / more concise** than padding — do not fill toward the ceiling. Trims are a **soft pack toward the brief's `ideal`** duration (`analysis.delivery_brief.ideal_fraction_of_source`, default `0.65` of source), not a hard floor. The hard floor is `analysis.delivery_brief.min_ratio_of_source` (**`0.10`** — catastrophe net only). The hard ceiling is `analysis.delivery_brief.max_ratio_of_source` (**`1.5`** / 150% of source — VO/music may expand past source, never a target). There is no separate `0.35` floor or `0.80×ideal` hard floor anywhere in the pack path. `selection_auto_pack.pack_selection_to_duration` is the single shared packer behind both `auto_pack_selection_to_brief` (hard-budget safety net → brief `max`, first_try mode only) and `creative_delivery.enforce_creative_selection_edit` (editorial soft-pack → brief `trim_target`, default `ideal`) — a selection already within budget is left untouched (no forced minimum-trim "theater" on top of an already-tight pack), and when segments must be dropped both paths prefer dropping mid-monologue segments (same speaker before/after) before touching segments that anchor a speaker volley, with rank as the tiebreaker.

**Seam glue (code constants in `seam_glue.py`, not config):** reorder bridges rebuild from the EDL air order; `|source_gap_ms| ≥ 60000` or `chapter_jump` → chapter-scale spoken hinge (`type: chapter`). Any gap `placement: before` VO on `before_segment_id` covers the pair — do not mint a second spoken host turn. Fallback hinge text from `seam_glue.default_bridge_text` invites the next beat **without embedding the native excerpt**. `bridge_completeness.stub_reorder_bridges` flags known generic stub phrases **and** verbatim text reused across ≥3 distinct seam pairs; `assert_bridges_complete` **blocks** on stubs (not advisory-only). Artifact: `master/assembly_ledger.json`.

## `audio_preclean`

| Key | Default | Meaning |
|-----|---------|---------|
| `default_action` | `run` | Accept DeepFilterNet unless operator dismisses |
| `auto_run_before_ingest` | `true` | Enable full_source scope by default |

TBIY runs (legacy) additionally refresh `flow_adaptation.tbiy_conformance` during `delivery_brief_build` and may copy `five_act_mode` / `moat_mode` / `vo_bridge_priority` onto the brief. Strategy authority is now the [Mastering Process](./mastering-process.md); conformance is descriptive pending cutover ([mastering-integration-backlog.md](./mastering-integration-backlog.md)).

---

## `mastering`

Canon: [mastering-process.md](./mastering-process.md) · Hardening: [mastering-quality-hardening.md](./mastering-quality-hardening.md).

### Gate modes

Every `*.mode` key takes `off` | `advisory` | `authoritative`.

| Mode | Behavior |
|------|----------|
| `off` | Gate never runs; no artifact written |
| `advisory` | Gate runs and writes its artifact, but never blocks (**default everywhere**) |
| `authoritative` | Hard failures block the run |

Flip gates to `authoritative` one at a time, after the [eval corpus](./mastering-eval-corpus.md) shows no regressions.

### Keys

| Key | Default | Used by | If wrong |
|-----|---------|---------|----------|
| `mastering.prompt_edit.allow_global_promotion` | `false` | Shape Engine prompt edit loop | When `true`, a run-local prompt edit can become a global seed and silently degrade other source types |
| `mastering.prompt_edit.require_operator_approval` | `true` | Promotion gate | When `false`, corpus pass alone promotes a prompt |
| `mastering.shape.soft_gate.enable` | `true` | Two-pass Shape stages (`mastering_shape_runtime.py`) | `false` skips agenda/candidates/plan synthesis entirely — forced-sparse plan only |
| `mastering.shape.soft_gate.mode` | `advisory` | Shape soft-gate blocking behavior | Reserved for future authoritative flip; no code currently branches on non-`advisory` values |
| `mastering.shape.soft_gate.shadow_compare` | `true` | Writes `mastering/shadow_diff.json` comparing plan vs legacy structure proxy | `false` skips the observability diff — no behavior change |
| `mastering.shape.soft_gate.consumers_bind` | `false` | Global switch downstream consumers would check before trusting Shape's emitted order | **Plan 6:** stays `false` until the [Shape mutation engine](./mastering-shape-engine.md#shape-as-mutation-engine) runs its full loop (capability mutations → critics → auditions → Pareto → hard delight) end-to-end and `shape_order_bind.resolve_air_order` has shadow-compare evidence across a corpus. Per-run hybrid bind (`resolve_air_order`) already prefers Shape order when the plan is complete and `story_health` passes — this flag does not gate that; see `mastering-integration-backlog.md` H7 |
| `mastering.shape.soft_gate.two_pass` | `true` | Pass1 provisional (pre-`missing_framing`) + Pass2 confirm (post-gap-eval) | `false` unused by current runtime; two-pass is the only shipped path |
| `mastering.shape.information_packages.enable` | `true` | Mid-episode information package planner | `false` skips packages; does **not** disable episode_close |
| `mastering.shape.information_packages.mode` | `commit_music_vo` | `shadow` / `commit_music_vo` / `commit_with_regroup` | **Full-auto default** commits packages onto `mastering_plan` (not shadow); shadow audits only; `commit_with_regroup` needs `allow_regroup` |
| `mastering.shape.information_packages.max_per_episode` | `2` | Hard cap on committed packages | — |
| `mastering.shape.information_packages.allow_regroup` | `false` | Phase-2 kept-native regroup | Keep false until order preflight tests pass |
| `mastering.shape.episode_close.require_music` | `true` | Always seed `theme_outro` after last native | Independent of package mode |
| `mastering.shape.episode_close.fade_out_ms` | `2200` | Gentle long outro fade | Mix also floors via `bookend_fade_out_ms` |
| `mastering.air_script.enable` | `true` | Pass A/B air-script on `mastering_plan` (`air_script_compose`, `air_script_seams`) | `false` skips both stages — EDL falls back to concatenating approved parts |
| `mastering.air_script.fail_open` | `true` | Compose exceptions log and continue | `false` raises so a broken paper-edit cannot silently concat |
| `mastering.air_script.bed_coverage_aim_lo` | `0.40` | Low end of underbed aim (Shape band ~40–85%) | Dry exceptions (skip-underscore / overlap) ignore this |
| `mastering.air_script.bed_coverage_aim_hi` | `0.85` | High end of underbed aim | Compose hunts scene beds rather than every-Nth wallpaper |
| `mastering.air_order_integrity.opening_window_ms` | `180000` | Source-tape window treated as opening (static fallback) | Segments with earlier `start_ms` subject to opening policy |
| `mastering.air_order_integrity.opening_window_ratio` | `0.05` | Scale opening window as `ceil(duration * ratio)` | Clamped by min/max below |
| `mastering.air_order_integrity.opening_window_min_ms` | `90000` | Floor for scaled opening window | — |
| `mastering.air_order_integrity.opening_window_max_ms` | `300000` | Ceiling for scaled opening window | — |
| `mastering.air_order_integrity.opening_air_slots` | `6` | Max opening **families** on air when host-first (with `count_opening_by_family`) | Guest-first runs use stricter index-0 rule |
| `mastering.air_order_integrity.opening_air_slots_min` | `4` | Floor for slot cap | — |
| `mastering.air_order_integrity.opening_air_slots_max` | `12` | Ceiling for slot cap | — |
| `mastering.air_order_integrity.opening_body_start_index` | `3` | Body-started threshold for transition/PMQ guards | — |
| `mastering.air_order_integrity.opening_body_start_index_min` | `2` | Floor for body-start index | — |
| `mastering.air_order_integrity.opening_body_start_index_max` | `6` | Ceiling for body-start index | — |
| `mastering.air_order_integrity.opening_body_start_index_long_tier_bump` | `0` | Added to body-start on long tier interviews | — |
| `mastering.air_order_integrity.reverse_jump_margin_ms` | `300000` | Min backward source gap to flag reverse jump (static fallback) | — |
| `mastering.air_order_integrity.reverse_jump_margin_ratio` | `0.083` | Scale reverse-jump margin as `ceil(duration * ratio)` | Clamped by min/max below |
| `mastering.air_order_integrity.reverse_jump_margin_min_ms` | `120000` | Floor for scaled reverse-jump margin | — |
| `mastering.air_order_integrity.reverse_jump_margin_max_ms` | `600000` | Ceiling for scaled reverse-jump margin | — |
| `mastering.air_order_integrity.count_opening_by_family` | `true` | Letter-split siblings count as one opening family for slot budget | `false` restores per-fragment counting |
| `mastering.air_order_integrity.fragmentation_extra_slots` | `2` | Extra slot budget when opening fragments exceed families | Junction letter-split blowups |
| `mastering.air_order_integrity.block_ranking_on_critical` | `false` | Halt ranking/transitions commit on critical integrity (after repair) | `true` after boundary-bus soak |
| `mastering.air_order_integrity.block_publish_on_critical` | `true` | PMQ backstop on unresolved critical integrity | — |
| `mastering.air_order_integrity.invalidate_edl_on_order_change` | `true` | Clear `.stage_done/edl` when selection order changes post-transitions | — |
| `mastering.air_order_integrity.invalidate_transitions_on_order_change` | `true` | Clear transitions + prune on order change | — |
| `mastering.air_order_integrity.invalidate_mix_on_order_change` | `false` | Clear `.stage_done/mix` when EDL invalidated and assembly stale | Opt-in; default off |
| `mastering.edl.clone_adjacency_verify` | `true` | EDL clone-adjacency suppress | `false` restores ID-only suppress (no same-person listen) |
| `mastering.edl.clone_adjacency_verify_clip_ms` | `4000` | Tape window paired with the clone sample | Too short starves Sortformer; too long mixes in the other speaker |
| `mastering.media_ip_cta.prune_max_depth` | `2` | `media_ip_cta` still-mixed re-split only | Higher than 2 re-peels leftover mixed children |
| `mastering.media_ip_cta.prune_max_children` | `12` | Max N-way children per original CTA parent | — |
| `mastering.media_ip_cta.min_child_ms` | `1500` | Floor for a peelable complete-thought child | Too low keeps dirty fragments |
| `mastering.media_ip_cta.prune_max_seed_passes` | `3` | Tape-level rescan after admitting clean children | — |
| `mastering.research.routing.mode` | `advisory` | `mastering_research_router` | `authoritative` lets routing actually skip fields |
| `mastering.research.routing.default_disposition` | `required` | Router fallback for unrouted fields | `skip` would silently drop analysis |
| `mastering.research.routing.max_deep_fields` | `12` | Router budget | Too high dilutes context; too low starves decisive fields |
| `mastering.timeline_optimizer.enabled` | `true` | Endless mid-mix daemon after mix | `false` skips open-ended search |
| `mastering.timeline_optimizer.mode` | `endless_daemon` | Search lifecycle | Other modes unused; keep endless for defaults |
| `mastering.timeline_optimizer.mutation_surface` | `maximum` | structure+glue+SDP+LLM | Narrower surfaces not plumbed yet |
| `mastering.timeline_optimizer.auto_start_after_mix` | `true` | Full-auto path | `false` requires GUI Keep optimizing |
| `mastering.timeline_optimizer.auto_promote_remaster` | `true` | Rebuild EDL and mix whenever plateau promotion changes order | `false` leaves promoted artifacts ahead of audible output |
| `mastering.timeline_optimizer.always_auto_apply_best` | `true` | Forces synchronous take-best remaster in the daemon | `false` permits deferred audible application |
| `mastering.timeline_optimizer.use_llm_proposer` | `true` | Periodic flagship mutation proposals | `false` = heuristics only |
| `mastering.timeline_optimizer.block_finalize_until_take_or_skip` | `true` | Finalize waits for optimizer authority | `false` permits finalize before take/skip |
| `mastering.quality_hardening.enabled` | `true` | Master switch for all gates below | `false` disables the whole layer regardless of per-gate modes |
| `mastering.quality_hardening.context.mode` | `advisory` | `mastering_context_compiler` | `authoritative` enforces token budgets on every consumer |
| `mastering.quality_hardening.context.default_max_tokens` | `24000` | Evidence packet budget | Too small truncates decisive evidence; too large overflows models |
| `mastering.quality_hardening.context.truncation_policy` | `drop_lowest_salience` | Packet overflow handling (`drop_lowest_salience` / `summarize` / `pointer_only`) | Wrong policy drops the wrong evidence |
| `mastering.quality_hardening.context.inline_max_chars` | `4000` | Materialize-vs-pointer threshold | Large values inline whole artifacts |
| `mastering.quality_hardening.diversity.mode` | `advisory` | `mastering_diversity` | `authoritative` forces remint of near-clone candidates |
| `mastering.quality_hardening.diversity.min_pairwise_distance` | `0.35` | Diversity threshold (0–1) | Too high causes endless reminting; too low permits clones |
| `mastering.quality_hardening.diversity.max_remint_rounds` | `1` | Remint budget | Unbounded reminting burns the agenda budget |
| `mastering.quality_hardening.feasibility.mode` | `authoritative` | `mastering_feasibility` | `authoritative` blocks unbuildable candidates before auditions/synthesize |
| `mastering.quality_hardening.feasibility.duration_slack_pct` | `0.15` | `duration_fits` tolerance | Too tight rejects workable plans |
| `mastering.quality_hardening.semantic_integrity.mode` | `authoritative` | `mastering_semantic_integrity` | `authoritative` blocks critical fabrication findings |
| `mastering.quality_hardening.semantic_integrity.adjacency_max_turns` | `3` | `false_reaction_adjacency` window | Too wide misses fabricated reactions |
| `mastering.quality_hardening.semantic_integrity.llm_confirm` | `true` | LLM confirm pass over deterministic flags | `false` keeps deterministic flags unconfirmed (more false positives) |
| `mastering.quality_hardening.voice_clone.mode` | `advisory` | Clone consent gate | `authoritative` hard-fails `vo_clone_*` without consent. **Never** relaxes the guest-clone ban |
| `mastering.quality_hardening.voice_clone.default_scopes` | `[]` | Scopes granted without explicit operator choice | Non-empty grants clone use the operator never approved |
| `mastering.quality_hardening.voice_clone.require_disclosure` | `false` | Forces a non-`none` disclosure | `true` blocks runs that chose no disclosure |
| `mastering.quality_hardening.rubric.mode` | `advisory` | L0 per-run `eval_rubric.json` | `authoritative` requires a rubric before critics run |
| `mastering.quality_hardening.auditions.mode` | `advisory` | Micro-render audition loop | `authoritative` requires rendered auditions before L4 |
| `mastering.quality_hardening.auditions.max_auditions` | `3` | Candidates rendered | Higher costs render time per run |
| `mastering.quality_hardening.auditions.window_ms` | `{opening: 20000, hinge: 20000, dense: 30000}` | Audition window lengths | Windows too short lose listener context |
| `mastering.quality_hardening.auditions.total_max_ms` | `90000` | Hard ceiling per audition | Above this, auditions stop being cheap |
| `mastering.quality_hardening.critics.mode` | `advisory` | L4 panel | `authoritative` requires the full panel before Pareto |
| `mastering.quality_hardening.critics.enabled_critics` | all six | Which critics run | Dropping `integrity` removes the only hard-fail critic |
| `mastering.quality_hardening.critics.max_deepen_rounds` | `1` | Arbiter deepen directives | Unbounded deepening burns flagship budget |
| `mastering.quality_hardening.pareto.mode` | `advisory` | `mastering_pareto` | `authoritative` restricts synthesize to frontier survivors |
| `mastering.quality_hardening.pareto.min_frontier_size` | `1` | Frontier floor | `0` can starve synthesize of inputs |
| `mastering.quality_hardening.polish.mode` | `advisory` | Closed-loop polish | `authoritative` blocks `master_finalize` on a failing audit |
| `mastering.quality_hardening.polish.audio_grounded` | `true` | Audit scores rendered audio, not plan text | `false` reverts to the weaker text-only audit |
| `mastering.quality_hardening.polish.max_remux_rounds` | `2` | Bounded remux budget | `0` disables repair; high values loop on marginal issues |
| `mastering.chapter_close_hitch.enabled` | `true` | One-shot `chapter_close_hitch` after the first `narrative_arc_plan` | `false` writes a committed skip latch and leaves first-pass cuts |
| `mastering.chapter_close_hitch.max_cut_ms` | `180000` | Ceiling on last-listen-complete search from a keeper open | Too small chops chapter/TP closes; too large can wander |
| `mastering.chapter_close_hitch.next_keeper_eps_ms` | `80` | Interior keepers stop this far before the next keeper start | `0` can swallow the next keeper |
| `mastering.junction_snip_qa.mode` | `advisory` | `junction_snip_qa` stage (`off` / `advisory` / `authoritative`) | **Dual meaning (JSQ-B1):** default label is `advisory`, but critical incomplete-cut residuals (`on_a_roll` / `incomplete_clause` / `chapter_bleed_incomplete`) always hard-block regardless of mode (EM8). `authoritative` additionally blocks other residual families; `off` skips the stage |
| `mastering.junction_snip_qa.micro_nudge_ms` | `2500` | Energy/word micro search window (scaled by pace) | Too small misses valleys; too large over-trims |
| `mastering.junction_snip_qa.phrase_extend_max_ms` | `24000` | Max phrase-complete extend/cut for on-a-roll | Caps continuum search; unresolved critical clauses hard-stop after two runs |
| `mastering.junction_snip_qa.impact_hold_ms_min` / `max` | `1200` / `3500` | Music-only sit after impact native close | Scaled by pace class |
| `mastering.junction_snip_qa.feel_audit_enabled` | `true` | One OH-J1 feel LLM after deterministic repairs | `false` skips LLM entirely |
| `mastering.junction_snip_qa.thought_complete_llm_enabled` | `true` | One batched OH-J2 recut LLM for hanging native ends | `false` uses transcript-only complete-thought cuts |
| `mastering.junction_snip_qa.thought_complete_max_segments` | `4` | Max following same-speaker clips to traverse | Caps lookahead; does not absorb whole sections |
| `mastering.junction_snip_qa.thought_complete_max_ms` | `24000` | Max source-ms after a hanging end to search | Independent of in-clip `phrase_extend_max_ms` |
| `mastering.junction_snip_qa.max_remaster_rounds` | `2` | Cap remasters (deterministic + feel) | Hard ceiling 2; also gated by `JUNCTION_REMASTER_GEN_CAP=3` per seating generation + sticky `oscillation_halt` in `operator/junction_remaster_budget.json` |
| `mastering.junction_snip_qa.apply_repairs` | `true` | Apply NLE/EDL/placement repairs | `false` detect-only |
| `mastering.junction_snip_qa.music_soft_crossfade_ms` | `180` | Suggested bed/theme crossfade when hard | Transition-only — never recreates stems |
| `mastering.junction_snip_qa.dead_air_clamp_ms` | `2500` | Clamp non-impact silence pads | Does not steal `impact_hold` |
| `mastering.junction_snip_qa.pace_multipliers` | sparse/fireside/… | Scales holds/nudges per source pace | Keeps policy dynamic across source types |
| `mastering.post_master_quality.never_skip` | `true` | Always write post-master quality after finalize | `false` would skip publish gate |
| `mastering.post_master_quality.block_publish` | `false` | `require_publishable` / finalize | `true` blocks publish on rubric PMQ failures |
| `mastering.post_master_quality.block_on_feel_unavailable` | `true` | Fail publish when feel audit verdict is unavailable after retry | `false` ignores missing feel judgment |
| `mastering.post_master_quality.overall_min` | `0.90` | Listener scorecard overall floor | Lower allows weaker masters to publish |
| `mastering.post_master_quality.dimension_floors.*` | flow/clarity/music/native `0.90`; synthetic_fit `0.85` | Per-dimension publish floors | Missing floors skip that dimension |
| `mastering.listen_delight.mode` | `authoritative` | `listen_delight.run_listen_delight_audit` (`off` / `advisory` / `authoritative`) | Default hard-blocks ship after ≤N remutate attempts (`max_remutate_attempts`); set `advisory` to soft-ship with PMQ advisories |
| `mastering.listen_delight.max_remutate_attempts` | `3` | `listen_delight_remutate.max_remutate_attempts` / recovery budget for `listen_delight_floors` | Cap remutate cycles then ship-best (aspirational) or refuse — no infinite thrash |
| `mastering.aspirational_quality.enabled` | `true` | Rubric gates (delight, PMQ scorecard, listenability, junction feel) | `false` restores authoritative blocking on rubrics |
| `mastering.aspirational_quality.max_attempts_per_family` | `3` | Remutate / heal budget per rubric family before pick-best | Lower = faster fallback to best candidate |
| `mastering.aspirational_quality.always_produce_master` | `true` | `master_finalize` completes with best structurally sound candidate | `false` not recommended |
| `mastering.aspirational_quality.require_operator_publish_when_advisory` | `true` | `podcast_publish` / S3 when `quality_advisories` present | `false` allows unattended RSS with advisories |
| `mastering.aspirational_quality.catastrophic_floors.*` | cut_integrity `0.55`; listen_delight_overall `0.50` | Hard stop even under aspirational policy | Below these = no master |
| `mastering.aspirational_quality.pick_best_weights.*` | delight overall `0.5`; cut_integrity `0.2`; … | `select_best_quality_candidate` ranking | Tune pick-best tie-breaks |
| `mastering.progress_floors.enabled` | `true` | Count/score floors as goals: stretch then advisory-continue; playability-only hard stops | `false` restores legacy fail-closed floors |
| `mastering.progress_floors.max_attempts_per_family` | `3` | Best-of-N / remutate budget for floor families | Lower = faster advisory-continue |
| `mastering.progress_floors.record_advisories` | `true` | Write `run_meta.floor_advisories` (+ mirror `quality_advisories`) | `false` skips advisory stamps |
| `mastering.progress_floors.require_operator_publish_when_advisory` | `true` | G-Publish / S3 when floor or quality advisories present | `false` allows unattended sync with advisories |
| `mastering.progress_floors.hosted_vo.aspirational` | `true` | Hosted VO count floor (`min_synthetic_vo_lines`) **PARTIAL** miss (`have≥1`) → revive discarded then advisory. **Never** covers **HOLLOW_ZERO** (`have==0`; playability stop). SSOT labels via `identify_hosted_vo_floor`: `UNWARRANTED` \| `WAIVED` \| `MET` \| `PARTIAL` \| `HOLLOW_ZERO` — see [hosted-vo-authority.md](hosted-vo-authority.md) | `false` keeps `hosted_vo_floor_unsatisfiable` halt on PARTIAL too |
| `mastering.progress_floors.layup_coverage.aspirational` | `true` | `min_layup_coverage` miss → advisory | `false` hard QC error |
| `mastering.progress_floors.nugget_air.aspirational` | `true` | Aligns with `air_coverage_aspirational` SSOT advisories | `false` does not override layup hard path alone |
| `mastering.progress_floors.listenability.aspirational` | `true` | Listenability coverage bands → advisory after remux budget | `false` fail-closed bands |
| `mastering.progress_floors.soundscape_density.aspirational` | `true` | Bed/SFX density miss after remux → advisory continue mix | `false` palette/mix refuse |
| `mastering.progress_floors.listen_delight.aspirational` | `true` | Delight/PMQ score floors → remutate then ship-best | `false` defer to `aspirational_quality` alone |
| `mastering.progress_floors.listen_delight.catastrophic_as_advisory` | `true` | Former catastrophic score floors become loud advisory + ship-best | `false` restores catastrophic hard-stop |
| `mastering.progress_floors.boundary_coverage.aspirational` | `true` | Boundary timeline coverage lint → soft advisory | `false` blocking lint |
| `mastering.listen_delight.overall_min` | `0.90` | Mean of the eight delight dimensions | Lower allows a weaker overall listen to ship |
| `mastering.listen_delight.dimension_floors.*` | nugget_retention `0.80`; cut_integrity `0.85`; conversation_fit `0.85`; sonic_weave `0.85`; mode_coherence `0.80`; finishability `0.80`; recommendability `0.75`; story_followability `0.85` | Per-dimension ship floors | Missing floors skip that dimension. `story_followability` defaults high when `air_script` is absent |
| `mastering.listen_delight.require_mode_consistency` | `true` | Gates `mode_coherence`/`finishability`/`recommendability` on `mode_consistency_report.ok` | `false` treats mode consistency as always-ok (softer scores) |
| `mastering.listen_delight.fail_early_at_audit_stage` | `false` | **Deprecated/ignored** — pre-mix is always advisory | Ship gate remains `master_finalize` post_master re-score; remutate APPLY via recovery playbook |
| `mastering.homunculus.default_version` | `latest` | Start-tab / create-run default brain. `latest` = highest registered (currently `0.2.0`). Pin `0.0.0` for the original walk. | Unknown versions refuse to start |
| `mastering.homunculus.mode` | `authoritative` | Homunculus gate-mode flag (`advisory` debug) | Unused on 0.0.0 |
| `mastering.homunculus.conductor_model` | `gpt-4o` | OpenAI model for nested homunculus LLM gateway calls | Nested stage LLMs still use the model registry |
| `mastering.homunculus.limits.max_invokes_per_identity` | `3` | Hard cap per stage/function (repack counts) | Cannot raise |
| `mastering.homunculus.limits.max_problem_analyses_per_issue` | `1` | One analysis per `(kind, implicated, speaker_id)` signature | A different speaker or style tag is a new signature |
| `mastering.homunculus.limits.max_complete_masters` | `3` | First master + at most two rebuilds | Halt `limit_exhausted` |
| `mastering.homunculus.limits.max_mix_cycles` | `3` | Mix / master_finalize cap | Halt `limit_exhausted` |
| `mastering.homunculus.limits.max_conductor_turns` | `198` | Legacy budget key retained for 0.1.0 resume only | Unused on 0.2.0 seed walk |
| `mastering.synthetic_framing.allow_canned_bridge_fallback` | `false` | `seam_glue.mint_missing_transitions`, `synthetic_framing.validate_synthetic_plan` | `true` mints marked `auto_minted` canned bridges and skips validate hard-stop for uncovered reorder seams; default loud-fails |
| `mastering.music_continuity.prefer_contiguous_beds` | `true` | `sound_design.py` contiguous under_segment merge; `seam_autopsy.score_seam` `continue_bed` hint | `false` forces per-segment hard fades / seam-level bed restarts instead of scene beds |
| `mastering.music_continuity.scene_crossfade_ms` | `1800` | Contiguous bed XF floor | Too short → scene seams click |
| `mastering.music_continuity.require_true_bookend_anchors` | `true` | Junction music detector | Escalates cold-open/outro missing XF severity |

---

## `analysis.prompt_thresholds.*`

Injected into prompts / STT prep; changing them changes **editorial behavior**, not just formatting.

| Key | If wrong |
|-----|----------|
| `pause_split_ms` | Too small → fragment boundaries; too large → merges distinct ideas (default **1000** — do not split mid-sentence on 400 ms breaths) |
| `short_question_max_words` | Mis-splits Q+A pairs in boundary prompt |
| `interviewer_question_max_words` / `interviewer_setup_max_words` | VO lines too long for product spec |
| `highlight_setup_max_sec` | **Inert** — Flow 2 removed |
| `max_chapters` | Narrative plan violates cap → validation / model confusion |
| `max_highlight_clips` | **Inert** — Flow 2 removed |
| `content_context_topic_anchor_min_duration_ms` | Medium+ interviews require topic evidence anchors in `content_context` lint (default `900000`, 15m) |
| `show_description_min_words` / `show_description_max_words` / `show_description_target_words` | **Inert** — Flow 3 publishing removed; no stage writes `show_notes/show_description.json` |

---

## `_comment`

Documentation only — not read by code.

---

## `secrets.env` keys (merged, not in `app.defaults.json`)

Loaded by `load_secrets()` / `merged_config()`. **Never commit** real values.

| Key | Effect if wrong / missing |
|-----|---------------------------|
| `INPUT_AUDIO_PATH` | Overrides `input_audio_path` for CLI default — optional when using GUI + `exec_*` run ids |
| `OPENAI_API_KEY` | LLM stages fail at runtime |
| `OPENAI_MODEL` | Fallback when `models.<stage>` missing |
| `OPENAI_SPEECH_MODEL` | Reserved for future OpenAI audio adapters |
| `AWS_DEFAULT_REGION` / `AWS_REGION` | Fallback region if `podcast.aws_region` unset; also used by `scripts/tf-*.sh` |
| `AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY` / `AWS_SESSION_TOKEN` / `AWS_PROFILE` | Auth for **Terraform** wrappers and **boto3** publish/seed/invalidate — not AWS CLI |
| `PODCAST_CLOUDFRONT_DISTRIBUTION_ID` | Invalidation target after `feed.xml` upload (synced by `sync_podcast_tf_secrets.sh` / `tf-rotate-cloudfront-url.sh`) |
| `PODCAST_FEED_BASE_URL` | Public CloudFront base (synced by apply / CloudFront rotate). App + `invalidate_podcast_cf.sh` always re-read this. Whenever this URL (or `{base}/feed.xml`) is printed, scripts also print the Apple Podcasts Connect pass-through (`new-feed?submitfeed=`). |
| `PODCAST_S3_BUCKET` | Optional override only — prefer `podcast.s3_bucket` in `app.defaults.json` |
| `CURSOR_API_KEY` | Cursor SDK agent runs (`config.cursor_api_key()`) — set in `config/secrets/secrets.env` or env |
| `HF_TOKEN` / `HUGGING_FACE_HUB_TOKEN` | Hugging Face Hub auth for public model downloads (CLAP / local stacks). Free account token is enough. Injected into local runtime subprocesses via `local_runtime` / `huggingface_hub_token()` |

Optional placeholders in `config/templates/secrets.env.example` (AssemblyAI, Deepgram, etc.) are **not wired** until an adapter exists — document when adding code.

---

## `podcast`

Shared pipeline defaults in `config/app.defaults.json`. Per-show identity and AWS destinations live in [`config/podcast/catalog.json`](../../config/podcast/catalog.json) (Start picker source of truth). See [podcast-rss-hosting.md](./podcast-rss-hosting.md).

| Key | Default | Role |
|-----|---------|------|
| `enabled` | `true` | Gates G-Publish |
| `cover_image.*` | OpenAI cascade | Shared cover pipeline; `style_reference.path` overlays the selected show's artwork |
| `mp3_bitrate_k` | `192` | Encode bitrate |
| `mp3_channels` | `2` | Stereo enclosure |
| `enclosure_type` | `audio/mpeg` | Documented enclosure MIME |
| `upload_master_wav` | `true` | Always upload `master.wav` beside MP3 |
| `s3.show_artwork_key` | `artwork.jpg` | Show art object name |
| `s3.episodes_prefix` / `s3.episode_files.*` | `episodes` / file names | S3 key layout per episode (`cover.jpg`, …) |

Catalog show fields (not in `app.defaults`): `id`, `title`, `artwork_path`, `cover_theme_path`, `project_name`, `s3_bucket`, `aws_region`, `cloudfront_distribution_id`, `feed_base_url`, channel identity (`show_author`, `podcast_guid`, …). Default show id: `zero_shot_podcast_demo`.

Cover cascade details: [podcast-cover-theme.md](./podcast-cover-theme.md). Flagship chat stages: `models.stages.episode_meta_build`, `episode_cover_prompt_craft`, `episode_cover_vision_pick`.

---

## `local_image`

Retired for War Room episode covers (OpenAI Images only). Keys retained so orphan tooling does not crash.

| Key | Default | Role |
|-----|---------|------|
| `enabled` | `false` | Do not bootstrap/use MLX T2I for episode covers |
| `models_dir` | `ASSETS/local_image/models` | Legacy model cache path |
| `fail_open` | `true` | Legacy |
| `request_timeout_sec` | `600` | Legacy |

---

## `musicgen`

Local MusicGen fixed palette stems for creative-delivery `theme_*` / palette kinds. See [local-audio-stack.md](./local-audio-stack.md).

| Key | Default | Role |
|-----|---------|------|
| `enabled` | `true` | Theme bed generation |
| `model_id` | `facebook/musicgen-large` | Primary MusicGen model (ladder starts here: large→medium→small) |
| `melody_model_id` | `facebook/musicgen-melody-large` | Melody-conditioned model when motif/cold-open WAV exists |
| `device` | `auto` | `auto` / `cpu` / `mlx` / `mps` / `cuda`. **`auto` prefers GPU**: MPS on Apple Silicon when available, else CUDA, else CPU. After a Metal abort, `ban_mps_on_abort` forces CPU for the rest of the run. Set `cpu` to force CPU-only. |
| `ban_mps_on_abort` | `true` | After SIGABRT, ban MPS for the rest of the run and retry once on CPU |
| `request_timeout_sec` | `900` | Hang budget for primary attempt on GPU (large needs several minutes per stem) |
| `cpu_request_timeout_sec` | `300` | Tighter hang budget when resolved device is CPU (step down instead of thrash) |
| `max_request_timeout_sec` | `2400` | Hard cap for duration-scaled hang budgets |
| `step_down_timeout_sec` | `480` | Hang budget for medium/small ladder steps |
| `step_down_duration_ratio` | `0.85` | Shorten clip duration on each ladder step-down |
| `pause_between_ladder_steps_sec` | `0` | Optional extra pause between ladder rungs (abort backoff handles kills) |
| `fail_closed_on_stub` | `true` | Production fail-closed: no underscore musical stub after ladder; omit bed + rewrite cues instead |
| `fail_closed_on_stub_roles` | `theme_cold_open`, `theme_outro` | QA fails (and stub is blocked) for these roles regardless of global `fail_closed_on_stub` |
| `keep_prior_stem_on_fail` | `true` | On regen failure, restore `.prior.bak` instead of overwriting a good stem |
| `skip_cold_open_on_total_failure` | `true` | After all fallbacks fail, emit silence for cold-open rather than stub |
| `best_of_n_speech_free` | `2` | Candidates for cold open / outro / accents |
| `best_of_n_underscore` | `3` | Candidates generated for each underscore loop before automatic musical/loop-safe selection |
| `max_best_of_n` | `3` | Hard cap (also clamps production-profile overrides) |
| `use_melody_conditioning` | `false` | Condition later stems on motif/cold-open melody |
| `prefer_medium_on_cpu` | `false` | When true, skip large on CPU if medium is cached — **default off** (large first) |
| `mmaudio_backup_on_stub` | `false` | After MusicGen ladder ends in a stub, try MMAudio (legacy/non-creative only). Creative theme path omits instead (MU7). |
| `min_duration_sec` | `4.0` | Soft floor only |
| `max_duration_sec` | `24.0` | Soft advisory only — not enforced as a hard ceiling in `clamp_music_duration` |
| `prefetch_models` | large + melody-large + medium + small | Bootstrap cache list |
| `keep_candidates` | `true` | Keep best-of-N WAVs under `_candidates/` through mix diagnostics |
| `min_loop_seam_score` | `0.55` | Strongly penalize underbed candidates below the loop-safety threshold |
| `candidate_selection_version` | `1` | Audit version written to `sound_design/musicgen_candidates.json` |

---

## `sound_design`

SDP asset caps and post-generation placement QA — [sound-design.md](./sound-design.md), [post-generation-placement.md](./post-generation-placement.md).

| Key | Default | Used by | If wrong |
|-----|---------|---------|----------|
| `enabled` | `true` | SDP / palettes / SFX craft stages | Disables sound-design lane |
| `early_palettes_llm` | `false` | `sound_design_palettes` | When false, skips early palette LLM; `sound_design_plan` owns musical direction |
| `max_assets` | `6` | SDP plan + `sdp_cross_validate` | Primary asset cap (prefers this over legacy keys) |
| `max_assets_flow1` | `6` | legacy fallback for `max_assets` | Prefer `max_assets` |
| `max_assets_flow2` | `4` | unused in single-delivery path | Kept for old runs only |
| `max_palettes` | `3` | `sound_design_palettes` planning bounds | Over-broad palette spread or constrained thematic coverage |
| `use_adaptive_caps` | `true` | `sound_design` planners + sonic context posture | Ignores scenario-based cap tuning when false |
| `post_listen_gate_mode` | `warn` | post-listen QA UX/reporting | Unexpected hard-block vs advisory behavior |
| `g_listen_enabled` | `true` | optional G-Listen offer after mix when listen_critic is borderline | Set false to hide |
| `g_listen_mode` | `warn` | `warn` advisory; `block` / `block_mix` hard-stops master_finalize until continue/skip | Default is advisory; set `block` to hard-stop finalize |
| `placement_qa_enabled` | `true` | `placement_qa.py` → `maybe_run_placement_qa` after `mmaudio_sfx_flow*` (and on mix refresh) | When `true`, writes `sound_design/placement_adjustments.json`; `apply_placement_adjustments` applies hints in `flow1_overlays_from_sdp` / Flow 2 overlay builder at mix |

`placement_qa` is deterministic (no OpenAI) — reads SDP cues + `source_acoustic_profile` and logs hints via `ctx.log()`. With BUILD-SS-03, `execute_fitness_remediation` may action `regenerate` / `skip_cue` after MMAudio (capped); mix still applies placement adjustments.

---

## `soundscape.*` — closed-loop policy (BUILD-SS)

| Key | Default | Used by | If wrong |
|-----|---------|---------|----------|
| `soundscape.enabled` | `true` | `soundscape_policy_build`, mix verify | Disables policy stage + verify |
| `soundscape.strict_slots` | `true` | `sdp_cross_validate.validate_post_sound_plan` | When true, beds must map to cue_slots |
| `soundscape.fail_closed` | `true` | `soundscape_verify` after remux | When true, a fail verdict after the remediation ladder is exhausted hard-blocks mix — **except** a residual failure that is *only* a `bed_coverage`/`hinge_stinger_coverage` **minimum** shortfall, which `run_soundscape_verify` downgrades to a warning (`fail_closed_softened: true` on the report) rather than forcing another remux pass to invent more per-clip beds; max-coverage overshoot and speech-intelligibility failures still hard-fail |
| `soundscape.fail_closed_default` | `true` | fail_closed fallback when key unset and not first-try | `journey_ui.first_try_mode=true` runs default this to `false` regardless |
| `soundscape.remediation.max_remux_cycles` | `2` | `soundscape_verify` / `mix` | Caps post-mix remux loops (honest, palette-anchored bed seeding only — see `soundscape_verify.run_soundscape_verify`) |
| `soundscape.remediation.max_regen_per_asset` | `2` | `execute_fitness_remediation` | Caps MMAudio regen per asset_id |
| `soundscape.min_density.min_bed_coverage_ratio` | `0.40` | `artifact_repairs.repair_sound_design_plan` coverage-floor seeding; raises `listenability_guards.bed_coverage_min_ratio` when higher | Mirrors the Shape-owned soft-band floor below — not a hard remux target |
| `soundscape.min_density.min_beds` / `min_stingers` / `min_foley` | `2` / `1` / `0` | Same coverage-floor seeding | Minimum active cue counts for creative delivery |
| `soundscape.min_density.min_audible_bed_level_db` / `max_audible_bed_level_db` | `-16` / `-12` | Constant underbed gain band | Mix applies this `level_db` with no speech-gate duck. Dense tape uses the quiet end (−16). Speech-maximal profile stays quieter (−20/−16). |

**Bed coverage / hinge-stinger are Shape-owned soft bands, not remux theater.** The [`creative_delivery.listenability_guards`](#creative_deliverylistenability_guards) table above sets `bed_coverage_min_ratio`/`max_ratio` = **`0.40`/`0.85`** and `hinge_stinger_coverage_min_ratio`/`max_ratio` = **`0.3`/`1.0`**. These bands describe what a well-produced Shape-driven master already looks like across many source types — the verify/remediation ladder measures the *real* plan (`soundscape_verify._estimate_bed_coverage` sums actual planned bed duration over actual selection duration) and, when short, delegates to `artifact_repairs.repair_sound_design_plan`'s palette/quartile-anchored, contiguous-preferring bed seeding rather than fabricating disjoint per-clip beds purely to move the ratio. See [mix-house-chain.md](./mix-house-chain.md) and [soundscape-policy.md](./soundscape-policy.md#standards-measurable).

---

## `mix` — crossfade, slice policy, completeness (gap-closure)

| Key | Used by | If wrong |
|-----|---------|----------|
| `mix.crossfade_ms` / `mix.crossfade_ms_flow1` | `sound_design.py` speech/overlay concat | Prefer `crossfade_ms`; `crossfade_ms_flow1` is legacy alias |
| `mix.crossfade_ms_flow2` | unused in single-delivery | Kept for old runs |
| `mix.crossfade_ms_assembly_preview` | `assembly.run_preview`, `audio_preclean.py` chunk merge | Preview clip seams audible or mushy |
| `mix.adaptive_crossfade` | `audio_timeline.append_with_crossfade` via `sound_design.py` | When `true` (default), crossfade length scales 80–200 ms from tail/head energy |
| `mix.adaptive_level_from_sap` | `sound_design.py` mix level defaults from source acoustic profile | Missed speech-first level adaptation by pace/policy |
| `mix.scenario_overlay_rules` | `sound_design.py` scenario-specific overlay behavior | Overlay cadence ignores scenario posture constraints |
| `mix.word_boundary_cuts` | `sound_design.py` EDL/highlight slices | When `true`, nudge slice ends to transcript word boundaries |
| `mix.word_boundary_margin_ms` / `mix.word_boundary_max_shift_ms` | `audio_timeline.snap_cut_to_word_boundary` | Too small → mid-word cuts remain; too large → clips drift from EDL |
| `mix.normalize_vo_pickup` | `gaps.ingest_vo_pickup` | When `true`, writes loudnorm copies under `vo_pickup/normalized/` |
| `mix.vo_adjacent_level_match.enabled` | `sound_design._level_match_vo` | Matches synthetic VO to the mean level of adjacent native speech; `false` leaves only ingest loudnorm |
| `mix.vo_adjacent_level_match.max_gain_db` / `reference_window_ms` | `sound_design._level_match_vo` | Bounds correction and the native speech windows used on each side |
| `mix.junction_crossfades.speech_to_speech` | `100` | Speech↔speech join ms |
| `mix.junction_crossfades.speech_to_vo` | `80` | Speech→VO join ms |
| `mix.junction_crossfades.vo_to_speech` | `100` | VO→speech join ms |
| `mix.junction_crossfades.vo_to_vo` | `80` | VO↔VO join ms |
| `mix.junction_crossfades.music_to_speech` / `speech_to_music` | `900` / `600` | Music↔speech joins; SDP per-pair overrides still win when present |
| `mix.junction_crossfades.*` | `audio_timeline.junction_crossfade_ms`, `sound_design.mix` | Type-specific speech↔VO and music↔speech joins; values are milliseconds |
| `mix.per_speaker_level_match.enabled` | `speaker_level_match`, `sound_design.mix` | Matches dialogue speakers to the run median before assembly (default `true`) |
| `mix.per_speaker_level_match.max_gain_db` | `speaker_level_match` | Caps per-speaker correction at ±6 dB by default |
| `mix.per_speaker_level_match.min_speech_sec` | `speaker_level_match` | Speakers with less usable speech fail open at 0 dB |
| `mix.sidechain_duck.enabled` | `sidechain_duck`, `sound_design.py` | Envelope-ducks **accents / overlapping bookends** only; underbeds use a constant `level_db` |
| `mix.sidechain_duck.attack_ms` / `release_ms` / `hop_ms` | `sidechain_duck` | Soft speech-gate response for those overlapping hits (defaults ~40 / 900 / 20) |
| `mix.sidechain_duck.pause_ride_db` | `sidechain_duck` | Air lift for sidechained accents; underbeds ignore this |
| `mix.underbed_arrangement.enabled` | `music_palette_compose` | Enables chapter-aware bed scenes and primary/alternate loop rotation |
| `mix.underbed_arrangement.max_scene_segments` / `dry_break_chapters` | `music_palette_compose` | Bounds repeated-loop runs and inserts dry chapter breaks when no alternate loop exists |
| `mix.underbed_arrangement.scene_crossfade_ms` | `music_palette_compose`, `sound_design` | Minimum handoff/crossfade length for underbed scenes |
| `mix.underbed_eq.enabled` | `sound_design` | Applies a cached speech-presence carve to underbed stems before the constant level |
| `mix.underbed_eq.low_hz` / `high_hz` / `carve_db` / `max_carve_db` | `sound_design` | Configures the broad 1.5–4 kHz carve and remediation ceiling |
| `mix.underbed_ab_qc.enabled` | `sound_design`, `underbed_ab_qc` | Runs automated speech-vs-rendered-bed A/B measurements after mix |
| `mix.underbed_ab_qc.min_bed_relative_db` / `max_speech_band_excess_db` | `underbed_ab_qc` | Presence floor (default −24) and masking ceiling |
| `mix.underbed_ab_qc.lift_step_db` / `level_cut_step_db` / `carve_step_db` | `sound_design` | Bounded targeted remux: lift ghosts, cut/carve masking. `duck_step_db` is a legacy alias for the cut step |
| `mix.underbed_ab_qc.max_remux_cycles` / `fail_closed_on_masking` | `sound_design` | Caps automatic retries; persistent masking blocks while presence-only misses warn |
| `mix.music_presence.cold_open_lead_in_fade_ms` | `sound_design.py` | Fade-in for speech-free cold open / outro bookends |
| `mix.music_presence.cold_open_air_ms` | `sound_design.py` / `_cold_open_bridge_budget_ms` | Air reserved after preface VO for the cold-open bridge before the question |
| `mix.music_presence.chapter_resolve_breathe_ms` | `sound_design.py` | Dry micro-gap after chapter resolve before speech resumes |
| `mix.music_presence.bed_fade_in_ms` / `bed_fade_out_ms` / `bed_span_fade_out_ms` | `sound_design._bed_fade_ms` | Organic under-segment bed fades (defaults ~700 / 2200 / 3200); cue crossfades may only lengthen |
| `mix.music_presence.bed_fade_curve` | `audio_timeline.organic_fade_*` | Taper power (>1 keeps beds present longer then soft-lands into silence) |
| `mix.completeness_gate.enabled` | `mix_completeness.enforce_mix_completeness` | When `true`, logs missing VO/SFX after mix |
| `mix.completeness_gate.mode` | `mix_completeness.enforce_mix_completeness` | `warn` (default) logs only; `block` raises before `master_flow*` |
| `mix.missing_vo_retry_once` | `sound_design.mix`, `transition_vo` | When `true` (default), mix generates a seated missing transition/VO pickup **once** for the current pair, then silence. Never reuse old-neighbor WAVs. |
| `mix.require_preclean_acknowledgment` | *(deprecated — unused)* | Formerly gated mix/master stages on mid-pipeline pre-clean ack; v1 offers only `before_ingest` and `g1_vo_pickup` (non-blocking) |
| `mix.intelligibility_qc.enabled` | `master_qc.maybe_check_mix_intelligibility` | Optional speech-vs-bed check after mix |

## `audio_preclean`

| Key | Default | Used by | If wrong |
|-----|---------|---------|----------|
| `audio_preclean.provider` | `deepfilternet` | `preclean/provider.json`, lineage | Wrong provider label in artifacts |
| `audio_preclean.chunk_max_bytes` | `52428800` | `audio_preclean.py` chunking before DeepFilterNet | Oversized sources chunked more/less than expected |
| `audio_preclean.local_fallback_enabled` | `true` | `stages/audio_preclean.py` | When `true` (default), DeepFilterNet failure falls back to ffmpeg `afftdn` denoise (`provider: ffmpeg_local`) |
| `audio_preclean.ffmpeg_highpass_hz` | `80` | `ffmpeg_denoise.py` | Rumble cutoff for the deterministic local fallback |
| `audio_preclean.ffmpeg_lowpass_hz` | `12000` | `ffmpeg_denoise.py` | High-frequency cutoff for the deterministic local fallback |
| `audio_preclean.ffmpeg_afftdn_nr` | `12` | FFmpeg `afftdn` | Noise-reduction depth; values outside FFmpeg's 0.01–97 dB range are rejected |

See [local-audio-stack.md](./local-audio-stack.md) · [audio_preclean README](../pipeline/audio_preclean/README.md).

---

## `local_runtimes`

Isolated venv paths — [local-audio-stack.md](./local-audio-stack.md).

| Key | Default | If wrong |
|-----|---------|----------|
| `local_runtimes.mlx.venv_dir` | `ASSETS/local_llm/venv` | MLX subprocess fails — re-run `bootstrap_venv.sh` |
| `local_runtimes.llm.venv_dir` | `ASSETS/local_llm/venv` | Alias for volley framer runtime |
| `local_runtimes.speech.venv_dir` | `ASSETS/local_speech/venv` | mlx-audio STT/S2S subprocess fails |
| `local_runtimes.deepfilter.venv_dir` | `ASSETS/local_deepfilter/venv` | Preclean DeepFilterNet subprocess fails |
| `local_runtimes.mmaudio.venv_dir` | `ASSETS/local_mmaudio/venv` | MMAudio SFX subprocess fails |
| `local_runtimes.*.enabled` | `true` | When `false`, `local_runtime` raises for that stack |

---

## `seed_policy`

Hard-freeze sticky seed stages. Soft freeze never sticky-completes.

| Key | Default | If wrong |
|-----|---------|----------|
| `seed_policy.freeze_sticky_extra_stages` | `[]` | Future seal no-op stage ids missing → seed may rewind sealed work; add id here or to `FREEZE_STICKY_SEED_STAGES_CORE`. Extras must be known pipeline stages and not on the critical denylist (`mix` / `vo_synthesize` / ship stages, etc.). |

---

## `local_gpu`

Machine-wide exclusive gate for heavy local AI subprocesses (MusicGen, Chatterbox, MMAudio, MLX speech/LLM, DeepFilter). Pipeline stages are already one-at-a-time per run; this also serializes back-to-back gens inside a stage and across runs.

| Key | Default | If wrong |
|-----|---------|----------|
| `local_gpu.serialize` | `true` | Overlapping GPU jobs thrash unified memory / Metal abort; set `INTERVIEW_MUX_GPU_SERIALIZE=0` only for debug |
| `local_gpu.cooldown_sec` | `5` | After each consumer exits, wait before next may start; override with `INTERVIEW_MUX_GPU_COOLDOWN_SEC` |
| `local_gpu.abort_backoff_sec` | `30` | After SIGTERM/SIGKILL/SIGABRT on a heavy subprocess, wait before the next GPU job; override with `INTERVIEW_MUX_GPU_ABORT_BACKOFF_SEC` |
| `local_gpu.lock_timeout_sec` | `7200` | Gate wait times out while another job still holds GPU |
| `local_gpu.consumers` | musicgen, mmaudio, chatterbox, speech, mlx, llm, deepfilter, image | Listed runtime ids must take the gate |

---

## `local_speech`

Local mlx-audio STT + S2S — [speech-to-speech-vo.md](./speech-to-speech-vo.md) · [local-audio-stack.md](./local-audio-stack.md).

| Key | Default | If wrong |
|-----|---------|----------|
| `local_speech.enabled` | `true` | STT stage fails on arm64 when disabled |
| `local_speech.models_dir` | `ASSETS/local_speech/models` | HF weights cache location |
| `local_speech.fail_open` | `false` | S2S/VO errors hard-stop when `false` (default); set `true` only for legacy soft continue |
| `local_speech.stt_timeout_sec` | `3600` | Long interviews timeout mid-transcribe |
| `local_speech.s2s_timeout_sec` | `600` | Gap VO synthesis timeout |
| `local_speech.context_clip_enabled` | `true` | Disable segment-adjacent prosody clips |
| `local_speech.context_clip_min_ms` | `2000` | Minimum context window |
| `local_speech.context_clip_max_ms` | `8000` | Maximum context window |
| `local_speech.min_reference_sec` | `3.0` | Speaker sample quality floor |
| `local_speech.warmup_tts_model_id` | `""` | MLX TTS id for probe warm-up (empty → selection `s2s_model_id`) |
| `local_speech.interrogate_model_id` | `""` | Listen STT model (empty → `stt_model_id` / selection Whisper) |
| `local_speech.interrogate_mode` | `stt_listen` | Certified path: clip STT → contract answer (`stt_listen`) |
| `local_speech.interrogate_timeout_sec` | `120` | Per-probe listen timeout |
| `local_speech.warmup_voice_wav` | `ASSETS/local_speech/warmup_voice/neutral.wav` | Neutral warm-up voice (auto-created via `scripts/ensure_warmup_voice.py`) |
| `local_speech.diarization_verify_max_pairs` | `200` | Cap GPU same-speaker pair checks per run after G0; hanging-setup flips run first |
| `local_speech.diarization_verify_timeout_sec` | `300` | Per-pair Sortformer / interrogate timeout |
| `local_speech.micro_other_max_ms` | `700` | Max duration of a nested um/uh island that can be absorbed into a monologue |
| `local_speech.micro_other_max_words` | `2` | Max tokens in an absorbable filled-pause island |
| `local_speech.dominant_speaker_min_share` | `0.98` | Enclosing-run duration share required to absorb a nested micro island |

---

## `audio_probes`

Local Audio Probe Platform + Vernacular Evidence Covenant — [vernacular-evidence-covenant.md](./vernacular-evidence-covenant.md).

| Key | Default | If wrong |
|-----|---------|----------|
| `audio_probes.enabled` | `true` | Stage may no-op or skip |
| `audio_probes.enforcement_mode` | `shadow` | `authoritative` hard-blocks auto_pack drops of vernacular must_keep |
| `audio_probes.low_confidence_threshold` | `0.85` | Prefilter brick detection |
| `audio_probes.fail_open` | `true` | Prefer continue with unknown facts / empty safe artifacts |
| `audio_probes.prefer_mlx` | `true` | Attempt local classify before heuristic (still fail-open) |
| `audio_probes.enabled_packs` | `null` | `null` = all packs; else list of pack names |
| `audio_probes.budget.max_clips` | `40` | Probe call cap |
| `audio_probes.budget.max_audio_sec` | `600` | Audio seconds fed to probes |
| `audio_probes.budget.max_wall_sec` | `900` | Wall-clock budget for probe stage |
| `audio_probes.sanitize.min_child_ms` | `800` | Min child duration after N-way split |

---

## `deepfilter`

| Key | Default | Used by | If wrong |
|-----|---------|---------|----------|
| `deepfilter.repo_dir` | `ASSETS/local_deepfilter/DeepFilterNet` | `deepfilter_runner`, bootstrap | Clone missing → enhance fails |
| `deepfilter.model` | `DeepFilterNet3` | `tools/deepfilter_enhance.py` | Wrong model load |
| `deepfilter.postfilter` | `false` | enhance CLI | Extra post-filter stage |
| `deepfilter.compensate_delay` | `true` | enhance CLI | Alignment vs latency tradeoff |
| `deepfilter.request_timeout_sec` | `600` | Base `local_runtime` subprocess timeout | Hung or premature timeout |
| `deepfilter.timeout_sec_per_file` | `90` | Per-file addend for batch enhance hang budget | Multi-file batch under one fixed 600s |
| `deepfilter.max_request_timeout_sec` | `2400` | Cap for batch hang budget | Unbounded batch wait |

---

## `mmaudio`

| Key | Default | Used by | If wrong |
|-----|---------|---------|----------|
| `mmaudio.repo_dir` | `ASSETS/local_mmaudio/MMAudio` | `mmaudio_runner`, bootstrap | Clone missing → generation fails |
| `mmaudio.model_id` | `large_44k_v2` | `mmaudio_generate.py` | Wrong HF weights |
| `mmaudio.device` | `auto` | local MMAudio runtime device choice | Wrong backend selection / avoidable runtime failures |
| `mmaudio.default_duration_sec` | `8.0` | craft/generate fallback duration | Unexpected clip length when role duration absent |
| `mmaudio.min_duration_sec` / `max_duration_sec` | `3.0` / `8.0` | `mmaudio_runner.clamp_duration_seconds` | Clamped generation length |
| `mmaudio.duration_bands_by_role` | role map | craft validation and plan-duration sanity | Role-specific lengths drift from product timing policy |
| `mmaudio.theme_fit_threshold` | `0.6` | post-generation thematic QA checks | Too lenient/strict thematic acceptance |
| `mmaudio.silence_rms_threshold` | `0.001` | silence/near-silence QA detection | False silence passes or noisy rejects |
| `mmaudio.semantic_qa_enabled` | `true` | Tier-2 CLAP text–audio similarity via `tools/clap_similarity.py` in MMAudio venv | Requires MMAudio venv bootstrap; fail-open when CLAP unavailable |
| `mmaudio.semantic_qa_threshold` | `0.18` | Minimum CLAP cosine similarity for pass | Low scores warn or fail depending on `semantic_qa_fail_on_low` |
| `mmaudio.semantic_qa_fail_on_low` | `false` | When true, sub-threshold CLAP scores set `verdict=fail` | Stricter auto-refine/regenerate loop |
| `mmaudio.semantic_qa_model_id` | `laion/clap-htsat-fused` | Hugging Face CLAP model id | Model download size / runtime |
| `mmaudio.semantic_qa_timeout_sec` | `120` | Per-asset CLAP subprocess timeout | Timeouts skip Tier-2 with `semantic_qa_verdict=skipped` |
| `mmaudio.cfg_strength_default` | `4.5` | `resolve_cfg_strength` | Global CFG fallback |
| `mmaudio.cfg_strength_by_role` | role map | `resolve_cfg_strength` | Per-role adherence |
| `mmaudio.num_steps` | `25` | `mmaudio_generate.py` | Quality vs speed |
| `mmaudio.seed_strategy` | `asset_id_hash` | `resolve_seed` | `fixed` / `random` / `asset_id_hash` |
| `mmaudio.fixed_seed` | `42` | `resolve_seed` when strategy `fixed` | Reproducibility |
| `mmaudio.legacy_influence_prose` | `false` | append influence to positive prompt | legacy prose-influence fallback |
| `mmaudio.auto_refine_enabled` | `true` | `sfx_mmaudio.maybe_auto_refine` | LLM refine after QA/listen fail |
| `mmaudio.auto_refine_max_attempts_per_asset` | `2` | refine loop cap | Runaway LLM spend |
| `mmaudio.auto_refine_on_qa_fail` / `on_listen_fail` | `true` | auto-refine triggers | Which failures invoke refine |
| `mmaudio.auto_refine_on_trauma` | `true` | auto-refine for `trauma_adjacent` without manual override | Set `false` to require per-asset `sfx_auto_refine_override` |
| `mmaudio.request_timeout_sec` | `900` | Base `local_runtime` subprocess timeout | Long generations time out |
| `mmaudio.max_request_timeout_sec` | `2400` | Cap for duration-scaled hang budget | Unbounded MMAudio wait |
| `mmaudio.ref_duration_sec` | `8.0` | Reference duration for hang budget scale | Short beds get full budget; long beds scale up |

Craft artifact optional fields (`sound_design/sfx_prompts.json`): `mmaudio_variant`, `cfg_strength`, `num_steps`, `seed`, `regression_notes` — see [mmaudio-prompt-tuning.md](./mmaudio-prompt-tuning.md).

Optional lock files: `requirements-local-mlx.txt`, `requirements-local-deepfilter.txt`, `requirements-local-mmaudio.txt` — regenerate with `pip-compile` when pinning local stacks.

---

## `nle_edits`

| Key | Used by | If wrong |
|-----|---------|----------|
| `nle_edits.strict` | `nle_state.save_nle`, GUI NLE PUT | When `true`, invalid `segments/nle_edits.json` raises HTTP 400 instead of warn-only |
| `nle_edits.block_incomplete_ends` | `nle_state.save_nle` (`incomplete_trim_ends`) | Default `false`: an operator trim whose end lands mid-clause (`ends_complete_thought()` false) only logs a loud `warning` (`stage=full_master_ranking`) and still saves. `true` raises `ValueError` (soft-block, same mechanism as `strict`) instead of saving — protects idea transmission by refusing to persist a trim that chops a thought in half |

---

## `v2` (greenfield simplified app)

| Key | Default | Used by | Notes |
|-----|---------|---------|-------|
| `v2.enabled` | `true` | `v2.config`, GUI PhaseWorkbench | Single master-podcast path |
| `v2.auto_commit_artifacts` | `true` | `write_staging.write_approval_enabled` | No `.pending_writes/` staging |
| `v2.g1_optional` | `true` | `gates.require_g1_clear`, pipeline analysis finalize | Skip gap VO allowed |
| `v2.llm_max_attempts` | `2` | `llm_simple.run_llm_stage_simple` | Schema + one retry |
| `v2.lint_blocking` | `false` | `llm_simple` warn-only lint | |
| `v2.cross_validate_blocking` | `false` | `llm_simple` warn-only crossval | |

See [NORTH_STAR.md](../../NORTH_STAR.md) and [docs/v2/drop-manifest.md](../v2/drop-manifest.md).

---

## `resilience` (stage resilience runtime)

| Key | Default | Used by | Notes |
|-----|---------|---------|-------|
| `resilience.quality_first` | `true` | `stage_resilience`, escalation resolve | Never auto-publish a waived master |
| `resilience.commit_barrier` | `true` | `write_staging.approve_stage_writes` | Validate staged overlay before flush |
| `resilience.identical_failure_halt_after` | `3` | `identical_failures`, Full-auto driver | Stop the same heal signature after N repeats; write execution report |
| `artifact_sanitize.block_consumers` | `true` | `artifact_sanitize` | Hard-gate consumers when selection/gap/air unsanitary |
| `artifact_sanitize.halt_after` | `3` | `artifact_sanitize.halt` | Identical sanitize_refused halt (code default if unset) |
| `artifact_sanitize.selection.max_same_family_on_air` | `8` | `artifact_sanitize.selection` | Cap NLE children from one base id on air |
| `artifact_sanitize.selection.max_fragment_depth` | `3` | `artifact_sanitize.selection` | Collapse deeper `seg_003aaaa…` trees |
| `artifact_sanitize.selection.max_cta_readmit` | `0` | `artifact_repairs._readmit_cta_story_children` | Disable unbounded CTA story readmit on every write |
| `artifact_sanitize.selection.max_order_growth_pct` | `15` | `repair_master_selection` | Refuse repair amplification beyond growth budget |
| `artifact_sanitize.gap.min_layup_coverage` / `layup.min_layup_coverage` | `0.70` | `artifact_sanitize.gap_report` | Optional coverage floor for gap/layup refuse |
| `resilience.unattended_defaults.enabled` | `false` | operator escalation resolve | Documented defaults write the same decision artifacts as humans. Full-auto (`MUX_FULL_AUTO=1` / `run_meta.full_auto`) treats this as on without `force_publish`. |
| `resilience.source_profiles` | (object) | `stage_families.select_source_profile`, ingest | Recipes for clean / town-hall / noisy / video / short / long |

---

## Related

- [model-routing.md](./model-routing.md) — tier registry and v1 mapping
- [llm-stage-model-matrix.md](./llm-stage-model-matrix.md) — per-stage tiers
- [prompts/README.md](../prompts/README.md) — prompt conventions
- `src/interview_mux/config.py` — merge rules
- `config/templates/secrets.env.example` — secret key names
- [config/README.md](../../config/README.md) — resolution order

## `thrash_spine` — ESR / seat freeze / meta-gates

| Key | Default | Used by | If wrong |
|-----|---------|---------|----------|
| `thrash_spine.progress_sla.*` | see defaults | `execution_status` | Too low → false sticky; too high → slow halt |
| `thrash_spine.seat_freeze.max_rewrites_post_soft` | `2` | `seat_authority` | Unlimited seat thrash after soft freeze |
| `thrash_spine.seat_freeze.max_rewrites_post_hard` | `1` | `seat_authority` | Seat thrash after VO synth |
| `thrash_spine.meta_gate.min_opportunity` | `0.65` | seat rewrite gate | Too low → thrashy rewrites |
| `thrash_spine.meta_gate.min_expected_gain` | `0.15` | timeline reopen gate | Too low → useless remasters |

See [execution-status.md](./execution-status.md).

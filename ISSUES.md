# ISSUES.md

Running log of bugs found while debugging this app, for handover to Nicket.
Environment notes and workflow: [CLAUDE.md](CLAUDE.md).

**Status key:** `OPEN` · `INVESTIGATING` · `FIXED` (patch in this fork) ·
`REPORTED` (handed to Nicket) · `WONTFIX` / `ENV` (environment, not a code bug).

---

## Lead: the failure window is stages 40 to 60

**Nicket's report:** all local models worked fine; every error appeared between
**stages 40 and 60**.

The pipeline is 72 stages (35 analysis + 37 delivery) per
`src/interview_mux/v2/config.py`. Note `README.md` still says 67, which is
stale; `AGENTS.md` has 72. Stages 40 to 60 are **entirely delivery**:

| # | Stage | # | Stage |
|---|---|---|---|
| 40 | `full_master_ranking` | 51 | `air_contract_sanitize` |
| 41 | `selection_order_sanitize` | 52 | `transitions` |
| 42 | `air_script_compose` | 53 | `sound_design_plan` |
| 43 | `nugget_corpus_mine` | 54 | `vo_line_adjudicate` |
| 44 | `information_package_plan` | 55 | `vo_synthesize` |
| 45 | `nugget_layup_compose` | 56 | `sound_design_vo_finalize` |
| 46 | `gap_report_sanitize` | 57 | `edl_narrative_audit` |
| 47 | `refinement_agenda` | 58 | `edl` |
| 48 | `gap_framing_recompose` | 59 | `assembly_preview` |
| 49 | `selection_framing_apply` | 60 | `listen_delight_audit` |
| 50 | `air_script_seams` | | |

### Why this is consistent with "local models worked fine"

Only **one** of those 21 stages touches a local model at all:

| Stage | Local runtime | In 40-60? |
|---|---|---|
| 1 `audio_preclean` | DeepFilterNet | no |
| 3 `transcribe` | faster-whisper / STT | no |
| **55 `vo_synthesize`** | **Chatterbox / S2S** | **yes** |
| 61 `music_palette_compose` | MusicGen | no |
| 63 `mmaudio_sfx` | MMAudio | no |

So the band is almost pure **cloud-LLM work plus deterministic assembly**:
ranking, air-script composition, nugget mining, framing, transitions, VO
adjudication, EDL build, and the audit gates over them. Working local models
tell us nothing about this band, which is exactly why the errors cluster here.

**Where to look first:** the LLM schema/contract layer and the audit gates,
not the model runtimes. Candidates: `full_master_ranking` (40) feeds everything
downstream, so a bad selection there cascades; the `*_sanitize` stages (41, 46,
51) exist to repair upstream output and are where contract violations surface;
`edl_narrative_audit` (57) and `listen_delight_audit` (60) are hard ship gates.

### Corroborating signal from the test suite

Of the 14 known-failing tests, **7 sit in or adjacent to this same band**. That
correlation is worth treating as a map of where the real defects are:

| Test | Band stage |
|---|---|
| `test_artifact_cross_validate::test_post_ranking_orphan_segment_raises` | 40 `full_master_ranking` |
| `test_artifact_cross_validate::test_maybe_cross_validate_ranking_raises_system_exit` | 40 `full_master_ranking` |
| `test_i13183_selection_residues::test_letter_kids_inherit_and_persist_hints` | 40-41 selection |
| `test_hosted_vo_real_exec_met_bugs::test_real_exec_hosted_vo_sufficiency_report` | 54-56 VO |
| `test_delivery_guardrails::test_premature_cap_keeps_music_palette_not_edl_narrative` | 57 `edl_narrative_audit` |
| `test_r6_edl_budget::test_r6_order_drift_fingerprint_flip_recovers_then_budget` | 58 `edl` |
| `test_creative_delivery::test_hydrate_cue_segments_from_cue_ids` | 53 `sound_design_plan` |

These were failing **before** the Windows port (verified against a clean
baseline), so they are pre-existing defects, not platform artifacts. They are
the cheapest entry point: reproducible in seconds, no API key, no audio.

**Status:** `OPEN`, not yet investigated.

---

## Known-failing tests (baseline)

`PYTHONUTF8=1 .venv/Scripts/python -m pytest -q` gives
**6633 passed, 14 failed, 19 skipped** in ~13 min.

All 14 were failing before any Windows work, confirmed by diffing the failure
set against a pre-change run. **Zero regressions introduced by the port.**

| Test | Cause | Status |
|---|---|---|
| `test_api_consent_flow::test_execute_not_blocked_by_consent` | `OPENAI_API_KEY` unset. Asserts `not needs_api_consent` but creds are missing. | `ENV` |
| `test_rstm/test_rstm_matrix::test_rstm_execute_all_cells` | `FileNotFoundError` | OPEN |
| `test_full_auto_launch_api::test_create_run_full_auto_launches_run_scoped_worker` | `WinError 2` spawning a worker subprocess. Likely platform. | OPEN |
| `test_homunculus::test_failed_stage_invokes_do_not_burn_cap` | regex did not match | OPEN |
| `test_homunculus::test_nested_llm_does_not_burn_stage_identity_cap` | regex did not match | OPEN |
| `test_creative_delivery::test_hydrate_cue_segments_from_cue_ids` | `assert 'seg_001' == 'seg_002'` | OPEN |
| `test_i13183_selection_residues::test_letter_kids_inherit_and_persist_hints` | `assert 'seg_062la' in {}` | OPEN |
| `test_artifact_cross_validate` (x2) | see band table above | OPEN |
| `test_delivery_guardrails::test_premature_cap_keeps_music_palette_not_edl_narrative` | | OPEN |
| `test_r6_edl_budget::test_r6_order_drift_fingerprint_flip_recovers_then_budget` | | OPEN |
| `test_r_workflow_residual::test_r_wf_musicgen_stub_forbidden_via_verify` | | OPEN |
| `test_hosted_vo_real_exec_met_bugs::test_real_exec_hosted_vo_sufficiency_report` | | OPEN |
| `test_musicgen_gpu_realworld::test_shipped_defaults_gpu_large_first_ladder` | Asserts `effective_musicgen_device() == "mps"`. Apple-hardware lock; **cannot** pass on CUDA. | `WONTFIX` (platform) |

Some are order-dependent: a few passed when run in isolation but failed in the
full run. Worth confirming which are genuinely flaky before chasing them.

---

## Blockers before a real end-to-end run

1. **`OPENAI_API_KEY` is unset** in `config/secrets/secrets.env`. Stages 40-60
   are mostly LLM calls, so the reported failure band cannot be reproduced
   without it. **This is the first thing to fix.**
2. **Diarization is not installed.** Transcription works but labels every word
   `spk_0`, so nothing downstream can tell interviewer from guest. That will
   corrupt exactly the selection and framing stages in the 40-60 band, so it
   must be installed before trusting any result there. Needs a gated pyannote
   model plus `HF_TOKEN`; steps in
   [docs/cross-cutting/windows-cuda-setup.md](docs/cross-cutting/windows-cuda-setup.md).
3. **No source audio** in `ASSETS/input/`.

There is an existing forensics campaign with its own protocol at
`.cursor/plans/full_auto_forensics_run.plan.md` (always start FRESH with
`MUX_FRESH=1`). Read it before launching a debug run.

---

## Platform gaps found during setup

Recorded because they affect debugging on this machine. Fixes are in PR #1 on
the fork unless noted.

| # | Issue | Status |
|---|---|---|
| P1 | `uvloop` pinned in `requirements.lock` has no Windows build. Core installer filters it out; uvicorn uses the asyncio loop. | FIXED |
| P2 | Pipeline logs non-ASCII; Windows cp1252 made subprocess stdout raise `UnicodeEncodeError`. `run.sh` now exports `PYTHONUTF8=1`. | FIXED |
| P3 | `system_memory_gb()` used `os.sysconf` and silently returned a hardcoded `8.0` on Windows. It drives model tiering, so tiering was wrong. Now reads `GlobalMemoryStatusEx`. **Worth reporting to Nicket: the silent fallback is a latent bug on any non-POSIX host.** | FIXED |
| P4 | Venv paths hardcoded `bin/python`; Windows uses `Scripts/python.exe`. Centralised in `src/interview_mux/venv_paths.py`. | FIXED |
| P5 | `static/index.html` committed with CRLF while `vite build` writes LF, so **every** GUI rebuild dirtied 13 tracked files with zero content change. Affects macOS too. **Worth reporting.** | FIXED |
| P6 | Committed test debris: `MagicMock/` (10 files, from a MagicMock used where a `Path` was expected, writing its repr as a directory tree), `.pytest_tmp_footgun_soft/`, root `gui_job.json`, `.DS_Store`, three stray DER blobs. **Worth reporting: the MagicMock leak means a test writes to a path built from a mock.** | FIXED |
| P7 | `run.sh` port and orphan cleanup uses `lsof` / `ps -ax`, skipped on Windows. A stale server on 8765 must be killed manually. | OPEN |
| P8 | 6 pre-existing `F821 undefined name` errors in `tools/full_auto_driver.py` (`err` at 5393/5399/5403, `meta_g` at 9639, `_log` at 11545). All in error-handling paths, so they raise `NameError` **only when the code is already failing**, masking the original error. Present on `HEAD` before any of my changes. **Worth reporting.** | OPEN |
| P9 | `README.md` says 67 stages; actual is 72 (`AGENTS.md` is correct). | OPEN |

---

## Template

```markdown
## [N] Short title

**Stage / area:** e.g. 40 `full_master_ranking`
**Status:** OPEN
**Repro:** exact command, run id, input
**Expected vs actual:**
**Error:**
```
paste the real error, trimmed
```
**Root cause:**
**Fix:** commit or PR
**Report to Nicket:** yes/no, and why
```

# ISSUES.md

Running log of bugs found while debugging this app, for handover upstream.
Environment notes and workflow: [CLAUDE.md](CLAUDE.md).

**Status key:** `OPEN` · `INVESTIGATING` · `FIXED` (patch in this fork) ·
`REPORTED` (handed upstream) · `WONTFIX` / `ENV` (environment, not a code bug).

---

## Lead: the failure window is stages 40 to 60

**Upstream report:** all local models worked fine; every error appeared between
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

## Test suite status

| Stage of work | Result |
|---|---|
| First Windows run, before any changes | 6624 passed, **23 failed**, 19 skipped |
| After ffmpeg + UTF-8 mode | 6630 passed, 17 failed |
| After the local CUDA stacks landed | 6633 passed, 14 failed |
| After the bug pass | **6657 passed, 0 failed, 22 skipped** |
| After the stub traversal work | **6672 passed, 0 failed, 22 skipped** |
| After the staging-flush fix | **6673 passed, 0 failed, 21 skipped** |
| After the livelock + 429 fixes | **6676 passed, 0 failed, 21 skipped** |

Command: `PYTHONUTF8=1 .venv/Scripts/python -m pytest -q` (~16 min).

Every one of the original 23 is now resolved: 6 were real product defects, 8
were stale tests or thin fixtures, 3 are environment or hardware limits now
skipped with an explicit reason, and the rest fell out as duplicates of the same
root causes (four separate red tests turned out to be one sanitize refusal).

The 22 skips are honest: they name why they cannot run here (no API key, no
operator run data, not Apple Silicon) rather than silently passing.

The original 23 failures, with the root cause and fix for each, are the
numbered entries under **Fixed bugs** below.

---

## Blockers before a real end-to-end run

1. **`OPENAI_API_KEY` is unset** in `config/secrets/secrets.env`. Stages 40-60
   are mostly LLM calls, so the reported failure band cannot be reproduced
   without it. **This is the first thing to fix.**
2. **Diarization: deliberately not installed.** Decided to skip for now. Every
   word is labelled `spk_0`, so nothing downstream can tell interviewer from
   guest.

   **What this means for debugging.** Crashes, exceptions and contract
   violations in the 40-60 band still reproduce normally, so bug-hunting is
   unaffected. But any *quality* output from the stages that reason about who
   is speaking is meaningless: `speaker_roles` (8), `full_master_ranking` (40),
   the framing stages (48-49) and `vo_line_adjudicate` (54) all consume
   speaker identity. Do not report a quality verdict from those stages to
   upstream while this is off, and do not treat a plausible-looking result there
   as evidence anything works. Install diarization first if a bug turns out to
   depend on speaker attribution.

   macOS needs no setup: `mlx-community/diar_sortformer_4spk-v1-fp32` is
   ungated and MLX bundles the implementation. CUDA has no equally free path:

   - **pyannote** (MIT, clean pip install) is gated `gated=auto`, so it needs
     terms accepted on the model page plus `HF_TOKEN`.
   - **The same Sortformer model** (`nvidia/diar_sortformer_4spk-v1`) is
     ungated, but `transformers` does not implement Sortformer. Its config
     claims `transformers_version: 5.0.0.dev0` from an unmerged branch; there
     is no `models/sortformer` in the 5.17 release or in git main (verified).
     It needs `nemo_toolkit[asr]`, heavy and imperfectly supported on Windows.

   **Licensing, worth raising upstream:** `nvidia/diar_sortformer_4spk-v1`
   is **cc-by-nc-4.0, non-commercial**, and the macOS default is the MLX port
   of it. A commercially published podcast therefore has a licensing problem on
   **both** platforms, not just this one. pyannote is MIT, so switching both
   machines to it may be the better call. Options and steps in
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
| P3 | `system_memory_gb()` used `os.sysconf` and silently returned a hardcoded `8.0` on Windows. It drives model tiering, so tiering was wrong. Now reads `GlobalMemoryStatusEx`. **Worth reporting upstream: the silent fallback is a latent bug on any non-POSIX host.** | FIXED |
| P4 | Venv paths hardcoded `bin/python`; Windows uses `Scripts/python.exe`. Centralised in `src/interview_mux/venv_paths.py`. | FIXED |
| P5 | `static/index.html` committed with CRLF while `vite build` writes LF, so **every** GUI rebuild dirtied 13 tracked files with zero content change. Affects macOS too. **Worth reporting.** | FIXED |
| P6 | Committed test debris: `MagicMock/` (10 files, from a MagicMock used where a `Path` was expected, writing its repr as a directory tree), `.pytest_tmp_footgun_soft/`, root `gui_job.json`, `.DS_Store`, three stray DER blobs. **Worth reporting: the MagicMock leak means a test writes to a path built from a mock.** | FIXED |
| P7 | `run.sh` port and orphan cleanup used `lsof` / `ps -ax`, skipped on Windows, so a stale server on 8765 had to be killed by hand. | **FIXED** (entry 27) |
| P8 | 6 pre-existing `F821 undefined name` errors in `tools/full_auto_driver.py` (`err` at 5393/5399/5403, `meta_g` at 9639, `_log` at 11545). All in error-handling paths, so they raised `NameError` **only when the code was already failing**, masking the original error. Present on `HEAD` before any of my changes. **Worth reporting.** | **FIXED**, `ruff --select F821` clean |
| P9 | `README.md` said 67 stages (34 + 33); actual is 72 (35 + 37), and `AGENTS.md` was already correct. | **FIXED**, pinned by `test_docs_stage_count_matches_code.py` |

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
**Report upstream:** yes/no, and why
```

---

# Fixed bugs

Numbered for handover. "Product" = a real defect in shipped code. "Test" = the
code was right and the test was stale. Every entry was reproduced before fixing
and verified after.

## [1] PRODUCT: six NameErrors destroyed the real error in Full-auto heal paths

**Stage / area:** `tools/full_auto_driver.py`, heal and gate handling
**Status:** FIXED (commit `978cee0`)
**Severity:** high for debugging. All six sit in `except` / heal branches, so
they only fire once something has *already* failed, and the `NameError` then
replaces the original diagnostic. This is the single worst place for this bug
and plausibly hid the real cause of the stage 40-60 failures.

**Repro:** `ruff check tools/full_auto_driver.py` on a clean checkout (the repo
configures ruff for F821). Six hits, all predating any Windows work.

| Line | Wrote | Should be | Effect |
|---|---|---|---|
| 5393, 5399, 5403 | `err` | `msg` | `handle_gate()` has no `err`; the gate text is `msg`. A looping `edl_narrative_qc` heal raised NameError instead of reporting QC errors and pausing for the operator. |
| 9639 | `meta_g` | `meta_r` | The line above reads run_meta.json into `meta_r`, then never uses it. The "resplit cycle already spent" guard could never run. |
| 11545 | `_log` | `log` | `_log` is defined nowhere; the module logger is `log` at line 116. Turned a successful ship-path shortcut into a crash. |

**Verified:** `ruff check src tools scripts tests` is now clean for the first
time. Remaining F811s are a parameter named `field` shadowing an unused
`dataclasses.field` import, which is harmless.

## [2] PRODUCT: letter-split children were deleted as orphan refs

**Stage / area:** 40-41 `full_master_ranking` / `selection_order_sanitize`,
via `artifact_sanitize/selection.py`
**Status:** FIXED
**Severity:** high. Silently corrupts the air order, and can hard-fail the run.

**Root cause.** Sanitize step 2b drops any ordered id that is not on the "live
tape", where live means present in `segments/manifest.json`. Letter-split
children (`seg_062la`, `seg_062lb`) are produced at runtime by the resplit
stages and are **never written to the manifest**, so every one of them looked
like a ghost and was dropped.

The module already contains `_inherit_letter_family_starts()`, written for
exactly this: resolve a letter-suffixed id to its nearest ancestor span. But it
was only called later, from `_starts_unavailable_error()`, which runs **after**
step 2b has already deleted the ids. So the helper never saw them.

**Cascade.** The drop collapsed the family to its parent, so
`_multi_member_family_ids()` then found a single-member family, so no
`inherited_segment_spans` hints were persisted, so the **next** sanitize refused
with `segment_starts_unavailable`. That is the exec_13183 signature.

**Repro:** a selection of `[seg_062, seg_062la, seg_062lb]` against a manifest
holding only `seg_062`. Before: `drop_orphan_ref: [seg_062la, seg_062lb]`,
output `[seg_062]`, hints `None`. After: hints persisted for both kids and the
round-trip sanitize no longer refuses.

**Fix:** step 2b resolves letter-family inheritance into the live set before
computing ghosts.

**Guard checks (confirmed the fix does not weaken the drop):** a letter kid of a
live parent survives; a bogus numeric id (`seg_999`) is still dropped; a letter
kid of a *non*-live parent (`seg_777zz`) is still dropped; an all-bogus
selection is still refused.

## [3] PRODUCT: the selection refusal threw away its own reason

**Stage / area:** `artifact_sanitize/selection.py` refuse thresholds
**Status:** FIXED
**Severity:** medium, diagnostics only, but it directly costs debugging time in
the 40-60 band.

When sanitize emptied the order it raised `sanitize_refused:selection:
ordered_segment_ids empty after sanitize` and **discarded the drop reasons it
had just computed**, forcing whoever debugs it to re-derive the cause from an
already-failed run.

Now includes the input count, the drop reasons grouped with example ids, and
whether any live segment ids were available at all:

```
ordered_segment_ids empty after sanitize (in=2, dropped: drop_orphan_ref=['seg_1', 'seg_2'])
ordered_segment_ids empty after sanitize (in=0, no live segment ids available)
```

## [4] TEST: four stale tests wrote selections that sanitize correctly refuses

**Status:** FIXED
**Not a product bug.** All four predate the one-writer sanitize layer and wrote
ordered ids that are genuinely orphans, so the write was correctly refused. They
all failed with the same message, which made one bug look like four.

| Test | Problem | Fix |
|---|---|---|
| `test_artifact_cross_validate::test_post_ranking_orphan_segment_raises` | deliberately writes orphan `seg_999` so the validator can flag it | `ctx._one_writer_raw = True`, the bypass two sibling tests in the same file already use |
| `test_artifact_cross_validate::test_maybe_cross_validate_ranking_raises_system_exit` | same | same |
| `test_r6_edl_budget::test_r6_order_drift_fingerprint_flip_recovers_then_budget` | synthetic ids `a`/`b`/`c` must survive verbatim for the fingerprint comparison | bypass |
| `test_delivery_guardrails::test_premature_cap_keeps_music_palette_not_edl_narrative` | wrote `seg_1` but the fixture seeds `seg_001` | corrected the id, keeping sanitize in the loop |

## [5] PRODUCT: Full-auto was broken on Windows and 500'd run creation

**Stage / area:** `tools/full_auto_daemon_launch.py`, GUI `POST /api/runs`
**Status:** FIXED (commit `dd2dbef`)
**Severity:** high. Full-auto is the self-propelled mode, and this failed in the
worst way: an unhandled `FileNotFoundError` escaped out of the HTTP handler.

**Root cause.** The daemon manager drives everything through `pgrep`, `pkill`,
`kill` and `lsof`. None exist on Windows, and `subprocess` raises
`FileNotFoundError` rather than returning non-zero, so the
`except subprocess.CalledProcessError` guards did not catch it.
`automation_driver_alive()` propagated straight through `full_auto_launch.py`
into the web layer.

**Fix.** New `src/interview_mux/proc_compat.py`: `process_alive`,
`pids_matching`, `kill_matching`, `terminate_pids`, `pids_listening_on_port`,
`detach_kwargs`. Every helper is best-effort and returns empty rather than
raising, because each caller is a liveness probe or a cleanup sweep where
failing to find a process must not abort the caller. No new hard dependency and
**macOS behaviour is unchanged**: `psutil` is used when importable, otherwise
POSIX keeps using the original `pgrep` and Windows queries CIM via PowerShell.

**Two quieter breakages fixed in the same file:**
- `_popen` passed `start_new_session=True`, which is POSIX-only and silently
  ignored on Windows, so "detached" daemons were never detached.
- The `pkill` sweeps matched the literal path `tools/musicgen_generate.py`,
  which never matches a Windows command line using backslashes.

**Verified positively, not just by absence of a crash:** a spawned process
matching the driver pattern flips `e2e_alive()` False to True to False across
spawn and kill, and `pids_listening_on_port` correctly identified a foreign
process holding the GUI port and terminated it.

## [6] Worth reporting: Popen pid is not the real pid on Windows

**Status:** OPEN, informational
Verified: a child reporting `os.getpid()` = 13880 while
`subprocess.Popen(...).pid` returned 11588. A venv `python.exe` re-execs on
Windows, so the Popen value names a process that has already gone.

Several `.pid` files here are written from the Popen value
(`ASSETS/full_auto_keepalive.pid`, `ASSETS/full_auto.pid`), so **any liveness
check that trusts them reads the wrong process** and will conclude a live daemon
is dead, then start a duplicate. The pattern-based probes in [5] do not have
this problem, which is why they are used for the alive checks. The pid files
themselves were left alone; fixing them means having the child report its own
pid.

## [7] TEST: preclean fixture tripped two disagreeing completeness authorities

**Status:** FIXED
**Not a product bug** on this path, but it exposes a design smell worth raising.

`test_homunculus` stamped `audio_preclean` complete by writing
`preclean/provider.json`. That satisfies `prepare_outputs_present()`, which
accepts **any** `preclean/*` artifact, but `done_authority` /
`reconcile_stage_done_marker` accept only `preclean/isolated.wav` or
`preclean/skip.json`. The second authority judged the marker hollow and
**unlinked it mid-dispatch**, so the next call died on the seed-order gate with
`complete audio_preclean before running boundary_detection` instead of the
expected error. The fixture now writes `skip.json`, which is what a real
operator skip writes (`ensure_preclean_skipped`).

**The smell, worth a look upstream.** The traced chain is

```
dispatch_stage -> _seed_prereq_block -> _earliest_incomplete_seed_stage
  -> _seed_order_skip_stage -> heal_or_refuse_mark -> reconcile_stage_done_marker
```

so a **liveness query deletes a stage marker as a side effect**, and two
functions disagree about what "complete" means. Real runs are safe because a
real skip writes `skip.json`, but any future third way of finishing preclean has
to satisfy both definitions or the run will silently rewind.

## [8] TEST/PLATFORM: three more Windows-only failures

**Status:** FIXED (commit `ed3eb66`)

| Test | Cause | Fix |
|---|---|---|
| `test_r_workflow_residual::test_r_wf_musicgen_stub_forbidden_via_verify` | executed `tools/verify_full_auto_env.sh` directly; Windows cannot exec a `.sh` (WinError 193) | resolve `bash` and invoke through it, skipping with a reason if absent. The script itself needed no change. |
| `tests/rstm/test_rstm_matrix::test_rstm_execute_all_cells` | `persist_result` truncated cell ids to a flat 180 chars, which still overflows the Windows 260-char path limit in a deep checkout, surfacing as `FileNotFoundError` from `write_text` | budget the stem against the real `CELLS_DIR` length and append a sha1 prefix when the readable part is cut, so cells cannot collide. Also pinned `encoding="utf-8"`, since notes carry non-ASCII and the default here is cp1252. |
| `test_creative_delivery::test_hydrate_cue_segments_from_cue_ids` | ordered `seg_001..seg_003` but the fixture seeds only `seg_001`, so sanitize dropped the rest and every cue hydrated to the first selected segment | seed the three segments the test actually orders |

## [9] Not defects: three tests this machine cannot satisfy

**Status:** converted to explicit skips with reasons, rather than left failing.

- `test_musicgen_gpu_realworld::test_shipped_defaults_gpu_large_first_ladder`
  asserts `effective_musicgen_device() == "mps"`. An Apple-hardware regression
  lock; it cannot hold on CUDA. Now skips unless `is_apple_silicon()`, so the
  lock still bites on macOS.
- `test_hosted_vo_real_exec_met_bugs::test_real_exec_hosted_vo_sufficiency_report`
  asserts over four `exec_1318x` runs that are **operator run data under a
  gitignored tree** and are not in this checkout. Now skips only when all
  candidates are absent; present-but-insufficient still fails, which is the
  case the test was written to catch.
- `test_api_consent_flow::test_execute_not_blocked_by_consent` needs a
  configured `OPENAI_API_KEY`. Without one the server reports
  `needs_api_consent` for missing credentials, which the test cannot
  distinguish from consent genuinely blocking execution. Skips when no key is
  set.

Side note, worth a glance: the server signals *missing credentials* through
`needs_api_consent` alongside an error reading
`Missing credentials for: OpenAI (LLM)`. If the GUI prompts for consent on that
flag, it will ask the operator to consent when the real problem is an unset key.

## [10] Housekeeping: my own probe runs polluted ASSETS/executions

**Status:** FIXED (cleaned up)
Five `exec_00N_*` dirs appeared under `ASSETS/executions/`. **Not a test bug:**
`conftest.py` has an autouse `redirect_executions_root` fixture that sandboxes
executions during pytest. They came from my manual probe scripts, which ran
outside pytest and so got no fixture. Removed, because
`newest_incomplete_run()` is used by the daemon and a stale fake run could
hijack a real Full-auto launch.

## [11] Keyless stub traversal: how far the pipeline actually gets

**Status:** tool built (`tools/stub_pipeline_smoke.py`), 3 product bugs found
and fixed from it.

```bash
python tools/stub_pipeline_smoke.py --audio <real_speech.wav> --keep
```

`src/interview_mux/stub_llm.py` answers every OpenAI call from the JSON schema
the stage itself asked for, so replies are structurally valid by construction.
Verified against the repo's own contracts: **all 43 schemas** in
`docs/cross-cutting/json-schemas/composed` generate instances that pass
`jsonschema`. Interception is at the client, so prompt assembly, model
resolution, envelope normalisation, schema validation, the retry ladder and call
recording all still run for real. The hook is one env-gated line, so with
`MUX_STUB_LLM` unset production is byte-identical.

The driver reports **three** outcomes, not two. A stage that returns cleanly
without writing its primary artifact is `NOOP`, not `ok`. That mattered
immediately: on the first run **12 stages** returned cleanly having written
nothing, and counting those as passes would have been pure false confidence.
It also signs off operator gates through the real sign-off functions
(`mark_transcript_review_complete`) rather than forging `.stage_done` markers,
then re-runs the halted stage, which is what an operator does after acting.

### Result, reproducible

**14 of the first 15 stages write their artifacts, with real work:**

| # | Stage | Note |
|---|---|---|
| 1 | `audio_preclean` | DeepFilterNet on CUDA |
| 2 | `ingest` | |
| 3 | `transcribe` | real faster-whisper, ~31 s |
| 4 | `transcript_review_build` | halts on the G0 gate, passes on the post-sign-off retry |
| 5 | `audio_probe_build` | ~299 s, the slowest stage by far |
| 6-13 | acoustic profile, spine, speaker roles, topology, content context, talking points, ideal cuts propose + materialize | |
| 15 | `segment_classification` | |

**Stage 14 `boundary_detection` is the ceiling.** It raises
`LoudStageFailure: coarse_or_invalid_segmentation`. That is the product working
correctly: the gate computes `reject` from coverage ratio, mean segment duration
and validity, and schema-shaped boundaries cannot satisfy a real segmentation.

Raising the stub's array fan-out to 8 rows was tried and **made things worse**:
it did not help stage 14 (its reject is computed from coverage, not row count)
and it broke stage 15, which passes with 1. `MUX_STUB_ARRAY_ITEMS` keeps the knob
for probing a single stage, default 1.

### The honest limit

Getting past stage 14 means deriving a real segmentation inside the stub, which
would mean testing the fake rather than the pipeline. Everything downstream of it
(including the whole 40-60 band reported upstream) needs a real key. What the
traversal can still do once a key exists is bisect: run with `--from` to re-enter
at any stage without redoing the 299 s probe.

### Product bugs this found

1. **`transcribe` refused any non-Apple host.** `stages/transcribe_local.py`
   opened `if not is_apple_silicon(): raise`, a blanket hardware gate predating
   any non-MLX backend. faster-whisper was installed and working and the stage
   refused anyway. Now gates on `speech_available()`, the actual requirement.
2. **MAX_PATH inside the pipeline.** `talking_points_compose` died on
   `FileNotFoundError` writing a staged volley pack, because
   `ASSETS/executions/<run>/.pending_writes/<stage>/mastering/homunculus/volley_packs/<stage>_<hash>.json.tmp`
   measures **266 chars** from this checkout, 6 over the limit, with the parent
   directory already at 217. **One path length caused 18 cascading failures.**
   `executions_root` is already a config key, so `app.local.json` points it at
   `C:/mux-local/executions` (~170 chars), which also keeps run WAVs out of
   OneDrive.
3. **`executions_root` did not actually work outside the repo.** Having moved
   it, `RunContext.init_run_meta` raised
   `ValueError: ... is not in the subpath of ...` from
   `self.run_dir.relative_to(self.root)`. So the config key advertised
   flexibility it did not support, and its main purpose (moving heavy run
   artifacts off the repo volume, or onto a shorter path on Windows) was
   unreachable. Now falls back to the absolute path. **Worth reporting: this is
   platform-independent and would bite anyone on the Mac who tried to move
   executions to an external drive.**

### Note on the traversal's own fidelity

It forces all 72 stages in order. The real `run_analysis` / `run_delivery` skip
some conditionally, so a few reported failures are artifacts of forcing a stage
the pipeline would not run at that point. `content_brief_reanchor` is the clear
example: it refuses with "timeline artifacts exist after G0 ... instead of
rewinding the classified tape", which is a correct guard being tripped by the
traversal, not a bug.

## [12] TEST: fake-repo fixtures inherited the operator's local config

**Status:** FIXED
**Found by:** moving `executions_root` off the repo volume for [11], which
turned 9 tests red.

Five fixtures build a throwaway repo with
`shutil.copytree(repo/"config", tmp_path/"config")` then
`INTERVIEW_MUX_ROOT=tmp_path`. That copies the whole config tree, **including the
gitignored `config/app.local.json`**. Any absolute path in the overlay then
resolves outside the sandbox, so the fixture's own seeded runs became invisible
and `/api/runs` returned an empty list.

It only worked before because `executions_root` happened to be the *relative*
`ASSETS/executions`, which resolved against the patched root by luck. So test
isolation silently depended on nobody ever setting an absolute path in config, and
the documented purpose of `executions_root` is precisely to set one.

Replaced all five with a shared `run_fixtures.copy_shipped_config()` that
excludes `app.local.json`, so these tests use the **shipped** config and are
independent of whatever the operator has configured locally.

Worth reporting: this is not Windows-specific. Anyone on the Mac who pointed
`executions_root` at an external drive would have hit the same 9 failures.

## [13] PRODUCT (headline): the staging flush deleted every artifact a stage owns but does not surface

**Stage / area:** `write_staging.flush_stage_writes`, all stages
**Status:** FIXED
**Severity:** high. It destroys the forensic trail you need to debug a run, which
is very likely why the stage 40-60 failures have been hard to diagnose.
**Platform-independent.** Nothing about this is Windows-specific.

**Found by:** the one real-API run.

### What happens

`flush_stage_writes` decided what to commit with
`operator_visible_staging_path(stage_id, rel)`. That predicate answers *"does the
GUI list this artifact?"*, resolved against `StageInfo.artifacts / editable /
audio_outputs` in `web/stages.py`. Using a **visibility** predicate as a
**persistence** gate means every artifact a stage legitimately owns but does not
surface to the operator was silently discarded on flush.

The code knew: the branch carried the comment *"exec_11871 lost
publish/episode.json and the ship-time listen_delight_audit this way"*, and
`vo_pickup` already had a special case bolted on for the same class of bug
(exec_13177). The mitigation was to log it loudly and drop it anyway.

### Measured on the real run (72 stages, one pass)

**64 staged writes dropped across 15 stages**, 29 distinct paths. Of those 29:

- **3** existed on disk anyway, written by another route
  (`homunculus/ledger.json`, `homunculus/memory.json`,
  `understanding/analysis_state.json`). Worth stating plainly: these were **not**
  lost, and an earlier reading of mine that the ledger and memory were being lost
  was wrong.
- **26 were genuinely missing from disk**:
  - **all 19 LLM call records** (`understanding/llm_calls/**/01_primary.json`
    and `.md`), the complete audit trail of every LLM call in the run
  - **all 7 volley packs** (`mastering/homunculus/volley_packs/*.json`), the
    context packets sent to the model
  - `segments/boundary_review_queue.json`, an operator-facing review queue
  - `transcript/diarization_repairs.json`
  - `understanding/ideal_cuts_selection_seed.json`

So a failing run deletes exactly the evidence needed to explain it.

### Fix

Ownership, not GUI visibility, now gates persistence. `flush_stage_writes` asks
`write_permitted(ctx, rel, stage_id, role="producer", verb="persist")`; an owned
path is committed and logged as undeclared (so a missing `StageInfo` entry is
still visible), and only an unowned path is discarded. On any error the ownership
check returns False, so an undecidable path keeps the old stricter behaviour.

`operator_visible_staging_path` keeps its real job, deciding what
`list_stage_staging_paths` shows the operator.

**Verified:** re-running the traversal, dropped writes went **64 to 0**, 29 writes
committed, and the `llm_calls` records and volley packs are on disk. Files
stranded under `.pending_writes` fell from 32 to 24 (the remainder belong to
stages the shorter traversal did not reach).

### One test had to change

`test_i54_dropped_owned_staging_path_is_logged` asserted the drop plus the
warning. Its own docstring said *"must announce itself instead of losing bytes
quietly"*, and the file it used is `mastering/listen_delight_audit.json`, the
exact artifact exec_11871 lost. The author knew bytes were disappearing and
settled for a warning. Renamed to
`test_i54_undeclared_owned_staging_path_is_committed_not_dropped`, now asserting
the bytes land **and** the undeclared status is still reported, which serves the
original intent rather than weakening it.

## [14] The single real-API run: how far it got

One run, 72 stages, `gpt-5.6-terra` / `gpt-4o-mini`, 203 s public speech sample,
diarization off. Zero-token `GET /v1/models` beforehand confirmed all three
configured models are available, so nothing was wasted on a model error.

**Stages 1-15 all passed with real work**, which is past the stub's ceiling:

| # | Stage | Time |
|---|---|---|
| 1-2 | `audio_preclean`, `ingest` | 4 s |
| 3 | `transcribe` | 38 s |
| 4 | `transcript_review_build` | halted on G0, passed on the post-sign-off retry |
| 5 | `audio_probe_build` | **299 s**, the slowest stage in the pipeline |
| 6-9 | acoustic profile, spine, `speaker_roles` (29 s), topology | |
| 10-13 | `content_context` (26 s), talking points, ideal cuts propose + materialize | |
| 14 | **`boundary_detection`** | **14 s, passed.** The stub could never clear this; a real model produced a valid segmentation. |
| 15 | `segment_classification` | 20 s |

**17 of 72 wrote artifacts, 36 no-op, 19 failed.** The first failure is #16
`content_brief_reanchor`, and everything after it is one cascade.

### What blocks #16 onward

`_refuse_delivery_timeline_rewind` refuses `content_brief_reanchor` with
"timeline artifacts exist after G0", which is **correct**: the artifact already
exists and rewinding the classified tape is exactly what it should prevent. It
marks the stage complete and raises, by design.

But the traced consequence is that `content_context` ends **unmarked** while its
artifact `understanding/content_brief.json` sits on disk, and 8 stages then fail
with `seed order: complete content_context`, with `recovery_controller` looping on
`resume=content_context`. Two stages share that primary artifact
(`_PRIMARY_BY_STAGE` maps both `content_context` and `content_brief_reanchor` to
it), which is what makes the marker ownership ambiguous.

Reproduced offline: restoring the pre-#16 state and re-running just that stage
flips `content_brief_reanchor` from `done=False` to `done=True` **while raising
RuntimeError**. Traced path:

```
run_single_stage -> dispatch_stage -> _refuse_delivery_timeline_rewind
  -> heal_or_refuse_mark -> mark_done      ... then raise
```

**Status: OPEN.** Not yet fixed, because whether a refused stage should hold the
shared marker is a design question for upstream, not something to guess at. Note
also that a linear traversal forces stages the real `run_analysis` would skip
conditionally, so part of this cascade is the traversal's own doing rather than a
pure defect.

**Next run should start at `--from content_brief_reanchor`**, which skips the
299 s probe and the 15 stages already proven.

## [15] PRODUCT: safe pruning did not recognise an oversized-request 429

**Stage / area:** `safe_pruning.is_context_length_error`, every LLM stage
**Status:** FIXED
**Found by:** the second real run, which died here after 18 stages.

`boundary_topic_resplit` failed with

```
429 - Request too large for gpt-4o ... on tokens per min (TPM):
Limit 30000, Requested 30233. The input or output tokens must be reduced.
```

233 tokens over. The repo already has a safe-pruning ladder for exactly this
("the input or output tokens must be reduced" is a description of what pruning
does), but `is_context_length_error` only matched `context_length`,
`maximum context`, `too many tokens`, `max_tokens` and `context window`. None
appear in that message, so pruning never engaged and the stage hard-failed
instead of shrinking the request and retrying.

Now also matches `request too large` and `tokens must be reduced`. Deliberately
narrow: a plain requests-per-minute "rate limit reached" needs backoff, not a
smaller prompt, and is asserted **not** to match.

Three tests pin all of it, including the real 429 text from the run.

**Note:** the 30k TPM cap is an account tier limit, not a defect. With
pruning engaged the run should get past it, but `boundary_topic_resplit` is the
one stage routed to `gpt-4o`, and a 30k-token request against a 30k cap has no
headroom. Routing it to the flagship tier, or raising the account limit, is the
durable fix.

## [16] Second real run: 18 stages complete, both fixes confirmed live

Fresh run with the staging-flush and rewind-lock fixes in place, driven through
**`run_analysis`, the real orchestrator**, not the linear walk.

**18 stages marked complete**, versus effectively 15 before:

```
audio_preclean, ingest, transcribe, transcript_review, transcript_review_build,
audio_probe_build, source_acoustic_profile, interview_spine_build, speaker_roles,
source_topology_build, content_context, talking_points_compose,
ideal_cuts_propose, ideal_cuts_materialize, boundary_detection,
segment_classification, content_brief_reanchor, framing_posture_decide
```

Both fixes verified in a live run rather than only in a unit test:

- `content_context` **stays marked** through the `content_brief_reanchor`
  rewrite, so the seed-order cascade that blocked 8 stages is gone.
- `framing_posture_decide` and `content_brief_reanchor` now complete, where
  before they were blocked or reported as failures.
- Staged writes are committed: the log shows `committed but undeclared` for
  `llm_calls`, volley packs, ledger and memory instead of `dropped`.

Stopped at `boundary_topic_resplit` on the account TPM limit in [15].

**Also learned: the linear walk overstates failures.** `content_brief_reanchor`
is reported FAIL by the linear traversal and is correctly *skipped* by the real
orchestrator via `may_skip_as_complete`. Use `--orchestrated` to answer "does an
execution work" and the linear walk only to answer "does this one stage work".

## [17] PRODUCT: the seed walk stalled on a prerequisite it could satisfy

**Status:** FIXED. Took the walk from **18 stages to 30** with no API cost.

A stage can be invalidated mid-walk by an upstream rewrite:
`boundary_topic_resplit` rewrites `segments/boundaries.json`, which correctly
makes `framing_posture_decide` stale. `walk_seed_agenda` then hit
`seed order: complete framing_posture_decide before running
vernacular_segment_sanitize` and re-raised, stopping the entire analysis.
`recovery_controller` had already worked out the same answer
(`resume=framing_posture_decide`) and was ignored.

The walk now parses the prerequisite out of that error, runs it, and retries the
stage once. Each prerequisite is auto-run at most once per walk, so a genuinely
broken stage cannot ping-pong forever.

The auto-run fired exactly once in practice, for `mastering_research_routing`
before `mastering_shape_agenda`, and unblocked nine stages behind it.

## [18] TOOL BUG, mine: resuming a run repointed it at a 12-second tone

**Status:** FIXED.
`--run-id` without `--audio` regenerated the synthetic tone and re-registered it
through `init_run_meta`, so a resumed run silently lost its real source. That is
why the voice reference gate kept reporting "approved reference WAV missing or
too short": every speaker sample was being cut from 12 seconds of sine wave.

**Two earlier resumed runs were affected**, so any conclusion drawn from their
later stages was drawn against a tone rather than the interview. Resume now
keeps the run's existing source. Recorded because it invalidates part of what I
reported from those two runs.

## [19] CORRECTED: the high-gap stop is not a design dead end

**Status:** superseded by [21]. Keeping the correction visible because I reported
the wrong conclusion first.

I originally recorded this as an unresolvable tension between two intentional
rules: `gap-framing-compose.system.txt` says

> Do **not** break long same-speaker monologues or force VO before nearly every
> native just to hit a coverage percentage.

while `stage_completion._high_gap_unframed_incompleteness` refuses to complete
`gap_framing_compose` while **any** high-severity gap lacks an interviewer line.
I concluded that no amount of retrying could resolve it.

**That was wrong.** The product has a designed remedy:
`recovery_controller.playbook_high_gap_unframed` calls
`seed_uncovered_high_gaps_deterministic`, which fills uncovered high gaps without
the model. It simply could never execute, which is [21]. The real reason the run
still stops is [22], and it is about the input, not the rules.

## [20] Where the pipeline actually reaches, measured

| Configuration | Stages complete |
|---|---|
| Before any of this work | 15, then a cascade |
| Stub, orchestrated, all fixes | 30 |
| **Real API, orchestrated, all fixes** | **32 of 72** |

The real run clears two quality gates the stub cannot (`missing_framing`,
`mastering_plan_confirm`), which is the expected difference: schema-shaped output
satisfies structure, not judgement.

**Analysis is 35 stages, and 32 are complete**, so analysis is nearly finished.
The blocker is [19], which is a design question rather than a defect.

**Important limitation of this tool, not the product.** `--orchestrated` drives
`run_analysis` / `run_delivery` directly. It is **not** the self-healing path.
Full-auto (`MUX_FULL_AUTO=1 ./scripts/run.sh`, `tools/full_auto_driver.py`) wraps
those with the heal / remutate / re-execute loop that is supposed to recover from
exactly this class of stop. Judging "can this pipeline self-propel" really means
running Full-auto, which I have not done. My tool answers the narrower question
"does a straight execution get through", and that answer is currently 32 of 72.

## [21] PRODUCT: the high-gap heal playbook crashed instead of repairing

**Stage / area:** `recovery_controller.playbook_high_gap_unframed`
**Status:** FIXED
**Severity:** high for self-propulsion. The designed remedy for an entire error
class could not run. **Platform-independent.**

The playbook wrote `understanding/gap_report.json` with **no `stage_key`**.
`assert_gap_report_body_sole_writer` accepts only `gap_framing_compose` or
`nugget_layup_compose` as body writers, and seeding lines changes body text, so
the write was refused:

```
AuthorityDenied: authority_denied:persist:understanding/gap_report.json::
pre_soft_freeze:nugget_layup_compose (gap_body_writers:empty_stage_key)
```

`handle_stage_failure` calls this playbook with no staging context **and does not
guard it** (there is no enclosing try), so the denial propagated out of the
recovery path. The remedy for `high_gap_unframed` therefore crashed rather than
repairing, and a run could not self-heal past it.

**Fix.** The write now names its owner, derived by mirroring the check rather than
guessing, and keyed off the **prior on-disk doc**, because that is what the check
reads (`prior.get("nugget_layup_authority")`).

That distinction is the entire orphan-stamp case, and my first attempt got it
wrong: the repair exists to *clear* an orphan stamp, but the write is still judged
against the stamp still on disk, so layup must own it even though the repaired
body no longer carries the authority. `high_gap_heal_resume_stage` deliberately
pins compose there, so the two authorities disagree at exactly that point.

**Verified both ways:** on the real run the gate goes from blocking on `seg_009`
to `None`, with a deterministic `vo_seed_seg_009` line targeting it, and
`test_hg5_high_gap_heal_pin` is 12/12 including the orphan-stamp case.

## [22] The remaining stops are my test audio, not the pipeline

**Status:** not a defect. This is the honest explanation of where runs 1 and 2 end.

With the heal path fixed, `gap_framing_compose` re-ran and produced a line, which
was then rejected:

```
[HARD STOP] required gap VO blocked by spoken_copy_guard
(vo_preface_pendulum_open): spoken_repeated_sentence, no_grounded_fallback
```

The framing line restates a sentence already spoken on the tape. The guard is
right to refuse it. The reason it happens is the input.

### The transcript describes a conversation that does not exist

My test audio is a **single-speaker physics lecture**. Diarization is not
installed, so STT labels every word `spk_0` (verified directly). Then
`transcript_normalize._infer_turn_speakers` does this, by its own docstring:

> When STT returns one speaker, alternate spk_0/spk_1 on interview-style pauses.

It flips the label on any pause of 700 ms or more. The result in this run is a
**perfectly alternating** `spk_0, spk_1, spk_0, spk_1 ...` across 25 turns, which
no real conversation produces. Every downstream stage then reasons about a
two-person interview that is not on the tape:

| Segment | Label | Actual text |
|---|---|---|
| `seg_001` | `interviewer_question` | "Now I want to return to the conservation of mechanical energy" |
| `seg_009` | `interviewer_question` | "I will close my eyes. I don't want to see this. So please be..." |

Both are the lecturer. So `gap_framing_compose` is asked to write interviewer
framing for a speaker who does not exist, and anything it writes either does not
appear (high_gap_unframed) or restates the monologue (spoken_copy_guard). **Those
two guards are the pipeline correctly refusing fabricated input.**

### Worth reporting upstream anyway

The fabrication is **silent**. Nothing marks the transcript as "speakers inferred
from pauses, not diarized", and neither the downstream stages nor the LLM can
tell. On a real interview where diarization happens to be unavailable, the same
heuristic applies, and a 700 ms pause inside one person's sentence becomes a
speaker change. A provenance flag on the transcript, and a way for
speaker-dependent stages to see it, would make that failure legible instead of
mysterious. The heuristic itself is reasonable for its intended case and is not
something to remove.

### What this means for the next run

Runs on this audio will keep hitting content-quality guards, so **run 3 is being
saved**. The decisive input is a **real two-person interview**, plus diarization
installed so the speakers are observed rather than invented. Everything
mechanical is fixed and green; what remains is a data question.

## [23] PRODUCT: the approved voice reference was deleted by its own flush

**Stage / area:** `artifact_ownership`, `voice_reference.approve_voice_reference`,
`gap_vo_gates.vo_path_ready`
**Status:** FIXED
**Severity:** high. Terminal stop with no recovery path, on real audio.
**Platform-independent.** This has nothing to do with Windows.

### What happens

`approve_voice_reference` does the right thing in the right order: it builds
`understanding/speaker_samples/<sid>.wav`, checks the duration against
`min_reference_sec`, and only then writes a manifest naming that file. It
succeeds. Then the stage flushes, and the WAV is gone.

The cause is one missing row. `flush_stage_writes` commits a staged file only
when `write_permitted` says its stage owns the path. The catalog covers the
inputs to the approval but not its output:

| Path | Verdict |
|---|---|
| `understanding/voice_reference/*.json` | `operational`, committed |
| `understanding/voice_reference/clips/**/*.wav` | `operational`, committed |
| `understanding/speaker_samples/*.wav` | **`unknown_path`, dropped** |
| `understanding/speaker_samples/*.json` | **`unknown_path`, dropped** |

So the manifest survives and the audio it points at does not. The run carries an
approval for a file that no longer exists.

The catalog comment above those rows reads `exec_11871 unknown_path`, so this
exact class was already hit once and patched for the candidates and the
manifest. The approved output was missed.

### Why it could not recover

The failure surfaces much later, in a different stage, as:

```
SystemExit: Voice reference gate: approved reference WAV missing or too short for clone.
```

That state was a dead end by construction. `check_voice_reference_pending`
returns False as soon as an approval is recorded, so the GUI gate does not
reopen; `voice_reference_unusable` has no recovery playbook; and the delivery
path refuses to continue without a usable reference. Nothing in the product
could move the run forward, which is why it matters more than a missing file
normally would.

### Fix, two parts

**The row**, which is the actual defect: `understanding/speaker_samples/*.wav`
and `*.json` are now owned, so the approval and its audio commit together. A
paired test asserts they are *equally* persistable, because the asymmetry is
what caused the dangling approval, not the absence of either row on its own.

**A rebuild**, because the dead end deserves a floor:
`repair_unusable_voice_reference` rebuilds the reference from the candidates and
clips, which are separate artifacts and survive. `vo_path_ready` calls it once
before reporting `voice_reference_unusable`. It needs no operator, no model, and
no network.

The attempt is stamped in `run_meta.json`, not in a sidecar under
`understanding/`, because a new sidecar there would be another unowned path and
would be dropped by the same flush it exists to work around. One attempt per
run: a second failure is a real stop, not a retry loop.

### Verified

On the stuck real run (`exec_016`), with forensics mode off:
`approved_voice_reference_usable` False → True, WAV rebuilt at 11.62 s from the
surviving candidate clips, and `require_vo_path_ready` returns instead of
raising `SystemExit`. Pinned by `tests/test_voice_reference_survives_staging.py`
(7 tests, both halves).

### Note on the ownership seal

Editing the catalog is not free for runs already in flight. A run stamps
`artifact_ownership_matrix_version` into `run_meta.json` at creation, and
`check_matrix_version` then refuses staged writes when the live catalog hash no
longer matches:

```
matrix_version_mismatch sealed=f7ed6c12e24a56b5 live=b696b4a244024e05
```

That is the seal doing its job, protecting a run from contract drift, not a bug.
The consequence is worth stating plainly: **after any ownership change, existing
runs cannot be resumed and a fresh run is needed.** `MUX_FORENSICS=1` restamps
and is the documented escape hatch, but it is a forensics tool and this project
should not depend on it to function, so it was left off throughout. Both halves
of the fix above work with it off, because `operational` paths and
`mutate_run_meta` are not gated by the seal.

## [24] RECOMMENDATION: ffmpeg is an unchecked hard dependency

**Status:** guarded in the test suite; product change left to upstream.
**Platform-independent.**

`src/interview_mux/` shells out to a bare `"ffmpeg"` in **22 places**, starting in
`stages/ingest.py`, and nothing anywhere checks that it exists first. The only
`shutil.which("ffmpeg")` in the tree is in `podcast_rss/encode.py`, the optional
component.

When it is absent the operator gets this, from inside `subprocess`:

```
FileNotFoundError: [WinError 2] The system cannot find the file specified
```

That names neither ffmpeg nor the stage. It reads like a code defect. I lost real
time to it: six tests failed in a suite that had been fully green, and the cause
was that the shell running pytest had been opened before ffmpeg was installed, so
it held a stale PATH. Nothing in the output pointed at the environment.

**Done here:** `tests/conftest.py` now checks `ffmpeg` and `ffprobe` in
`pytest_configure` and aborts the session with one actionable line naming the
missing binary and how to install it, instead of the same opaque error stapled to
each affected test. It is a hard failure, not a skip, on purpose: a suite that
went green without ffmpeg would be reporting health it never verified.

**Suggested for the product:** the same check once at run start. Because ingest is
stage 1 the run does fail fast today, so this is about the message rather than
the timing, but it is the difference between "install ffmpeg" and a debugging
session. I did not add it, because it means touching the ingest path and the
value is a better error string, which is not worth the risk of me changing a
stage that currently works on the Mac.

### The general shape, which matters more than ffmpeg

This is the class of failure worth watching in this project: **the suite and the
pipeline both depend on ambient state that nothing declares.** ffmpeg was the one
that bit. The others I checked and found already handled:

| Ambient dependency | Status |
|---|---|
| `ffmpeg` / `ffprobe` on PATH | **was unguarded**, now aborts with a clear message |
| Outbound network | blocked by an autouse fixture, localhost allowed for TestClient |
| `config/app.local.json` (my gitignored host overlay) | fixtures copy shipped config only, and the one test that reads it branches on absence |
| Random test ordering | not a risk: `pytest-randomly` is not installed, order is deterministic |
| Absolute `executions_root` | redirected per test by an autouse fixture |

## [25] MY BUG, worth flagging because it would have wrecked the PR

**Status:** FIXED and guarded.

Every file I edited through a Python script came out CRLF. `pathlib.write_text`
opens in text mode, so on Windows it translates newlines on the way out, and a
file that was LF is rewritten wholesale.

Python does not care, the tests pass, and the pipeline runs, so nothing surfaced
it. The cost is entirely in review: **33 of 1308 tracked `.py` files** had been
flipped, each showing as roughly 100% rewritten. A 20-line fix arrived inside a
2800-line diff. Since the point of this work is to hand changes upstream as PRs,
that alone would have made them unreadable.

**Fixed:** `.gitattributes` pins `*.py` and `*.md` to LF, and
`git add --renormalize` corrected the 36 affected files. Verified
content-identical: `git diff --cached --ignore-cr-at-eol` reports only
`.gitattributes` itself. `test_committed_python_has_no_crlf` asserts on committed
blobs, mirroring the shell-script guard that already existed, so a future Windows
edit cannot reintroduce it silently.

Still deliberately not a repo-wide `* text=auto`, which would renormalise
thousands of unrelated files and bury real work, which is the reason the existing
`.gitattributes` comment gives for avoiding it.

**Why this is in the bug report:** nothing here is a defect in the upstream code.
It is in this document because the diffs he receives were shaped by it, and
because the same trap waits for anyone who edits this repo from Windows.

## [26] Run and suite state at handover

**Test suite:** 6683 passed, 21 skipped, 0 failed. Run twice consecutively with
identical results, on Windows with CUDA, with `MUX_FORENSICS` unset.

**The real-podcast run (`exec_016`, 59.5 min source, 73 min wall):** reached 32
of 72 stages, with real Sortformer diarization (7931 words, 216 turn switches,
spk_0 1321 s / spk_1 1606 s). Its voice-reference blocker is now cleared:
`vo_path_ready` returns `(True, '')` where it previously raised `SystemExit`.

**That run cannot be resumed, and should not be.** Adding the ownership rows
changed the catalog hash, and a run seals the hash it started with:

```
matrix_version_mismatch sealed=f7ed6c12e24a56b5 live=b696b4a244024e05
```

Staged writes in `exec_016` are therefore refused. `MUX_FORENSICS=1` would
restamp it, and it was left off on purpose: the pipeline should not need a
forensics tool to function, so relying on it would hide rather than answer the
question of whether this works. The correct next step is a fresh run, which
starts from the corrected catalog and never encounters the bug in [23] at all.

## [27] PRODUCT: the stale-process sweep was Windows-blind, and could kill its own shell

**Stage / area:** `scripts/run.sh`, `interview_mux.proc_compat`
**Status:** FIXED
**Severity:** medium for the Windows gap, **high for the second half**, which is
platform-independent in principle and live on Windows in practice.

### The Windows gap (P7)

`run.sh` cleaned up a stale server and orphaned workers with:

```bash
if command -v lsof >/dev/null 2>&1; then  ... lsof -ti "tcp:${WEB_PORT}" ...
if command -v ps   >/dev/null 2>&1; then  ... ps -ax -o pid=,command= | grep ...
```

The port sweep was simply skipped on Windows, so a stale server on 8765 had to be
killed by hand before every launch. The `ps` sweep was **worse than skipped**: Git
Bash ships a `ps`, so the guard passed, but it does not report native Windows
command lines, so the greps matched nothing while the block looked like it had
run. A silent no-op is harder to notice than an absent one.

Both now go through `tools/cleanup_stale_processes.py`, which uses
`proc_compat`. macOS is unchanged, because `proc_compat` deliberately tries
`lsof` and `pgrep` first on POSIX even when psutil is installed, and
`terminate_pids` sends SIGTERM there, which is what the inline `kill` did.

### The part worth upstream attention: a sweep that killed its own caller

Wiring that up surfaced a real defect in `proc_compat`. `_filter_pids` excluded
only `os.getpid()`, never the ancestors. So the first run of

```bash
python tools/cleanup_stale_processes.py --pattern interview_mux.stage_worker
```

killed my shell. The pattern is an argument, so it appears in the **invoking
shell's own command line**, that shell matched, and on Windows `taskkill /T`
takes the whole tree including the sweeper. The symptom was the tool printing
nothing at all and the session dying, which reads like a crash rather than the
sweep doing exactly what it was told.

`pgrep -f` has the same exposure on POSIX. The shell code this replaced dodged it
only because its patterns were literals in the script, never arguments, which is
luck rather than design.

This is not hypothetical for the existing product: `full_auto_driver.py` is
launched as `python tools/full_auto_driver.py`, so its parent shell's command
line contains `full_auto_driver.py`, and any `kill_matching` on a pattern like
that would target the parent.

**Fix.** `_filter_pids` now excludes the caller's whole ancestor chain, resolved
from a pid/ppid map, with cycle detection and a depth cap. It degrades safely:
self and the direct parent stay protected even when the process table cannot be
read. `pids_matching(..., exclude_self=False)` still returns the unfiltered list
for a caller that genuinely means it.

**Trade-off, stated plainly:** if a flow ever needs to stop a server that is
genuinely its own ancestor, the pattern sweep will no longer do it. That was
already self-destructive on Windows, and the explicit route through
`terminate_pids` on a known pid remains open.

Pinned by `tests/test_proc_compat_never_kills_its_caller.py` (6 tests, both
platforms via patched `IS_WINDOWS`) plus two run.sh parity tests.

## [28] PRODUCT: narrative constraints named a segment a re-split had merged away

**Stage / area:** `talking_points_authority.narrative_from_talking_points`
**Status:** FIXED. **Platform-independent.**

`chapter_close_hitch` re-runs `boundary_topic_resplit` inside its inner walk. On
the second pass resplit honours seams that `connector_fuse_pass` locked on the
first pass, and merges a segment away (4 became 3 here). `mastering_plan.json`'s
`ordered_segment_ids` and the materialized cuts still listed the old id, and the
deterministic narrative builder copied them verbatim:

```
ordering_constraint segment seg_004 not in manifest; heal_success:pre_flush_soft_refused
```

The pre-flush barrier was right to refuse; the plan named a segment that did
not exist. The builder now receives the live manifest ids from its ctx-holding
caller and merged-away ids drop out. Relative order of the survivors is
unchanged. Callers without a manifest keep the exact old behaviour.
Pinned by `tests/test_narrative_constraints_follow_manifest.py`.

## [29] PRODUCT: the commit barrier validated the file being replaced, not the replacement

**Stage / area:** `artifact_cross_validate._committed_json`
**Status:** FIXED. **Platform-independent.** This one is subtle and worth reading.

`_committed_json` read the committed artifact first and used the stage's
pending write only when no committed file existed. That is backwards for any
stage that *replaces* an artifact: during its write approval the stale
committed copy is what got validated, while the clean replacement sat in
staging waiting on that very check.

Concretely, after [28] was fixed, `_validate_post_narrative` still failed with
`narrative segment seg_004 not in manifest`. The staged narrative plan was
clean. The committed one, from the first pass, was not, and it was the one
being judged, so the fix could never land. The docstring said "overlay pending
writes during write approval"; the code did the opposite whenever a file
already existed.

The overlay is only active during that stage's write approval and only for
paths that stage has staged, so it is precisely "what is about to land". It
now wins. `_committed_exists` was already correct (either copy counts).
All 93 existing tests around the overlay still pass;
`tests/test_cross_validate_overlay_wins.py` pins the precedence.

**Effect on the keyless traversal:** 38 to 41 stages; `chapter_close_hitch` and
`narrative_arc_plan` complete for the first time.

## [30] PRODUCT: the high-gap remedy ran only after the stage had already failed

**Stage / area:** `stages/gaps.py`, `recovery_controller.playbook_high_gap_unframed`
**Status:** FIXED. **Platform-independent.** First real-API run to complete analysis.

The prompt tells the model not to force lines into a monologue; the completion
check refuses while any high-severity gap lacks an interviewer line. The
designed remedy is deterministic seeding, and after entry 21 it worked, but
two things kept it from ever helping a run:

1. The playbook seeded and returned without marking. The walk re-dispatched the
   pinned stage, and `may_skip_as_complete` is `land_honest`, which needs a
   seed-complete marker; the stage had just failed, so there was none. Compose
   re-ran, the model's fresh body again had no line for the high gap, the seed
   was overwritten and the playbook seeded again. On the real 120-second run:
   `identical_failure x2` with `vo_seed_seg_003` on disk the whole time. The
   playbook now calls `heal_or_refuse_mark` on the pinned stage, which marks
   only when the whole stage satisfies completion and refuses otherwise.

2. Even then the remedy ran from the recovery path, after compose had raised,
   so each orchestrated pass ended there although the next pass would have
   skipped straight through. `_heal_gap_framing_compose_if_complete` now seeds
   before `heal_or_raise`. If seeding cannot clear it, it raises exactly as
   before; nothing is greenwashed.

The traversal driver also retries a phase when the previous pass marked more
stages complete, since the recovery path heals after a stage raises.

**Result:** on the real API, analysis went HALT, FAIL, FAIL, OK across retries
with 30 and then 2 more stages landing each time, and completed all 35
analysis stages for the first time. HG-5 suite: 15 tests.

## [31] OBSERVATION: boundary quality has a hard floor of 8 segments

**Status:** not changed. For upstream to decide.

`evaluate_boundary_quality` sets `expected_min_segments = max(8, ...)`, and
the fine-grained exemption from the coverage rule needs at least 8 rows. On a
120-second excerpt the real model produced 3 boundaries covering 71.5% of the
tape, under the 0.85 minimum, so `boundary_detection` recorded
`coarse_or_invalid_segmentation` and the first delivery-critical stages
refused with `LoudStageFailure: Boundary detection produced unsafe cuts`.

A 6-minute excerpt (41 turn switches, both speakers balanced) fails the same
way: the model produced 5 boundaries of 62, 3, 90, 113 and 17 seconds,
covering 79.2% of the tape, and with `expected_min_segments=8` there is no
fine-grained exemption, so `reject=True`. Analysis still completes on both
excerpts; it is the first delivery-critical stage that enforces the flag.

For a real episode this is the right rule, and the 59-minute source passes
it. It does mean any tape under roughly ten minutes cannot reach delivery
regardless of model quality, which matters for smoke tests, not production.
A duration-scaled floor would be the product fix if short sources are ever
meant to be supported; otherwise, end-to-end checks need a full episode.

## [32] OBSERVATION: the product's own `_meta` stamp fails its own schemas

**Status:** FIXED (see the note at the end of this entry).

```
fingerprint flush skipped for understanding/sonic_context.json:
schema validation failed: (root): Additional properties are not allowed ('_meta' was unexpected)
```

Shared-path writes stamp `_meta` into artifacts; the artifact schemas declare
`additionalProperties: false`; the fingerprint validator therefore refuses
them and skips the fingerprint flush. The artifact itself still lands, so this
is a warning per artifact, not a stop. Either the schemas should allow `_meta`
or the validator should strip it before validating.

**Fix (macOS one-hour runs, where it hit sonic_context, delivery_brief and
soundscape_policy on every run):** `prompt_validation._validate_dict` validates
without `_meta` when the schema is closed (`additionalProperties: false`) and
does not declare `_meta`. Any other extra key still fails. A schema that
declares `_meta` still validates it. The fingerprinted copy now lands. Tests:
`tests/test_meta_stamp_passes_artifact_schemas.py`.

## [33] PRODUCT: the boundary-quality floor made short sources untestable

**Stage / area:** `stages/segmentation.evaluate_boundary_quality`
**Status:** FIXED. **Platform-independent.** Supersedes the "for upstream to
decide" in entry 31.

The floor of 8 boundaries applied regardless of tape length, and the coverage
rule only relaxes for maps at or above the floor. A 6-minute two-speaker
excerpt with 5 sensible boundaries at 79% coverage, and a 2-minute one with 3
at 71.5%, were both flagged `coarse_or_invalid_segmentation`, and every
delivery-critical stage then refused. The only way to test delivery was a full
episode at full token cost, which is the wrong incentive for a debugging loop.

The floor now scales with duration below a 10-minute full-episode length and
never drops under 2. At and above that length it is still exactly 8, so real
episodes are judged by the rule they always were. The critical coverage rule
(0.70) still applies at any length. Both knobs are configurable
(`boundary_quality_segment_floor`, `boundary_quality_full_episode_ms`).
Pinned by `tests/test_boundary_floor_scales_with_duration.py`: both real
excerpts accepted, an hour with three slabs still rejected, a short tape under
critical coverage still rejected.

## [34] TEST: one parallel-only flake

`tests/test_gap_framing_gates.py::test_small_batch_llm_fail_refuses_hollow_done`
failed in 2 of 3 full-suite runs under `-n 12`, both times while another heavy
process was running. It passes serially (3 of 3), and passes under 12 workers
when run with its neighbouring modules (68 tests, twice). The assertion is a
regex on the raised message (`partial|incomplete|gap_report`), so under load a
different error is surfacing first; the message was not captured. Worth a
look if it recurs; the serial suite is the number to trust for that test.

## [35] PRODUCT: the keeper air trim rewrote the source boundary map

**Stage / area:** `segment_fuse.rerun_air_bounds_on_fused`
**Status:** FIXED. **Platform-independent.** The real reason short sources
could not reach delivery even after entry 33.

After a connector fuse, the fused slab is trimmed to its keeper air bounds,
and that trim was written into both `segments/manifest.json` and
`segments/boundaries.json`. The boundary map is the *source* segmentation:
`evaluate_boundary_quality` measures coverage on it, and `ideal_cuts` binds
it from source cuts for that reason. Traced on a real 6-minute run with
`MUX_TRACE_WRITES=1`:

```
connector_fuse_pass  apply_connector_fuses    seg_001 (0, 90370)      16 rows -> 5
connector_fuse_pass  rerun_air_bounds_on_fused seg_001 (46270, 62280)  both files
```

Coverage fell to 25% and every delivery-critical stage refused with
`Boundary detection produced unsafe cuts`. On the previous run the same path
shrank a whole-tape 0..357s survivor to a 90-second window. On a full episode
most rows are never fused, so the loss stayed invisible; on a short tape one
high-value cluster can swallow the whole map.

Two things about that cluster fuse are worth upstream's attention, though I
left them alone: it bypasses `max_fused_duration_ms` (25 s) and
`max_fused_members` (3) via the forced path, which is how a whole 6-minute
tape became one segment, and `apply_connector_fuses` reloads segments from
disk per cluster, which is fine but makes the chain hard to reason about.

**Fix:** the trim lands in the manifest only; the fuse's own write already
leaves the boundary rows spanning the union with `fused_from` stamped.
Pinned by `tests/test_fuse_chain_keeps_union_span.py`, verified to fail
without the fix; fuse, island and pre-ranking suites 106 passed.

**Diagnostic kept:** `MUX_TRACE_WRITES=1` appends every manifest/boundaries
write, with stage key, row count, first/last spans and caller frames, to
`<run_dir>/write_trace.log`. It found this in one run after four runs of
reading code.

## [36] PRODUCT: the hitch's segment-id remap of gap_report was refused by the body-writer rule

**Stage / area:** `segment_id_remap.rewrite_artifact_segment_refs` (called by
`chapter_close_hitch.rewrite_upstream_segment_refs`)
**Status:** FIXED. **Platform-independent.** Same class as entry 21.

After the hitch re-cuts the map it remaps segment ids across upstream
artifacts. For `understanding/gap_report.json` the sole-writer check accepts a
remap on `mutation_class=segment_id_remap` alone only while every spoken text
is unchanged. After a merge, two interviewer lines can fold onto one target,
so the line set shrinks; the check correctly treats that as a body change,
and the write carried the hitch's own key:

```
authority_denied:persist:understanding/gap_report.json:chapter_close_hitch:
pre_soft_freeze:gap_framing_compose (gap_body_writers:gap_framing_compose|nugget_layup_compose)
```

The hitch failed. Because the hitch had already archived and unmarked every
analysis stage from `missing_framing` onward (by design, so they re-run on the
new map), the run ended with 22 stages marked instead of climbing back to 40.
Watching the monitor this looks like the pipeline going backwards; it is the
re-cut cascade with the hitch failing before the climb.

**Fix.** The remap names the owner the check itself suggests, keyed off the
prior doc exactly as the check keys: `nugget_layup_compose` after
`nugget_layup_authority`, else `gap_framing_compose`. `gap_report_remap_owner`
in `segment_id_remap.py`. Pinned by
`tests/test_segment_id_remap_gap_report_owner.py`, which also asserts the
hitch's own key is still refused.

**Pattern worth naming upstream:** three stops so far (entries 21, 29, 36)
came from a legitimate writer holding the wrong key at a sole-writer or
overlay check. The checks are right; the callers are not consistently told
which key to present. A single helper that answers "who may write this body
right now" would remove the class.

## [37] PRODUCT: cross-speaker island fuses swallowed the host's questions

**Stage / area:** `segment_fuse.apply_connector_fuses`
**Status:** FIXED. **Platform-independent.**

`allow_cross_speaker_fuse` is off, but a seam carrying an incomplete-thought
hint (`island_straddle` counts as one) may still fuse across speakers, since a
broken sentence is worth mending. On a 6-minute source three such fuses
absorbed every `interviewer_question` row into the guest's slabs. The roster
was right (`spk_2` interviewer, 169 words, 8 questions), the pass-one manifest
had the host's rows, and after the hitch's re-cut the manifest kept one
0.5-second `interviewer_reaction` for the host. `missing_framing` then refused:

```
starved_host_packet: speakers name a host but manifest has no interviewer-framed segments
```

and delivery cascaded. On a long episode the host has many turns outside any
island, so the loss is invisible; on a short one an island can cover the lot.

**Fix.** A cross-speaker fuse never consumes a frame-role speaker's question
or prompt row, whatever hint or force asked for it. The one exception is
`diarization_yes_same`, which asserts the two labels are one person, so there
is no host row to protect. Skipped pairs are recorded as
`host_frame_protected`. Pinned in `tests/test_fuse_chain_keeps_union_span.py`;
the four suites that pin island and cross-speaker fusing still pass (180).

## [38] PRODUCT: the VO contract drift repair wrote gap_report under the seams stage's key

**Stage / area:** `vo_contract.repair_vo_contract_drift` (from
`sync_vo_contract_after_layup`, called by `air_script_seams`)
**Status:** FIXED. **Platform-independent.** Fourth instance of the pattern in
entry 36, and the reason for the shared helper below.

After nugget layup the gap report's only accepted body writer is
`nugget_layup_compose`. The drift repair reconciles the report after layup,
so it acts on layup's behalf, and its own log lines already say
`stage="nugget_layup_compose"`. Its write did not: `persist_frozen_seat_doc`
forwards whatever `stage_key` it is given and the caller gave none, and the
fallback `write_json` gave none either, so both landed under
`air_script_seams`:

```
authority_denied:persist:understanding/gap_report.json:air_script_seams:
pre_soft_freeze:nugget_layup_compose (not_allow:owner=nugget_layup_compose)
```

The seams stage failed at 51 of 72 on the 6-minute real run.

**Fix.** `artifact_ownership.gap_report_body_owner(prior)` is now the one
answer to "who may write the gap report body right now", mirroring the check
and the catalog row. The hitch's id remap and every gap-report write in
`vo_contract.py` use it. Suites around ownership, seats, air script and the VO
contract: 847 passed.

## [39] PRODUCT: the authority-undo detector halted on two no-op rewrites

**Stage / area:** `thrash_hardening.note_authority_undo_attempt`
**Status:** FIXED. **Platform-independent.**

The detector halts on A, B, A for one artifact when the first and third
content hashes match: writer A reverting what B changed. It never checked that
B's write differed. On the 6-minute real run the gap report ledger read

```
gap_framing_compose   4d8d3514...
nugget_layup_compose  4d8d3514...
gap_framing_compose   4d8d3514...
```

one identical hash for all three, and the run halted at 51 of 72 with
`authority_undo_thrash: action_oscillation:gap_framing_compose<->nugget_layup_compose`
and nothing to undo. Two repairs had rewritten the same bytes under the two
accepted keys, which entries 36 and 38 made them present correctly.

**Fix.** The branch also requires the middle hash to differ from the last. A
real revert still halts, through the `hash_oscillation` branch. Pinned by
`tests/test_authority_undo_needs_a_real_change.py`; the five thrash suites are
unchanged (108 passed).

## [40] PRODUCT: an empty transitions list the model chose was refused as partial

**Stage / area:** `stages/selection.run_transitions`,
`artifact_completeness._gaps_transitions`
**Status:** FIXED. **Platform-independent.** The product contradicted its own
prompt.

`docs/prompts/assembly/transitions.system.txt` says it four times: "Empty
array is a complete result", "Return `transitions: []` when adjacent segments
already flow naturally". The stage contract agrees (`transitions min_rows 0`).
On the 6-minute real run the model did exactly that, status complete, with the
rationale "No additional spoken bridges are needed across the locked order".

`_gaps_transitions` accepts an empty list only through `empty_ok`,
`selection_count < 2`, or `_meta.empty_allowlist == "selection_lt_2"`, and
nothing in the product stamps any of them. So the doc was "partial", Done
Authority refused `mark_done(transitions)`, the file stayed in staging as
`pending_only`, and the run stopped at 52 of 72:

```
StageError: LLM stage transitions: auto_complete mark_done refused (Done Authority)
```

**Fix.** When the model's own list is empty, the stage stamps
`empty_ok: true` and `empty_reason: model_returned_no_transitions` before
persisting, which the gap rule already accepts. Rows removed by the stage's
own guards do not count as the model's verdict, so a list emptied by
filtering is still partial. The artifact schema accepts the two keys. Pinned by
`tests/test_transitions_empty_by_verdict.py`; transitions, completeness and
selection suites 147 passed.

## [41] ENV (this machine): Smart App Control blocked scikit-learn inside the Chatterbox venv

**Stage / area:** `vo_synthesize`, local Chatterbox runtime
**Status:** FIXED on this machine. Not a product defect; does not exist on
macOS. Recorded because the pipeline stopped on it and the diagnosis was slow.

With delivery reaching `vo_synthesize` for the first time, Chatterbox failed
twice per line:

```
DLL load failed while importing _k_means_common: An Application Control policy has blocked this file.
```

Windows 11 Smart App Control was on (policy state 1) and was refusing
scikit-learn 1.9.1's compiled extensions on reputation, a different file each
attempt. Chatterbox itself never uses scikit-learn; `librosa.effects` lazily
imports `librosa.decompose`, which hard-imports it, so generate failed while
the import-only verify had passed. Installing the widely distributed
`scikit-learn==1.6.1` into that venv cleared every block, and a standalone
synthesis then rendered 2.8 s at 24 kHz on CUDA.

**Product note:** the pipeline behaved well here: two attempts, a clear cause
in the log, a loud stop. A `--verify` that performs one short real synthesis
rather than an import would have caught this at setup time. Documented in
`docs/cross-cutting/windows-cuda-setup.md`.

## [42] OBSERVATION: the delivery walk skipped `mix` silently, twice

**Stage / area:** `homunculus/agenda.walk_seed_agenda`
**Status:** worked around; the cause did not survive for a fix.

With delivery at 61 of 72 on the 6-minute real run, the conductor pinned
`mix` and logged "walking remaining seed to master (1 stage(s))", and then
nothing: no "Stage start: mix", no refusal, no error, and `run_delivery`
reported `remaining stages: mix; resume=mix`. Re-entering the driver at `mix`
did the same. Dispatching the stage directly through `run_single_stage`
ran it cleanly: `master/assembly.wav` landed and the stage marked itself
done, after which the walk continued.

What was checked afterwards: `refuse_skip_then_consume`, the three walk
refusals (`_refuse_g0_locked_rerun`, `_refuse_delivery_timeline_rewind`,
`_refuse_music_before_assembly`) under both `run` and `walk`, the input-issue
gate, the skipped ledger, the sticky halt: none refuse `mix`. The state had
changed by then, so whichever guard fired in the walk could not be pinned.

**Worth fixing upstream regardless:** in `walk_seed_agenda`, a refusal caught
from those three guards falls through to a plain `continue` when it is not a
music or assembly case. A stage can be skipped by the walk with no line in the
log, and the conductor then reports it as remaining forever. One `ctx.log` at
warning level naming the stage and the exception text on that path would have
turned this from an afternoon into a minute. Same shape as entry 24: a silent
failure that looks like a hang.

## [43] PRODUCT: MusicGen's device was decided with the wrong interpreter

**Stage / area:** `musicgen_runner.effective_musicgen_device`
**Status:** FIXED. **Platform-independent in shape; only CUDA hosts were hit.**

`auto` prefers MPS, then checks `torch.cuda.is_available()` in the core
process. The core venv is deliberately lean and here carries no torch at all;
CUDA torch lives in MusicGen's own venv (`torch 2.6.0+cu124`, 6.4 GB
reported). So `auto` resolved to `cpu`, and the first delivery run to reach
`mmaudio_sfx` sent a 20-second `musicgen-medium` bed to the CPU, where each
candidate takes many minutes and the GPU sat at 7%. On the Mac the core venv
has torch with MPS, so `auto` was right there and nobody saw this.

**Fix.** When neither MPS nor core-process CUDA is available, `auto` now asks
the MusicGen venv itself (`python -c "import torch; ..."`, 30 s cap, cached
per process) before falling back to CPU. An explicit `musicgen.device` still
wins. Pinned by `tests/test_musicgen_auto_device_asks_runtime_venv.py`.

**This machine:** `config/app.local.json` also pins `device: cuda` and
`model_id: facebook/musicgen-small`; medium at fp32 does not fit 6 GB. The
existing step-down ladder would have found small eventually, at the cost of
an OOM per asset.

**Test note (open):** `test_ladder_stepdowns_stay_on_mps` now fails on this
machine with every ladder step attempted twice ([large, large, medium,
medium, small, small]). It passed in the full sweep earlier the same day at a
commit whose MusicGen code is unchanged since. Excluded as causes, each by
direct experiment: this fix, the `app.local.json` keys, a held
`ASSETS/.gpu_exclusive.lock`, and `ASSETS/.heavy_abort_state.json`. Something
else on this machine changed its behaviour; it is not on the path to a
complete run, so it is recorded rather than chased. Two hygiene points stand
regardless: the test writes the real `ASSETS/.heavy_abort_state.json`, and
the suite touches the real GPU gate; both belong under `tmp_path`.

## [44] PRODUCT: the conductor names a resume stage it then never dispatches

**Stage / area:** `homunculus` delivery conductor, `pipeline.run_delivery`
**Status:** worked around in the driver; upstream fix recommended.

Three stops on the 6-minute real run had the same shape. The failure named
its own remedy:

```
Delivery incomplete after conductor: remaining stages: vo_line_adjudicate; resume=vo_line_adjudicate
Delivery incomplete after conductor: remaining stages: mix; resume=mix
junction_snip_qa remaster owed: resume mix: music_epoch_pre_beds_seat ...
```

and the next pass named the same stage as remaining without running it, so
retries made no progress. In every case dispatching the named stage directly
through `run_single_stage` ran it cleanly, and the walk continued. For `mix`
the entry-42 silent skip is one cause; for the remaster case the walk pins
`junction_snip_qa` while the remedy is `mix`, which sits earlier in the
conductor's own list, and the walk-to-master only walks forward.

**Driver:** when the same error repeats with no progress and it names a
resume stage, the driver now dispatches that stage directly once and retries
the phase. **Upstream:** the conductor already computes the resume pin; when
it differs from the seed front, it should dispatch the pin itself instead of
reporting it.

## [45] MILESTONE: a complete episode from a 6-minute excerpt on this machine

**Status:** achieved, with caveats stated below.

Run `exec_046`: **72 of 72 stages**, analysis 35 of 35 and delivery 37 of 37,
on the real model (`gpt-5.6-terra`) with local STT, Sortformer diarization,
Chatterbox VO on CUDA, MusicGen and MMAudio. Outputs: `master/master.wav`
and `publish/audio.mp3` at 180.9 s, `publish/cover.jpg`, `chapters.json`,
`description.txt`, `episode.json`, `package_ready.json`.

**Caveats, honestly.** That run was not hands-off. It took: three direct
dispatches of stages the walk named but did not run (entry 44; the driver
now does this itself), the Smart App Control workaround for Chatterbox
(entry 41), and MusicGen rendering on CPU for about 38 minutes before the
device fix (entry 43). A fresh run with no manual steps is the real test.

**Update, `exec_047`:** the first attempt at that stalled twice, on entry 46
(co-producers unmarking each other) and entry 47 (the conductor stopping
after every stage). With both fixed it reached 72 of 72 using only the
driver's own re-entries: master.wav at 114.1 s, `publish/audio.mp3`,
`cover.jpg`, `chapters.json`, `package_ready.json`. The last re-entry landed
the final 11 stages (`music_palette_compose` through `podcast_publish`) in
one driver pass. A run started from scratch on the same excerpt with all
fixes in place (`exec_048`) is the standing check.

**What it took to get here, in order:** entries 23 through 47, of which the
platform-independent product defects are 23, 28, 29, 30, 33, 35, 36, 37, 38,
39, 40, 43, 46 and 47. The rest are this machine (24, 25, 41), observations for
upstream (31, 32, 42, 44), or test hygiene (34).

## [46] PRODUCT: two co-producers of one file unmarked each other in a loop

**Stage / area:** `post_decision_sanitize.after_shared_path_write` (A-05
shared-path reconcile), `sound_design_palettes`, `sound_design_plan`
**Status:** fixed.

`understanding/sound_design_plan.json` has an analysis writer
(`sound_design_palettes`, stage 34) and a delivery writer (`sound_design_plan`,
stage 51). The reconcile hook that runs after any shared-path write unmarked
*every other* co-producer of that path whenever the content fingerprint
changed, in both directions. On the hands-off 6-minute run (`exec_047`) that
became a cycle:

1. `sound_design_plan` lands (one LLM call) and restamps the file as its own.
   The hook unmarks `sound_design_palettes`.
2. The next pass sees analysis incomplete and re-runs `sound_design_palettes`.
   With `early_palettes_llm=false` it reloads the plan from disk and writes it
   back under its own producer stamp. The hook unmarks `sound_design_plan`,
   and the done authority now reads the plan as "unpaid land".
3. Delivery re-runs `sound_design_plan`. Back to step 1.

Every cycle cost one real model call and the run reported the same
`remaining stages: sound_design_plan` until the resume budget ran out. The
run stopped at 53 of 72. `operator/gap_inventory/shared_restamp_*.json` shows
the two decisions clearing each other.

The same shape exists for `content_brief.json` (`content_context` /
`content_brief_reanchor`) and `boundaries.json` (`boundary_detection` /
`boundary_topic_resplit`); there it was only held back by the post-G0 rewind
lock, whose own docstring describes this deadlock.

**Fix, two layers:**

- `after_shared_path_write` now unmarks only co-producers that come *after*
  the writer in pipeline order. A later writer restamping the path is its
  normal job and never invalidates the earlier producer; an earlier producer
  re-running still stales the later ones.
- `sound_design_palettes` no longer rewrites the plan file when a delivery
  writer (`sound_design_plan`, `music_palette_compose`, `sfx_prompt_craft`,
  `sound_design_vo_finalize`) already owns it. It leaves the paid document
  alone and marks itself done.

Tests: `test_shared_path_authority.py` (`reanchor keeps upstream done`,
`upstream rewrite clears downstream`, `sound_design_plan never unmarks
palettes`). The old test asserted the bidirectional clear and was replaced.

**Driver:** the direct dispatch from entry 44 now fires on any pass that
fails without progress and names a resume stage, not only when the same
error text repeats. On `exec_047` the two consecutive errors differed in
their remaining-stage list, so the old condition never triggered.

## [47] PRODUCT: the delivery conductor stopped after every single stage

**Stage / area:** `pipeline.run_delivery`, `homunculus.run_homunculus_phase`
**Status:** fixed.

With entry 46 out of the way, the hands-off run on `exec_047` still spent its
whole resume budget (8 passes) in delivery and stopped at 62 of 72. The pass
log shows the pattern:

```
resume 1: remaining stages: vo_synthesize; resume=vo_synthesize
resume 2: remaining stages: sound_design_vo_finalize, edl_narrative_audit; ...
resume 3: remaining stages: edl_narrative_audit; ...
resume 4: remaining stages: edl; resume=edl
resume 5: remaining stages: assembly_preview; ...
resume 6: remaining stages: listen_delight_audit; ...
resume 7: remaining stages: mix; resume=mix
```

Every pass landed exactly one stage. The conductor pins itself to the seed
front ("pinning conductor to seed front X (was N stage(s))"), walks that one
producer, and returns the rest as `remaining_after`; `run_delivery` then
raised "Delivery incomplete after conductor" even though the pass had just
made progress. Delivery has 37 stages, so a self-propelled run was
arithmetically impossible: the raise was doing the driver's job of
re-entering, one resume per stage.

**Fix:** `run_delivery` now re-enters the conductor while each pass lands at
least one new delivery stage and no master is committed (bounded by the
delivery stage count). The first pass that lands nothing falls through to the
existing incomplete handling, so real stalls still name their resume stage.
The per-pass logs are unchanged; a new info line records each re-entry.

Tests: `tests/test_delivery_conductor_loop.py`.

## [48] PRODUCT: a stale "cyclic ordering constraints" audit blocked the EDL forever

**Stage / area:** `edl_narrative_audit`, `artifact_repairs.repair_edl_audit`
**Status:** fixed.

On the from-scratch run `exec_049` (6-minute excerpt) `narrative_arc_plan`
produced ordering constraints that were cyclic over the selected segments
(`seg_007 < seg_004 < seg_005 < seg_006 < seg_007`). `full_master_ranking`
noticed, said so in `selection.notes`, and ordered the segments by tape
order. `align_narrative_plan_to_selection` then rewrote the plan to three
constraints the air order satisfies. All correct so far.

`edl_narrative_audit` still returned `verdict: fail` with a
`ordering_constraint_broken` blocking issue quoting the *old* constraints.
The repair loop demotes several kinds of complaint that the current
artifacts contradict (chapter membership, blank scraps, duplicate seams),
but had no check for ordering constraints, so the fail stood. The audit
artifact failed the pre-flush commit barrier on its own verdict, its done
marker was cleared as "newer uncommitted pending", and the driver stopped
on the same error twice with no progress. Run stalled at 56 of 72.

**Fix:** `_ordering_constraints_satisfied` checks every committed constraint
against `selection.ordered_segment_ids` (a constraint naming an off-air
segment is vacuous; a missing plan or selection never demotes). When they
all hold, an `ordering_constraint_broken` complaint is demoted to a warning
like the other disk-contradicted complaints, and the verdict is recomputed.
A genuinely violated constraint still blocks.

Tests: `tests/test_edl_audit_ordering_demotion.py`.

## [49] OBSERVATION: the layup re-run under the walk reverted to its old plan and halted

**Stage / area:** `nugget_layup_compose` under `delivery_walk_to_master`,
`thrash_hardening.note_authority_undo_attempt`
**Status:** open; the direct dispatch passed, recorded for upstream.

On `exec_049`, right after entry 48, `edl` excluded `seg_007`
(`blank_or_unusable_answer_audio`) from the selection during its narrative
pre-repair. That correctly made the layup plan stale (`stale=['seg_007']`),
the walk unmarked `nugget_layup_compose` as hollow and re-ran it. The
re-run's model call succeeded, then the plan file was written twice within
a second: first a new hash, then the *exact old hash* from 09:13. The
authority-undo detector read that as A -> B -> A and halted the run:

```
authority_undo_thrash:understanding/nugget_layup_plan.json: hash_oscillation:12eae82e...<->fbdb9177...
```

Dispatching the same stage directly (`run_single_stage`) a minute later
wrote two *new* hashes and landed a 4-segment plan, so the revert is
specific to the walk path. Every layup run writes the plan twice (the model
persist, then an adopt/order-lock rewrite); in the walk case the second
write restored stale content. Suspect a committed-copy overlay winning
over the staged plan inside the second write. Not chased further: the run
was re-entered and continued. The detector was right that this is thrash;
what is wrong is the source of the revert.

The authority-undo history that shows it: `operator/authority_undo.json`
under `understanding/nugget_layup_plan.json`.

## [50] PRODUCT: EDL QC failed a locked speaker volley the selection itself reorders

**Stage / area:** `edl_narrative_qc._validate_speaker_volley_integrity`,
`speaker_volley.detect_speaker_volleys`, `episode_structure_compose`
**Status:** fixed at the QC; the detector's over-reach is noted for upstream.

`exec_049` (6-minute excerpt, 5 selected segments) halted at `edl` with

```
edl_narrative_qc strict: speaker_volley_integrity:speaker_volley_split:sv_seg_001_seg_007
identical_failure edl x3/3 halt=True
```

`detect_speaker_volleys` chains consecutive segments while the speaker
alternates, up to six, and locks the chain. In a two-speaker interview every
adjacent pair alternates, so on a short clip the *entire tape* became one
locked volley: `[seg_001, seg_004, seg_005, seg_006, seg_007]`.
`full_master_ranking`, which is the air-order authority, then placed
`seg_006` before `seg_004`; the selection was hard-frozen and the episode
structure sealed (`authority_denied ... hard_freeze:episode_structure_compose`
on every repair attempt). The EDL faithfully followed the selection and the
QC failed it three times for an order it is not allowed to change.

**Fix:** the EDL volley check now runs the same integrity check against the
selection's `ordered_segment_ids`. A flag the selection already raises is
inherited, logged as a warning, and dropped; a flag only the EDL raises
(the EDL deviating from the selection) still fails. Tests:
`tests/test_edl_qc_volley_inherited.py`.

**For upstream:** the chain rule needs a bound tied to what it is protecting.
A question-answer pair is a volley; five turns spanning the whole episode
is the episode. Either cap `speaker_turn` chains at a pair, or do not lock
them when they cover most of the selection.

## [51] PRODUCT: native_only runs demanded an orientation VO line nobody may mint

**Stage / area:** `publishability_boundary` (`opening_orientation` check at
`post_edl` / `pre_mix`), `opening_orientation.ensure_episode_orientation_body`
**Status:** fixed.

`exec_049` was routed `native_only` by `framing_posture_decide`: run_meta has
`gap_fill_mode: skipped`, `gap_framing_enabled: false`, reason "pipeline_mode
native_only, skip gap-fill VO". Every layup was a typed skip and the gap
report had no interviewer lines. That is a legitimate posture.

The publishability contract still required exactly one opening orientation
line: `mix` failed with

```
PublishabilityBlocked: publishability blocked at pre_mix: opening_orientation_inaudible
  opening_orientation_count=0 expected=1
```

The recovery playbook rewound to `vo_synthesize`, which cannot mint a line
either (`ensure_episode_orientation_body` returns at once when framing is
disabled, without recording anything), and the retarget from `edl` / `mix`
is refused by ownership (`orientation retarget: gap_report frozen ...
not_allow:owner=nugget_layup_compose`). The run went 60 -> 55 and stopped.
`exec_047` did not hit this because its posture kept framing on and the
layup minted `vo_preface_seg_001`.

**Fix, two layers:**

- With framing disabled, `ensure_episode_orientation_body` now records the
  durable omit payload (`omitted: true, required: false,
  omit_reason: gap_framing_disabled`) on the gap report instead of returning
  silently, so every consumer reads the same decision. An existing omit is
  kept as is.
- `validate_publishability` skips the orientation demand when framing is
  disabled and no orientation line exists on the books; a line that does
  exist is still validated.

Tests: `tests/test_orientation_native_only.py`.

## [52] PRODUCT: a denied optimizer auto-promote retried forever and unseated a good mix

**Stage / area:** `timeline_optimizer/daemon.py` (endless daemon),
`air_order.mix_wav_fresh_versus_edl`, `mix_outputs_seated`
**Status:** fixed.

On `exec_049`, `mix` rendered cleanly (assembly.wav, intelligibility QC
passed, stage finished), and 34 seconds later
`seat_authority: demoted hollow mix .stage_done (not mix_outputs_seated)`.
Every sub-check of `mix_outputs_seated` passed except the mtime one:
`master/edl.json` and `master/selection.json` were 18 minutes newer than
the assembly, with unchanged content (order hash, lock and speech clips all
identical; `verify_commitment` said `committed`).

The rewrite came from the timeline-optimizer daemon. Its plateau
auto-promote was denied by authority
(`authority_denied:persist:master/selection.json:timeline_optimizer:edl_sealed`),
the exception path never set `auto_promoted_once`, so it retried on every
generation: 113 identical failures in 17 minutes, each attempt touching the
EDL and selection on the way to the denial. With mix "unseated", the walk
would not run `mix` again (already marked), the music band stayed blocked
on `assembly_not_seated_for_music`, and the driver's own direct dispatch
of `mix` landed only to be demoted again. A dead loop that burned one
resume budget per pass.

**Fix:**

- The daemon attempts auto-promote once per run. A failed attempt is
  recorded in the optimizer state (`auto_promote_attempted`,
  `auto_promote_error`) and not retried; the daemon keeps exploring and the
  operator can still take the best candidate by hand.
- `mix_wav_fresh_versus_edl` falls back to the render commitment when the
  EDL file is newer than the assembly: if `verify_commitment` still proves
  the assembly was rendered from the current EDL and order, the mix stays
  seated. A real EDL change still unseats it.

Tests: `tests/test_mix_seat_content_commitment.py`.

## [53] PRODUCT: master_finalize waited on an optimizer gate nobody could answer

**Stage / area:** `gates.check_timeline_optimizer_pending`, `master_finalize`,
driver `clear_operator_gates`
**Status:** fixed.

With `timeline_optimizer.block_finalize_until_take_or_skip: true` (the
shipped default), `master_finalize` exits with

```
Timeline optimizer pending: Take best or Skip via GUI (POST .../timeline-optimizer/take-best|skip)
```

while the optimizer daemon is running and has neither promoted nor been
skipped. On `exec_049` the daemon's one auto-promote was refused by
authority (entry 52), so `auto_promoted_once` never set and the gate stayed
up; the driver signs off every other operator gate but not this one. The
run sat at 65 of 72 with the last seven stages blocked on a GUI button.
`exec_047` did not hit it because its daemon promoted on plateau.

**Fix:** a refused promote (`auto_promote_attempted` in the optimizer
state) clears the gate, since the seat is sealed and there is nothing for
the operator to take. The driver's `clear_operator_gates` now also answers
"Skip" when the gate is up, the way an unattended operator would.

The same run then hit the second half of the rule in `run_master_finalize`
(S1): "best take is unpaid, apply take-best via mix/junction". The daemon's
best candidate had a different order from the live selection, and
promoting it is exactly what authority refused, so finalize was demanding
something that can never be paid. A refused or skipped best is now waived
there too (`_optimizer_skipped`, `auto_promote_attempted`). A best that was
neither refused nor skipped is still owed. Tests:
`tests/test_optimizer_gate_refused_promote.py`.

## [54] PRODUCT: ingest read the whole tape into memory three times over for a peak envelope

**Stage / area:** `waveform_peaks.generate_peaks` (called from `ingest`)
**Status:** fixed.

The first full-length run (`exec_051`, 59.5 minutes, 315 MB mono PCM after
preclean) was killed at `ingest/waveform_peaks` on this 16 GB machine before
any model had loaded. `generate_peaks` did `sf.read()` of the whole file
(float64: 1.26 GB), then `mean(axis=1).astype(float64)` (another 1.26 GB),
then `np.abs(windows)` (another 1.26 GB): about 3.8 GB transient for a
100 ms peak envelope the GUI timeline draws. A 6-minute clip never showed
it. On an 8 GB machine a one-hour source would fail here every time; on a
16 GB one it depends on what else is open.

**Fix:** peaks are computed from `sf.blocks()` in 4 MiB float32 reads with a
carry for the window that straddles two blocks. The output is identical to
the whole-file computation (tests compare the two on mono and stereo files
with block boundaries inside windows). The one-hour tape now takes 0.8 s and
a few MB. Tests: `tests/test_waveform_peaks_streamed.py`.

## [55] PRODUCT: selection_framing_apply excluded a hard-keep segment, refused every time

**Stage / area:** `refinement_passes.run_selection_framing_apply`
**Status:** fixed.

On the first full-length run (`exec_052`, 59.5 minutes) the pass that drops
segments already covered by a framing VO line tried to exclude `seg_059`,
which is a hard keep. The selection sanitizer refuses any order missing a
hard keep (`sanitize_refused:selection: hard_keep_missing_from_order:seg_059`),
so the stage failed on every attempt, the walk recorded it as a defect and
refused to re-dispatch it, and `transitions` could not run behind it. The
run stopped at 50 of 72 with analysis complete. Six-minute clips have no
hard keep that a framing line also covers, so they never reached this.

**Fix:** framing coverage skips hard-keep segments; they stay on air and
the other covered segments are still excluded. Test:
`tests/test_framing_apply_keeps_hard_keep.py`.

## [56] PRODUCT: edl refused NLE edits that were already on the disk selection

**Stage / area:** `stages/assembly.py` (edl, S5 NLE guard)
**Status:** fixed.

On `exec_052` (one-hour source) pipeline stages (`full_master_ranking`, CTA
prune) saved NLE edits through `save_nle`, which records them as operator
edits. `edl` then requires the disk selection to carry both the same order
and the `nle_applied` stamp. The order matched exactly (49 of 49, same
sequence), but `nle_applied` is only stamped by `apply_nle_to_selection`,
and a later selection writer landed the order without going through it. No
stage re-stamps it, so `edl` halted on every attempt at 57 of 72.

**Fix:** the guard compares the order only (`nle_committed_on_disk`). A
differing order still refuses. Test: `tests/test_edl_nle_committed_by_order.py`.
Same class as entries 48 and 50 to 55: a check demanding a marker that no
stage is responsible for producing.

## [57] PRODUCT: edl QC failed a hanging end nothing could fix, and a hard keep "covered" by framing

**Stage / area:** `stages/assembly.py` (speech clip bounds),
`gap_framing.ranking_exclude_segment_ids`, `edl_narrative_qc`
**Status:** fixed.

After entry 56, `exec_052` (one-hour source) failed EDL narrative QC twice:

1. `transition after "seg_060" lands on an incomplete thought`. Boundary
   detection cut `seg_060` mid-sentence ("...this is going to be"). The EDL
   build already trims a hanging end back to the last complete thought, but
   this segment has none inside it, so the trim did nothing and QC blocked.
   The junction thought-complete recut, which can extend into following
   tape, runs only after mix and only toward on-air clips.
2. `framing-covered segment "seg_059" appears as speech`. Entry 55 kept the
   hard keep `seg_059` on air in `selection_framing_apply`, but QC computes
   "framing-covered" from the same function without that exception.

**Fix:**

- When no complete-thought point exists inside a kept segment, the EDL
  extends its end forward through the transcript to the first complete
  thought (`extend_hanging_end_to_thought`, at most 12 s, never into the next
  on-air segment). The existing trim-back still wins when it can.
  Follow-up on the same run: the first version only capped the extension
  when the next on-air segment started *after* this one ended, so `seg_059`
  (tape-adjacent to `seg_060`) was extended straight into it and EDL QC
  refused the overlapping source ranges. The extension now never runs when
  any on-air segment starts at or just after this end, and is capped at the
  nearest on-air start anywhere in the air order, not only the next clip.
- `ranking_exclude_segment_ids` itself drops hard keeps, so the apply pass,
  ranking, the framing guard and EDL QC all see the same coverage set. This
  replaces the one-sided fix of entry 55 (which stays as a harmless guard).

Tests: `tests/test_hanging_end_and_framing_hard_keep.py`.

## [58] PRODUCT: edl and mastering read the whole tape into memory, same class as entry 54

**Stage / area:** `audio_energy._energy_windows_cached` (edl cut-edge
refine), `mastering_bus.measure_assembly_bus` (mastering)
**Status:** fixed.

On the one-hour run (`exec_052`) the memory guard killed the driver in
`edl/build_edl`. The acoustic cut-edge refine asks `audio_energy` for RMS
windows, which read the full `ingest/normalized.wav` as float64 and made a
mono copy and a squared copy: several GB transient, the same defect entry
54 fixed in ingest. A sweep for whole-file reads found one more that runs
late on long sources: `measure_assembly_bus` loads the whole assembly
(about 38 minutes of stereo for this source) as float64 for pyloudnorm,
which copies it again to K-weight it.

**Fix:** both stream. RMS windows come from `sf.blocks()` with a carry for
the straddling window (bit-identical to the whole-file result in tests).
Integrated loudness is a streamed BS.1770-4 implementation: K-weighting
with carried filter state per channel, per-100 ms sums of squares, then the
standard 400 ms / 75 % overlap absolute and relative gating; it matches
`pyloudnorm.Meter.integrated_loudness` within 0.01 LU in tests. Other
whole-file reads found in the sweep either run in analysis, which completed
on this tape, or read short assets. Tests: `tests/test_audio_energy_streamed.py`,
`tests/test_mastering_bus_streamed.py`.

## [59] PRODUCT: two definitions of "complete thought", and QC demanding one the tape lacks

**Stage / area:** `edl_narrative_qc._validate_vo_after_legal_hinge`,
`stages/assembly.extend_hanging_end_to_thought` (entry 57)
**Status:** fixed.

With entry 57 in place, `exec_052` still failed EDL narrative QC on
`transition after "seg_060" lands on an incomplete thought`. Two causes:

1. The entry 57 extension picked its end with
   `thought_complete_recut.complete_thought_candidates`, which counts a
   following discourse word ("right") as a close. QC uses
   `is_legal_conceptual_hinge` on the last 24 words, which is stricter, so
   the extended end ("...this is going to be more") still failed QC.
2. The tape never finishes that thought: after "they're going to be" the
   speaker stops and 22 seconds of silence follow. No trim, fuse or
   extension can reach a close QC accepts, yet QC blocked the run.

**Fix:**

- One predicate for both sides: `edl_narrative_qc.qc_hinge_at` /
  `first_qc_hinge_between` (the exact QC text window and hinge test). The
  EDL extension now uses it, so whatever the build lands on, QC accepts.
- QC downgrades the complaint to a logged warning only when no QC-legal
  close exists inside the clip or within 12 s after it (a true tape
  trail-off). If a close is reachable, it stays an error, because the
  build could and should have used it.

Tests: `tests/test_edl_hinge_shared_predicate.py`, plus
`tests/test_edl_narrative_qc.py` (the existing refusal case now includes a
reachable close so it keeps testing the error path; a new case covers the
trail-off warning).

## [60] PERFORMANCE: the conductor spent minutes between stages in Path.resolve()

**Stage / area:** `vo_synthesis_audit._audited_wav_path`, `wav_content_sha256`
(reached from every delivery completeness check)
**Status:** fixed (first pass); measured.

On the one-hour run the conductor took 3 to 17 minutes to decide each next
stage. A 90-second `py-spy` sample of the live driver showed 98 % of the
time in `constrain_conductor_to_seed_front` -> completeness checks, and
**76 % inside `Path.resolve()`** called from `_audited_wav_path._add`, which
builds a candidate list of up to ~40 WAV paths per VO line and resolved every
one of them (existing or not) just to de-duplicate. On Windows `resolve()`
walks each path component through the filesystem, and these paths sit under
OneDrive. The completeness checks nest (`seed_stage_complete` inside
`stage_artifact_incompleteness` inside `edl_ready` ...), so this ran thousands
of times per decision. The same WAVs were also re-hashed in full every time.

**Fix:**

- `_add` checks existence first and de-duplicates on
  `normcase(abspath(path))`. Run paths contain no symlinks, so this names the
  same file without touching the filesystem per component.
- `wav_content_sha256` memoizes by (path, size, mtime_ns). A rewritten file
  changes size or mtime, so a stale digest cannot be served.

- A second profile after that fix showed the time had moved to `stat()`:
  for every VO line, `_audited_wav_path` probed every staging directory under
  `.pending_writes` (56 on this run) times five sub-paths, although only two
  of them contain VO audio at all. The staging dirs are now listed once per
  call and only those holding the right top-level folder are probed.

**Measured** on `exec_052` (7 VO lines): `_audited_wav_path` 89 ms/line ->
13 ms/line -> 4.4 ms/line (20x), identical results. Test: `tests/test_vo_audit_sha_memo.py`; the
519 existing VO-audit tests pass.

**Not fixed yet (for upstream):** the deeper cause is that the conductor
re-evaluates full completeness for every candidate stage from scratch, and the
checks call each other recursively with no per-decision cache. A per-pass
memo of `stage_artifact_incompleteness` keyed on the run's artifact
fingerprint would remove most of the remaining time.

## [61] PRODUCT: mix, junction_snip_qa and the music band blocked each other in a cycle

**Stage / area:** `delivery_guardrails.mix_epoch_block`, seed order in
`homunculus/runtime.py`, `junction_snip_qa`
**Status:** fixed.

`exec_052` (one-hour source) reached 60 of 72 and then could not move:

- the conductor pinned `mix` (speech-first seat needed before music);
- `mix` refused: `incomplete_cut_unresolved: live incomplete-cut residuals
  on_a_roll, recut/fuse/omit at junction_snip_qa first`;
- `junction_snip_qa` refused: `cannot run junction_snip_qa: delivery epoch
  music_incomplete`;
- the music band refused until `mix` seats the assembly (HAU speech-first).

Seed order in `homunculus/runtime.py` already has the right rule: when
`junction_recut_precedes_mix(ctx)` is true, junction runs before the first
mix. `mix_epoch_block` had its own copy of the ordering and did not know
that exception, so the two layers disagreed and closed the cycle.

**Fix:** `mix_epoch_block` admits `junction_snip_qa` when
`junction_recut_precedes_mix` is true; every other case, and every other
stage (finalize, ship), still waits for music. The traversal driver also now
follows a short remedy chain when a directly dispatched stage refuses and
names another stage ("at X first"). Tests:
`tests/test_junction_admitted_before_music.py`,
`tests/test_driver_resume_hint.py`.

## [62] STRUCTURAL: one ordering authority instead of four copies of the exceptions

**Area:** new `ordering_authority.py`; consumers `homunculus/runtime._seed_prereq_block`,
`llm_flow_hardening.maybe_require_upstream_llm_progress`,
`delivery_guardrails.mix_epoch_block`
**Status:** done.

After entry 61 the one-hour run still could not run `junction_snip_qa`: each
fix unblocked one check and the next one refused with its own reason
(`seed order: complete music_palette_compose`, then `Prerequisite stage
music_palette_compose is not complete`). Delivery ordering was being decided
in four places, and each carried a private copy of the two legitimate
exceptions ("junction may recut before the first mix", "speech-first mix may
run before music"). The copies had drifted: one allowed junction only when
`mix` was the earliest incomplete stage, another never allowed it at all.

This is the class behind many of entries 48 to 61: independent guards that
each encode part of the same policy. Rather than patch a fifth copy, the
ordering exceptions now live in one function, `ordering_exempt(ctx, stage,
prerequisite)`, and every ordering check asks it. A guard test fails if any
of those checks grows its own `junction_recut_precedes_mix(...)` again.

Follow-up on the same run: once music finished, the same cycle reappeared
under a different epoch token. The remaster mix refused until junction
recut its residuals, and `mix_epoch_block` refused junction with
`speech_first_remaster_pending` from a branch that did not consult the
authority. That branch now asks `ordering_exempt` as well.

A third face of the same cycle after the edit was re-cut: `mix` waited for
music (a stale "music complete" stamp meant beds were no longer deferred)
while music refused with `assembly_not_seated_for_music`. The authority now
also lets mix run first whenever `allow_speech_first_mix` holds, which is
exactly the state music is waiting on.

Tests: `tests/test_ordering_authority.py` (16),
`tests/test_junction_admitted_before_music.py` (remaster case), and the
existing ordering-related tests pass.

**Scope, honestly:** this unifies *ordering*. The other disagreement families
from entries 48 to 61 (framing coverage, completeness predicate, hard keeps)
were each unified to a single shared function at the time; there is no single
cross-cutting "who may write what" authority yet. See the note at the end of
this file.

## [63] PRODUCT: junction offered only "omit" for a hard-keep trail-off

**Stage / area:** `junction_snip_qa.detect_junction_findings` (`on_a_roll`)
**Status:** fixed.

With ordering unified, `junction_snip_qa` ran on `exec_052` and tried to
repair `seg_060` (the trail-off of entry 59) by omitting it; the selection
sanitizer refused (`hard_keep_missing_from_order:seg_060`,
`never_exclude_primary_impact`). With no extend, no earlier cut and no LLM
recut available, the repair ladder had nothing left but omit.

**Fix:** the detector uses the same trail-off rule as EDL QC (entry 59,
`edl_narrative_qc._hinge_reachable`): when there is no extend, no earlier
cut, no thought-complete recut and no legal close within reach, it is a tape
trail-off and gets no repair finding. Every case with any real repair option
is unchanged (the existing ladder tests pass, 1565 junction tests in all).

## [64] PRODUCT: a hard keep fused into a survivor was still "missing" to every keep check

**Stage / area:** `hard_keep.hard_keep_segment_ids`,
`edl_overlap_repair.consumed_segment_ids`, `junction_snip_qa` fuse ladder
**Status:** fixed.

On `exec_052` the junction repaired the `seg_059`/`seg_060` hanging join by a
fuse *union*: `seg_059`'s span grew to cover both, so no audio was lost, and
the id `seg_060` was retired. The junction's own comment says unions are
allowed for hard keeps (exec_11871). But the hard-keep list still contained
`seg_060`, so `_exclude_from_selection` refused to retire it, the selection
kept 49 ids while the EDL had 48, and the stage failed on the divergence,
with the sanitizer also reporting `hard_keep_missing_from_order:seg_060`.

**Fix (one rule, every consumer):** `hard_keep_segment_ids` drops ids that a
fuse union consumed, because their tape airs under the survivor's id.
`consumed_segment_ids` now recognises junction fuse retires
(`junction_snip_qa:<kind>:fuse_<why>`) as well as overlap-repair unions; a
junction *omit* still does not count, since omitted audio is gone.
Follow-up on the same run: the actual retire of `seg_060` came from another
junction path and was stamped `junction_snip_qa:on_a_roll` (no `fuse_`),
while `seg_059` had been extended to 3163670 over all of `seg_060`. The rule
is now geometric as well: an excluded id whose manifest span lies inside an
on-air segment's effective span (NLE override or manifest) is consumed,
whatever path wrote the retire. A later stop on `seg_025` showed recuts do
not land to the millisecond (`seg_024` recut over 97 % of it), so the test is
a coverage share: at least 90 % of the retired tape still airs. The
primary-impact check in `framing_coverage_guard` (`never_exclude_primary_impact`)
applies the same rule through `_impact_source_is_unenforceable`. Tests:
`tests/test_hard_keep_union_consumed.py`; 1791 hard-keep and junction tests
and 351 framing / primary-impact tests pass.

## [65] PRODUCT: edl refused to run because its own old output drifted from the selection

**Stage / area:** `artifact_sanitize/preflight.sanitary_preflight_errors` (edl)
**Status:** fixed.

After the junction retired `seg_025` and `seg_060` on `exec_052`, the
selection changed and the pipeline correctly sent the run back to `edl`. The
edl preflight then refused: `Stage 'edl' blocked, edl_unsanitary:
selection_edl_order_drift`. That check reads the *existing* `edl.json`, and
drift from the selection is exactly what running `edl` repairs, since it
rebuilds the EDL from the selection. A producer blocked by the staleness of
its own output can never run.

**Fix:** the edl preflight drops order-drift findings about its own output;
every other EDL sanitary finding (phantom VO and the rest) still blocks, as
do the gap and VO checks. Test: `tests/test_edl_preflight_self_drift.py`.

## [66] PRODUCT: two layup recoveries existed but were never called

**Stage / area:** `nugget_layup.ensure_deterministic_floor_before_refuse`
**Status:** fixed.

`nugget_layup_compose` hard-stops when its QC finds open high-salience
nuggets (`open_high_salience_nuggets`) or open must-keep talking points
(`open_must_keep_talking_points`). Both have purpose-built recoveries in the
same module: `recover_open_high_salience_nuggets` (attach to an aired layup
or unskip a native to carry it) and `recover_open_must_keep_talking_points`
(attach to a layup already carrying it, or discharge when the selected
native already covers it; never invents VO). **Neither was called anywhere.**
The one deterministic heal pass before the refuse only handled
materialize / spine. So every layup run that left one such id open was a
hard stop: exec_050 on `tp_001`/`tp_002`, exec_052 on `nug_009` and
`tp_002`. Each re-run of the layup (it re-runs whenever the selection
changes) is a fresh model call that can leave a different id open.

**Fix:** the heal pass calls both recoveries, once each, only when QC names
that error. Still one deterministic pass, no ladder. On exec_052's plan it
discharged `tp_002` (`already_on_tape`) and QC came back clean. Tests:
`tests/test_layup_floor_recoveries.py`; 1965 layup tests pass.

## [67] MILESTONE: the full one-hour source reaches 72 of 72

**Status:** achieved, with re-entries; a clean from-scratch rerun is in progress.

`exec_052` on `mohan_uttarwar_podcast_transforming_cancer_science_direct.wav`
(59.5 minutes, real model on the new key, default tier routing for
`boundary_topic_resplit`): **72 of 72**, analysis 35 of 35 and delivery 37 of
37. `master/master.wav` and `publish/audio.mp3` at 48.1 minutes, cover,
chapters, description, transcript, `package_ready.json` true. **No 429 / TPM
errors** anywhere in the run; the old account's override was not needed.

It needed re-entries: long-source defects that six-minute clips never reach
(memory, entries 54 and 58), delivery-band disagreements between checks
(55 to 57, 59, 61, 63 to 65), two recoveries that were never wired (66), and
a performance defect (60) that made each between-stage decision take minutes.
Entry 62 replaced four drifting copies of the ordering exceptions with one
authority.

**Performance after entry 60:** a delivery pass that took 6293 s before the
fix took 300 to 530 s after it on the same run.

## [68] PRODUCT: the tidy-up pass moved the guest's farewell off the end of the episode

**Stage / area:** `air_order_integrity.pull_mid_arc_reverse_jumps` (called from
`full_master_ranking` topo repair and `repair_air_order_integrity`)
**Status:** fixed.

On the from-scratch one-hour run `exec_054`, the tape has a second block of
conversation after the guest's farewell. `full_master_ranking` deliberately
placed that later material first and ended on the farewell `seg_063` ("Mohan,
thank you very much"), and said so in `selection.notes`. The deterministic
"mid-arc reverse jump" pass saw a jump back in tape time into the last slot,
treated it as a mistake, and moved `seg_063` in front of the later material.
The EDL narrative audit then (correctly) failed the episode for continuing
after the farewell, and the frozen selection could not be re-ordered, so the
run stopped at 57 of 72.

**Fix:** a reverse jump into the **final** slot is not pulled when the final
segment's own text reads as a farewell (`is_farewell_text`: "thank you very
much", "thanks for joining", "all our best", "goodbye" and similar, on its
closing words). `closing_segment_ids(ctx, order)` computes that from the
manifest and is passed through `repair_selection_order` / `topo_satisfy_order`
by every caller that has the run context. Earlier-tape leftovers appended after
a sign-off (the original bug the pass was written for) are still pulled. Tests:
`tests/test_farewell_last_protected.py`; 234 ordering tests pass.

## [69] PRODUCT: the high-gap lint ignored the layup's recorded decision to skip a seam

**Stage / area:** `deterministic_lint._lint_optimal_questions` (pre-flush barrier
for `gap_framing_compose` and others)
**Status:** fixed.

Every one-hour run hit `Pre-flush commit barrier failed: high gap segment
seg_NNN has no interviewer line` (exec_052 `seg_067`/`seg_019`, exec_054
`seg_076`/`seg_068`), and it only ever cleared by luck on a retry. On
`exec_055` it did not clear and the driver stopped at 48 of 72.

The gap report was under `nugget_layup_authority`, and the layup plan had
decided `seg_059` explicitly: a typed skip with `skip_reason_code:
no_eligible_unspent_nugget` and a stated compensating path. The layup module
itself documents the policy ("layup only owes a line when it claimed air for
that target"). The lint did not know about layup decisions and still demanded
a line, so the two owners disagreed and the barrier refused.

**Fix:** under layup authority, a high-gap segment the plan typed-skipped
counts as decided (`_layup_decided_targets`). An untyped skip is not a
decision, and without layup authority the line is still owed. Tests:
`tests/test_high_gap_layup_decided.py`; 387 related lint tests pass.

## [70] POLICY (for upstream review): audit complaints that need a re-rank, after the order is frozen

**Stage / area:** `edl_narrative_audit`, `artifact_repairs.repair_edl_audit`
**Status:** changed; flagged for upstream decision.

On `exec_055` the EDL narrative audit (a model call) failed the episode with two
`selected_continuity_broken` issues: a short fragment opening a chapter, and
the farewell not being the very last item. Both are fair editorial notes. The
audit's own remedy for both is "rerun full_master_ranking". But by the time
the audit runs, the selection is hard-frozen (the seat freeze stamped after
`vo_synthesize`), and the audit repair itself logs "skip selection rewrite
under hard freeze". So the demanded fix can never happen: the audit failed its
own commit barrier on every pass and the run stopped at 57 of 72. exec_054
stopped the same way.

**Change:** when the order is frozen, a blocking issue whose remedy is a
re-rank (`rerun full_master_ranking`, `re-rank`, `reorder the selection`) is
demoted to a warning marked "needs operator re-rank", and the verdict is
recomputed. Any other blocking issue (phantom VO, missing audio and the rest)
still blocks. Tests: `tests/test_audit_rerank_demoted_under_freeze.py`.

**Why this is a policy call:** it ships an episode the audit had editorial
notes on, instead of stopping. The alternative is a design change: either run
the narrative audit before the seat freeze (so a re-rank is still possible),
or give the audit a sanctioned "unfreeze and re-rank once" path. Both are
upstream decisions.

Also in this commit: two earlier edits in this session had written literal
backspace characters into regexes through a shell heredoc; a repo scan for
`` now comes back empty.

## [71] PRODUCT: the audit flagged a deferred transition that is synthesized later by design

**Stage / area:** `artifact_repairs._edl_issue_contradicted_by_disk`,
`transition_vo` deferred pairs
**Status:** fixed.

With entry 70 in place, `exec_055`'s audit still blocked with
`transition_missing: rerun transitions to place or replace the deferred
seg_037-to-seg_041 bridge`. That pair sits in `deferred_transition_pairs`
with its spoken text; `transition_vo.stamp_transitions_pair_freeze` documents
that such pairs are "synth only in mix last-chance", which happens after the
narrative audit. The audit was judging a seam that is not supposed to exist
yet. The repair already demotes the same shape for VO placement ("NLE
placement is edl's job").

**Fix:** a `transition_missing` complaint naming a pair that is deferred with
text is contradicted by disk and demoted. A missing pair that is not deferred
still blocks. Test in `tests/test_audit_rerank_demoted_under_freeze.py`.

## [72] PRODUCT: junction's bounded budget ran out on three unfixable-by-design residuals

**Stage / area:** `junction_snip_qa` (detector and repair ladder)
**Status:** fixed.

On `exec_055` the pre-mix junction pass (allowed by the ordering authority,
entry 62) failed with `critical_incomplete_cut_residuals,
critical_junction_residuals_after_two_runs`. The three residuals were:

1. `hollow_opening_music`: no audible cold-open theme WAV. That WAV comes from
   the music band, which has not run on the pre-mix pass. Premature. It is now
   only raised once the music epoch is complete.
2. `seg_031` `cut_earlier` recorded `skipped_clip_index_mismatch`: earlier
   fuses/omits in the same apply pass had shifted clip positions, so the
   stamped `clip_index` no longer pointed at the clip. When the segment id is
   unique among speech clips the index is ignored; multi-appearance ids still
   honour it.
3. `seg_058` `on_a_roll`: the thought-complete recut found no close, no EDL
   neighbour could fuse, and omit is refused for a hard keep. The ladder
   recorded `skipped_no_recommendation`, the next detect raised the same
   critical again, and the two-run budget ran out. The ladder now records an
   explicit decision on the NLE override (`accepted_hanging_end:
   hard_keep_no_recut_no_fuse`, status `accepted_hard_keep_hang`); the
   detector does not re-raise an on-a-roll finding for a clip carrying that
   decision when no extend or cut exists. This is not a severity soften (the
   module's S3 rule): it is a durable, logged decision about one clip that has
   no legal repair.

Tests: the two ladder tests that pinned the old status now assert the
accepted decision and that the keep stays on air; 1571 junction tests pass.

---

## [73] PRODUCT: omitting the segment that carried a retired hard keep dropped the keep

**Stage / area:** `hard_keep`, `edl_overlap_repair.consumed_segment_ids`
**Status:** fixed.

On `exec_055` mix failed with `sanitize_refused:selection:
hard_keep_missing_from_order:seg_060`. Entry 64 lets a hard keep be retired
when an on-air neighbour's span covers at least 90% of its tape (the keep
still airs under the neighbour's id). Here `seg_059` carried `seg_060`, so
`seg_060` was retired and dropped from the keep list. A later junction pass
then flagged `seg_059` itself as `on_a_roll`; it was not a hard keep, so the
omit was allowed (`End-A omit permitted (junction_incomplete_cut_omit)`). With
`seg_059` gone, `seg_060`'s tape no longer aired, it became a hard keep again,
and it was missing from the order. Every guard was locally right; the keep's
protection just did not follow its tape.

Fix: when a keep is retired as covered, the on-air carrier inherits the keep
(`consumed_carrier_ids`). Junction, overlap repair and sanitize all read the
one keep list, so the carrier can no longer be omitted. Fuse-reason retires
(no geometric carrier) are unchanged.

Run repair: `seg_060` restored after `seg_058` in selection and EDL, its NLE
exclude cleared (backups of the three files kept outside the run dir). Resume
from `selection_order_sanitize`.

Test: `test_carrier_of_a_retired_keep_inherits_the_keep`.

---

## [74] STRUCTURAL: one authority decides whether a segment may come off air

**Stage / area:** new `removal_authority.py`; hooked at
`air_order_boundary.commit_selection_mutation`, `nle_state.save_nle`,
`edl_overlap_repair._update_nle`, and junction's omit ladder
**Status:** fixed.

Entries 55, 57, 64, 68 and 73 are one bug in five costumes: a producer took a
segment off air that another rule needed on air. Ranking, junction QA, overlap
repair, media-IP CTA, the NLE and the sanitize repairs each carried a private
"not if it is a hard keep" guard, and the guards disagreed about what a hard
keep was as soon as a keep's tape moved under another segment. Each local
guard was right on the state it saw; the run still shipped without the keep,
then died at mix with `hard_keep_missing_from_order`.

There are exactly two ways off air: an air-order write that drops the id, and
an NLE override that sets `excluded`. Both now pass through
`removal_authority` before they land:

- `refuse_selection_removals` puts a protected id back after its nearest
  surviving predecessor and logs who tried. It judges under both the previous
  and the proposed order, because a carrier is protected only while its
  retired keep is covered (the previous order); in the proposed order the
  keep is bare and the carrier looks free.
- `refuse_nle_excludes` clears a proposed `excluded` on a protected id (and
  stamps `removal_refused`), judged under the proposed overrides so a
  legitimate retire under a carrier (entry 64) still passes. A split's parent
  exclude is not a removal.
- `hard_keep_segment_ids` and the consumed helpers take the proposed
  `overrides` / `on_air` so the judgement is on the state the write would
  produce, not the state on disk.
- Junction's ladder re-asks the authority against its in-progress overrides
  before each omit, since its start-of-pass keep list goes stale within the
  pass.

Producers keep their early local refusals (cheaper, better messages); the
decision that counts is the authority's. Same shape as the ordering
authority (entry 62). A guard test fails if either write point stops
consulting it.

Quality: unchanged. It blocks only removals that were never allowed and that
today end the run. Cost: one keep-list evaluation per write.

Tests: `tests/test_removal_authority.py` (the exec_055 shape through both
write paths, a free segment still removable, a retire under a carrier still
allowed, the guard); 1229 selection/NLE/junction/sanitize tests pass.

---

## [75] PRODUCT: junction and mix held each other, and an inherited halt stopped the driver

**Stage / area:** `delivery_guardrails.upstream_stale_blockers`,
`identical_failures.sync_identical_halts_with_product`
**Status:** fixed.

On the exec_055 resume after entry 74, mix refused (`incomplete_cut_unresolved`:
seg_058 ends mid-thought, junction must recut first) and junction refused
(`stale upstream assembly_stale_versus_edl`: the assembly predates the EDL,
mix must render first). The conductor's candidate filter dropped junction on
the stale assembly and walked mix alone, twice, then the driver halted on
`identical_failure mix x6/3`.

Two defects:

1. `upstream_stale_blockers` was a fourth ordering check with its own copy of
   the junction-before-mix rule (entry 62 covered three). When the ordering
   authority says junction runs ahead of mix (a recut is owed), a stale
   assembly is mix's job afterwards, not a reason to hold junction. It now
   asks `ordering_exempt`; `master_finalize` and `mmaudio_sfx` still wait for
   a fresh assembly. The guard test in `test_ordering_authority.py` now covers
   this check too.
2. The x3 halt counter is only cleared for the EDL repair chain when the code
   changes. The "complete junction before mix" signature was first counted at
   09:16 under code two fixes old and reached x6 before either fix could run.
   A halt counted under previous code is not evidence about the current code:
   a product fingerprint change now clears every halt (the module's own
   stated intent, "patch-and-resume must never inherit a prior x3 halt").

Not changed: the detector. seg_058 and seg_060 are 80 ms apart on tape, so
the "hanging" end is the tape continuing in the next clip; the right repair
is junction's fuse (a union, legal for a hard keep), which it could not reach
only because of defect 1. An attempt to have the detector skip such ends was
withdrawn: `test_same_speaker_incomplete_prefers_thought_complete_recut`
pins that these seams are recut, not ignored.

Tests: `tests/test_junction_before_mix_deadlock.py` (junction released when a
recut is owed, held otherwise, finalize still held, inherited halt cleared).

---

## [76] STRUCTURAL: every gate on the junction-before-mix path now yields to the ordering authority

**Stage / area:** `air_order.assert_consumer`; sweep of all junction start gates
**Status:** fixed.

Right after entry 75, the resume halted at junction's own start:
`SystemExit: assembly_not_rendered_from_current_edl: verify_commitment
reasons=claimed_repairs_missing_from_edl,assembly_not_rendered_from_current_edl`.
`assert_consumer` verifies, for junction, that the assembly on disk was
rendered from the current EDL (A5). When junction is seated ahead of mix
because a recut is owed, the assembly predates the EDL by design and mix
re-renders it afterwards; the check halted with no legal producer (mix wanted
junction first). Its comment even said "not whenever junction precedes mix":
the sixth gate to keep a private copy of the one rule.

Executive decision, instead of finding these one run at a time: every gate
junction passes at start was enumerated and probed on the exec_055 state
after the fix. All open when the authority seats junction ahead of mix:

| gate | module | entry |
|---|---|---|
| seed order | `homunculus.runtime._seed_prereq_block` | 62 |
| LLM flow hardening | `llm_flow_hardening.maybe_require_upstream_llm_progress` | 62 |
| music epoch lock | `delivery_guardrails.mix_epoch_block` | 62 |
| stale upstream | `delivery_guardrails.upstream_stale_blockers` | 75 |
| conductor filter | `delivery_guardrails.filter_delivery_candidates` (via stale) | 75 |
| commitment | `air_order.assert_consumer` | 76 |
| admit schedule, lifecycle pre-stage, `may_skip_as_complete`, `next_delivery_seat` | probed open, no change needed | |

`assert_consumer` skips the pre-mix commitment check for junction when
`ordering_exempt(ctx, "junction_snip_qa", "mix")` is set (in addition to the
missing-assembly case). A5 is unchanged when no recut is owed: a stale
assembly still verifies and refuses. `master_finalize` still verifies always.

The ordering-authority guard test now lists all five code gates; a sixth
private copy fails the suite. `tests/test_junction_resume_gate_matrix.py`
pins the exec_055 resume shape (assembly on disk, EDL newer, recut owed)
against each gate.

Output quality: unchanged. These are scheduling gates; the audio decisions
(what junction recuts, what mix renders) are the same.

---

## [77] PLATFORM: the job API crashed creating a run when executions_root is outside the repo

**Stage / area:** `web/server.py` `create_run` and the existing-run branch
**Status:** fixed.

First thing the GUI path did on this machine: `POST /api/runs` returned 500,
`ValueError: 'C:\mux-local\executions\exec_056_...' is not in the subpath of
'<repo>'`. Both run-creation responses built their `run_dir` field with
`run_dir.relative_to(root)`, which assumes executions live under the repo.
`config/app.local.json` puts them at `C:\mux-local` here (path-length and
cloud-sync reasons, see docs/cross-cutting/windows-cuda-setup.md), and any
macOS checkout that sets `executions_root` elsewhere would hit the same. The
CLI driver never touched this code, which is why 72-of-72 runs passed
without it.

Fix: `_run_dir_label()` returns the relative path when the run is under the
repo and the absolute path otherwise. The field is informational.

Found while reproducing the reported serve wedge through the GUI path
(serve + full-auto driver over the job API) rather than the CLI driver.

---

## [78] PLATFORM: the full-auto driver never saw G0 accepted when executions live outside the repo

**Stage / area:** `tools/full_auto_driver.py` `bind_run`, `tools/full_auto_daemon_launch.py`
**Status:** fixed.

Second GUI-path finding on this machine. After auto-accepting G0 the driver
logged `no pending stages but pipeline incomplete, waiting` forever (twice,
exec_058). `g0_complete()` looks for `.stage_done/transcript_review` under
`MASTER.parent.parent`, and `bind_run` built `MASTER` from a hard-coded
`REPO/ASSETS/executions/<run_id>`. With `executions_root` at `C:/mux-local`
that directory does not exist, so G0 never read as complete, `build_bodies`
returned nothing, and the driver waited. The server, reading the real run
dir, reported the gate closed the whole time.

Fix: `bind_run` resolves the run dir through `RunContext`, falling back to
the repo path; the daemon launcher's newest-run discovery uses the config
`executions_root` the same way. exec_060 passed G0 into analysis on the
first try after the fix.

Same class as entry 77 (and, for the CLI, entry 27): code outside
`RunContext` guessing where runs live. A macOS checkout with the default
`ASSETS/executions` never hits either.

---

## [79] STRUCTURAL: one in-process engine drives full-auto and partially-accelerated runs; the GUI keeps two gates

**Stage / area:** new `interview_mux/orchestrator.py`, `cli.py orchestrate`,
`web/server.py /execute`, `tools/full_auto_daemon_launch.py`
**Status:** phase 1 landed (engine + launch); phase 2 (prune) after one GUI-path run.

Requested by the maintainer after the stalled run on macOS: the GUI and the
job API are only needed at G0 (transcript review) and at a final listen and
cover sign-off before publish. Everything else should run in one process.

What was there: `tools/full_auto_driver.py` (15,010 lines) drove every stage
by calling `POST /execute`, so stages executed inside the web server, with the
driver polling `/job` and healing by HTTP. `run_analysis` / `run_delivery`
raise "resume=<stage>" and stop; the only in-process re-entry loop in the
project was `tools/stub_pipeline_smoke.py --orchestrated`, the loop behind
every 72-of-72 run on this machine. Nothing gated `podcast_publish`: the
`require_g_publish_clear` helper is documented as dead, and partial-auto
waited for the operator *after* publish.

Phase 1:

- `interview_mux/orchestrator.py`: that loop, promoted. Analysis, then
  delivery; re-enter a phase while gates clear or stages progress; dispatch a
  stage a failure names as its own remedy; hop back to analysis when
  delivery invalidates one; stop early on the same error with no progress.
  In `partially-accelerated` mode it waits at G0 (polls the transcript
  review marker the GUI page writes) and, new, before `podcast_publish`
  (polls `g_publish_cleared` / `g_publish_skipped`, written by the GUI's
  g-publish page); delivery runs with `until_stage` one short of publish and
  resumes after sign-off. In `full-auto` it signs both off itself. The consent
  chain (framing, gap VO delivery, voice reference, clone consent, timeline
  optimizer) is signed off through the real functions in both modes, as the
  CLI loop always did. The run lock is held only while a phase executes,
  never while waiting, so the GUI's gate POSTs get through.
- `python -m interview_mux orchestrate --mode M (--run-id R | --input A)`.
- `POST /execute` returns `deferred` while a live orchestrator owns the run
  (`run_meta.orchestrator`, pid checked). The GUI posts there after every
  gate it completes; a second walk inside the server next to the engine is
  the dual-driver problem the job API had.
- The launcher spawns the orchestrator instead of the driver. The driver is
  reachable behind `MUX_LEGACY_DRIVER=1` for one release.
- `gui_job.json` is written by the engine (running / gate / done / error) so
  the GUI's job poll keeps showing progress.

Phase 2, after the engine has done one run through `run.sh` on the GUI path:
delete `full_auto_driver.py`, the keepalive loop, the daemon's driver paths,
and the server's in-process execute modes the driver alone used. The map of
what the driver did that `src/` does not (a survey is in the branch notes) is
short and none of it was needed for 72 of 72.

Tests: `tests/test_orchestrator.py` (full-auto never waits; partial waits at
G0 and before publish and never signs G0 off itself; remedy dispatch; gate
timeout; ownership only while the process lives; the three remedy shapes).

---

## [80] PLATFORM: the pid liveness probe was TerminateProcess on Windows

**Stage / area:** `process_cleanup.worker_pid_alive`, `driver_singleton._pid_alive`,
`thrash_hardening` (driver claim check)
**Status:** fixed.

Three places asked "is this pid alive?" with `os.kill(pid, 0)`. That is a
probe on POSIX. On Windows, `os.kill` with any signal other than the CTRL
events calls `TerminateProcess`: with the right to do so, the probe kills the
process it asks about; without it, `OSError` reads as "dead". On this machine
it read the live orchestrator (pid 21212, 1.5 GB resident) as dead, so
`orchestrator_owns_run` returned None and `POST /execute` was not deferred.
The same probe guards the driver claim (`thrash_hardening`) and the
dual-driver refusal, so on Windows those were either blind or dangerous.

Fix: one read-only implementation. Windows uses `OpenProcess` with
`PROCESS_QUERY_LIMITED_INFORMATION` and `GetExitCodeProcess == STILL_ACTIVE`;
POSIX keeps `os.kill(pid, 0)`. The two other sites call it; the
`driver_singleton` fallback to `os.kill` is gone. A test asserts `os.kill` is
never called on win32 and that the helper sees its own process and not a
bogus pid.

Found while proving the orchestrator through the GUI path (exec_061).

---

## [81] PRODUCT: narrative_arc_plan cited segments that connector fusion had retired, and the write barrier refused the whole plan

**Stage / area:** `artifact_repairs.repair_narrative_plan` (delivery stage 38 `narrative_arc_plan`, then `chapter_close_hitch`)
**Status:** fixed.

Reported from the maintainer's macOS run (exec_004, one-hour source, CLI
orchestrated with a real key): `WriteApprovalBlockedError: Pre-flush commit
barrier failed: ordering_constraint segment seg_052 not in manifest;
heal_success:pre_flush_soft_refused`, identical failure x3, halt at 38 of
72. The LLM wrote ordering constraints citing seg_052 and seg_057. After
connector fuse and resplit the live manifest had 26 rows ending at seg_046;
the ids came from the content brief the prompt also carries. The lint is
right to refuse an id that does not exist, but refusing the whole plan for
one bad constraint leaves no legal producer: re-running the stage asks the
same model the same question.

Fix: the narrative plan repair, which already runs before the write,
resolves every constraint to the live manifest. A retired id that a
manifest row's `fused_from` names is remapped to that survivor (its tape
still airs there); an id nothing accounts for drops the constraint; a
constraint whose two ends collapse onto one survivor is dropped. Logged as
`resolve_constraint_refs_to_manifest {remapped, dropped}`. Chapter refs were
already filtered this way; constraints were not.

Resume on the affected run: `--from narrative_arc_plan`.

Tests: `tests/test_narrative_plan_orphan_constraints.py` (remap, drop, keep;
collapse; the lint accepts the repaired plan).

---

## [82] PRODUCT: the GUI did not see an engine-driven run, then offered a manual Run button beside a running stage

**Stage / area:** `cli.py orchestrate` (run creation), `orchestrator._stamp`
**Status:** fixed.

Two GUI-facing gaps in the engine's first real run (exec_062):

1. The Executions tab showed "No runs yet". The engine's create path
   imported the session setter from `interview_mux.web.session`, a module
   that does not exist; the import was inside a try/except and failed
   quietly, so serve never learned about the run. It is
   `interview_mux.application_session.set_active_execution`, which writes
   the state file serve reads. The legacy driver did this over HTTP.
2. With the run visible, the stage workbench showed "Complete this stage:
   Run Generate theme audio" above a card that said the same stage was
   running. The engine stamped `partial_auto_driver_active = False` for
   full-auto runs (the name suggests partial only). The GUI reads that flag
   in both modes as "a driver owns this run", and an explicit False makes
   it offer manual actions. The legacy driver's claim set it True in both
   modes. The engine now sets it True while alive and False when it exits.
   Clicking the button would have been harmless (`/execute` is deferred
   while the engine owns the run, entry 79), but the prompt was wrong.

Tests: `test_engine_declares_driver_ownership_in_both_modes`.

---

## [83] GUI: a deliberately skipped optional step shows as FAILED

**Stage / area:** stage workbench card for `missing_framing` / Fill gaps
**Status:** fixed (the output view reports skipped gap-fill outputs as skipped; the card is gone).

On a run whose pipeline mode is native-only (`gap_fill_mode: skipped`,
reason "pipeline_mode native_only, skip gap-fill VO"), the Fill gaps step
shows a red FAILED card: "Interviewer script incomplete,
understanding/gap_framing_plan.json is pending". The stage was skipped on
purpose and every downstream stage completed; the card reads a missing
optional artifact as a failure. The maintainer's macOS run showed the same
card at the same step. The stage row should report skipped with the reason.
Fix: `artifact_lifecycle.build_outputs_view` reports the outputs of the five
gap-fill stages as `skipped` when gap fill was skipped, and
`stage_output_mode` returns `optional_skipped` for all five (it did for
three). The GUI treats a skipped output as satisfied. No run output changes.

---

## [84] PRODUCT: MusicGen deadlocked on its own stderr pipe for the whole generation timeout, twice

**Stage / area:** `musicgen_runner._spawn_musicgen` (macOS and Windows alike)
**Status:** fixed.

exec_062, `mmaudio_sfx`: a 6-second stinger on `facebook/musicgen-small`
showed "MusicGen generating" for 15 minutes, was killed at the 900 s
timeout (rc -9), and the ladder's retry did the same. The GPU sat at 5
percent; the child had used 116 CPU-seconds. A `py-spy dump` of the child
showed the main thread inside `tqdm ... fp_write`, writing the
model-loading progress bar to stderr. The parent captured stderr through a
pipe but only read it after the process exited: it polled with
`proc.wait(timeout=30)` in a loop and called `communicate()` at the end.
Once the child's output exceeded the pipe buffer (64 KB) it blocked on
write forever, the parent waited out the timeout, and the retry repeated
it. The newer `transformers` loader prints one progress line per tensor,
which is why this began now. Nothing platform-specific: the pipe buffer
is the same size on macOS.

Fix:
- The parent drains stdout and stderr on reader threads while it polls
  (the operator-subprocess runner already did this; MusicGen had its own
  loop). On timeout the collected stderr tail is kept in the result.
- The child environment disables progress bars (`TQDM_DISABLE`,
  `HF_HUB_DISABLE_PROGRESS_BARS`), loads a model that is fully in the local
  cache offline (`HF_HUB_OFFLINE`, `TRANSFORMERS_OFFLINE`), and bounds the
  Hub timeouts otherwise, so a dead Hub connection cannot stall a load
  either. Explicit values in the caller's environment win.

Tests: `tests/test_musicgen_pipe_drain.py` (a child that floods stderr with
12,000 progress lines finishes in seconds; a hung child is still killed at
the timeout with its stderr tail kept) and
`tests/test_hub_offline_and_skipped_outputs.py`.

---

## [85] PRODUCT: the delivery conductor handed `mix` to music and music back to `mix`, and ran neither

**Stage / area:** `delivery_guardrails.defer_until_producers_ready` (the
candidate filter every delivery walk goes through); platform independent
**Status:** fixed.

exec_062, delivery resumes 4 and 5: the conductor pinned the seed front to
`mix`, walked it, and returned "Delivery incomplete after conductor,
remaining stages: mix; resume=mix" twice in a row without running anything.
The legacy driver could not get past this (exec_060 halted here at 62 of 72);
the orchestrator escaped only because it dispatches the named remedy stage
directly when a resume makes no progress, which bypasses the filter.

The walk log shows the loop. `walk_seed_agenda([mix])` runs
`filter_delivery_candidates`, which asks `defer_until_producers_ready` about
`mix`. The sound design plan (written at stage `sound_design_plan`) already
lists a `theme_outro` asset but no close cue, because the cue is placed by
`music_palette_compose`, which has not run yet. The filter's outro rule
("mix fail-closes on a missing theme_outro; do not walk mix until compose
lands it") therefore replaced `mix` with `music_palette_compose`. The walk
then applied the HAU speech-first rule: with only `assembly_preview.wav` on
disk, music may not spend, so `music_palette_compose` was refused and the
walk asked for `mix` again, which the filter turned into
`music_palette_compose` again, which was skipped as "already in speech-first
walk". Each rule deferred to the other's stage. Direct dispatch of `mix`
proved the outro rule is moot in this state: speech-first mix seats the
assembly with beds deferred, and compose places the close cue afterwards.

Fix: the outro reinjection is skipped while `hold_speech_first_mix` holds
for `mix` (music cannot admit until mix has seated the assembly). Once music
may admit, the rule behaves as before. A helper `_speech_first_holds` keeps
the predicate next to the HAU exception table it belongs to.

Tests: `tests/test_speech_first_outro_deadlock.py` (speech-first mix is not
deferred to compose for the outro cue; the same state with an admitting
assembly still is; `filter([mix])` returns `[mix]` in the exec_062 shape).

---

## [86] PRODUCT: a prerequisite with a stale `.stage_done` was never rerun, so the seed walk looped on it

**Stage / area:** `homunculus.agenda.walk_seed_agenda` (prerequisite
autorun); reported from macOS exec_005, platform independent
**Status:** fixed.

Upstream exec_005 (one-hour source, real key): `full_master_ranking` ran,
the pre-flush commit barrier refused its staged `selection.json`
(`heal_success:pre_flush_soft_refused`, see entry 89 for the order itself),
and dispatch recorded the stage as failed. A `.stage_done` marker for it was
on disk anyway. Every later pass then raised "seed order: complete
full_master_ranking before running refinement_agenda": `dispatch_stage`
judges the prerequisite by seed completeness, which was false, but the
walk's prerequisite autorun only reruns a prerequisite whose marker is
absent (`not ctx.is_done(prereq)`), so it never reran ranking. The
identical-failure counter on `refinement_agenda` then halted the run.

Fix: `_seed_prereq_needs_run` decides whether the named prerequisite is
worth a run. Marker absent: yes. Marker present and seed-complete: no.
Marker present but not seed-complete: drop the marker (it is the lie, the
check is right) and rerun. One rerun per prerequisite per walk, as before.

Tests: `tests/test_seed_prereq_stale_marker.py`.

---

## [87] PRODUCT: a courtesy rewrite of `content_brief.json` inside ranking was logged as an authority denial, and two of those halt the run

**Stage / area:** `artifact_repairs.propagate_nle_split_segment_refs`, called
from CTA child materialize and NLE splits during `full_master_ranking`
**Status:** fixed.

Same exec_005. While ranking materialised split children it remapped the
parent segment id in `understanding/content_brief.json`, which in the
`pre_soft_freeze` epoch belongs to `content_brief_reanchor`. The write was
wrapped in try/except, so the stage did not fail on it. But the ownership
layer had already logged `authority_denied:persist:understanding/content_brief.json:full_master_ranking:pre_soft_freeze:content_brief_reanchor`,
recorded an identical failure, and on the second occurrence stamped
`authority_denied_halted` into `run_meta.json`. That stamp is a sticky halt
the conductor honours, so a write nobody needed became a stop.

Fix: the remap asks `write_permitted` first and skips the brief with an
info line when the running stage may not touch it; the owner rewrites the
brief on its next pass anyway (the segment-id sync at approve time already
covers `content_brief_reanchor` and `segment_classification`).

Tests: `tests/test_seed_prereq_stale_marker.py` (denied remap: no denial
logged, brief untouched; permitted remap: rewritten as before).

---

## [88] STRUCTURAL: `tools/stub_pipeline_smoke.py --orchestrated` now runs the engine instead of its own copy of the loop

**Status:** fixed.

The maintainer runs the pipeline with this tool. It carried the resume loop
the orchestrator was promoted from (entry 79), minus what the engine gained
since: after a hop back to analysis it ran delivery once with no resumes and
no remedy dispatch, and stopped. exec_005 ended exactly there ("hop 1:
delivery -> FAIL, seed order: complete full_master_ranking ...") where the
engine would have dispatched `full_master_ranking` directly and carried on.

`--orchestrated` now constructs `interview_mux.orchestrator.Orchestrator`
in full-auto mode and prints the same phase table; the linear walk is
unchanged. `resume_hint` and `MAX_GATE_RESUMES` are imported from the
engine so the tool cannot drift again.

---

## [89] OPEN: `full_master_ranking` produced an order the commit barrier refused (macOS exec_005)

**Status:** open, needs the run directory.

The ranking model returned a partial order that violated a narrative
ordering constraint and parked an early-chapter segment after the finale.
`finalize_selection_order` repairs both classes (`repair_selection_order`
runs three times, with a finale-tail pass), and an offline reproduction on
the one-hour exec_055 data with random partial, shuffled rankings (three
seeds, 30 percent of segments dropped) commits cleanly every time. So the
refusal depends on exec_005's plan and manifest: most likely a constraint
that conflicts with the protected closing ids or with a chapter span, which
the topological repair cannot satisfy and the barrier then refuses. Entries
86 to 88 make the run recover from the refusal (rerun ranking, no false
halt, engine keeps going). To fix the refusal itself the following files
from `ASSETS/executions/exec_005_20260929T213649Z` are needed:
`master/narrative_plan.json`, `segments/manifest.json`,
`.pending_writes/full_master_ranking/`, `operator/resilience_report.json`,
`operator/forensics_errors.json`, `run_meta.json`, and the run log.

---

## [90] PRODUCT: seven tolerated writes were logged as authority denials, and three of the same one halt a run

**Stage / area:** `RunContext.write_json` and the ownership layer; sites in
`split_plan_apply`, `gap_framing_recompose`, `air_contract_sanitize`,
`vo_line_adjudicate`, `edl`, and the flush fingerprint restamp
**Status:** fixed.

exec_062 (72 of 72) still carried 11 error-level lines. Every one was an
`authority_denied:persist:...` from a stage writing an artifact another
stage owns in the current epoch: `split_plan_apply` enriching
`segments/boundaries.json`, `gap_framing_recompose` and
`vo_line_adjudicate` republishing VO seats into `mastering_plan.json`, the
flush fingerprint restamp touching `gap_report.json`, `edl` unlocking
`episode_structure.json` and stamping `transitions_pair_freeze.json`, and
the air-contract sanitizer carrying its VO flags into `gap_report.json`. In
every case the caller already caught the exception and went on ("split_plan
skipped", "transitions pair freeze skipped"), so the pipeline was right to
continue. But the ownership layer had already logged an error, written
`forensics_errors.json`, and recorded an identical-failure signature. Three
of the same one set `halt=True` (`gap_framing_recompose` reached 3 of 3 in
this run), and two of them stamp `authority_denied_halted` into run_meta
(that is what stopped the maintainer's exec_005, entry 87). The sanitizer's
case was worse: its refused gap write failed the whole stage once.

Fix: `write_json(..., optional=True)` marks a courtesy write. The ownership
table is asked first; a refused optional write is skipped with one info
line and nothing else (no error, no signature, no halt, no forensics row).
The seven sites pass the flag. The sanitizer's gap write was made optional
here as well; that was wrong (its flags are required state, see entry 101)
and it now goes through the End-A stamp path instead.
exec_063 surfaced an eighth site of the same shape under the soft freeze:
the local-runtime last-error sidecar (`vo_pickup/local_runtime_last_error.json`,
written when a Chatterbox child prints invalid JSON but leaves a usable
WAV). Same fix.

Tests: `tests/test_optional_write.py`.

---

## [91] GUI: outputs a stage never produces show as pending after the stage is done

**Stage / area:** `artifact_lifecycle.build_outputs_view`
**Status:** fixed.

`junction_snip_qa` declares seven artifacts and writes three of them only on
some paths (thought-complete recut, failure review, remediation plan). On
exec_062 it finished in 15 seconds with nothing to recut, and the outputs
panel listed `master/junction_thought_complete.json` as pending; the T1
reconcile in `ui_truth` turned that into a red "Junction snip QA incomplete"
card on a stage that was done. Same shape as entry 83.

Fix: once a stage is done and its primary artifact is committed, declared
rows still pending are marked `n_a` ("not produced on this run"). The
primary artifact is never touched, so a hollow stage still reads hollow.

---

## [92] PRODUCT: a finished full-auto run kept asking for the G-Publish sign-off

**Stage / area:** `orchestrator` (full-auto), `gates.check_g_publish_pending`
**Status:** fixed.

Full-auto ran `podcast_publish` and reported "Run complete", and the GUI
banner still read "Package episode needs your input". `g_publish_pending`
is stamped when the master commits and is only cleared by the operator's
Continue or Skip; the engine signs that gate off in partial mode by waiting
for it, and in full-auto by running publish, which never stamped anything.
During the run the same banner showed too, inviting a click the engine
would then refuse.

Fix: after `podcast_publish` lands in full-auto the engine stamps
`g_publish_cleared` through the real `clear_g_publish` (package prepared),
and while a full-auto engine is alive the gate check answers "not pending".
Partial mode is unchanged: the operator still signs off.

Tests: `tests/test_orchestrator.py` (two sign-off cases),
`tests/test_g_publish_full_auto_engine.py`.

---

## [93] PERFORMANCE: transcription and the audio probes re-ran on every rerun of the same file

**Stage / area:** `transcribe`, `audio_probe_build`; new `stage_cache.py`
**Status:** fixed.

Per-stage timings from the two complete runs: on the one-hour source
`audio_probe_build` took 8.4 minutes and `transcribe` 6.1; on the 6-minute
clip 7.9 and 2.5 (the probes are dominated by model start-up). Both are
pure functions of bytes already on disk (the normalized audio, the
transcript) and configuration, and both ran again on every rerun of the
same file, which is the normal development loop.

Fix: a content-addressed stage cache beside the executions directory
(`<executions_root>/../stage_cache`, or `stage_cache.root`). The key is the
SHA-256 of the input files plus the model ids and the config block that
shape the output; the value is the stage's JSON outputs. On a hit the stage
writes the cached documents through `write_json` as if it had produced
them, so staging, ownership and completion are unchanged, and logs one
info line. `MUX_STAGE_CACHE=0` disables it; a changed model or config is a
miss. Nothing else is cached: every later stage depends on operator input
or a model call.

Tests: `tests/test_stage_cache.py`.

---

## [94] PRODUCT: the manifest hydrate at approve time wrote back into staging, so the nested classification refused its own commit

**Stage / area:** `write_staging.approve_stage_writes` (after flush);
seen under `boundary_topic_resplit`'s nested `segment_classification`
**Status:** fixed.

exec_062 and exec_063 both logged "Failed: Stage boundary_topic_resplit:
Stage segment_classification artifacts incomplete, segments/manifest.json
has newer uncommitted pending", then recovered on the next pass after the
walk unmarked classification as hollow and reran it. The sequence: the
nested stage flushed its manifest, `approve_stage_writes` hydrated the
committed manifest from the boundaries and, because the hydrate changed it,
wrote it again with `ctx.write_json`. The staging root of the nested stage
is still open at that point, so the rewrite landed in
`.pending_writes/segment_classification/`, newer than the commit, and the
completeness assertion two lines later refused exactly that. One wasted
pass per run, one error line, and a hollow-unmark of a stage that was fine.

Fix: the hydrate rewrite uses `write_committed_json`, which persists to the
committed tree without touching the staging root. Ownership is unchanged
(still attributed to `segment_classification`).

Tests: `tests/test_hydrate_rewrite_commits.py` (the replayed approve path
leaves no newer pending copy; the old shape is shown to be what the check
refuses).

---

## [95] PRODUCT: the orientation guard in gap_framing_recompose wrote the gap report under its own key, and the undo ledger halted the run

**Stage / area:** `refinement_passes.after_gap_recompose_or_skip`
**Status:** fixed.

exec_063 (partially-accelerated, first end-to-end attempt on the engine)
stopped at delivery resume 3 with
`authority_undo_thrash:understanding/gap_report.json: hash_oscillation`.
The chain: `gap_framing_recompose` republished the layup plan (the layup is
now the report's sole body writer), then its post hook ran the opening
orientation guard, which changed the report and wrote it back with the
stage's own key. The ownership table refused (`owner=nugget_layup_compose`),
the stage failed, and the authority-undo mechanism rolled the report back
to the older compose version. The next pass republished the layup, the
guard fired again, and the ledger saw compose hash, layup hash, compose
hash: an A-B-A oscillation, which is a halt. exec_062 saw the same guard
refuse under `vo_line_adjudicate` as a warning; this run hit it in the
stage that owns nothing.

Fix: the guard presents the report's current owner key,
`gap_report_body_owner(prior)`, the answer entry 36 gave the other
legitimate repairs. No refusal, no undo, no oscillation.

Tests: `tests/test_orientation_guard_owner_key.py` (layup authority: the
write carries `nugget_layup_compose`; before it, `gap_framing_compose`).

---

## [96] PRODUCT: the soft seat freeze refused the first production of `master/transitions.json`

**Stage / area:** `seat_authority.frozen_seat_write_allowed`; stamped by
`air_contract_sanitize`, hit by `transitions`
**Status:** fixed.

exec_063, after the sanitizer's gap write became optional (entry 90) and
the sanitizer therefore finished on its first pass: it stamped the soft
seat freeze (`soft_reason=air_contract_sanitize`), seed order then ran
`transitions`, the model answered, and the one-writer persist logged
"seat_freeze: skip write master/transitions.json (not End-A;
reason=transitions)". With no artifact the stage could not be marked done,
the attempt memo refused a second try, and the conductor stopped with
`remaining stages: transitions, edl_narrative_audit`.

Every earlier complete run (exec_052, 055, 062) shows `soft_reason:
"implied"`: the sanitizer had never reached its stamp because it failed on
the gap write first, transitions slipped in before any freeze, and the
soft flag only appeared later, implied by vo_synthesize's hard freeze. So
"first production of a seat doc under the soft freeze" had never run.

Fix: a seat doc that does not exist on disk is not a frozen seat. The
freeze protects existing seats from foreign rewrites; the first production
by seed order (`transitions`, then `sound_design_plan`) is allowed with an
info line. Rewrites of an existing doc and the End-A allowlist are
unchanged.

Tests: `tests/test_seat_freeze_first_production.py`.

---

## [97] PRODUCT: a cross-speaker source overlap had no repair, so strict EDL QC halted the run

**Stage / area:** `gates.check_edl_qc`, `edl_overlap_repair`
**Status:** fixed.

exec_063, `edl`: "Overlapping source range: seg_006 [223570,244430ms)
intersects seg_005 [222790,223950ms)". The manifest has seg_005 (spk_0,
"Right?") at [222580,223290) and seg_006 (spk_2) from 223570; a cut-edge
refinement extended seg_005's clip end to 223950, 660 ms past its own
bound and 380 ms into the next speaker's clip. The QC's only repair,
`repair_overlapping_source_ranges`, unions overlapping *same-speaker*
speech; across a speaker change it finds no component and returns
unrepaired, the strict gate raises, and the engine stops after the same
error twice ("same error as the previous resume and no progress").

Fix: `trim_residual_source_overlaps` runs after the union repair when
overlaps remain. It trims the earlier clip's end back to the later clip's
start (or the later clip's start forward when the earlier one would drop
under 200 ms), retimes the clips, logs the actions, and persists when the
EDL came from disk. The refinement that extends across a neighbour is the
next thing to look at; the gate no longer stops the run on it.

Tests: `tests/test_edl_residual_overlap_trim.py` (the exec_063 shape trims
380 ms off seg_005; a tiny earlier clip trims the later start instead; no
overlap, no action).

---

## [98] PRODUCT: partial mode packaged the episode without the operator's sign-off

**Stage / area:** `pipeline._run_steps` (ship walk after master),
`stages.podcast_publish`, `orchestrator`
**Status:** fixed.

exec_063 was the first partially-accelerated run driven end to end by the
engine. It waited at G0 as designed, then finished 72 of 72 with
`run_meta.g_publish_pending` still true: nobody had signed the final gate
off. The engine stops its delivery phase one stage short of
`podcast_publish` (`until_stage=episode_cover_generate`), and the conductor
honoured that for its own planning. But once master.wav is committed the
same phase runs "walk remaining ship stages", which lists every ship stage
whose outputs are missing and walks them, `podcast_publish` included. The
old GUI path never saw this because the server deferred publish itself;
the engine relies on the phase boundary, and this walk ignored it.

Fix, in three places so no walk can overrun the gate again:
- the ship walk filters its list through `stages_within_until`, the same
  boundary the planner uses;
- `run_podcast_publish` starts with `require_partial_signoff_before_publish`:
  in partial mode, with neither Continue nor Skip stamped, it marks the gate
  pending and halts with a gate message. Full-auto and manual runs are
  untouched (full-auto signs off after packaging, entry 92; manual reaches
  the stage only through the GUI's Continue). `require_g_publish_clear`
  stays dead as the clinic pin requires;
- the engine treats that halt as arriving at the sign-off point and waits
  there, exactly as it does after a clean stop at the boundary.

Tests: `tests/test_partial_publish_boundary.py`,
`tests/test_orchestrator.py` (a delivery phase that halts on the guard
makes the engine wait, and the run completes after Skip).

---

## [99] GUI: Continue at the final sign-off started a second packaging job under the engine

**Stage / area:** `web/server.py` `POST /api/runs/{id}/g-publish/continue`
**Status:** fixed.

exec_064 (partially-accelerated, engine-driven, zero error lines up to the
sign-off): the operator's Continue cleared the gate and, as the old GUI
path did, started the server's own `master_transcript_build ->
podcast_publish` job. The engine was already polling for that sign-off and
took the run lock first, so the server job failed with "Could not start
pipeline: directory lock busy", logged at error level, and wrote
`gui_job.json` status "error" while the engine packaged the episode
correctly one minute later. The run finished 72 of 72 with exactly this
one error line.

Tests: `tests/test_g_publish_continue_deferred.py`.

Fix: the handler asks `orchestrator_owns_run` after clearing the gate, the
same check `/execute` makes, and returns `deferred` instead of starting a
job when the engine owns the run. Skip already needs no job.

---

## [100] PRODUCT: a chapter-boundary clip whose hanging end was accepted was re-raised as critical every pass, so mix and junction handed the run back and forth

**Stage / area:** `junction_snip_qa.detect_junction_findings`
(`chapter_bleed_incomplete` branch); reported from macOS exec_006
**Status:** fixed.

Upstream exec_006 (partially-accelerated, real key): 61 of 72, then
`mix` refused twice with `incomplete_cut_unresolved` (residual kind
`chapter_bleed_incomplete`), the recovery pinned `junction_snip_qa`, junction
ran, mix refused again, four rounds of it. The clip: a hard-kept segment
that ends mid-thought exactly on a chapter join, with its EDL neighbour far
away on tape (the order jumps 039 -> 038 -> 042, two transitions suppressed
for clone adjacency).

Junction's ladder for that clip has three rungs: cut earlier (no complete
phrase end with room inside the clip), fuse into the neighbour (only when
the neighbour is source-adjacent; it is not), omit (refused: the removal
authority protects the segment, entry 74). Entry 72 gave the ladder a
terminal outcome for exactly this: record `accepted_hanging_end` on the
clip's NLE override and stop raising it. The detector honours that record
in its `on_a_roll` branch, and only there. The `chapter_bleed_incomplete`
branch never consulted it, so every fresh detect (which is what mix's
refusal runs) raised the same critical again, junction accepted it again,
and the two stages handed the run back and forth until the identical
failure cap.

Why it slipped in: the detector builds the same repair ladder three
times, once per incomplete-cut kind (`on_a_roll`, `chapter_bleed_incomplete`,
`incomplete_clause`), and entry 72's decision was wired into the one
branch exec_055 had hit. The other two copies were never touched, and even
the `on_a_roll` copy only honoured it when no cut or extend was
recommended, which is not the state the ladder records it in.

Fix: the decision is honoured in one place, the detector's `add`: any
critical finding of an incomplete-cut kind on a clip whose override carries
`accepted_hanging_end` is emitted as advisory (still in the QA report, with
`accepted_hanging_end: true` in its detail). Mix's live critical check no
longer sees it, assembly seats, and no branch can miss it again.

Reproduced offline: a two-chapter fixture with the clip ending on a comma
at the join and a non-adjacent neighbour raises the critical; with the
override it reports advisory and the live critical list is empty.

Tests: `tests/test_chapter_bleed_accepted_hang.py`.

---

## [101] PRODUCT: the air-contract sanitizer's VO flags never reached the gap report, so adjudicate refused a contract that disagreed with it

**Stage / area:** `artifact_sanitize.air_script.commit_air_contract`;
reported from macOS exec_008 (54 of 72, stopped at `vo_line_adjudicate`)
**Status:** fixed. Regression from entry 90.

exec_008: the hosted-VO floor was thin, so the sanitizer omitted two
preface lines in the execution contract to protect it
(`protect_hosted_vo_floor_reseat`). Those omits must be mirrored as
skip/omit flags on the matching `gap_report` lines; adjudicate compares
the two before synthesis and refuses when they disagree. The report never
got the flags: the sanitizer wrote it under its own stage key, which the
ownership table refuses once the layup owns the report, and entry 90 had
made that write *optional*, so the stage finished with the flags unlanded
instead of failing. Adjudicate then hard-blocked on the mismatch and the
contract stayed unsanitary.

Before entry 90 the same refusal failed the sanitizer outright (exec_062,
one resume), which was also wrong; the write is required, not a courtesy.
The other stampers already knew how to do this: `vo_contract` and
`omit_ledger` persist the flags with the End-A reason
`stamp_gap_omit_flags` and the report's current owner key
(`gap_report_body_owner`), which the ownership table allows because the
stamp changes no interviewer text.

Fix: `persist_air_contract_gap` does exactly that, and a refusal is an
error again. My runs did not show it because their VO floor never needed
an omit, so the unlanded write carried nothing.

The same audit found four more seat-truth writes that entry 90 had turned
into courtesy writes: the two seat repairs in `vo_contract`
(`_unseat_ineligible_plan_seats`, orphan unseat), the seat republish in
`hosted_vo_authority`, and the catastrophe fallback in `vo_bind_authority`.
Each now lands through `persist_frozen_seat_doc` under the seat owner's key
(`air_contract_sanitize`) with its End-A reason (`air_script_gap_omit_sync`,
`drop_seated_missing_from_gap`, `hosted_vo_disposition_apply`,
`catastrophe_seated_bind_synth_failed`). The remaining optional sites are
genuine courtesies: fingerprint restamp, boundary enrichment, the pair
freeze stamp, the volley unlock note, the runtime error sidecar.

A second sweep covered raw gap-report and transitions writes that never
went through entry 90 but sat on the same VO path: the execution-contract
waive (`_tier_d_logged_waive`), the synthesis fallback that flips a line to
`record`, and the split-child id remap in `propagate_nle_split_segment_refs`.
Each wrote under the active stage's key and would have been refused under
layup authority exactly like the sanitizer. They now use
`seat_authority.persist_gap_report_stamp` (owner key from
`gap_report_body_owner`, an End-A reason, the sole-writer text guard still
in force); the transitions remap presents the transitions owner with
`segment_id_remap_omit`.

A third sweep, from exec_065 here: the recovery ladder's tier-C opening
unseat stamps seats on the mastering plan through
`air_script.persist_air_script_omits_on_gap_report`, which called
`write_plan` under the running stage's key (`gap_framing_compose`) and was
refused. `write_plan` now takes a `stage_key`, and that helper presents
the seat owner with `stamp_gap_omit_flags`.

Tests: `tests/test_air_contract_gap_stamp.py` (the stamp carries the End-A
reason and the owner key under and before layup authority; a refusal
raises), `tests/test_seat_repair_owner_key.py`.

---

## [102] PRODUCT: a stage re-entered after a refused commit was blocked by its own stale staged files

**Stage / area:** `write_staging.run_wrapped_stage` (stage entry); seen on
exec_065 `gap_framing_compose`, and the shape the maintainer reported when
he asked whether to delete `.pending_writes` by hand
**Status:** fixed.

exec_065: the model's framing left high-gap segment seg_004 without a line,
the pre-flush barrier refused the commit
("heal_success:pre_flush_soft_refused"), the deterministic seed then covered
the gap, and the walk re-entered `gap_framing_compose`. It refused at
once: "gap_framing_compose blocked, missing_framing incomplete:
understanding/gap_evaluations.json has newer uncommitted pending". The
newer copy was the refused attempt's own staged file. Nothing discards a
stage's overlay when the barrier refuses it, so every re-entry of that
stage read the producer upstream of it as incomplete because of a file the
stage itself had left behind. Two resumes and three error lines to get
past it here; on a run with more high gaps it would exhaust the identical
failure counter.

Fix: `discard_stale_staging_before_entry` runs at stage entry. A stage that
is not done and still has staged writes from a previous attempt drops
them (logged with the paths) and starts from a clean overlay. The two WAV
stages keep their existing orphan promotion, and nothing is touched while
the operator approval flow owns pending writes.

Tests: `tests/test_stale_staging_discard.py`.

---

## [103] PRODUCT: a host present only as reactions read as a "starved host packet", and the attempt memo then locked the run out of missing_framing

**Stage / area:** `llm_preflight` (missing_framing input check)
**Status:** fixed.

exec_065 (6-minute clip, partially-accelerated): after the chapter-close
hitch reclassified the tape, the manifest held four interviewee answers
and two host rows typed `interviewer_reaction`; speakers.json names that
speaker as the interviewer. The input check for `missing_framing` counts
only `interviewer_question`, `interviewer_prompt` and `host_turn` as host
tape, so it refused with `starved_host_packet` ("resume speaker_roles or
segment_classification"). The engine dispatched `speaker_roles` as the
named remedy, which changed nothing; the attempt memo then refused every
re-entry of `missing_framing` ("dispatch refused for incomplete critical
missing_framing"), delivery reported analysis incomplete, the engine
hopped back to analysis, and the cycle repeated until the invoke cap: nine
error lines and no progress. Three earlier runs of the same clip had
typed those two rows as questions, so nothing here was seen before; the
classifier's choice decided which path ran.

Fix: `interviewer_reaction` counts as host tape. The check exists to stop
LLM spend on a hollow host packet; a host present only in reactions is
thin, not hollow, and what framing that needs is missing_framing's own
decision (it already handles no questions on tape).

Tests: `tests/test_starved_host_reactions.py`.

---

## [104] PRODUCT: the soft seat freeze kept the sound design plan's own stage from re-deriving a plan the selection had outgrown

**Stage / area:** `seat_authority.frozen_seat_write_allowed`; `sound_design_plan`
**Status:** fixed.

exec_065 (resumed after entries 102 and 103): the plan on disk came from
the pass before the restart, with a bed cue anchored on seg_007. Ranking
re-ran on the resume and excluded seg_007. `sound_design_plan` re-ran to
re-derive the plan, the model answered, and the one-writer persist logged
"seat_freeze: skip write understanding/sound_design_plan.json (not End-A;
reason=sound_design_plan)": the soft freeze stamped by the sanitizer a
minute earlier refused the rewrite. The stale plan stayed, the pre-flush
barrier refused "cue anchor segment_id=seg_007 not in selection" on every
pass, and the identical-failure counter climbed toward a halt. Entry 96
covered the first production of the plan under the freeze; this is the
next case, a re-derivation after the selection changed.

Fix: under the soft freeze (never the hard one, which means WAVs are
rendered against these seats) the plan's own stage may rewrite the plan
when the committed plan anchors a cue on a segment outside the live
selection. Other writers and coherent plans are unchanged.

Tests: `tests/test_sdp_rederive_under_soft_freeze.py`.

---

## [105] TOOLING: a resume harness, because every recent defect lived on a resume path

**Status:** built; first real cases pending (they need the GPU free).

Entries 94, 96, 98, 102 and 104 all appeared only when a run was re-entered:
a stage after a refused commit, a resume after ranking changed the
selection, an engine restarted after a code fix. Straight runs never walk
those paths, so neither the unit suite nor the two clean end-to-end proofs
could see them. `tools/resume_harness.py` walks them on purpose:

- **rewind**: clone a completed run, `clear_from` a stage (the call delivery
  itself uses to invalidate analysis), drive the engine to completion again;
- **crash**: clone, rewind, start the engine as a subprocess, kill the whole
  process tree the moment the named stage reports running, restart, repeat
  `--kills` times.

The harness is the operator at both gates, through the same functions the
GUI endpoints call. A case passes only when the run completes with publish
outputs, adds zero error-level lines to the run log, and leaves no staged
files for finished stages. Presets: `boundaries` (nine rewind points on
authority and freeze boundaries), `crash` (four long stages with child
processes), `quick` (one of each). The maintainer's workflow is unchanged;
this runs here, against clones.

Tests: `tests/test_resume_harness.py` (completion, error and stale-staging
judgements; gate sign-off once per gate; rewind drops the old sign-off).

---

## [106] PRODUCT: two deterministic guardrails in the walk and the engine loop

**Stage / area:** `homunculus.agenda.walk_seed_agenda`, `orchestrator.run`
**Status:** fixed.

Two behaviours every recent run showed, neither a stop on its own, both
paid for in failed passes and error lines:

- **Prerequisites learned from the exception.** `dispatch_stage` refuses a
  stage whose earlier seed-order stage is incomplete; the walk learned the
  prerequisite from the error and ran it, one per pass. Every run paid two
  or three failed passes climbing "complete chapter_close_hitch before
  refinement_agenda", then full_master_ranking, then the next. The walk now
  asks the same check before dispatch and runs the chain up front,
  bounded by the order length and by the one-run-per-prerequisite set the
  reactive path already keeps. A prerequisite that fails to run falls
  through to dispatch, which reports it as before.
- **Hops that reproduce the last error.** When delivery reports analysis
  incomplete the engine hops back to analysis and tries again, up to
  sixteen times. exec_065 spent nine hops and nine error lines on one
  refused input check (entry 103) with nothing landing in between. The
  engine now stops a hop that reproduces the previous hop's error with the
  same stage count, and says so.

Tests: `tests/test_seed_prereqs_ahead.py`, `tests/test_orchestrator.py`
(the hop-loop case).

---

## [107] TOOLING: the engine writes a run verdict

**Stage / area:** `orchestrator.run_verdict`
**Status:** done.

The maintainer's acceptance test was "is there a master.wav". master.wav
appears at stage 68 of 72, and a run can carry it while an error line, a
refused commit's leftovers, or a missing package sit behind it. The engine
now ends every run, complete or not, by writing
`operator/run_verdict.json` and logging one line:
`=== verdict: PASS|FAIL complete=... stages=... error_lines=... stale_staging=... outputs=... ===`.
`pass` is true only when the pipeline is complete, the publish outputs are
on disk (or the sign-off was Skip and the package stub exists), the run log
holds no error-level line, and no finished stage left staged files behind.
The first five error lines are quoted in the file. The resume harness
(entry 105) judges its cases by the same rules.

Tests: `tests/test_orchestrator.py` (a complete clean run passes; a complete
run with one error line fails).

---

## [108] GUI: a browser notice was recorded at error level in the run log

**Stage / area:** `POST /api/runs/{id}/log`
**Status:** fixed.

exec_065 gained an error line that no stage produced: "Refresh run: Failed
to fetch", origin gui. The open browser tab's refresh timed out while the
server held the run lock, the frontend posted the notice with the level it
uses for its own failures, and the server wrote it into the run log as is.
The maintainer saw the same on his run and rightly called it noise, but
noise at error level fails the verdict (entry 107) and reads as a pipeline
error in the log.

Fix: the run log's error level belongs to the pipeline. A browser notice
posted at error level is recorded as a warning with `gui_level: error`
kept in its detail.

Tests: `tests/test_gui_notice_level.py`.

---

## [109] PRODUCT: one malformed model reply was logged as a run error although the next attempt repaired it

**Stage / area:** `stages.llm_runner` (parse of a model reply)
**Status:** fixed.

exec_066, `mastering_shape_candidates`: the model's first reply was not
valid JSON ("Expecting ',' delimiter"), the runner's repair pass had a good
reply six seconds later, and the stage finished. The parse failure was
logged at error level, so the only error line in an otherwise clean run
was an attempt the ladder had already handled.

Fix: a single attempt's parse failure is a warning (still carrying the raw
reply prefix, model id and attempt). A stage whose every attempt fails
reports its own error as before.

---

## [110] PRODUCT: a slow Chatterbox import was reported as a missing runtime, hard-stopping gap framing, and the answer was cached for the engine's life

**Stage / area:** `synthesis_fallback.chatterbox_runtime_available`
**Status:** fixed.

exec_066 (fresh partial run, engine spawned by a freshly restarted server
while the resume harness was copying a run on the same disk): gap framing
hard-stopped with "Chatterbox runtime unavailable (venv or import check
failed)". The venv was fine; the probe runs `python -c "import chatterbox"`
with a 30 s timeout, importing chatterbox loads torch, the cold import on a
busy disk took longer, and `TimeoutExpired` was folded into "unavailable".
The result was then cached (`lru_cache`) for the whole engine process, so
every later ask in that run got the same wrong answer. A first run on a
cold server would see exactly this.

Fix: the probe timeout is 180 s, a timeout counts as present (the
synthesis call has its own budget and reports its own failure), and only
a positive answer is cached; a miss is re-probed on the next ask.

Tests: `tests/test_chatterbox_probe_timeout.py`.

---

## [111] PRODUCT: a re-entered run reported itself complete on its old package, skipping the sign-off and the stages that remained

**Stage / area:** `execution_status.pipeline_complete` (the Partial DONE bar), orchestrator end-of-run
**Status:** fixed.

Resume harness, boundaries preset, every rewind case (exec_069 to exec_073):
a completed run cloned and rewound to an earlier stage ran the engine for
about 90 s, which then printed "Run complete" with 67 to 73 of 74 stage
markers, no G-Publish sign-off, and the conductor's own note "delivery
incomplete after conductor (5 remaining)". The crash and the
master_finalize rewind of the quick preset passed because they rebuild the
master.

The bar was file-based only: committed master, cover, mp3, and a
`package_ready.json` with `ready:true`. All four survive a re-entry, so
the engine saw "complete", skipped the hold for the operator, and the
delivery conductor (which consults the same bar to decide whether to
wait) returned OK with work remaining. An operator who re-enters a
finished run from the GUI, or the engine's own hop back to analysis on a
run that has a package, would get the stale package as the run's result.

Fix: the bar also requires the `podcast_publish` stage marker and a
package no older than the master on disk (`package_bound_to_current_master`).
`ship_bar_incomplete_reasons` names the two new holes
(`podcast_publish_not_done`, `package_older_than_master`), the run verdict
carries `package_current`, and the harness judges completion the same way.
A re-entered run now holds at the final sign-off and packages again.

Tests: `tests/test_package_bound_to_master.py`; ship-bar and footgun
fixtures now model a complete run with its marker.

---

## [112] PRODUCT: after a re-entry the ship stages never ran again (outputs on disk keyed them as done) and a current package stayed unmarked

**Stage / area:** `homunculus.agenda.ship_after_master_remaining`, the delivery runner's committed-master walk
**Status:** fixed.

With entry 111 in place, the rewound clones held at the sign-off as they
should, and then stopped incomplete: rewind vo_synthesize (exec_073) ended
with 68 of 74 markers and "6 remaining", rewind edl (exec_074) with 73 and
the package stage never run. Two causes, one mechanism. The ship stages
(transcript, meta, cover prompt, mp3, cover, package) are keyed on their
outputs being present, not on their markers. After a re-entry the outputs
are still on disk, so the walk had nothing to do; the markers the re-entry
cleared were never written again, and the run could not complete. When the
master had been rebuilt, the same keying shipped the old mp3 and cover.

Fix, both deterministic:
- A ship output older than `master/master.wav` counts as missing
  (`ship_stage_output_stale`); the walk makes it again from the new master.
- Ship stages whose outputs are present and current but whose marker is gone
  are re-marked before the walk (`backfill_ship_holes_after_master`), the
  same way pre-master holes behind an existing master already were. Outputs
  first, never a hollow stamp. The package stage is included: a package
  current for this master is this master's package.

Tests: `tests/test_ship_holes_after_master.py`.

---

## [113] PRODUCT: the overlap family. A permitted removal left every selection-derived document behind, and the repair's own writes were refused

**Stage / area:** `edl_overlap_repair`, `air_order_boundary.commit_selection_mutation`, `nugget_layup`, `recovery_controller`, `seat_authority`
**Status:** fixed (structural).

Maintainer's exec_009 (59.5-minute interview, partially-accelerated): at
stage 58, `edl` built the timeline, then the overlap repair was refused
writing `segments/nle_edits.json`; on the retry the absorbed segment
seg_019 was retired from the selection (revision 2 to 3), the lay-up plan
kept the id and revision 2, `edl/load_inputs` failed closed on the stale
plan, the heal `recovery_adopt_layup` was refused as not the plan's owner,
and after two identical failures the conductor exited with edl remaining.
Same shape as entries 101 (gap report omit stamps) and 104 (sound design
plan cues anchored on a dropped segment, which exec_065 hit as "cue anchor
seg_007 not in selection"): the selection moves under a permitted action
and what is derived from it does not.

Three defects, each deterministic:

1. **Overlap repair refused at its own writes, in every epoch.** A remap
   stage persisting a remap path must declare `mutation_class="segment_id_remap"`
   (that is the ownership rule for referential-integrity writes). The
   repair's NLE and transitions writes did not, so ownership answered
   `mutation_class_required:segment_id_remap` on every overlap union; the
   recovery test had been passing on the half-done repair (its run log
   carried `authority_denied:persist:segments/nle_edits.json:edl_overlap_repair`).
   Both writes now declare the class.
2. **Nothing reconciled the selection's dependents after a commit.** New
   `selection_dependents.reconcile_selection_dependents`, called from the
   selection's single write point right after the write. It fits the lay-up
   plan to the committed order and lock (`adopt_layup_plan_to_selection`
   under the plan owner's key, without republishing the gap body) and
   re-anchors sound design cues whose anchor left the selection onto the
   nearest live neighbour (or skips them when none exists), persisting
   under the plan's owner with the new End-A core reason
   `selection_dependents_reconcile`. It never widens the air order. A plan
   the unfrozen cascade marked for recompose is left to the recompose.
3. **The heal wrote under its own key.** `recovery_adopt_layup` and
   `order_reconcile` now adopt under `nugget_layup_compose`, the owner, so
   the recovery path works even if a commit ever reaches disk without the
   reconcile (it answered `not_allow:owner=nugget_layup_compose` before).

Why it did not show on the 6-minute clip: an overlap union under hard
freeze needs two neighbouring segments whose source ranges intersect after
the EDL is built, which the short tape's selection does not produce; the
cue variant (104) did, and was patched as a carve-out. This entry replaces
the carve-outs with the rule.

Tests: `tests/test_selection_dependents.py` (eleven cases: End-A row, retired
id and bumped revision under hard freeze, lock-only drift, fresh plan
untouched, cascade-stale plan left alone, recovery playbook, cue re-anchor
rules, no-neighbour skip, owner key and reason on the SDP write, commit
ordering, NLE write lands). The recovery test now also asserts no
`authority_denied` line in its run log.

---

## [114] PRODUCT: the sound design plan over its asset cap was refused at the barrier while the lint called it a warning; two handled failures stood as run errors

**Stage / area:** `artifact_sanitize.sound_design_plan`, `deterministic_lint`, `llm_simple`, `orchestrator.run_verdict`
**Status:** fixed.

exec_084 (fresh partial run through the GUI endpoints on the final code):
the model returned a sound design plan with 8 assets against a cap of 7
(the density budget of the delivery brief). The lint logged "non-blocking",
the pre-flush barrier refused the flush on the same message, vo_synthesize
found its prerequisite had not landed, the walk re-ran transitions and the
plan, and the second plan landed. The run went on, but carried two error
lines: the model status "partial" on the first plan, logged at error level
right before the fail-open commit that completed the stage, and the
"Prerequisite stage vo_line_adjudicate is not complete" raised by a stage
the resume loop then ran. With the verdict of entry 107, both counted as
failures.

Fixes:
- One cap, `sound_design_caps.sound_design_asset_cap`, read by the lint and
  applied by the sanitizer: a plan over the cap is trimmed to it, assets
  referenced by cues first, orphaned cues dropped. Non-amplifying; the plan
  on disk never exceeds the cap, so the barrier never refuses on count.
- The fail-open commit path logs its note as a warning; the error line
  stays for the case where fail-open did not commit and the stage fails.
- The verdict splits error rows into standing and recovered: a row whose
  stage reports "Stage finished" later in the log was handled by the resume
  loop. `pass` counts standing errors only; recovered ones are listed under
  `recovered_errors` so a noisy run is still visible.

Tests: `tests/test_sdp_asset_cap_clamp.py`, `tests/test_run_verdict_recovered_errors.py`.

---

## [115] PRODUCT: a blank fragment reached the air order and was judged blank only at the EDL gate under hard freeze; the narrative constraints and the episode structure did not follow the order

**Stage / area:** `air_order_boundary.commit_selection_mutation`, `selection_dependents`, `episode_structure`
**Status:** fixed.

exec_084 after the sound design plan: seg_007, a 3.2-second, 7-word fragment
("Okay? Where the sensitivity, the specific"), had been ranked onto air. The
EDL rendered it as a speech clip and passed QC; the EDL gate then ran its
narrative pre-repair, whose blank-or-unusable predicate dropped seg_007 from
the selection under the hard freeze, and the gate failed its own parity
check ("speech clips do not match final selection"). The resume loop
re-entered, the order change invalidated the lay-up plan and the sound
design plan, and the run died on the plan's asset cap (entry 114).

Upstream of that, the narrative plan carried a constraint over the same
fragment ("seg_007 must appear before seg_004") that the committed order
could not satisfy; the sound design plan's model refused with "rerun
narrative_arc_plan", and the episode structure, also derived from the
order, was stale, with the EDL gate's rewrite of it refused for ownership.

Three rules:
- A blank or unusable segment is excluded once, before the freeze, at
  `selection_order_sanitize` (the existing predicate and helper, now
  `drop_blank_segments`, applied to the sanitized order before its commit).
  Hard-keeps are exempt as before; an order that would empty is left alone.
  The EDL stage's "no selection blank handling" intent (its S1 to S5 peel)
  holds again: the gate's pre-repair finds nothing to drop.
- Narrative ordering constraints that contradict the committed order are
  dropped or flipped by the reconcile (`rewrite_constraints_to_selection`,
  under the ranking's freeze-safe metadata-align class, as `order_reconcile`
  already did when invoked as a heal).
- The episode structure is rebuilt on the committed order under its owner's
  key when the order changes (`persist_structure` takes `stage_key`). The
  build is deterministic.

Tests: `tests/test_blank_segments_and_order_dependents.py`.

---

## [116] PRODUCT: the sound design plan's own producer could not land its plan under the soft freeze, so the unclamped palettes plan stayed on disk and failed the barrier

**Stage / area:** `seat_authority.frozen_seat_write_allowed`
**Status:** fixed.

exec_094 (fresh run on the code with entry 114): the stage composed a plan,
the sanitizer clamped it to the cap, and the write was skipped: "seat_freeze:
skip write understanding/sound_design_plan.json (not End-A;
reason=sound_design_plan)". `sound_design_palettes` writes the file before
the soft freeze, so the stage's production is never a first production
(entry 96), and the stale-versus-selection carve-out (entry 104) did not
apply. What stayed on disk was the palettes plan with 8 assets, allowed at
palettes time because the delivery brief that narrows the cap to 7 is
written later; the stage marked itself done, and the pre-flush barrier
refused the next flush on "asset count 8 exceeds cap 7". exec_084 died the
same way after its second attempt.

Fix: under the soft freeze the plan's own producer (reason
`sound_design_plan`) may write the plan, whatever is on disk; the hard
freeze still refuses, since WAVs are rendered against those seats. Entry
104's carve-out is a special case of this rule and is folded into it.

Tests: `tests/test_sdp_producer_write_under_soft_freeze.py`.

---

## [117] PRODUCT: the narrative audit demanded an opening orientation the lay-up authority had durably omitted, and the run halted on the third identical failure

**Stage / area:** `artifact_repairs.repair_edl_audit` (opening_orientation_invalid)
**Status:** fixed.

exec_094 (fresh run on the code with entries 114 to 116): the lay-up plan
decided the native opening orients itself (omit ledger
`episode_open_native_self_orients`), the gap report recorded the opening
orientation as omitted and not required, and the EDL narrative audit's
model returned verdict "fail" with `opening_orientation_invalid` on every
pass. The repair demotes that issue only when an orientation line with
usable copy exists; with none, the fail stood, the unattended decision was
"retry_stage", the hollow guard unmarked the stage, and the run halted on
the pre-flush barrier after the third identical failure. The audio opens
mid-sentence ("Second thing is"), so the model's opinion is reasonable;
the point is that a recorded, durable decision must win over a repeated
opinion, or the run never ends.

Fix: a durably omitted orientation (`orientation_omitted`: omitted and not
required, written only by the native-open / lay-up authority) demotes
`opening_orientation_invalid` to an advisory, the same way a typed lay-up
skip is a decision rather than a missing line (entry 69).

Tests: `tests/test_audit_orientation_omitted_decision.py`.

---

## [118] PRODUCT: bookkeeping written after a stage's seal landed in a staging directory nobody flushes again

**Stage / area:** `write_staging.staged_path`, `run_context.mark_done`
**Status:** fixed.

Every fresh run left 40 to 74 files under `.pending_writes/<stage>/` for
stages that were done: homunculus memory, ledger and admitted rows,
`understanding/analysis_state.json`, the llm_calls index, written 0.2 to 5
seconds after the stage marker (exec_084, exec_094). The seal flushes and
removes the staging root, the walk's post-stage bookkeeping then writes
under the stage's still-active staging context, and the new files sit in a
root that is never flushed. The run verdict (entry 107) counted them as
stale staging and failed otherwise clean runs; the next re-entry's stale
staging discard (entry 102) had to clean them.

Fix: `mark_done` notes the seal, and `resolve_write_path` (the write
target) routes a sealed stage's later writes to the committed tree, where
the flush would have put them. Re-entering the stage reopens its staging,
and a cleared marker voids the seal.

The first version of this fix put the redirect in `staged_path`, which is
also what readers and the pre-flush barrier use to find a staged copy:
after a stage's first seal, every re-entry of that stage had its barrier
look for the staged file at the committed path and refuse with "cannot
read staged file" (exec_096 looped on sfx_prompt_craft and stopped). Only
the write target moves now; `staged_path` is the staging location again.

Two more pieces, from exec_099 (the first run to complete on this chain):
the EDL stage seals through the forced heal path, which leaves its overlay
for the orphan promote on a later entry that a completing run never makes.
At run end the engine now promotes staged files of done stages that the
committed tree never got or has older (`promote_lost_staged_writes`); the
clone adjacency report was the one such file. The verdict counts as stale
only a staged copy the stage owns that is newer than the committed file or
has none; older or byte-identical copies, and courtesy copies the stage
may not promote, are listed under `staging_leftovers` and do not fail the
run.

Tests: `tests/test_post_seal_writes_land_committed.py`.

---

## [119] PRODUCT: the asset clamp dropped the outro theme; a failed prerequisite still dispatched its consumer

**Stage / area:** `sound_design_caps.clamp_assets_to_cap`, `homunculus.agenda._run_seed_prerequisites_first`
**Status:** fixed.

exec_095 (fresh run on the code through entry 117): the plan's producer
landed its plan under the soft freeze (entry 116) and the clamp (entry 114)
trimmed 8 assets to 7, dropping the one no cue referenced: the outro theme.
The pre-flush barrier then refused "creative delivery missing music
role:theme_outro". The walk logged that the prerequisite had not landed and
dispatched vo_synthesize anyway, which raised "Prerequisite stage
vo_line_adjudicate is not complete" at error level, the same row every
fresh run since exec_084 has carried, before the resume loop filled it.

Fixes:
- The clamp ranks the first asset of each protected role (the theme roles
  the creative delivery check requires, and the roles that stand in for
  them) above cue-referenced assets, then the rest.
- A prerequisite that fails to land ahead of a stage is raised by the walk
  as "Prerequisite stage X is not complete", in the words the walk's own
  parser reads, instead of dispatching the consumer into the same wall.

Tests: `tests/test_sdp_asset_cap_clamp.py` (protected roles),
`tests/test_seed_prereqs_ahead.py` (raise before dispatch).

---

## [120] PRODUCT: the narrative audit was shown no heard-WAV evidence for spoken transitions and failed every pass on WAVs that existed

**Stage / area:** `stages.edl_narrative_audit.compact_vo_coverage`, `artifact_repairs._edl_issue_premature_vo_nle_placement`
**Status:** fixed.

exec_095 (fresh run on the code through entry 117): no gap lines were
planned, so the audit payload's `vo_coverage` was empty, while
`seam_occupancy` showed two spoken transitions (seg_007 to seg_005, seg_004
to seg_008). The model answered `pre_edl_vo_placement_missing`: "cannot
verify either occupied spoken transition because vo_coverage is empty".
Both transition WAVs had been rendered minutes earlier
(`master/transitions/tr_*.wav`). The repair's demote for that code looks
only at gap lines, found none, and left the fail; three identical passes
and the run halted at the pre-flush barrier.

Fixes:
- `vo_coverage` carries one row per planned transition pair with its
  heard-WAV status (rendered, missing, omitted, not_spoken), so the model
  sees the evidence.
- The demote also accepts the issue when every transition pair it cites
  has a playable WAV on disk.

A related warning stays: vo_synthesize's scratch copy under
`master/transitions/synthesized/` is refused at promotion as an unknown
path. The canonical WAV lands beside it, so nothing is lost; noise only.

Tests: `tests/test_audit_transition_vo_coverage.py`.

---

## [121] PRODUCT: the walk skipped vo_line_adjudicate as a prerequisite when G1 was not pending, but the VO stage's gate still demanded it

**Stage / area:** `homunculus.runtime._seed_prereq_block`
**Status:** fixed.

Every fresh run since exec_084 carried "Prerequisite stage vo_line_adjudicate
is not complete" (exec_096 again, with every other prerequisite landed
ahead). The walk's prerequisite check and the stage's own gate both start
from the earliest incomplete seed stage, but the walk added an exception
for the adjudication stage: skip it when the G1 gate is not pending. The
stage gate (`maybe_require_upstream_llm_progress`) consults only the
ordering authority (entry 62) and has no such exception, so the walk
dispatched vo_synthesize straight into the gate's refusal, and the resume
loop ran the adjudication afterwards.

Fix: the walk clears the block only when adjudication is sealed and
seed-complete (the seal helper still marks a stale stamp done when G1 is
green and the WAVs are fresh); otherwise it runs the stage ahead, as it does
for every other prerequisite. Both checks now read the same rule.

---

## [122] PRODUCT: a duplicated asset row in the sound design plan made the prompt craft fail its own barrier on every pass

**Stage / area:** `artifact_sanitize.sound_design_plan`, `sdp_cross_validate`
**Status:** fixed.

exec_096 and exec_098 (fresh runs on the final code): the plan carried the
outro asset twice (`show_theme_v1_full_bed_close`, 8 rows, 7 distinct ids).
`sfx_prompt_craft` writes one prompt per distinct id (7); the pre-flush
check compared 7 prompts with 8 asset rows and refused "fewer prompts than
SDP assets", the stage's attempt to sync the plan was refused under the
hard freeze, the marker was cleared as "pending_only", and the conductor
exited after the attempt memo with sfx_prompt_craft remaining. Nothing in
the run could change the plan's row count at that point.

Fixes:
- The plan sanitizer drops duplicate asset ids (first row wins) before the
  cap clamp, so the plan on disk never carries the duplicate.
- The cross-validate compares prompts with distinct asset ids, so a
  duplicate that reached disk through another path cannot trip it.

Tests: `tests/test_sdp_duplicate_assets.py`.

---

## [123] PRODUCT: the high-gap seed minted stock bridge copy the spoken-copy guard bans, and a seated line with unspeakable copy could neither be rendered nor released

**Stage / area:** `gap_vo_prior_context.courtesy_seed_text`, `vo_bind_authority` (seated bind heal)
**Status:** fixed.

Maintainer's exec_010: delivery failed twice on "vo_synthesize: seated
synthesize VO not rendered: vo_seed_seg_019" and stopped. The line had been
minted by the high-gap VO-seed playbook as a required synthesize bridge with
the stock copy "What tension carries into what comes next?". The guard's
generic-filler ban refuses exactly that phrase, with no grounded fallback,
so synthesis never attempted it; the adjudicated rewrite is advisory by
design (S1) and the seat freeze kept the stock text authoritative; the bind
heal refused the line (S2: no omit as success); the seat-rewrite meta-gate
refused as low gain; and the contract asserted a WAV for a seat nothing
could speak.

Two deterministic defects:
- The stock pool of eight bridge phrases carried three the guard bans
  (checked here: three of eight), although its comment claimed every phrase
  passed, and the minter returned the blocked phrase whenever the guard
  emptied it. Three in eight story-bridge seeds were unspeakable at birth.
  The pool now holds only phrases the guard speaks, a blocked candidate
  falls back to the first pool phrase the guard accepts, and a test keeps
  the pool and the guard in agreement.
- The seated bind heal now distinguishes "the guard refuses this copy with
  no fallback" from a transient synthesis failure: for the former it
  releases the seat under the existing synth-fail End-A action, recording
  the reason, so the content hole stays visible instead of fatal. Transient
  failures are still refused, as S2 intends.

Why it did not show here: which seed phrase a line gets is a hash of its
target segment, and the 6-minute clip's seeds landed on speakable entries.

Tests: `tests/test_seed_pool_speakable.py`.

---

## [124] PRODUCT: two self-correction backstops: refusal feedback to the model, and a declared fallback at the identical-failure cap

**Stage / area:** `fallback_backstop` (new), `write_staging.approve_stage_writes`, `llm_simple.run_llm_stage_simple`, `homunculus.agenda.note_identical_stage_error`
**Status:** added.

Asked for by the maintainer after exec_010: rather than halting on a hard
blocker, adjust and retry, with deterministic rules where an old version
exists and model re-runs that are told what went wrong.

- **Refusal feedback.** When the pre-flush commit barrier refuses a stage's
  artifact, its reasons are written to `operator/refusal_feedback/<stage>.json`.
  The next run of that LLM stage reads them once and appends them to the
  user turn ("PREVIOUS ATTEMPT REFUSED: ... fix exactly these"). Each
  distinct reason set is fed back once, so a model that cannot comply does
  not loop; the deterministic sanitizers and the cap still stand behind it.
  Until now a refused LLM stage was re-run blind: exec_084's plan came back
  with 8 assets twice.
- **Declared fallback at the cap.** When a stage fails identically for the
  third time and its primary artifact already exists committed and
  acceptable (the old version), the engine keeps it, marks the stage done
  through the heal ladder, appends the decision to
  `operator/fallback_decisions.jsonl`, and continues. Without an old
  version there is nothing honest to fall back to, and the halt stands as
  before.

Tests: `tests/test_fallback_backstop.py`.

---

## [125] PRODUCT: transient OpenAI errors ended a stage attempt at once; optional stages had no rung below "keep the old version"

**Stage / area:** `stages.llm_runner` (OpenAI call site), `fallback_backstop`
**Status:** added.

- **Transient retry.** A rate limit, connection error, timeout, or 5xx from
  OpenAI was raised straight to the attempt loop, so a stage with two
  attempts could die on two blips. The call is now retried up to four times
  with 2, 4, 8 second backoff before the attempt is judged, each retry logged
  as a warning. Context-length and content errors are not transient and are
  handled as before.
- **Skip ladder.** Entry 124's fallback keeps a stage's old version at the
  identical-failure cap. For a stage with no old version, the second rung
  is now a skip stub, for the stages the pipeline already runs without
  (delivery brief, episode structure, the pass-2 framing stages), each
  through the stub writer the stage's own disabled path uses, with the
  decision recorded. Stages the master cannot do without are not on the
  ladder; for those the halt still stands, and the deterministic repairs
  of entries 101 to 123 are the real guard.

Local models: the on-device LLM stack in this codebase frames volleys
before OpenAI (prep only, by design) and is not available on this machine,
so there is no local path that produces a stage's artifact; the local
stacks that do produce (STT, diarization, Chatterbox, MusicGen) already
have their own retries and probes (entry 110).

Tests: `tests/test_fallback_backstop.py` (transient retry, skip ladder).

---

## [126] PRODUCT: the engine takes one second wind before it stops

**Stage / area:** `orchestrator.Orchestrator._second_wind`
**Status:** added.

When the resume loop gives up (hop loop with no progress, identical-failure
halt, exhausted remedies), the engine now resets the persisted failure
counters once (`clear_all_halts`), clears the operator-need flag the walk
set, logs "second wind", and re-enters analysis and delivery one more time
with the fallback ladder of entries 124 and 125 active. Once per engine
run; a gate wait (sign-off, transcript review) is not a failure and is
never re-entered this way. A run that fails again after the second wind
stops as before, with the verdict naming the stage.

Tests: `tests/test_orchestrator.py` (second wind recovers, hop loop plus
second wind, gate wait untouched).

---

## [127] PRODUCT: a refused side-effect write halted the stage that happened to be active, and the dispatch door then refused the only repair (macOS exec_011)

**Stage / area:** `artifact_ownership` (denial accounting), `run_context.write_json`,
`write_staging.write_committed_json`, `dispatch_door`, `homunculus.agenda` (seed
prerequisites), `fallback_backstop`, `media_ip_cta`
**Status:** fixed at the choke points. The exact helper that made the write in
exec_011 is not identified (see "What is not proven").

**Reported (one-hour source, 53 segments, three compose shards):** the run died
with `seed order: complete gap_framing_compose before running delivery_brief_build`.
`gap_framing_compose` had done its work (27 interviewer lines, high-gap seeding),
then logged
`authority_denied:persist:mastering/mastering_plan.json:gap_framing_compose:pre_soft_freeze:air_contract_sanitize`,
its marker did not stay, every re-entry of compose was refused as `no_delta`,
and the walk cycled brief, compose, brief until the identical-failure cap.

**Chain, each link confirmed in the code:**

1. Shared helpers (seat sync, id remaps, orientation republish) run under
   whichever stage is active and write documents that stage does not own. The
   ownership table has no row for `gap_framing_compose` on the mastering plan,
   so the write is refused (`not_allow:owner=air_contract_sanitize`).
2. `_log_authority_denied` ran at raise time, before anyone knew whether the
   caller handled the denial, and most callers do. It logged at error level,
   recorded an identical failure against the active stage, and on the second
   identical fingerprint stamped `halt` with `authority_denied_no_heal`. Two
   handled denials were enough to halt compose with no heal. This is why the
   run completes in forensics mode: `is_halted` returns False there.
3. The dispatch door's `no_delta` guard refuses a stage whose hard inputs are
   byte-identical to its last success while its outputs are on disk. It does
   not ask whether the stage is still complete. Compose's inputs had not
   changed and `gap_report.json` was on disk, so the door refused it at the
   walk layer (advance) and again at the dispatch layer, where
   `dispatch_stage` returns silently on a refusal.
4. The walk's prerequisite retry therefore "ran" compose (a silent no-op) and
   retried the consumer, which raised the same seed-order error. Three of
   those reached the cap.
5. The fallback ladder of entries 124 and 125 was then applied to the blocked
   consumer (`delivery_brief_build`), not to the incomplete prerequisite.

**Why the six-minute clip never showed it:** about ten segments, so compose
takes the single-batch path, has no CTA recut and few high gaps, and its
finish path makes no foreign write. The maintainer's question was right: the
short source does not exercise this path.

**Fix (rules, not carve-outs):**

- **A denial is counted where it fails a stage, not where it is raised.**
  `_log_authority_denied` no longer records an identical failure or stamps a
  halt. A denial that escapes a stage is counted by the walk's failure path
  under the ordinary cap, with the ordinary fallbacks.
- **A refused side-effect write costs the write, not the stage.**
  `skip_foreign_side_effect`: when the writer is inferred from the active
  stage (no `stage_key`, no `role`) and the table's answer is the last
  fall-through (`not_allow:owner=...`, the stage has no row for the path at
  all), `write_json` and `write_committed_json` skip the write, log a warning
  naming the owner, and append to `operator/foreign_writes_skipped.jsonl`.
  A caller that names a key or a role still gets `AuthorityDenied`, so every
  existing fallback (retry as owner, mirrored write) is unchanged, and
  explicit DENY rows, freeze blocks and mutation-class rules still raise.
  Documents with their own commit path (EDL, selection, air order,
  transitions, manifest, boundaries, gap report, sound design plan) are
  exempt and still raise, because their writers catch the refusal and retry
  as the owner. An AST sweep of every try block with an unkeyed write and a
  writing handler found one site outside those documents that relied on the
  refusal (`vo_bind_authority`, the bind-failure omit on the mastering
  plan); it now names the seat owner up front, and a static test keeps that
  pattern out.
  A foreign denial that is raised logs at warning level, not error.
- **The door does not hold a prerequisite the seed order has just demanded.**
  `demand_seed_prereq` marks the stage while the walk runs it as a
  prerequisite; `evaluate_dispatch` then skips `no_delta` and the attempt
  memo for that stage only. The dispatch caps still apply, and the walk asks
  once per prerequisite. The mix and junction ping-pong the guard was built
  for is untouched (its tests pass unchanged).
- **A prerequisite left incomplete says why.** After a demanded run the walk
  offers the marker to the heal ladder (which marks only a complete body) and
  otherwise logs `stage_artifact_incompleteness` for the prerequisite, so the
  log carries the cause and not only the consumer's seed-order line.
- **The fallback at the cap goes to the incomplete prerequisite.** A
  `seed order: complete X before running Y` failure applies the ladder to X
  (keep its prior committed artifact through the heal ladder) and records
  `unblocks: Y`. Y is never skipped through its stub for X's failure.
- `media_ip_cta._publish_story_children_sources` asked for a writer by
  denial: first as `full_master_ranking`, then as the active stage, then a
  mirrored write. It now asks `write_permitted` which key may write and
  falls to the mirrored write without logging two denials.

**Guardrails around the same chain** (each one closes a way the walk could
still be left with an incomplete stage it will not run):

- **A refused stage that is not complete is run, not skipped.** At the walk
  layer a `no_delta` refusal means "the last success stands". The walk now
  checks that: a seed-complete stage is advanced past as
  before; a stage whose body is complete on disk gets its marker back
  through the heal ladder; a stage that is neither is run once in that walk
  under the demand. The audio stages the guard was built for (mix, junction,
  MMAudio, VO synthesis, music, master) are excluded and keep the refusal.
  The attempt memo ("already failed at this state") is left alone: voiding
  it would bring back the re-walk of failed stages it exists to stop.
- **An incomplete prerequisite gets its own recovery playbook.** A stage that
  raises is handed to `handle_stage_failure`; a stage that returned without
  becoming complete was not, and recovery ran for the consumer instead. The
  prerequisite's incompleteness reason now goes through the same controller
  once, and the marker is offered again afterwards.
- **The second wind resets the door's memory too.** `_second_wind` cleared
  the failure counters but left `operator/dispatch_memo.json`, so its one
  re-entry could be refused by the same rows. It now drops the memo rows of
  every stage that is not seed-complete, and when the stopping error names
  a prerequisite it applies the fallback ladder to that prerequisite first.
- **The verdict shows what the self-correction did.**
  `operator/run_verdict.json` gains `fallback_decisions` (last ten) and
  `foreign_writes_skipped` (stage:path, distinct). Neither fails the
  verdict; both are the first place to look when a run stopped or a passing
  run sounds wrong.

**Reproduced on the one-hour source (exec_102, 2026-10-02).** The keyless
replays of sharded compose on a clone of exec_055 never made the write, in
five states. The first fresh one-hour run on this code did, at the same point
as exec_011: after compose's second pass landed its lines, the completion
path promoted orphan markers and ran the delivery sanitizers inline
(`gap_report_sanitize`, the pass-2 skip copy of `gap_framing_recompose`,
which is the "skip-copy landed done in the noise" of the report), the
execution contract's seat sync stayed read-only three times, and a fourth
helper wrote the plan unkeyed. The log line is now a warning,
`side-effect write skipped: mastering/mastering_plan.json is not
gap_framing_compose's to write (owner air_contract_sanitize)`, the stage
completed, and the walk went on to the brief. The ledger row now also
records the calling frames inside the package, so the next occurrence names
the helper.

Tests: `tests/test_foreign_side_effect_writes.py` (24). Existing
`tests/test_p15_no_delta_guard.py` and `tests/test_fallback_backstop.py`
pass unchanged.

---

## [128] PRODUCT: one unspeakable required line failed the whole of sharded `gap_framing_compose` (exec_102, one-hour source)

**Stage / area:** `spoken_copy_guard.guard_spoken_copy`, `artifact_repairs.repair_gap_report`
**Status:** fixed.

**Symptom:** four compose shards returned (65 segments), then
`required gap VO blocked by spoken_copy_guard (vo_context_seg_062):
spoken_repeated_sentence, no_grounded_fallback`, `Failed: Stage
gap_framing_compose`. The engine re-entered analysis and paid for all four
shards again; the second roll happened to pass.

**Cause:** each shard is written without sight of the others, so two shards
can open a context line with the same sentence. The guard flags the second
as a repeated sentence (a hard structure violation: it sounds like a
synthesis fault). Its only remedy was a grounded hinge built from topic
evidence, there was none, so the verdict was `block`, and for a required
line that is not a lay-up the repair raised a loud failure. One line in 65
segments took the stage down. The six-minute clip has one shard, so it
cannot produce a cross-shard repeat.

**Fix:**

- **Cure before blocking.** `strip_repeated_sentences` drops the sentences
  another line already voiced, and repeats inside the line. When the rest is
  still a line (six words or more) and passes the guard, it is the fallback:
  the model's own grounded copy minus the repeat. A shorter remnant ("What
  broke next?") is a hinge, not a line, and is not kept.
- **Release instead of failing.** A required line the guard still cannot
  make speakable is released (`release_unspeakable_required_vo`, logged as a
  warning with the violations) in both branches that used to raise
  (`required gap VO blocked`, `required high-gap VO omitted after rewrite`).
  The high-gap seed that follows the repair covers the segment with a stock
  phrase the guard accepts (entry 123), and the completion check still
  refuses the stage if a high gap ends up uncovered. Same rule as entry 123:
  a line whose copy the guard refuses is released, not asserted.

Tests: `tests/test_unspeakable_required_line_released.py` (6).

---

## [129] PRODUCT: a split's id remap of `gap_evaluations.json` was refused from `full_master_ranking` (exec_102, one-hour source)

**Stage / area:** `artifact_repairs.propagate_nle_split_segment_refs`
**Status:** fixed.

**Symptom:** twice during ranking on the one-hour source,
`side-effect write skipped: understanding/gap_evaluations.json is not
full_master_ranking's to write (owner missing_framing)`. The ledger's caller
frames (entry 128) name the site: `propagate_nle_split_segment_refs`, once
from the NLE split and once from the CTA recut. Before entry 127 the same
write raised `AuthorityDenied`, which logged an error line and counted
toward a no-heal halt against ranking; after it, the write is skipped
quietly. Neither is right: the evaluations stay keyed by a parent segment
that no longer exists.

**Cause:** the function renames a split parent to its children in every
document that stores the id. Transitions and the gap report already present
their owner's key for this (entry 101). Three other documents were written
unkeyed, as whichever stage was active: the coverage audit, the narrative
plan, and the gap evaluations. The six-minute clip has no CTA recut and no
split during ranking, so it never took the path.

**Fix:** `persist_segment_id_remap(ctx, rel, doc)` asks the table which key
may write: the active stage, then the active stage declaring
`segment_id_remap`, then the document's owner with that class. An id remap
changes no judgement, so presenting the owner is the rule already in use
for transitions and the gap report. When no key is accepted it logs a
warning and returns False instead of raising. The three unkeyed writes now
go through it.

Tests: `tests/test_segment_id_remap_owner_key.py` (5).

---

## [130] PRODUCT: edl refused "NLE operator edits not committed on disk selection" with no operator and no exit (exec_102, one-hour source)

**Stage / area:** `nle_state.apply_nle_to_selection`, `stages.assembly.run_edl`
**Status:** fixed.

**Symptom:** delivery halted at `edl` with `SystemExit: edl: NLE operator
edits not committed on disk selection` (the message goes on to say the NLE
must land through the selection owner first), again on the resume, and
again on the second wind. No error-level line
is logged (it is a halt, not a failure), no stage can act on it, and the run
has no operator at the timeline.

**Cause:** `split_segment_at_cuts` seeds `sequence_order` from the whole
manifest when no operator order exists, so the timeline keeps children at
their parent's position. `nle_has_operator_edits` is true for any non-empty
order, and `apply_nle_to_selection` then has to tell that mirrored spine
from a real reorder. Its test was length: ignore the order when it covers
every on-air id and is at least 1.25 times as long. On the one-hour source
68 manifest ids against 60 on air is 1.13, so the spine was taken for an
operator reorder. At ranking it overrode the ranked order with source
order; later selection writers restored theirs; edl compared the two and
refused. The six-minute clip airs a small share of its manifest, so it
always passed the ratio.

**Fix:**

- **A spine is recognised by what it is.** `is_source_spine`: the order
  covers every on-air id and runs in source order (by `start_ms`, children
  included). That is no reorder and is ignored for ordering; exclude and
  split overlays still apply. The length rule stays for the cases it
  catches. A subset in source order, or an order with an unknown start
  time, is not called a spine, and a real full-cover reorder is still
  honoured.
- **An engine-driven run has an exit.** When the NLE order and the disk
  selection hold the same segments in a different order and the run is
  driven by the engine (full-auto or partially-accelerated), edl builds from
  the committed selection, which is the air-order authority the stage's own
  comment names, and logs a warning with the count of differing positions.
  Different segment sets still refuse, and a manual run keeps the refusal:
  there the operator can land the timeline through the selection owner.

Tests: `tests/test_nle_source_spine_is_not_a_reorder.py` (8).

---

## [131] PRODUCT: the walk dispatched `mix` into its own refusal instead of running `junction_snip_qa` first (exec_102, one-hour source)

**Stage / area:** `homunculus.agenda._run_seed_prerequisites_first`
**Status:** fixed.

**Symptom:** `incomplete_cut_unresolved: Mix refused: live incomplete-cut
residuals on_a_roll` (seg_056, seg_057), `Failed: Stage mix`, the recovery
playbook pinning `junction_snip_qa`, and the delivery pass ending FAIL before
the resume did what the refusal said. Two error lines on a path that is the
designed order.

**Cause:** the order is already decided in one place: `junction_precedes_mix`
says junction runs ahead of the first mix when the live EDL carries critical
incomplete-cut residuals, and `ordering_authority` exempts junction from the
seed order for exactly that. Nothing asked before dispatching mix. The walk
found out from mix's loud failure, the same shape as entries 106, 119 and
121. Residuals of this kind need a long timeline with mid-thought joins; the
six-minute clip produced none.

**Fix:** `_junction_owes_recut_before_mix` reads the two rules the stages
themselves use, mix's refusal (`live_incomplete_cut_critical_findings`) and
the ordering exemption for junction ahead of mix. When both hold, the walk
runs `junction_snip_qa` before `mix`, once per walk, under the seed order's
demand (entry 127). A junction failure is raised as the prerequisite failure
in the words the walk's parser understands. Without the exemption junction
is not sent ahead, since its dispatch would only refuse on the seed order.

Tests: `tests/test_junction_runs_ahead_of_mix.py` (7).

---

## [132] PRODUCT: the transferred keep list demanded a child the family cap had taken off air (exec_102, one-hour source)

**Stage / area:** `hard_keep.hard_keep_segment_ids`
**Status:** fixed.

**Symptom:** with junction running ahead of mix (entry 131), its ladder
omitted one hanging clip and remastered, then `Junction remediation run 1
could not remaster: sanitize_refused:selection:
hard_keep_missing_from_order:seg_002i`, `Failed: Stage junction_snip_qa`.

**Cause:** seg_002 is a hard keep and a CTA parent, split into twelve
children, ten of them admitted story. Two places cut that family to the
same budget (`max_same_family_on_air`, 8) from different inputs:

- the selection sanitizer's family cap works on what is on air and prefers
  hard keeps: it aired a to h and excluded i and j
  (`cap_same_family_on_air`);
- the keep transfer in `hard_keep_segment_ids` works on the whole admitted
  story set, collapses overlapping spans, then takes the first eight.

They agreed until an overlap union folded seg_002h into seg_002g. The
transfer then collapsed g and h into one, its eighth slot moved to
seg_002i, and the keep list demanded a segment the cap had excluded. The
lattice lint (`hard_keep_missing_from_order`) is critical, so every later
selection commit was refused, here junction's. The six-minute clip has no
twelve-way split.

**Fix:** a child the committed selection excludes for a lattice reason
(`cap_same_family_on_air`, `sanitize_duplicate_source_span`) is left out of
the transfer (`lattice_dropped_ids`). The lattice ruled on the family with
the keeps in hand; the transfer must not hand that ruling back as a demand.
A child excluded for any other reason is still a transferred keep, so a
wrongful drop is still refused, and with no selection on disk every story
child is still offered to ranking.

Tests: `tests/test_hard_keep_transfer_respects_family_cap.py` (5).

---

## [133] ENV + PRODUCT: DeepFilterNet ran without its native build; preclean logged an error-level runtime failure on every Mac without Rust (macOS exec_001)

**Stage / area:** `audio_preclean`, `deepfilter_runner`, `scripts/lib/bootstrap_local_runtimes.sh`
**Status:** FIXED

**Seen:** first seconds of a fresh partially accelerated run on a new Mac:
`[ERROR] [audio_preclean] Local runtime failed (exit 1): deepfilter/deepfilter_enhance_batch.py`
with `DeepFilterNet import failed: No module named 'df'`. The stage then fell
back to ffmpeg denoise, so the run continued, but with an error on the log and
a different preclean than a machine with Rust.

**Cause:** the bootstrap skips the Rust (`maturin`) build of DeepFilterNet's
native `df` module when `rustc` is missing, and only prints a WARN. The runner
treated "repo directory exists" as "stack is runnable", spawned the batch
script, and the script died on import. Two machines with the same repo got
different audio: one DeepFilterNet, one ffmpeg.

**Fix:**
- `deepfilter_runner._require_deepfilter_stack` probes `import df.enhance` in
  the DeepFilter venv once per process before any spawn. An unbuilt stack is a
  quiet `DeepFilterUnavailable`, which the existing ffmpeg fallback handles at
  warning level. No subprocess, no error-level log.
- The bootstrap installs Rust with Homebrew on macOS when it is missing, so
  the build is no longer silently skipped.
- `tools/env_sync.py` (new) checks every local runtime's key import, including
  `df.enhance`, and offers a bootstrap re-run when one fails.

Tests: `tests/test_deepfilter_runner.py::test_enhance_wav_unbuilt_stack_refuses_before_spawn`.

---

## [134] PRODUCT: a high gap ended every compose attempt neither covered nor demoted, so the barrier refused until the class cap halted the run (macOS exec_002, one-hour source)

**Stage / area:** `gap_framing_compose` (`_seed_uncovered_high_gaps_before_heal`),
`high_gap_vo.seed_uncovered_high_gaps_deterministic`, `high_gap_vo.resolve_seats`
**Status:** FIXED

**Seen:** fresh partially accelerated run on the one-hour source, 14 high
gaps. Compose failed with
`Pre-flush commit barrier failed: high gap segment seg_048 has no interviewer line`,
then on retry `seg_021`, then `class_failure gap_framing_compose/high_gap_unframed x4/3 halt=True`
and `dispatch refused gap_framing_compose: max_invokes_per_identity`. The run
stopped at `error`.

**Chain (reproduced offline on a copy of exec_002's state; unmodified code
fails, fixed code passes):**
1. Each pass the repair restamps air-contract omits, so several high-gap lines,
   including earlier seeds such as `vo_seed_seg_021`, sit in the report with
   `skipped_optional` and `air_script_omit`.
2. `targeted_segment_ids` correctly ignores inactive rows, so the seeder sees
   seg_021 as uncovered and **appends a second row with the same id**
   `vo_seed_seg_021`.
3. `resolve_seats` runs next, counts seg_021 as covered by the new seed, and
   does not demote it.
4. `dedupe_line_ids` runs after that and keeps the **first** row with the id,
   the inactive one. The live seed is dropped.
5. seg_021 is now neither covered nor demoted. The barrier refuses. Every
   attempt repeats the same sequence until the class cap halts the run.

The six-minute clip has one or two high gaps and never builds up omitted seeds
across passes, so it cannot show this.

**Fix:**
- **One row per seed id.** The seeder removes an inactive row under the same
  `vo_seed_<seg>` id before appending, so dedupe cannot keep the dead copy.
- **Seats are settled last.** If high gaps are still uncovered after the
  in-stage seeding, `_seed_uncovered_high_gaps_before_heal` runs
  `resolve_seats(intent="repair")` on the final staged report, so each high
  gap is either covered or demoted with `severity_demotion_reason` before the
  completion check (logged as `high_gap_final_seat_settle`). This is the
  repair path's own demotion rule, applied after the repairs that can undo a
  seed rather than before them.

Tests: `tests/test_high_gap_seed_settles_before_barrier.py` (2; both fail on
the unfixed code).


**Guard for the class (follow-up):** the seeder fix covered one writer. Both
gap-report dedupes kept the first row with a repeated `line_id`:
`artifact_sanitize.gap_report.sanitize_gap_report` and
`artifact_repairs._dedupe_interviewer_lines`, two copies of the same rule.
Any writer that appends a live row beside a skipped or omitted row with the
same id lost the live row. Both now keep the live copy when exactly one of
the two is skipped or omitted (`artifact_repairs.gap_line_inactive`).
Otherwise the first row still wins, as before. Tests:
`tests/test_dedupe_keeps_live_line.py` (the two live-copy tests fail on the
unfixed code).
---

## [135] PRODUCT: a lay-up plan made only of typed skips replaced a body that met the hosted VO floor, and loud-failed as "unsatisfiable" (macOS exec_003, one-hour source)

**Stage / area:** `nugget_layup_compose`, `nugget_layup.publish_layup_plan_to_gap_report`,
`raise_hosted_vo_floor_unsatisfiable`
**Status:** FIXED

**Seen:** first run past stage 47 on the one-hour source:
`[ERROR] hosted_vo_floor met have=6 need=3`, then
`hosted_vo_floor_unsatisfiable (active_synthetic=0 < min=3; eligible_nuggets=0) — escalate once, do not recompose`,
`Failed: Stage nugget_layup_compose`. The engine walked back to
`gap_framing_compose`.

**Cause:**
1. The model returned a plan of 21 rows, all typed skips
   (`no_eligible_unspent_nugget`, `native_self_orients`,
   `self_explanatory_native`, `media_ip_cta_hole`), with the warning "All
   corpus nuggets are already represented in selected native audio". That is
   a correct plan: there was nothing unspent to lay up.
2. The gap report on disk already had 6 active synthetic lines from compose,
   above the floor of 3.
3. The publish rule "a hollow plan keeps the prior body when it meets the
   floor" treated a plan as hollow only when `layups` was empty or carried
   `compose_restart`. 21 skip rows are not empty, so publish tried to replace
   6 lines with 0 and raised.
4. The error text came from the floor snapshot, which reads the committed
   body, so the first error line said "floor met".

**Fix:**
- A plan whose candidate body has no active synthetic line is hollow for this
  rule (`active_new == 0`), so a body that meets the floor is kept verbatim,
  through the same path the empty-plan case already used.
- The loud failure names the candidate counts instead of the snapshot's prose.

Tests: `tests/test_nugget_layup.py::test_publish_keeps_prior_body_when_plan_is_all_typed_skips`
(fails on the unfixed code). Replayed on a copy of exec_003: unmodified code
raises, fixed code publishes with 6 active lines.

---

## [136] PRODUCT: the transferred keep list demanded sponsor-outro scraps the selection had excluded (macOS exec_003, one-hour source)

**Stage / area:** `hard_keep.hard_keep_segment_ids` (CTA parent keep transfer),
`selection_order_sanitize`
**Status:** FIXED

**Seen:** `sanitize_refused:selection: hard_keep_missing_from_order:seg_035g,seg_035h,seg_035i`,
`Failed: Stage selection_order_sanitize` twice, then
`dispatch refused for incomplete critical selection_order_sanitize`, and the
run halted with `Delivery incomplete after conductor`.

**Cause:**
1. seg_035 is the sponsor outro (sponsor thanks, production credits, "follow
   us", contact email, host sign-off; `cta_region: whole`). Split into
   children: a to f excluded as `media_ip_cta`; g, h, i are 2-second tail
   fragments ("The Life Sciences DNA.", "I'm Daniel Levine. Thanks for
   joining", "joining us.") that media_ip_cta's recut listed as admitted story.
2. Ranking and the lay-up producer then excluded g, h, i with an outro /
   degraded-CTA reason ("empty, heavily degraded transcript after the CTA cut").
3. A different banned CTA parent was a hard keep. The keep transfer offers
   the **whole** admitted story set, not just that parent's children, and
   only subtracts lattice drops (entry 132). g, h, i were handed back as
   demands, and the lattice lint refused every selection commit.

**Fix:** same shape as entry 132. A child of any banned CTA parent that the
committed selection excludes for an editorial reason (CTA, outro, fragmentary
tail, blank: `is_editorial_exclude_reason`) is not part of the transfer
(`editorial_dropped_cta_children`). A child excluded for any other reason is
still a transferred keep, so a wrongful drop is still refused.

Tests: `tests/test_hard_keep_transfer_skips_cta_scraps.py` (3; the two bug
tests fail on the unfixed code). Replayed on a copy of exec_003: unmodified
code keeps g, h, i and sanitize refuses; fixed code keeps none and sanitize
passes.


**Guard for the class (follow-up):** the transfer offers the whole admitted
story set, not only children of the banned parents. So an editorially
excluded segment from any other family (CTA, outro, blank, fragmentary tail)
could still come back as a demand. The ruled-out set is now
`lattice_dropped_ids | editorial_excluded_ids`, every editorial exclusion in
the committed selection, whatever its parent. A non-editorial exclusion
(budget, ranking) is still transferable. Test:
`test_an_editorial_exclusion_outside_the_banned_families_is_not_transferred`
(fails on the unfixed code).
---

## [137] PRODUCT: 46 nullable enums rejected null, so valid LLM replies failed verification and were discarded; a budget refusal was logged as an OpenAI failure (macOS exec_004)

**Stage / area:** `openai_structured_output._make_nullable`,
`schema_nullability._add_null_to_type`, `openai_schema_semantic_lint`,
`docs/cross-cutting/json-schemas/{artifacts,composed}`, `stages/llm_runner`
**Status:** FIXED

**Seen:** `[ERROR] [connector_seam_adjudicate] OpenAI chat.completions failed (gpt-5.6-terra, advisory)`,
then `Seam adjudication LLM unavailable for 15 pair(s): limit_exhausted:connector_seam_adjudicate:max_invokes_per_identity`.
The run continued, but with no LLM seam verdicts.

**Cause:**
1. The seam schema declares `fuse_direction` as `"type": ["string", "null"]`
   with `"enum": ["into_earlier", "into_later"]`. JSON Schema applies `enum`
   independently of `type`, so null is always rejected.
2. Every `stay_independent` verdict correctly has no direction, so every
   reply failed verification (`None is not one of [...]`), the stage retried
   until the per-identity invoke cap, and all fifteen verdicts were dropped.
   This happens on every run and silently lowers seam quality; it is not
   specific to this source.
3. The same shape existed in 46 nodes across 21 schemas. Three were
   hand-written artifact schemas; the rest came from the strict-mode
   composer, which makes optional fields nullable by adding `"null"` to
   `type` but never added null to `enum`.
4. The cap refusal (`LimitExhausted`) is raised before any request is sent,
   but `llm_runner` logged it as `OpenAI chat.completions failed` at error
   level.

**Fix:**
- `_make_nullable` and `_add_null_to_type` add `null` to `enum` whenever they
  make an enum node nullable. The three artifact schemas list null
  (three-line diff). The 39 composed schemas were regenerated with
  `tools/codegen_openai_schemas.py`; files committed with CRLF keep CRLF, so
  the diff is 18 files, 86 lines.
- The semantic schema lint accepts null in the enum of a nullable node.
- `LimitExhausted` is logged as `LLM call not sent ... ` at warning level and
  re-raised for the caller's fallback.
- Verified against the live API: OpenAI strict structured output accepts the
  regenerated seam schema and returns `fuse_direction: null` for a
  `stay_independent` verdict, which now validates.

Tests: `tests/test_nullable_enum_lists_null.py` (4), including a sweep that
fails if any committed schema has a nullable enum without null.

---

## [138] PRODUCT: a repaired role/tape conflict left a stale blocking stamp on speakers.json, unmarking speaker_roles after G0 and deadlocking the walk; a live run was stamped "Server restarted" (macOS exec_005)

**Stage / area:** `llm_preflight._preflight_missing_framing`,
`speaker_role_evidence.stamp_role_tape_conflict`,
`deterministic_lint._lint_speaker_roles`, `homunculus` seed walk,
`gui_job_reconcile._reconcile_job_file`
**Status:** FIXED

**Seen:** run halted with `RuntimeError: seed order: complete speaker_roles before running missing_framing`.
Every remedy was refused: `cannot run speaker_roles: timeline artifacts exist after G0`.
The second wind repeated it and the run stopped (`complete=False`, 29 stages).
Twice in the same minute the GUI job flipped to `interrupted` with
"Server restarted — infrastructure interrupt" while serve and the driver were
both alive.

**Chain (replayed on a copy of exec_005):**
1. `missing_framing`'s preflight linted the manifest for interviewer/guest
   labels that contradict the tape and found it blocking (8 of 52).
2. It called `repair_role_tape_segment_types`, which re-read the manifest from
   disk, retyped the segments and committed it. The live manifest now lints
   clean (0 of 44).
3. The preflight then re-linted **the copy it had read before the repair**,
   still saw 8 conflicts, and stamped a blocking `role_tape_conflict` onto
   `understanding/speakers.json`.
4. Nothing could clear that stamp: the clearing write in the repair was
   unkeyed, and an unkeyed write from `missing_framing` to a file
   `speaker_roles` owns is a foreign side effect that is skipped (entry 127).
5. `speakers.json` completeness and the speaker lint read the stamp, not the
   manifest, so `speaker_roles` read as partial. The hollow guard unmarked it.
6. `speaker_roles` is protected after G0 and may not rerun, so the heal could
   not re-mark it and the walk could not run it: a deadlock.

Separately, the orchestrator releases `.run.lock` between phases. A GUI poll
in that gap found a running job with no lock and stamped it interrupted.

**Fix:**
- The preflight re-reads the manifest after the repair before re-linting.
- When the live manifest is clean, the preflight replaces a stale blocking
  stamp with the fresh lint, so every reader agrees.
- `stamp_role_tape_conflict` and the repair's clearing write name the owner
  (`stage_key="speaker_roles"`), so they are not skipped as foreign writes.
- `_lint_speaker_roles` re-checks a blocking stamp against the live manifest.
- The job reconciler does not mark a job interrupted while the run's claimed
  driver process (`operator/driver_claim.json`) is alive.

Replay on the exec_005 state: unmodified code leaves speakers.json partial and
the heal refuses; fixed code clears the stamp, the heal re-marks
`speaker_roles`, and it is seed-complete.

Tests: `tests/test_role_tape_conflict_stale_stamp.py` (4; all fail on the
unfixed code).

---

## [139] PRODUCT: a segment that left the manifest after reanchor stayed in content_brief, so topic_coverage_audit refused to start and retries were memo-refused (client Mac, one-hour source)

**Stage / area:** `stage_input_checks._check_topic_coverage_audit`,
`artifact_cross_validate._validate_post_reanchor`,
`artifact_cross_validate.validate_cross_artifacts_healing`,
`artifact_repairs.heal_content_brief_orphan_segment_ids`,
`artifact_repairs.heal_stale_segment_refs_from_errors`
**Status:** FIXED

**Seen (client machine, same pinned environment):** the run stopped at 36/72
with analysis complete:
`StageInputError: topic_coverage_audit blocked`. The cause was
`content_brief topics[2]` and `topics[10]` citing `seg_018`. That id was a
1.5-second clip still present in `segments/boundaries.json` but absent from
`segments/manifest.json` (48 ids). Retries hit `attempt_memo` on the same
fingerprint. The conductor's fallback (`selection_order_sanitize`) could not
run because `master/selection.json` did not exist yet.

**Chain:**
1. `content_brief_reanchor` passes its post_reanchor check against the
   manifest of that moment.
2. Later analysis stages rewrite the manifest. `vernacular_segment_sanitize`
   N-way splits parents that touch a protected zone. `connector_fuse_pass`
   then fuses children and connectors back together. Usually the brief's
   ids come back through the fuse remap. When a short clip's tape ends up
   under a different survivor, the parent id is simply gone from the manifest
   and nothing rewrites the brief.
3. The delivery readiness report re-runs the post_reanchor cross-check before
   `topic_coverage_audit`, reports each such id as an orphan, and the stage
   input check blocks. The block is deterministic, so every retry is refused
   by the attempt memo and the run halts.

Which split or fuse sequence occurs depends on the LLM's segmentation and the
vernacular zones, which vary per run on the same source (our six runs of the
same tape ended with 28 to 52 manifest segments). The same pinned environment
can still take this path on one machine and not another.

**Fix:** before the readiness report, the `topic_coverage_audit` input check
calls `heal_content_brief_orphan_segment_ids`. For each brief id missing from
the manifest (topics, key_claims `segment_ids` / `evidence_segment_ids`):
- Follow the connector fuse `id_remap` chain. If it lands on a live id, use it.
- Otherwise take the id's span from `segments/boundaries.json`. Pick the live
  manifest segment with the most overlap; with no overlap, pick the nearest.
  That segment now carries the tape the id stood for.
- An id with no recoverable span is dropped from the list.

The brief is committed under its producer key and re-stamped, the same commit
`_patch_brief_ids_after_resplit` uses. A warning (`content_brief_orphan_ids_healed`)
records the mapping. It changes nothing on a run whose brief has no orphans.

**Guard for the whole class:** the brief is one of six artifacts a cross-check
can halt on for a stale segment id. The others are gap_evaluations, selection,
narrative_plan, coverage_audit and episode_structure. The two paths that turn
cross-check errors into a halt now go through
`artifact_cross_validate.validate_cross_artifacts_healing`:
- `maybe_cross_validate_after_stage` (hard checkpoint halt)
- `progression_readiness._cross_blockers` (delivery and pre-audio readiness)

When a check names a stale id, `heal_stale_segment_refs_from_errors` resolves
each named id with `resolve_stale_segment_ids`. It follows the fuse remap,
then the id's recorded span in `segments/boundaries.json` or
`vernacular/resplit_report.json`. It applies the rewrite with the same walker
the fuse remap uses (`apply_segment_id_map`) and lands it under the owner key
with the integrity-only mutation class (`persist_segment_id_remap`). Then the
check re-runs. Only ids the errors name are touched. A listed orphan id is
dropped rather than mapped onto a segment that is already covered.

Follow-up: the sound design plan's palette check (`palette segment X not in
manifest`, post_sound_palettes) goes through the same path and is now mapped.
The repair re-reads the file after writing and reports a repair only when the
rewrite is on disk. A gate below the ownership table (the seat freeze) can
skip a write and return normally, and the first version logged those as
healed. Under the delivery freeze the plan stays frozen and the check reports
as before. Replay on a copy of exec_006 with seg_009 removed: before the
freeze the palette id moves to the live segment and the check passes. Under
the freeze the write is skipped and logged as not landed.

Replay on a copy of macOS exec_006 with `seg_017` removed from the manifest:
unmodified code blocks `topic_coverage_audit` with four orphan errors. Fixed
code maps `seg_017` to the adjacent `seg_016` and the orphan block is gone.

Tests: `tests/test_content_brief_orphan_after_reanchor.py` (8; all fail on the
unfixed code).

---

## [140] PRODUCT: owner-keyed side-effect writes were staged under the active stage and discarded at its flush; the tier-D waive never unseated the plan (macOS exec_001, 003, 004, 006)

**Stage / area:** `RunContext.write_json`, `write_staging.keyed_write_lost_at_flush`,
`write_staging.flush_stage_writes`, `execution_contract._tier_d_logged_waive`
**Status:** FIXED

**Seen:** four of six one-hour runs logged
`side-effect write skipped: mastering/mastering_plan.json is not
gap_framing_compose's to write (owner air_contract_sanitize)`. The callers were
`run_execution_invariants`, then `run_vo_contract_ladder`, then
`_tier_d_logged_waive`, then `write_plan`.

**Chain:**
1. The VO contract ladder runs inside `gap_framing_compose`. Its tier-D waive
   marks the line not on air in the gap report (that write lands) and unseats
   it in the mastering plan.
2. The plan write was unkeyed. `gap_framing_compose` has no ALLOW row for the
   plan, so entry 127's rule skipped it as a foreign side effect. The plan
   kept seating a line the gap report had waived.
3. Keying the write as the seat owner is not enough on its own. A write made
   while a stage is active is staged under that stage. At commit,
   `flush_stage_writes` keeps only paths the stage shows or owns and silently
   drops the rest. So an owner-keyed write passed the ownership check, then
   vanished at flush with no log. This applies to every owner-keyed write made
   under another stage, not only this one (for example the
   `stamp_gap_omit_flags` seat stamps in `air_script.py` when run from a
   non-owner stage).

**Fix:**
- `_tier_d_logged_waive` presents the seat owner with the omit-stamp reason
  (`stage_key="air_contract_sanitize"`, `seat_reason="stamp_gap_omit_flags"`),
  the pattern from entry 101. Unseating only shrinks seats, which the freeze
  permits.
- **Guard for the class:** `RunContext.write_json` asks
  `keyed_write_lost_at_flush` whether an owner-keyed write would be discarded
  by the active stage's flush. The predicate is the flush's own test (not
  operator-visible and not owned by the active stage). If it would be, the
  write goes to the committed tree through `write_mirrored_json`, the same
  route `skip_handoff` writes already take. Writes the active stage keeps are
  unchanged, and so are unkeyed writes.

Tests: `tests/test_foreign_side_effect_writes.py` (3 new) and
`tests/test_execution_contract_ladder.py::test_tier_d_waive_unseats_the_plan_when_compose_hosts_the_ladder`
(all 4 fail on the unfixed code).

---

## [141] PRODUCT: a request too large for the org's tokens-per-minute limit was retried as transient before escalating (every macOS one-hour run)

**Stage / area:** `stages/llm_runner.is_transient_openai_error`
**Status:** FIXED

**Seen:** every run logged three
`OpenAI transient error on boundary_topic_resplit (attempt N/4): RateLimitError:
429 Request too large for gpt-4o in organization ...`, and the same for
`island_cluster_structure_adjudicate` on gpt-4o-mini. Only after the fourth
attempt did the call escalate to the flagship tier, which succeeded.

A 429 "Request too large" means the single request exceeds the org's
tokens-per-minute limit. The same request can never fit, so the backoff only
added 14 seconds and three wasted calls per occurrence.

**Fix:** a "request too large" error is not transient, so it escalates on the
first refusal. An ordinary 429 rate limit is still retried with backoff.

**Machine-to-machine note:** the limit belongs to the OpenAI organization, not
the machine. The escalated `boundary_topic_resplit` request was about 265k
characters on the one-hour source. An organization on a lower usage tier may
refuse it on the flagship tier as well, so env matching cannot make this
path identical across accounts.

Tests: `tests/test_fallback_backstop.py` (2 new; the escalation one fails on
the unfixed code).

---

## [142] PRODUCT: a fused slab's keeper trim was copied into the source boundaries on the next fuse round; nine minutes of speech left the map and delivery refused it as unsafe cuts (macOS exec_007)

**Stage / area:** `segment_fuse._write_boundaries`, `segment_fuse.rerun_air_bounds_on_fused`,
`stages/segmentation.evaluate_boundary_quality`
**Status:** FIXED

**Seen:** run 7 stopped with every delivery stage failing
`Boundary detection produced unsafe cuts; delivery is blocked`
(`coarse_or_invalid_segmentation`: 23 segments, mean 130 s, coverage 0.84).
The recovery re-ran the segmentation chain, which re-read the damaged
boundaries, and the run ended with `Delivery blocked: analysis incomplete:
gap_framing_compose`.
`segments/boundaries.json` had no row for 2271-2818 s. That stretch held 1295
transcript words, 16 percent of the tape. The model's own boundary reply
covered it (`seg_037` to `seg_046`).

**Chain:**
1. In the second segmentation session, `connector_fuse_pass` fused a slab
   spanning 2271-2948 s.
2. `rerun_air_bounds_on_fused` clamped that slab's manifest row to its
   ideal-cut keeper window, starting at 2818 s. That is intended: the keeper
   trim is an on-air decision and the code says it must not reach the
   boundaries, because doing so once dropped coverage to 25 percent.
3. The next fuse round's `_write_boundaries` copied every surviving manifest
   row's start and end into `segments/boundaries.json`, trimmed rows
   included. The trim reached the source map anyway, one round later.
4. Coverage fell under 0.85 with too few rows to count as fine-grained, so
   the quality check rejected the map at delivery. Re-running segmentation
   started from the damaged boundaries and could not restore the lost span.

Whether this path occurs depends on which slabs the model fuses and where the
ideal cuts fall, so it differs per run on the same source.

**Fix:** `_write_boundaries` never takes times from the manifest. A fused
survivor spans the source rows it absorbed (its own row and every
`fused_from` id still in the document). Any other row keeps its own source
times.

**Guard for the class:** any writer can narrow or drop source rows. Today that
means connector fuse, resplit, the chapter-close hitch and overlap repair, and
any future one. `RunContext.write_json` now passes every write of
`segments/boundaries.json` through
`boundary_coverage_guard.preserve_speech_coverage`. It compares the transcript
words the map on disk covers with the words the new map covers. If a stretch
of at least 8 words and 3 s of speech would become uncovered, it acts:
- A row that still exists is widened back over its own source span, inside
  the gap.
- A dropped row is restored, clipped to the gap.

It logs `boundary_speech_coverage_preserved`. Edge nudges below that size
pass through unchanged. The fresh map from `boundary_detection` is the
model's output and is not compared. With the original fuse writer restored,
the guard alone keeps the 100-400 s span in the run-7 shape.

Tests: `tests/test_fuse_keeps_source_spans.py` (6). The two writer tests fail
on the unfixed code with the trimmed span, `(340000, 400000)` instead of
`(100000, 400000)`. The two any-writer tests (narrowed row, dropped row) fail
without the guard.


**Correction (exec_009):** the first version of the guard matched rows by
`segment_id` across the rewrite. A resplit renumbers segments, so the new
`seg_032` (1387-1491 s) was widened to the old `seg_032`'s end (1769 s). That
overlapped eleven rows, and the nested re-classification failed with
`boundary timeline invalid: manifest times not monotonic at seg_033`. The
guard no longer matches ids. For each stretch of lost speech, it extends only
the row bordering the gap, and only inside the gap. It prefers a bordering row
that covered that speech before, otherwise the row before the gap (or the row
after it at the start of the tape). Order and non-overlap hold for any
renumbering writer, and no ids are created. Test:
`test_a_renumbering_writer_never_gets_overlapping_rows`.
---

## [143] PRODUCT: the boundary quality gate judged the hitch's editorial keeper map as source segmentation, so pass or fail depended on how much the ideal cuts dropped (macOS exec_006, 007, 008)

**Stage / area:** `stages/segmentation._assert_boundary_quality`,
`chapter_close_hitch` (keeper map), `boundary_coverage_guard`
**Status:** FIXED

**Seen:** the final `segments/boundaries.json` of every run is the
chapter-close hitch's keeper map (`proposed_split_reason: chapter_close_hitch`).
The hitch keeps the tape the episode keeps and leaves editorial cuts out. The
map it replaced is archived as `mastering/chapter_close_hitch/pre_keepers.json`.
The delivery quality gate measured coverage on the keeper map:

| run | map before hitch | keeper map | gate |
|---|---|---|---|
| exec_006 | 0.99, 32 rows | 0.93, 30 rows | pass |
| exec_007 | 0.72, 38 rows | 0.84, 23 rows | reject (needs 0.85 below 30 rows) |
| exec_008 | 1.00, 34 rows | 0.74, 31 rows | pass only because 31 rows clears the fine-grained floor of 30 by one |

Two rules disagree. The hitch says this file is the keep map, and the gate
says it is the source segmentation and must cover the tape. How much the
ideal cuts drop is the model's choice and varies per run. So the same source
passed or failed on an editorial decision, not on segmentation quality.
(exec_007 also had real damage before the hitch, entry 142.)

**Fix:**
- When the hitch has rewritten the map and the keeper map fails on coverage,
  the gate judges the segmentation the hitch was given (`pre_keepers`). If
  that passes, delivery proceeds and logs
  `boundary_quality_judged_on_hitch_source`. Malformed rows in the current map
  still fail. A source map that fails on its own still fails.
- The speech-coverage guard from entry 142 exempts the hitch's write. Its
  omissions are editorial, and its source is archived. Every other writer
  stays guarded.

Applied to the saved state: exec_006 and exec_008 pass as before. exec_007's
source map (0.725, 38 rows) clears the existing fine-grained thresholds. The
damage behind it is now prevented by entry 142.

Tests: `tests/test_fuse_keeps_source_spans.py` (hitch exemption, gate judged
on the hitch source, damaged source still fails; the first two fail on the
unfixed code).

---

## [144] PRODUCT: lay-up copy written as notes about the tape ("the host", "he") refused the stage, and recovery kept reintroducing it until the budget ran out (client run exec_014, Mohan source)

**Stage / area:** `nugget_layup.repair_or_skip_spoken_copy_layups`,
`nugget_layup.recover_open_high_salience_nuggets`, `_unskip_row_with_nuggets`,
`ensure_deterministic_floor_before_refuse`, `spoken_copy_guard`
**Status:** FIXED

**Seen (client):** `nugget_layup_compose` hard-stopped on spoken-copy QC,
first for `seg_001d`, then `seg_023`, on `spoken_speaker_role_label` and
`spoken_gendered_pronoun`. The surviving line was "The host recap adds that the
assay discussed covers more than a thousand genes. Who is he — and why start
there?". QC also reported `open_high_salience_nuggets=['nug_016']`. The
recovery escalated to `budget_exhausted`, the stage never became seed-complete,
and the run stopped at 48/72.

**Chain:**
1. The model wrote lay-up copy as editor notes. For role labels and gendered
   pronouns, the spoken-copy heal only tried rewrites built from planner
   fields. It never applied the existing scrubbers
   (`rewrite_speaker_role_labels`, `scrub_spoken_gendered_pronouns`) to the
   line itself, so an unhealable row was skipped.
2. The skip reopened its high-salience nugget.
   `recover_open_high_salience_nuggets` attached the nugget by pasting its
   raw `text_claim` into a spoken line. Corpus claims are analysis notes
   ("The host recap adds that…"), so the recovery itself produced illegal
   copy.
3. Nothing re-checked spoken copy after the recovery. QC refused, and every
   retry repeated the same steps.

**Fix (the class, not the line):**
- `spoken_copy_guard.scrub_spoken_register` removes role and name
  attribution lead-ins ("The host … adds that"), then applies the shared
  role-label and pronoun rewrites, keeping the claim. Client line becomes:
  "The assay discussed covers more than a thousand genes. Who are they, and
  why start there?"
- The heal scrubs register violations in place first, the same pattern as
  edit-structure refs, so the nugget body survives instead of being skipped.
- Every place a corpus claim becomes spoken copy (recovery attach, unskip)
  uses `_nugget_spoken_claim`, so recovery cannot inject note-style copy.
- After recovery, the spoken heal runs again. A high-salience nugget that
  still has no legal line is parked on the orientation, the existing last
  resort, instead of refusing the stage on every pass.
- The compose input carries `airable_copy_doctrine`.

Tests: `tests/test_layup_airable_copy.py` (3; the heal and recovery tests fail
on the unfixed code: the row is skipped, and the recovered line keeps "the
host").

---

## [145] PRODUCT: a second FileLock instance on a held .write.lock raised filelock's same-thread "Deadlock" (client run exec_014, master/.write.lock)

**Stage / area:** `session_log.append_log`, `operator_action_trace`,
`write_staging._staging_lock`, `file_store.write_lock`
**Status:** FIXED

**Seen (client):** on the final resume, the incomplete lay-up prerequisite
collided with "a different-holder deadlock on master/.write.lock". It was
wrapped as `SeedPrerequisiteFailed` and the orchestrator stopped.

**Cause:** filelock 3.13+ raises `Deadlock: lock ... is already held by a
different FileLock instance in this thread` when a second instance blocks on
a path the thread already holds. `file_store.write_lock` is a per-path
singleton for exactly this reason. But `session_log` (gui_log),
`operator_action_trace` and the staging lock each built their own
`FileLock(lock_path_for(...))` on the same `.write.lock` files. Logging while
a write in that folder held the lock raised the deadlock. Reproduced: holding
`write_lock(run_dir/"run_meta.json")` and appending a log line raises it on
the unfixed code.

**Fix:** all three use `file_store.write_lock`. A sweep test fails if any
module outside `file_store` builds a FileLock on a `.write.lock` again.

Tests: `tests/test_write_lock_single_instance.py` (2; both fail on the unfixed
code, the second with the exact Deadlock error).

---

## [146] PRODUCT: a four-minute network outage ended the run: calls gave up after 14 seconds and the attempt memo refused the retry (macOS exec_008)

**Stage / area:** `stages/llm_runner.create_with_transient_retry`,
`homunculus.runtime` (failure outcome), `dispatch_delta.memo_skip`
**Status:** FIXED

**Seen:** from 13:52 UTC every OpenAI call failed with `Connection error`.
`full_master_ranking` failed, and then
`dispatch refused for incomplete critical full_master_ranking (attempt_memo)
— stopping walk`. The run ended at 42/72 with `Delivery incomplete after
conductor`.

**Cause:** transient retry used 4 attempts over 14 seconds. The failed stage
was recorded as `failed`. The memo refuses a `failed` stage whose state and
progress tokens are unchanged, which is always true after a network outage.

**Fix:**
- Retries back off up to 60 s per wait across 7 attempts, about two minutes
  in all.
- A stage whose failure is transient (OpenAI connection, timeout, rate limit
  or 5xx, or a built-in `ConnectionError` or `TimeoutError` anywhere in the
  cause chain) is recorded as `failed_transient`. The memo does not refuse it
  for up to `MAX_TRANSIENT_RETRIES` (3) in a row, then treats it as failed so
  a dead network still ends the walk. A "request too large" error is not
  transient. Restart clean-up clears `failed_transient` rows like `failed`
  ones.

Tests: `tests/test_transient_failure_retries.py` (5).

---

## [147] PRODUCT: the sponsor-outro tail of the closing segment stayed on air as "story", layup refused it as post-roll, and its high-salience recovery landed on never-touch targets (macOS exec_010, step 47)

**Stage / area:** `media_ip_cta.on_air_orphaned_cta_scrap_ids`,
`media_ip_cta.is_selection_cta_omit_need` (reason vocabulary),
`nugget_layup.recover_open_high_salience_nuggets`,
`stages/analysis_extended.run_nugget_layup_compose` (persist)
**Status:** FIXED

**Seen:** run 10 reached `nugget_layup_compose` (step 47) and failed twice.
1. The model answered `status=partial` with a blocking need: "Remove
   seg_038g … seg_038k from the locked air order. They are
   post-roll/end-credit or corrupt fragments after the coherent close in
   seg_037".
2. On the retry: `open_high_salience_nuggets=['nug_003', 'nug_011']`.

**Chain:**
1. `seg_038` (sponsor read and credits) closes the tape. The recut excluded
   `seg_038a-f` as `media_ip_cta` and admitted the remainder `g-k` as story:
   "The Life Sciences DNA.", "I'm Daniel Levine. Thanks for joining us.", and
   undecodable fragments.
2. `on_air_orphaned_cta_scrap_ids` skips anything admitted as story, and its
   text check missed four of the five. The excluded siblings are gone from the
   live manifest, so position could not be judged from it either.
3. Layup's selection need was meant to become a host-executed CTA omit
   (`llm_simple` demotes such needs). But `is_editorial_exclude_reason`
   matched neither "post-roll", "end-credit" nor "corrupt fragments", so the
   need stayed blocking.
4. On the retry, `recover_open_high_salience_nuggets` placed `nug_003` and
   `nug_011` on rows targeting `seg_007` and `seg_032`, which are never-touch
   CTA segments. Persist prep re-skipped those rows, the nuggets reopened, and
   QC refused after the orientation-park step had already passed.

**Fix:**
- **Closing outro tail:** for an excluded CTA parent that closes the tape, its
  on-air children after the last editorially excluded sibling are scraps even
  when admitted as story. Excluded siblings are read from the selection and
  ordered by split suffix. A CTA parent mid-tape keeps its story children.
- Undecodable STT text (`U+FFFD`) is a scrap.
- The outro vocabulary covers post-roll, end credit, closing credit, end card
  and "after the close". The fragment vocabulary covers corrupt, garbled and
  unintelligible fragments. Budget-type reasons stay non-editorial.
- High-salience recovery never targets never-touch CTA segments.
- After persist prep, if open high-salience nuggets are the only QC errors,
  they are parked on the orientation before the stage would refuse.

Tests: `tests/test_closing_outro_tail.py` (4; three fail on the unfixed code).

---

## [148] PRODUCT: the keep-list transfer protected the closing outro tail, so removal_authority refused its omit on every pass (macOS exec_011)

**Stage / area:** `hard_keep.hard_keep_segment_ids` (parent-keep transfer),
`removal_authority.refuse_selection_removals`,
`media_ip_cta.closing_outro_tail_segment_ids`
**Status:** FIXED

**Seen:** run 11 passed step 47. Entry 147's rule found the closing outro tail
(`seg_032g-k`, credits and fragments after the sponsor read), but every omit
logged `removal_authority: refused to take must-air segment(s) off the order:
seg_032g … seg_032k (producer=media_ip_cta.orphaned_cta_child_omit)`. The
episode ended on the outro.

**Cause:** `seg_032` is a must-keep CTA parent. Its keep transfers onto the
admitted story children, and the recut admitted the tail as story. So the tail
was a hard keep, and `removal_authority` restores any hard keep a producer
drops. Two rules in a circle: the CTA omit says scrap; the keep list says
must-air because it is on air.

**Fix:** the transfer's ruled-out set adds
`closing_outro_tail_segment_ids` (entry 147's rule over the committed
selection), alongside the lattice drops (entry 132) and editorial exclusions
(entry 136). Replayed on a copy of exec_011: the tail leaves the hard keeps,
`heal_on_air_cta_residue` drops it, the air order ends on `seg_031`, and
dependents reconcile with no refusal.

Test: `test_the_parent_keep_is_not_transferred_onto_the_outro_tail` (fails on
the unfixed code).

---

## [149] ENV + PRODUCT: on an 8 GB Mac every music stem started at MusicGen-large, thrashed swap to the hang timeout, and one stem stalled 80 minutes while disk fell to 257 MB (macOS exec_011)

**Stage / area:** `musicgen_runner` model ladder, `mmaudio_sfx`
**Status:** FIXED

**Seen:** run 11 reached `mmaudio_sfx` with 64/72 stages done and no errors.
The first stem's MusicGen-large process was killed at the timeout (`Heavy
task killed: musicgen rc=-9`) and retried as large. Two stems took about 2
hours. A third process sat 80 minutes in uninterruptible wait (`UN`, 1 MB
resident). Swap grew to 17.6 GB and free disk fell to 257 MB before space was
cleared. Run 6 showed the same thrash on the same machine.

**Cause:** the ladder always starts at the configured `musicgen-large`, and
melody conditioning loads `musicgen-melody-large`. Neither fits the unified
memory of an 8 GB Apple Silicon machine next to the pipeline. The ladder only
steps down after a 15 to 40 minute timeout per attempt, per stem.

**Fix:** when installed RAM is known and below
`musicgen.large_min_ram_gb` (default 12), the ladder starts at
`musicgen-medium`, which is where it ends up on such a machine anyway, and
step 0 does not use melody conditioning (same footprint as large). The plan
meta records `musicgen_large_skipped_low_ram_gb`. 16 GB and larger machines,
and machines where RAM cannot be read, are unchanged. A test fixture fixes
reported RAM at 32 GB so ladder tests do not depend on the machine running
them.

Tests: `tests/test_musicgen_low_ram_ladder.py` (3).

---

## [150] PRODUCT: EDL narrative QC required the impact block's framing VO by the compose-era line id and against the plan's first segment, so a framed cut was refused until the invoke cap (client exec_015, step 58 edl)

**Stage / area:** `edl_narrative_qc._validate_framing_before_impact`
**Status:** FIXED

**Seen (client):** `edl` stopped at 58/72 with
`impact segment seg_006 lacks preceding framing VO (vo_question_seg_007).
Re-run gap_framing_compose (never soft-pass EDL)`, three times, then
`max_invokes_per_identity (3/3)` and the walk stopped. The EDL had built 50
events and passed structural QC.

**Cause:** the check took the impact block's `source_segment_ids[0]` as the
segment that must be preceded. It then looked for a VO clip whose `line_id`
was exactly one of the block's planned `framing_line_ids`. Ranking may reorder
a block (the client air order had seg_007 before seg_006). After
`nugget_layup_authority`, layup owns the gap-report body, so the VO that airs
before a segment can carry another id than the compose-era plan names. Either
way a cut whose block was framed failed. The pinned heal (re-run
gap_framing_compose) changes neither the air order nor the EDL's VO ids, so
every retry failed the same way.

**Fix:** the block opens at its first-airing member. It counts as framed when
a VO clip airs before that member and is either one of the planned framing
lines or a seated VO targeting a member of the block. A block with no framing
VO on air still fails.

Tests: `tests/test_edl_narrative_qc.py` (3 new; the other-line-id test fails
on the unfixed code).

---

## [151] PRODUCT (audit): delivery checks that refused a valid state or demanded what their heal cannot produce

**Status:** FIXED (found by a code audit after entry 150, before any run hit
them)

**Class:** a deterministic delivery check refuses a state that is valid, or
asks for something only another stage could produce, so the pinned heal
(usually an edl rebuild from the same inputs) repeats the same failure until
`identical_failure` or `max_invokes_per_identity` stops the run. Entries 139,
147 and 150 were instances found on runs. This entry closes the rest an audit
of `edl_narrative_qc`, the transitions and bridge-completeness path, and the
mix gates found in the code.

| # | check | refused / demanded | fix |
|---|---|---|---|
| 1 | `gap_framing.ranking_exclude_segment_ids` (feeds EDL QC framing-covered) | segments "covered" by skipped or omitted framing lines, which never air | only lines that air cover source tape; plan blocks count only when a framing line is live |
| 2 | `edl_narrative_qc._validate_single_synthetic_between_natives` | orientation seated beside the opening line's VO (straight open, or cold-open deferred hook line), which the builder does on purpose | the episode orientation does not count toward the one-insert limit |
| 3 | `_validate_clone_voice_adjacency` | seams the builder kept because the acoustic listen proved the voices differ (`clone_adjacency_id_mismatch_kept`) | honour the builder's record |
| 4 | `air_order_integrity.collect_violations` | read `bridges.get("bridges")`; the file key is `pairs`, so declared reorder pairs were never honoured | read `pairs` |
| 5 | `_validate_chapter_continuity` | chapters listed out of air order read as "overlap" | sort by air position |
| 6 | `_validate_framing_before_impact` | a zero-length VO at the same start as its speech | order by (start, clip index) |
| 7 | gap-report spoken-sentence collision | collisions with lines that never air | compare only lines that aired in the EDL (record/synthesize, not omitted) |
| 8 | `bridge_completeness.missing_reorder_bridges` | an "after" line on the prior native covers the seam for `_gap_line_covers_seam` but not here | count it |
| 9 | transitions completeness (stage_completion, edl preflight) | hitch air in the EDL counted at mint time only | pass the EDL to both |
| 10 | mint vs `_lint_transitions` | the mint re-created pairs the lint forbids (reverse jump, late opening tape) | `forbidden_bridge_pairs` excluded from mint and completeness |
| 11 | mix gate (`require_spend_artifacts_complete`, post-listen gate, `_missing_sfx_from_mmaudio_qa`) | QA failures of assets that are not placed (unreferenced, `music_omitted`, remediation `skip_cue`) | `qa_failures_that_block_mix` |
| 12 | `transition_vo.synthesize_spoken_transitions` | one ungrounded row raised out of the loop, leaving every later pair unsynthesized | record the row as blocked and continue |
| 13 | `current_transition_pairs_missing` | WAVs for planned pairs no longer adjacent on air | only adjacent pairs |
| 14 | `thought_complete_recut.apply_thought_complete_to_clips` | dropped a reordered callback from earlier tape as "consumed" | only tape that continues from the hanging clip (and listed consumed ids) |
| 15 | `_validate_vo_after_legal_hinge` | a legal close beyond the builder's extension horizon (40 ms before the next on-air tape) | search only up to that horizon; beyond it is the entry-59 warning |
| 16 | `_validate_transitions` | stale transitions.json rows for pairs no longer adjacent (never air) | not an error; non-adjacent transition clips in the EDL are still refused |
| 17 | `_validate_selection_parity` | segments the builder omitted as unplayable when the selection commit was refused | the EDL records `omitted_unplayable_segment_ids`; QC honours them |

Each fix keeps the check strict for genuinely bad states. Tests:
`tests/test_edl_narrative_qc.py` (new cases), `test_bridge_cover_rules_agree.py`,
`test_mix_gate_placed_assets_only.py`,
`test_thought_complete_keeps_reordered_callback.py`.

---

## [152] PRODUCT: under the hard freeze the EDL audit repair skipped every selection write, so a true chapter_continuity_broken blocker could never clear (macOS exec_013)

**Stage / area:** `audit_repair_loop.maybe_repair_after_narrative_audit`,
`artifact_repairs.relabel_chapters_contiguous`
**Status:** FIXED

**Seen:** run 13 reached `edl_narrative_audit` at 57/72 with no errors before
it. Then: `edl_narrative_audit fail after demote — leaving for operator (3
notes; no remutate)`. The blocker was `chapter_continuity_broken`: selection
chapter 2 held `seg_016, seg_021, seg_023` at air positions 11, 12 and 15,
with chapter 3 (`seg_017, seg_018`) at 13 and 14 in between. The opening
`seg_001d-k` belonged to no selection chapter.

**Cause:** the repair loop checks `hard_freeze_active` and, if set, skips all
selection writes (`skip_selection_write_hard_freeze`). It only aligns the
narrative plan. The freeze policy (`FREEZE_WRITE_POLICY`) explicitly allows
`edl_narrative_audit` / `narrative_metadata_align` under `hard_freeze`. So
chapter labels, which change no audio, were never repaired. The disk-stale
demote correctly found the claim true, and the audit stayed failed on every
pass.

**Fix:** under the hard freeze, when the policy allows it, the loop commits
`relabel_chapters_contiguous`. Each chapter becomes a contiguous run of the
locked air order. A chapter that returns after another run merges into the run
before it. An unowned on-air segment joins its neighbour. `ordered_segment_ids`
is never changed. Replayed on a copy of exec_013: chapters become positions
0-10, 11-12, 13-15, 16-17 and 18-21, the stale demote applies, and the verdict
moves from fail to warn.

Tests: `tests/test_relabel_chapters_contiguous.py` (3).

---

## [153] PRODUCT: the orientation retarget in vo_line_adjudicate passed write_permitted, then failed the gap-body sole-writer rule as an error-level authority denial (macOS exec_014)

**Stage / area:** `opening_orientation.retarget_orientation_to_open`,
`stages/vo_line_adjudicate._prep_opening_orientation`
**Status:** FIXED

**Seen:** at 55/72, `authority_denied:persist:understanding/gap_report.json:
vo_line_adjudicate:soft_freeze:nugget_layup_compose
(gap_body_sole_writer:nugget_layup_compose)`. The stage then logged
"opening orientation prep incomplete" and finished without the retarget.

**Cause:** the retarget asks `write_permitted` first, which allowed
`vo_line_adjudicate`. The write itself then ran
`assert_gap_report_body_sole_writer`, which after `nugget_layup_authority`
accepts only the body owner. Two rules, two answers. The retarget relabels the
orientation line's target, the same kind of change as an id remap, and those
already present the body owner (`gap_report_remap_owner`, entries 101 and 129).

**Fix:** when the body owner differs from the calling stage, the retarget
writes as the owner with the integrity-only `segment_id_remap` class. If the
owner still refuses, it keeps the retarget in memory and logs at info, the
same as the frozen branch above it, instead of raising. Reproduced on a copy
of exec_014: the unfixed call raises `AuthorityDenied`; the fixed call writes
the orientation onto `seg_005`.

Tests: `tests/test_orientation_retarget_presents_body_owner.py` (2).

---

## [154] PRODUCT: a bind-heal take Chatterbox wrote before erroring was accepted but never recorded, so vo_synthesize failed on a stale script hash (macOS exec_014)

**Stage / area:** `vo_bind_authority._try_resynth_seated_line`,
`vo_contract.assert_seated_vo_rendered`, `vo_synthesis_audit.record_synthesis`
**Status:** FIXED

**Seen:** at 55/72, `vo_synthesize: seated synthesize VO not rendered:
vo_preface_episode_orientation`, right after `seated bind heal
resynth=['vo_preface_episode_orientation']` and `local_runtime chatterbox rc=0
parsed=True cause=runtime_error`. The orientation WAV was on disk, but
`synthesis_entry_matches_line` returned `stale_script_hash`. The next attempt
re-rendered and passed. The run recovered, but the stage logged a failure.

**Cause:** the heal re-renders a seated line through
`s2s_runner.synthesize_line`. Chatterbox can write the WAV and then fail its
JSON report. The heal then accepts the durable bytes as success, but only the
normal path calls `record_synthesis`, so the synthesis report kept the
previous script hash. The render assert compares hashes and refused the take
it had just accepted.

**Fix:** in that except path, if the line's WAV was written during this
attempt (mtime at or after the attempt start), record it with
`wav_just_rendered=True`, exactly as the normal path records a take. A WAV
older than the attempt is not recorded as new.

Tests: `tests/test_bind_heal_records_durable_take.py` (2).

---

## [155] PRODUCT: post-master quality refused the finished master on an ordinary sentence-opening word read as an unsupported name (macOS exec_014, master_finalize)

**Stage / area:** `spoken_copy_guard.spoken_copy_violations` (strict grounding),
`post_master_quality` check `spoken_vo_speakable`
**Status:** FIXED

**Seen:** at 66/72, `master_finalize: Post-master quality failed:
spoken_vo_speakable` with `gap[vo_layup_seg_017]:spoken_unsupported_entity:
Reaching` for "Reaching routine care raises two practical tests...".

**Cause:** `_PROPER_NAME` matches every capitalised word of three or more
letters, sentence starts included. `_ENTITY_IGNORE` and `_IMPERATIVE_IGNORE`
are hand lists that cannot cover every word a model starts a sentence with.
The layup stage checks the same copy without strict grounding and passes it.
Post-master quality runs strict, so a line that was already synthesized and
mixed is refused at the last stage.

**Fix:** a single capitalised word that opens a sentence and has an ordinary
English word form (-ing, -ed, -ly, -tion, -ment, -ness, -ity and similar) is
not treated as a name. Multi-word names, mid-sentence names, and
sentence-opening words without such a form are still checked, so an invented
"Zorblat" is refused at either position.

Tests: `tests/test_sentence_start_word_is_not_an_entity.py` (2).

---

## [156] PRODUCT: the VO contract ladder's tier A published an empty layup plan before layup had run, raising hosted_vo_floor_unsatisfiable at error level (macOS exec_015)

**Stage / area:** `execution_contract._tier_a_publish_orientation`
**Status:** FIXED

**Seen:** at 41/72, between `narrative_arc_plan` and delivery,
`nugget_layup_compose: hosted_vo_floor_unsatisfiable (active_synthetic=0 <
min=3; eligible_nuggets=0)` twice at error level, from `invariant vo_contract
ladder tier_a_publish_orientation failed`. The ladder moved on and the run
continued.

**Cause:** tier A reads `understanding/nugget_layup_plan.json` as `{}` when it
does not exist and publishes it. Before `nugget_layup_compose` (and before
`nugget_corpus_mine`) an empty plan can never meet the hosted VO floor, so
the publish raised every time the invariant ran early.

**Fix:** tier A returns without writing when no layup plan exists yet. The
next tier handles that state, as it already does when tier A applies nothing.

**Also seen, recovered:** `narrative_arc_plan` answered `partial` once,
asking for an `episode_structure` rerun because the structure separated
`seg_024` from its answer `seg_025`. The stage's own retry completed. The
locked volleys `detect_speaker_volleys` produced on that run overlapped and
did not contain the pair the model named (see entry 50 on the detector's
reach). Left as is.

Tests: `tests/test_tier_a_waits_for_layup_plan.py` (1).

---

## [157] PRODUCT: layup QC read a story line as "reuses dropped CTA wording" because never-touch texts are whole mixed segments (macOS exec_015, step 47)

**Stage / area:** `media_ip_cta.air_overlaps_never_touch`, `nugget_layup.evaluate_layup_qc`
**Status:** FIXED

**Seen:** `Nugget layup QC failed: never_touch_cta[seg_014]: lay-up reuses
dropped CTA wording`, then `Failed: Stage nugget_layup_compose`. The line was
story copy: "Blood tests may reveal molecular changes before imaging does...
circulating tumour cell is captured alive...".

**Cause:** `never_touch_texts` are whole segment transcripts. A mixed CTA
parent (`seg_001`: the sponsor intro plus about 650 words of interview)
carries all the story vocabulary around the sponsor read. `_overlap` divides
shared tokens by the smaller set, which is the short layup line, so the story
line overlapped the parent at 0.59 against a 0.45 threshold.

**Fix:** only wording that does not also air as story counts. Tokens of the
on-air selection's segments are removed from each never-touch text before
the comparison, and the share is taken over the line's own tokens. Sponsor
copy ("sponsored by Agilisium Labs ...") is still refused. With no selection
yet, the previous measure applies. On exec_015's state the seg_014 line now
passes and a sponsor-copy line is still flagged.

Tests: `tests/test_cta_wording_overlap_ignores_story.py` (2).

---

## [158] PRODUCT: a hosted-VO floor miss after layup authority pinned gap_framing_compose, which the sole-writer rule refuses, so the run looped authority_denied to the invoke cap (macOS exec_015)

**Stage / area:** `stage_completion.high_gap_heal_resume_stage`,
`artifact_ownership.assert_gap_report_body_sole_writer`
**Status:** FIXED

**Seen:** after layup failed QC (entry 157), recovery skipped its CTA rows and
the gap report had 0 active synthetic lines. `Cleared stale
.stage_done/gap_framing_compose: hosted_vo_floor — resume
gap_framing_compose`, then compose failed three times with
`authority_denied:persist:understanding/gap_report.json:gap_framing_compose:
pre_soft_freeze:nugget_layup_compose (gap_body_sole_writer)`. Then
`max_invokes_per_identity`, and `Delivery blocked — analysis incomplete:
gap_framing_compose` stopped the run at 45/72. The VO contract warning in the
same log already named the right owner (`resume nugget_layup_compose`).

**Cause:** `high_gap_heal_resume_stage` pins compose unless layup claimed air
for a missing high gap. Once the gap report carries `nugget_layup_authority`,
`assert_gap_report_body_sole_writer` accepts only layup, so the pinned stage
can never land the floor.

**Fix:** with layup authority stamped and a layup plan on disk, the resume is
`nugget_layup_compose`, the only stage that can write the body (the walk runs
its seed prerequisites first). An orphan stamp without a plan keeps the compose
pin, as HG-5 designed. `test_hg5_recovery_empty_plan_pins_framing` now asserts
the layup pin and that recovery never resumes at compose in that state.

**Follow-up (same family):** the floor snapshot in exec_015 was built with
`stage_id="gap_framing_compose"`, and `hosted_vo_authority.resume_producer`
returned that id unchanged before reaching the fixed function. One helper,
`stage_completion.writable_gap_body_stage`, now maps a compose pin to layup
whenever layup owns the body. It is applied in `high_gap_heal_resume_stage`,
both floor branches of the compose incompleteness check,
`hosted_vo_authority.resume_producer`, and the generic
`thrash_hardening.resume_producer`.

Tests: `tests/test_gap_body_resume_is_writable.py` (3; the first fails
without the follow-up).

---

## [159] PRODUCT: layup's floor check read the committed gap report (0 lines) as hollow while publishing a plan with a live line, so it logged hosted_vo_floor_unsatisfiable at error level before proceeding anyway (macOS exec_016, step 47)

**Stage / area:** `nugget_layup.raise_hosted_vo_floor_unsatisfiable`
**Status:** FIXED

**Seen:** `nugget_layup_compose: hosted_vo_floor_unsatisfiable
(active_synthetic=1 < min=3; eligible_nuggets=1) — escalate once, do not
recompose` at error level. The run did not stop. The next floor check
logged `progress_floors advisory: hosted_vo_floor have=1 need=3
pool_exhausted=True` and the walk went on to `refinement_agenda`. The GUI
still showed 1 error.

**Cause:** the function judged hollowness from `floor_snapshot`, which reads
the committed gap report. During publish that is the body being replaced
(0 synthetic lines), so the status was `HOLLOW_ZERO` although the candidate
body had 1 live line. The hollow branch stamped `hosted_vo_floor_unsatisfiable`
into the plan and run meta, then logged the loud failure. The function's own
`except Exception` swallowed the raise, and the aspirational path proceeded
on the next lines. The error and the stale stamp were both spurious.

**Fix:** hollow is judged on the published body's count (`active < 1`). A
`HOLLOW_ZERO` snapshot no longer takes the hollow branch when the candidate
has a live line, and it skips the snapshot-based aspirational check (which
would refuse on the stale state). The `hosted_vo_aspirational` path then
proceeds on the advisory. A truly hollow publish, or a short floor without
aspirational floors, still fails loudly.

Tests: `tests/test_layup_floor_judges_published_body.py` (3; the first fails
on the unfixed code with the exec_016 message).

---

## [160] PRODUCT: the VO contract invariant read "seated line missing WAV" before vo_synthesize as a violation, and the ladder's tier D waived the run's only host line, looping layup (macOS exec_016, steps 47 to 51)

**Stage / area:** `vo_contract.validate_vo_contract`,
`execution_invariants.invariant_vo_contract`,
`execution_contract._tier_d_logged_waive`
**Status:** FIXED

**Seen:** layup published one live host line (`vo_layup_seg_024`; the floor
of 3 was short with the pool exhausted, entry 159). At 11:38, around step 51
and before `vo_synthesize`, the VO contract repair plan recorded `seated
synthesize vo_layup_seg_024 missing WAV` with `consumer_stage: invariant`.
The ladder ran tiers A to D, and tier D marked the line
`execution_contract_waive` / `tier_d_logged_waive`. The gap report then had 0
active lines, `hosted_vo_floor_unmet: need=3 active=0 — resume
nugget_layup_compose` repeated with `identical_failure ... x38/3`, the walk
dropped layup's done marker, and layup re-ran (done fell from 51 to 50).

**Cause:** two gaps in one family (a check demanding output from a stage
that has not run, as in entry 156):
1. `validate_vo_contract` reports every seated synthesize line without a WAV.
   Before `vo_synthesize` that is every seated line, by construction.
   `stage_input_checks` already filtered it for four named stages; the
   invariant ladder (`consumer_stage="invariant"`) did not.
2. Tier D could waive the last live host line. Zero active lines is the
   catastrophic floor state; nothing downstream can recover it, so it can
   only loop back to layup.

**Fix:**
1. `vo_contract.vo_wavs_due(ctx)`: missing-WAV violations are reported only
   once `vo_synthesize` is done. This is in `validate_vo_contract` itself, so
   every caller gets it. `vo_synthesize`'s own completeness check is
   unchanged and still demands every WAV.
2. `_tier_d_would_hollow_hosted_floor`: when hosted framing requires
   synthetic VO, tier D refuses to waive the last live host line (logged as
   `tier_d_refused_last_host_line`), as it already refused a required
   orientation.

Tests: `tests/test_vo_contract_not_due_before_synth.py` (3).

---

## [161] PRODUCT (audit): checks that ran before the stage producing what they check

**Status:** FIXED (found by a code audit after entries 156 and 160; #1 was
also live on macOS exec_016)

**Class:** a check runs before the producer of the artifact it inspects,
reads the artifact's legitimate absence as a defect, and then logs an error,
runs a repair that destroys valid state, or pins a resume that loops.
Entries 156 (tier A published an empty layup plan) and 160 (missing WAV
before `vo_synthesize`, tier D waived the only host line) were the first two
members found on runs. Entry 156 fixed one instance without a family sweep,
which is why 160 reached a run. This entry is that sweep.

| # | check | premature demand | consequence | fix |
|---|---|---|---|---|
| 1 | `vo_contract._record_hosted_floor_unmet`, reached from layup's own preflight (`stage_input_checks._vo_contract_issues` → `ensure_hosted_framing_vo_seats`) | hosted floor judged on the analysis-era body before layup had written lines; layup counted as seed-front | `identical_failure nugget_layup_compose` toward the halt cap before layup ever ran (exec_016: `x3/3 halt=True` at 11:24:18, first layup call 11:25:49); `needs_operator` / unsatisfiable stamps; reseat beats layup then replaced | a floor miss is layup's failure only once its plan exists (advisory before, stamp kept); the layup preflight skips the reseat until the plan exists (`_hosted_floor_due`) |
| 2 | `execution_contract._tier_c_vo_adjudicate_heal` (EDL VO coverage ladder) | lines with no WAV at all, which only `vo_synthesize` can produce | unmarked `vo_line_adjudicate`, which rewrites gap text and purges valid WAVs | acts on `wav_stale` rows only; missing WAVs go to the exhaust path, which pins `vo_synthesize` (HV-2) |
| 3 | `execution_contract._tier_d_logged_waive` | a `missing_wav` violation after synthesis (a line the floor just reseated) | waived that line again: reseat/waive ping-pong below the floor | tier D refuses `missing_wav` (`tier_d_refused_missing_wav`) |
| 4 | `execution_contract._tier_b_gap_recompose` | ran before layup had a plan | recompose took its skip-copy branch and force-marked `gap_framing_recompose` done, so its layup authority pass was skipped later | returns when layup is enabled and has no plan yet, as tier A does |

The audit also examined and cleared about twenty other checks:
`assert_books_agree`, floor snapshot `have()`, the cross-validate
checkpoints, the per-stage input checks, `vo_sanitary_errors`, `check_g1_vo`
and others. Each one is already guarded, or reads only its own or an
earlier stage's output.

Tests: `tests/test_checks_wait_for_their_producer.py` (6; five fail on the
unfixed code, one keeps the post-layup behaviour).

---

## [162] PRODUCT: a stage's persisted output was unioned with the doc already on disk, so a re-audit could never clear its own complaint and a retry kept the failed attempt's chapters (macOS exec_016, step 58)

**Stage / area:** `artifact_completeness.make_stage_persist`, `merge_artifact`,
`_deep_merge`; seen on `edl_narrative_audit` and `narrative_arc_plan`
**Status:** FIXED

**Seen:** `edl_narrative_audit` pass 1 failed on one seam (`seg_019 →
seg_015` lands mid-thought) and logged `fail after demote — leaving for
operator`. Pass 2's own reply had the same complaint, and its repair demoted
it under the frozen order (verdict `warn`). But the staged file read `verdict:
fail` with pass 1's blocking issue, in pass 1's wording, plus pass 1's repair
entries. The pre-flush commit barrier refused it (reproduced on a clone of the
run: `halt`, first reason = pass 1's complaint). The stage still logged
`Stage finished`, then `Cleared stale .stage_done/edl_narrative_audit: ...
newer uncommitted pending`. Pass 3 went the same way, then `Delivery incomplete
after conductor — edl_narrative_audit thrash cap` stopped the run at 57/72.
The audit also warned that the persisted narrative plan had duplicate
chapter ids: `ch_05` appeared twice, and the chapters came from two
different narrative plans.

**Cause:** every `make_stage_persist` write uses `merge_from_disk=True`.
`_deep_merge` unions lists with the disk copy and skips an empty patch list
(`if not val: continue`). So a cleared `blocking_issues: []` kept the old
issue, and the verdict was re-derived as `fail`. The narrative plan's failed
`partial` attempt and its retry were unioned the same way. The
`<key>_replace` escape hatch is never set by any caller.

**Fix:** a read-only trace of every caller confirmed that each persist
carries the attempt's complete document, and sharded stages merge in memory
before one persist. So `make_stage_persist` now passes `replace_lists`: the
attempt's lists, empty ones included, replace the disk lists. `_meta` still
deep-merges and omitted keys survive. The two single-owner verdict reports
(`master/edl_narrative_audit.json`, `master/coverage_audit.json`) are
replaced wholesale apart from `_meta`, so stale `repair_notes` cannot carry
over either. Direct `merge_from_disk=True` callers keep the old union
(gap_framing_compose relies on it to keep seeded lines through an empty
shard). The keyed manifest and speakers merges are unchanged.
`boundary_topic_resplit` keeps the union for now: it shares
`segments/boundaries.json` with `boundary_detection` and has no row floor if
the model returns only the split rows.

**Follow-up:** a refused commit still logged `Stage finished`. That is
fixed in entry 163.

Tests: `tests/test_stage_persist_replaces_lists.py` (6).

---

## [163] PRODUCT: when the commit barrier refused a stage's staged outputs, mark_done swallowed the refusal and sealed the stage with nothing committed (macOS exec_016, step 58)

**Stage / area:** `RunContext.mark_done` (auto-flush before seal)
**Status:** FIXED

**Seen:** three times on `edl_narrative_audit`: `mark_done(...):
auto-flushing pending writes before seal`, then 0.17 s later `Stage finished`
with the committed audit unchanged since pass 1. That was too fast for a
flush, which promotes a 326 MB memory file. Then `Cleared stale .stage_done/...
newer uncommitted pending`, and the thrash cap. On a clone of the run the
pre-flush barrier returned `halt` for that audit (entry 162).

**Cause:** the auto-flush runs inside `mark_done`'s pre-seal block, which
ends `except _AD: raise / except Exception: pass`. The barrier refusal is a
`WriteApprovalBlockedError`, not an `AuthorityDenied`, so it was swallowed
and the done marker was touched with the staged outputs never committed. The
comment above the auto-flush (exec_13177) says this split-brain is what it was
added to prevent.

**Fix:** a barrier refusal during the auto-flush is logged with its reason and
re-raised as `authority_denied:mark_done:commit_barrier:<stage>`, the same
shape as "pending writes remain after auto-flush". `try_mark_done` returns
False, and `_auto_complete_or_raise` fails the stage loudly instead of
reporting success. Other flush errors still propagate as before.

Tests: `tests/test_mark_done_refuses_on_commit_barrier.py` (2; both fail on
the unfixed code, where `try_mark_done` returned True with nothing committed).

---

## [164] PRODUCT: an intro cut into many pieces read as "opening tape airing late", which failed the listen-delight ship gate (cut_integrity 0.5) and the post-master intro check (macOS exec_017)

**Stage / area:** `listen_delight._late_opening_native_in_edl_ok`,
`post_master_quality._pmq_no_late_opening_native`
(`spoken_native_intro_duplicate`)
**Status:** FIXED

**Seen:** run 17 reached G-Publish with 0 errors, but `listen_delight_audit`
scored overall 0.8607 (min 0.9) with `cut_integrity` 0.5, below the
catastrophic 0.7, and `finishability` 0.79. It passed only through
`catastrophic_as_advisory`. Post-master quality failed
`spoken_native_intro_duplicate`. The advisory also wrote an active remutate
plan after the master, so the hollow-done guard unmarked `edl`,
`junction_snip_qa` and `listen_delight_audit`. The EDL was rebuilt twice,
identically under the hard freeze, until `max_invokes_per_identity`, which
left the run at 63/72 at the gate.

**Cause:** both checks call an opening-window clip "late" when its index in
the speech list is at or past 25% of the clip count. The episode opened with
its intro in tape order, cut into eight children (`seg_001d`..`seg_001k`,
indices 0 to 7 of 24), so the last two pieces were "late". Junction residuals
were 0 and real hanging ends were 2 of 24.

**Fix:** one shared rule, `air_order_integrity.opening_tape_airs_late`.
Opening tape is late only when a quarter of the speech clips from outside
the opening have already aired before it. A split intro and a cold-open hook
are allowed; opening tape returning mid-episode is still caught. On
exec_017's state, `cut_integrity` is 0.9333 and listen-delight passes
(overall 0.9419, no failed dimensions), so the post-master remutate would
not have fired.

Tests: `tests/test_split_intro_is_not_late_opening.py` (4).

---

## [165] PRODUCT: the mix dropped every planned stinger (a 0.4/min rate truncated to 0) and the closing outro (deduped against a cold open anchored at the end) (macOS exec_017)

**Stage / area:** `sound_design.flow1_overlays_from_sdp`,
`acoustic_profile`, `music_lane.collapse_duplicate_music_cues`,
`artifact_repairs` (cue anchor fallback)
**Status:** FIXED

**Seen:** post-master quality failed `episode_close_outro_present`
(`required: true, realized: false`). Of 18 planned music cues, the mix
realized only the cold-open motif and the underscore beds. The log showed
`mix: stinger cap reached (0/timeline) — dropped
onecell_documentary_stinger_method` ten times, then `generated but unplaced
music assets (lane/cap): [stinger_method, full_bed_close]`. Both assets had
been generated. `music_cue_coverage.missing_asset_ids` was empty, so the drop
was not visible there either.

**Cause:**
1. `soundscape_policy` sets `stinger_max_per_minute: 0.4`, a rate. The mix
   did `int(contract.get("stinger_max_per_minute", 4))`, which is 0, times
   integer minutes, so the cap was 0. `acoustic_profile` had the same
   `int()`.
2. The air script creates the cold-open cue `before_segment` the first
   segment. On the plan it arrived as `after_segment` with no anchor, and
   the anchor fallback gives any such cue the last selection id. The mix
   places cold opens in the opening window regardless of anchor, so the
   motif still played at the top. But `collapse_duplicate_music_cues` keys
   cues by (placement, anchor, lane), and the cold open and the outro share
   the bookend lane, so the outro was dropped as its duplicate.

**Fix:** the cap is the rate times real timeline minutes (at least 1 when the
rate allows any); the profile keeps the rate as a float. Bookends are keyed
by role in the dedupe, so a cold open and an outro are never duplicates. The
anchor fallback sends an unanchored cold open to `before_segment` the first
id. On exec_017's state the mix now places the cold open at 10.7 s, 6
stingers (cap 7; 3 dropped by the policy cap as intended), 5 beds and the
closing outro at the end of the timeline (17.9 s).

Tests: `tests/test_music_stingers_and_outro_land.py` (3).

---

## [166] PRODUCT (audit): the rest of the 164/165 families, found by a sweep on exec_017's artifacts

**Status:** FIXED (low-impact items listed as open)

**Class:** entry 164 measured air position by raw clip index although
segments are split into letter children; entry 165 truncated a fractional
rate, keyed a dedupe by a shared lane instead of a role, and let a coverage
report hide drops. A read-only sweep probed each candidate against exec_017's
artifacts.

| # | where | on exec_017 | fix |
|---|---|---|---|
| 1 | `sound_design` music coverage (`master/music_cue_coverage.json`) | generated-but-unplaced assets were erased from the report, so the ten dropped stingers and the dropped outro left `missing_asset_ids: []` and `preserved: true` | recorded as `unplaced_asset_ids` / `unplaced_assets` (with role); an unplaced bookend clears `preserved`. A cap drop still does not fail the mix |
| 2 | `artifact_repairs.repair_sound_design_plan` bed quartile seeds and palette expansion | seeds by clip index landed on 12 s intro and outro children (bed islands at 61 s and 1017 s); 6 of 9 added anchors were intro or outro pieces | `_duration_spread_ids` and `quartile_segment_buckets`: each duration quarter without a bed gets one on its longest segment |
| 3 | `creative_delivery.hydrate_flow_cue_segments`, anchor fallback | an unanchored cold open took a transition slot (after `seg_017`) and shifted later stingers; the fallback read `cue.role` only | bookends anchored by effective role first: cold open before the first id, outro after the last |
| 4 | `listenability_guards.host_vo_coverage_ratio` | 2/24 = 0.083 below the 0.12 floor (false advisory) | counted by parent family (0.30 on exec_017) |
| 5 | late opening landing against `opening_body_start_index` in `bridge_completeness.forbidden_bridge_pairs`, `artifact_repairs` transition prune, `deterministic_lint`, `stages/selection` transition filter, `air_order_integrity` chapter mask | 5 pairs inside the intro marked forbidden | `air_order_integrity.family_air_positions`: every child takes its family's first position (no forbidden pairs on exec_017) |
| 6 | `soundscape_policy` density scaling | `int(v * scale)`: cap 2 gave {0,0,0}, cap 4 lost 2 slots | largest remainder, at least one slot per budgeted role the cap affords |
| 7 | `music_lane.collapse_duplicate_music_cues` | legacy `motif` / `full_bed` still collapsed into each other | bookend keys normalised (motif = cold open, full_bed = outro) |
| 8 | `story_health` | verdict `fail` with missing reorder bridges while bridge completeness was complete | same inputs as completeness: justified skips, live EDL, forbidden pairs (exec_017 now `pass`) |

**Open, low impact:** `ensure_hook_early` and related hook-in-first-three
checks count by index (no hook declared on exec_017); the non-creative
stinger trim in `artifact_repairs` (`int(cap*minutes)`, off by default); the
hard-keep partial-family check at `air_order_integrity` keeps per-clip
position on purpose; `spoken_copy_guard`'s `before_air_index` branch is
never fed. `opening_orientation` reads exec_017's host intro as interviewee
(`native_open_already_orients` False); not yet investigated.

Tests: `tests/test_split_children_and_music_family.py` (6).

---

## [167] PRODUCT: the EDL overlap merge seated the union at the consumed id's slot while the selection kept it at the survivor's, so EDL narrative QC refused the EDL (macOS exec_019, step 58)

**Stage / area:** `edl_overlap_repair._rebuild_clips`,
`retire_consumed_ids_from_selection`
**Status:** FIXED (one open observation below)

**Seen:** `EDL overlap merge: seg_019→seg_017`, then `EDL narrative QC
failed (3 issue(s)): master/edl.json: speech clips do not match final
selection ordered_segment_ids; expected [... seg_016, seg_018, seg_017,
seg_020 ...], got [... seg_016, seg_017, seg_018, seg_020 ...]`, plus a
chapter overlap and a volley split that follow from the same order. The walk
unmarked four upstream producers as hollow and re-ran them (layup recomposed
and two VO takes were purged for re-synthesis) before rebuilding the EDL.

**Cause:** the air order at VO time was `seg_019, seg_018, seg_017`, and
`seg_019` overlapped `seg_017` in source tape. `_rebuild_clips` put the union
at the first member's slot (`seg_019`'s) and moved `seg_018`, which aired
between the members, after it. `retire_consumed_ids_from_selection` drops
the consumed id in place and keeps the survivor where it already airs. The
two orders disagreed whenever a segment aired between the members.
`seg_017`'s span (1716 to 1892 s) is wholly after `seg_018` (1668 to 1716 s),
so the selection's order was also tape order.

**Fix:** the union is seated at the survivor's own air slot; consumed clips
are dropped in place, transitions between members are dropped as before,
and VO pickups that targeted any member follow the survivor. The EDL and the
selection now agree by construction.

**Open observation:** in the same recovery, layup logged `seat_freeze: skip
write understanding/gap_report.json` and then `Purged stale VO for
vo_layup_seg_024 / seg_031 (spoken_text_change)` 30 ms later. If the text
change never landed, those re-syntheses repeat identical audio (wasted
minutes, not a wrong master: synthesis renders from the committed text). Not
yet traced to the write that triggered the cascade.

Tests: `tests/test_overlap_merge_keeps_selection_order.py` (3; the first fails
on the unfixed code).

---

## [168] PRODUCT: a fuse pass that re-planned the same merges was recorded as a critical cut residual, which blocked junction_snip_qa two hours later (macOS exec_019)

**Stage / area:** `segment_fuse.run_high_value_cluster_fuse_rounds` and the
connector fuse round loop, `delivery_guardrails.record_delivery_residual`,
`publishability_boundary` (post_junction)
**Status:** FIXED

**Seen:** at 20:13 `junction_snip_qa` failed with `PublishabilityBlocked:
publishability blocked at post_junction: incomplete_cut_unresolved —
critical_residuals=1 kinds=['fuse_oscillation'] sources=['delivery_ledger']
blocking=[]`. The only residual was `fuse_oscillation` (critical, open),
recorded by `connector_fuse_pass` at 18:16 when round 2's plan signature
equalled round 1's. The retry passed and the walk went on to
`master_finalize`, but the run logged an error and spent a retry.

**Cause:** both fuse round loops treat "this round planned exactly the
previous round's merges" as an oscillation and record a critical residual.
An identical repeat is a stable state: no further round, junction step or
EDL rebuild can change it, so a blocking cut residual can never clear. A
genuine A to B to A flip-flop would not even match this check. Separately,
`record_delivery_residual` accepts only `critical`, `soft` and `advisory`
and coerces anything else (including `warning`) to critical; the first
attempt at this fix used `warning` and its test caught that it still blocked.

**Fix:** both loops record the repeat as `advisory` with the detail kept
(`converged_repeat` on the round). Advisory rows are not counted by
`critical_residual_view`. The three other `record_delivery_residual` callers
already pass a valid severity.

Tests: `tests/test_fuse_repeat_is_not_a_critical_cut.py` (3, including one
that pins the coercion of an unknown severity to critical).

---

## [169] PRODUCT: an LLM null that its envelope allows was refused by a stricter artifact schema, and the CTA lock re-applied it on every retry, stopping delivery at full_master_ranking (client run, step 44)

**Stage / area:** `RunContext.write_json` (validation), new
`schema_conform.py`, `media_ip_cta.normalize_media_ip_cta_rows`,
`media_ip_cta._reapply_locked` / `extract_judgments`
**Status:** FIXED

**Seen (client):** delivery entry blocked on `full_master_ranking`:
`master/selection.json` never committed because schema validation rejected
`media_ip_cta[1].mixed_with_story` = null (segment seg_035); the schema
requires a boolean. `selection_order_sanitize` and the rest of delivery could
not proceed and the orchestrator halted after retries.

**Cause:**
1. The LLM envelope allows null there (entry 137 made enums nullable so valid
   replies stop failing); `normalize_media_ip_cta_rows` copied every optional
   value through verbatim, null included.
2. After the first failure the judgment was locked in
   `mastering/media_ip_cta.json`; `_reapply_locked` re-injects the locked
   judgments on every retry, so a later valid reply could not clear it.
3. Family: some write validators relax optional leaves to accept null
   (`_validate_by_artifact_schema` -> `with_nullable_optional_leaves`); others,
   including `master/selection.json`, the EDL, the narrative audit, the
   synthesis report and the acoustic profile, call `_validate_dict` on the raw
   schema. The same reply was admitted for one artifact and refused for
   another. The existing `omit_nullable_null_leaves_for_disk` only covers
   paths listed by hand per stage, and `mixed_with_story` was not listed.

**Fix:**
1. `normalize_media_ip_cta_rows` drops null optional fields, coerces
   `mixed_with_story`, `must_keep_in_clip` and `cta_open` to booleans, and
   matches `cta_region` / `open_choice` case-insensitively. `extract_judgments`
   runs the same normaliser, so a lock file already holding the null heals on
   the next retry.
2. Family: `schema_conform.conform_to_schema` repairs a document against the
   artifact's own schema, and `write_json` runs it only after validation has
   failed, then validates again, so a valid document is never changed. It
   drops null optionals the schema cannot take, defaults a required null to
   False / [] / {} for booleans, arrays and objects (a required string or
   number still fails), coerces scalars to the declared type, matches enums
   case-insensitively, drops null array items and unknown keys under
   `additionalProperties: false`, and logs every fix. The schema is derived
   from each write validator, so 73 of 74 validators are covered without a
   list (the exception is `mastering/research/waves.json`, a hand-written
   check).

Tests: `tests/test_schema_conform_on_write.py` (6, including a real
`write_json` that the old code refused).

---

## [170] PRODUCT: a garbled sign-off cut as its own segment after the closing sponsor read aired as the episode's last clip (macOS exec_020)

**Stage / area:** `media_ip_cta.closing_outro_tail_segment_ids`,
`on_air_orphaned_cta_scrap_ids`, new `_tape_tail_scrap_ids`
**Status:** FIXED

**Seen:** run 20 reached G-Publish with 0 errors, but the last clip on air
was `seg_050` (5.3 s): "You most of the time, Michael. You are listening to
your follow-up. You are listening to usHS\ufffd bone and cut-". Layup had
warned that `seg_049g` through `seg_050` were incoherent end fragments.

**Cause:** the closing-outro checks from entries 147/148 only look at NLE
children of CTA parents (`seg_049a..i`). `seg_050` is a top-level segment
that starts after the closing sponsor parent `seg_049`, so neither check saw
it, although `looks_like_orphaned_cta_scrap` already flags its text (U+FFFD,
outro wording).

**Fix:** `_tape_tail_scrap_ids`: an on-air segment that is not a child of any
CTA parent, starts at or after the closing sponsor read (the latest CTA parent
ending within 2 minutes of the tape end), and fails the existing scrap test is
part of the closing tail. Both `closing_outro_tail_segment_ids` and
`on_air_orphaned_cta_scrap_ids` include it, so the CTA residue heal and the
keep-list exclusions take it off air. Closing story after the outro passes
the scrap test and stays. On exec_020's state both detectors now return
`seg_050`.

**Also on run 20 (product decision, not a code fault):** layup skipped all 27
planned lines with typed reasons (`self_explanatory_native` 15,
`no_eligible_unspent_nugget` 8, `listener_already_oriented` 3), so host VO is
15 s of a 26-minute master and the listener scorecard's clarity is 0.77
against a 0.80 floor (advisory under aspirational floors). Nuggets are mined
from the tape, so when the selection keeps the strong tape few "unspent"
nuggets remain to frame. Whether hosted VO should be forced to its floor is
for the product owner.

Tests: `tests/test_tape_tail_scrap_after_closing_cta.py` (3).

---

## [171] PRODUCT: an all-skip layup plan raised hosted_vo_floor_unsatisfiable at error level when the prior body had live lines below the floor, then the walk proceeded on that same body (macOS exec_021, step 47)

**Stage / area:** `nugget_layup.publish_layup_plan_to_gap_report` (hollow-plan
guard)
**Status:** FIXED

**Seen:** layup's reply skipped every row (`self_explanatory_native`,
`no_eligible_unspent_nugget`, ...) and omitted the opening orientation as
redundant. Publish raised `nugget_layup_compose: hosted_vo_floor_unsatisfiable
(active_synthetic=0 < min=3; eligible_nuggets=0)` (4 error lines, stage
failed). One step later the walk continued with the compose-era gap report
(`vo_context_seg_012`, `vo_bridge_seg_022` live) as a partial floor on the
advisory.

**Cause:** entry 135's guard preserves the prior body when a plan is hollow,
but only when that body fully meets the floor. With 2 of 3 live lines it fell
through to the loud escalation, although progress floors accept a partial
floor and the run proceeded on exactly that body anyway.

**Fix:** a hollow plan also preserves a partial prior body (at least one live
line) when progress floors are aspirational, logged as a warning. A hollow
plan with no live prior line, or a partial prior under strict floors, still
escalates loudly.

Tests: `tests/test_nugget_layup.py::test_publish_keeps_partial_prior_body_under_aspirational_floors`
(fails on the unfixed code) and `::test_partial_prior_without_aspirational_floors_still_escalates`.

---

## [172] PRODUCT: after re-segmentation renumbered ids, readers paired the stale episode structure with live segment text; gap_framing_compose answered partial and fell back (macOS exec_022)

**Stage / area:** `episode_structure.load_episode_structure` and its readers
(`stages/gaps.py`, `stages/selection.py`, `stages/analysis_extended.py`,
`sound_design`, `opening_orientation`, `shape_order_emit`,
`talking_points_authority`, `assembly_ledger`, `timeline_optimizer`)
**Status:** FIXED

**Seen:** `LLM stage gap_framing_compose incomplete: status=partial needs=
[rerun_stage episode_structure_compose: "Reconcile the duplicated
episode_structure.segment_order with the ordered manifest ... target contexts
are shifted or role-inconsistent after seg_013 ... seg_021 is an interviewee
answer but its target context is only 'Okay.'"]` at error level; the stage
fell back to a deterministic cover and the run continued.

**Cause:** the walk re-segmented after the first compose (hitch,
classification, then the connector fuse merged 15 seams), renumbering segment
ids. `gap_framing_compose` ran again before `episode_structure_compose`, so
its packet carried the structure built for the old ids next to contexts built
from the new manifest. Thirteen readers took `episode_structure.json` from
disk as is.

**Fix:** `load_episode_structure` returns a live structure: when the stored
one names segment ids the manifest no longer has
(`episode_structure_is_stale`), it is rebuilt in memory with
`build_episode_structure(refresh=True)` and marked
`rebuilt_in_memory_from_stale`; nothing is persisted (the owner stage stays
the writer). Every direct reader now goes through it. The two callers that
work on the stored document use `live=False`: the hitch's
`align_episode_structure_to_narrative` (it remaps the stored ids and
persists) and the GUI endpoint (it shows what is on disk). `artifact_repairs`
keeps its raw read for the same reason.

Tests: `tests/test_episode_structure_live_after_resegmentation.py` (3).

---

## [173] PRODUCT: the narrative LLM returned ordering constraints that form a cycle, so full_master_ranking could not produce any order and failed (macOS exec_022, step 44)

**Stage / area:** `artifact_repairs.repair_narrative_plan`, new
`break_ordering_constraint_cycles`
**Status:** FIXED

**Seen:** `full_master_ranking` answered partial twice: "The ordering
constraints create a cycle: seg_017 must precede seg_024, seg_024 must precede
seg_015, seg_015 must precede seg_016, and seg_016 must precede seg_017", then
the commit barrier refused its output and the stage failed loudly (entry 163).
The walk continued on the previous selection.

**Cause:** `narrative_arc_plan` produced the cyclic set itself; nothing checked
that ordering constraints admit an order before ranking used them.
(`order_reconcile` flips constraints to match an existing selection, which
cannot create a cycle, but runs only when a selection exists.)

**Fix:** `repair_narrative_plan` breaks every cycle before the plan is
persisted: in each cycle the edge that jumps furthest backwards in tape time is
dropped (tape order is the natural default), else the cycle's last edge; the
dropped edges are recorded in the plan's repair meta. On exec_022's set it
drops seg_024 -> seg_015 and keeps the other four; an acyclic set is untouched.

Tests: `tests/test_ordering_constraint_cycles.py` (4).
`tests/test_narrative_plan_orphan_constraints.py::test_fused_ref_is_remapped_orphan_ref_is_dropped_live_ref_is_kept`
had asserted a repaired plan holding both seg_046 -> seg_030 and
seg_030 -> seg_046 (a two-node cycle created by the fused-id remap); it now
expects the satisfiable set and the recorded cycle break.

---

## [174] PRODUCT: a closing sign-off piece sandwiched between excluded siblings aired as the last clip, because the tail rule started after the last excluded piece (macOS exec_022)

**Stage / area:** `media_ip_cta._closing_outro_tail_ids`
**Status:** FIXED

**Seen:** run 22 reached G-Publish with post-master quality passing, but the
last clip on air was `seg_031i` "You most of the time, Michael." from the
closing CTA parent `seg_031`. Its siblings `seg_031a-h` (sponsor read,
credits) and `seg_031j-k` (more garbled sign-off) were excluded. Layup had
asked to "Remove seg_031i and seg_031k from the locked air order"; the keep
list protected `seg_031i`.

**Cause:** entry 147's rule takes on-air children of a tape-closing CTA
parent that sort after the *last* excluded sibling. Garbled pieces were
excluded on both sides of `seg_031i`, so the last excluded sibling (`k`) came
after it and it was not counted. Its text has no scrap markers, so entry
170's text test did not catch it either.

**Fix:** the tail starts after the *first* excluded sibling: for a parent that
closes the tape, anything on air after its sponsor read begins is sign-off
territory. Story children before the sponsor read still stay. On exec_022 the
closing tail is now `seg_031i`; exec_020 still yields `seg_050`, and exec_017
and exec_019 have none.

**Also on run 22 (expected to clear with entry 173):** the listen critic and
story health each report one `finale_tail` error ("early-chapter segment(s)
after finale block: seg_024, seg_021, seg_022, seg_023"). It is the same
complaint the commit barrier raised when ranking failed on the ordering cycle;
the walk continued on the earlier selection, which carries it.

Tests: `tests/test_tape_tail_scrap_after_closing_cta.py` (2 added; the
sandwiched-child test fails on the unfixed code).

---

## [175] PRODUCT: EDL stage hardening: a segment two chapters claimed stopped the client run at edl, and an audit found eight more EDL blockers whose only heal cannot run under the freeze (client exec_018, step 58)

**Stage / area:** `edl`: `gates.check_edl_narrative_qc`,
`artifact_repairs.repair_edl_narrative_selection`, `edl_narrative_qc`,
`edl_overlap_repair`, `publishability_boundary._check_phantom_vo`
**Status:** FIXED

**Seen (client):** run stopped at 57/72 with `edl` incomplete. Strict EDL
narrative QC: chapters "The challenge of finding rare circulating tumour
cells" [seg_016, seg_015] and "From cell counts to actionable single-cell
analysis" [seg_015, seg_022, seg_018] overlap in final EDL order; "Re-run
full_master_ranking". Earlier attempts also showed a misplaced transition
(seg_022 -> seg_015) and a clone-voice adjacency warning that cleared on
retry. EDL hit `max_invokes_per_identity` (3/3).

**Cause:** `relabel_chapters_contiguous` (entry 152) already turns chapter
membership into a partition of the air order (first claim wins), but only
the narrative audit's repair loop called it. The EDL gate's own repair
(`repair_edl_narrative_selection`) only dropped blanks and filled gaps, and
the re-rank QC asks for cannot run under the hard freeze.

**Fix:** the EDL gate's repair applies the relabel (a label-only write,
`narrative_metadata_align`, allowed under the freeze). A read-only audit of
every blocker at and around `edl` then found these dead ends, fixed in the
same change:

| # | check | why it looped | fix |
|---|---|---|---|
| 1 | chapters overlap / chapter split (`_validate_chapter_continuity`) | relabel not run at edl | relabel in the EDL pre-repair |
| 2 | chapter references a segment the EDL omitted as unplayable | relabel works on the selection, not EDL speech | ignore `omitted_unplayable_segment_ids`, as parity does |
| 3 | framing before impact, framing-covered segment on air, ordering constraint violated, `air_order_integrity` critical | remedy is a re-rank or re-compose; both refused under the hard freeze | recorded as warnings while the order is frozen (still errors before the freeze) |
| 4 | transition no longer between adjacent clips after an overlap merge | renamed onto the survivor in place; cleared only on the next invoke | `edl_overlap_repair` drops transitions that are not adjacent after the rebuild |
| 5 | overlap merge chapter lookup | last chapter won, relabel uses first | first chapter wins |
| 6 | phantom VO at post_edl (WAV exists, no EDL clip) | the builder skips clone-adjacent / out-of-selection lines on purpose | lines in `suppressed_clone_adjacency` / `gap_targets_not_in_selection` are exempt |
| 7 | missing transition clip where the builder seated a layup that was then suppressed or had no WAV | the seam is empty on purpose | exempt when the seam's layup or target is in `missing_vo_files` / suppressed |
| 8 | gap_report duplicate line_id / identical text | counted lines that never air; the compose dedupe is refused under the freeze | count only lines seated in the EDL |

Tests: `tests/test_edl_chapter_overlap_repaired_at_edl.py` (7, including the
client's chapter shape before and after).

---

## [176] PRODUCT: the finale rule judged ranking's order by the pre-ranking narrative chapters, and the EDL disagreed with the selection after an overlap merge depending on the freeze (macOS exec_023)

**Stage / area:** `selection_order_repair.finale_tail_errors` (with
`deterministic_lint`, `story_health`, `stages/selection`),
`edl_overlap_repair.repair_overlapping_source_ranges`
**Status:** FIXED

**Seen (run 23, reached G-Publish, post-master quality passing):**
1. `full_master_ranking` failed once: the commit barrier refused its order
   with "early-chapter segment(s) after finale block: ['seg_049']"; the final
   master still ended on seg_049 and the listen critic and story health each
   flagged the same `finale_tail` (run 22 had the same flag).
2. EDL QC failed once after `EDL overlap merge: seg_048→seg_046`: speech
   order [..., seg_044, seg_045, seg_047, seg_046, seg_049] against the
   selection [..., seg_044, seg_046, seg_045, seg_047, seg_049]. The walk
   recovered by re-running upstream stages and purging a VO take.

**Cause:**
1. Ranking swapped the last two narrative chapters; in its own chapters
   seg_049 closes the final chapter. `finale_tail_errors` read chapter
   membership only from the narrative plan, written before ranking, where
   seg_049 belonged to the chapter that is no longer last.
2. After the merge, the selection either takes the rename (first occurrence
   kept) or the freeze refuses it and the consumed id is retired (survivor in
   place). Entry 167 seated the EDL union at the survivor's slot, which matched
   run 19's branch and not run 23's.

**Fix:**
1. `finale_tail_errors(..., selection_chapters)` uses the ranking's own
   chapters when they give at least two; the lint and the selection stage
   pass the ranking output's chapters, and story health passes the committed
   selection's chapters when that selection is the order being judged. A real
   early-chapter segment after the final chapter is still flagged.
2. After the selection updates, the merge reorders the EDL's speech blocks to
   the committed selection (each block keeps its before/after VO), drops
   transitions that are no longer between neighbours, retimes and rewrites
   the EDL. Only a pure reordering is applied.

Tests: `tests/test_edl_follows_selection_and_finale_chapters.py` (5).

---

## [177] PRODUCT: a blocking "rerun_stage" need for work the walk does anyway failed the stage (macOS exec_024, step 18)

**Stage / area:** `llm_simple.run_llm_stage_simple` (needs handling), new
`is_walk_satisfied_need`
**Status:** FIXED

**Seen:** `LLM stage boundary_topic_resplit incomplete: status=partial
needs=[{'type': 'rerun_stage', 'stage': 'content_brief_reanchor', 'reason':
'Remap topics, claims, and narrative beats from superseded parent IDs to the
revised segment IDs ...', 'blocking': True}]`, then `Failed: Stage
boundary_topic_resplit` (3 errors). On the retry the model returned a
non-blocking need, the stage was accepted, and its own follow-up ran
segment_classification and content_brief_reanchor, the work the first answer
had asked for.

**Cause:** a blocking need fails the stage unless one of three per-stage
demotions applies (speaker_roles, transitions, nugget_layup_compose).
`boundary_topic_resplit` reruns `segment_classification` and
`content_brief_reanchor` as nested stages after it persists, and needs for a
stage later in the pipeline are satisfied by the walk itself (run 22's
gap_framing_compose asked for episode_structure_compose the same way).

**Fix:** `is_walk_satisfied_need`: a `rerun_stage` need is non-blocking when
the requested stage is one the current stage reruns as its own follow-up
(`_SELF_FOLLOWUP_STAGES`) or comes later in `ANALYSIS_ORDER` +
`DELIVERY_ORDER`; the demotion is logged. Needs for upstream stages stay
blocking. The existing rule then accepts a partial with valid artifacts and
only non-blocking needs on the second attempt.

Tests: `tests/test_walk_satisfied_needs.py` (4).

---

## [178] PRODUCT: a reordered air order cut keepers by their air neighbours, not their tape neighbours (macOS exec_025, step 58 edl)

**Stage / area:** `stages/assembly.build_flow1_edl`, `ideal_cuts.resolve_keeper_air_bounds`,
`write_staging.run_wrapped_stage`
**Status:** FIXED

**Seen:** `dispatch refused for incomplete critical edl (max_invokes_per_identity)`
after three EDL attempts that each logged "EDL QC passed" and no error. The
EDL had 14 speech clips, the selection 15: `seg_021` was missing.
`media_ip_cta` and `air_order` then tried to take `seg_021` off the order and
removal authority refused (must-air).

**Cause:** the air order was `... seg_026, seg_025, seg_021, seg_028 ...`
(tape 32:13, 31:39, 27:48, 35:22). The builder passed the previously *aired*
clip's end as the keeper's disjointness floor, and the next *aired* clip's
start as its cap. For `seg_021` (27:48 to 28:36) the floor was `seg_025`'s end
at 32:13, past `seg_021`'s own end; edge refine seated the clip on `seg_026`'s
head words ("Okay. We do the whole genome", 0.94 s), the residual overlap
trim cut it to 280 ms, and it fell out of the EDL. The EDL's
`stage_outputs_present` then saw clips != selection and
`heal_or_refuse_mark` refused `outputs_missing`; `run_edl` raises
`SystemExit` for that refusal, which `run_wrapped_stage` (catching
`Exception`) never logged, so the walk retried to the cap with no reason on
record.

The same mix-up on the cap side hit runs 22 to 24 silently: a clip whose
air-next keeper starts *earlier* on tape got a cap before its own start,
collapsed below the minimum keep and reverted to the full raw slab
(`min_keep_revert`), airing single clips of 15.5, 10.6 and 11.0 minutes
against the 3-minute keeper budget.

**Fix:**
- `build_flow1_edl` computes both bounds from tape neighbours among on-air
  segments: the floor is the latest end of any tape-earlier on-air clip
  (its resolved end once emitted, its raw end before), the cap the earliest
  start of any tape-later on-air clip. In tape order these equal the old
  air-neighbour values.
- `resolve_keeper_air_bounds` ignores a "next" keeper that starts at or
  before this keeper's start and a "previous" keeper that ends at or after
  this keeper's end (defence for the hitch callers).
- `run_wrapped_stage` logs a stage's `SystemExit` refusal with its reason
  before re-raising.

Replayed on a copy of exec_025: the EDL completes and marks done, `seg_021`
airs its own 27:48 to 28:36, no clip exceeds 3 minutes (`seg_028` was a
14.4 minute revert; it is now a 3.0 minute budget cut), timeline 32.6 to 22.3
minutes.

Tests: `tests/test_reorder_keeps_tape_spans.py` (3, on real exec_025 words;
all three fail on the old code).

---

## [179] PRODUCT: family sweep of 178: no producer reads an air neighbour as a tape neighbour, and no EDL write drops a must-air keep

**Stage / area:** `media_ip_cta`, `air_order.write_live_edl`, `junction_snip_qa`,
`chapter_close_hitch`, `ideal_cuts`, `gap_framing`, `opening_adjacency_repair`,
`thought_complete_recut`
**Status:** FIXED

**Why 178 was not the whole story:** 175 and 176 hardened the EDL against
chapter overlaps and merge reorders. exec_025's failure had a different root
(air order treated as tape order) and a second, independent defect that turned
it into a dead end: the never-touch clamp in `write_live_edl` deleted every
speech clip under 400 ms, including one it never clamped (`seg_021`, 280 ms
after the overlap trim), labelled it `dropped_unplayable_never_touch`, and
asked the selection to omit it. Removal authority refused (must-air), so the
EDL and the selection could never agree. A sweep then found the same
air-vs-tape shape in six more producers.

**Fixes:**
- *EDL never loses a must-air keep.*
  - `clamp_edl_speech_away_from_never_touch` never drops a protected keep.
  - An unprotected short clip it did not clamp is still dropped (the selection
    follows), but is labelled `dropped_unplayable_short`.
  - New `air_order.restore_protected_speech_clips` runs inside
    `write_live_edl`, the one writer every EDL producer passes. A protected
    keep that is missing, unplayable (< 400 ms) or seated on none of its own
    tape is reseated from its manifest span, clamped off never-touch tape and
    off other on-air clips, after the nearest earlier keep. It logs a warning.
- *`junction_snip_qa._next_speech_source_start`:* returns the earliest on-air
  speech start after the clip on tape, not the next clip in air order.
  Before, the cap could fall before the clip's own start (no repair possible),
  or an extend could run into a tape-later clip that airs elsewhere.
- *`junction_snip_qa._merge_candidate_for_clip`:* a reordered air neighbour
  read as gap 0, and the fused union aired the unselected tape between them.
  Now a neighbour must also be the tape neighbour in the same direction, and
  a union that covers another on-air clip is refused.
- *`chapter_close_hitch._next_chapter_start_ms`:* the next chapter in plan
  order can start earlier on tape. Only starts after the keeper bound it.
- *`ideal_cuts.snap_ideal_cuts` (new `resolve_cut_overlaps`):* compared
  against the last kept cut's end, so any should_keep or optional cut that
  started before the last must_keep ended was dropped, however far away. Now
  a cut is dropped only on a real interval overlap.
- *`gap_framing.drop_contiguous_light_bridge_lines`:* a target before its air
  predecessor on tape (a short predecessor makes the gap read near 0) is a
  backward jump. The bridge VO is kept.
- *`opening_adjacency_repair.drop_post_coda_reverse_jump_from_selection`:* a
  late-tape clip is the coda only when it airs in the closing chapter.
  Mid-arc late tape no longer deletes the early chapter after it.
- *`thought_complete_recut.apply_thought_complete_to_clips`:* the extension
  stops at the start of a tape-later clip that aired earlier. Before, that
  tape played twice.

Checked and safe (no change): `edl_overlap_repair`, `segment_fuse`, `edl_qc`,
`edl_narrative_qc`, `cut_edge_refine`, `sound_design`, `diarization_suspicion`,
`air_order_integrity`, `air_script`, `seam_autopsy`, `reorder_bridges`,
`listen_quality`, `listenability_guards`, `information_packages`,
`talking_points_authority`, `gap_vo_prior_context`, `mix_junction_seat`.
`selection_order_repair` orders by tape but never cuts or drops audio.

Replayed on a copy of exec_025 with 178's bound fix reverted: the net reseats
`seg_021` on its own tape and the EDL completes.

Tests: `tests/test_edl_never_drops_must_air.py` (7),
`tests/test_reorder_family_sweep.py` (9); each fails on the old code.

---

## [180] PRODUCT: the undecodable last 5 s of tape aired as the episode's closing line (macOS exec_026, 72/72, 0 errors)

**Stage / area:** `hard_keep.hard_keep_segment_ids`, `media_ip_cta` tail and
never-touch checks, new `nle_state.segments_by_id_including_excluded`
**Status:** FIXED

**Seen:** exec_026 finished with 0 errors, but the master's last clip was
`seg_033` (59:22 to 59:27), whose transcript is an STT hallucination after the
closing sponsor read: "You most of the time, Michael. You are listening to
usHS\ufffd bone and cut -edge on depression ... ouribaigilaw .com ...", 86
words with zero-length timings over 5 s. ISSUES 170 was meant to catch exactly
this scrap.

**Causes (two, both needed):**
1. The tail check `_tape_tail_scrap_ids` finds the closing sponsor parent's
   start in `_segments_by_id`, the live NLE view. That view drops every
   excluded row, and the sponsor read (`seg_032`, `seg_032a-f`) is excluded by
   design, so there was no closing start and nothing was ever a tail scrap.
   170's tests passed because their fixtures kept the parent row.
2. `low_conf_island_scan` hard-included `seg_033` in its top decile (the most
   STT-uncertain speech), so `hard_keep_segment_ids` protected it from any
   omit.

**Family audit (51 live-view call sites):** the same missing-row failure was
in `never_touch_source_intervals` (excluded CTA children's tape never became
never-touch, so a speech clip could extend into sponsor tape), the tail
check's candidate loop, `_closing_outro_tail_ids` (tape end understated
without excluded closing rows), `_texts_for` and the `never_touch_texts`
fallback (empty CTA wording), and the latent `_readmit_cta_story_children`
(an NLE-excluded child read as start 0 and readmitted as the cold open; off by
config). Sites that rely on "missing row = empty text" stay on the live view.

**Fix:**
- `nle_state.segments_by_id_including_excluded` (and
  `apply_segments_with_nle(include_excluded=True)`) returns every row with NLE
  bounds, NLE-only children included, excluded ones flagged.
  `media_ip_cta._all_segments_by_id` merges it over the manifest.
- The tail check, closing-tail tape end, never-touch intervals and CTA texts
  read excluded rows from it.
- `hard_keep._drop_tape_tail_scrap_keeps`: no keep source may protect a scrap
  that starts after the closing sponsor read.
- The CTA story readmit skips children a later pass excluded.

On exec_026's artifacts: `seg_033` is no longer a keep, the closing-tail check
returns it, and the sponsor read's children are never-touch intervals.

Tests: `tests/test_tape_tail_scrap_after_excluded_outro.py` (5).

Not fixed here (product decision, logged): 6 of 7 reorder bridges were
suppressed by the clone-adjacency rule (cloned host voice next to the real
host), so story_health reports 11 missing reorder bridges; listen_delight
passes (0.957).

## [181] PRODUCT: the first ranking commit on a new source was refused with hard_keep_missing_from_order:seg_007 (maintainer's exec_024, granola 47-minute source, stalled at 40 of 72)

**Stage / area:** `selection_constraints.lattice_lint_codes`,
`hard_keep.hard_keep_segment_ids`, `hard_keep.enforce_hard_keeps`,
`framing_coverage_guard.inject_ranking_lattice_keeps`,
`deterministic_lint._lint_full_master_ranking`,
`artifact_sanitize.selection._hard_keep_ids`
**Status:** FIXED

**Seen:** exec_024 cleared analysis and reached `full_master_ranking`. The
LLM produced an order; the seal refused it with
`selection_lattice_seal_refused: hard_keep_missing_from_order:seg_007`, so
`master/selection.json` never landed. The orchestrator dispatched
`selection_order_sanitize`, which refused for `missing_hard_input` (the same
missing file); the second-wind retry hit the identical seal failure; the
attempt memo refused a third dispatch and delivery stopped incomplete.

**Cause:** two judges of the same proposed selection read different
documents. `enforce_hard_keeps` reads the proposal: a keep the finalize pass
excluded with a typed omit (opening constitution `opening_slot_overflow` /
`opening_skipped_duplicate` / `late_intro_reset`, the finale rule's
`finale_tail_leftover`, any `cta_*`) is exempt and not restored.
`lattice_lint_codes` called `hard_keep_segment_ids(ctx)` with no selection,
and that list drops typed omits by reading `master/selection.json` from disk.
On a first commit the file does not exist, so the omit was invisible, seg_007
stayed a demand, and every commit was refused. The same disk read sat under
`inject_ranking_lattice_keeps` (re-admitting constitution-excluded keeps the
finalize pass then dropped again), the ranking deterministic lint, and the
sanitizer's keep list.

**Family (every judge of a proposed selection that read the keep list from
disk):** the seal lint, the ranking injector, `_lint_full_master_ranking`,
`artifact_sanitize.selection` (shape and family-cap checks),
`air_order_integrity.lint_hard_keep_family_errors`, and the undersize
pull-back in `selection_membership`. Consumers that judge the committed file
(junction QA, EDL speech list, CTA omit refusal, air script omits) stay on
disk.

**Fix:**
- `hard_keep_segment_ids(ctx, selection=...)`: the keep list is judged
  against the selection in hand (the proposal when given, else disk). One
  exemption list, `hard_keep.keep_exempt_ids`, serves the restore, the lint
  and the injector: playability's unplayable and CTA classes, `cta_*`, and
  the framing VO cover. The CTA-parent transfer, the lattice and editorial
  rulings and the tape-tail scrap check read the same document.
- Every judge above passes the document it is judging.

**Verified on the granola source (macOS exec_028, 2026-10-07):** 72 of 72,
seg_007 on air, post-master quality pass with no failed checks, listener
scorecard 0.947, 34.6-minute master from the 47-minute tape, longest clip
under 3 minutes. The run's two error lines are from a mid-run process
restart (the orchestrator had the old `hard_keep` module in memory while
the freshly imported ranking lint passed the new keyword); not a code
fault. `master/story_health.json` reads `fail` with 8 missing reorder
bridges because it is a ranking-time snapshot written before the
transitions stage minted the bridges; `master/bridge_completeness.json`
(the authority) is complete. Left as is: the snapshot feeds only the GUI
and listen quality's finale / ordering codes.

Tests: `tests/test_hard_keep_judged_against_proposal.py` (7): first commit
with a constitution omit seals; first commit whose finalize omits an early
keep seals; the committed file neither overrides nor excuses the proposal;
an editorial aside is still restored; the injector does not re-admit a typed
omit but still injects an aside in tape order; the deterministic lint reads
the proposal.

Fixtures updated for upstream `872361153` (they pass on `d55f0c6c1`, fail
on upstream main alone):
`tests/test_hr2_off_bus_selection.py` pass-A omit tests (a 96-second tape
sat wholly inside the new opening window and the constitution excluded half
of it on write; one beat a minute now), and
`tests/test_junction_snip_qa.py::test_jsq_b1_advisory_mode_still_blocks_critical_incomplete`
(ENDD-2 makes the commitment seat hard; the seat is stubbed).

Not fixed here: 26 suite failures pre-existing on upstream main
`872361153` (all pass on `d55f0c6c1`), by file:
test_cross_platform_parity (1), test_delivery_guardrails (2),
test_g_publish_review (2, `master/transcript.vtt missing`),
test_hau_assembly_freshness (2), test_hau_footgun_harden (1),
test_hitch_thought_continuity (1), test_homunculus (1),
test_hx1_mix_epoch_unsealed (5, `music_incomplete` no longer reported),
test_i11_mix_mark_done_seats_mtime (1),
test_i25_junction_recut_before_first_mix (2), test_i4_hollow_hosted_vo_zero
(1), test_junction_precedes_gate_matrix (2),
test_layup_selection_commit_ownership (1), test_quality_polish_hardening
(2), test_r_workflow_residual (1). Mostly the ENDD mix / junction / music
epoch ordering rework; left for the maintainer unless a run hits them.

## [182] PRODUCT: one name attribution in one transition line failed the transitions stage (granola exec_029, 47-minute source)

**Stage / area:** `spoken_copy_guard.guard_spoken_copy`,
`spoken_copy_guard._strip_name_attribution_clause`
**Status:** FIXED

**Seen:** exec_029 (the zero-error confirmation run after ISSUES 181)
failed at `transitions` with
`transition_plan[seg_044->seg_045] blocked by spoken_copy_guard:
spoken_name_attribution, no_grounded_fallback`. The attempt memo refused the
retry, the walk stopped with "Delivery incomplete", and the conductor's
direct dispatch happened to get a clean second roll from the model. The
master shipped, but with two error lines and a paid retry.

**Cause:** the model wrote the bridge as "<Name> explains that ...". The
guard rightly flags that register (`spoken_name_attribution`, a wording
fault that sounds like narration about the tape). Its only cure was a
grounded hinge built from topic evidence; the pair had no topics, so the
verdict was `block`, and a required transition line blocks the stage. ISSUES
128 already cures a repeated sentence before blocking, and the lay-up path
scrubs this exact register (`scrub_spoken_register`, nug_016); the guard's
own decision never tried the scrub. Every caller of
`assert_guarded_spoken_copy` (transitions, seam glue, synthetic framing,
transition VO, speech-to-speech) shared the gap.

**Fix:**
- `guard_spoken_copy`: when the violations include a register slip (name or
  role attribution, gendered pronoun), scrub it and keep the model's own
  grounded copy as the fallback when the remainder is still a line (five
  words or more) and passes the guard. A line that is nothing but an
  attribution still blocks.
- `_strip_name_attribution_clause` now also cures an attribution on a later
  sentence ("... came first. Sam adds that ..."), a parenthetical one
  ("..., as Sam explains, ...") and "let's hear Sam on ...", and drops the
  "that / how / why" connective so the remainder is a statement.

Tests: `tests/test_spoken_copy_guard_register_cure.py` (7).

exec_029 itself finished after the conductor's retry: 72 of 72, post-master
quality pass, scorecard 0.925, bridge completeness complete, 37.4-minute
master, longest clip under 3 minutes. exec_030 is the fresh run on this fix.

## [183] PRODUCT: a gap-framing shard failed twice over a rerun need naming a stage that does not exist (granola exec_030, step 32)

**Stage / area:** `llm_simple.is_walk_satisfied_need`, new
`llm_simple.resolve_need_stage`
**Status:** FIXED

**Seen:** `LLM stage gap_framing_compose incomplete: status=partial
needs=[{'type': 'rerun_stage', 'stage': 'segment_ranking', 'reason':
'seg_045 remains in the ordered sequence despite a high-severity incomplete
interviewer turn ... Remove or recut it before final assembly.', 'blocking':
True}]`. Shard 3 of 3 answered the same way on both attempts, was logged as
failed, and the stage completed on the other shards' lines (4 lines), so the
run went on with one error line and without that shard's framing.

**Cause:** ISSUES 177 demotes a blocking `rerun_stage` need when the named
stage comes later in the walk. The model named the stage loosely
("segment_ranking"); the exact-id lookup found nothing, the need stayed
blocking, and the shard failed over a request that `full_master_ranking`
(eight stages later) exists to judge.

**Fix:** `resolve_need_stage` maps a loose name to the stage id (exact id,
then an alias table, then the one stage whose last token matches). The
later-stage rule runs on the resolved id. A name that resolves to nothing is
not a stage the walk can run, so failing the current stage cannot satisfy
it either; such a need is demoted and logged with its resolution. Upstream
stages stay blocking.

Tests: `tests/test_walk_satisfied_needs.py` (+4, 8 total).

exec_030 itself finished: 72 of 72, post-master quality pass, scorecard
0.935, bridge completeness complete, 32.6-minute master, longest clip under
3 minutes, and that shard failure was its only error line.

**Clean run on 181, 182 and 183 together (macOS exec_031, 2026-10-07):**
72 of 72, zero error lines in the console and zero error rows in the run
log, post-master quality pass with no failed checks, scorecard 0.938,
bridge completeness complete, seg_007 on air, 35.2-minute master from the
47-minute granola tape, longest clip under 3 minutes. The run was resumed
once on the same id after the disk filled during the mix (not a code
fault); the resume re-verified analysis and continued from the mix.

---

## [184] PRODUCT: a required VO with a WAV was dropped from the EDL for clone-voice adjacency and the mix refused forever (maintainer's exec_025, ~60 of 72)

**Stage / area:** `stages/assembly.build_flow1_edl`,
`edl_narrative_qc._validate_clone_voice_adjacency`,
`listenability_guards` re-seat, `publishability_boundary._check_unseated_required_vo`,
`heal_routing.PLAYBOOK_REGISTRY`, `recovery_controller`
**Status:** FIXED

**Seen (maintainer's report):** `PublishabilityBlocked` at pre_mix:
`omit_collateral_vo_strip / unseated_required_vo` for `vo_question_seg_027`.
Three mix attempts, `max_mix_cycles`, no `master/assembly.wav`, "Delivery
incomplete ... resume=mix". The gap report kept the line as a required
synthesize question, adjudication said air, the WAV existed, and the lay-up's
earlier clone-adjacency omit had been deactivated by
`restore_nugget_layup_line`.

**Cause:** the EDL builder asked the clone-adjacency verifier whether the
cloned host voice would sit next to the same native speaker; the verifier said
yes and the builder dropped the line (and several transitions), recording it
only under `edl.warnings.suppressed_clone_adjacency`. Publishability then
found a required line with no seat and no active omit. The heal route named
`exempt_rebuild_edl`, an action no code implements, so resuming `edl` reached
the same suppress every time; recovery had no branch for the class
(`unhandled_class`) and unattended escalation retried mix. Two paperwork bugs
in the checker: it read `line_id` from ledger rows keyed by `subject_id`, and
called `line_is_omitted(line)` instead of `line_is_omitted(ledger, line_id)`
(a swallowed TypeError), so even an active omit could not compensate.

**Fix (the maintainer's minimal reliability stack):**
1. EDL: a line the gap report airs, with a WAV, is seated. Clone adjacency is
   advisory everywhere after planning: the builder records the seam in
   `edl.warnings.clone_adjacency_advisory` (VO and transitions), EDL narrative
   QC logs it as a warning instead of a blocking error, and the listenability
   re-seat path seats at the planned target instead of skipping.
   Planning-time preferences stay: the lay-up and gap framing may still
   retarget or typed-skip a line, with an omit-ledger row.
2. Publishability keeps `unseated_required_vo` as the guardrail; it now reads
   `subject_id` and calls `line_is_omitted(ledger, line_id)`.
3. Heal: `omit_collateral_vo_strip` resumes `vo_synthesize` (a missing WAV is
   now the only way to be unseated) and recovery has an explicit branch for it.

Tests: `tests/test_required_vo_seats_despite_clone_adjacency.py` (7);
`tests/test_clone_voice_adjacency.py` and `tests/test_edl_narrative_qc.py`
updated from "suppressed" to "seated + advisory".

## [185] STRUCTURAL: hard blockers become warnings or simple rules (maintainer's request after exec_025)

**Status:** DONE (code + tests); fresh-run verification pending operator go-ahead.

**Why:** the maintainer asked to cut features and simplify: a quality
judgement must not refuse a stage or the mix. Four read-only audits mapped
every blocker (publishability/PMQ/guardrails, lints/QC/sanitize, loud
failures/guards/LLM runner, and which ones fired across ISSUES 1-184 and
the run logs). Each was classified QUALITY (judgement: becomes a warning) or
STRUCTURAL (missing/corrupt/empty/unplayable: keeps blocking, with a heal
whose resume stage can actually produce the missing thing). ISSUES 184 was
the template.

**Central switches**
- Lints: stage acceptance failed on *any* lint string, so lints the runner
  labelled "non-blocking" still blocked through the commit barrier.
  `deterministic_lint.split_lint_errors`: only `STRUCTURAL_LINT_MARKERS`
  (empty / missing / not in manifest / unknown asset / missing line_id or
  voice / bad boundary rows) block; every other lint is logged. A lint that
  crashes warns (exec_028 lost ranking to a TypeError inside the lint).
- LLM runner: on the second attempt, valid artifacts are accepted whatever the
  model labels its needs; the needs are logged (172, 177, 183 shape).
- Config: `block_ranking_on_critical` and `block_publish_on_critical` false;
  lay-up `block_on_open_must_keep` / `block_on_open_high_salience` false.
  New off-by-default switches `analysis.edl_narrative_audit_blocking` and
  `analysis.selection_seal_fail_closed` re-enable the old refusals.
- Unattended runs: sanitize refusals route through their resume pins instead
  of stamping needs_operator (only authority-undo thrash still pauses).

**Now warnings (logged, recorded, never refuse)**
- Ranking: unresolved air-order criticals after repair (incl. the
  unconditional late_opening_cluster raise), story_health fail, selection
  seal findings (hard keep not placed, primary impact), framing coverage
  guard, sanitizer integrity/lattice codes.
- EDL narrative QC: coverage lost, chapter continuity, ordering, framing
  before impact / covered-on-air, VO after an incomplete thought, duplicate
  copy / sentence collisions, one synthetic per seam, LLM audit verdict.
  Structural QC (selection parity, transitions adjacency, orphan/duplicate
  line ids, voice identity, volley split) still blocks.
- EDL builder: the LLM narrative audit's blocking issues, a repeated spoken
  sentence (both lines seated), orientation mission labels / thin wording /
  late position (count and audibility still block), bridge completeness
  (all five refusal sites).
- Spoken copy: a transition the guard cannot cure is dropped and the seam
  plays without spoken glue (transitions persist, seam-glue mint, synthetic
  framing plan); an uncovered reorder seam is not a plan error; adjudication
  only stops on empty text or placeholder markup; at synthesis time the
  writer's accepted copy is voiced as written unless it would speak an
  internal id, meta or scaffolding.
- Junction / ship: residual verdicts, residuals after the run budget and an
  unavailable feel audit (LLM outage) are advisory in junction, mix,
  publishability, ship-path readiness and the mix resume picker.
- Publishability: `ADVISORY_ERROR_CLASSES` (incomplete cut, claim inventory
  paperwork, VO audibility drift, stale staging) never raise; unseated
  required VO, order drift, zeroed speech and orientation count do.
- PMQ: structural set is master integrity only (exists, seam commitment,
  script-hash agreement), each with a heal route (mix, junction_snip_qa,
  vo_synthesize); speakable re-lint, omit-ledger paperwork, duration floor
  and junction residuals are advisory.
- Lay-up / air contract: the hosted-VO count floor is a target at every
  count; gap-report scaffolding on a required line is an advisory note.

**Now simple rules**
- Transition synthesis records pairs the guard refused
  (`mastering/vo_synthesize.json` `unspeakable_pairs`, keyed by script hash);
  every missing-WAV gate skips them until their text changes, so mix and
  vo_synthesize stop bouncing.
- A lay-up row a row-level QC finding names (canned air, thin, ungrounded,
  CTA wording, restates target) becomes a justified typed skip.

**Kept as is (structural, with working heals):** missing/corrupt artifacts,
ids not in manifest, unseated required VO, order drift, zeroed speech,
boundary quality gate, ownership denials, schema/env failures, mix epoch
ordering, seed-order prerequisites, speaker preflight (ISSUES 103 already
softened it).

**Not changed, noted for later:** the retry-limit guards (attempt memo,
identical-failure x3) still amplify a single refusal; with the blockers above
gone they should rarely see one. The dead `pre_finalize` publishability
checkpoint is left in place.

Tests: `tests/test_blockers_become_warnings_or_rules.py` (11); about 40
existing tests updated from "refuses" to "warns", and opt-in paths kept
tested through the new switches.

**Verified (macOS exec_033, granola 47-minute source, fresh, 6ef1b4791):**
72 of 72, zero error lines in the console and zero error rows in the run
log, post-master quality pass (publish allowed), scorecard 0.951, 37.9-minute
master, longest clip under 3 minutes, 3 VO pickups and 6 transitions seated.
Seven advisories were logged instead of blocking, three of which would have
stopped earlier code: 4 clone-voice adjacency seams (exec_025's stall shape),
a `spoken_vo_speakable` PMQ miss (would have refused publish), and the music
palette density miss. The listenability note "bed_coverage 0.000" says the
episode has no music bed under speech; that is a sound-design question for
the maintainer, not a failure.

---

## [186] PRODUCT: the cut-location rework (5010ba067) left six ways to stop master.wav, and its edge repair cut speech mid-sentence (maintainer's report, review of upstream 5010ba067 + 8e0b47c04)

**Stage / area:** `artifact_sanitize/one_writer`, `order_hash`, `air_order.mix_outputs_seated`,
`homunculus/agenda.stage_outputs_present`, `junction_snip_qa`, `edl_overlap_repair`,
`artifact_completeness.align_manifest_ids_to_boundaries`, `write_staging.approve_stage_writes`,
`artifact_ownership` remap gate, `stages/understanding` (ideal_cuts span coverage),
`boundary_edge_score.repair_low_confidence_edges`, `boundary_collate._finished_sentence_gap`,
stage contract `podcast_publish`
**Status:** FIXED (blockers); quality items OPEN for the maintainer, listed below

**Seen (maintainer's report):** segments were cut early, sometimes mid-sentence.
The cut-location rework in 5010ba067 fixed some of that, but every pass found
another downstream break. A review was requested: is there anything that would
prevent master.wav? Full suite on upstream 5010ba067: 27 failed; 22 predate it
(ISSUES 181 family), 4 came from 5010ba067 and 1 from 8e0b47c04.

**Blockers found and fixed:**
1. *connector_fuse_pass failed on every normal run.* 5010ba067 added 11 paths
   to `SEGMENT_ID_REMAP_PATHS`; the remap gate runs before the operational-row
   early return, so the fuse pass's ordinary write of
   `analysis/high_value_island_clusters.json` (every fuse round, even with zero
   islands) was refused `mutation_class_required:segment_id_remap`, uncaught.
   Fix: the remap class is required only when the writing stage is not a
   producer of the path, and connector_fuse_pass is a declared producer of the
   clusters file (low_conf_island_scan stays the owner). This is also why
   `test_declared_producer_is_a_permitted_writer_of_the_input_path` failed.
2. *Seat-freeze reason suffix skipped End-A writes.* `commit_transitions_doc`
   and `commit_sound_design_plan_doc` passed `reason="<reason>:<mutation_class>"`;
   the End-A allowlist and the ISSUES 116 carve-out match exactly, so under a
   freeze every End-A heal and the sound_design_plan stage's own write were
   skipped while returning success (the ISSUES 116 loop again). Fix: pass the
   reason unchanged; admit `mutation_class == "segment_id_remap"` separately.
3. *An EDL-omitted clip made master_finalize refuse forever.* The drift check
   now discounts `omitted_unplayable_segment_ids` and returns "stamp" instead
   of excluding the id from the selection, but `order_hashes_match`, both
   seated checks and the junction re-align still compared the full selection.
   `verify_commitment` never reached "committed", so `assert_consumer` stopped
   junction_snip_qa / master_finalize and `stage_input_checks` refused
   master_finalize; edl and mix were re-dispatched to the cap. The new
   `trim_residual_source_overlaps` (drops clips under 400 ms) makes omissions
   routine. Fix: one helper, `order_hash.seatable_selection_ids`, used by every
   selection-versus-EDL comparison (drift, lock copy, selection-leads,
   `order_hashes_match`, both seated checks, the junction re-align, the
   overlap-repair speech reorder).
4. *Every flush deleted CTA/NLE split children from the manifest.*
   `approve_stage_writes` ran `align_manifest_ids_to_boundaries` +
   `drop_retired_segment_refs` after every stage. Split children live only in
   the manifest and sit inside the parent span, so they were dropped (and
   `repair_manifest_segments`' overlap trim drops any row inside its parent);
   ranking's selection then named ids not in the manifest and the flush was
   refused on every pass, while chapters lost the child ids. Fix: align only
   on a flush that wrote `segments/boundaries.json`; the align keeps split
   children whose parent is still a boundary row and adds them after the
   repair.
5. *Span coverage could raise.* Redistribution no longer falls back to a
   time-only window when a probe point lands in a short sentence, so the
   `ideal_cuts_propose span coverage < 0.45` RuntimeError became reachable on
   tapes of 15 minutes or more. Coverage is a quality judgement (ISSUES 185):
   now a warning.
6. *podcast_publish contract drift (8e0b47c04).* The YAML was hand-edited;
   regenerated from `tools/bootstrap_stage_contracts.py` (the verify script
   would have reverted it anyway). Ownership already lets both stages write the
   four publish files.

**Cut quality fixed (it worked against the rework's own goal):**
7. *Edge repair moved boundaries mid-sentence and lost speech.* The new cap
   `overall = min(overall, 0.4)` for any edge below a 0.95 legal score grades
   most edges `reject` (any open on "And/So/But"), so repair runs on nearly
   all of them and accepts plain word-edge candidates; the nudge moves one
   side only. On the 47-minute granola transcript: 135 rejects (baseline 3),
   81 nudges, 148 words of real speech in no segment, segments opening on
   "you for coming and joining us". Fix: a nudge may not shrink a row over
   non-filler words. Same transcript after the fix: 26 nudges, 11 words lost
   (all "Um"; baseline lost 33), stable by pass 3.
8. *`_finished_sentence_gap` read the wrong next words.* The next side took
   the last 16 words of a 30 s window instead of the first 16 after the cut.

**OPEN for the maintainer (quality, not blocking; verified by the review):**
- Config overrides the new code defaults: `analysis.ideal_cuts.semantic_edge_buffer_ms`
  is 5000 and `max_cut_ms` 180000 in `config/app.defaults.json`, the same for
  `chapter_close_hitch.max_cut_ms`, and `junction_snip_qa.phrase_extend_max_ms` /
  `thought_complete_max_ms` stay 24000. config-keys.md documents 30000 / unset,
  so the new windows are not in effect.
- The 0.4 edge cap still grades about 90% of edges `reject` (GUI review queue
  of ~255 items on granola); it no longer moves speech, but the grade is noise.
- `_finished_sentence_gap` returns False for abutting rows (gap 0), so
  same-speaker concept splits are still merged by `merge_same_speaker_boundary`.
- Backchannel split triggers on "right/sure/okay" anywhere in a sentence, with
  no speaker or pause check.
- Spine windows and topic hints call the hinge predicates over all words per
  word: about 50 s each on 9,335 words, quadratic in tape length.
- `investigate_forward_cut` and `is_legal_conceptual_hinge` disagree (a cut the
  placer accepts, the checkers call a hang).
- `resolve_keeper_air_bounds`: on a reorder, each keeper is capped by the
  other's raw (not resolved) bound, so tape plays twice; overlong keepers are
  no longer trimmed (a 288 s slab airs whole).
- `fold_following_segments_into_chapters` folds unowned manifest rows (CTA,
  omitted) into chapters, appends them out of tape order, and drops emptied
  chapters without remapping their ids.
- `resolve_cut_overlaps` keeps pieces down to 80 ms with the parent's priority
  (a one-word must_keep tail).
- Hitch layup adopt failure is rewritten to ok=True, so its heal route never
  fires and the block moves to `edl`.
- Host-frame protection now overrides `diarization_yes_same` /
  `micro_other_absorb` (the comment below it still says yes_same is the
  exception); `test_micro_keeper_fuse` fails on this. Mis-diarized "mm-hm"
  islands now keep guest sentences split.
- `kept_source_gap` can put 8 to 30 s of silence into the master between
  chapters, which junction_snip_qa then flags as dead air.
- Some tests were loosened to pass (`start >= -18000`, a cut ending on "and").

Tests: `tests/test_omitted_clip_seated.py` (2), `tests/test_edge_nudge_keeps_speech.py` (3),
`tests/test_halt_safe_id_align.py` (+1, split children); the freeze, contract
and ownership tests that 5010ba067 broke pass again.

# Planned: prune the job-API driver (phase 2 of entry 79)

Sized on 2026-09-30 after the engine proofs (exec_062 full-auto, exec_064
partially-accelerated, both 72 of 72 through the GUI endpoints):

- `tools/full_auto_driver.py` is 15,020 lines; `tools/full_auto_keepalive_loop.py`
  and the daemon paths in `tools/full_auto_daemon_launch.py` go with it
  (`MUX_LEGACY_DRIVER=1` is the only remaining entry point).
- Seven source modules import from the driver (`cli`, `delivery_guardrails`,
  `forensics_error_ledger`, `forensics_minor_fixes`, `full_auto_launch`,
  `heal_routing`, `identical_failures`): each import is a heal or forensics
  helper that must move into `src/interview_mux/` before the file can go.
- 56 test files reference the driver. Most exercise heal branches that the
  engine now reaches through `homunculus.agenda`; each needs retargeting or
  deleting, not blanket removal.
- `scripts/run.sh` and `tools/catalog_unattended_breakpoints.py`,
  `tools/subtraction_predict.py` name the driver in comments and env plumbing.

Order: (1) move the seven imported helpers into the package with their
tests; (2) delete the keepalive and the daemon spawn path, keeping
`ensure_e2e` as the engine launcher; (3) delete the driver and retarget its
tests; (4) drop the driver-only execute modes from `web/server.py` and the
`_STAGE_REUSE_POLICY` rows that exist for it; (5) rerun the suite and reset
the baseline. Do this in its own branch with the maintainer's go-ahead: it
touches the GUI's execute modes.

**Status:** OPEN, sized, not started.

# Planned: exhaustive pre-flight suite

Goal requested: a suite such that **if it passes, an execution works**.

Being straight about the ceiling: no test suite can fully guarantee that for
this pipeline. Stages 40-60 call a hosted LLM, so outputs are non-deterministic
and the failure modes include schema drift, refusals, truncation, rate limits
and network faults. A suite can guarantee the *deterministic* substrate and it
can guarantee the pipeline **degrades predictably** instead of crashing, which
is what "self propelled" actually needs. It cannot prove a given model response
will be well-formed.

What it can cover, and what is worth building:

1. **Contract round-trips** for every artifact the 40-60 band writes: write,
   sanitize, re-read, re-sanitize. Bug [2] is exactly a round-trip failure, and
   the existing tests missed it because none of them sanitized twice.
2. **Adversarial artifact fuzzing** into each stage's validator: orphan ids,
   duplicate ids, empty orders, letter-split families, out-of-range spans,
   unicode, null fields. Assert a *named refusal*, never a `NameError`,
   `KeyError` or `TypeError`. Bug [1] is the class this catches.
3. **Every heal and except branch executed at least once.** Bugs [1] and [2]
   both lived in paths no test entered. Coverage over `tools/full_auto_driver.py`
   heal branches and the `artifact_sanitize` refuse paths is the highest-value
   target in the repo.
4. **Stage-order invariants**: the 72-stage order, no stage reading an artifact
   an earlier stage has not written, resume-from-stage valid at every index.
5. **LLM boundary with a fake provider**: replay recorded responses plus
   deliberately broken ones (truncated JSON, wrong schema, refusal text, empty)
   and assert each stage escalates or degrades rather than raising.
6. **A no-key smoke run** of the full 72 stages against a stub provider and a
   short synthetic WAV, asserting it reaches `master_finalize`.

Item 6 is the closest thing to the actual request and the one real gate: a full
pipeline traversal that needs no API key and no real audio, so it can run in CI
on every change.

**Status:** OPEN, to start after the bug pass.

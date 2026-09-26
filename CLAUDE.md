# CLAUDE.md

Context for Claude Code sessions in this checkout. Repo-wide agent guidance
lives in [AGENTS.md](AGENTS.md); this file covers only what is specific to
**this machine and this working arrangement**.

## Why this checkout exists

This is a **debugging checkout**, not a production one. Swapnil runs the app
here to find bugs, then reports fixes back to Nicket, who owns the upstream
repo (`NicketUttarwar/interview_helper_mux`).

- Log every bug and fix in **[ISSUES.md](ISSUES.md)** so it can be handed over.
- Fork: `swapnilgarg7/interview_helper_mux`. Remote `fork`; `origin` is Nicket's.
- Work on branches off the fork and open PRs against `swapnilgarg7:main`.
  **Do not open PRs against `origin` (Nicket's repo) without asking**: it
  notifies him and is visible on his repo.

## This machine

Windows 11, NVIDIA GTX 1660 SUPER (6 GB VRAM, sm_75, no bf16), 16 GB RAM.
Upstream targets macOS Apple Silicon, so the MLX stacks are replaced with CUDA
equivalents behind the same contracts. Details and rationale:
**[docs/cross-cutting/windows-cuda-setup.md](docs/cross-cutting/windows-cuda-setup.md)**.

- **Run scripts from Git Bash**, not PowerShell. They are bash.
- **Heavy stacks live at `C:\mux-local`**, not under `ASSETS/`. Windows caps
  paths at 260 chars and `pip install torch` fails outright inside this repo's
  OneDrive path. It also keeps ~35 GB of weights out of cloud sync.
- **`config/app.local.json` is gitignored and machine-specific.** It holds the
  `C:\mux-local` paths and the VRAM-sized MusicGen tier. Never commit it, and
  never move these values into `config/app.defaults.json`, which is shared and
  would break the M1. A fresh clone needs this file recreated.
- **`PYTHONUTF8=1` is required.** The pipeline logs non-ASCII and Windows
  defaults to cp1252, which makes subprocess stdout raise `UnicodeEncodeError`.
  `run.sh` exports it; set it yourself for direct `pytest` or CLI runs.
- **ffmpeg** is on the user PATH but new shells may need a restart to see it:
  `C:\Users\Swapnil\AppData\Local\Microsoft\WinGet\Packages\Gyan.FFmpeg_*\ffmpeg-*\bin`
- Python 3.12 is the core interpreter. The DeepFilter venv is **3.11** on
  purpose: `DeepFilterLib` publishes no cp312 wheel.

## Commands

```bash
./scripts/run.sh                          # GUI at http://127.0.0.1:8765
./scripts/run.sh --cli run --run-id ...   # headless
MUX_REBUILD_GUI=1 ./scripts/run.sh        # rebuild React bundle first
PYTHONUTF8=1 .venv/Scripts/python -m pytest -q    # full suite, ~13 min

./scripts/bootstrap_venv_windows.sh                # setup (NOT bootstrap_venv.sh)
python scripts/download_local_models_cuda.py --verify
```

Runs land in `ASSETS/executions/exec_*`.

## Scope

- **Terraform and podcast RSS publish are out of scope.** Do not set them up or
  debug them; they are the last stage and explicitly deferred.
- Local model stacks are installed and verified working. Nicket also reports
  them fine. Suspect the **LLM-driven delivery band, stages 40 to 60** first.
  See [ISSUES.md](ISSUES.md).

## Test baseline

**6633 passed, 14 failed, 19 skipped.** All 14 were already failing before the
Windows port, so treat that set as the known baseline, not as regressions.
`OPENAI_API_KEY` being unset accounts for some of them. The list and analysis
are in [ISSUES.md](ISSUES.md).

## Conventions

- No em dashes in generated text, anywhere (code comments, docs, commit
  messages, PR bodies).
- No AI attribution or `Co-Authored-By` trailers in commits.
- Never let unvalidated metadata drive an expensive operation. A declared
  duration, size, or count from a file header or API response gets a sanity
  check against a real measure before it becomes a loop bound, an allocation,
  or an ffmpeg `-t` argument. Give such subprocesses a timeout as a backstop.

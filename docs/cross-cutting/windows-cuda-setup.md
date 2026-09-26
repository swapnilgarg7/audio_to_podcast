# Windows + CUDA setup

The shipped bootstrap targets **macOS Apple Silicon** and builds MLX stacks for
local STT, diarization, and the volley-framer LLM. MLX is Apple-only. This page
covers the CUDA/CPU equivalents that satisfy the same pipeline contracts, so the
72-stage pipeline runs unchanged on a Windows + NVIDIA host.

Nothing here is Windows-only by design: the same substitutions apply to a Linux
CUDA box. The MLX path stays the default wherever MLX imports.

---

## What replaces what

| Stack | macOS (MLX) | Windows / Linux (CUDA) | Contract |
|---|---|---|---|
| STT + word timings | `mlx-audio` Whisper | **faster-whisper** (CTranslate2) | `tools/stt_transcribe.py` → `{text, words[], segments[]}` |
| Diarization | Sortformer (`mlx_audio.vad`) | **pyannote 3.1**, optional | speaker ids on words; falls open to `spk_0` |
| Volley framer LLM | `mlx-lm` 4-bit | **transformers + bitsandbytes** NF4 | `tools/local_llm_infer.py` stdin/stdout JSON |
| Speech denoise | DeepFilterNet (maturin build) | **`deepfilternet` PyPI wheel** | `tools/deepfilter_enhance.py` |
| SFX + CLAP | MMAudio | MMAudio (already CUDA-native) | `tools/mmaudio_generate.py`, `tools/clap_embed_window.py` |
| Theme music | MusicGen (transformers) | MusicGen (transformers, CUDA) | `musicgen_runner` |
| Gap VO clone | `mlx-audio` S2S | **Chatterbox** (torch) | `synthesis_fallback` |

Backend selection is automatic: each entry point prefers MLX when it imports and
falls back to the CUDA/CPU backend otherwise. No flag to set.

---

## Prerequisites

```powershell
winget install Python.Python.3.12       # core + most runtimes
winget install Python.Python.3.11       # DeepFilterNet only (see note)
winget install Gyan.FFmpeg
winget install OpenJS.NodeJS.LTS        # if Node is not already present
```

An NVIDIA driver supporting CUDA 12.x. The CUDA **toolkit** is not needed:
torch and the `nvidia-*-cu12` wheels ship their own runtime libraries.

**Python 3.11 for DeepFilterNet.** `DeepFilterLib` publishes prebuilt wheels only
up to cp311. Its venv is isolated from the core 3.12 venv, so running it on 3.11
costs nothing. To stay on 3.12 instead, install Rust and build from source:
`CLONE_DEEPFILTER=1 ./scripts/clone_local_audio_repos.sh`.

---

## Two Windows-specific settings

**UTF-8 mode.** The pipeline logs non-ASCII (`→`, `✓`) and Windows defaults to
cp1252, so subprocess output raises `UnicodeEncodeError`. `scripts/run.sh`
exports `PYTHONUTF8=1`; set it globally for direct `pytest` / CLI runs:

```powershell
[Environment]::SetEnvironmentVariable('PYTHONUTF8','1','User')
```

**MAX_PATH.** Windows caps paths at 260 characters. `torch`'s ATen headers have
very long names, so `pip install torch` fails outright inside a deep repo path
(a cloud-synced folder is usually deep enough). Keep the heavy stacks on a short
path, as described below. Enabling `HKLM\...\FileSystem\LongPathsEnabled` also works but
needs admin and does not address cloud sync.

---

## Where the heavy stacks live

Venvs, model weights, and upstream clones total roughly **45 GB** (measured: musicgen 15, llm 11, mmaudio 6, chatterbox 5.5, deepfilter 4.7, speech 2.3). They belong
off the repo volume: short paths dodge MAX_PATH, and a cloud-synced repo folder
should not be syncing model weights.

`config/app.defaults.json` is committed and shared across machines, so it must
stay machine-neutral. Per-machine paths go in **`config/app.local.json`**
(gitignored), which `load_defaults()` deep-merges over the defaults:

```json
{
  "local_runtimes": {
    "deepfilter": { "venv_dir": "C:/mux-local/local_deepfilter/venv" },
    "mmaudio":    { "venv_dir": "C:/mux-local/local_mmaudio/venv" },
    "musicgen":   { "venv_dir": "C:/mux-local/local_musicgen/venv" },
    "speech":     { "venv_dir": "C:/mux-local/local_speech/venv" },
    "mlx":        { "venv_dir": "C:/mux-local/local_llm/venv" }
  },
  "local_llm": { "models_dir": "C:/mux-local/local_llm/models" },
  "mmaudio":   { "repo_dir":   "C:/mux-local/local_mmaudio/MMAudio" },
  "musicgen":  { "hf_cache_dir": "C:/mux-local/local_musicgen/hf_cache" }
}
```

Only the keys that differ need to appear; everything else falls through to the
committed defaults.

---

## Bootstrap

From **Git Bash**, not PowerShell (the scripts are bash):

```bash
./scripts/bootstrap_venv_windows.sh
./scripts/run.sh          # http://127.0.0.1:8765
```

Flags: `BOOTSTRAP_SKIP_GUI=1`, `BOOTSTRAP_SKIP_MODELS=1`,
`MUX_LOCAL_ROOT=D:/mux-local`.

Weights only, after the venvs exist:

```bash
python scripts/download_local_models_cuda.py           # all
python scripts/download_local_models_cuda.py --stt     # one stack
python scripts/download_local_models_cuda.py --verify  # report only
```

That script also writes `ASSETS/local_speech/selection.json` and
`ASSETS/local_llm/selection.json`, which the runners read to learn which model
and backend this host selected.

---

## Sizing the model ladder to your VRAM

`musicgen.model_id` ships as `facebook/musicgen-large` (3.3B, ~6.6 GB fp16).
On a card with less VRAM than that, every theme generation OOMs and walks down
the ladder before producing anything. Pin the ladder to what actually fits:

```json
{
  "musicgen": {
    "model_id": "facebook/musicgen-medium",
    "melody_model_id": "facebook/musicgen-melody",
    "prefetch_models": ["facebook/musicgen-medium", "facebook/musicgen-small"]
  }
}
```

The local LLM loads 4-bit NF4 via bitsandbytes, so a 3B instruct model needs
~2.5 GB. Set `MUX_LOCAL_LLM_4BIT=0` to force fp16 on a larger card.

Turing cards (sm_75: GTX 16xx, RTX 20xx, T4) have no bf16, so the backend
computes in fp16. STT uses `int8_float16`; override with `MUX_STT_DEVICE` /
`MUX_STT_COMPUTE_TYPE`.

---

## Diarization

macOS needs no setup here: `mlx_audio.vad` loads
`mlx-community/diar_sortformer_4spk-v1-fp32`, which is **ungated**, and MLX
bundles the Sortformer implementation. No token, no extra toolkit.

CUDA has no equally free path, so pick one:

**Option A, pyannote (MIT, easy install, needs a token).** Gated on Hugging
Face (`gated=auto`), so the weights 401 without one:

1. Accept the terms for `pyannote/speaker-diarization-3.1` and `pyannote/segmentation-3.0`.
2. Put `HF_TOKEN=...` in `config/secrets/secrets.env`.
3. `<speech venv>/Scripts/python -m pip install pyannote.audio`

Note this pulls torch into the speech venv, which otherwise has none
(faster-whisper runs on CTranslate2). Re-pin `torch==...+cu124` afterwards if
pip resolves a CPU build, the same fixup MMAudio needs.

**Option B, the same Sortformer model macOS uses (no token, but NeMo).**
`nvidia/diar_sortformer_4spk-v1` is ungated, but `transformers` does **not**
implement Sortformer. Its `config.json` advertises
`transformers_version: 5.0.0.dev0` and `architectures: ['SortformerOffline']`,
which is from an unmerged branch: there is no `models/sortformer` in the 5.17
release or in git main. Loading the `.nemo` checkpoint therefore needs
`nemo_toolkit[asr]`, which is a heavy dependency and imperfectly supported on
Windows.

**Licensing matters in this choice.** `nvidia/diar_sortformer_4spk-v1` is
**cc-by-nc-4.0 (non-commercial)**; pyannote is MIT. The macOS default is the
MLX port of the NC model, so a commercially published podcast has a licensing
problem on *both* platforms, not just this one. That is an argument for moving
both to pyannote rather than matching the macOS default.

With neither installed, transcription still works and every word is labelled `spk_0`. That
is the same behaviour as the MLX Whisper path, but multi-speaker stages will not
be able to tell your speakers apart, so install it for real interviews.

`MUX_STT_DIARIZATION=0` disables diarization explicitly.

---

## Known platform gaps

- **`uvloop`** is pinned in `requirements.lock` and has no Windows build.
  `install_core_venv.sh` filters it out on Windows; uvicorn uses the asyncio
  loop instead.
- **`tests/test_musicgen_gpu_realworld.py::test_shipped_defaults_gpu_large_first_ladder`**
  asserts `effective_musicgen_device() == "mps"`. It is an Apple-hardware
  regression lock and cannot pass on CUDA.
- **`lsof` / `ps -ax`** port and orphan cleanup in `run.sh` is skipped on Windows
  (guarded by `command -v`). A stale server on the web port must be killed
  manually.
- **HF cache duplication on Windows.** Without symlink support the hub copies
  each file into `snapshots/` as well as `blobs/`, so a model can occupy twice
  its size. An interrupted download also leaves a `*.incomplete` blob and can
  strand a second revision holding only `model.safetensors` with no config.
  Check `refs/main` for the live revision, then delete `*.incomplete` blobs and
  any snapshot revision it does not point at. This reclaimed 13.4 GB from
  musicgen-medium alone; verify afterwards with `HF_HUB_OFFLINE=1`.

#!/usr/bin/env bash
# One-time setup for Windows + NVIDIA CUDA hosts (run from Git Bash).
#
#   ./scripts/bootstrap_venv_windows.sh
#
# The macOS bootstrap (scripts/bootstrap_venv.sh) builds MLX stacks that only
# exist on Apple Silicon. This script builds the CUDA/CPU equivalents that the
# same pipeline contracts accept:
#
#   local_speech   mlx-audio Whisper  ->  faster-whisper (CTranslate2, CUDA)
#   local_llm      mlx-lm             ->  transformers + bitsandbytes 4-bit
#   local_deepfilter                  ->  deepfilternet PyPI wheel (CUDA torch)
#   local_mmaudio                     ->  upstream MMAudio (already CUDA-native)
#   local_musicgen                    ->  transformers MusicGen (CUDA)
#
# Where those stacks live comes from config/app.local.json (gitignored) so the
# committed config stays machine-neutral. Heavy venvs belong OFF the repo
# volume on Windows: the 260-char MAX_PATH limit breaks torch's ATen headers
# inside a deep repo path, and keeps GBs of weights out of cloud-sync folders.
#
# Optional:
#   BOOTSTRAP_SKIP_GUI=1       Skip the npm build
#   BOOTSTRAP_SKIP_MODELS=1    Create venvs but do not download model weights
#   MUX_LOCAL_ROOT=C:/mux-local  Where the heavy stacks live
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
# shellcheck source=scripts/lib/venv_shim.sh
source "$ROOT/scripts/lib/venv_shim.sh"

LOCAL_ROOT="${MUX_LOCAL_ROOT:-C:/mux-local}"
TORCH_INDEX="https://download.pytorch.org/whl/cu124"

PY312="$(mux_host_python)"
if [[ -z "$PY312" ]]; then
  echo "ERROR: Python 3.12 not found. Install it (winget install Python.Python.3.12)." >&2
  exit 1
fi
# DeepFilterLib publishes no cp312 wheel; its venv needs 3.11 unless you build
# the Rust extension yourself (CLONE_DEEPFILTER=1 + rustc + maturin).
PY311="${PYTHON311:-C:/Users/$USER/AppData/Local/Programs/Python/Python311/python.exe}"

echo "=== 1/6 Core .venv ==="
bash "$ROOT/scripts/lib/install_core_venv.sh"
CORE_PY="$(mux_venv_python "$ROOT/.venv")"

echo "=== 2/6 GUI bundle ==="
if [[ "${BOOTSTRAP_SKIP_GUI:-0}" != "1" ]]; then
  bash "$ROOT/scripts/lib/install_frontend.sh"
else
  echo "Skipped (BOOTSTRAP_SKIP_GUI=1)"
fi

echo "=== 3/6 Local speech (faster-whisper on CUDA) ==="
SPEECH_VENV="$LOCAL_ROOT/local_speech/venv"
if [[ ! -d "$SPEECH_VENV" ]]; then
  "$PY312" -m venv "$SPEECH_VENV"
fi
SPEECH_PY="$(mux_venv_python "$SPEECH_VENV")"
"$SPEECH_PY" -m pip install -q -U pip setuptools wheel
# nvidia-* wheels carry the cuBLAS/cuDNN DLLs CTranslate2 loads at runtime.
"$SPEECH_PY" -m pip install -q \
  "faster-whisper>=1.1" "soundfile>=0.12.1" "numpy>=1.26.0" "huggingface_hub>=0.26" \
  "nvidia-cublas-cu12" "nvidia-cudnn-cu12>=9,<10"
"$SPEECH_PY" "$ROOT/tools/stt_transcribe.py" --verify

echo "=== 4/6 Local audio (DeepFilterNet + MMAudio) ==="
bash "$ROOT/scripts/clone_local_audio_repos.sh"

DF_VENV="$LOCAL_ROOT/local_deepfilter/venv"
if [[ -x "$PY311" ]]; then
  if [[ ! -d "$DF_VENV" ]]; then
    "$PY311" -m venv "$DF_VENV"
  fi
  DF_PY="$(mux_venv_python "$DF_VENV")"
  "$DF_PY" -m pip install -q -U pip setuptools wheel
  # df.io imports torchaudio.backend, removed after 2.5.x — keep the pin.
  "$DF_PY" -m pip install -q torch==2.5.1 torchaudio==2.5.1 --index-url "$TORCH_INDEX"
  "$DF_PY" -m pip install -q "numpy<2" "soundfile>=0.13.1" deepfilternet
  "$DF_PY" "$ROOT/tools/deepfilter_enhance.py" --verify \
    || echo "WARN: DeepFilterNet verify failed — preclean unavailable."
else
  echo "WARN: Python 3.11 not found at $PY311 — skipping DeepFilterNet."
  echo "      DeepFilterLib has no cp312 wheel; install 3.11 or set PYTHON311=."
fi

MM_VENV="$LOCAL_ROOT/local_mmaudio/venv"
MM_REPO="$LOCAL_ROOT/local_mmaudio/MMAudio"
if [[ ! -d "$MM_VENV" ]]; then
  "$PY312" -m venv "$MM_VENV"
fi
MM_PY="$(mux_venv_python "$MM_VENV")"
"$MM_PY" -m pip install -q -U pip setuptools wheel
"$MM_PY" -m pip install -q "transformers>=4.36.0" "librosa>=0.10.0"
"$MM_PY" -m pip install -q -e "$MM_REPO"
# `pip install -e MMAudio` pulls the CPU torch from PyPI over the CUDA build,
# so re-pin the whole trio afterwards or every SFX stage silently runs on CPU.
"$MM_PY" -m pip install -q torch==2.6.0 torchaudio==2.6.0 torchvision==0.21.0 --index-url "$TORCH_INDEX"
"$MM_PY" "$ROOT/tools/mmaudio_generate.py" --verify --repo "$MM_REPO"

echo "=== 5/6 Local MusicGen (CUDA) ==="
MG_VENV="$LOCAL_ROOT/local_musicgen/venv"
if [[ ! -d "$MG_VENV" ]]; then
  "$PY312" -m venv "$MG_VENV"
fi
MG_PY="$(mux_venv_python "$MG_VENV")"
"$MG_PY" -m pip install -q -U pip setuptools wheel
"$MG_PY" -m pip install -q torch torchaudio --index-url "$TORCH_INDEX"
"$MG_PY" -m pip install -q numpy scipy transformers

echo "=== 6/6 Local LLM (transformers + bitsandbytes 4-bit) ==="
LLM_VENV="$LOCAL_ROOT/local_llm/venv"
if [[ ! -d "$LLM_VENV" ]]; then
  "$PY312" -m venv "$LLM_VENV"
fi
LLM_PY="$(mux_venv_python "$LLM_VENV")"
"$LLM_PY" -m pip install -q -U pip setuptools wheel
"$LLM_PY" -m pip install -q torch --index-url "$TORCH_INDEX"
"$LLM_PY" -m pip install -q \
  "transformers>=4.44,<5" "accelerate>=0.30" "bitsandbytes>=0.45" \
  "huggingface_hub>=0.26" sentencepiece protobuf

if [[ "${BOOTSTRAP_SKIP_MODELS:-0}" != "1" ]]; then
  echo "--- Downloading model weights (several GB) ---"
  "$CORE_PY" "$ROOT/scripts/download_local_models_cuda.py" || \
    echo "WARN: weight download incomplete — rerun scripts/download_local_models_cuda.py"
fi

echo ""
echo "Setup complete. Start the app:"
echo "  ./scripts/run.sh"

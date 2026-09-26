#!/usr/bin/env bash
# Clone or update upstream MMAudio and DeepFilterNet into separate ASSETS trees.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
MMAUDIO_URL="https://github.com/hkchengrex/MMAudio.git"
DF_URL="https://github.com/rikorose/deepfilternet.git"
# Clone destinations come from config (mmaudio.repo_dir / deepfilter.repo_dir)
# so operators can host the heavy stacks off the repo volume — needed on
# Windows, where the 260-char MAX_PATH limit breaks deep dependency trees.
_config_dir() {
  local key="$1"
  local fallback="$2"
  local py
  if [[ -x "$ROOT/.venv/bin/python" ]]; then
    py="$ROOT/.venv/bin/python"
  elif [[ -x "$ROOT/.venv/Scripts/python.exe" ]]; then
    py="$ROOT/.venv/Scripts/python.exe"
  else
    echo "$ROOT/$fallback"
    return 0
  fi
  MUX_CFG_KEY="$key" MUX_CFG_FALLBACK="$fallback" "$py" - <<'PY' 2>/dev/null || echo "$ROOT/$fallback"
import os
from pathlib import Path
from interview_mux.config import merged_config, repo_root

key = os.environ["MUX_CFG_KEY"]
fallback = os.environ["MUX_CFG_FALLBACK"]
block = merged_config().get(key) or {}
rel = str(block.get("repo_dir") or fallback)
path = Path(rel)
print(path if path.is_absolute() else repo_root() / path)
PY
}

MMAUDIO_DIR="$(_config_dir mmaudio ASSETS/local_mmaudio/MMAudio)"
DF_DIR="$(_config_dir deepfilter ASSETS/local_deepfilter/DeepFilterNet)"

mkdir -p "$(dirname "$MMAUDIO_DIR")" "$(dirname "$DF_DIR")"

clone_or_pull() {
  local url="$1"
  local dest="$2"
  if [[ -d "$dest/.git" ]]; then
    echo "Updating $(basename "$dest")…"
    git -C "$dest" pull --ff-only
  elif [[ -d "$dest" ]]; then
    echo "ERROR: $dest exists but is not a git clone. Remove it and re-run." >&2
    exit 1
  else
    echo "Cloning $url → $dest"
    git clone "$url" "$dest"
  fi
}

clone_or_pull "$MMAUDIO_URL" "$MMAUDIO_DIR"

# The DeepFilterNet source clone only exists to build the Rust extension with
# maturin. Where a prebuilt DeepFilterLib wheel is published (Windows/Linux/
# macOS CPython <= 3.11), pip installs `deepfilternet` directly and the clone
# is dead weight — set CLONE_DEEPFILTER=1 to force building from source.
if [[ "${CLONE_DEEPFILTER:-0}" == "1" ]]; then
  clone_or_pull "$DF_URL" "$DF_DIR"
fi

echo "Local audio repos ready:"
echo "  MMAudio:       $MMAUDIO_DIR"
if [[ "${CLONE_DEEPFILTER:-0}" == "1" ]]; then
  echo "  DeepFilterNet: $DF_DIR"
else
  echo "  DeepFilterNet: pip wheel (clone skipped; CLONE_DEEPFILTER=1 for source build)"
fi

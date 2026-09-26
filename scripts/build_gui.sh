#!/usr/bin/env bash
# Build React + TypeScript GUI into src/interview_mux/web/static/
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
FRONTEND="$ROOT/frontend"
STATIC="$ROOT/src/interview_mux/web/static"

if ! command -v npm >/dev/null 2>&1; then
  echo "ERROR: npm is required to build the GUI. Install Node.js 20+." >&2
  exit 1
fi

_python_for_verify() {
  if [[ -x "$ROOT/.venv/bin/python" ]]; then
    echo "$ROOT/.venv/bin/python"
  elif [[ -x "$ROOT/.venv/Scripts/python.exe" ]]; then
    # Windows venv layout (Git Bash / MSYS).
    echo "$ROOT/.venv/Scripts/python.exe"
  elif command -v python3.12 >/dev/null 2>&1; then
    command -v python3.12
  else
    command -v python3
  fi
}

_verify_bundle() {
  local py
  py="$(_python_for_verify)"
  if ! PYTHONPATH="$ROOT/src${PYTHONPATH:+:$PYTHONPATH}" "$py" - <<'PY'
import sys
from interview_mux.gui_bundle import bundle_contract_ok, missing_referenced_assets, static_dir

root = static_dir()
missing = missing_referenced_assets(root)
if missing:
    print("GUI bundle incomplete — missing referenced assets:", ", ".join(missing), file=sys.stderr)
    raise SystemExit(1)
if not bundle_contract_ok(root):
    print(
        "GUI bundle is missing required checkpoint API markers "
        "(expected advanceFromCheckpoint in primary JS).",
        file=sys.stderr,
    )
    raise SystemExit(1)
PY
  then
    echo "ERROR: GUI bundle verification failed." >&2
    if [[ ! -d "$STATIC/assets" ]] || [[ -z "$(ls -A "$STATIC/assets" 2>/dev/null || true)" ]]; then
      echo "       static/assets/ is empty or missing — rebuild with: ./scripts/build_gui.sh" >&2
    fi
    exit 1
  fi
}

cd "$FRONTEND"

_host_arch="$(uname -m)"
_node_arch="$(node -p "process.arch" 2>/dev/null || echo unknown)"
case "$_host_arch" in
  arm64) _want_node_arch="arm64" ;;
  x86_64 | amd64) _want_node_arch="x64" ;;
  *)
    _want_node_arch="$_host_arch"
    ;;
esac

if [[ "$_node_arch" != "$_want_node_arch" ]]; then
  echo "WARN: Node.js process.arch is ${_node_arch} but uname -m is ${_host_arch} (expected ${_want_node_arch} for native addons)." >&2
  echo "      On Apple Silicon, prefer arm64 Node (brew install node@20) so Rollup installs @rollup/rollup-darwin-arm64." >&2
fi

_install_deps() {
  if [[ -f package-lock.json ]]; then
    npm ci
  else
    npm install
  fi
}

_rollup_ok() {
  node -e "require('rollup')" >/dev/null 2>&1
}

if [[ ! -d node_modules ]] || ! _rollup_ok; then
  if [[ -d node_modules ]]; then
    echo "Repairing frontend/node_modules (missing or wrong-arch Rollup binary) ..."
    rm -rf node_modules
  else
    echo "Installing frontend dependencies ..."
  fi
  _install_deps
fi

if ! _rollup_ok; then
  echo "ERROR: Rollup native module still missing after npm ci." >&2
  echo "From repo root:" >&2
  echo "  cd frontend && rm -rf node_modules && npm ci && cd .. && ./scripts/build_gui.sh" >&2
  exit 1
fi

echo "Building React GUI ..."
# Vite emptyOutDir clears static/, but remove assets explicitly so no orphan chunks linger.
rm -rf "$STATIC/assets"
if ! npm run build; then
  echo "ERROR: vite build failed. If static/assets/ is missing, rerun ./scripts/build_gui.sh." >&2
  exit 1
fi

_verify_bundle
echo "GUI built → src/interview_mux/web/static/"

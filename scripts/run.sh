#!/usr/bin/env bash
# Start the web GUI (default) or headless CLI.
#
# First time on this machine:
#   ./scripts/bootstrap_venv.sh
#   ./scripts/run.sh
#
# Options:
#   ./scripts/run.sh --run-id exec_NNN_…   Open GUI on an existing execution
#   ./scripts/run.sh --cli …     python -m interview_mux … (no server)
#   ./scripts/run.sh --cli run --run-id exec_NNN_… --from-stage boundary_detection
#                              Headless resume (same as python -m interview_mux run …)
#   MUX_PRESERVE_SESSION=1       Keep last run selected in the GUI
#   MUX_REBUILD_GUI=1            Rebuild React bundle before serve
#   MUX_REFRESH_DEPS=1           Re-pip core .venv after git pull
#   MUX_SKIP_ASSETS_CLEANUP=1    Skip ephemeral ASSETS/ cleanup (debug)
#   MUX_NO_BROWSER=1             Pass --no-browser to serve (headless / e2e)
#   MUX_RUN_MODE=manual|full-auto  Skip interactive mode prompt
#   MUX_FULL_AUTO=1              Alias for Full-auto (soft automation)
#   MUX_BABA_E2E=1               Legacy alias for MUX_FULL_AUTO
#                                (MUX_INPUT_AUDIO / MUX_FRESH / MUX_RUN_ID honored)
#   MUX_DETACH_SERVE=1           Start serve in its own session and return
#                                (unattended e2e: server survives parent shell exit)
#   MUX_KEEPALIVE=1              Opt-in Full-auto crash watchdog (default off)
#
# Fresh launch (default): clears ephemeral ASSETS/ state (.gui session,
# operator session logs, stale locks inside exec_*). Never deletes any
# directory under ASSETS/executions/ (prior runs always kept). Input WAVs
# and local_* runtimes are never deleted.
#
# Regular GUI/CLI (not --full-auto / MUX_FULL_AUTO / MUX_RUN_MODE=full-auto):
# also runs `python tools/full_auto_daemon_launch.py stop` so a leftover
# Full-auto driver/keepalive cannot hijack the new session. Full-auto/e2e
# launches skip that stop so they can recycle/resume their own stack.
set -euo pipefail
IFS=$'\n\t'

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

export PYTHONUNBUFFERED=1
export MUX_LAUNCHED_VIA=run.sh
# Windows prints cp1252 by default and the pipeline logs non-ASCII (→, ✓);
# UTF-8 mode keeps subprocess stdout decodable on every platform.
export PYTHONUTF8=1

# Put the core .venv first on PATH so every bare `python` below is the venv
# interpreter without the caller having to activate it. bin/ on POSIX,
# Scripts/ on Windows (Git Bash / MSYS).
if [[ -x "$ROOT/.venv/bin/python" ]]; then
  export PATH="$ROOT/.venv/bin:$PATH"
elif [[ -x "$ROOT/.venv/Scripts/python.exe" ]]; then
  export PATH="$ROOT/.venv/Scripts:$PATH"
else
  echo "ERROR: core .venv missing — run ./scripts/bootstrap_venv.sh first" >&2
  exit 1
fi

CLI_MODE=0
SERVE_ARGS=()
GUI_RUN_ID=""

_validate_execution_id() {
  local rid="$1"
  MUX_VALIDATE_RUN_ID="$rid" python - <<'PY'
import os
import sys
from interview_mux.assets_ephemeral_cleanup import executions_root, is_product_execution_dir

rid = os.environ.get("MUX_VALIDATE_RUN_ID", "")
if not is_product_execution_dir(rid):
    print(f"ERROR: invalid execution id: {rid}", file=sys.stderr)
    sys.exit(1)
path = executions_root() / rid
if not path.is_dir():
    print(f"ERROR: execution not found: {path}", file=sys.stderr)
    sys.exit(1)
PY
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --run-id)
      if [[ -z "${2:-}" ]]; then
        echo "ERROR: --run-id requires an execution id (exec_NNN_…)" >&2
        exit 1
      fi
      GUI_RUN_ID="$2"
      export MUX_RUN_ID="$2"
      shift 2
      ;;
    --cli)
      CLI_MODE=1
      shift
      break
      ;;
    --full-auto)
      export MUX_RUN_MODE=full-auto
      export MUX_FULL_AUTO=1
      shift
      ;;
    --input)
      if [[ -z "${2:-}" ]]; then
        echo "ERROR: --input requires a path under ASSETS/input/" >&2
        exit 1
      fi
      export MUX_INPUT_AUDIO="$2"
      shift 2
      ;;
    -h | --help)
      cat <<'EOF'
Usage: ./scripts/run.sh [--run-id exec_NNN_…] [--cli] [--full-auto] [--input ASSETS/input/file.mp3] [serve args…]

Setup once:  ./scripts/bootstrap_venv.sh
Launch:      ./scripts/run.sh

Resume an existing execution in the GUI:

  ./scripts/run.sh --run-id exec_2512_d19c15b58ab4_20260828T222510Z

Opens the browser at /?run=… so the Pipeline workbench loads that run. Re-run stages
from the GUI (e.g. boundary_detection) or use headless resume:

  ./scripts/run.sh --cli run --run-id exec_2512_… --from-stage boundary_detection

Interactive (TTY): choose Manual (default) or Full-auto, then pick source audio
for Full-auto. Flags skip the prompts:

  ./scripts/run.sh --full-auto --input ASSETS/input/interview.mp3

Full-auto heal/remutates/re-executes with bounded product repairs (no silent
quality waivers). On halt or complete it writes operator/EXECUTION_REPORT.md.
Prefer the GUI Start-page control for Full-auto when launching Manual.

Environment:
  MUX_RUN_MODE=manual|full-auto  Skip mode prompt
  --run-id exec_NNN_…            Open GUI on existing execution; sets MUX_RUN_ID
  --full-auto                    Same as MUX_RUN_MODE=full-auto
  --input ASSETS/input/file.mp3  Same as MUX_INPUT_AUDIO (Full-auto)
  MUX_INPUT_AUDIO=ASSETS/input/… Skip audio picker (Full-auto; mp3 is converted to WAV)
  MUX_PRESERVE_SESSION=1         Keep GUI session across launches
  MUX_REBUILD_GUI=1              npm build before serve
  MUX_REFRESH_DEPS=1             Refresh core .venv after git pull
  MUX_SKIP_ASSETS_CLEANUP=1      Skip ephemeral ASSETS/ cleanup
  MUX_NO_BROWSER=1               Do not open a browser tab
  MUX_FULL_AUTO=1                Alias for Full-auto (same soft stack)
  MUX_BABA_E2E=1                 Legacy alias for MUX_FULL_AUTO
  MUX_DETACH_SERVE=1             Detach serve into its own session and return
  MUX_KEEPALIVE=1                Opt-in Full-auto crash watchdog (off by default)
  MUX_FRESH / MUX_RUN_ID         Fresh create vs resume (MUX_FRESH=1 wins over MUX_RUN_ID)

Regular ./scripts/run.sh (Manual GUI / --cli, not Full-auto) stops leftover
Full-auto daemons first (driver, keepalive, prior serve) so a previous
overnight run cannot claim the new session. --full-auto / MUX_FULL_AUTO
skip that stop and recycle their own stack.
EOF
      exit 0
      ;;
    *)
      SERVE_ARGS+=("$1")
      shift
      ;;
  esac
done

_normalize_run_mode() {
  local raw
  raw="$(printf '%s' "${1:-}" | tr '[:upper:]' '[:lower:]' | tr -d ' ')"
  case "$raw" in
    full-auto|fullauto|auto|e2e|baba) echo "full-auto" ;;
    manual|gui|"") echo "manual" ;;
    *) echo "" ;;
  esac
}

_full_auto_env_set() {
  # Prefer MUX_FULL_AUTO; accept legacy MUX_BABA_E2E.
  local v
  v="$(printf '%s' "${MUX_FULL_AUTO:-${MUX_BABA_E2E:-0}}" | tr '[:upper:]' '[:lower:]')"
  case "$v" in
    1|true|yes) return 0 ;;
    *) return 1 ;;
  esac
}

_is_full_auto_cli_launch() {
  # True when this invocation is already Full-auto/e2e via flag or env — not
  # the interactive Manual GUI. Those paths recycle/resume their own driver.
  if _full_auto_env_set; then
    return 0
  fi
  local preset
  preset="$(_normalize_run_mode "${MUX_RUN_MODE:-}")"
  [[ "$preset" == "full-auto" ]]
}

# shellcheck source=scripts/lib/require_venv.sh
source "$ROOT/scripts/lib/require_venv.sh"
require_core_venv "$ROOT"

if [[ -n "$GUI_RUN_ID" ]]; then
  _validate_execution_id "$GUI_RUN_ID" || exit 1
fi

if [[ "${MUX_REFRESH_DEPS:-0}" == "1" ]]; then
  bash "$ROOT/scripts/lib/install_core_venv.sh"
  # shellcheck source=/dev/null
  if [[ -f "$ROOT/.venv/bin/activate" ]]; then
    source "$ROOT/.venv/bin/activate"
  else
    source "$ROOT/.venv/Scripts/activate"
  fi
fi

if [[ "${MUX_REBUILD_GUI:-0}" == "1" ]]; then
  if ! command -v npm >/dev/null 2>&1; then
    echo "ERROR: npm required for MUX_REBUILD_GUI=1 — install Node.js 20+" >&2
    exit 1
  fi
  bash "$ROOT/scripts/build_gui.sh"
elif [[ ! -f "$ROOT/src/interview_mux/web/static/index.html" ]]; then
  echo "ERROR: GUI bundle missing — run ./scripts/bootstrap_venv.sh first" >&2
  exit 1
fi

# Release stale process holds before deleting lock files under exec_*/
WEB_PORT="$(python - <<'PY'
from interview_mux.config import merged_config
print(int(merged_config().get("web_port", 8765)))
PY
)"

# Regular GUI/CLI: this launch is a unique session. Tear down leftover
# Full-auto driver + keepalive + prior serve so they cannot POST /execute
# into the new GUI. Skip for --full-auto / e2e (they recycle their stack).
if ! _is_full_auto_cli_launch; then
  echo "Stopping leftover Full-auto daemons (prior driver/keepalive/serve)…" >&2
  python "$ROOT/tools/full_auto_daemon_launch.py" stop || true
fi

if command -v lsof >/dev/null 2>&1; then
  stale_pids="$(lsof -ti "tcp:${WEB_PORT}" 2>/dev/null || true)"
  if [[ -n "${stale_pids}" ]]; then
    kill ${stale_pids} 2>/dev/null || true
    sleep 1
  fi
fi

if command -v ps >/dev/null 2>&1; then
  orphan_workers="$(ps -ax -o pid=,command= 2>/dev/null | grep 'interview_mux\.stage_worker' | awk '{print $1}' | tr '\n' ' ' || true)"
  if [[ -n "${orphan_workers// /}" ]]; then
    kill ${orphan_workers} 2>/dev/null || true
    sleep 1
  fi
  # Stale keepalive will resurrect serve after this launch otherwise.
  # Regular launches already stopped it above; Full-auto still needs this.
  orphan_ka="$(ps -ax -o pid=,command= 2>/dev/null | grep -E 'full_auto_keepalive_loop\.py|baba_keepalive_loop\.py' | grep -v grep | awk '{print $1}' | tr '\n' ' ' || true)"
  if [[ -n "${orphan_ka// /}" ]]; then
    kill ${orphan_ka} 2>/dev/null || true
    sleep 1
  fi
fi

if [[ "${MUX_SKIP_ASSETS_CLEANUP:-0}" != "1" ]]; then
  CLEANUP_ARGS=()
  if [[ "${MUX_PRESERVE_SESSION:-0}" == "1" ]]; then
    CLEANUP_ARGS+=(--preserve-session)
  fi
  # Under `set -u`, empty "${arr[@]}" is unbound on some Bash builds — expand safely.
  if ((${#CLEANUP_ARGS[@]} > 0)); then
    python -m interview_mux.assets_ephemeral_cleanup "${CLEANUP_ARGS[@]}"
  else
    python -m interview_mux.assets_ephemeral_cleanup
  fi
fi

if [[ "$CLI_MODE" == "1" ]]; then
  if (($# > 0)); then
    exec python -m interview_mux "$@"
  fi
  exec python -m interview_mux
fi

if [[ -n "$GUI_RUN_ID" ]]; then
  SERVE_ARGS+=("--run-id" "$GUI_RUN_ID")
  echo "GUI will open execution ${GUI_RUN_ID}" >&2
fi

# --- Manual / Full-auto mode (default Manual) ---------------------------------
_keepalive_requested() {
  local v
  v="$(printf '%s' "${MUX_KEEPALIVE:-0}" | tr '[:upper:]' '[:lower:]')"
  case "$v" in
    1|true|yes|on) return 0 ;;
    *) return 1 ;;
  esac
}

pick_run_mode() {
  local preset
  preset="$(_normalize_run_mode "${MUX_RUN_MODE:-}")"
  if [[ -n "$preset" ]]; then
    echo "$preset"
    return
  fi
  if _full_auto_env_set; then
    echo "full-auto"
    return
  fi
  if [[ ! -t 0 ]]; then
    echo "manual"
    return
  fi
  # Prompts on stderr so only the mode token is captured on stdout.
  echo "" >&2
  echo "Run mode" >&2
  echo "  [ Manual ●──────── Full-auto ]" >&2
  echo "  1) Manual     — GUI; pick Manual/Full-auto on Start (default)" >&2
  echo "  2) Full-auto  — headless: heal/remutate/re-execute, soft waivers," >&2
  echo "                  cover art, publish package, S3 upload" >&2
  echo "" >&2
  while true; do
    read -r -p "Choice [1]: " choice || choice=""
    case "${choice:-1}" in
      "" | 1 | m | M | manual | Manual) echo "manual"; return ;;
      2 | f | F | full-auto | Full-auto | auto | e2e) echo "full-auto"; return ;;
      *) echo "Enter 1 (Manual) or 2 (Full-auto)." >&2 ;;
    esac
  done
}

list_assets_audio() {
  python - <<'PY'
from pathlib import Path
exts = {".wav", ".mp3", ".m4a", ".flac", ".ogg", ".aac", ".webm", ".mp4"}
root = Path("ASSETS/input")
if not root.is_dir():
    raise SystemExit(0)
for p in sorted(root.iterdir()):
    if p.is_file() and p.suffix.lower() in exts:
        print(p.as_posix())
PY
}

pick_input_audio() {
  if [[ -n "${MUX_INPUT_AUDIO:-}" ]]; then
    if [[ ! -f "${MUX_INPUT_AUDIO}" ]]; then
      echo "ERROR: MUX_INPUT_AUDIO not found: ${MUX_INPUT_AUDIO}" >&2
      exit 1
    fi
    case "${MUX_INPUT_AUDIO}" in
      ASSETS/input/*) ;;
      *) echo "ERROR: MUX_INPUT_AUDIO must be directly under ASSETS/input/" >&2; exit 1 ;;
    esac
    echo "${MUX_INPUT_AUDIO}"
    return
  fi
  local files=()
  local line
  while IFS= read -r line; do
    [[ -n "$line" ]] && files+=("$line")
  done < <(list_assets_audio)
  if ((${#files[@]} == 0)); then
    echo "ERROR: No audio files in ASSETS/input/ (expected .wav/.mp3/…)" >&2
    exit 1
  fi
  if [[ ! -t 0 ]]; then
    echo "ERROR: Full-auto needs MUX_INPUT_AUDIO when stdin is not a TTY" >&2
    exit 1
  fi
  echo "" >&2
  echo "Source audio (ASSETS/input/)" >&2
  local i
  for i in "${!files[@]}"; do
    printf "  %2d) %s\n" "$((i + 1))" "${files[$i]}" >&2
  done
  echo "" >&2
  while true; do
    read -r -p "Select file [1]: " choice || choice=""
    choice="${choice:-1}"
    if [[ "$choice" =~ ^[0-9]+$ ]] && ((choice >= 1 && choice <= ${#files[@]})); then
      echo "${files[$((choice - 1))]}"
      return
    fi
    echo "Enter a number between 1 and ${#files[@]}." >&2
  done
}

RUN_MODE="$(pick_run_mode)"
export MUX_RUN_MODE="$RUN_MODE"

if [[ "$RUN_MODE" == "full-auto" ]]; then
  export MUX_FULL_AUTO=1
  export MUX_NO_BROWSER=1
  export MUX_DETACH_SERVE=1
  INPUT_PICKED="$(pick_input_audio)"
  export MUX_INPUT_AUDIO="$INPUT_PICKED"
  echo ""
  echo "Full-auto selected — soft automation"
  echo "  input:  ${MUX_INPUT_AUDIO}"
  echo "  logs:   ASSETS/full_auto_console.log"
  echo "  report: ASSETS/executions/<run_id>/operator/EXECUTION_REPORT.md"
  echo "  stop:   python tools/full_auto_daemon_launch.py stop"
  echo "  gui:    http://127.0.0.1:${WEB_PORT} (detached, no browser)"
  echo ""
fi

if [[ "${MUX_NO_BROWSER:-0}" == "1" ]]; then
  # Avoid duplicating --no-browser if already passed.
  _has_no_browser=0
  for _a in "${SERVE_ARGS[@]+"${SERVE_ARGS[@]}"}"; do
    if [[ "$_a" == "--no-browser" ]]; then
      _has_no_browser=1
      break
    fi
  done
  if [[ "$_has_no_browser" == "0" ]]; then
    SERVE_ARGS+=(--no-browser)
  fi
fi

# MUX_FRESH=1 always wins — never resume a stale exec when operator asked for fresh.
if _full_auto_env_set && [[ "${MUX_FRESH:-1}" == "1" ]]; then
  unset MUX_RUN_ID
  export MUX_FRESH=1
  echo "Fresh Full-auto — stopping prior driver + keepalive…" >&2
  python "$ROOT/tools/full_auto_daemon_launch.py" stop || true
fi

E2E_ARGS=(e2e)
if _keepalive_requested; then
  E2E_ARGS+=(keepalive)
fi
if [[ "${MUX_FRESH:-1}" == "1" ]]; then
  E2E_ARGS+=(--fresh)
elif [[ -n "${MUX_RUN_ID:-}" ]]; then
  E2E_ARGS+=(--run-id "${MUX_RUN_ID}")
fi

# Unattended mode: serve runs in its own session so it outlives this shell.
# Foreground `exec serve` dies with the parent (SIGHUP/process-group kill), which
# interrupts in-flight stages, so Full-auto launches must use this path.
if [[ "${MUX_DETACH_SERVE:-0}" == "1" ]]; then
  # --no-keepalive: do not let MUX_KEEPALIVE=1 attach keepalive to server-only
  # start (that races a second driver ahead of the e2e+keepalive call below).
  python "$ROOT/tools/full_auto_daemon_launch.py" server --no-keepalive
  if _full_auto_env_set; then
    python "$ROOT/tools/full_auto_daemon_launch.py" "${E2E_ARGS[@]}"
  fi
  echo "Web GUI → http://127.0.0.1:${WEB_PORT} (detached)"
  if _full_auto_env_set; then
    if _keepalive_requested; then
      echo "Full-auto driver + keepalive detached — watch ASSETS/full_auto_console.log"
    else
      echo "Full-auto driver detached — watch ASSETS/full_auto_console.log"
    fi
  fi
  exit 0
fi

if _full_auto_env_set; then
  # Detach durable Full-auto companion before serve (serve is exec'd and replaces this shell).
  # Start detached driver after a short delay so serve binds first.
  (
    for _ in 1 2 3 4 5 6 7 8 9 10 11 12 13 14 15 16 17 18 19 20; do
      if curl -sf "http://127.0.0.1:${WEB_PORT}/api/health" >/dev/null 2>&1; then
        break
      fi
      sleep 1
    done
    python "$ROOT/tools/full_auto_daemon_launch.py" "${E2E_ARGS[@]}"
  ) >/dev/null 2>&1 &
  disown || true
fi

if ((${#SERVE_ARGS[@]} > 0)); then
  exec python -m interview_mux serve "${SERVE_ARGS[@]}"
fi
exec python -m interview_mux serve

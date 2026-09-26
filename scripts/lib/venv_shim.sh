#!/usr/bin/env bash
# Cross-platform venv layout helpers, sourced by the bootstrap scripts.
#
# POSIX venvs expose <venv>/bin/{python,activate}; Windows venvs expose
# <venv>/Scripts/{python.exe,activate}. Sourcing this keeps the bootstrap
# scripts identical on macOS, Linux, and Windows (Git Bash / MSYS).

# True on Git Bash / MSYS2 / Cygwin.
mux_is_windows() {
  case "$(uname -s)" in
    MINGW* | MSYS* | CYGWIN*) return 0 ;;
    *) return 1 ;;
  esac
}

# Path to the interpreter inside a venv.
mux_venv_python() {
  local venv="$1"
  if [[ -x "$venv/bin/python" ]]; then
    echo "$venv/bin/python"
  else
    echo "$venv/Scripts/python.exe"
  fi
}

# Activate a venv regardless of layout.
mux_venv_activate() {
  local venv="$1"
  # shellcheck source=/dev/null
  if [[ -f "$venv/bin/activate" ]]; then
    source "$venv/bin/activate"
  elif [[ -f "$venv/Scripts/activate" ]]; then
    source "$venv/Scripts/activate"
  else
    echo "ERROR: no activate script in $venv" >&2
    return 1
  fi
}

# Resolve a Python 3.12+ interpreter to build venvs with.
mux_host_python() {
  local py="${PYTHON:-/opt/homebrew/bin/python3.12}"
  if [[ -x "$py" ]]; then
    echo "$py"
    return 0
  fi
  py="$(command -v python3.12 2>/dev/null || true)"
  if [[ -n "$py" ]]; then
    echo "$py"
    return 0
  fi
  if mux_is_windows; then
    # The py launcher knows where the python.org installers put 3.12.
    local win_py
    win_py="$(py -3.12 -c 'import sys; print(sys.executable)' 2>/dev/null || true)"
    if [[ -n "$win_py" ]]; then
      echo "$win_py"
      return 0
    fi
  fi
  command -v python3 2>/dev/null || command -v python 2>/dev/null
}

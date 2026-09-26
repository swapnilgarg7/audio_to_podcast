"""Cross-platform virtualenv layout helpers.

POSIX venvs put the interpreter at ``<venv>/bin/python``; Windows venvs put it
at ``<venv>/Scripts/python.exe``. Every call site that reaches into an isolated
runtime venv goes through here so the repo runs unchanged on both.
"""

from __future__ import annotations

import os
from pathlib import Path

#: Interpreter directory inside a venv for the running platform.
BIN_DIR = "Scripts" if os.name == "nt" else "bin"

_PYTHON_NAMES = ("python.exe", "python3.exe") if os.name == "nt" else ("python", "python3")


def venv_bin(venv_dir: Path | str) -> Path:
    """Return ``<venv_dir>/Scripts`` on Windows, ``<venv_dir>/bin`` elsewhere."""
    return Path(venv_dir) / BIN_DIR


def venv_python(venv_dir: Path | str) -> Path:
    """Return the interpreter path inside ``venv_dir`` for this platform.

    The path is returned whether or not it exists, so callers keep their own
    ``is_file()`` checks and error messages. When several candidate names are
    possible (``python.exe`` / ``python3.exe``) the first existing one wins,
    falling back to the canonical name.
    """
    bin_dir = venv_bin(venv_dir)
    for name in _PYTHON_NAMES:
        candidate = bin_dir / name
        if candidate.is_file():
            return candidate
    return bin_dir / _PYTHON_NAMES[0]


def venv_exe(venv_dir: Path | str, name: str) -> Path:
    """Return a console-script path inside ``venv_dir`` (adds ``.exe`` on Windows)."""
    bin_dir = venv_bin(venv_dir)
    if os.name == "nt" and not name.lower().endswith(".exe"):
        exe = bin_dir / f"{name}.exe"
        if exe.is_file():
            return exe
        return exe
    return bin_dir / name

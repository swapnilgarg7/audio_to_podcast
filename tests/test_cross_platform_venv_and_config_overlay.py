"""Cross-platform venv resolution + the per-machine config overlay.

Covers the two pieces that let this repo run off Apple Silicon: the venv layout
shim (Scripts/ on Windows, bin/ elsewhere) and the gitignored
``config/app.local.json`` layer that keeps host paths out of committed config.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from interview_mux.config import _deep_merge, load_defaults, shipped_defaults
from interview_mux.venv_paths import BIN_DIR, venv_bin, venv_exe, venv_python

# --- venv layout shim -------------------------------------------------------


def test_bin_dir_matches_platform() -> None:
    assert BIN_DIR == ("Scripts" if os.name == "nt" else "bin")


def test_venv_bin_appends_platform_dir(tmp_path: Path) -> None:
    assert venv_bin(tmp_path) == tmp_path / BIN_DIR


def test_venv_python_returns_path_even_when_absent(tmp_path: Path) -> None:
    """Callers keep their own is_file() check and error message."""
    py = venv_python(tmp_path / "nonexistent")
    assert py.parent.name == BIN_DIR
    assert py.name.startswith("python")
    assert not py.exists()


def test_venv_python_prefers_an_existing_interpreter(tmp_path: Path) -> None:
    bin_dir = tmp_path / BIN_DIR
    bin_dir.mkdir(parents=True)
    name = "python.exe" if os.name == "nt" else "python"
    (bin_dir / name).write_text("#!/bin/sh\n", encoding="utf-8")
    assert venv_python(tmp_path) == bin_dir / name


def test_venv_python_accepts_str_path(tmp_path: Path) -> None:
    assert venv_python(str(tmp_path)).parent == tmp_path / BIN_DIR


def test_venv_exe_adds_exe_suffix_on_windows(tmp_path: Path) -> None:
    exe = venv_exe(tmp_path, "pip")
    if os.name == "nt":
        assert exe.name == "pip.exe"
    else:
        assert exe.name == "pip"


def test_local_runtime_uses_the_shim() -> None:
    """resolve_venv_python must not hardcode the posix layout."""
    from interview_mux.local_runtime import resolve_venv_dir, resolve_venv_python

    try:
        py = resolve_venv_python("mmaudio")
    except Exception:
        pytest.skip("mmaudio runtime not installed on this host")
    assert py.parent.name == BIN_DIR
    assert py.parent.parent == resolve_venv_dir("mmaudio")


# --- config overlay ---------------------------------------------------------


def test_deep_merge_recurses_into_nested_dicts() -> None:
    base = {"a": {"b": 1, "c": 2}, "top": "keep"}
    overlay = {"a": {"c": 99}}
    out = _deep_merge(base, overlay)
    assert out == {"a": {"b": 1, "c": 99}, "top": "keep"}
    # inputs are not mutated
    assert base["a"]["c"] == 2


def test_deep_merge_overlay_scalar_replaces_dict() -> None:
    assert _deep_merge({"a": {"b": 1}}, {"a": "x"}) == {"a": "x"}


def test_deep_merge_adds_new_keys() -> None:
    assert _deep_merge({"a": 1}, {"b": 2}) == {"a": 1, "b": 2}


def test_shipped_defaults_ignores_the_local_overlay() -> None:
    """Regression locks must read the committed file, not the merged config."""
    from interview_mux.config import repo_root

    shipped = shipped_defaults()
    on_disk = json.loads(
        (repo_root() / "config" / "app.defaults.json").read_text(encoding="utf-8")
    )
    assert shipped == on_disk


def test_load_defaults_applies_the_overlay_when_present() -> None:
    """load_defaults() == shipped_defaults() only when no overlay exists."""
    from interview_mux.config import repo_root

    overlay_path = repo_root() / "config" / "app.local.json"
    merged = load_defaults()
    if not overlay_path.is_file():
        assert merged == shipped_defaults()
        return
    overlay = json.loads(overlay_path.read_text(encoding="utf-8"))
    # Every scalar the overlay sets must win in the merged result.
    for key, val in overlay.items():
        if key.startswith("_") or isinstance(val, dict):
            continue
        assert merged.get(key) == val
    assert set(shipped_defaults()) <= set(merged)


def test_overlay_is_gitignored() -> None:
    """A committed app.local.json would leak one machine's paths to everyone."""
    import subprocess

    from interview_mux.config import repo_root

    proc = subprocess.run(
        ["git", "check-ignore", "-q", "config/app.local.json"],
        cwd=str(repo_root()),
        capture_output=True,
        check=False,
    )
    assert proc.returncode == 0, "config/app.local.json must stay gitignored"

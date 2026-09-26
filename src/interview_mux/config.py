from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any


def repo_root() -> Path:
    """Repository root (works for src checkout and non-editable pip install)."""
    env = os.environ.get("INTERVIEW_MUX_ROOT")
    if env:
        return Path(env).resolve()
    start = Path.cwd().resolve()
    for parent in [start, *start.parents]:
        if (parent / "pyproject.toml").is_file() and (parent / "config" / "app.defaults.json").is_file():
            return parent
    here = Path(__file__).resolve()
    for parent in [here, *here.parents]:
        if (parent / "pyproject.toml").is_file() and (parent / "config" / "app.defaults.json").is_file():
            return parent
    raise RuntimeError(
        "Cannot find repo root (pyproject.toml + config/app.defaults.json). "
        "Run from the project directory or set INTERVIEW_MUX_ROOT."
    )


def _deep_merge(base: dict[str, Any], overlay: dict[str, Any]) -> dict[str, Any]:
    """Recursively overlay one config dict onto another (overlay wins)."""
    out = dict(base)
    for key, val in overlay.items():
        prev = out.get(key)
        if isinstance(prev, dict) and isinstance(val, dict):
            out[key] = _deep_merge(prev, val)
        else:
            out[key] = val
    return out


def shipped_defaults() -> dict[str, Any]:
    """`app.defaults.json` alone — no per-machine overlay, no secrets.

    Regression tests that lock what the project *ships* must read this, not
    ``merged_config()``: otherwise any operator with a legitimate
    ``app.local.json`` (different GPU tier, runtime venvs on another volume)
    fails the suite, and the override layer becomes unusable.
    """
    path = repo_root() / "config" / "app.defaults.json"
    return json.loads(path.read_text(encoding="utf-8"))


def load_defaults() -> dict[str, Any]:
    """app.defaults.json, with gitignored app.local.json layered on top.

    ``app.defaults.json`` is committed and shared across machines, so it must
    stay machine-neutral. Per-machine values — where the heavy local runtime
    venvs and model weights live, which device tier to target — belong in
    ``config/app.local.json``, which is gitignored. Keys merge recursively, so
    the override file only names what actually differs.
    """
    cfg = shipped_defaults()
    local_path = repo_root() / "config" / "app.local.json"
    if local_path.is_file():
        try:
            local = json.loads(local_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"Invalid JSON in {local_path}: {exc}") from exc
        if isinstance(local, dict):
            cfg = _deep_merge(cfg, local)
    return cfg


def load_secrets() -> dict[str, str]:
    """Load secrets.env without polluting os.environ."""
    path = repo_root() / "config" / "secrets" / "secrets.env"
    out: dict[str, str] = {}
    if not path.is_file():
        return out
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            continue
        key, _, val = line.partition("=")
        out[key.strip()] = val.strip().strip('"').strip("'")
    return out


def merged_config() -> dict[str, Any]:
    cfg = load_defaults()
    secrets = load_secrets()
    if secrets.get("INPUT_AUDIO_PATH"):
        cfg["input_audio_path"] = secrets["INPUT_AUDIO_PATH"]
    cfg["secrets"] = secrets
    return cfg


def get_model(stage_key: str) -> str:
    from interview_mux.model_registry import resolve_model

    cfg = merged_config()
    models = cfg.get("models") or {}
    if isinstance(models.get(stage_key), str):
        return str(models[stage_key])
    resolved = resolve_model(stage_key, "primary")
    return resolved.model_id or secrets_model_fallback(cfg, stage_key)


def secrets_model_fallback(cfg: dict[str, Any], stage_key: str) -> str:
    secrets = cfg.get("secrets") or {}
    return secrets.get("OPENAI_MODEL", "gpt-4o-mini")


def require_secret(key: str) -> str:
    val = (merged_config().get("secrets") or {}).get(key, "")
    if not val:
        raise RuntimeError(f"Missing required secret: {key} in config/secrets/secrets.env")
    return val


def cursor_api_key() -> str:
    """CURSOR_API_KEY for CURSOR_EXECUTE / cursor-sdk (env overrides secrets.env)."""
    env_key = os.environ.get("CURSOR_API_KEY", "").strip()
    if env_key:
        return env_key
    return (load_secrets().get("CURSOR_API_KEY") or "").strip()


def huggingface_hub_token() -> str:
    """HF Hub token for public-model downloads (env overrides secrets.env).

    Free Hugging Face accounts are sufficient for public repos (CLAP, MLX).
    Accepts ``HF_TOKEN`` or ``HUGGING_FACE_HUB_TOKEN``.
    """
    for key in ("HF_TOKEN", "HUGGING_FACE_HUB_TOKEN"):
        env_val = os.environ.get(key, "").strip()
        if env_val:
            return env_val
    secrets = load_secrets()
    for key in ("HF_TOKEN", "HUGGING_FACE_HUB_TOKEN"):
        val = (secrets.get(key) or "").strip()
        if val:
            return val
    return ""

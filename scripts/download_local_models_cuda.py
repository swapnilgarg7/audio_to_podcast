#!/usr/bin/env python3
"""Download model weights for the CUDA/CPU local stacks (Windows / Linux hosts).

The MLX downloaders (``download_local_llm.py`` / ``download_local_speech.py``)
fetch Apple-Silicon weights. This fetches the equivalents the faster-whisper,
transformers and MusicGen backends load, into the directories
``config/app.local.json`` points at, and writes the selection manifests the
pipeline reads (``ASSETS/local_speech/selection.json``,
``ASSETS/local_llm/selection.json``).

  python scripts/download_local_models_cuda.py            # everything
  python scripts/download_local_models_cuda.py --stt      # one stack
  python scripts/download_local_models_cuda.py --verify   # report, download nothing
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from interview_mux.config import merged_config, repo_root  # noqa: E402
from interview_mux.local_runtime import resolve_venv_python  # noqa: E402

#: 3B instruct model, ungated mirror — ~2.5 GB in 4-bit NF4 on a 6 GB card.
DEFAULT_LLM_MODEL = "unsloth/Llama-3.2-3B-Instruct"
#: faster-whisper resolves this size name to a CTranslate2 repo on the hub.
DEFAULT_STT_MODEL = "large-v3-turbo"


def _run(py: Path, code: str, *, env_extra: dict[str, str] | None = None) -> int:
    import os

    env = os.environ.copy()
    if env_extra:
        env.update(env_extra)
    proc = subprocess.run(
        [str(py), "-c", code], cwd=str(ROOT), env=env, check=False
    )
    return proc.returncode


def download_stt(*, verify_only: bool) -> bool:
    try:
        py = resolve_venv_python("speech")
    except Exception as exc:
        print(f"SKIP stt: {exc}")
        return False
    if verify_only:
        return _run(py, "import faster_whisper; print('faster-whisper OK')") == 0
    print(f"--- STT weights ({DEFAULT_STT_MODEL}) ---", flush=True)
    code = (
        "import sys; sys.path.insert(0, 'tools')\n"
        "import stt_backend_faster_whisper as b\n"
        "b._add_cuda_dll_dirs()\n"
        "from faster_whisper import WhisperModel\n"
        "dev, ct = b._device_and_compute_type()\n"
        f"WhisperModel({DEFAULT_STT_MODEL!r}, device=dev, compute_type=ct)\n"
        "print('STT weights ready on', dev, ct)\n"
    )
    return _run(py, code) == 0


def download_llm(*, verify_only: bool) -> bool:
    try:
        py = resolve_venv_python("mlx")
    except Exception as exc:
        print(f"SKIP llm: {exc}")
        return False
    if verify_only:
        return _run(py, "import transformers; print('transformers OK')") == 0

    from interview_mux.local_llm_config import _resolve_models_dir, repo_slug

    models_dir = _resolve_models_dir(merged_config())
    dest = models_dir / repo_slug(DEFAULT_LLM_MODEL)
    print(f"--- LLM weights ({DEFAULT_LLM_MODEL}) -> {dest} ---", flush=True)
    code = (
        "from huggingface_hub import snapshot_download\n"
        f"snapshot_download({DEFAULT_LLM_MODEL!r}, local_dir=r{str(dest)!r},\n"
        "    allow_patterns=['*.json','*.safetensors','*.txt','*.model'])\n"
        "print('LLM weights ready')\n"
    )
    return _run(py, code) == 0


def download_musicgen(*, verify_only: bool) -> bool:
    from interview_mux.musicgen_runner import musicgen_cfg, musicgen_hf_home, musicgen_venv_python

    py = musicgen_venv_python()
    if py is None:
        print("SKIP musicgen: venv missing")
        return False
    if verify_only:
        return _run(py, "import transformers, torch; print('musicgen deps OK')") == 0

    cfg = musicgen_cfg()
    models = list(cfg.get("prefetch_models") or [cfg.get("model_id")])
    cache = musicgen_hf_home()
    cache.mkdir(parents=True, exist_ok=True)
    print(f"--- MusicGen weights {models} -> {cache} ---", flush=True)
    code = (
        "from transformers import AutoProcessor, MusicgenForConditionalGeneration\n"
        f"for mid in {models!r}:\n"
        "    print('prefetch', mid, flush=True)\n"
        "    AutoProcessor.from_pretrained(mid)\n"
        "    MusicgenForConditionalGeneration.from_pretrained(mid)\n"
        "    print('ok', mid, flush=True)\n"
    )
    return _run(py, code, env_extra={"HF_HOME": str(cache)}) == 0


def write_manifests() -> None:
    """Record what this host actually selected, for the runners to read back."""
    from interview_mux.hardware_detect import detect_torch_device, system_memory_gb

    now = datetime.now(timezone.utc).isoformat()
    device = detect_torch_device()

    speech = repo_root() / "ASSETS" / "local_speech" / "selection.json"
    speech.parent.mkdir(parents=True, exist_ok=True)
    speech.write_text(
        json.dumps(
            {
                "stt_model_id": DEFAULT_STT_MODEL,
                "stt_backend": "faster-whisper",
                # pyannote needs a HF token + accepted gates; the backend
                # fails open to spk_0 when it is not installed.
                "diarization_mode": "pyannote",
                "diarization_model_id": "pyannote/speaker-diarization-3.1",
                "s2s_model_id": "",
                "capabilities": ["stt"],
                "selection_source": "cuda_bootstrap",
                "device": device,
                "ram_gb": round(system_memory_gb(), 1),
                "selected_at": now,
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    print(f"wrote {speech}")

    llm = repo_root() / "ASSETS" / "local_llm" / "selection.json"
    llm.parent.mkdir(parents=True, exist_ok=True)
    llm.write_text(
        json.dumps(
            {
                "model_id": DEFAULT_LLM_MODEL,
                "backend": "transformers",
                "quantization": "nf4",
                "selection_source": "cuda_bootstrap",
                "device": device,
                "ram_gb": round(system_memory_gb(), 1),
                "selected_at": now,
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    print(f"wrote {llm}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--stt", action="store_true")
    ap.add_argument("--llm", action="store_true")
    ap.add_argument("--musicgen", action="store_true")
    ap.add_argument("--verify", action="store_true", help="Report only, download nothing")
    args = ap.parse_args()

    selected = args.stt or args.llm or args.musicgen
    want_stt = args.stt or not selected
    want_llm = args.llm or not selected
    want_mg = args.musicgen or not selected

    ok = True
    if want_stt:
        ok = download_stt(verify_only=args.verify) and ok
    if want_llm:
        ok = download_llm(verify_only=args.verify) and ok
    if want_mg:
        ok = download_musicgen(verify_only=args.verify) and ok
    if not args.verify:
        write_manifests()
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())

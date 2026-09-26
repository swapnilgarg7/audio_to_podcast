#!/usr/bin/env python3
"""Local MLX STT CLI — stdout JSON words contract for transcribe_local."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any


def _mlx_available() -> bool:
    """True when the Apple-Silicon MLX audio stack is importable."""
    try:
        import mlx_audio  # noqa: F401
    except ImportError:
        return False
    return True


def _fw_backend():
    """Import the faster-whisper backend (CUDA/CPU hosts without MLX)."""
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import stt_backend_faster_whisper as backend

    return backend


def _verify() -> int:
    if _mlx_available():
        print(json.dumps({"ok": True, "stack": "mlx-audio"}))
        return 0
    try:
        payload = _fw_backend().verify()
    except ImportError as exc:
        print(json.dumps({"error": f"no STT backend: mlx_audio and faster_whisper missing ({exc})"}))
        return 1
    print(json.dumps(payload))
    return 0 if payload.get("ok") else 1


def _segments_to_words(segments: list[Any]) -> list[dict[str, Any]]:
    words: list[dict[str, Any]] = []
    for seg in segments:
        if not isinstance(seg, dict):
            continue
        text = str(seg.get("text") or seg.get("Content") or "").strip()
        if not text:
            continue
        start = seg.get("start_time", seg.get("Start", 0))
        end = seg.get("end_time", seg.get("End", start))
        try:
            start_f = float(start)
            end_f = float(end)
        except (TypeError, ValueError):
            start_f, end_f = 0.0, 0.0
        spk = seg.get("speaker_id", seg.get("Speaker"))
        if spk is not None:
            spk = f"spk_{spk}" if str(spk).isdigit() else str(spk)
        for token in text.split():
            words.append(
                {
                    "text": token,
                    "start_ms": int(start_f * 1000),
                    "end_ms": int(end_f * 1000),
                    "speaker_id": spk,
                    "confidence": None,
                }
            )
    return words


def _fallback_whisper_model(model_id: str) -> str:
    """mlx-audio 0.4.8 Whisper ids need the ``-asr-fp16`` processor package."""
    raw = (model_id or "").strip()
    if "whisper-large-v3-turbo-asr-fp16" in raw:
        return raw
    if "whisper-large-v3-turbo" in raw.lower() or "whisper" in raw.lower():
        return "mlx-community/whisper-large-v3-turbo-asr-fp16"
    return "mlx-community/whisper-large-v3-turbo-asr-fp16"


def _transcribe_vibevoice(audio: Path, model_id: str) -> dict[str, Any]:
    try:
        from mlx_audio.stt.utils import load_model
    except ImportError as exc:
        raise RuntimeError(f"mlx_audio missing: {exc}") from exc

    try:
        model = load_model(model_id)
    except Exception:
        return _transcribe_whisper(audio, _fallback_whisper_model(model_id))

    result = model.generate(str(audio), max_tokens=8192, temperature=0.0)
    text = str(getattr(result, "text", "") or "").strip()
    segments = getattr(result, "segments", None) or []
    if not segments and text.startswith("["):
        try:
            segments = json.loads(text)
            text = " ".join(
                str(s.get("Content") or s.get("text") or "")
                for s in segments
                if isinstance(s, dict)
            ).strip()
        except json.JSONDecodeError:
            segments = []
    norm_segments: list[dict[str, Any]] = []
    for seg in segments:
        if not isinstance(seg, dict):
            continue
        norm_segments.append(
            {
                "text": str(seg.get("text") or seg.get("Content") or ""),
                "start_time": seg.get("start_time", seg.get("Start")),
                "end_time": seg.get("end_time", seg.get("End")),
                "speaker_id": seg.get("speaker_id", seg.get("Speaker")),
            }
        )
    words = _segments_to_words(norm_segments)
    return {"text": text, "words": words, "segments": norm_segments}


def _words_from_whisper_segments(segments: list[Any]) -> list[dict[str, Any]]:
    words: list[dict[str, Any]] = []
    for seg in segments:
        if not isinstance(seg, dict):
            continue
        for item in seg.get("words") or []:
            if not isinstance(item, dict):
                continue
            token = str(item.get("word") or item.get("text") or "").strip()
            if not token:
                continue
            start = float(item.get("start", item.get("start_time", seg.get("start", 0))))
            end = float(item.get("end", item.get("end_time", start)))
            words.append(
                {
                    "text": token,
                    "start_ms": int(start * 1000),
                    "end_ms": int(end * 1000),
                    "speaker_id": "spk_0",
                    "confidence": item.get("probability", item.get("confidence")),
                }
            )
    return words


def _transcribe_whisper(audio: Path, model_id: str) -> dict[str, Any]:
    import contextlib
    import io
    import tempfile

    from mlx_audio.stt.generate import generate_transcription

    model_id = _fallback_whisper_model(model_id)
    buf = io.StringIO()
    # mlx_audio always writes `{output_path}.txt`. Default output_path="" → repo-root
    # `.txt` (see mlx_audio.stt.generate.save_as_txt). Pin a temp prefix so STT
    # never pollutes the process cwd.
    with tempfile.TemporaryDirectory(prefix="mux_stt_") as tmp:
        out_prefix = str(Path(tmp) / "transcript")
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
            result = generate_transcription(
                model=model_id,
                audio=str(audio),
                output_path=out_prefix,
                format="txt",
                verbose=False,
                word_timestamps=True,
            )
    text = str(getattr(result, "text", result) or "").strip()
    segments = list(getattr(result, "segments", None) or [])
    words = _words_from_whisper_segments(segments)
    if not words:
        for item in getattr(result, "words", None) or []:
            if isinstance(item, dict):
                start = float(item.get("start", item.get("start_time", 0)))
                end = float(item.get("end", item.get("end_time", start)))
                words.append(
                    {
                        "text": str(item.get("word") or item.get("text") or ""),
                        "start_ms": int(start * 1000),
                        "end_ms": int(end * 1000),
                        "speaker_id": "spk_0",
                        "confidence": item.get("confidence"),
                    }
                )
    if not words and text:
        words = [
            {
                "text": tok,
                "start_ms": 0,
                "end_ms": 0,
                "speaker_id": "spk_0",
                "confidence": None,
            }
            for tok in text.split()
        ]
    return {"text": text, "words": words}


def transcribe(audio: Path, model_id: str, *, diarization_mode: str) -> dict[str, Any]:
    if not _mlx_available():
        return _fw_backend().transcribe(audio, model_id, diarization_mode=diarization_mode)
    if "VibeVoice" in model_id or "MOSS" in model_id or diarization_mode == "integrated":
        return _transcribe_vibevoice(audio, model_id)
    return _transcribe_whisper(audio, model_id)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--verify", action="store_true")
    parser.add_argument("--audio", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--model", default="mlx-community/whisper-large-v3-turbo-asr-fp16")
    parser.add_argument("--diarization-mode", default="sortformer")
    args = parser.parse_args()

    if args.verify:
        return _verify()

    if not args.audio or not args.audio.is_file():
        print(json.dumps({"error": "missing --audio"}))
        return 1

    try:
        payload = transcribe(args.audio, args.model, diarization_mode=args.diarization_mode)
    except Exception as exc:
        print(json.dumps({"error": str(exc)[:500]}))
        return 1

    out = json.dumps(payload, indent=2)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(out + "\n", encoding="utf-8")
    else:
        print(out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""MLX local LLM infer — stdin JSON {system,user,max_tokens,model_path} → stdout JSON."""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path


def main() -> int:
    try:
        raw_in = sys.stdin.read()
        payload = json.loads(raw_in or "{}")
    except json.JSONDecodeError:
        print(json.dumps({"error": "invalid stdin JSON"}))
        return 1

    system = str(payload.get("system") or "")
    user = str(payload.get("user") or "")
    max_tokens = int(payload.get("max_tokens") or 768)
    model_path = payload.get("model_path")

    try:
        from mlx_lm import generate, load

        mlx_ok = True
    except ImportError:
        mlx_ok = False

    if model_path:
        path = Path(str(model_path))
    else:
        print(json.dumps({"error": "model_path required"}))
        return 1

    if not path.is_dir():
        print(json.dumps({"error": f"weights missing at {path}"}))
        return 1

    if not mlx_ok:
        # No MLX on this host (Windows / Linux / Intel Mac) — use transformers.
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        try:
            import local_llm_backend_transformers as backend
        except ImportError as exc:
            print(json.dumps({"error": f"no local LLM backend: mlx_lm and transformers missing ({exc})"}))
            return 1
        try:
            text, meta = backend.generate(
                system=system, user=user, max_tokens=max_tokens, model_path=path
            )
        except Exception as exc:
            print(json.dumps({"error": f"transformers infer failed: {str(exc)[:400]}"}))
            return 1
        print(json.dumps({"text": text, "meta": meta}))
        return 0

    model, tokenizer = load(str(path))
    messages = [{"role": "system", "content": system}, {"role": "user", "content": user}]
    if getattr(tokenizer, "chat_template", None):
        prompt = tokenizer.apply_chat_template(messages, tokenize=False, add_generation_prompt=True)
    else:
        prompt = f"{system}\n\nUser:\n{user}\n\nAssistant:\n"

    started = time.perf_counter()
    text = generate(model, tokenizer, prompt=prompt, max_tokens=max_tokens, verbose=False)
    latency_ms = int((time.perf_counter() - started) * 1000)
    out_text = text if isinstance(text, str) else str(text)
    meta = {
        "model_path": str(path),
        "latency_ms": latency_ms,
        "tokens_approx": max(1, len(out_text) // 4),
        "max_tokens": max_tokens,
    }
    print(json.dumps({"text": out_text.strip(), "meta": meta}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

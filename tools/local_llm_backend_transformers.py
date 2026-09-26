#!/usr/bin/env python3
"""CUDA/CPU local-LLM backend (transformers) — same stdin/stdout contract as MLX.

Used on hosts without MLX (Windows / Linux / Intel Mac). ``local_llm_infer.py``
prefers ``mlx_lm`` when it imports and falls back here, so the
``{"text", "meta"}`` contract read by ``local_llm_runner.generate_local_chat``
is unchanged either way.

Weights load 4-bit NF4 when bitsandbytes is present (a 3B instruct model then
fits in ~2.5 GB of VRAM), else fp16 on GPU, else fp32 on CPU.
"""

from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Any


def _quantization_config():
    """4-bit NF4 config, or None when bitsandbytes is unavailable/disabled."""
    if (os.environ.get("MUX_LOCAL_LLM_4BIT") or "").strip().lower() in {"0", "off", "no"}:
        return None
    try:
        import torch
        from transformers import BitsAndBytesConfig
    except ImportError:
        return None
    try:
        import bitsandbytes  # noqa: F401
    except ImportError:
        return None
    return BitsAndBytesConfig(
        load_in_4bit=True,
        bnb_4bit_quant_type="nf4",
        bnb_4bit_use_double_quant=True,
        # sm_75 (GTX 16xx / T4) has no bf16 path, so compute in fp16.
        bnb_4bit_compute_dtype=torch.float16,
    )


def verify() -> dict[str, Any]:
    try:
        import torch
        import transformers  # noqa: F401
    except ImportError as exc:
        return {"error": f"transformers missing: {exc}"}
    quant = _quantization_config()
    return {
        "ok": True,
        "stack": "transformers",
        "device": "cuda" if torch.cuda.is_available() else "cpu",
        "quantization": "nf4" if quant is not None else "none",
    }


def _load(model_path: Path):
    import torch
    from transformers import AutoModelForCausalLM, AutoTokenizer

    tokenizer = AutoTokenizer.from_pretrained(str(model_path))
    quant = _quantization_config()
    kwargs: dict[str, Any] = {}
    if quant is not None and torch.cuda.is_available():
        kwargs["quantization_config"] = quant
        kwargs["device_map"] = "auto"
    elif torch.cuda.is_available():
        kwargs["torch_dtype"] = torch.float16
        kwargs["device_map"] = "auto"
    else:
        kwargs["torch_dtype"] = torch.float32
    model = AutoModelForCausalLM.from_pretrained(str(model_path), **kwargs)
    model.eval()
    return model, tokenizer


def generate(
    *, system: str, user: str, max_tokens: int, model_path: Path
) -> tuple[str, dict[str, Any]]:
    """Run one chat completion; return (text, meta) matching the MLX path."""
    import torch

    model, tokenizer = _load(model_path)
    messages = [{"role": "system", "content": system}, {"role": "user", "content": user}]
    if getattr(tokenizer, "chat_template", None):
        prompt = tokenizer.apply_chat_template(
            messages, tokenize=False, add_generation_prompt=True
        )
    else:
        prompt = f"{system}\n\nUser:\n{user}\n\nAssistant:\n"

    inputs = tokenizer(prompt, return_tensors="pt")
    device = next(model.parameters()).device
    inputs = {k: v.to(device) for k, v in inputs.items()}
    prompt_tokens = int(inputs["input_ids"].shape[-1])

    started = time.perf_counter()
    with torch.inference_mode():
        out = model.generate(
            **inputs,
            max_new_tokens=max_tokens,
            do_sample=False,
            pad_token_id=tokenizer.pad_token_id or tokenizer.eos_token_id,
        )
    latency_ms = int((time.perf_counter() - started) * 1000)

    # Decode only the continuation so the prompt never leaks into the payload.
    generated = out[0][prompt_tokens:]
    text = tokenizer.decode(generated, skip_special_tokens=True)
    meta = {
        "model_path": str(model_path),
        "latency_ms": latency_ms,
        "tokens_approx": int(generated.shape[-1]),
        "max_tokens": max_tokens,
        "stack": "transformers",
        "device": str(device),
    }
    return text.strip(), meta

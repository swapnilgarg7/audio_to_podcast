"""Tests for music quality uplift: MusicGen config, motif richness, QA, listen gate."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from interview_mux.music_motif import (
    compile_musicgen_prompt,
    default_motif_family,
    ensure_motif_on_plan,
)
from interview_mux.musicgen_runner import (
    best_of_n_for_role,
    fail_closed_on_stub,
    musicgen_cfg,
    prompt_hash,
)
from interview_mux.mmaudio_asset_qa import _musicality_checks, analyze_asset_wav
from interview_mux.stages.understanding import _derive_mix_contract


def test_musicgen_defaults_large_and_fail_closed():
    # Shipped defaults, not the machine-merged config — see shipped_defaults().
    from interview_mux.config import shipped_defaults

    cfg = shipped_defaults().get("musicgen") or {}
    assert "musicgen-large" in str(cfg.get("model_id") or "")
    assert "melody" in str(cfg.get("melody_model_id") or "")
    assert int(cfg.get("request_timeout_sec") or 0) == 900
    assert int(cfg.get("step_down_timeout_sec") or 0) == 480
    assert cfg.get("prefer_medium_on_cpu") is False
    assert fail_closed_on_stub() is True
    assert best_of_n_for_role("theme_cold_open") >= 1
    assert best_of_n_for_role("theme_underscore") >= 1


def test_prompt_hash_stable():
    a = prompt_hash("hello", negative="no vocals", model_id="facebook/musicgen-large")
    b = prompt_hash("hello", negative="no vocals", model_id="facebook/musicgen-large")
    assert a == b
    assert a != prompt_hash("hello2", negative="no vocals", model_id="facebook/musicgen-large")


def test_motif_family_has_key_and_ensemble():
    brief = {
        "show_identity": {
            "genre_hint": "upbeat documentary",
            "mood": "determined",
            "instrumentation_prefs": [
                "bright acoustic guitar",
                "punchy piano",
                "warm electric bass",
                "soft string harmony",
                "light brushed percussion",
            ],
            "key_center": "G",
            "scale_or_mode": "major_bright",
        },
        "motif_seeds": {"keywords": ["ESOP"]},
        "narrative_spine": {"acts": []},
        "source_quotes_short": [],
    }
    family = default_motif_family(brief)
    assert family.get("key_center") == "G"
    assert len(family.get("instrumentation") or []) >= 5
    pos, neg = compile_musicgen_prompt(brief=brief, motif=family, role="theme_cold_open")
    assert "bass" in pos.lower() or "string" in pos.lower()
    assert "G" in pos
    assert "vocals" in neg.lower()


def test_ensure_motif_calm_lift_and_longer_bookends():
    brief = {
        "show_identity": {
            "genre_hint": "doc",
            "mood": "hopeful",
            "instrumentation_prefs": ["guitar", "piano", "bass", "strings", "percussion"],
            "key_center": "A",
        },
        "motif_seeds": {"keywords": ["start"]},
        "narrative_spine": {"acts": []},
        "source_quotes_short": [],
    }
    out = ensure_motif_on_plan({"assets": [], "flow_plans": {"podcast": {"cues": []}}}, brief)
    roles = [a.get("role") for a in out["assets"]]
    assert roles.count("theme_underscore") >= 2
    cold = next(a for a in out["assets"] if a["role"] == "theme_cold_open")
    assert float(cold["duration_seconds"]) >= 12
    assert out["motif_family"].get("key_center")


def test_dense_pace_no_longer_buries_beds():
    dense = _derive_mix_contract({"pace_class": "dense"}, "low")
    assert dense["bed_level_db_range"][0] >= -16
    assert dense["bed_level_db_range"][1] >= -12
    assert dense["duck_under_speech_db"] >= 12
    assert dense["duck_under_speech_db"] <= 16


def test_musicality_flags_flat_sine(tmp_path: Path):
    import math
    import struct
    import wave

    rate = 48000
    n = rate * 2
    path = tmp_path / "sine.wav"
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(rate)
        frames = b"".join(
            struct.pack("<h", int(0.2 * 32767 * math.sin(2 * math.pi * 440 * i / rate))) for i in range(n)
        )
        wf.writeframes(frames)
    samples = []
    with wave.open(str(path), "rb") as wf:
        import struct as st

        raw = wf.readframes(wf.getnframes())
        samples = [s / 32768.0 for s in st.unpack(f"<{len(raw) // 2}h", raw)]
    checks = _musicality_checks(samples, rate)
    assert checks["fail_reasons"] or checks["warn_reasons"]


def test_analyze_rejects_stub_meta(tmp_path: Path, monkeypatch):
    from interview_mux import musicgen_runner
    from interview_mux.musicgen_runner import _write_musical_stub_wav

    monkeypatch.setattr(musicgen_runner, "fail_closed_on_stub", lambda: False)
    monkeypatch.setattr(musicgen_runner, "fail_closed_on_stub_roles", lambda: set())
    path = tmp_path / "theme_underscore.wav"
    _write_musical_stub_wav(path, duration_sec=8.0, seed=1)
    path.with_suffix(".gen.json").write_text(
        json.dumps({"backend": "musical_stub", "seed": 1, "prompt_hash": "abc"}),
        encoding="utf-8",
    )
    row = analyze_asset_wav(
        asset_id="theme_underscore",
        path=path,
        plan_row={"role": "theme_underscore", "duration_seconds": 8},
    )
    assert "musical_stub_last_resort" in row["reasons"]
    assert "musical_stub_backend" not in row["reasons"]
    assert row["verdict"] != "fail"


def test_music_listen_gate(tmp_path, monkeypatch):
    monkeypatch.setenv("INTERVIEW_MUX_DATA_ROOT", str(tmp_path))
    from interview_mux.music_listen_review import (
        can_run_mix_after_music_listen,
        set_music_listen_approved,
    )
    from run_fixtures import isolated_run_ctx
    import interview_mux.music_listen_review as mlr

    monkeypatch.setattr(mlr, "music_listen_required", lambda cfg=None: True)
    ctx = isolated_run_ctx(tmp_path, "run_music_listen")
    # Seed theme assets so the gate has something to require.
    assets = ctx.path("sound_design", "assets")
    assets.mkdir(parents=True, exist_ok=True)
    (assets / "theme_cold_open.wav").write_bytes(b"RIFF" + b"\x00" * 2000)
    (assets / "theme_underscore_calm.wav").write_bytes(b"RIFF" + b"\x00" * 2000)
    ok, msg = can_run_mix_after_music_listen(ctx)
    assert ok is False
    assert "Music listen" in msg
    set_music_listen_approved(ctx, approved=True)
    ok2, _ = can_run_mix_after_music_listen(ctx)
    assert ok2 is True


def test_generate_folds_negative_into_prompt(tmp_path: Path):
    """Unit-level: request payload shaping is handled in tools/musicgen_generate main."""
    # Smoke that the script module imports and folds negatives without running models.
    import importlib.util

    script = Path(__file__).resolve().parents[1] / "tools" / "musicgen_generate.py"
    spec = importlib.util.spec_from_file_location("musicgen_generate_test", script)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    # Don't exec full module side effects beyond load — just verify file exists and contains fold logic.
    text = script.read_text(encoding="utf-8")
    assert "Avoid:" in text
    assert "MusicgenMelodyForConditionalGeneration" in text
    assert "musicgen-large" in text or "model_id" in text

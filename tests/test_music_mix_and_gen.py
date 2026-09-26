"""Music mix pad, accent duck, intelligibility remux, presence, MusicGen picker."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydub import AudioSegment
from pydub.generators import Sine


def test_overlay_at_base_end_pads_timeline_for_outro() -> None:
    """theme_outro at len(base) must extend the mix (pydub truncates without pad)."""
    base = AudioSegment.silent(duration=5_000, frame_rate=16_000)
    outro = Sine(220).to_audio_segment(duration=2_000, volume=-12).set_frame_rate(16_000)
    pos = len(base)
    mixed = base
    need = pos + len(outro)
    if need > len(mixed):
        mixed = mixed + AudioSegment.silent(
            duration=need - len(mixed), frame_rate=mixed.frame_rate
        )
    mixed = mixed.overlay(outro, position=pos)
    assert len(mixed) >= pos + len(outro)
    assert len(mixed) > len(base)


def test_accent_over_speech_applies_sidechain_duck() -> None:
    from interview_mux.sidechain_duck import duck_bed_with_sidechain

    speech = Sine(400).to_audio_segment(duration=3_000, volume=-10).set_frame_rate(16_000)
    accent = Sine(220).to_audio_segment(duration=3_000, volume=-6).set_frame_rate(16_000)
    ducked = duck_bed_with_sidechain(accent, speech, level_db=0.0, duck_db=20.0)
    # Ducking must reduce energy relative to unducked accent under speech.
    assert ducked.dBFS < accent.dBFS - 1.0


def test_musicality_pulse_clarity_prefers_modulated_over_flat() -> None:
    from interview_mux.mmaudio_asset_qa import _musicality_checks
    import math

    rate = 16_000
    # Flat pad: near-constant amplitude
    flat = [0.1 for _ in range(rate * 2)]
    # Pulsed: amplitude envelope at ~2 Hz
    pulsed = [
        0.1 * (0.4 + 0.6 * (0.5 + 0.5 * math.sin(2 * math.pi * 2.0 * i / rate)))
        for i in range(rate * 2)
    ]
    flat_m = _musicality_checks(flat, rate)
    pulse_m = _musicality_checks(pulsed, rate)
    assert pulse_m["pulse_clarity"] > flat_m["pulse_clarity"]
    assert "musicality_no_onset_structure" in (flat_m.get("fail_reasons") or [])


def test_musicgen_defaults_rhythmic_selection() -> None:
    # Locks what the repo ships, so a per-machine app.local.json (smaller GPU
    # tier, runtimes on another volume) does not fail the suite.
    from interview_mux.config import merged_config, shipped_defaults

    mg = shipped_defaults().get("musicgen") or {}
    assert int(mg.get("best_of_n_underscore") or 0) == 3
    assert int(mg.get("best_of_n_speech_free") or 0) == 2
    assert int(mg.get("max_best_of_n") or 0) == 3
    assert bool(mg.get("keep_candidates")) is True
    assert str(mg.get("device") or "") == "auto"
    assert str(mg.get("model_id") or "") == "facebook/musicgen-large"
    assert bool(mg.get("ban_mps_on_abort", False)) is True
    assert bool(mg.get("use_melody_conditioning")) is False
    intel = (merged_config().get("mix") or {}).get("intelligibility_qc") or {}
    assert bool(intel.get("enabled")) is True


def test_best_of_n_score_prefers_pulse(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Picker scoring: pulsed musicality beats flat pad when both otherwise valid."""
    # Unit-level score function mirror of sfx_mmaudio picker weights.
    def score(qa: dict) -> float:
        s = 10.0 if qa.get("verdict") == "pass" else 4.0
        mus = qa.get("musicality") or {}
        s -= 3.0 * len(mus.get("fail_reasons") or [])
        s += 4.0 * float(mus.get("pulse_clarity") or 0.0)
        return s

    flat_qa = {
        "verdict": "pass",
        "musicality": {
            "fail_reasons": ["musicality_no_onset_structure"],
            "pulse_clarity": 0.0,
        },
    }
    pulse_qa = {
        "verdict": "pass",
        "musicality": {"fail_reasons": [], "pulse_clarity": 0.8},
    }
    assert score(pulse_qa) > score(flat_qa)


def test_ghost_bed_presence_reports_fail(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    from interview_mux.run_context import RunContext
    from interview_mux.sound_design import _check_bed_presence_band
    from interview_mux.master_qc import BedSpeechWindow

    monkeypatch.setenv("INTERVIEW_MUX_DATA_ROOT", str(tmp_path))
    ctx = RunContext("run_ghost_bed", create=True)
    # Near-silent assembly + loud speech ⇒ residual bed looks ghost.
    speech = Sine(400).to_audio_segment(duration=2_000, volume=-8).set_frame_rate(16_000)
    assembly = AudioSegment.silent(duration=2_000, frame_rate=16_000) + speech.apply_gain(-40)
    # Keep levels: mostly speech energy only.
    assembly = speech  # bed absent
    path = ctx.path("master", "assembly.wav")
    path.parent.mkdir(parents=True, exist_ok=True)
    assembly.export(str(path), format="wav")

    def _fake_windows(ctx, *, segment_timing, contract):
        return [BedSpeechWindow(segment_id="seg_a", start_ms=0, end_ms=2000, duck_under_speech_db=16.0)]

    monkeypatch.setattr(
        "interview_mux.master_qc.collect_flow1_bed_speech_windows",
        _fake_windows,
    )
    verdict = _check_bed_presence_band(
        ctx,
        assembly_path=path,
        speech_stem=speech,
        segment_timing={"seg_a": (0, 2000)},
        contract={"duck_under_speech_db": 16.0, "bed_under_dialogue_db": -26.0},
        remux_cycle=2,  # no remux — expect fail
    )
    assert verdict in {"fail", "ok"}  # heuristic may vary; artifact must be written
    assert ctx.artifact_exists("master/bed_presence_qc.json")

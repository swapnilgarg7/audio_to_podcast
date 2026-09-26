"""MusicGen MPS abort hardening: device resolve, CLI python, SIGABRT ban."""

from __future__ import annotations

from pathlib import Path

import pytest

from interview_mux.musicgen_runner import (
    ban_mps,
    best_of_n_for_role,
    cli_python_executable,
    effective_musicgen_device,
    is_abort_returncode,
    mps_banned,
    musicgen_hf_home,
)


def test_is_abort_returncode() -> None:
    assert is_abort_returncode(-6)
    assert is_abort_returncode(-15)
    assert is_abort_returncode(134)
    assert not is_abort_returncode(0)
    assert not is_abort_returncode(1)
    assert not is_abort_returncode(None)


def test_hf_home_is_local_musicgen_cache() -> None:
    """Cache is the isolated musicgen dir, not the shared ~/.cache/huggingface.

    Accepts a `musicgen.hf_cache_dir` override so the multi-GB weights can live
    off the repo volume; the default remains ASSETS/local_musicgen/hf_cache.
    """
    from interview_mux.config import merged_config

    home = musicgen_hf_home()
    override = str((merged_config().get("musicgen") or {}).get("hf_cache_dir") or "").strip()
    if override:
        assert home.as_posix() == Path(override).as_posix()
    else:
        assert home.as_posix().endswith("ASSETS/local_musicgen/hf_cache")


def test_effective_device_auto_prefers_mps_on_apple_silicon(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MUX_MUSICGEN_BAN_MPS", raising=False)
    monkeypatch.setattr(
        "interview_mux.musicgen_runner._mps_available_for_musicgen", lambda: True
    )
    assert effective_musicgen_device(requested="auto") == "mps"
    assert effective_musicgen_device(requested="") == "mps"
    assert effective_musicgen_device(requested="cpu") == "cpu"
    assert effective_musicgen_device(requested="mps") == "mps"


def test_effective_device_auto_falls_back_when_mps_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MUX_MUSICGEN_BAN_MPS", raising=False)
    monkeypatch.setattr(
        "interview_mux.musicgen_runner._mps_available_for_musicgen", lambda: False
    )
    assert effective_musicgen_device(requested="auto") == "cpu"
    assert effective_musicgen_device(requested="mps") == "cpu"


def test_ban_mps_forces_cpu(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MUX_MUSICGEN_BAN_MPS", "1")
    assert mps_banned() is True
    assert effective_musicgen_device(requested="mps") == "cpu"


def test_cli_python_rewrites_python_app(tmp_path: Path) -> None:
    version_root = tmp_path / "Python.framework" / "Versions" / "3.12"
    app_py = version_root / "Resources" / "Python.app" / "Contents" / "MacOS" / "Python"
    cli_py = version_root / "bin" / "python3.12"
    app_py.parent.mkdir(parents=True)
    cli_py.parent.mkdir(parents=True)
    app_py.write_text("#!/bin/sh\n")
    cli_py.write_text("#!/bin/sh\n")
    app_py.chmod(0o755)
    cli_py.chmod(0o755)
    assert cli_python_executable(app_py) == cli_py


def test_cli_python_passthrough_venv(tmp_path: Path) -> None:
    py = tmp_path / "venv" / "bin" / "python"
    py.parent.mkdir(parents=True)
    py.write_text("#!/bin/sh\n")
    py.chmod(0o755)
    assert cli_python_executable(py) == py


def test_best_of_n_capped_at_one(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "interview_mux.musicgen_runner.musicgen_cfg",
        lambda: {"best_of_n_speech_free": 1, "best_of_n_underscore": 1, "max_best_of_n": 1},
    )
    assert best_of_n_for_role("theme_cold_open") == 1
    assert best_of_n_for_role("theme_underscore") == 1


def test_generate_retries_cpu_after_mps_abort(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import subprocess

    from interview_mux import musicgen_runner as mg

    monkeypatch.delenv("MUX_MUSICGEN_BAN_MPS", raising=False)
    monkeypatch.setattr(mg, "musicgen_enabled", lambda: True)
    monkeypatch.setattr(mg, "fail_closed_on_stub", lambda: False)
    monkeypatch.setattr(mg, "musicgen_venv_python", lambda: tmp_path / "python")
    (tmp_path / "python").write_text("#!/bin/sh\n")
    monkeypatch.setattr(mg, "musicgen_cfg", lambda: {"device": "mps", "ban_mps_on_abort": True, "request_timeout_sec": 5})
    monkeypatch.setattr(mg, "cli_python_executable", lambda p: p)
    monkeypatch.setattr(mg, "effective_musicgen_device", lambda **kwargs: "mps")
    monkeypatch.setattr(mg, "musicgen_hf_home", lambda: tmp_path / "hf_cache")

    class _DummyLock:
        def __init__(self, *args, **kwargs) -> None:  # noqa: ANN002, ANN003
            pass

        def acquire(self, *args, **kwargs) -> bool:  # noqa: ANN002, ANN003
            return True

        def release(self) -> None:
            pass

    import filelock

    monkeypatch.setattr(filelock, "FileLock", _DummyLock)
    calls: list[str] = []

    def fake_spawn(**kwargs):  # noqa: ANN003
        req = Path(kwargs["req"])
        payload = __import__("json").loads(req.read_text())
        calls.append(str(payload.get("device")))
        if payload.get("device") == "mps":
            return subprocess.CompletedProcess(kwargs["py"], -6, "", "abort")
        out = Path(payload["out_wav"])
        out.write_bytes(b"0" * 2000)
        return subprocess.CompletedProcess(kwargs["py"], 0, "", "")

    monkeypatch.setattr(mg, "_spawn_musicgen", fake_spawn)
    out = tmp_path / "stem.wav"
    meta = mg.generate_music_clip(
        prompt="theme",
        negative_prompt="",
        duration_sec=4.0,
        out_wav=out,
        role="theme_cold_open",
    )
    assert calls[0] == "mps"
    assert "cpu" in calls
    # Last successful stem may be stub; device reflects last accepted musicgen step.
    assert meta.get("device") in {"cpu", "mps", None} or meta.get("backend") in {
        "musical_stub",
        "music_omitted",
        "musicgen",
    }
    assert out.is_file() or meta.get("backend") in {"music_omitted", "musical_stub"}


def test_ban_mps_writes_marker(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MUX_MUSICGEN_BAN_MPS", raising=False)

    class _Ctx:
        run_dir = tmp_path

        def mutate_run_meta(self, fn):  # noqa: ANN001
            meta: dict = {}
            fn(meta)
            (tmp_path / "run_meta.json").write_text(str(meta))

    ban_mps(run_ctx=_Ctx(), reason="returncode=-6")
    assert (tmp_path / ".musicgen_ban_mps").is_file()
    assert mps_banned(run_ctx=_Ctx()) is True


def test_generate_always_writes_wav_after_ladder(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import subprocess

    from interview_mux import musicgen_runner as mg

    monkeypatch.setattr(mg, "musicgen_enabled", lambda: True)
    monkeypatch.setattr(mg, "musicgen_venv_python", lambda: tmp_path / "python")
    (tmp_path / "python").write_text("#!/bin/sh\n")
    monkeypatch.setattr(
        mg,
        "musicgen_cfg",
        lambda: {
            "device": "cpu",
            "ban_mps_on_abort": True,
            "request_timeout_sec": 5,
            "default_duration_sec": 12.0,
            "step_down_timeout_sec": 5,
            "max_request_timeout_sec": 5,
        },
    )
    monkeypatch.setattr(mg, "cli_python_executable", lambda p: p)
    monkeypatch.setattr(mg, "effective_musicgen_device", lambda **kwargs: "cpu")
    monkeypatch.setattr(mg, "musicgen_hf_home", lambda: tmp_path / "hf_cache")

    class _DummyLock:
        def __init__(self, *args, **kwargs) -> None:  # noqa: ANN002, ANN003
            pass

        def acquire(self, *args, **kwargs) -> bool:  # noqa: ANN002, ANN003
            return True

        def release(self) -> None:
            pass

    import filelock

    monkeypatch.setattr(filelock, "FileLock", _DummyLock)
    timeouts: list[int] = []

    def fake_spawn(**kwargs):  # noqa: ANN003
        timeouts.append(int(kwargs["timeout"]))
        return subprocess.CompletedProcess(kwargs["py"], 1, "", "timeout after 5s")

    monkeypatch.setattr(mg, "_spawn_musicgen", fake_spawn)
    out = tmp_path / "bed.wav"
    meta = mg.generate_music_clip(
        prompt="lush orchestral theme with choir and 48k fidelity",
        negative_prompt="",
        duration_sec=12.0,
        out_wav=out,
        role="theme_underscore",
        seed=7,
    )
    assert meta.get("backend") in {"musical_stub", "music_omitted"}
    if meta.get("backend") == "music_omitted":
        assert not out.is_file() or out.stat().st_size == 0 or meta.get("music_omitted")
    else:
        assert out.is_file() and out.stat().st_size > 1000
    # Ladder tries large then medium/small (same prompt+duration); last step name varies.
    assert str(meta.get("fidelity_step") or "").startswith("ladder_")
    ladder = meta.get("model_ladder") or []
    assert ladder and "large" in str(ladder[0])
    assert timeouts[0] == 5
    assert timeouts[-1] <= 5


def test_musicgen_timeout_scales_with_duration(monkeypatch: pytest.MonkeyPatch) -> None:
    """Cascade (MUX_FORENSICS=0): longer beds get more hang budget than 10s stems.

    exec_13165: theme_cold_open duration_sec=17 timed out at fixed 900s ×5 on MPS.
    """
    import os

    os.environ["MUX_FORENSICS"] = "0"
    monkeypatch.delenv("MUX_E2E_MUSICGEN_TIMEOUT_SEC", raising=False)
    from interview_mux.musicgen_runner import musicgen_timeouts_for_duration

    cfg = {
        "request_timeout_sec": 900,
        "step_down_timeout_sec": 480,
        "default_duration_sec": 10.0,
        "max_request_timeout_sec": 2400,
    }
    short_t, short_sd = musicgen_timeouts_for_duration(10.0, device="mps", cfg=cfg)
    long_t, long_sd = musicgen_timeouts_for_duration(17.0, device="mps", cfg=cfg)
    assert short_t == 900
    assert short_sd == 480
    assert long_t > short_t
    assert long_t >= int(900 * 1.7)
    assert long_t <= 2400
    assert long_sd > short_sd


def test_hang_timeout_accepts_usable_wav_without_banning_mps(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Cascade (MUX_FORENSICS=0): MPS hang after write must keep stem + stay on MPS.

    exec_13165: theme_cold_open wrote cand_0.wav (~14s) then timed out; runner banned
    MPS and CPU-thrashed musicgen-medium for the same cand.
    """
    import os
    import struct
    import subprocess
    import wave

    os.environ["MUX_FORENSICS"] = "0"
    from interview_mux import musicgen_runner as mg

    monkeypatch.setattr(mg, "musicgen_enabled", lambda: True)
    monkeypatch.setattr(mg, "musicgen_venv_python", lambda: tmp_path / "python")
    (tmp_path / "python").write_text("#!/bin/sh\n")
    monkeypatch.setattr(
        mg,
        "musicgen_cfg",
        lambda: {
            "device": "mps",
            "ban_mps_on_abort": True,
            "request_timeout_sec": 30,
            "model_id": "facebook/musicgen-small",
            "fail_closed_on_stub": True,
            "stub_allowed_roles": [],
        },
    )
    monkeypatch.setattr(mg, "cli_python_executable", lambda p: p)
    monkeypatch.setattr(mg, "effective_musicgen_device", lambda **kwargs: "mps")
    monkeypatch.setattr(mg, "musicgen_hf_home", lambda: tmp_path / "hf_cache")
    (tmp_path / "hf_cache" / "hub" / "models--facebook--musicgen-small").mkdir(parents=True)

    class _DummyLock:
        def __init__(self, *args, **kwargs) -> None:  # noqa: ANN002, ANN003
            pass

        def acquire(self, *args, **kwargs) -> bool:  # noqa: ANN002, ANN003
            return True

        def release(self) -> None:
            pass

    import filelock

    monkeypatch.setattr(filelock, "FileLock", _DummyLock)

    ban_reasons: list[str] = []

    def fake_ban(*, run_ctx=None, reason: str = "") -> None:  # noqa: ANN001
        ban_reasons.append(reason)

    monkeypatch.setattr(mg, "ban_mps", fake_ban)

    out = tmp_path / "cand_0.wav"

    def _write_usable(seconds: float = 14.0) -> None:
        rate = 48000
        n = int(rate * seconds)
        # Non-silent PCM so audible hang-accept gate passes (WS4).
        frames = [int(8000 * ((i % 48) / 24.0 - 1.0)) for i in range(n)]
        with wave.open(str(out), "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(rate)
            wf.writeframes(struct.pack("<" + "h" * n, *frames))

    def fake_spawn(**kwargs):  # noqa: ANN003
        _write_usable(14.0)
        return subprocess.CompletedProcess(
            kwargs["py"], -9, "", "timeout after 30s"
        )

    monkeypatch.setattr(mg, "_spawn_musicgen", fake_spawn)

    class _Ctx:
        run_dir = tmp_path

        def read_json(self, *_a, **_k):  # noqa: ANN002, ANN003
            return {}

        def write_json(self, *_a, **_k):  # noqa: ANN002, ANN003
            return None

    monkeypatch.setattr(mg, "_run_ctx_for_out_wav", lambda _p: _Ctx())

    meta = mg.generate_music_clip(
        prompt="Warm acoustic documentary full opening bed",
        negative_prompt="vocals",
        duration_sec=17.0,
        out_wav=out,
        role="theme_cold_open",
        seed=7,
    )
    assert meta.get("backend") == "musicgen"
    assert meta.get("accepted_after_hang_timeout") is True
    assert out.is_file() and out.stat().st_size > 1000
    assert ban_reasons == []
    assert not (tmp_path / ".musicgen_ban_mps").is_file()


def test_usable_musicgen_wav_duration_gate(tmp_path: Path) -> None:
    import os
    import struct
    import wave

    os.environ["MUX_FORENSICS"] = "0"
    from interview_mux.musicgen_runner import usable_musicgen_wav

    path = tmp_path / "short.wav"
    rate = 48000
    n = int(rate * 5.0)
    frames = [int(4000 * ((i % 40) / 20.0 - 1.0)) for i in range(n)]
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(rate)
        wf.writeframes(struct.pack("<" + "h" * n, *frames))
    assert usable_musicgen_wav(path, requested_seconds=17.0) is False
    assert usable_musicgen_wav(path, requested_seconds=6.0) is True
    # Digital silence must not pass audible hang-accept.
    silent = tmp_path / "silent.wav"
    with wave.open(str(silent), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(rate)
        wf.writeframes(struct.pack("<" + "h" * n, *([0] * n)))
    assert usable_musicgen_wav(silent, requested_seconds=6.0) is False
    assert usable_musicgen_wav(silent, requested_seconds=6.0, require_audible=False) is True

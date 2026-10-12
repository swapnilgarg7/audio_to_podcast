"""Full-auto launch helpers and create_run wiring."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path
import sys

import pytest
from fastapi.testclient import TestClient

from interview_mux.config import repo_root as real_repo_root
from interview_mux.full_auto_launch import normalize_run_mode
from interview_mux.web.server import create_app

_TOOLS = Path(__file__).resolve().parents[1] / "tools"
if str(_TOOLS) not in sys.path:
    sys.path.insert(0, str(_TOOLS))
import full_auto_driver  # noqa: E402


def test_normalize_run_mode() -> None:
    assert normalize_run_mode(None) == "manual"
    assert normalize_run_mode("manual") == "manual"
    assert normalize_run_mode("full-auto") == "full-auto"
    assert normalize_run_mode("full_auto") == "full-auto"
    assert normalize_run_mode("FULLAUTO") == "full-auto"
    assert normalize_run_mode("auto") == "full-auto"


def test_daemon_defaults_homunculus_version_to_latest(monkeypatch) -> None:
    import full_auto_daemon_launch as dal

    monkeypatch.delenv("MUX_HOMUNCULUS_VERSION", raising=False)
    assert dal._default_homunculus_version() == "0.2.0"
    env = dal._driver_env(
        port=8765,
        audio="ASSETS/input/x.wav",
        keep_gui_server=True,
        partial_auto=False,
    )
    assert env["MUX_HOMUNCULUS_VERSION"] == "0.2.0"
    assert env["MUX_CONTRACT_RECORD"] == "1"
    monkeypatch.setenv("MUX_HOMUNCULUS_VERSION", "0.1.0")
    assert dal._default_homunculus_version() == "0.1.0"


def test_full_auto_escalates_after_three_identical_vo_failures() -> None:
    full_auto_driver._VO_REPAIR_FAILURES.clear()
    error = (
        "required gap VO blocked by spoken_copy_guard "
        "(vo_layup_seg_004): spoken_generic_filler"
    )
    assert full_auto_driver.repeated_vo_repair_failure(error) is False
    assert full_auto_driver.repeated_vo_repair_failure(error) is False
    assert full_auto_driver.repeated_vo_repair_failure(error) is True


def test_full_auto_circuit_breaker_writes_decision_brief(monkeypatch) -> None:
    writes: list[tuple[str, dict]] = []

    class FakeContext:
        def __init__(self, *_args, **_kwargs) -> None:
            pass

        def artifact_exists(self, rel: str) -> bool:
            return rel == "understanding/nugget_layup_plan.json"

        def read_json(self, _rel: str) -> dict:
            return {"layups": []}

        def write_json(self, rel: str, data: dict) -> None:
            writes.append((rel, data))

    monkeypatch.setattr("interview_mux.run_context.RunContext", FakeContext)
    monkeypatch.setattr(
        "interview_mux.nugget_layup.uncovered_high_value_forgone",
        lambda *_args, **_kwargs: [{"nugget_id": "nug_critical"}],
    )
    monkeypatch.setattr(full_auto_driver, "RUN_ID", "exec_decision_brief")
    full_auto_driver._VO_REPAIR_FAILURES.clear()
    full_auto_driver._VO_REPAIR_FAILURES["layup"] = 3

    brief = full_auto_driver.write_vo_repair_decision_brief(
        "vo_layup failed spoken_copy_guard"
    )

    assert brief and brief["failure_count"] == 3
    assert brief["unresolved_needs"] == [{"nugget_id": "nug_critical"}]
    assert writes and writes[0][0] == "analysis/decision_briefs/vo_repair_layup.json"


def _seed_client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    from run_fixtures import copy_shipped_config
    copy_shipped_config(tmp_path)
    monkeypatch.setenv("INTERVIEW_MUX_ROOT", str(tmp_path))
    assets = tmp_path / "ASSETS"
    (assets / "input").mkdir(parents=True)
    (assets / "executions").mkdir(parents=True)
    # Minimal RIFF header so create_run accepts the file.
    (assets / "input" / "interview.wav").write_bytes(
        b"RIFF$\x00\x00\x00WAVEfmt \x10\x00\x00\x00\x01\x00\x01\x00"
        b"D\xac\x00\x00\x88X\x01\x00\x02\x00\x10\x00data\x00\x00\x00\x00"
    )
    return TestClient(create_app())


@pytest.mark.real_executions_root  # _seed_client supplies its own INTERVIEW_MUX_ROOT
def test_create_run_manual_does_not_launch_full_auto(tmp_path, monkeypatch) -> None:
    client = _seed_client(tmp_path, monkeypatch)
    launched: list[dict] = []

    def _fake_launch(**kwargs):
        launched.append(kwargs)
        return {"ok": True, "driver_pid": 1}

    monkeypatch.setattr(
        "interview_mux.full_auto_launch.launch_full_auto_for_run",
        _fake_launch,
    )

    res = client.post(
        "/api/runs",
        json={"input_audio_path": "ASSETS/input/interview.wav", "run_mode": "manual"},
    )
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["run_mode"] == "manual"
    assert body["full_auto"] is False
    assert launched == []

    meta = (tmp_path / "ASSETS" / "executions" / body["run_id"] / "run_meta.json").read_text(
        encoding="utf-8"
    )
    assert '"run_mode": "manual"' in meta
    assert '"full_auto": false' in meta


@pytest.mark.real_executions_root  # _seed_client supplies its own INTERVIEW_MUX_ROOT
def test_create_run_full_auto_launches_run_scoped_worker(tmp_path, monkeypatch) -> None:
    client = _seed_client(tmp_path, monkeypatch)
    launched: list[dict] = []

    def _fake_launch(**kwargs):
        launched.append(kwargs)
        return {
            "ok": True,
            "run_id": kwargs["run_id"],
            "driver_pid": 42,
            "keepalive_pid": 43,
            "keep_gui_server": kwargs.get("keep_gui_server"),
            "console_log": "ASSETS/full_auto_console.log",
        }

    monkeypatch.setattr(
        "interview_mux.full_auto_launch.launch_full_auto_for_run",
        _fake_launch,
    )
    # Patch the name used inside create_run's local import path.
    import interview_mux.web.server as server_mod

    monkeypatch.setattr(
        server_mod,
        "launch_full_auto_for_run",
        _fake_launch,
        raising=False,
    )

    # create_run imports from interview_mux.full_auto_launch inside the handler —
    # patching that module attribute is enough when import resolves at call time.
    import interview_mux.full_auto_launch as fal

    monkeypatch.setattr(fal, "launch_full_auto_for_run", _fake_launch)

    res = client.post(
        "/api/runs",
        json={
            "input_audio_path": "ASSETS/input/interview.wav",
            "run_mode": "full-auto",
            "full_auto": True,
        },
    )
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["run_mode"] == "full-auto"
    assert body["full_auto"] is True
    assert body["full_auto_launch"]["driver_pid"] == 42
    assert len(launched) == 1
    assert launched[0]["run_id"] == body["run_id"]
    assert launched[0]["keep_gui_server"] is True
    assert "interview.wav" in launched[0]["input_audio"]

    meta_path = tmp_path / "ASSETS" / "executions" / body["run_id"] / "run_meta.json"
    meta_text = meta_path.read_text(encoding="utf-8")
    assert '"run_mode": "full-auto"' in meta_text
    assert '"full_auto": true' in meta_text


def test_shutdown_automation_stack_passes_keep_driver(monkeypatch) -> None:
    import interview_mux.full_auto_launch as fal

    calls: list[dict] = []

    def _fake_shutdown(**kwargs):
        calls.append(kwargs)
        return {"ok": True}

    monkeypatch.setattr(fal, "shutdown_full_auto_stack", _fake_shutdown, raising=False)
    # Patch via tools import path used inside shutdown_automation_stack
    tools = str(Path(fal._tools_dir()))
    if tools not in sys.path:
        sys.path.insert(0, tools)
    import full_auto_daemon_launch as dal  # noqa: E402

    monkeypatch.setattr(dal, "shutdown_full_auto_stack", _fake_shutdown)

    fal.shutdown_automation_stack(keep_gui_server=True, keep_driver=True)
    assert calls
    assert calls[-1]["keep_driver"] is True
    assert calls[-1]["kill_e2e"] is False

    fal.shutdown_automation_stack(keep_gui_server=True, keep_driver=False)
    assert calls[-1]["keep_driver"] is False
    assert calls[-1]["kill_e2e"] is True


@pytest.mark.real_executions_root  # _seed_client supplies its own INTERVIEW_MUX_ROOT
def test_create_run_refuses_when_driver_already_running(tmp_path, monkeypatch) -> None:
    """D-04 / GUI-START: cross-run dual driver → 409 (not silent wrong launch)."""
    client = _seed_client(tmp_path, monkeypatch)
    launched: list[dict] = []

    def _fake_launch(**kwargs):
        launched.append(kwargs)
        return {"ok": True, "driver_pid": 42}

    monkeypatch.setattr(
        "interview_mux.full_auto_launch.launch_full_auto_for_run",
        _fake_launch,
    )
    monkeypatch.setattr(
        "interview_mux.full_auto_launch.automation_driver_alive",
        lambda: True,
    )
    monkeypatch.setattr(
        "interview_mux.full_auto_launch.automation_driver_bound_run_id",
        lambda: "exec_other_alive",
    )
    monkeypatch.setattr(
        "interview_mux.full_auto_launch.alive_driver_run_mode",
        lambda _bound=None: "full-auto",
    )

    res = client.post(
        "/api/runs",
        json={
            "input_audio_path": "ASSETS/input/interview.wav",
            "run_mode": "full-auto",
            "full_auto": True,
        },
    )
    assert res.status_code == 409, res.text
    assert launched == []
    assert "driver" in res.text.lower() or "Automation" in res.text


def test_refuse_dual_driver_allows_unbound_fresh_create(monkeypatch) -> None:
    """CLI ensure_run is pgrep-alive before POST /api/runs binds a pointer."""
    import interview_mux.full_auto_launch as fal

    monkeypatch.setattr(fal, "automation_driver_alive", lambda: True)
    monkeypatch.setattr(fal, "automation_driver_bound_run_id", lambda: None)
    monkeypatch.setattr(fal, "alive_driver_run_mode", lambda _bound=None: None)

    assert fal.refuse_dual_driver_launch(requested_run_id="__new__", requested_mode="full-auto") is None
    assert fal.refuse_dual_driver_launch(requested_run_id="", requested_mode="full-auto") is None


@pytest.mark.real_executions_root  # _seed_client supplies its own INTERVIEW_MUX_ROOT
def test_create_run_allows_unbound_cli_driver_fresh(tmp_path, monkeypatch) -> None:
    """Fresh kickoff must not 409 when the launching CLI driver is already visible."""
    client = _seed_client(tmp_path, monkeypatch)
    launched: list[dict] = []

    def _fake_launch(**kwargs):
        launched.append(kwargs)
        return {"ok": True, "driver_pid": 99}

    monkeypatch.setattr(
        "interview_mux.full_auto_launch.launch_full_auto_for_run",
        _fake_launch,
    )
    monkeypatch.setattr(
        "interview_mux.full_auto_launch.automation_driver_alive",
        lambda: True,
    )
    monkeypatch.setattr(
        "interview_mux.full_auto_launch.automation_driver_bound_run_id",
        lambda: None,
    )
    monkeypatch.setattr(
        "interview_mux.full_auto_launch.alive_driver_run_mode",
        lambda _bound=None: None,
    )

    res = client.post(
        "/api/runs",
        json={
            "input_audio_path": "ASSETS/input/interview.wav",
            "run_mode": "full-auto",
            "full_auto": True,
        },
    )
    assert res.status_code == 200, res.text
    body = res.json()
    assert body.get("run_id")
    assert len(launched) == 1
    assert launched[0]["run_id"] == body["run_id"]


def test_env_keepalive_requested(monkeypatch) -> None:
    import full_auto_daemon_launch as dal

    monkeypatch.delenv("MUX_KEEPALIVE", raising=False)
    assert dal.env_keepalive_requested() is False
    monkeypatch.setenv("MUX_KEEPALIVE", "1")
    assert dal.env_keepalive_requested() is True
    monkeypatch.setenv("MUX_KEEPALIVE", "true")
    assert dal.env_keepalive_requested() is True
    monkeypatch.setenv("MUX_KEEPALIVE", "0")
    assert dal.env_keepalive_requested() is False


def test_resolve_launch_modes_omits_keepalive_by_default(monkeypatch) -> None:
    import full_auto_daemon_launch as dal

    monkeypatch.delenv("MUX_KEEPALIVE", raising=False)
    assert dal.resolve_launch_modes([]) == {"server", "e2e"}
    assert dal.resolve_launch_modes(["all"]) == {"server", "e2e"}
    assert dal.resolve_launch_modes(["e2e", "--fresh"]) == {"e2e"}
    assert dal.resolve_launch_modes(["e2e", "--keepalive"]) == {"e2e", "keepalive"}
    assert dal.resolve_launch_modes(["keepalive"]) == {"keepalive"}
    assert "stop" in dal.resolve_launch_modes(["stop"])
    assert dal.resolve_launch_modes(["e2e"], keepalive_from_env=True) == {"e2e", "keepalive"}
    assert dal.resolve_launch_modes(["e2e"], keepalive_from_env=False) == {"e2e"}


def test_launch_full_auto_skips_keepalive_by_default(monkeypatch) -> None:
    import full_auto_daemon_launch as dal

    monkeypatch.delenv("MUX_KEEPALIVE", raising=False)
    ka_calls: list[dict] = []
    monkeypatch.setattr(dal, "ensure_e2e", lambda **kwargs: 11)
    monkeypatch.setattr(
        dal,
        "ensure_keepalive",
        lambda **kwargs: ka_calls.append(kwargs) or 22,
    )
    out = dal.launch_full_auto_for_run(
        run_id="exec_test",
        input_audio="ASSETS/input/interview.wav",
    )
    assert ka_calls == []
    assert out["driver_pid"] == 11
    assert out["keepalive_pid"] is None


def test_launch_full_auto_starts_keepalive_when_flag_set(monkeypatch) -> None:
    import full_auto_daemon_launch as dal

    monkeypatch.setenv("MUX_KEEPALIVE", "1")
    ka_calls: list[dict] = []
    monkeypatch.setattr(dal, "ensure_e2e", lambda **kwargs: 11)
    monkeypatch.setattr(
        dal,
        "ensure_keepalive",
        lambda **kwargs: ka_calls.append(kwargs) or 22,
    )
    out = dal.launch_full_auto_for_run(
        run_id="exec_test",
        input_audio="ASSETS/input/interview.wav",
        keep_gui_server=True,
    )
    assert len(ka_calls) == 1
    assert ka_calls[0]["keep_gui_server"] is True
    assert out["keepalive_pid"] == 22


def test_launch_partial_auto_skips_keepalive_by_default(monkeypatch) -> None:
    import full_auto_daemon_launch as dal

    monkeypatch.delenv("MUX_KEEPALIVE", raising=False)
    ka_calls: list[dict] = []
    monkeypatch.setattr(dal, "ensure_e2e", lambda **kwargs: 11)
    monkeypatch.setattr(
        dal,
        "ensure_keepalive",
        lambda **kwargs: ka_calls.append(kwargs) or 22,
    )
    out = dal.launch_partial_auto_for_run(
        run_id="exec_test",
        input_audio="ASSETS/input/interview.wav",
    )
    assert ka_calls == []
    assert out["keepalive_pid"] is None


def test_keepalive_does_not_relaunch_gui_attached_serve(monkeypatch) -> None:
    import full_auto_keepalive_loop as keepalive

    monkeypatch.delenv("MUX_FULL_AUTO_KEEP_SERVER", raising=False)
    assert keepalive.should_relaunch_server_on_death() is True
    monkeypatch.setenv("MUX_FULL_AUTO_KEEP_SERVER", "1")
    assert keepalive.should_relaunch_server_on_death() is False


def test_fresh_pending_roundtrip(tmp_path, monkeypatch) -> None:
    import full_auto_daemon_launch as dal

    assets = tmp_path / "ASSETS"
    assets.mkdir()
    monkeypatch.setattr(dal, "ASSETS", assets)
    monkeypatch.setattr(dal, "FRESH_PENDING", assets / "full_auto_fresh_pending.json")

    assert dal.fresh_pending_active() is False
    dal.write_fresh_pending(input_audio="ASSETS/input/x.wav")
    assert dal.fresh_pending_active() is True
    dal.clear_fresh_pending()
    assert dal.fresh_pending_active() is False


def test_driver_run_bound_reads_pointer(tmp_path, monkeypatch) -> None:
    import full_auto_daemon_launch as dal

    assets = tmp_path / "ASSETS"
    execs = assets / "executions" / "exec_1_abcd_20260831T120000Z"
    execs.mkdir(parents=True)
    pointer = assets / "full_auto_current_run.txt"
    pointer.write_text("exec_1_abcd_20260831T120000Z\n", encoding="utf-8")
    monkeypatch.setattr(dal, "ASSETS", assets)
    monkeypatch.setattr(dal, "RUN_POINTER", pointer)
    monkeypatch.setattr(dal, "E2E_CONSOLE", assets / "full_auto_console.log")

    assert dal.driver_run_bound() == "exec_1_abcd_20260831T120000Z"


def test_ensure_e2e_fresh_kills_keepalive_and_writes_pending(monkeypatch) -> None:
    import full_auto_daemon_launch as dal

    killed: list[str] = []
    pending_writes: list[str] = []

    monkeypatch.setattr(dal, "e2e_alive", lambda: False)
    monkeypatch.setattr(dal, "_pkill_pattern", lambda pattern, **kwargs: killed.append(pattern))
    import time as time_mod

    monkeypatch.setattr(time_mod, "sleep", lambda _s: None)
    monkeypatch.setattr(dal, "rotate_e2e_console", lambda: None)
    monkeypatch.setattr(
        dal,
        "write_fresh_pending",
        lambda **kwargs: pending_writes.append(kwargs.get("input_audio", "")),
    )
    monkeypatch.setattr(dal, "_popen", lambda *_args, **_kwargs: 99)
    monkeypatch.setattr(dal, "web_port", lambda: 8765)
    monkeypatch.setenv("MUX_INPUT_AUDIO", "ASSETS/input/interview.wav")

    pid = dal.ensure_e2e(fresh=True, force=True)

    assert pid == 99
    assert dal._KEEPALIVE_PGREP in killed
    assert dal._DRIVER_PGREP in killed
    assert pending_writes == ["ASSETS/input/interview.wav"]


def test_wait_for_driver_bind_returns_pointer(tmp_path, monkeypatch) -> None:
    import full_auto_daemon_launch as dal

    assets = tmp_path / "ASSETS"
    execs = assets / "executions" / "exec_2_abcd_20260831T120000Z"
    execs.mkdir(parents=True)
    pointer = assets / "full_auto_current_run.txt"
    monkeypatch.setattr(dal, "ASSETS", assets)
    monkeypatch.setattr(dal, "RUN_POINTER", pointer)
    monkeypatch.setattr(dal, "E2E_CONSOLE", assets / "full_auto_console.log")
    monkeypatch.setattr(dal, "fresh_pending_active", lambda: True)
    monkeypatch.setattr(dal, "e2e_alive", lambda: True)
    calls = {"n": 0}

    def _bound() -> str | None:
        calls["n"] += 1
        if calls["n"] >= 2:
            pointer.write_text("exec_2_abcd_20260831T120000Z\n", encoding="utf-8")
            return "exec_2_abcd_20260831T120000Z"
        return None

    monkeypatch.setattr(dal, "driver_run_bound", _bound)
    import time as time_mod

    monkeypatch.setattr(time_mod, "sleep", lambda _s: None)

    assert dal.wait_for_driver_bind(timeout_sec=5) == "exec_2_abcd_20260831T120000Z"


def test_keepalive_launch_disables_nested_keepalive(monkeypatch) -> None:
    import full_auto_keepalive_loop as keepalive

    captured: dict = {}

    def _run(cmd, cwd, check, env):  # type: ignore[no-untyped-def]
        captured["cmd"] = cmd
        captured["env"] = env

    monkeypatch.setattr("subprocess.run", _run)
    keepalive.launch("e2e", "--run-id", "exec_test")

    assert "--no-keepalive" in captured["cmd"]
    assert captured["env"]["MUX_KEEPALIVE"] == "0"


def test_keepalive_sees_the_orchestrator_driver() -> None:
    """The loop's pattern matches the in-process orchestrator the launcher starts."""
    import re

    import full_auto_daemon_launch as dal
    import full_auto_keepalive_loop as keepalive

    cmdline = "/x/.venv/bin/python -u -m interview_mux orchestrate --mode full-auto --input ASSETS/input/a.wav"
    assert re.search(keepalive._DRIVER_PGREP, cmdline)
    assert re.search(dal._DRIVER_PGREP, cmdline)
    assert keepalive._DRIVER_PGREP == dal._DRIVER_PGREP


def test_keepalive_resume_never_inherits_a_fresh_request(monkeypatch) -> None:
    import full_auto_keepalive_loop as keepalive

    captured: dict = {}

    def _run(cmd, cwd, check, env):  # type: ignore[no-untyped-def]
        captured["cmd"] = cmd
        captured["env"] = env

    monkeypatch.setattr("subprocess.run", _run)
    monkeypatch.setenv("MUX_FRESH", "1")
    keepalive.launch("e2e", "--run-id", "exec_test")
    assert captured["env"]["MUX_FRESH"] == "0"
    keepalive.launch("e2e", "--fresh")
    assert captured["env"]["MUX_FRESH"] == "1"
    keepalive.launch("server")
    assert captured["env"]["MUX_FRESH"] == "1"


def test_keepalive_fresh_bind_in_progress(tmp_path, monkeypatch) -> None:
    import full_auto_daemon_launch as dal
    import full_auto_keepalive_loop as keepalive

    assets = tmp_path / "ASSETS"
    assets.mkdir()
    monkeypatch.setattr(dal, "ASSETS", assets)
    monkeypatch.setattr(dal, "FRESH_PENDING", assets / "full_auto_fresh_pending.json")
    monkeypatch.setattr(keepalive, "ASSETS", assets)
    monkeypatch.setattr(keepalive, "RUN_POINTER", assets / "full_auto_current_run.txt")

    assert keepalive.fresh_bind_in_progress() is False
    dal.write_fresh_pending()
    assert keepalive.fresh_bind_in_progress() is True
    (assets / "full_auto_current_run.txt").write_text("exec_bound\n", encoding="utf-8")
    (assets / "executions" / "exec_bound").mkdir(parents=True)
    assert keepalive.fresh_bind_in_progress() is False


def test_keepalive_resume_requires_pointer(monkeypatch) -> None:
    import full_auto_keepalive_loop as keepalive

    launches: list[tuple[str, tuple[str, ...]]] = []

    monkeypatch.setattr(keepalive, "server_alive", lambda: True)
    monkeypatch.setattr(keepalive, "e2e_alive", lambda: False)
    monkeypatch.setattr(keepalive, "latest_run", lambda: "exec_stale_incomplete")
    monkeypatch.setattr(keepalive, "pointed_run", lambda: None)
    monkeypatch.setattr(keepalive, "fresh_bind_in_progress", lambda: False)
    monkeypatch.setattr(keepalive, "remutate_exhausted", lambda _rid: False)
    monkeypatch.setattr(keepalive, "write_status", lambda _rid: None)
    monkeypatch.setattr(
        keepalive,
        "launch",
        lambda mode, *extra: launches.append((mode, extra)),
    )

    pointed = keepalive.pointed_run()
    run_id = keepalive.latest_run()
    if not keepalive.e2e_alive():
        if keepalive.fresh_bind_in_progress():
            pass
        elif pointed and not keepalive.pipeline_complete(pointed):
            keepalive.launch("e2e", "--run-id", pointed)
        elif not pointed:
            keepalive.launch("e2e", "--fresh")

    assert launches == [("e2e", ("--fresh",))]


def test_resolve_launch_modes_honors_no_keepalive(monkeypatch) -> None:
    import full_auto_daemon_launch as dal

    monkeypatch.setenv("MUX_KEEPALIVE", "1")
    assert dal.resolve_launch_modes(["e2e", "--no-keepalive"]) == {"e2e"}
    assert dal.resolve_launch_modes(["e2e", "--keepalive", "--no-keepalive"]) == {"e2e"}


def test_main_fresh_waits_for_bind_before_keepalive(monkeypatch) -> None:
    import full_auto_daemon_launch as dal

    waits: list[float] = []
    ka_calls: list[bool] = []

    monkeypatch.setattr(dal, "ensure_e2e", lambda **kwargs: 11)
    monkeypatch.setattr(dal, "wait_for_driver_bind", lambda **kwargs: waits.append(1) or "exec_new")
    monkeypatch.setattr(
        dal,
        "ensure_keepalive",
        lambda **kwargs: ka_calls.append(kwargs.get("force_restart", False)) or 22,
    )
    monkeypatch.setattr(dal, "server_alive", lambda: True)
    monkeypatch.setattr(dal, "e2e_alive", lambda: True)
    monkeypatch.setattr(dal, "g1_resynth_alive", lambda: False)
    monkeypatch.setattr(dal, "web_port", lambda: 8765)
    monkeypatch.setattr(sys, "argv", ["full_auto_daemon_launch.py", "e2e", "--fresh", "--keepalive"])

    assert dal.main() == 0
    assert waits == [1]
    assert ka_calls == [True]


def test_env_fresh_requested() -> None:
    import full_auto_daemon_launch as dal

    assert dal.env_fresh_requested() is False
    os.environ["MUX_FRESH"] = "1"
    assert dal.env_fresh_requested() is True
    os.environ["MUX_FRESH"] = "0"
    assert dal.env_fresh_requested() is False
    del os.environ["MUX_FRESH"]


def test_main_mux_fresh_env_wins_over_run_id(monkeypatch) -> None:
    import full_auto_daemon_launch as dal

    captured: dict = {}

    def _ensure_e2e(**kwargs):  # type: ignore[no-untyped-def]
        captured.update(kwargs)
        return 11

    monkeypatch.setattr(dal, "ensure_e2e", _ensure_e2e)
    monkeypatch.setattr(dal, "server_alive", lambda: True)
    monkeypatch.setattr(dal, "e2e_alive", lambda: True)
    monkeypatch.setattr(dal, "g1_resynth_alive", lambda: False)
    monkeypatch.setattr(dal, "web_port", lambda: 8765)
    monkeypatch.setenv("MUX_FRESH", "1")
    monkeypatch.setenv("MUX_RUN_ID", "exec_stale_d19c15b58ab4_20260831T120000Z")
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "full_auto_daemon_launch.py",
            "e2e",
            "--run-id",
            "exec_stale_d19c15b58ab4_20260831T120000Z",
        ],
    )

    assert dal.main() == 0
    assert captured["fresh"] is True
    assert captured.get("run_id") is None


def test_popen_strips_empty_mux_run_id(monkeypatch, tmp_path) -> None:
    import full_auto_daemon_launch as dal

    captured: dict[str, str] = {}
    real_popen = subprocess.Popen

    def _spy_popen(cmd, **kwargs):  # type: ignore[no-untyped-def]
        env = kwargs.get("env") or {}
        for key in ("MUX_FRESH", "MUX_RUN_ID"):
            if key in env:
                captured[key] = env[key]
        proc = real_popen(cmd, **kwargs)
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
        return proc

    monkeypatch.setattr(subprocess, "Popen", _spy_popen)
    monkeypatch.setenv("MUX_RUN_ID", "exec_stale_d19c15b58ab4_20260831T120000Z")

    dal._popen(
        [sys.executable, "-c", "pass"],
        tmp_path / "log.txt",
        env={"MUX_FRESH": "1", "MUX_RUN_ID": ""},
    )

    assert captured.get("MUX_FRESH") == "1"
    assert "MUX_RUN_ID" not in captured


def test_driver_fresh_env_wins_over_run_id(monkeypatch) -> None:
    monkeypatch.setenv("MUX_FRESH", "1")
    monkeypatch.setenv("MUX_RUN_ID", "exec_stale_d19c15b58ab4_20260831T120000Z")
    assert full_auto_driver._driver_fresh_from_env() is True


def test_ensure_run_clears_session_with_keep_driver(monkeypatch) -> None:
    """CLI fresh create must not suicide via DELETE /api/session/active."""
    calls: list[tuple[str, str]] = []

    def _fake_api(method: str, path: str, body=None, timeout: int = 180):
        calls.append((method, path))
        if method == "DELETE":
            return {"ok": True, "active": None}
        if method == "POST" and path == "/api/runs":
            return {"run_id": "exec_keep_driver_d19c15b58ab4_20260831T000000Z"}
        if method == "PUT":
            return {"ok": True}
        return {}

    bound: list[str] = []

    def _fake_bind(run_id: str) -> None:
        bound.append(run_id)
        full_auto_driver.RUN_ID = run_id

    monkeypatch.setattr(full_auto_driver, "api", _fake_api)
    monkeypatch.setattr(full_auto_driver, "bind_run", _fake_bind)
    monkeypatch.setattr(full_auto_driver, "RUN_ID", "")
    monkeypatch.setattr(full_auto_driver, "FRESH", True)
    monkeypatch.setattr(
        full_auto_driver,
        "INPUT_AUDIO",
        "ASSETS/input/mohan_uttarwar_podcast_transforming_cancer_science_direct.mp3",
    )

    assert full_auto_driver.ensure_run() is True
    assert any(
        m == "DELETE" and "keep_driver=true" in p for m, p in calls
    ), calls
    assert bound == ["exec_keep_driver_d19c15b58ab4_20260831T000000Z"]

#!/usr/bin/env python3
"""Launch long-running Full-auto processes detached from the parent session (macOS-safe).

Keepalive is opt-in. It is not started by GUI Full-auto, ``run.sh --full-auto``,
or ``python tools/full_auto_daemon_launch.py`` / ``e2e`` unless requested:

  MUX_KEEPALIVE=1
  python tools/full_auto_daemon_launch.py keepalive
  python tools/full_auto_daemon_launch.py e2e --keepalive
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ASSETS = ROOT / "ASSETS"

def _venv_python(venv_dir: Path) -> Path:
    """Interpreter inside a venv, cross-platform (Scripts/ on Windows, bin/ elsewhere)."""
    bin_dir = venv_dir / ("Scripts" if os.name == "nt" else "bin")
    for name in (("python.exe", "python3.exe") if os.name == "nt" else ("python", "python3")):
        cand = bin_dir / name
        if cand.is_file():
            return cand
    return bin_dir / ("python.exe" if os.name == "nt" else "python")

VENV_PY = _venv_python(ROOT / ".venv")
E2E_CONSOLE = ASSETS / "full_auto_console.log"
RUN_POINTER = ASSETS / "full_auto_current_run.txt"
FRESH_PENDING = ASSETS / "full_auto_fresh_pending.json"
_DRIVER_BIND_POLL_SEC = 1.0
_DRIVER_BIND_TIMEOUT_SEC = 120.0

# Legacy process patterns (pre-rename) — still matched for stop/status during transition.
_DRIVER_PGREP = r"full_auto_driver\.py|_baba_e2e_driver\.py"
_KEEPALIVE_PGREP = r"full_auto_keepalive_loop\.py|baba_keepalive_loop\.py"


def _default_homunculus_version() -> str:
    """Registered default brain when MUX_HOMUNCULUS_VERSION is unset (currently 0.2.0)."""
    pinned = (os.environ.get("MUX_HOMUNCULUS_VERSION") or "").strip()
    if pinned:
        return pinned
    try:
        sys.path.insert(0, str(ROOT / "src"))
        from interview_mux.homunculus.version import default_version

        return default_version()
    except Exception:
        return "0.2.0"


def web_port() -> int:
    """GUI serve port from config (fallback 8765)."""
    try:
        sys.path.insert(0, str(ROOT / "src"))
        from interview_mux.config import merged_config

        return int(merged_config().get("web_port", 8765))
    except Exception:
        try:
            return int(os.environ.get("MUX_WEB_PORT") or 8765)
        except ValueError:
            return 8765


def env_keepalive_requested() -> bool:
    """True when the operator opted into the Full-auto crash-restart watchdog."""
    raw = str(os.environ.get("MUX_KEEPALIVE") or "").strip().lower()
    return raw in {"1", "true", "yes", "on"}


def env_fresh_requested() -> bool:
    """True when the operator asked for a brand-new exec_* (MUX_FRESH=1)."""
    raw = str(os.environ.get("MUX_FRESH") or "").strip().lower()
    return raw in {"1", "true", "yes", "on"}


def health_url() -> str:
    return f"http://127.0.0.1:{web_port()}/api/health"


def _pipeline_complete(run_dir: Path) -> bool:
    try:
        from interview_mux.execution_status import pipeline_complete as ctx_complete
        from interview_mux.run_context import RunContext

        return bool(ctx_complete(RunContext(run_dir.name, create=False)))
    except Exception:
        pass
    master = run_dir / "master" / "master.wav"
    done = run_dir / ".stage_done"
    pub = run_dir / "publish"
    if not (master.is_file() and master.stat().st_size > 1000):
        return False
    if not (done / "podcast_publish").is_file():
        return False
    if not (done / "episode_cover_generate").is_file():
        return False
    if not ((pub / "cover.jpg").is_file() or (pub / "cover.png").is_file()):
        return False
    return (pub / "audio.mp3").is_file()


def newest_incomplete_run() -> str | None:
    """Newest execution that has not reached the ship bar, else newest execution."""
    execs = sorted(
        (p for p in (ASSETS / "executions").glob("exec_*") if p.is_dir()),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    for cand in execs:
        if not _pipeline_complete(cand):
            return cand.name
    return execs[0].name if execs else None


def write_fresh_pending(*, input_audio: str = "") -> None:
    """Mark an in-flight fresh launch so keepalive does not resume a stale run."""
    ASSETS.mkdir(parents=True, exist_ok=True)
    FRESH_PENDING.write_text(
        json.dumps({"started_at": time.time(), "input_audio": input_audio}),
        encoding="utf-8",
    )


def clear_fresh_pending() -> None:
    FRESH_PENDING.unlink(missing_ok=True)


def fresh_pending_active() -> bool:
    return FRESH_PENDING.is_file()


def driver_run_bound() -> str | None:
    """Return run_id when the driver has bound (pointer file or console line)."""
    for pointer in (RUN_POINTER, ASSETS / "baba_current_run.txt"):
        if pointer.is_file():
            rid = pointer.read_text(encoding="utf-8").strip()
            if rid and (ASSETS / "executions" / rid).is_dir():
                return rid
    if not E2E_CONSOLE.is_file():
        return None
    for line in reversed(E2E_CONSOLE.read_text(errors="ignore").splitlines()):
        if "created fresh run=" in line or "resuming existing run=" in line:
            m = re.search(r"exec_\d+_[a-f0-9]+_\d{8}T\d{6}Z", line)
            if m:
                return m.group(0)
    return None


def wait_for_driver_bind(
    *,
    timeout_sec: float = _DRIVER_BIND_TIMEOUT_SEC,
    poll_sec: float = _DRIVER_BIND_POLL_SEC,
) -> str | None:
    """Block until the driver binds a run or fresh launch fails."""
    deadline = time.time() + timeout_sec
    while time.time() < deadline:
        rid = driver_run_bound()
        if rid:
            return rid
        if not fresh_pending_active() and not e2e_alive():
            return None
        time.sleep(poll_sec)
    return driver_run_bound()


def rotate_e2e_console() -> None:
    """Archive the driver console log so run discovery never latches a stale run."""
    RUN_POINTER.unlink(missing_ok=True)
    # Also clear legacy pointer if present.
    (ASSETS / "baba_current_run.txt").unlink(missing_ok=True)
    if not E2E_CONSOLE.is_file() or E2E_CONSOLE.stat().st_size == 0:
        # Migrate legacy console if present.
        legacy = ASSETS / "baba_e2e_console.log"
        if legacy.is_file() and legacy.stat().st_size > 0:
            stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
            archive = ASSETS / "logs_archive"
            archive.mkdir(parents=True, exist_ok=True)
            legacy.replace(archive / f"full_auto_console.{stamp}.log")
        return
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    archive = ASSETS / "logs_archive"
    archive.mkdir(parents=True, exist_ok=True)
    E2E_CONSOLE.replace(archive / f"full_auto_console.{stamp}.log")


# Removed skips: MusicGen must always run the real ladder (large → medium → small).
_MUSICGEN_SKIP_ENV = (
    "MUX_E2E_MUSICGEN_FAST_STUB",
    "MUX_E2E_MUSICGEN_FORCE_STUB",
)


def _popen(cmd: list[str], log_path: Path, env: dict[str, str] | None = None) -> int:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    out = open(log_path, "a", buffering=1)
    full_env = os.environ.copy()
    if env:
        full_env.update(env)
        # Empty MUX_RUN_ID means fresh create — do not inherit a stale id from the parent shell.
        if not str(env.get("MUX_RUN_ID") or "").strip():
            full_env.pop("MUX_RUN_ID", None)
    for key in _MUSICGEN_SKIP_ENV:
        full_env.pop(key, None)
    proc = subprocess.Popen(
        cmd,
        cwd=str(ROOT),
        stdout=out,
        stderr=subprocess.STDOUT,
        stdin=subprocess.DEVNULL,
        env=full_env,
        start_new_session=True,
        close_fds=True,
    )
    return int(proc.pid)


def server_alive() -> bool:
    try:
        import urllib.request

        with urllib.request.urlopen(health_url(), timeout=3) as resp:
            return resp.status == 200
    except Exception:
        return False


def e2e_alive() -> bool:
    try:
        out = subprocess.check_output(["pgrep", "-f", _DRIVER_PGREP], text=True)
        return bool(out.strip())
    except subprocess.CalledProcessError:
        return False


automation_driver_alive = e2e_alive


def ensure_server(*, force_restart: bool = False) -> int | None:
    """Start GUI serve if down. With force_restart, recycle to load current code."""
    port = web_port()
    if server_alive() and not force_restart:
        return None
    # Recycle listeners so Python module edits load. SIGTERM first, then SIGKILL
    # leftovers (a second serve can bind while an old worker keeps synthesizing).
    subprocess.run(["pkill", "-f", "interview_mux serve"], check=False)
    time.sleep(1.0)
    subprocess.run(["pkill", "-9", "-f", "interview_mux serve"], check=False)
    # Orphan MusicGen workers survive serve recycle (PPID 1) and starve the
    # current clip on MPS. Kill them with the listener.
    subprocess.run(["pkill", "-f", "tools/musicgen_generate.py"], check=False)
    time.sleep(0.4)
    subprocess.run(["pkill", "-9", "-f", "tools/musicgen_generate.py"], check=False)
    _kill_pids_on_port(port)
    time.sleep(1.5)
    pid = _popen(
        [str(VENV_PY), "-m", "interview_mux", "serve", "--no-browser", "--port", str(port)],
        ASSETS / "full_auto_server.log",
        env={
            "INTERVIEW_MUX_E2E_SOFT": "1",
            "MUX_WEB_PORT": str(port),
            "MUX_HOMUNCULUS_VERSION": _default_homunculus_version(),
            # Cached per process — must be on the server at launch. "0" stays "0".
            "MUX_CONTRACT_RECORD": os.environ.get("MUX_CONTRACT_RECORD") or "1",
        },
    )
    (ASSETS / "full_auto_server.pid").write_text(str(pid))
    for _ in range(40):
        if server_alive():
            return pid
        time.sleep(0.5)
    raise RuntimeError("server failed to become healthy")


def _driver_env(
    *,
    port: int,
    audio: str,
    keep_gui_server: bool,
    partial_auto: bool,
) -> dict[str, str]:
    run_mode = "partially-accelerated" if partial_auto else os.environ.get("MUX_RUN_MODE", "full-auto")
    env: dict[str, str] = {
        "INTERVIEW_MUX_AUTO_ACCEPT_GATES": "1",
        "MUX_POLL_SEC": "20",
        "MUX_INPUT_AUDIO": audio,
        "INTERVIEW_MUX_E2E_SOFT": "1",
        "MUX_RUN_MODE": run_mode,
        "MUX_BASE": os.environ.get("MUX_BASE", f"http://127.0.0.1:{port}"),
        "MUX_WEB_PORT": str(port),
        "MUX_HOMUNCULUS_VERSION": _default_homunculus_version(),
        "MUX_CONTRACT_RECORD": os.environ.get("MUX_CONTRACT_RECORD") or "1",
    }
    if partial_auto:
        env["MUX_PARTIAL_AUTO"] = "1"
    else:
        env["MUX_FULL_AUTO"] = "1"
    if keep_gui_server:
        env["MUX_FULL_AUTO_KEEP_SERVER"] = "1"
    skip_preclean = str(os.environ.get("MUX_SKIP_PRECLEAN") or "").strip()
    if skip_preclean:
        env["MUX_SKIP_PRECLEAN"] = skip_preclean
    return env


def ensure_e2e(
    *,
    fresh: bool = False,
    run_id: str | None = None,
    force: bool = False,
    input_audio: str | None = None,
    keep_gui_server: bool = False,
    partial_auto: bool = False,
) -> int | None:
    """Launch or resume the Full-auto driver.

    When ``keep_gui_server`` is True (browser / in-app launch), the driver is told
    not to tear down ``interview_mux serve`` on ship so the GUI stays observable.

    Dual-driver guard: without ``force``, refuse starting a second healer when a
    live ``operator/driver_claim.json`` already owns the target ``run_id``.
    """
    if e2e_alive() and not fresh and not force:
        return None
    resume_rid = None if fresh else (run_id or newest_incomplete_run())
    if resume_rid and not force:
        try:
            sys.path.insert(0, str(ROOT / "src"))
            from interview_mux.driver_singleton import assert_can_bind_driver

            existing = assert_can_bind_driver(resume_rid, force=False)
            if existing:
                other = int(existing.get("pid") or 0)
                raise RuntimeError(
                    f"driver already active for {resume_rid} pid={other} "
                    f"— refuse second driver (pass force=True to replace)"
                )
        except RuntimeError:
            raise
        except Exception:
            pass
    # Never inherit a MusicGen skip flag into the driver process.
    for key in _MUSICGEN_SKIP_ENV:
        os.environ.pop(key, None)
    if fresh:
        _pkill_pattern(_KEEPALIVE_PGREP)
    _pkill_pattern(_DRIVER_PGREP)
    time.sleep(1)
    port = web_port()
    audio = (
        input_audio
        or os.environ.get("MUX_INPUT_AUDIO")
        or "ASSETS/input/mohan_uttarwar_podcast_transforming_cancer_science_direct.mp3"
    )
    env = _driver_env(port=port, audio=audio, keep_gui_server=keep_gui_server, partial_auto=partial_auto)
    if fresh:
        rotate_e2e_console()
        write_fresh_pending(input_audio=audio)
        env["MUX_FRESH"] = "1"
        env["MUX_RUN_ID"] = ""
    else:
        rid = resume_rid
        if not rid:
            raise RuntimeError("no existing execution to resume — pass --fresh")
        env["MUX_FRESH"] = "0"
        env["MUX_RUN_ID"] = rid
    pid = _popen(
        [str(VENV_PY), str(ROOT / "tools" / "full_auto_driver.py")],
        E2E_CONSOLE,
        env=env,
    )
    (ASSETS / "full_auto.pid").write_text(str(pid))
    return pid


def launch_partial_auto_for_run(
    *,
    run_id: str,
    input_audio: str,
    keep_gui_server: bool = True,
) -> dict[str, object]:
    """In-app entry: partially-accelerated driver on an already-created run."""
    rid = (run_id or "").strip()
    if not rid:
        raise ValueError("run_id is required")
    audio = (input_audio or "").strip()
    if not audio:
        raise ValueError("input_audio is required")
    pid = ensure_e2e(
        fresh=False,
        run_id=rid,
        force=True,
        input_audio=audio,
        keep_gui_server=keep_gui_server,
        partial_auto=True,
    )
    ka_pid = maybe_ensure_keepalive(keep_gui_server=keep_gui_server)
    return {
        "ok": True,
        "run_id": rid,
        "run_mode": "partially-accelerated",
        "driver_pid": pid,
        "keepalive_pid": ka_pid,
        "console_log": str(E2E_CONSOLE.relative_to(ROOT)),
        "keep_gui_server": keep_gui_server,
    }


def launch_full_auto_for_run(
    *,
    run_id: str,
    input_audio: str,
    keep_gui_server: bool = True,
) -> dict[str, object]:
    """In-app / API entry: attach Full-auto to an already-created run.

    Does not recycle the GUI server. Forces a fresh driver process scoped to
    ``run_id`` (kills any prior Full-auto driver so the explicit run wins).
    """
    rid = (run_id or "").strip()
    if not rid:
        raise ValueError("run_id is required")
    audio = (input_audio or "").strip()
    if not audio:
        raise ValueError("input_audio is required")
    pid = ensure_e2e(
        fresh=False,
        run_id=rid,
        force=True,
        input_audio=audio,
        keep_gui_server=keep_gui_server,
    )
    ka_pid = maybe_ensure_keepalive(keep_gui_server=keep_gui_server)
    return {
        "ok": True,
        "run_id": rid,
        "driver_pid": pid,
        "keepalive_pid": ka_pid,
        "console_log": str(E2E_CONSOLE.relative_to(ROOT)),
        "keep_gui_server": keep_gui_server,
    }


def maybe_ensure_keepalive(*, keep_gui_server: bool = False) -> int | None:
    """Start keepalive only when MUX_KEEPALIVE is set. Default is off."""
    if not env_keepalive_requested():
        return None
    return ensure_keepalive(keep_gui_server=keep_gui_server)


def ensure_keepalive(*, keep_gui_server: bool = False, force_restart: bool = False) -> int | None:
    if force_restart:
        _pkill_pattern(_KEEPALIVE_PGREP)
        time.sleep(0.3)
    else:
        try:
            out = subprocess.check_output(["pgrep", "-f", _KEEPALIVE_PGREP], text=True)
            if out.strip():
                return None
        except subprocess.CalledProcessError:
            pass
    port = web_port()
    env = {"MUX_WEB_PORT": str(port)}
    skip_preclean = str(os.environ.get("MUX_SKIP_PRECLEAN") or "").strip()
    if skip_preclean:
        env["MUX_SKIP_PRECLEAN"] = skip_preclean
    if keep_gui_server or str(os.environ.get("MUX_FULL_AUTO_KEEP_SERVER") or "").strip().lower() in {
        "1",
        "true",
        "yes",
    }:
        env["MUX_FULL_AUTO_KEEP_SERVER"] = "1"
    pid = _popen(
        [str(VENV_PY), str(ROOT / "tools" / "full_auto_keepalive_loop.py")],
        ASSETS / "full_auto_watchdog.log",
        env=env,
    )
    (ASSETS / "full_auto_keepalive.pid").write_text(str(pid))
    return pid


def g1_resynth_alive() -> bool:
    try:
        out = subprocess.check_output(["pgrep", "-f", "_g1_resynth_missing.py"], text=True)
        return bool(out.strip())
    except subprocess.CalledProcessError:
        return False


def ensure_g1_resynth(*, run_id: str | None = None, force: bool = False) -> int | None:
    """macOS-safe detached G1 VO resynth (start_new_session; survives parent exit)."""
    if g1_resynth_alive() and not force:
        return None
    if force:
        _pkill_pattern("_g1_resynth_missing.py")
        _pkill_pattern("chatterbox_generate.py")
        time.sleep(1)
    rid = (run_id or newest_incomplete_run() or "").strip()
    if not rid:
        raise RuntimeError("no run_id for g1-resynth")
    log_path = ASSETS / "g1_resynth3.log"
    # Rotate only when forcing a fresh pass so monitors can append to one file.
    if force and log_path.is_file():
        stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
        archive = ASSETS / "logs_archive"
        archive.mkdir(parents=True, exist_ok=True)
        log_path.replace(archive / f"g1_resynth3.{stamp}.log")
    pid = _popen(
        [str(VENV_PY), "-u", str(ROOT / "tools" / "_g1_resynth_missing.py"), rid],
        log_path,
    )
    (ASSETS / "g1_resynth3.pid").write_text(str(pid))
    return pid


def _kill_pids_on_port(port: int | None = None) -> list[int]:
    """SIGTERM anything listening on the GUI serve port."""
    if port is None:
        port = web_port()
    killed: list[int] = []
    try:
        out = subprocess.check_output(
            ["lsof", f"-tiTCP:{port}", "-sTCP:LISTEN"],
            text=True,
            stderr=subprocess.DEVNULL,
        )
    except (subprocess.CalledProcessError, FileNotFoundError):
        return killed
    self_pid = os.getpid()
    for tok in out.split():
        try:
            pid = int(tok.strip())
        except ValueError:
            continue
        if pid <= 1 or pid == self_pid:
            continue
        try:
            os.kill(pid, 15)
            killed.append(pid)
        except ProcessLookupError:
            pass
        except PermissionError:
            subprocess.run(["kill", "-15", str(pid)], check=False)
            killed.append(pid)
    return killed


def _pkill_pattern(pattern: str, *, exclude_pid: int | None = None) -> None:
    """Best-effort pkill for a process pattern, optionally skipping one pid."""
    try:
        out = subprocess.check_output(["pgrep", "-f", pattern], text=True)
    except subprocess.CalledProcessError:
        return
    self_pid = os.getpid()
    for tok in out.split():
        try:
            pid = int(tok.strip())
        except ValueError:
            continue
        if pid <= 1 or pid == self_pid:
            continue
        if exclude_pid is not None and pid == exclude_pid:
            continue
        try:
            os.kill(pid, 15)
        except ProcessLookupError:
            pass
        except PermissionError:
            subprocess.run(["kill", "-15", str(pid)], check=False)


def shutdown_full_auto_stack(
    *,
    kill_server: bool = True,
    kill_e2e: bool = True,
    keep_driver: bool = False,
    kill_keepalive: bool = True,
    exclude_pid: int | None = None,
    port: int | None = None,
) -> dict[str, object]:
    """Tear down serve + Full-auto driver + keepalive after a completed (or abandoned) run.

    Call from the driver with kill_e2e=False so this process can exit cleanly.
    Call from keepalive with kill_keepalive=False for the same reason.
    """
    if port is None:
        port = web_port()
    ASSETS.mkdir(parents=True, exist_ok=True)
    clear_fresh_pending()
    killed_port: list[int] = []
    if kill_keepalive:
        _pkill_pattern(_KEEPALIVE_PGREP, exclude_pid=exclude_pid)
    if kill_e2e and not keep_driver:
        _pkill_pattern(_DRIVER_PGREP, exclude_pid=exclude_pid)
    if kill_server:
        _pkill_pattern("interview_mux serve", exclude_pid=exclude_pid)
        _pkill_pattern("tools/musicgen_generate.py", exclude_pid=exclude_pid)
        killed_port = _kill_pids_on_port(port)
        # Second pass after brief settle — catch respawn races / child listeners.
        time.sleep(0.6)
        killed_port.extend(_kill_pids_on_port(port))
        _pkill_pattern("interview_mux serve", exclude_pid=exclude_pid)
    for name in (
        "full_auto_server.pid",
        "full_auto.pid",
        "full_auto_keepalive.pid",
        # Legacy pid files
        "baba_server.pid",
        "baba_e2e.pid",
        "baba_keepalive.pid",
    ):
        (ASSETS / name).unlink(missing_ok=True)
    return {
        "server_alive": server_alive() if kill_server else None,
        "e2e_alive": e2e_alive() if kill_e2e else None,
        "port_killed": sorted(set(killed_port)),
    }


# Back-compat alias
shutdown_baba_stack = shutdown_full_auto_stack


def resolve_launch_modes(args: list[str], *, keepalive_from_env: bool | None = None) -> set[str]:
    """Modes to start. Keepalive is never implied by ``all`` / empty argv."""
    run_id = None
    input_audio = None
    for i, arg in enumerate(args):
        if arg == "--run-id" and i + 1 < len(args):
            run_id = args[i + 1]
        if arg == "--input" and i + 1 < len(args):
            input_audio = args[i + 1]
    skip = {
        "--fresh",
        "--run-id",
        "--restart-server",
        "--force-e2e",
        "--input",
        "--keepalive",
        "--no-keepalive",
        run_id,
        input_audio,
    }
    modes = {a for a in args if a not in skip and not a.startswith("--")}
    if "stop" in modes or "shutdown" in modes:
        return modes
    if not modes or "all" in modes:
        modes = {"server", "e2e"}
    no_keepalive = "--no-keepalive" in args
    want_keepalive = "--keepalive" in args
    if keepalive_from_env is None:
        want_keepalive = want_keepalive or env_keepalive_requested()
    else:
        want_keepalive = want_keepalive or bool(keepalive_from_env)
    if want_keepalive and not no_keepalive:
        modes.add("keepalive")
    return modes


def main() -> int:
    args = sys.argv[1:]
    run_id = None
    input_audio = None
    for i, arg in enumerate(args):
        if arg == "--run-id" and i + 1 < len(args):
            run_id = args[i + 1]
        if arg == "--input" and i + 1 < len(args):
            input_audio = args[i + 1]
    fresh = "--fresh" in args or env_fresh_requested()
    if fresh:
        run_id = None
    restart_server = "--restart-server" in args
    force_e2e = (
        "--force-e2e" in args
        or restart_server
        or (bool(run_id) and not fresh)
    )
    modes = resolve_launch_modes(args)
    if "stop" in modes or "shutdown" in modes:
        info = shutdown_full_auto_stack()
        print(f"shutdown={info}")
        return 0

    if "server" in modes:
        pid = ensure_server(force_restart=restart_server)
        print(f"server pid={pid or 'already-up'}")
    if "e2e" in modes:
        # Recycle driver when --run-id / --force-e2e / --restart-server so code edits load.
        pid = ensure_e2e(
            fresh=fresh,
            run_id=run_id,
            force=force_e2e,
            input_audio=input_audio,
        )
        print(f"e2e pid={pid or 'already-up'}")
    if "keepalive" in modes:
        if fresh:
            bound = wait_for_driver_bind()
            if bound:
                print(f"driver bound run={bound}", flush=True)
        pid = ensure_keepalive(force_restart=fresh)
        print(f"keepalive pid={pid or 'already-up'}")
    if "g1-resynth" in modes:
        pid = ensure_g1_resynth(run_id=run_id, force=force_e2e or fresh)
        print(f"g1-resynth pid={pid or 'already-up'}")
    print(
        f"health={server_alive()} e2e={e2e_alive()} g1_resynth={g1_resynth_alive()} "
        f"port={web_port()}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

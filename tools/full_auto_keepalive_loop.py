#!/usr/bin/env python3
"""Keep Full-auto server + driver alive until podcast_publish completes.

Opt-in only (``MUX_KEEPALIVE=1``, ``--keepalive``, or
``python tools/full_auto_daemon_launch.py keepalive``). Default Full-auto and
GUI launches do not start this loop.

When the pointed run reaches the ship bar (master + cover + publish), this
loop tears down serve + driver and exits — it does not relaunch a finished run.
When ``MUX_FULL_AUTO_KEEP_SERVER=1`` (in-app GUI launch), the GUI serve process
is left running so the operator can keep watching status in the browser.
"""

from __future__ import annotations

import json
import os
import re
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

def _venv_python(venv_dir: Path) -> Path:
    """Interpreter inside a venv, cross-platform (Scripts/ on Windows, bin/ elsewhere)."""
    bin_dir = venv_dir / ("Scripts" if os.name == "nt" else "bin")
    for name in (("python.exe", "python3.exe") if os.name == "nt" else ("python", "python3")):
        cand = bin_dir / name
        if cand.is_file():
            return cand
    return bin_dir / ("python.exe" if os.name == "nt" else "python")

ASSETS = ROOT / "ASSETS"
STATUS = ASSETS / "full_auto_status.json"
LOG = ASSETS / "full_auto_watchdog.log"
RUN_POINTER = ASSETS / "full_auto_current_run.txt"
_DRIVER_PGREP = r"full_auto_driver\.py|_baba_e2e_driver\.py"


def web_port() -> int:
    try:
        from full_auto_daemon_launch import web_port as _wp

        return int(_wp())
    except Exception:
        try:
            return int(os.environ.get("MUX_WEB_PORT") or 8765)
        except ValueError:
            return 8765


def api_base() -> str:
    return f"http://127.0.0.1:{web_port()}"


def log(msg: str) -> None:
    # stdout is redirected to LOG by full_auto_daemon_launch; a second file write duplicates lines.
    print(f"{time.strftime('%Y-%m-%dT%H:%M:%S')} {msg}", flush=True)


def launch(mode: str, *extra: str) -> None:
    import subprocess

    py = _venv_python(ROOT / ".venv")
    cmd = [str(py), str(ROOT / "tools" / "full_auto_daemon_launch.py"), mode, *extra, "--no-keepalive"]
    env = os.environ.copy()
    env["MUX_KEEPALIVE"] = "0"
    subprocess.run(cmd, cwd=str(ROOT), check=False, env=env)


def server_alive() -> bool:
    try:
        with urllib.request.urlopen(f"{api_base()}/api/health", timeout=5) as resp:
            return resp.status == 200
    except Exception:
        return False


def e2e_alive() -> bool:
    import subprocess

    try:
        out = subprocess.check_output(["pgrep", "-f", _DRIVER_PGREP], text=True)
        return bool(out.strip())
    except subprocess.CalledProcessError:
        return False


def _keep_gui_server() -> bool:
    return str(os.environ.get("MUX_FULL_AUTO_KEEP_SERVER") or "").strip().lower() in {
        "1",
        "true",
        "yes",
    }


def should_relaunch_server_on_death() -> bool:
    """Unattended keepalive may recycle a crashed serve; GUI-attached must not."""
    return not _keep_gui_server()


def latest_run() -> str | None:
    # The driver writes this on bind — authoritative and race-free at fresh start.
    for pointer in (RUN_POINTER, ASSETS / "baba_current_run.txt"):
        if pointer.is_file():
            pointed = pointer.read_text(encoding="utf-8").strip()
            if pointed and (ASSETS / "executions" / pointed).is_dir():
                return pointed
    # Fall back to explicit resume/create lines from the current driver.
    for console_name in ("full_auto_console.log", "baba_e2e_console.log"):
        console = ASSETS / console_name
        if not console.is_file():
            continue
        for line in reversed(console.read_text(errors="ignore").splitlines()):
            if "resuming existing run=" in line or "created fresh run=" in line:
                m = re.search(r"exec_\d+_[a-f0-9]+_\d{8}T\d{6}Z", line)
                if m:
                    return m.group(0)
    # Prefer newest incomplete execution over completed ones.
    execs = sorted(
        (ASSETS / "executions").glob("exec_*"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    for cand in execs:
        if cand.is_dir() and not pipeline_complete(cand.name):
            return cand.name
    return execs[0].name if execs else None


def remutate_exhausted(run_id: str) -> bool:
    """Do not relaunch when listen-delight remutate already exhausted (loop halt)."""
    path = ASSETS / "executions" / run_id / "mastering" / "listen_delight_remutate.json"
    if not path.is_file():
        pending = (
            ASSETS
            / "executions"
            / run_id
            / ".pending_writes"
            / "listen_delight_audit"
            / "mastering"
            / "listen_delight_remutate.json"
        )
        path = pending if pending.is_file() else path
    if not path.is_file():
        return False
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    return bool(isinstance(doc, dict) and doc.get("exhausted"))


def pipeline_complete(run_id: str) -> bool:
    try:
        from interview_mux.execution_status import pipeline_complete as ctx_complete
        from interview_mux.run_context import RunContext

        return bool(ctx_complete(RunContext(run_id, create=False)))
    except Exception:
        pass
    root = ASSETS / "executions" / run_id
    master = root / "master" / "master.wav"
    done = root / ".stage_done"
    pub = root / "publish"
    if not (master.is_file() and master.stat().st_size > 1000):
        return False
    if not (done / "podcast_publish").is_file():
        return False
    if not (done / "episode_cover_generate").is_file():
        return False
    if not ((pub / "cover.jpg").is_file() or (pub / "cover.png").is_file()):
        return False
    return (pub / "audio.mp3").is_file()


def write_status(run_id: str | None) -> None:
    status: dict = {"ts": time.time(), "run": run_id, "server": server_alive(), "e2e": e2e_alive()}
    if run_id:
        root = ASSETS / "executions" / run_id
        done_dir = root / ".stage_done"
        done = list(done_dir.glob("*")) if done_dir.is_dir() else []
        status["done"] = len(done)
        status["master"] = (root / "master" / "master.wav").is_file()
        status["publish"] = (done_dir / "podcast_publish").is_file()
        status["cover"] = (done_dir / "episode_cover_generate").is_file()
        status["complete"] = pipeline_complete(run_id)
        try:
            with urllib.request.urlopen(
                f"{api_base()}/api/runs/{run_id}/job", timeout=15
            ) as resp:
                job = json.loads(resp.read().decode())
            status["job_status"] = job.get("status")
            status["stage"] = job.get("stage") or job.get("current_stage")
            status["message"] = (job.get("message") or "")[:240]
        except Exception as exc:
            status["job_err"] = str(exc)[:160]
    STATUS.write_text(json.dumps(status, indent=2))
    log(
        f"status run={run_id} done={status.get('done')} stage={status.get('stage')} "
        f"job={status.get('job_status')} master={status.get('master')} publish={status.get('publish')}"
    )


def pointed_run() -> str | None:
    for pointer in (RUN_POINTER, ASSETS / "baba_current_run.txt"):
        if not pointer.is_file():
            continue
        pointed = pointer.read_text(encoding="utf-8").strip()
        if pointed and (ASSETS / "executions" / pointed).is_dir():
            return pointed
    return None


def fresh_bind_in_progress() -> bool:
    """True while a fresh launch is binding and the run pointer is still cleared."""
    try:
        from full_auto_daemon_launch import fresh_pending_active
    except ImportError:
        return False
    return fresh_pending_active() and pointed_run() is None


def main() -> None:
    log("keepalive loop start")
    while True:
        run_id = latest_run()
        pointed = pointed_run()
        # Only treat completion as terminal when the authoritative pointer
        # matches a finished run AND the e2e driver has exited. Otherwise a
        # fresh launch (pointer cleared while POST /api/runs is still creating)
        # can latch onto a prior completed exec_* and exit immediately.
        if (
            pointed
            and pipeline_complete(pointed)
            and not e2e_alive()
        ):
            write_status(pointed)
            log(f"DONE pipeline complete run={pointed} — shutting down stack")
            try:
                from full_auto_daemon_launch import shutdown_full_auto_stack

                info = shutdown_full_auto_stack(
                    kill_keepalive=False,
                    kill_server=not _keep_gui_server(),
                    exclude_pid=os.getpid(),
                )
                log(f"stack shutdown: {info}")
            except Exception as exc:
                log(f"stack shutdown failed: {exc}")
            return
        if run_id and pipeline_complete(run_id) and not pointed and e2e_alive():
            log(f"fresh e2e still binding — ignoring prior complete run={run_id}")
            write_status(None)
            time.sleep(15)
            continue
        # Never relaunch once the pointed run has already shipped.
        if pointed and pipeline_complete(pointed):
            write_status(pointed)
            log(f"DONE pointed run complete (e2e may still be finishing) run={pointed}")
            time.sleep(15)
            continue
        if not server_alive():
            if not should_relaunch_server_on_death():
                write_status(pointed or run_id)
                log("server down — GUI serve ended; shutting down stack (not relaunching)")
                try:
                    from full_auto_daemon_launch import shutdown_full_auto_stack

                    info = shutdown_full_auto_stack(
                        kill_keepalive=False,
                        kill_server=False,
                        exclude_pid=os.getpid(),
                    )
                    log(f"stack shutdown: {info}")
                except Exception as exc:
                    log(f"stack shutdown failed: {exc}")
                return
            log("server down — relaunch")
            launch("server")
            time.sleep(3)
        if not e2e_alive():
            pointed = pointed_run()
            halt_id = pointed or run_id
            if halt_id and remutate_exhausted(halt_id):
                write_status(halt_id)
                log(
                    f"STOP: listen_delight remutate exhausted — not relaunching run={halt_id}"
                )
                try:
                    from full_auto_daemon_launch import shutdown_full_auto_stack

                    info = shutdown_full_auto_stack(
                        kill_keepalive=False,
                        kill_server=not _keep_gui_server(),
                        exclude_pid=os.getpid(),
                    )
                    log(f"stack shutdown: {info}")
                except Exception as exc:
                    log(f"stack shutdown failed: {exc}")
                return
            if fresh_bind_in_progress():
                log("fresh bind in progress — waiting")
                write_status(None)
                time.sleep(15)
                continue
            if pointed and not pipeline_complete(pointed):
                log(f"e2e down — resume {pointed}")
                launch("e2e", "--run-id", pointed)
            elif not pointed:
                log("e2e down — fresh")
                launch("e2e", "--fresh")
            elif run_id and pipeline_complete(run_id):
                log(f"DONE incomplete pointer but run complete — shutting down stack run={run_id}")
                write_status(run_id)
                try:
                    from full_auto_daemon_launch import shutdown_full_auto_stack

                    info = shutdown_full_auto_stack(
                        kill_keepalive=False,
                        kill_server=not _keep_gui_server(),
                        exclude_pid=os.getpid(),
                    )
                    log(f"stack shutdown: {info}")
                except Exception as exc:
                    log(f"stack shutdown failed: {exc}")
                return
            time.sleep(3)
        write_status(pointed or run_id)
        time.sleep(45)


if __name__ == "__main__":
    main()

"""Subprocess runners for isolated local AI venvs."""

from __future__ import annotations

import json
import logging
import subprocess
from pathlib import Path
from typing import Any

from interview_mux.operator_subprocess import run_command
from interview_mux.venv_paths import venv_python
from interview_mux.operator_trace import log_api_call, resolve_ctx, resolve_stage

logger = logging.getLogger(__name__)

RUNTIME_IDS = frozenset({"deepfilter", "mmaudio", "mlx", "llm", "speech", "chatterbox", "image"})

_RUNTIME_ALIASES = {"llm": "mlx"}


class LocalRuntimeUnavailable(RuntimeError):
    """Raised when a local runtime venv or script is missing."""


def _canonical_runtime_id(runtime_id: str) -> str:
    return _RUNTIME_ALIASES.get(runtime_id, runtime_id)


def repo_root() -> Path:
    from interview_mux.config import repo_root as _repo_root

    return _repo_root()


def runtime_cfg(runtime_id: str) -> dict[str, Any]:
    from interview_mux.config import merged_config

    rid = _canonical_runtime_id(runtime_id)
    if rid not in RUNTIME_IDS:
        raise ValueError(f"Unknown runtime_id: {runtime_id}")
    runtimes = merged_config().get("local_runtimes") or {}
    row = runtimes.get(runtime_id) or runtimes.get(rid) or {}
    if not isinstance(row, dict):
        return {}
    return row


def runtime_enabled(runtime_id: str) -> bool:
    return bool(runtime_cfg(runtime_id).get("enabled", True))


def resolve_venv_dir(runtime_id: str) -> Path:
    from interview_mux.config import merged_config, repo_root

    rid = _canonical_runtime_id(runtime_id)
    cfg = runtime_cfg(runtime_id)
    rel = cfg.get("venv_dir")
    if not rel:
        defaults = {
            "deepfilter": "ASSETS/local_deepfilter/venv",
            "mmaudio": "ASSETS/local_mmaudio/venv",
            "mlx": "ASSETS/local_llm/venv",
            "llm": "ASSETS/local_llm/venv",
            "speech": "ASSETS/local_speech/venv",
            "chatterbox": "ASSETS/local_chatterbox/venv",
            "image": "ASSETS/local_image/venv",
        }
        rel = defaults.get(rid, f"ASSETS/local_{rid}/venv")
    path = Path(str(rel))
    if not path.is_absolute():
        path = repo_root() / path
    return path


def resolve_venv_python(runtime_id: str) -> Path:
    if not runtime_enabled(runtime_id):
        raise LocalRuntimeUnavailable(f"Local runtime {runtime_id} is disabled in config")
    py = venv_python(resolve_venv_dir(runtime_id))
    if not py.is_file():
        raise LocalRuntimeUnavailable(
            f"Missing venv python for {runtime_id}: {py}. Run ./scripts/bootstrap_venv.sh"
        )
    return py


def runtime_python(runtime_id: str) -> Path:
    """Alias for resolve_venv_python (used by synthesis_fallback / callers)."""
    return resolve_venv_python(runtime_id)


def _default_timeout(runtime_id: str) -> int:
    from interview_mux.config import merged_config

    cfg = merged_config()
    rid = _canonical_runtime_id(runtime_id)
    if rid == "deepfilter":
        block = cfg.get("deepfilter") or {}
        return int(block.get("request_timeout_sec", 600))
    if rid == "mmaudio":
        block = cfg.get("mmaudio") or {}
        return int(block.get("request_timeout_sec", 900))
    if rid == "mlx":
        block = cfg.get("local_llm") or {}
        return int(block.get("request_timeout_sec", 120))
    if rid == "speech":
        block = cfg.get("local_speech") or {}
        return int(block.get("stt_timeout_sec", 3600))
    if rid == "chatterbox":
        block = cfg.get("local_chatterbox") or {}
        return int(block.get("timeout_sec", 600))
    return 600


def run_runtime_script(
    runtime_id: str,
    script_rel: str,
    args: list[str],
    *,
    timeout_sec: int | None = None,
    cwd: Path | None = None,
    env_extra: dict[str, str] | None = None,
    stdin_data: str | None = None,
    ctx: Any = None,
    stage: str | None = None,
) -> subprocess.CompletedProcess[str]:
    python = resolve_venv_python(runtime_id)
    script = repo_root() / script_rel
    if not script.is_file():
        raise LocalRuntimeUnavailable(f"Missing runtime script: {script}")
    cmd = [str(python), str(script), *args]
    script_name = Path(script_rel).name
    label = f"{runtime_id}/{script_name}"
    sid = resolve_stage(stage)
    run = resolve_ctx(ctx)
    if run:
        log_api_call(
            "local_runtime",
            label,
            ctx=run,
            stage=sid,
            detail={"runtime_id": runtime_id, "script": script_rel},
        )
    import os

    from interview_mux.config import huggingface_hub_token

    # Always pass a copy so secrets.env HF tokens reach isolated venvs
    # (huggingface_hub does not read config/secrets/secrets.env itself).
    env = os.environ.copy()
    hf_token = huggingface_hub_token()
    if hf_token:
        env.setdefault("HF_TOKEN", hf_token)
        env.setdefault("HUGGING_FACE_HUB_TOKEN", hf_token)
    if env_extra:
        env.update(env_extra)
    timeout = timeout_sec if timeout_sec is not None else _default_timeout(runtime_id)
    from interview_mux.gpu_exclusive import gpu_exclusive

    consumer = _canonical_runtime_id(runtime_id)
    proc: subprocess.CompletedProcess[str] | None = None
    popen_ref: subprocess.Popen[str] | None = None
    try:
        with gpu_exclusive(consumer, ctx=run, stage=sid):
            if stdin_data is not None:
                if run:
                    popen_ref = subprocess.Popen(
                        cmd,
                        stdin=subprocess.PIPE,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                        text=True,
                        cwd=str(cwd or repo_root()),
                        env=env,
                    )
                    try:
                        stdout, stderr = popen_ref.communicate(stdin_data, timeout=timeout)
                    except subprocess.TimeoutExpired:
                        from interview_mux.hang_escalation import kill_process_tree

                        kill_process_tree(popen_ref)
                        raise
                    if popen_ref.returncode != 0:
                        run.log(
                            f"Local runtime failed (exit {popen_ref.returncode}): {label}",
                            level="error",
                            stage=sid,
                            detail={"stderr": (stderr or "")[:500], "stdout_tail": (stdout or "")[-300:]},
                        )
                    for stream_name, text in (("stdout", stdout), ("stderr", stderr)):
                        for line in (text or "").splitlines():
                            if line.strip():
                                run.log(
                                    line,
                                    level="info" if popen_ref.returncode == 0 else "warning",
                                    stage=sid,
                                    detail={"stream": stream_name, "journey_kind": "execute"},
                                )
                    if popen_ref.returncode == 0:
                        run.log(f"Done: {label}", level="success", stage=sid)
                    proc = subprocess.CompletedProcess(
                        cmd, popen_ref.returncode, stdout or "", stderr or ""
                    )
                else:
                    proc = subprocess.run(
                        cmd,
                        input=stdin_data,
                        cwd=str(cwd or repo_root()),
                        capture_output=True,
                        text=True,
                        timeout=timeout,
                        env=env,
                        check=False,
                    )
                    proc = subprocess.CompletedProcess(
                        cmd, proc.returncode, proc.stdout or "", proc.stderr or ""
                    )
            else:
                proc = run_command(
                    cmd,
                    ctx=run,
                    stage=sid,
                    label=label,
                    cwd=str(cwd or repo_root()),
                    timeout=timeout,
                    capture_output=True,
                    check=False,
                )
    except subprocess.TimeoutExpired as exc:
        try:
            from interview_mux.hang_escalation import kill_process_tree

            kill_process_tree(popen_ref)
        except Exception:
            pass
        out_wav = (env_extra or {}).get("INTERVIEW_MUX_OUT_WAV") or ""
        if not out_wav:
            for i, a in enumerate(args):
                if a in {"--output-wav", "--out-wav", "--output"} and i + 1 < len(args):
                    out_wav = args[i + 1]
                    break
        if out_wav:
            try:
                from pathlib import Path as _P
                from interview_mux.musicgen_runner import usable_musicgen_wav

                p = _P(out_wav)
                if usable_musicgen_wav(
                    p,
                    requested_seconds=float(
                        (env_extra or {}).get("INTERVIEW_MUX_OUT_WAV_SEC") or 3.0
                    ),
                ):
                    return subprocess.CompletedProcess(
                        cmd,
                        0,
                        getattr(exc, "stdout", None) or "",
                        f"timeout after {timeout}s; accepted usable wav",
                    )
            except Exception:
                pass
        raise LocalRuntimeUnavailable(
            f"Local runtime {runtime_id} timed out after {timeout}s"
        ) from exc
    if proc is not None:
        from interview_mux.heavy_task_policy import record_heavy_abort

        record_heavy_abort(consumer, proc.returncode, ctx=run, stage=sid)
        return proc
    raise LocalRuntimeUnavailable(f"Local runtime {runtime_id} produced no result")


def parse_runtime_json_stdout(raw: str) -> dict[str, Any] | None:
    """Parse a JSON object from a local-runtime script's stdout.

    Isolated venvs often print warnings/progress to stdout before the contract
    JSON line. Prefer the last parseable object so a successful generate is not
    treated as ``invalid JSON``.
    """
    text = (raw or "").strip()
    if not text:
        return None
    try:
        candidate = json.loads(text)
        if isinstance(candidate, dict):
            return candidate
    except json.JSONDecodeError:
        pass
    for line in reversed(text.splitlines()):
        line = line.strip()
        if not line or line[0] not in "{[":
            continue
        try:
            candidate = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(candidate, dict):
            return candidate
    start = text.rfind("{")
    if start >= 0:
        try:
            candidate, _ = json.JSONDecoder().raw_decode(text[start:])
        except json.JSONDecodeError:
            candidate = None
        if isinstance(candidate, dict):
            return candidate
    return None


def classify_runtime_error(stderr: str, stdout: str = "", *, returncode: int | None = None) -> str:
    from interview_mux.heavy_task_policy import is_heavy_kill_returncode

    if returncode is not None:
        rc = int(returncode)
        if rc == -9:
            return "sigkill"
        if is_heavy_kill_returncode(rc):
            if rc in {-15, 143}:
                return "sigterm"
            if rc in {-6, 134, 6}:
                return "sigabrt"
            return "sigkill"
    blob = f"{stderr}\n{stdout}".lower()
    if "out of memory" in blob or "oom" in blob or "mps backend out of memory" in blob:
        return "oom"
    if "timed out" in blob or "timeout" in blob:
        return "timeout"
    if "voice" in blob and ("ref" in blob or "reference" in blob or "missing" in blob):
        return "missing_voice_ref"
    if "qc" in blob and "fail" in blob:
        return "qc_fail"
    if not (stdout or "").strip() or "invalid json" in blob:
        return "invalid_json_stdout"
    return "runtime_error"


def persist_runtime_last_error(ctx: Any, payload: dict[str, Any]) -> None:
    if ctx is None:
        return
    try:
        rel = "vo_pickup/local_runtime_last_error.json"
        hist: list[dict[str, Any]] = []
        if ctx.artifact_exists(rel):
            prev = ctx.read_json(rel)
            if isinstance(prev, dict):
                hist = list(prev.get("failures") or [])
            elif isinstance(prev, list):
                hist = list(prev)
        hist.append(payload)
        ctx.write_json(rel, {"version": 1, "failures": hist[-12:]})
    except Exception:
        pass


def _usable_runtime_out_wav(path: Path, *, min_bytes: int = 256) -> bool:
    """True when dest WAV exists and looks like a real take (not empty stub)."""
    try:
        if not path.is_file():
            return False
        if path.stat().st_size < int(min_bytes):
            return False
    except OSError:
        return False
    try:
        import wave

        with wave.open(str(path), "rb") as wf:
            rate = int(wf.getframerate() or 0)
            frames = int(wf.getnframes() or 0)
        return rate > 0 and frames > 0
    except Exception:
        # Non-wave container still counts if non-trivial bytes landed.
        return True


def run_runtime_json(
    runtime_id: str,
    script_rel: str,
    payload: dict[str, Any],
    *,
    timeout_sec: int | None = None,
    ctx: Any = None,
    stage: str | None = None,
) -> dict[str, Any]:
    proc = run_runtime_script(
        runtime_id,
        script_rel,
        [],
        timeout_sec=timeout_sec,
        stdin_data=json.dumps(payload),
        ctx=ctx,
        stage=stage,
    )
    parsed = parse_runtime_json_stdout(proc.stdout or "") or parse_runtime_json_stdout(
        proc.stderr or ""
    )
    likely = classify_runtime_error(proc.stderr or "", proc.stdout or "", returncode=proc.returncode)
    event = {
        "runtime_id": runtime_id,
        "script": script_rel,
        "returncode": proc.returncode,
        "parsed_ok": parsed is not None,
        "error": (parsed or {}).get("error") if isinstance(parsed, dict) else None,
        "stderr_tail": (proc.stderr or "")[-500:],
        "likely_cause": likely,
        "stage": stage,
        "line_id": (payload or {}).get("line_id"),
    }

    def _accept_landed_wav(*, cause: str) -> dict[str, Any] | None:
        """Chatterbox may write WAV then pollute stdout (pkg_resources) — accept bytes."""
        out_raw = str((payload or {}).get("out_wav") or "").strip()
        if not out_raw:
            return None
        out_path = Path(out_raw)
        if not _usable_runtime_out_wav(out_path):
            return None
        if ctx is not None:
            try:
                ctx.log(
                    f"local_runtime {runtime_id}: accepted usable out_wav after {cause}",
                    level="warning",
                    stage=stage or runtime_id,
                    detail={
                        **event,
                        "out_wav": out_raw,
                        "accepted_despite": cause,
                    },
                )
            except Exception:
                pass
        return {
            "ok": True,
            "out_wav": out_raw,
            "accepted_despite": cause,
            "likely_cause": cause,
        }

    if ctx is not None:
        try:
            ctx.log(
                f"local_runtime {runtime_id} rc={proc.returncode} parsed={event['parsed_ok']} cause={likely}",
                level="error" if proc.returncode else "info",
                stage=stage or runtime_id,
                detail=event,
            )
        except Exception:
            pass
        if proc.returncode != 0 or parsed is None:
            persist_runtime_last_error(ctx, event)
    if parsed is not None:
        if proc.returncode != 0 or parsed.get("ok") is False:
            accepted = _accept_landed_wav(cause=str(likely or "runtime_failed"))
            if accepted is not None:
                return accepted
            err = str(parsed.get("error") or "runtime failed")[:500]
            raise LocalRuntimeUnavailable(
                f"Local runtime {runtime_id} failed: {err} (likely_cause={likely})"
            )
        return parsed
    accepted = _accept_landed_wav(cause=str(likely or "invalid_json_stdout"))
    if accepted is not None:
        return accepted
    err = (proc.stderr or proc.stdout or "").strip()[:500]
    raise LocalRuntimeUnavailable(
        f"Local runtime {runtime_id} failed: {err or 'invalid JSON'} (likely_cause={likely})"
    )


def write_install_manifest(
    stack_dir: Path,
    *,
    runtime_id: str,
    repo_url: str,
    repo_dir: Path,
    venv_dir: Path,
    verified: bool,
) -> Path:
    """Write ASSETS/local_*/install.json after bootstrap verify."""
    from datetime import datetime, timezone

    commit = ""
    git_dir = repo_dir / ".git"
    if git_dir.is_dir():
        try:
            proc = subprocess.run(
                ["git", "-C", str(repo_dir), "rev-parse", "HEAD"],
                capture_output=True,
                text=True,
                timeout=15,
                check=False,
            )
            if proc.returncode == 0:
                commit = proc.stdout.strip()
        except (OSError, subprocess.TimeoutExpired):
            pass
    payload = {
        "runtime_id": runtime_id,
        "repo_url": repo_url,
        "repo_dir": str(repo_dir),
        "repo_commit": commit,
        "venv_dir": str(venv_dir),
        "verified": verified,
        "verified_at": datetime.now(timezone.utc).isoformat(),
    }
    dest = stack_dir / "install.json"
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    return dest

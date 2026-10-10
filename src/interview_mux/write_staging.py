"""Per-stage write staging — outputs land in .pending_writes/ until auto-commit."""

from __future__ import annotations

import os
import shutil
from contextvars import ContextVar
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from filelock import FileLock

from interview_mux.file_store import lock_path_for
from interview_mux.file_store import read_json as fs_read_json
from interview_mux.file_store import read_text as fs_read_text
from interview_mux.file_store import write_json as fs_write_json
from interview_mux.file_store import write_text as fs_write_text
from interview_mux.run_context import RunContext

# Re-export for tests that monkeypatch write_staging.merged_config.
from interview_mux.config import merged_config  # noqa: F401

_active_stage: ContextVar[str | None] = ContextVar("write_staging_stage", default=None)

OPERATIONAL_REL_PATHS = frozenset(
    {
        "run_meta.json",
        "gui_job.json",
        "gui_log.jsonl",
    }
)


def write_approval_enabled() -> bool:
    """v2 auto-commit disables operator write-approval pauses."""
    from interview_mux.v2.config import v2_auto_commit

    return not v2_auto_commit()


def active_stage() -> str | None:
    """Stage id currently executing (write staging context)."""
    return _active_stage.get()


# Stages sealed in this process (stage_id -> run_dir). A write made under a
# stage's staging context after its seal and flush used to land in a staging
# dir nobody would flush again: the walk's bookkeeping (homunculus memory and
# ledger, analysis_state, llm_calls index) arrived 0.2 to 5 s after the marker
# on every stage and stayed there forever (ISSUES 118). Once sealed, the
# stage's writes go to the committed tree, where the flush would have put them.
_SEALED: dict[str, str] = {}


def note_stage_sealed(ctx: RunContext, stage_id: str) -> None:
    _SEALED[str(stage_id)] = str(ctx.run_dir)


def stage_sealed_here(ctx: RunContext, stage_id: str | None) -> bool:
    """True while ``stage_id`` is sealed in this process and its marker stands.

    A cleared marker (the hollow guard unmarking a stage for a rerun) voids
    the seal: the rerun stages its writes again whether or not the runner
    re-entered the staging context.
    """
    if not stage_id or _SEALED.get(str(stage_id)) != str(ctx.run_dir):
        return False
    try:
        return bool(ctx.is_done(str(stage_id)))
    except Exception:
        return False


def enter_stage_staging(stage_id: str) -> None:
    _SEALED.pop(str(stage_id), None)
    sid = str(stage_id or "").strip()
    _active_stage.set(sid or stage_id)


def preflight_stage_enter(ctx: RunContext, stage_id: str) -> None:
    """Call before enter_stage_staging when a RunContext is available."""
    sid = str(stage_id or "").strip()
    if not sid:
        return
    try:
        from interview_mux.artifact_ownership import AuthorityDenied, stage_enter_preflight

        ok, reason, pin = stage_enter_preflight(ctx, sid)
        if not ok:
            raise AuthorityDenied(
                f"authority_denied:stage_enter:{sid}:{reason}:pin={pin}",
                path="",
                stage_key=sid,
                suggested_owner=pin,
                verb="execute",
            )
    except ImportError:
        pass


def exit_stage_staging() -> None:
    _active_stage.set(None)


def active_stage_id() -> str | None:
    """Currently entered staging stage (None when not inside run_wrapped_stage)."""
    return _active_stage.get()


def _open_nested_stage_row(ctx: RunContext, stage_id: str) -> bool:
    """Append a ``started`` stage row unless one is already open for ``stage_id``."""
    try:
        from interview_mux.homunculus.ledger import append_ledger, read_ledger
        from interview_mux.homunculus.runtime import has_dispatch_ledger

        from interview_mux.homunculus.budget import attempt_cap

        if not has_dispatch_ledger(ctx):
            return False
        # mix / master_finalize / complete_master count stage rows as cycles;
        # junction's nested mix makes no LLM call and must not spend one.
        if attempt_cap(stage_id)[1] != "max_invokes_per_identity":
            return False
        started = closed = 0
        for row in read_ledger(ctx):
            if row.get("identity") != stage_id or row.get("kind") != "stage":
                continue
            if row.get("status") == "started":
                started += 1
            elif row.get("status") in {"failed", "done"}:
                closed += 1
        if started > closed:
            return False
        append_ledger(
            ctx,
            {"kind": "stage", "identity": stage_id, "status": "started", "source": "nested"},
        )
        return True
    except Exception:
        return False


def _close_nested_stage_row(ctx: RunContext, stage_id: str, opened: bool, status: str) -> None:
    if not opened:
        return
    try:
        from interview_mux.homunculus.ledger import append_ledger

        append_ledger(
            ctx,
            {"kind": "stage", "identity": stage_id, "status": status, "source": "nested"},
        )
    except Exception:
        pass


def run_nested_staged_stage(ctx: RunContext, stage_id: str, fn: Any) -> None:
    """Run ``fn`` in ``stage_id`` staging and auto-commit, then restore the parent stage.

    In-process follow-ups (e.g. classification after ``boundary_topic_resplit``) otherwise
    write into the parent's ``.pending_writes/`` and are dropped on flush because they are
    not in the parent's ``STAGE_BY_ID.artifacts``.
    """
    parent = _active_stage.get()
    # One nested run is one invoke of stage_id. Without an open stage row every
    # LLM call inside it (each classification shard, each schema retry) was
    # counted against max_invokes_per_identity, so a 5-shard classification
    # nested in boundary_topic_resplit was refused at shard 4 (exec_034).
    ledger_open = _open_nested_stage_row(ctx, stage_id)
    status = "failed"
    enter_stage_staging(stage_id)
    try:
        fn()
        after_stage_write_check(ctx, stage_id)
        status = "done"
        # Re-stamp fingerprints for known stage artifacts so downstream stale
        # guards (e.g. sonic_context_build ← content_brief) see a coherent hash
        # even if a post-commit heal rewrote the body under another stage key.
        try:
            from interview_mux.artifact_lifecycle import (
                STAGE_ARTIFACT_DISK_PATHS,
                restamp_committed_artifact,
            )

            rel = STAGE_ARTIFACT_DISK_PATHS.get(stage_id)
            if rel and ctx.artifact_exists(rel):
                restamp_committed_artifact(ctx, rel, producer_stage=stage_id)
        except Exception:
            pass
    finally:
        if parent:
            enter_stage_staging(parent)
        else:
            exit_stage_staging()
        # Closed outside the nested staging, which a failed run discards.
        _close_nested_stage_row(ctx, stage_id, ledger_open, status)


def is_operational_path(rel: str) -> bool:
    if rel in OPERATIONAL_REL_PATHS:
        return True
    if rel.startswith(".stage_done/"):
        return True
    return False


def staging_root(ctx: RunContext, stage_id: str) -> Path:
    return ctx.run_dir / ".pending_writes" / stage_id


def staged_path(ctx: RunContext, rel: str, *, stage_id: str | None = None) -> Path:
    sid = stage_id or _active_stage.get()
    if not sid:
        return ctx.run_dir.joinpath(*rel.split("/"))
    return staging_root(ctx, sid).joinpath(*rel.split("/"))


VO_PICKUP_OWNER_STAGES = frozenset({"vo_synthesize", "vo_ingest", "audio_preclean"})


def is_vo_pickup_rel(rel: str) -> bool:
    """True for vo_pickup files/dirs (WAV bind authority lives here)."""
    norm = str(rel or "").replace("\\", "/").lstrip("./")
    return norm == "vo_pickup" or norm.startswith("vo_pickup/")


def _vo_pickup_line_id_from_rel(rel: str) -> str | None:
    """``vo_pickup/.../{line_id}.wav`` → line_id; else None."""
    norm = str(rel or "").replace("\\", "/").lstrip("./")
    if not is_vo_pickup_rel(norm) or not norm.endswith(".wav"):
        return None
    return Path(norm).stem or None


def _should_skip_stale_vo_pickup_promote(
    ctx: RunContext, *, src: Path, dest: Path, rel: str
) -> bool:
    """Refuse pending VO WAV that would clobber a sha-bound audited take.

    Stale ``.pending_writes/vo_synthesize/vo_pickup/*.wav`` (exec_11630) can
    overwrite a fresh G1 take whose synthesis_report sha still matches gap
    script — leaving ``wav_content_mismatch`` and EDL missing-WAV thrash.

    Protect only when dest/audit is still script-fresh. Script-stale, missing
    line, or script-check errors → do not skip (prefer promote over thrash).
    """
    if not is_vo_pickup_rel(rel) or not str(rel).endswith(".wav"):
        return False
    if not dest.is_file() or not src.is_file():
        return False
    lid = _vo_pickup_line_id_from_rel(rel)
    if not lid:
        return False
    try:
        from interview_mux.vo_synthesis_audit import (
            _vo_pickup_script_lines,
            synthesis_entry_for_line,
            synthesis_entry_matches_line,
            wav_content_sha256,
        )

        entry = synthesis_entry_for_line(ctx, lid)
        if not isinstance(entry, dict):
            return False
        want = str(entry.get("wav_sha256") or "").strip()
        if not want:
            return False
        dest_sha = wav_content_sha256(dest)
        src_sha = wav_content_sha256(src)
        if not (dest_sha == want and src_sha != want):
            return False
        # Dest matches audit; pending differs — only protect if script-fresh.
        try:
            line = _vo_pickup_script_lines(ctx).get(lid)
            if not isinstance(line, dict):
                ctx.log(
                    f"allow pending VO promote {rel} (script_check_failed: no line)",
                    level="warning",
                    stage=str(active_stage_id() or "vo_synthesize"),
                    detail={
                        "line_id": lid,
                        "reason": "script_check_failed",
                        "dest_sha": dest_sha[:16],
                        "src_sha": src_sha[:16],
                    },
                )
                return False
            matches, _reason = synthesis_entry_matches_line(ctx, line)
            if not matches:
                ctx.log(
                    f"allow pending VO promote {rel} (script_stale_allow)",
                    level="warning",
                    stage=str(active_stage_id() or "vo_synthesize"),
                    detail={
                        "line_id": lid,
                        "reason": "script_stale_allow",
                        "dest_sha": dest_sha[:16],
                        "src_sha": src_sha[:16],
                    },
                )
                return False
        except Exception as exc:
            ctx.log(
                f"allow pending VO promote {rel} (script_check_failed: {exc})",
                level="warning",
                stage=str(active_stage_id() or "vo_synthesize"),
                detail={
                    "line_id": lid,
                    "reason": "script_check_failed",
                    "dest_sha": dest_sha[:16],
                    "src_sha": src_sha[:16],
                },
            )
            return False
        ctx.log(
            f"skip stale pending VO promote {rel} "
            f"(sha_protect: dest matches audit+script; pending differs)",
            level="warning",
            stage=str(active_stage_id() or "vo_synthesize"),
            detail={
                "line_id": lid,
                "reason": "sha_protect",
                "dest_sha": dest_sha[:16],
                "src_sha": src_sha[:16],
            },
        )
        return True
    except Exception:
        return False


def _discard_skipped_stale_vo_pending(src: Path) -> None:
    """End-B: remove skipped pending so flush/orphan cannot re-promote it."""
    try:
        if src.is_file():
            src.unlink()
    except OSError:
        pass


def _record_contract_touch(
    ctx: RunContext, rel: str, stage_id: str | None, *, write: bool
) -> None:
    """Contract conformance recorder seam (plan §4.1) — off unless MUX_CONTRACT_RECORD=1.

    Both path resolvers are hot, so the whole body is behind one cached flag read
    and it never raises: an observer must not be able to fail a dispatch.
    """
    if not stage_id:
        return
    try:
        from interview_mux.contract_conformance import (
            note_read,
            note_write,
            recording_enabled,
        )

        if not recording_enabled():
            return
        (note_write if write else note_read)(ctx, rel, stage_id)
    except Exception:
        return


def resolve_write_path(ctx: RunContext, rel: str) -> Path:
    """Return staging path when a stage is active; else the committed run path.

    Non-owner stages (especially ``edl``) must not write ``vo_pickup/`` into their
    own pending tree — those copies flush as stale sha-mismatched takes (F2 / exec_11165).
    """
    sid = _active_stage.get()
    _record_contract_touch(ctx, rel, sid, write=True)
    if not sid or is_operational_path(rel):
        return ctx.run_dir.joinpath(*rel.split("/"))
    if stage_sealed_here(ctx, sid):
        # A sealed stage's later writes go where the flush would have put
        # them (ISSUES 118). Only the write target moves: staged_path stays
        # the staging location for readers and the pre-flush barrier, which
        # the first version of this fix broke (exec_096: "cannot read staged
        # file" at the committed path on every re-entry of a sealed stage).
        return ctx.run_dir.joinpath(*rel.split("/"))
    if is_vo_pickup_rel(rel) and sid not in VO_PICKUP_OWNER_STAGES:
        return staged_path(ctx, rel, stage_id="vo_synthesize")
    return staged_path(ctx, rel, stage_id=sid)


def write_committed_json(
    ctx: RunContext,
    rel: str,
    data: Any,
    *,
    stage_key: str | None = None,
    mutation_class: str | None = None,
) -> Path:
    """Persist to the committed run tree without opening a new staging root."""
    try:
        from interview_mux.artifact_ownership import assert_write, skip_foreign_side_effect
        from interview_mux.write_staging import active_stage_id

        sk = stage_key or active_stage_id()
        if skip_foreign_side_effect(
            ctx, rel, stage_key=stage_key, role=None, mutation_class=mutation_class
        ):
            return ctx.run_dir.joinpath(*str(rel).replace("\\", "/").lstrip("/").split("/"))
        assert_write(
            ctx,
            rel,
            sk,
            role="producer" if sk else "ops",
            verb="persist",
            mutation_class=mutation_class,
        )
    except ImportError:
        pass
    if rel == "understanding/gap_report.json" and isinstance(data, dict):
        try:
            from interview_mux.artifact_ownership import assert_gap_report_body_sole_writer

            prior = None
            try:
                if ctx.artifact_exists(rel):
                    prior = ctx.read_json(rel)
            except Exception:
                prior = None
            assert_gap_report_body_sole_writer(
                ctx,
                stage_key=stage_key,
                prior=prior,
                new=data,
                mutation_class=mutation_class,
            )
        except ImportError:
            pass
    if isinstance(data, dict):
        from interview_mux.artifact_writes import _prepare_for_disk_validation
        from interview_mux.edl_source_contract import prepare_edl_payload_for_disk
        from interview_mux.prompt_validation import validate_artifact_write

        data = prepare_edl_payload_for_disk(ctx, rel, data)
        payload = _prepare_for_disk_validation(data, rel_path=rel, stage_key=stage_key)
        try:
            from interview_mux.artifact_sanitize.one_writer import maybe_admit_hot_write

            admitted = maybe_admit_hot_write(
                ctx,
                rel,
                payload,
                stage_key=stage_key,
                skip_handoff=True,
                write_committed=True,
                reason=stage_key or "write_committed_json",
            )
            if admitted is not None:
                return admitted
        except ImportError:
            pass
        errors = validate_artifact_write(rel, payload)
        if errors:
            raise ValueError(
                f"{rel}: schema validation failed — " + "; ".join(errors[:6])
            )
        data = payload
    return write_mirrored_json(ctx, rel, data)


def write_mirrored_json(ctx: RunContext, rel: str, data: Any) -> Path:
    """Write JSON to the committed final path and sync existing pending copies.

    Do **not** invent a new active-stage pending twin after commit — that leaves
    pending mtime newer than final and trips HC-3 ``newer uncommitted pending``
    on the same stage's ``heal_or_raise`` (exec_13170 selection_order_sanitize).
    Existing pending files are updated in place so mid-stage readers stay current.
    """
    from interview_mux.edl_source_contract import prepare_edl_payload_for_disk

    data = prepare_edl_payload_for_disk(ctx, rel, data)
    final = ctx.final_path(*rel.split("/"))
    final.parent.mkdir(parents=True, exist_ok=True)
    fs_write_json(final, data)

    pending_root = ctx.run_dir / ".pending_writes"
    if pending_root.is_dir():
        for stage_dir in sorted(pending_root.iterdir()):
            if not stage_dir.is_dir():
                continue
            candidate = stage_dir.joinpath(*rel.split("/"))
            if candidate.is_file():
                fs_write_json(candidate, data)
    return final


def write_mirrored_text(ctx: RunContext, rel: str, text: str) -> Path:
    """Same as write_mirrored_json for plain-text artifacts."""
    final = ctx.final_path(*rel.split("/"))
    final.parent.mkdir(parents=True, exist_ok=True)
    fs_write_text(final, text)

    pending_root = ctx.run_dir / ".pending_writes"
    if pending_root.is_dir():
        for stage_dir in sorted(pending_root.iterdir()):
            if not stage_dir.is_dir():
                continue
            candidate = stage_dir.joinpath(*rel.split("/"))
            if candidate.is_file():
                fs_write_text(candidate, text)
    return final


# Owner-body JSON that must not flush from foreign pending (0F / End-B cousin).
_OWNER_PROMOTE_JSON: frozenset[str] = frozenset(
    {
        "understanding/gap_report.json",
        "understanding/reorder_bridges.json",
        "master/transitions.json",
        "master/selection.json",
        "mastering/mastering_plan.json",
    }
)


def _may_promote_pending(ctx: RunContext, rel: str, stage_id: str) -> bool:
    """True when stage may flush this pending path (owner or non-gated side-effect)."""
    norm = str(rel or "").replace("\\", "/").lstrip("./")
    wav_owned = norm.endswith(".wav") and (
        norm.startswith("master/transitions/") or norm.startswith("vo_pickup/")
    )
    if rel not in _OWNER_PROMOTE_JSON and not wav_owned:
        return True
    try:
        from interview_mux.artifact_ownership import write_permitted

        ok, _reason = write_permitted(
            ctx, rel, stage_id, role="producer", verb="promote_pending"
        )
        return bool(ok)
    except Exception:
        # Fail-open only when ownership module missing; otherwise refuse foreign.
        return False


# Directory promotes (`rel` ending in "/") never consulted ownership at all:
# `_may_promote_pending` runs on the file branch only, so a DENY row on a
# directory artifact was documented and never imposed. Verdict is computed per
# child always and *acted on* for prefixes on this ratchet, mirroring
# `contract_conformance.STRICT_GROUPS`. `master/transitions/*.wav` now has a
# catalog row (producers + glue promote_pending ALLOWs), so the prefix is armed.
# Only ever grows.
PROMOTE_DIR_STRICT_PREFIXES: tuple[str, ...] = ("master/transitions/",)

_ENV_PROMOTE_DIR_STRICT = "MUX_PROMOTE_DIR_STRICT_PREFIXES"

# (stage_id, dir rel, child rel, reason, fatal) for every refused-or-would-be
# refused child in this process. Tests read it; the campaign reads the log.
_dir_promote_refusals: list[dict[str, Any]] = []


def promote_dir_strict_prefixes() -> tuple[str, ...]:
    """``MUX_PROMOTE_DIR_STRICT_PREFIXES`` overrides the ratchet (comma list)."""
    raw = os.environ.get(_ENV_PROMOTE_DIR_STRICT)
    if raw is None:
        return PROMOTE_DIR_STRICT_PREFIXES
    return tuple(p.strip() for p in raw.split(",") if p.strip())


def promote_dir_refusal_is_fatal(rel: str) -> bool:
    """True when a refused child under ``rel`` is discarded instead of reported."""
    norm = str(rel or "").replace("\\", "/").lstrip("./")
    return any(norm.startswith(prefix) for prefix in promote_dir_strict_prefixes())


def observed_dir_promote_refusals() -> list[dict[str, Any]]:
    return list(_dir_promote_refusals)


def clear_dir_promote_refusals() -> None:
    _dir_promote_refusals.clear()


def _dir_promote_permitted(ctx: RunContext, child: str, stage_id: str) -> tuple[bool, str]:
    """Ownership verdict for one child of a directory promote.

    Fails *open* when the ownership module itself raises: a directory promote
    carries rendered media (transition WAVs, VO takes), and losing bytes to an
    unrelated ownership bug is worse than promoting a foreign one. An explicit
    DENY / not_allow / unknown_path still refuses.
    """
    try:
        from interview_mux.artifact_ownership import write_permitted

        ok, reason = write_permitted(
            ctx, child, stage_id, role="producer", verb="promote_pending"
        )
    except Exception as exc:
        return True, f"ownership_unavailable:{type(exc).__name__}"
    return bool(ok), str(reason or "")


def _note_dir_promote_refusal(
    ctx: RunContext,
    *,
    stage_id: str,
    rel: str,
    child: str,
    reason: str,
    fatal: bool,
) -> None:
    _dir_promote_refusals.append(
        {
            "stage_id": stage_id,
            "rel": rel,
            "child": child,
            "reason": reason,
            "fatal": fatal,
        }
    )
    verdict = "refused" if fatal else "would refuse (report-only)"
    try:
        ctx.log(
            f"promote {rel} child {child}: {verdict} for {stage_id} ({reason})",
            level="warning",
            stage=stage_id,
            detail={"rel": rel, "child": child, "reason": reason, "fatal": fatal},
        )
    except Exception:
        pass


def promote_staged_side_effects(
    ctx: RunContext,
    rels: list[str] | tuple[str, ...],
    *,
    stage_id: str | None = None,
) -> list[str]:
    """Commit staged paths that StageInfo does not claim (side-effect remasters).

    Staging flush only promotes operator-visible StageInfo outputs, then deletes
    the staging tree.  Junction remasters of EDL/assembly must land in the
    committed run tree or the repairs vanish on stage completion.

    Owner JSON bodies (gap_report / transitions / selection / plan) require
    ``verb=promote_pending`` ownership — foreign pending is discarded, not sealed.

    A directory rel resolves ownership per child. Refusals are report-only
    unless the rel is on ``PROMOTE_DIR_STRICT_PREFIXES``.
    """
    sid = stage_id or _active_stage.get()
    if not sid:
        return []
    root = staging_root(ctx, sid)
    if not root.is_dir():
        return []
    from interview_mux.file_store import atomic_copy

    flushed: list[str] = []
    for rel in rels:
        if not rel:
            continue
        if is_vo_pickup_rel(rel) and sid not in VO_PICKUP_OWNER_STAGES:
            continue
        if rel.endswith("/"):
            src_dir = root.joinpath(*rel.rstrip("/").split("/"))
            if not src_dir.is_dir():
                continue
            for src in sorted(src_dir.rglob("*")):
                if not src.is_file():
                    continue
                child = str(src.relative_to(root)).replace("\\", "/")
                if is_vo_pickup_rel(child) and sid not in VO_PICKUP_OWNER_STAGES:
                    continue
                ok, reason = _dir_promote_permitted(ctx, child, sid)
                if not ok:
                    fatal = promote_dir_refusal_is_fatal(rel)
                    _note_dir_promote_refusal(
                        ctx,
                        stage_id=sid,
                        rel=rel,
                        child=child,
                        reason=reason,
                        fatal=fatal,
                    )
                    if fatal:
                        try:
                            src.unlink()
                        except OSError:
                            pass
                        continue
                dest = ctx.final_path(*child.split("/"))
                if _should_skip_stale_vo_pickup_promote(ctx, src=src, dest=dest, rel=child):
                    _discard_skipped_stale_vo_pending(src)
                    continue
                dest.parent.mkdir(parents=True, exist_ok=True)
                atomic_copy(src, dest)
                flushed.append(child)
            continue
        src = root.joinpath(*rel.split("/"))
        if not src.is_file():
            continue
        if not _may_promote_pending(ctx, rel, sid):
            try:
                src.unlink()
            except OSError:
                pass
            continue
        dest = ctx.final_path(*rel.split("/"))
        if _should_skip_stale_vo_pickup_promote(ctx, src=src, dest=dest, rel=rel):
            _discard_skipped_stale_vo_pending(src)
            continue
        prior_spoken: dict | None = None
        if rel in {
            "understanding/gap_report.json",
            "master/transitions.json",
        } and dest.is_file():
            try:
                prior_spoken = fs_read_json(dest)
            except Exception:
                prior_spoken = None
        dest.parent.mkdir(parents=True, exist_ok=True)
        atomic_copy(src, dest)
        flushed.append(rel)
        if rel == "understanding/gap_report.json":
            try:
                from interview_mux.vo_synthesis_audit import (
                    maybe_propagate_gap_spoken_text_change,
                )

                new_doc = fs_read_json(dest)
                if isinstance(new_doc, dict):
                    maybe_propagate_gap_spoken_text_change(
                        ctx,
                        prior_report=prior_spoken
                        if isinstance(prior_spoken, dict)
                        else None,
                        new_report=new_doc,
                        stage=sid or "promote_gap_report",
                    )
            except Exception:
                pass
        elif rel == "master/transitions.json":
            try:
                from interview_mux.transition_vo import (
                    maybe_propagate_transitions_spoken_text_change,
                )

                new_doc = fs_read_json(dest)
                if isinstance(new_doc, dict):
                    maybe_propagate_transitions_spoken_text_change(
                        ctx,
                        prior_doc=prior_spoken if isinstance(prior_spoken, dict) else None,
                        new_doc=new_doc,
                        stage=sid or "promote_transitions",
                    )
            except Exception:
                pass
    from interview_mux.edl_source_contract import heal_committed_edl_source_paths

    heal_committed_edl_source_paths(ctx)
    try:
        from interview_mux.vo_synthesis_audit import canonicalize_synthesis_out_wav_paths
        from interview_mux.transition_vo import restamp_edl_transition_source_paths

        canonicalize_synthesis_out_wav_paths(ctx)
        if any(str(r).startswith("master/transitions") for r in flushed) or any(
            str(r).startswith("master/transitions") for r in rels
        ):
            restamp_edl_transition_source_paths(ctx)
    except Exception:
        pass
    return flushed


def _transcript_has_operator_edits(doc: Any) -> bool:
    if not isinstance(doc, dict):
        return False
    if doc.get("review_applied_at"):
        return True
    words = doc.get("words") or []
    return any(isinstance(w, dict) and w.get("corrected") for w in words)


def _should_preserve_committed_transcript(ctx: RunContext, rel: str, staged_src: Path) -> bool:
    """True when promoting staged STT would clobber committed operator corrections."""
    if rel != "transcript/full.json":
        return False
    dest = ctx.run_dir.joinpath(*rel.split("/"))
    if not dest.is_file() or not staged_src.is_file():
        return False
    try:
        committed = fs_read_json(dest)
        staged = fs_read_json(staged_src)
    except Exception:
        return False
    return _transcript_has_operator_edits(committed) and not _transcript_has_operator_edits(staged)


def staging_approval_hint(ctx: RunContext, rel: str) -> str | None:
    if write_approval_enabled():
        if is_operational_path(rel):
            return None
        if ctx.run_dir.joinpath(*rel.split("/")).is_file():
            return None
        pending_root = ctx.run_dir / ".pending_writes"
        if not pending_root.is_dir():
            return None
        for stage_dir in sorted(pending_root.iterdir()):
            if not stage_dir.is_dir():
                continue
            candidate = stage_dir.joinpath(*rel.split("/"))
            if candidate.is_file():
                return (
                    f"{rel} is awaiting write approval for stage '{stage_dir.name}' — "
                    "open the review modal and choose Save & continue before running later stages."
                )
    return None


def staging_read_trap_hint(ctx: RunContext, rel: str) -> str | None:
    """Hint when an approved artifact exists but the active staging write root would miss it."""
    if is_operational_path(rel):
        return None
    resolved = resolve_read_path(ctx, rel)
    if not resolved.is_file():
        return staging_approval_hint(ctx, rel)
    staged = staged_path(ctx, rel)
    if staged == resolved or staged.is_file():
        return None
    return (
        f"{rel} exists at {resolved} but not under the active staging directory ({staged}). "
        "Prior-stage inputs must be read via read_path(), not path()."
    )


def _legacy_read_alias(ctx: RunContext, rel: str) -> Path | None:
    """Pre single-flow migration: flow_1_master/ mirrors master/."""
    if rel.startswith("master/"):
        legacy = ctx.run_dir / "flow_1_master" / rel[len("master/") :]
        if legacy.is_file():
            return legacy
    return None


def resolve_read_path(ctx: RunContext, rel: str) -> Path:
    """Committed tree, plus this stage's in-flight pending only (HC-3).

    Leftover ``.pending_writes`` from a crashed or other stage must not shadow
    reads. The live producer still sees its own staged files via ``_active_stage``.
    """
    sid = _active_stage.get()
    _record_contract_touch(ctx, rel, sid, write=False)
    if is_operational_path(rel):
        return ctx.run_dir.joinpath(*rel.split("/"))
    if sid:
        staged = staged_path(ctx, rel, stage_id=sid)
        if staged.is_file():
            return staged
    primary = ctx.run_dir.joinpath(*rel.split("/"))
    if primary.is_file():
        return primary
    legacy = _legacy_read_alias(ctx, rel)
    if legacy is not None:
        return legacy
    return primary


def uncommitted_pending_reason(ctx: RunContext, rel: str) -> str | None:
    """HC-3 / 2B: pending-only or newer pending than commit is not complete.

    Leftover ``.pending_writes`` from a *different* stage must not block
    completeness of the artifact's canonical producer (exec_11630:
    ``gap_framing_compose`` pending ``gap_evaluations.json`` falsely
    incomplete'd ``missing_framing`` and thrash-looped delivery).
    """
    if is_operational_path(rel):
        return None
    pending = pending_stage_for_path(ctx, rel)
    if not pending:
        return None
    active = _active_stage.get()
    canonical = None
    try:
        from interview_mux.prompt_validation import STAGE_ARTIFACT_DISK_PATHS

        for sid, path in STAGE_ARTIFACT_DISK_PATHS.items():
            if path == rel:
                canonical = str(sid)
                break
    except Exception:
        canonical = None
    if canonical is None and rel == "understanding/gap_evaluations.json":
        canonical = "missing_framing"
    if (
        pending
        and pending != active
        and canonical
        and pending != canonical
    ):
        return None
    staged = staged_path(ctx, rel, stage_id=pending)
    if not staged.is_file():
        return None
    primary = ctx.run_dir.joinpath(*rel.split("/"))
    if not primary.is_file():
        return f"{rel} is pending_only"
    try:
        if staged.stat().st_mtime_ns <= primary.stat().st_mtime_ns:
            return None
    except OSError:
        return f"{rel} has newer uncommitted pending"
    # Mirrored commit sync can leave pending mtime newer with identical bytes
    # (write_mirrored updates existing shadows after final). Content-equal is sealed.
    try:
        if staged.read_bytes() == primary.read_bytes():
            return None
    except OSError:
        pass
    return f"{rel} has newer uncommitted pending"


def artifact_exists_resolved(ctx: RunContext, rel: str) -> bool:
    if is_operational_path(rel):
        return ctx.run_dir.joinpath(*rel.split("/")).is_file()
    return resolve_read_path(ctx, rel).is_file()


def _stage_output_spec_matches(rel: str, spec: str) -> bool:
    import fnmatch

    if spec.startswith("glob:"):
        return fnmatch.fnmatch(rel, spec[5:])
    if spec.endswith("/"):
        prefix = spec.rstrip("/") + "/"
        return rel == spec.rstrip("/") or rel.startswith(prefix)
    return rel == spec


_AUDIO_OUTPUT_SUFFIXES = (".wav", ".mp3", ".m4a", ".aac", ".flac", ".ogg")


def expand_audio_output_paths(ctx: RunContext, specs: Iterable[str]) -> list[str]:
    """Resolve stage audio_outputs specs (exact, glob:, dir/) to relative file paths."""
    found: list[str] = []
    seen: set[str] = set()
    expandable = [s for s in specs if s.startswith("glob:") or s.endswith("/")]
    for spec in specs:
        if spec.startswith("glob:") or spec.endswith("/"):
            continue
        if artifact_exists_resolved(ctx, spec) and spec not in seen:
            found.append(spec)
            seen.add(spec)
    if expandable and ctx.run_dir.is_dir():
        for p in sorted(ctx.run_dir.rglob("*")):
            if not p.is_file():
                continue
            rel = p.relative_to(ctx.run_dir).as_posix()
            if rel in seen or not rel.lower().endswith(_AUDIO_OUTPUT_SUFFIXES):
                continue
            if any(_stage_output_spec_matches(rel, spec) for spec in expandable):
                found.append(rel)
                seen.add(rel)
    return sorted(found)


def operator_visible_staging_path(stage_id: str, rel: str) -> bool:
    """True when a staged relative path is an operator-facing stage output.

    VO pickup owner stages always promote any ``vo_pickup/**`` path so a
    StageInfo subpath regression cannot rmtree-drop rendered takes (exec_13177).
    """
    if is_vo_pickup_rel(rel) and stage_id in VO_PICKUP_OWNER_STAGES:
        return True
    from interview_mux.web.stages import STAGE_BY_ID

    info = STAGE_BY_ID.get(stage_id)
    if not info:
        return True
    specs = tuple(info.artifacts) + tuple(info.editable) + tuple(info.audio_outputs)
    if not specs:
        return True
    return any(_stage_output_spec_matches(rel, spec) for spec in specs)


def list_stage_staging_paths(ctx: RunContext, stage_id: str) -> list[str]:
    root = staging_root(ctx, stage_id)
    if not root.is_dir():
        return []
    paths: list[str] = []
    for p in sorted(root.rglob("*")):
        if p.is_file() and p.name != ".write.lock":
            rel = str(p.relative_to(root)).replace("\\", "/")
            if operator_visible_staging_path(stage_id, rel):
                paths.append(rel)
    return paths


def list_pending_paths(ctx: RunContext, stage_id: str) -> list[str]:
    return list_stage_staging_paths(ctx, stage_id)


def resolve_pending_stage_for_path(ctx: RunContext, stage_id: str, rel: str) -> str:
    return stage_id


def clear_segmentation_classification_on_boundary_restage(ctx: RunContext) -> None:
    """No-op in v2 — segmentation uses standard per-stage staging."""
    return None


def pending_stage_for_path(ctx: RunContext, rel: str) -> str | None:
    if is_operational_path(rel):
        return None
    meta_path = ctx.run_dir / "run_meta.json"
    if meta_path.is_file():
        meta = ctx.read_json("run_meta.json")
        if isinstance(meta, dict):
            pending = meta.get("pending_write_approval") or {}
            if isinstance(pending, dict):
                for sid, info in pending.items():
                    paths = info.get("paths") if isinstance(info, dict) else None
                    if isinstance(paths, list) and rel in paths:
                        return str(sid)
    pending_root = ctx.run_dir / ".pending_writes"
    if not pending_root.is_dir():
        return None
    for stage_dir in sorted(pending_root.iterdir()):
        if not stage_dir.is_dir():
            continue
        candidate = stage_dir.joinpath(*rel.split("/"))
        if candidate.is_file():
            return stage_dir.name
    return None


def has_pending_writes(ctx: RunContext, stage_id: str) -> bool:
    return bool(list_pending_paths(ctx, stage_id))


def stages_with_pending_writes(ctx: RunContext) -> list[str]:
    """Stage ids with staged files on disk (ignores save-blocked / schema state)."""
    stages: list[str] = []
    root = ctx.run_dir / ".pending_writes"
    if root.is_dir():
        stages.extend(
            p.name
            for p in sorted(root.iterdir())
            if p.is_dir() and has_pending_writes(ctx, p.name)
        )
    meta_path = ctx.run_dir / "run_meta.json"
    if meta_path.is_file():
        meta = ctx.read_json("run_meta.json")
        pending = meta.get("pending_write_approval") if isinstance(meta, dict) else {}
        if isinstance(pending, dict):
            for sid in pending:
                if sid not in stages:
                    stages.append(str(sid))
    return stages


def record_pending_approval(ctx: RunContext, stage_id: str) -> None:
    paths = list_pending_paths(ctx, stage_id)
    if not paths:
        return
    created = datetime.now(timezone.utc).isoformat()

    def _patch(meta: dict[str, Any]) -> None:
        pending = dict(meta.get("pending_write_approval") or {})
        pending[stage_id] = {"paths": paths, "created_at": created}
        meta["pending_write_approval"] = pending

    ctx.mutate_run_meta(_patch)


def clear_pending_approval(ctx: RunContext, stage_id: str) -> None:
    if not ctx.final_path("run_meta.json").is_file():
        return

    def _patch(meta: dict[str, Any]) -> None:
        pending = dict(meta.get("pending_write_approval") or {})
        pending.pop(stage_id, None)
        meta["pending_write_approval"] = pending

    ctx.mutate_run_meta(_patch)


def _staging_lock(ctx: RunContext, stage_id: str) -> FileLock:
    root = staging_root(ctx, stage_id)
    root.mkdir(parents=True, exist_ok=True)
    # The staging root's .write.lock is also the file_store lock for files at that
    # root; a second, non-singleton instance on it raised filelock's same-thread
    # "Deadlock: ... held by a different FileLock instance" (ISSUES 145).
    from interview_mux.file_store import write_lock

    return write_lock(root / ".staging.lock")


_LARGE_FLUSH_BYTES = 8 << 20  # 8 MiB


def _owned_staging_path(ctx: RunContext, stage_id: str, rel: str) -> bool:
    """True when ``stage_id`` is permitted to persist ``rel``.

    Ownership, not GUI visibility, decides whether a staged write may land. On
    any error this returns False so an undecidable path keeps the old, stricter
    behaviour rather than committing something unowned.
    """
    try:
        from interview_mux.artifact_ownership import write_permitted

        allowed, _reason = write_permitted(
            ctx, rel, stage_id, role="producer", verb="persist"
        )
        return bool(allowed)
    except Exception:
        return False


def keyed_write_lost_at_flush(ctx: RunContext, rel: str, stage_key: str | None) -> bool:
    """True when a write presented as ``stage_key`` would be staged and then discarded.

    A helper that names an owner (a seat stamp as ``air_contract_sanitize``, a
    tier-D unseat) passes the ownership check as that owner, but the file is
    staged under whichever stage is active. That stage's flush keeps only
    what it shows or owns, so the write was dropped with no log at all
    (ISSUES 140). Mirrors the flush's own test so only writes that would be
    lost are rerouted.
    """
    sid = str(_active_stage.get() or "").strip()
    sk = str(stage_key or "").strip()
    if not sid or not sk or sk == sid:
        return False
    rel = str(rel or "").replace("\\", "/").lstrip("/")
    if is_operational_path(rel) or is_vo_pickup_rel(rel) or stage_sealed_here(ctx, sid):
        return False
    if operator_visible_staging_path(sid, rel):
        return False
    return not _owned_staging_path(ctx, sid, rel)


def _log_undeclared_owned_staging_path(ctx: RunContext, stage_id: str, rel: str) -> None:
    """Note an owned path that no StageInfo declares, so the GUI will not list it."""
    try:
        ctx.log(
            f"staged write {rel} committed but undeclared — {stage_id} owns it and it "
            "is no longer dropped; declare it in web/stages.py StageInfo to surface "
            "it to the operator",
            level="info",
            stage=stage_id,
            action_id="write_staging.flush",
            detail={"event": "undeclared_owned_staging_path", "path": rel},
        )
    except Exception:
        pass


def flush_stage_writes(ctx: RunContext, stage_id: str) -> list[str]:
    with _staging_lock(ctx, stage_id):
        root = staging_root(ctx, stage_id)
        if not root.is_dir():
            clear_pending_approval(ctx, stage_id)
            return []
        flushed: list[str] = []
        from interview_mux.file_store import atomic_copy
        from interview_mux.operator_subprocess import touch_job_message

        for src in sorted(root.rglob("*")):
            if not src.is_file() or src.name.endswith(".lock"):
                continue
            rel = str(src.relative_to(root)).replace("\\", "/")
            if is_vo_pickup_rel(rel) and stage_id not in VO_PICKUP_OWNER_STAGES:
                continue
            if not operator_visible_staging_path(stage_id, rel):
                # operator_visible_staging_path answers "does the GUI list this?",
                # which is the wrong question for persistence. Using it as the
                # commit gate silently deleted every artifact a stage legitimately
                # owns but does not surface: exec_11871 lost publish/episode.json
                # and the ship-time listen_delight_audit, exec_13177 needed a
                # vo_pickup special case above for the same reason, and a full
                # traversal here dropped 26 paths including every
                # understanding/llm_calls/** record and every volley pack, which is
                # exactly the forensic trail needed to debug a failing run.
                #
                # Ownership is the right authority for whether a write may land.
                # Commit anything this stage is permitted to own, and only discard
                # what it is not.
                if not _owned_staging_path(ctx, stage_id, rel):
                    continue
                _log_undeclared_owned_staging_path(ctx, stage_id, rel)
            if _should_preserve_committed_transcript(ctx, rel, src):
                ctx.log(
                    f"Keeping operator-corrected {rel} — skipped stale staged copy from {stage_id}.",
                    level="warning",
                    stage=stage_id,
                    action_id="write_staging.flush",
                    detail={"event": "preserve_corrected_transcript", "path": rel},
                )
                continue
            dest = ctx.run_dir.joinpath(*rel.split("/"))
            # End-B: owner flush must share bind skip with promote_owner / side-effects.
            if _should_skip_stale_vo_pickup_promote(ctx, src=src, dest=dest, rel=rel):
                _discard_skipped_stale_vo_pending(src)
                continue
            size = src.stat().st_size
            if size >= _LARGE_FLUSH_BYTES:
                mb = size / (1 << 20)
                msg = f"Promoting {rel} ({mb:.1f} MiB) to working directory…"
                touch_job_message(ctx, msg)
                ctx.log(
                    msg,
                    level="info",
                    stage=stage_id,
                    action_id="write_staging.flush",
                    origin="api",
                    detail={
                        "journey_kind": "execute",
                        "event": "flush_progress",
                        "path": rel,
                        "bytes": size,
                    },
                )

                def _progress(copied: int, total: int, *, _rel: str = rel) -> None:
                    if total and copied >= total:
                        ctx.log(
                            f"Promoted {_rel}",
                            level="info",
                            stage=stage_id,
                            action_id="write_staging.flush",
                            origin="api",
                        )

                atomic_copy(src, dest, on_progress=_progress if size >= _LARGE_FLUSH_BYTES else None)
            else:
                atomic_copy(src, dest)
            flushed.append(rel)
        shutil.rmtree(root, ignore_errors=True)
    clear_pending_approval(ctx, stage_id)
    from interview_mux.edl_source_contract import heal_committed_edl_source_paths

    heal_committed_edl_source_paths(ctx)
    return flushed


def discard_stage_writes(ctx: RunContext, stage_id: str) -> None:
    with _staging_lock(ctx, stage_id):
        root = staging_root(ctx, stage_id)
        if root.is_dir():
            shutil.rmtree(root, ignore_errors=True)
    clear_pending_approval(ctx, stage_id)


GLUE_PROMOTE_RELS: tuple[str, ...] = (
    "understanding/gap_report.json",
    "understanding/reorder_bridges.json",
    "master/transitions.json",
    "master/transitions/",
)


def discard_non_owner_pending_vo_pickup(ctx: RunContext) -> list[str]:
    """Drop ``.pending_writes/<non-owner>/vo_pickup/`` so EDL cannot clobber a later synth."""
    root = ctx.run_dir / ".pending_writes"
    if not root.is_dir():
        return []
    removed: list[str] = []
    for stage_dir in sorted(root.iterdir()):
        if not stage_dir.is_dir():
            continue
        if stage_dir.name in VO_PICKUP_OWNER_STAGES:
            continue
        pickup = stage_dir / "vo_pickup"
        if not pickup.exists():
            continue
        shutil.rmtree(pickup, ignore_errors=True)
        removed.append(f"{stage_dir.name}/vo_pickup")
    return removed


def promote_owner_vo_pickup(ctx: RunContext) -> list[str]:
    """Flush ``vo_pickup/`` from vo_synthesize pending into the committed tree."""
    return promote_staged_side_effects(ctx, ("vo_pickup/",), stage_id="vo_synthesize")


def discard_staged_rel(ctx: RunContext, stage_id: str, rel: str) -> bool:
    """Delete one staged path without rmtree of the whole stage tree."""
    root = staging_root(ctx, stage_id)
    path = root.joinpath(*str(rel).split("/"))
    if not path.is_file():
        return False
    try:
        path.unlink()
    except OSError:
        return False
    return True


def promote_glue_then_discard_stale_edl(ctx: RunContext) -> dict[str, Any]:
    """Commit glue/VO side-effects from pending stages, then drop only staged EDL.

    Naked-seam heals used to ``discard_stage_writes`` the entire pending tree,
    which deleted the only copy of ``gap_report`` / ``reorder_bridges``.
    Walk every ``.pending_writes/<stage>`` dir — not only operator-visible
    StageInfo outputs — so a staged gap_report still promotes.
    """
    exit_stage_staging()
    promoted: list[str] = []
    discarded_edl: list[str] = []
    root = ctx.run_dir / ".pending_writes"
    sids: list[str] = []
    if root.is_dir():
        sids = [p.name for p in sorted(root.iterdir()) if p.is_dir()]
    for sid in sids:
        flushed = promote_staged_side_effects(ctx, GLUE_PROMOTE_RELS, stage_id=sid)
        promoted.extend(f"{sid}:{rel}" for rel in flushed)
        if discard_staged_rel(ctx, sid, "master/edl.json"):
            discarded_edl.append(sid)
    discarded_vo = discard_non_owner_pending_vo_pickup(ctx)
    return {
        "promoted": promoted,
        "discarded_edl": discarded_edl,
        "discarded_vo_pickup": discarded_vo,
    }


def read_pending_content(ctx: RunContext, stage_id: str, rel: str) -> bytes:
    p = staged_path(ctx, rel, stage_id=stage_id)
    if not p.is_file():
        raise FileNotFoundError(rel)
    return p.read_bytes()


def write_pending_content(
    ctx: RunContext,
    stage_id: str,
    rel: str,
    *,
    data: dict[str, Any] | None = None,
    text: str | None = None,
    raw: bytes | None = None,
) -> Path:
    p = staged_path(ctx, rel, stage_id=stage_id)
    p.parent.mkdir(parents=True, exist_ok=True)
    import json as _json

    from interview_mux.edl_source_contract import is_edl_rel, prepare_edl_payload_for_disk

    if data is not None:
        data = prepare_edl_payload_for_disk(ctx, rel, data)
        fs_write_json(p, data)
    elif text is not None:
        if is_edl_rel(rel):
            try:
                parsed = _json.loads(text)
            except Exception:
                parsed = None
            if isinstance(parsed, dict):
                fs_write_json(p, prepare_edl_payload_for_disk(ctx, rel, parsed))
                record_pending_approval(ctx, stage_id)
                return p
        fs_write_text(p, text)
    elif raw is not None:
        if is_edl_rel(rel):
            try:
                parsed = _json.loads(raw.decode("utf-8"))
            except Exception:
                parsed = None
            if isinstance(parsed, dict):
                fs_write_json(p, prepare_edl_payload_for_disk(ctx, rel, parsed))
                record_pending_approval(ctx, stage_id)
                return p
        p.write_bytes(raw)
    else:
        raise ValueError("Provide data, text, or raw")
    record_pending_approval(ctx, stage_id)
    return p


def all_pending_stages(ctx: RunContext, *, savable_only: bool = True) -> list[str]:
    stages: list[str] = []
    root = ctx.run_dir / ".pending_writes"
    if root.is_dir():
        stages.extend(
            p.name
            for p in sorted(root.iterdir())
            if p.is_dir() and list_pending_paths(ctx, p.name)
        )
    meta_path = ctx.run_dir / "run_meta.json"
    if meta_path.is_file():
        meta = ctx.read_json("run_meta.json")
        pending = meta.get("pending_write_approval") if isinstance(meta, dict) else {}
        if isinstance(pending, dict):
            for sid, info in pending.items():
                if sid in stages:
                    continue
                paths = info.get("paths") if isinstance(info, dict) else None
                if isinstance(paths, list) and paths:
                    stages.append(str(sid))
    if savable_only:
        return [sid for sid in stages if write_approval_save_blocked_reason(ctx, sid) is None]
    return stages


def read_pending_json(ctx: RunContext, stage_id: str, rel: str) -> Any:
    return fs_read_json(staged_path(ctx, rel, stage_id=stage_id))


def read_pending_text(ctx: RunContext, stage_id: str, rel: str) -> str:
    return fs_read_text(staged_path(ctx, rel, stage_id=stage_id))


class WriteApprovalBlockedError(Exception):
    """Staged save blocked — stage failed a quality gate."""

    def __init__(self, stage_id: str, message: str) -> None:
        self.stage_id = stage_id
        super().__init__(message)


class WriteApprovalPending(Exception):
    """Pipeline paused until operator approves staged writes (legacy only)."""

    def __init__(self, stage_id: str, paths: list[str]) -> None:
        self.stage_id = stage_id
        self.paths = paths
        super().__init__(
            f"Stage '{stage_id}' outputs await review before saving ({len(paths)} file(s))."
        )


def read_gui_job(ctx: RunContext) -> dict[str, Any] | None:
    if not ctx.artifact_exists("gui_job.json"):
        return None
    try:
        job = ctx.read_json("gui_job.json")
    except Exception:
        return None
    return job if isinstance(job, dict) else None


def set_llm_gate(ctx: RunContext, stage_id: str, *, message: str) -> None:
    job = read_gui_job(ctx) or {}
    if not isinstance(job, dict):
        job = {}
    job["status"] = "gate"
    job["stage"] = stage_id
    job["message"] = message
    ctx.write_json("gui_job.json", job, skip_handoff=True)


def gate_blocked_stage(ctx: RunContext) -> str | None:
    job = read_gui_job(ctx)
    if not job or str(job.get("status")) != "gate":
        return None
    stage_id = str(job.get("stage") or "")
    if not stage_id or ctx.is_done(stage_id):
        return None
    return stage_id


def is_llm_gate_blocked(ctx: RunContext, stage_id: str) -> bool:
    return gate_blocked_stage(ctx) == stage_id


def is_itr_clarification_blocked(ctx: RunContext, stage_id: str) -> bool:
    return False


def is_stage_gate_blocked(ctx: RunContext, stage_id: str) -> bool:
    return is_llm_gate_blocked(ctx, stage_id)


def write_approval_save_blocked_reason(ctx: RunContext, stage_id: str) -> str | None:
    if not write_approval_enabled() or not has_pending_writes(ctx, stage_id):
        return None
    if is_stage_gate_blocked(ctx, stage_id):
        job = read_gui_job(ctx) or {}
        return str(
            job.get("message")
            or (
                f"Stage {stage_id} failed the LLM quality gate — "
                "re-run or discard staged outputs instead of saving."
            )
        )
    from interview_mux.stage_completion import staged_artifacts_acceptable

    ok, reason = staged_artifacts_acceptable(ctx, stage_id)
    if not ok:
        return reason
    return None


def write_approval_allowed(ctx: RunContext, stage_id: str) -> bool:
    if not write_approval_enabled():
        return False
    if write_approval_save_blocked_reason(ctx, stage_id):
        return False
    return has_pending_writes(ctx, stage_id)


def assert_write_approval_allowed(ctx: RunContext, stage_id: str) -> None:
    if is_stage_gate_blocked(ctx, stage_id):
        job = read_gui_job(ctx) or {}
        msg = str(
            job.get("message")
            or (
                f"Stage {stage_id} failed the LLM quality gate — "
                "re-run or discard staged outputs instead of saving."
            )
        )
        raise WriteApprovalBlockedError(stage_id, msg)
    from interview_mux.stage_completion import staged_artifacts_acceptable

    ok, reason = staged_artifacts_acceptable(ctx, stage_id)
    if not ok:
        raise WriteApprovalBlockedError(stage_id, reason)
    if not has_pending_writes(ctx, stage_id):
        raise FileNotFoundError(f"No pending writes for stage: {stage_id}")


def _commit_stage_writes(ctx: RunContext, stage_id: str) -> list[str]:
    """Promote staged outputs and mark the stage done (v2 auto-commit)."""
    if not has_pending_writes(ctx, stage_id):
        return []
    return approve_stage_writes(ctx, stage_id)


def after_stage_write_check(ctx: RunContext, stage_id: str) -> None:
    if not has_pending_writes(ctx, stage_id):
        return
    if write_approval_enabled():
        record_pending_approval(ctx, stage_id)
        raise WriteApprovalPending(stage_id, list_pending_paths(ctx, stage_id))
    _commit_stage_writes(ctx, stage_id)


def _report_contract_conformance(ctx: RunContext, stage_id: str) -> None:
    """Report-only conformance warning + observed-map flush (plan §4.1)."""
    try:
        from interview_mux.contract_conformance import warn_on_mismatch

        warn_on_mismatch(ctx, stage_id)
    except Exception:
        return


#: Stages whose orphaned pending writes are promoted, not discarded: they hold
#: expensive WAVs (exec_10066).
_ORPHAN_PROMOTE_STAGES: frozenset[str] = frozenset({"vo_synthesize", "edl"})


def promote_lost_staged_writes(ctx: RunContext) -> list[str]:
    """Land staged files of done stages that the committed tree never got.

    A stage sealed through the forced heal path leaves its overlay for the
    orphan promote on its next entry; a run that completes never re-enters
    it, so a file staged there with no committed copy, or a copy newer than
    the committed one, is simply lost (exec_099: the EDL's clone adjacency
    report, ISSUES 118). Called once at run end. Only files the stage owns
    are promoted, and only when the committed copy is missing or older;
    everything else in the overlay is a leftover and is left alone. Returns
    the promoted paths.
    """
    from interview_mux.file_store import atomic_copy

    pending = ctx.run_dir / ".pending_writes"
    if not pending.is_dir():
        return []
    promoted: list[str] = []
    for stage_dir in sorted(pending.iterdir()):
        if not stage_dir.is_dir():
            continue
        stage_id = stage_dir.name
        try:
            if not ctx.is_done(stage_id):
                continue
        except Exception:
            continue
        for src in sorted(stage_dir.rglob("*")):
            if not src.is_file() or src.name.endswith(".lock"):
                continue
            rel = str(src.relative_to(stage_dir)).replace("\\", "/")
            if is_vo_pickup_rel(rel) and stage_id not in VO_PICKUP_OWNER_STAGES:
                continue
            try:
                if not _owned_staging_path(ctx, stage_id, rel):
                    continue
            except Exception:
                continue
            dest = ctx.run_dir.joinpath(*rel.split("/"))
            try:
                if dest.is_file() and dest.stat().st_mtime >= src.stat().st_mtime:
                    continue
                atomic_copy(src, dest)
                src.unlink(missing_ok=True)
            except OSError:
                continue
            promoted.append(f"{stage_id}/{rel}")
    if promoted:
        try:
            ctx.log(
                f"run end: promoted {len(promoted)} staged write(s) the committed tree never got: "
                + ", ".join(promoted[:6]),
                level="info",
                stage=None,
                action_id="write_staging.promote_lost",
                detail={"promoted": promoted[:40]},
            )
        except Exception:
            pass
    return promoted


def discard_stale_staging_before_entry(ctx: RunContext, stage_id: str) -> list[str]:
    """Drop staged writes a previous attempt of ``stage_id`` left behind (ISSUES 102).

    A fresh attempt starts from a clean overlay. Files staged by an attempt
    whose commit the barrier refused are newer than the commit, and the
    completeness check then reads the upstream producer as incomplete on the
    very next entry (exec_065: gap_framing_compose refused, then blocked on
    its own stale gap_evaluations copy). Returns the discarded paths. Stages
    in ``_ORPHAN_PROMOTE_STAGES`` are left to their own recovery, and nothing
    is touched while the operator approval flow owns pending writes.
    """
    if stage_id in _ORPHAN_PROMOTE_STAGES or write_approval_enabled():
        return []
    try:
        if ctx.is_done(stage_id):
            return []
        # Every file in the overlay, not only the operator-visible ones: the
        # copy that blocked exec_065 (gap_evaluations under gap_framing_compose)
        # is exactly the kind the visible listing leaves out.
        root = staging_root(ctx, stage_id)
        if not root.is_dir():
            return []
        stale = sorted(
            str(p.relative_to(root)).replace("\\", "/")
            for p in root.rglob("*")
            if p.is_file() and p.name != ".write.lock"
        )
        if not stale:
            return []
        discard_stage_writes(ctx, stage_id)
    except Exception as exc:  # noqa: BLE001 - hygiene must not block the stage
        try:
            ctx.log(
                f"{stage_id}: stale staging discard failed open: {exc}",
                level="warning",
                stage=stage_id,
            )
        except Exception:
            pass
        return []
    try:
        ctx.log(
            f"{stage_id}: discarded {len(stale)} stale staged write(s) from a "
            "previous refused attempt before re-entering",
            level="warning",
            stage=stage_id,
            detail={"discarded": stale[:24]},
        )
    except Exception:
        pass
    return stale


def run_wrapped_stage(ctx: RunContext, stage_id: str, fn: Any) -> None:
    """Execute a stage function with write staging and v2 auto-commit."""
    from interview_mux.operator_trace import active_run_context, log_step

    ctx_token = active_run_context.set(ctx)
    stage_action_id = f"pipeline.stage.{stage_id}"
    try:
        log_step(
            f"Preparing stage: {stage_id}",
            ctx=ctx,
            stage=stage_id,
            detail={"action_id": stage_action_id, "event": "stage_start"},
        )
        from interview_mux.stage_input_checks import require_stage_inputs

        require_stage_inputs(ctx, stage_id)
        from interview_mux.llm_flow_hardening import maybe_require_upstream_llm_progress

        maybe_require_upstream_llm_progress(ctx, stage_id)
        # Promote crash-orphaned pending before re-entering staging so G1/resolve
        # see committed WAVs (exec_10066: pending transitions/gap VO, seed-order thrash).
        if stage_id in {"vo_synthesize", "edl"} and not write_approval_enabled():
            try:
                if stage_id == "edl":
                    discard_non_owner_pending_vo_pickup(ctx)
                if has_pending_writes(ctx, stage_id):
                    flushed = _commit_stage_writes(ctx, stage_id)
                    if flushed:
                        ctx.log(
                            f"{stage_id}: recovered {len(flushed)} orphaned pending write(s)",
                            level="warning",
                            stage=stage_id,
                            detail={"flushed": flushed[:24]},
                        )
                if stage_id == "vo_synthesize":
                    # Clear false layup-invalidation so stability does not rewind to
                    # nugget_layup_compose after a VO-only hole (exec_10066).
                    if ctx.artifact_exists("mastering/vo_synthesize.json"):
                        try:
                            doc = ctx.read_json("mastering/vo_synthesize.json")
                            meta = dict((doc or {}).get("_meta") or {}) if isinstance(doc, dict) else {}
                            reason = str(meta.get("stale_reason") or "")
                            if meta.get("stale") and "nugget_layup_compose" in reason:
                                meta.pop("stale", None)
                                meta.pop("stale_reason", None)
                                meta.pop("orphan_incomplete", None)
                                meta.pop("orphan_incomplete_reason", None)
                                meta.pop("orphan_incomplete_at", None)
                                assert isinstance(doc, dict)
                                doc["_meta"] = meta
                                ctx.write_json(
                                    "mastering/vo_synthesize.json",
                                    doc,
                                    skip_handoff=True,
                                )
                        except Exception:
                            pass
            except Exception as exc:
                ctx.log(
                    f"{stage_id}: pending recovery failed open: {exc}",
                    level="warning",
                    stage=stage_id,
                )
        # A fresh attempt starts from a clean overlay. Staged files left by an
        # attempt whose commit the barrier refused are newer than the commit
        # and make upstream producers read as incomplete on the very next
        # entry (exec_065: gap_framing_compose refused, then blocked on its own
        # stale gap_evaluations copy; ISSUES 102). vo_synthesize and edl
        # recovered their orphans above because those hold expensive WAVs.
        discard_stale_staging_before_entry(ctx, stage_id)
        preflight_stage_enter(ctx, stage_id)
        enter_stage_staging(stage_id)
        stage_exc: BaseException | None = None
        try:
            fn()
            ctx.log(
                f"Stage finished: {stage_id}",
                level="success",
                stage=stage_id,
                action_id=stage_action_id,
                detail={"journey_kind": "execute", "event": "stage_finish"},
            )
        except WriteApprovalPending:
            raise
        except SystemExit as exc:
            # A stage that refuses its own completion raises SystemExit, which
            # the Exception arm never saw: the walk retried to the invoke cap
            # with no reason on record (exec_025 edl; ISSUES 178).
            try:
                ctx.log(
                    f"Stage {stage_id} refused completion: {exc}",
                    level="warning",
                    stage=stage_id,
                    detail={"event": "stage_refused", "reason": str(exc)[:400]},
                )
            except Exception:
                pass
            raise
        except Exception as exc:
            from interview_mux.operator_trace import log_stage_error

            log_stage_error(stage_id, exc, ctx=ctx)
            stage_exc = exc
        finally:
            exit_stage_staging()
            _report_contract_conformance(ctx, stage_id)
        # Flush staged outputs even when the stage loud-fails after writing them
        # (exec_13167: delight abort left master.wav pending, PMQ missing, hollow
        # mark_done). mark_done still enforces completeness after flush.
        if stage_exc is None:
            after_stage_write_check(ctx, stage_id)
        elif has_pending_writes(ctx, stage_id):
            try:
                after_stage_write_check(ctx, stage_id)
            except Exception:
                pass
            raise stage_exc
        else:
            raise stage_exc
    finally:
        active_run_context.reset(ctx_token)


def check_write_approval_before_execute(
    ctx: RunContext,
    stage_id: str | None = None,
) -> WriteApprovalPending | None:
    """Block execute when pending writes require an operator Save pause.

    v2 auto-commit disables this entirely. Under ``defer_write_approval_until=phase_end``,
    prior stages may keep staged files without pausing every subsequent execute.
    """
    from interview_mux.first_try import write_approval_deferred

    if not write_approval_enabled():
        return None

    if write_approval_deferred():
        if stage_id and has_pending_writes(ctx, stage_id):
            return WriteApprovalPending(stage_id, list_pending_paths(ctx, stage_id))
        return None

    stages = stages_with_pending_writes(ctx)
    if not stages:
        return None
    sid = stages[0]
    paths = list_pending_paths(ctx, sid)
    if paths:
        return WriteApprovalPending(sid, paths)
    return None


def approve_batch_stage_writes(ctx: RunContext, stages: list[str] | None = None) -> dict[str, Any]:
    pending = all_pending_stages(ctx)
    if stages is None:
        stages = list(pending)
    else:
        stages = [s for s in stages if s in pending or has_pending_writes(ctx, s)]

    results: dict[str, Any] = {"approved": {}, "errors": {}, "phases": {}}
    for sid in stages:
        try:
            assert_write_approval_allowed(ctx, sid)
            flushed = approve_stage_writes(ctx, sid)
            results["approved"][sid] = flushed
        except Exception as exc:
            results["errors"][sid] = str(exc)
    ctx.log(
        f"Batch write approval: {len(results['approved'])} stage(s) saved"
        + (f", {len(results['errors'])} error(s)" if results["errors"] else ""),
        level="success" if not results["errors"] else "warning",
        stage="write_staging",
        action_id="write_staging.batch_approve",
        detail={
            "event": "write_approval_batch",
            "approved_stages": list(results["approved"].keys()),
            "error_stages": list(results["errors"].keys()),
        },
    )
    return results


def segmentation_pair_approve_needed(ctx: RunContext, stage_id: str) -> bool:
    return False


def approve_segmentation_pair_writes(ctx: RunContext) -> list[str]:
    return approve_stage_writes(ctx, "segment_classification")


def approve_stage_writes(ctx: RunContext, stage_id: str) -> list[str]:
    from interview_mux.operator_action_trace import begin_action, end_action

    trace_id = begin_action(
        "write_staging.approve",
        run_dir=ctx.run_dir,
        stage=stage_id,
        origin="api",
        summary=f"Approve staged writes for {stage_id}",
        function="write_staging.approve_stage_writes",
    )
    try:
        if stage_id in ("segment_classification", "content_brief_reanchor", "content_context", "boundary_detection"):
            from interview_mux.artifact_repairs import sync_content_brief_topic_segment_ids

            sync_content_brief_topic_segment_ids(ctx, overlay_stage=stage_id)
        # Commit barrier: validate staged overlay BEFORE flush so mark_done cannot
        # race ahead of promoted files.
        from interview_mux.stage_resilience import (
            after_flush_resilience,
            record_resilience_event,
            validate_staged_before_flush,
        )

        pre = validate_staged_before_flush(ctx, stage_id)
        if pre.action == "halt" and pre.acceptance_ok is False:
            record_resilience_event(
                ctx,
                stage_id,
                event="commit_barrier_halt",
                action="halt",
                reasons=pre.reasons,
            )
            # Refusal feedback (ISSUES 124): the stage's next run reads these
            # reasons once and tells the model exactly what was refused.
            try:
                from interview_mux.fallback_backstop import note_refusal

                note_refusal(ctx, stage_id, list(pre.reasons or []))
            except Exception:
                pass
            raise WriteApprovalBlockedError(
                stage_id,
                "Pre-flush commit barrier failed: " + "; ".join(pre.reasons[:4] or ["unacceptable"]),
            )
        flushed = flush_stage_writes(ctx, stage_id)
        from interview_mux.artifact_lifecycle import apply_fingerprints_on_flush, post_commit_validate

        apply_fingerprints_on_flush(ctx, stage_id, flushed)
        # Only a flush that wrote the boundaries can leave the manifest on old
        # ids. Aligning after every flush also rewrote fused/trimmed manifest
        # spans back to the boundary rows.
        if "segments/boundaries.json" in {str(p).replace("\\", "/") for p in flushed}:
            try:
                from interview_mux.artifact_completeness import (
                    align_manifest_ids_to_boundaries,
                    drop_retired_segment_refs,
                )

                align_manifest_ids_to_boundaries(ctx)
                drop_retired_segment_refs(ctx)
            except Exception:
                pass
        post_errors = post_commit_validate(ctx, stage_id)
        if post_errors:
            raise ValueError(
                "Post-commit validation failed: " + "; ".join(post_errors[:4])
            )
        if "segments/manifest.json" in flushed and ctx.artifact_exists("segments/manifest.json"):
            from interview_mux.artifact_completeness import hydrate_manifest_from_boundaries

            manifest = ctx.read_json("segments/manifest.json")
            hydrated = hydrate_manifest_from_boundaries(ctx, manifest)
            if hydrated != manifest:
                # Ensure hydrate stubs pass schema before write_json validation.
                from interview_mux.artifact_repairs import repair_manifest_segments

                repaired, _notes = repair_manifest_segments(ctx, hydrated)
                # Hydrate is segment_classification ownership — never attribute
                # the rewrite to the flushing consumer stage (S2 / exec_13198).
                # The flush is over, so this goes to the committed tree: the
                # staging root is still open here, and a staged copy newer than
                # the commit is exactly what the completeness check below
                # refuses ("newer uncommitted pending", ISSUES 94).
                write_committed_json(
                    ctx,
                    "segments/manifest.json",
                    repaired,
                    stage_key="segment_classification",
                )
        from interview_mux.stage_completion import (
            assert_stage_artifacts_complete,
            vo_synthesize_should_defer_done,
        )

        deferred = vo_synthesize_should_defer_done(ctx, stage_id)
        if deferred:
            # S5: bytes may already be flushed — still refuse mark_done and pin
            # honestly (no fail-open continue-to-mix from this flush path).
            # Mix last-chance remains an explicit later-stage policy.
            ctx.log(
                f"vo_synthesize incomplete after flush — refuse mark_done: {deferred}",
                level="warning",
                stage=stage_id,
            )
            post = after_flush_resilience(ctx, stage_id, flushed)
            if post.action == "halt" and post.acceptance_ok is False:
                raise ValueError(
                    "Post-flush resilience failed: "
                    + "; ".join(post.reasons[:4] or ["unacceptable"])
                )
            record_resilience_event(
                ctx,
                stage_id,
                event="stage_committed_incomplete",
                action="halt",
                reasons=[deferred],
                detail={"flushed": flushed[:40], "fail_open": False, "honest_pin": True},
            )
            end_action(
                trace_id,
                run_dir=ctx.run_dir,
                status="error",
                detail={
                    "flushed": flushed,
                    "stage_id": stage_id,
                    "deferred_done": deferred,
                },
            )
            raise RuntimeError(
                f"vo_synthesize incomplete — resume vo_synthesize: {deferred}"
            )
        assert_stage_artifacts_complete(ctx, stage_id)
        post = after_flush_resilience(ctx, stage_id, flushed)
        # Heal Success V3: retry/halt with acceptance fail must not mark_done.
        try:
            from interview_mux.heal_success import may_mark_after_flush

            may_mark, mark_reason = may_mark_after_flush(
                ctx,
                stage_id,
                resilience_action=str(post.action or ""),
                acceptance_ok=post.acceptance_ok,
            )
        except Exception:
            may_mark, mark_reason = True, "ok"
        if not may_mark:
            raise ValueError(
                "Post-flush heal success refused: "
                + mark_reason
                + "; "
                + "; ".join(post.reasons[:4] or ["unacceptable"])
            )
        if post.action == "halt" and post.acceptance_ok is False:
            raise ValueError(
                "Post-flush resilience failed: " + "; ".join(post.reasons[:4] or ["unacceptable"])
            )
        ctx.mark_done(stage_id)
        record_resilience_event(
            ctx,
            stage_id,
            event="stage_committed",
            action="pass",
            reasons=[],
            detail={"flushed": flushed[:40]},
        )
        end_action(
            trace_id,
            run_dir=ctx.run_dir,
            status="ok",
            detail={"flushed": flushed, "stage_id": stage_id},
        )
        return flushed
    except Exception:
        end_action(trace_id, run_dir=ctx.run_dir, status="error")
        raise

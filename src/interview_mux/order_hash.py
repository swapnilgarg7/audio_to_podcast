"""Stable content hash + revisioned order_lock for air-order authority.

Selection is the sole authority. EDL/layup/ledger copy the lock; they must never
rewrite selection order via sync_selection_order_to_edl.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

ORDER_LOCK_AUTHORITY = "master/selection.json"


def ordered_segment_ids_hash(ordered: list[Any] | None) -> str:
    """Return a short sha256 of the ordered segment id list."""
    ids = [str(s) for s in (ordered or []) if s]
    payload = json.dumps(ids, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()[:16]


def stamp_order_hash(doc: dict[str, Any], *, ordered_key: str = "ordered_segment_ids") -> dict[str, Any]:
    """Return a copy of doc with ``order_content_hash`` (and lock hash alias) set."""
    out = dict(doc)
    content_hash = ordered_segment_ids_hash(out.get(ordered_key) or [])
    out["order_content_hash"] = content_hash
    lock = out.get("order_lock")
    if isinstance(lock, dict):
        lock = dict(lock)
        lock["order_content_hash"] = content_hash
        lock["ordered_segment_ids"] = [str(s) for s in (out.get(ordered_key) or []) if s]
        out["order_lock"] = lock
    return out


def _lock_from_doc(
    doc: dict[str, Any],
    *,
    ordered_key: str = "ordered_segment_ids",
    created_by: str | None = None,
    revision: int = 1,
    supersedes: str | None = None,
) -> dict[str, Any]:
    ids = [str(s) for s in (doc.get(ordered_key) or []) if s]
    content_hash = ordered_segment_ids_hash(ids)
    return {
        "version": 1,
        "revision": max(1, int(revision)),
        "authority": ORDER_LOCK_AUTHORITY,
        "ordered_segment_ids": ids,
        "order_content_hash": content_hash,
        "air_bounds_hash": str(doc.get("air_bounds_hash") or "") or None,
        "selection_content_hash": str(doc.get("selection_content_hash") or "") or None,
        "nle_revision": str(doc.get("nle_revision") or "") or None,
        "segment_manifest_hash": str(doc.get("segment_manifest_hash") or "") or None,
        "created_by": created_by or str(doc.get("created_by") or "selection"),
        "supersedes": supersedes,
    }


def get_order_lock(doc: dict[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(doc, dict):
        return None
    lock = doc.get("order_lock")
    return dict(lock) if isinstance(lock, dict) else None


def bump_order_lock(
    doc: dict[str, Any],
    *,
    source: str,
    ordered_key: str = "ordered_segment_ids",
) -> dict[str, Any]:
    """Increment revision when ordered ids change; always refresh hashes.

    Selection-authority writers must call this (not bare stamp) before persist.
    """
    out = dict(doc)
    ids = [str(s) for s in (out.get(ordered_key) or []) if s]
    content_hash = ordered_segment_ids_hash(ids)
    prev = get_order_lock(out)
    prev_rev = int(prev.get("revision") or 0) if prev else 0
    prev_hash = str(prev.get("order_content_hash") or "") if prev else ""
    prev_ids = [str(s) for s in (prev.get("ordered_segment_ids") or []) if s] if prev else []
    supersedes = None
    if prev and (prev_ids != ids or prev_hash != content_hash):
        revision = prev_rev + 1 if prev_rev >= 1 else 1
        supersedes = f"rev:{prev_rev}:{prev_hash}" if prev_rev else None
    elif prev:
        revision = max(1, prev_rev)
        supersedes = prev.get("supersedes")
    else:
        revision = 1
    lock = _lock_from_doc(
        out,
        ordered_key=ordered_key,
        created_by=source,
        revision=revision,
        supersedes=supersedes,
    )
    # Drop null optional fields for cleaner JSON.
    lock = {k: v for k, v in lock.items() if v is not None}
    out["order_lock"] = lock
    out["order_content_hash"] = content_hash
    return out


def copy_order_lock(src: dict[str, Any], dst: dict[str, Any]) -> dict[str, Any]:
    """Copy selection lock onto a derivative doc; never invent a new revision."""
    out = dict(dst)
    lock = get_order_lock(src)
    if lock is None:
        # Legacy: synthesize a non-authoritative lock from ids for propagation.
        lock = _lock_from_doc(src if isinstance(src, dict) else out, created_by="legacy_copy")
        lock = {k: v for k, v in lock.items() if v is not None}
    else:
        lock = dict(lock)
    out["order_lock"] = lock
    out["order_content_hash"] = str(lock.get("order_content_hash") or ordered_segment_ids_hash(
        out.get("ordered_segment_ids") or lock.get("ordered_segment_ids") or []
    ))
    return out


def order_locks_match(a: dict[str, Any] | None, b: dict[str, Any] | None) -> bool:
    """True when revision + content hash agree (or lists equal for legacy)."""
    if not isinstance(a, dict) or not isinstance(b, dict):
        return False
    la, lb = get_order_lock(a), get_order_lock(b)
    if la and lb:
        if int(la.get("revision") or 0) != int(lb.get("revision") or 0):
            return False
        if str(la.get("order_content_hash") or "") != str(lb.get("order_content_hash") or ""):
            return False
        return True
    return order_hashes_match(a, b)


def order_hashes_match(
    selection: dict[str, Any] | None,
    edl: dict[str, Any] | None,
) -> bool:
    """True when selection and EDL agree on air order (hash or list equality)."""
    if not isinstance(selection, dict) or not isinstance(edl, dict):
        return False
    # Ids the EDL omitted as unplayable stay in the selection while the EDL
    # list drops them. Without this filter verify_commitment never reached
    # "committed" and master_finalize was refused on every pass.
    sel_ids = seatable_selection_ids(selection, edl)
    edl_ids = seatable_selection_ids(edl, edl)
    # Air-order list equality is authoritative. Recompute hashes so a stale
    # order_content_hash field cannot false-fail commitment checks.
    if sel_ids != edl_ids:
        return False
    return ordered_segment_ids_hash(sel_ids) == ordered_segment_ids_hash(edl_ids)


def edl_speech_clip_ids(edl: dict[str, Any] | None) -> list[str]:
    """Ordered speech segment ids from EDL clips (mix concatenates these, not the id list)."""
    if not isinstance(edl, dict):
        return []
    out: list[str] = []
    for clip in edl.get("clips") or []:
        if not isinstance(clip, dict):
            continue
        if str(clip.get("type") or "") != "speech":
            continue
        sid = str(clip.get("segment_id") or "").strip()
        if sid:
            out.append(sid)
    return out


def last_speech_clip_id(edl: dict[str, Any] | None) -> str:
    ids = edl_speech_clip_ids(edl)
    return ids[-1] if ids else ""


def _is_subsequence(small: list[str], big: list[str]) -> bool:
    it = iter(big)
    return all(item in it for item in small)


def seatable_selection_ids(
    selection: dict[str, Any] | None,
    edl: dict[str, Any] | None,
    *,
    use_lock: bool = False,
) -> list[str]:
    """Selection ids the EDL must seat: the lock or ordered ids, minus EDL omissions.

    Every selection-versus-clips comparison goes through here. A clip the EDL
    omitted as unplayable stays in the selection, so a comparison that skips
    this filter never sees the EDL as seated and re-dispatches edl and mix.
    """
    sel = selection if isinstance(selection, dict) else {}
    omitted = {
        str(s)
        for s in ((edl or {}).get("omitted_unplayable_segment_ids") or [])
        if s
    } if isinstance(edl, dict) else set()
    ids: list[Any] = []
    if use_lock:
        ids = list((get_order_lock(sel) or {}).get("ordered_segment_ids") or [])
    if not ids:
        ids = list(sel.get("ordered_segment_ids") or [])
    return [str(s) for s in ids if s and str(s) not in omitted]


def order_drift_heal_action(
    selection: dict[str, Any] | None,
    edl: dict[str, Any] | None,
) -> str:
    """How to reconcile selection vs seated EDL.

    ``exclude_unseated`` — clips are a proper subsequence of selection (extras never
    seated); drop them from the lock in the same generation. ``rebuild`` — different
    order or clips not a subset. ``stamp`` — clips already match selection ids.
    ``ok`` — ids, lock, and clips agree.
    """
    if not isinstance(selection, dict) or not isinstance(edl, dict):
        return "rebuild"
    sel_ids = seatable_selection_ids(selection, edl)
    clip_ids = edl_speech_clip_ids(edl)
    if clip_ids and clip_ids != sel_ids:
        clip_set = set(clip_ids)
        sel_set = set(sel_ids)
        if clip_ids and sel_ids and clip_set < sel_set and _is_subsequence(clip_ids, sel_ids):
            return "exclude_unseated"
        return "rebuild"
    if not sel_ids:
        return "rebuild"
    if order_hashes_match(selection, edl) and (not clip_ids or clip_ids == sel_ids):
        return "ok"
    return "stamp"


def copy_order_lock_if_clips_match(
    selection: dict[str, Any],
    edl: dict[str, Any],
) -> dict[str, Any]:
    """Copy selection lock onto EDL only when speech clips already equal selection ids."""
    sel_ids = seatable_selection_ids(selection, edl)
    clip_ids = edl_speech_clip_ids(edl)
    if clip_ids and clip_ids != sel_ids:
        raise ValueError(
            "speech clip order diverges from selection; rebuild EDL (run_edl), "
            "do not stamp ordered_segment_ids. "
            f"clips_tail={clip_ids[-8:]} selection_tail={sel_ids[-8:]}"
        )
    out = dict(edl)
    out["ordered_segment_ids"] = list(sel_ids)
    return copy_order_lock(selection, stamp_order_hash(out))


def assert_selection_leads_edl(
    selection: dict[str, Any],
    edl: dict[str, Any],
) -> None:
    """Fail closed when EDL diverges from selection — never rewrite selection from EDL."""
    sel_ids = seatable_selection_ids(selection, edl)
    clip_ids = edl_speech_clip_ids(edl)
    if clip_ids and clip_ids != sel_ids:
        raise ValueError(
            "speech clip order diverges from selection; "
            "rebuild EDL from selection (run_edl) — ID-only stamp is banned. "
            f"clips_n={len(clip_ids)} selection_n={len(sel_ids)}"
        )
    if order_locks_match(selection, edl) or order_hashes_match(selection, edl):
        return
    edl_ids = [str(s) for s in (edl.get("ordered_segment_ids") or []) if s]
    raise ValueError(
        "EDL air order diverges from selection order_lock; "
        "rebuild EDL from selection (run_edl) — selection-from-EDL sync is banned. "
        f"selection_n={len(sel_ids)} edl_n={len(edl_ids)} "
        f"sel_rev={(get_order_lock(selection) or {}).get('revision')} "
        f"edl_rev={(get_order_lock(edl) or {}).get('revision')}"
    )


def sync_selection_order_to_edl(selection: dict[str, Any], edl: dict[str, Any]) -> dict[str, Any]:
    """Banned: selection must never be rewritten from EDL.

    Kept as a named symbol so call sites fail loudly instead of silently drifting.
    """
    raise RuntimeError(
        "sync_selection_order_to_edl is banned under order_lock policy; "
        "commit a new selection revision (bump_order_lock) and rebuild EDL via run_edl"
    )

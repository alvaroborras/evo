"""Session registry — opt-in via auto-register on first `evo X` call.

A session that never invokes any evo command never appears in the
registry. `evo direct` enumerates registered sessions to fan out
markers; unregistered sessions are invisible.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import time
from pathlib import Path
from typing import Iterable

from evo.core import atomic_write_json

from .paths import (
    ensure_dirs,
    inject_root,
    offset_file,
    optimize_mode_dir,
    optimize_mode_flag_file,
    session_file,
    sessions_dir,
)

REGISTRY_SCHEMA_VERSION = 1
# 30 days. The freshness signal is no longer used as an engagement
# heuristic — `has_evo_engaged` is. This just prevents on-disk leakage
# of records for sessions that genuinely died.
STALE_AFTER_SECONDS = 30 * 24 * 60 * 60

# Order matters: first hit wins. Each entry is (host, env_var_name).
# Codex exposes the session as CODEX_THREAD_ID (verified empirically on
# codex-cli 0.130 — it shows "session id: <uuid>" at startup and exports
# the same uuid via CODEX_THREAD_ID, not CODEX_SESSION_ID).
HOST_SESSION_ENV_VARS = (
    ("claude-code", "CLAUDE_CODE_SESSION_ID"),
    ("codex", "CODEX_THREAD_ID"),
    ("hermes", "HERMES_SESSION_ID"),
    ("opencode", "OPENCODE_SESSION_ID"),
)


def detect_session() -> tuple[str, str] | None:
    """Detect host + session_id from environment. Returns None if no
    host's session env var is set — meaning we're not running inside
    an evo-aware agent session."""
    for host, env_var in HOST_SESSION_ENV_VARS:
        sid = os.environ.get(env_var)
        if sid:
            return host, sid
    return None


def _now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def register_session(
    root: Path,
    session_id: str,
    host: str,
    *,
    exp_id: str | None = None,
    parent_session_id: str | None = None,
) -> None:
    """Idempotent: write `<inject>/sessions/<sid>.json` if absent;
    update `last_seen_at` if present.

    Merges non-null `exp_id` / `parent_session_id` into an existing
    record when those fields are currently empty — covers the case
    where the Rust binary registered at SessionStart with `exp_id=null`
    and a later Python `auto_register_from_env` call sees `EVO_EXP_ID`
    set (subagent dispatched into an experiment after registration).
    Never clobbers a non-null existing value.

    Caller is responsible for only calling this when there's an active
    workspace.
    """
    ensure_dirs(root)
    path = session_file(root, session_id)
    now = _now_iso()
    if path.exists():
        try:
            data = json.loads(path.read_text())
        except (OSError, ValueError):
            data = None
        if data is not None:
            data["last_seen_at"] = now
            # Merge-only: fill in fields that are currently null.
            if exp_id is not None and not data.get("exp_id"):
                data["exp_id"] = exp_id
            if parent_session_id is not None and not data.get("parent_session_id"):
                data["parent_session_id"] = parent_session_id
            # Ensure engagement + optimize_mode fields exist on records
            # written by older code paths (e.g. the Rust binary's v1
            # schema without these fields).
            data.setdefault("has_evo_engaged", False)
            data.setdefault("engaged_at", None)
            data.setdefault("optimize_mode", False)
            data.setdefault("optimize_mode_at", None)
            atomic_write_json(path, data)
            return
    data = {
        "schema_version": REGISTRY_SCHEMA_VERSION,
        "session_id": session_id,
        "host": host,
        "pid": os.getpid(),
        "registered_at": now,
        "last_seen_at": now,
        "exp_id": exp_id,
        "parent_session_id": parent_session_id,
        "has_evo_engaged": False,
        "engaged_at": None,
        # v0.4.5+: optimize_mode tags a session as the orchestrator
        # currently driving /evo:optimize. Set automatically when the
        # user invokes /optimize (UserPromptSubmit pattern-match in
        # drain.py). Drives the policy nudge + stop-hook continuation.
        "optimize_mode": False,
        "optimize_mode_at": None,
    }
    atomic_write_json(path, data)
    # Seed the workspace offset to the current queue tail so this session
    # only sees events queued AFTER it registered. Without this, the
    # SessionStart-unconditional-drain in the Rust binary would backfill
    # every pre-existing event in workspace.jsonl, bypassing the
    # engagement filter.
    from .queue import init_offset_to_latest
    init_offset_to_latest(root, session_id)


def _write_optimize_mode_flag(root: Path, session_id: str) -> None:
    """Drop the empty `inject/optimize_mode/<sid>.flag` side-channel file.
    The Rust hook checks this via stat() to decide whether to hand off
    on tool/stop events without reading the session JSON. Best-effort:
    a missing flag self-heals on the next mark_optimize_mode call or
    via the drain's consistency check."""
    try:
        optimize_mode_dir(root).mkdir(parents=True, exist_ok=True)
        optimize_mode_flag_file(root, session_id).touch(exist_ok=True)
    except OSError:
        pass  # never block on cache-write failures


def _clear_optimize_mode_flag(root: Path, session_id: str) -> None:
    try:
        optimize_mode_flag_file(root, session_id).unlink()
    except FileNotFoundError:
        pass
    except OSError:
        pass


def mark_optimize_mode(root: Path, session_id: str) -> bool:
    """Flip `optimize_mode` true on the session record if currently false.

    Refuses to flag a subagent session (one with exp_id set) — subagents
    are never the optimize orchestrator. Returns True on the false→true
    transition, False on no-op (already true, missing record, or
    subagent). Caller should treat True as a signal to (optionally)
    side-effect, e.g. write to telemetry.

    Side-effect on transition: drops `inject/optimize_mode/<sid>.flag`
    as a fast-path side channel for the Rust hook.
    """
    path = session_file(root, session_id)
    if not path.exists():
        return False
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return False
    if data.get("exp_id"):
        return False  # subagent — never tag as orchestrator
    if data.get("optimize_mode"):
        # Self-heal: if the JSON says optimize_mode=true but the flag
        # file is missing (e.g. someone manually cleaned `inject/`),
        # re-create it. Cheap — the touch is idempotent.
        _write_optimize_mode_flag(root, session_id)
        return False
    data["optimize_mode"] = True
    data["optimize_mode_at"] = _now_iso()
    # Fail-open on disk-full / perms: this fires from the hot prompt
    # path. Crashing here would halt the agent. Better to leave
    # optimize_mode unset and let the user re-arm than to block the
    # hook entirely.
    try:
        atomic_write_json(path, data)
    except OSError:
        return False
    _write_optimize_mode_flag(root, session_id)
    return True


def unmark_optimize_mode(root: Path, session_id: str) -> bool:
    """Flip `optimize_mode` false on the session record if currently true.

    Used by the `evo exit-optimize-mode` CLI command. Returns True on
    the true→false transition, False on no-op (already false, missing
    record). Also removes the `inject/optimize_mode/<sid>.flag` side
    channel.
    """
    path = session_file(root, session_id)
    if not path.exists():
        # Still try to clear the flag in case the JSON was already removed
        # but the flag leaked. Cheap.
        _clear_optimize_mode_flag(root, session_id)
        return False
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        _clear_optimize_mode_flag(root, session_id)
        return False
    if not data.get("optimize_mode"):
        _clear_optimize_mode_flag(root, session_id)
        return False
    data["optimize_mode"] = False
    data["optimize_mode_at"] = None
    try:
        atomic_write_json(path, data)
    except OSError:
        # Even if the JSON write fails, clear the flag — that's the
        # signal Rust reads. User invoked `evo exit-optimize-mode`
        # explicitly; honor that intent.
        _clear_optimize_mode_flag(root, session_id)
        return False
    _clear_optimize_mode_flag(root, session_id)
    return True


def mark_engaged(root: Path, session_id: str) -> bool:
    """Flip `has_evo_engaged` true on the session record if currently
    false. Returns True if a transition happened (caller should seed
    the workspace offset to the queue tail to prevent backfill of
    directives queued before engagement). Idempotent — returns False
    on subsequent calls.

    No-op if the session isn't registered.
    """
    path = session_file(root, session_id)
    if not path.exists():
        return False
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return False
    if data.get("has_evo_engaged"):
        return False
    data["has_evo_engaged"] = True
    data["engaged_at"] = _now_iso()
    atomic_write_json(path, data)
    return True


def list_active_sessions(root: Path) -> list[dict]:
    """Return all registered (non-stale) session records. Side effect:
    GC's stale entries (and matching offset files)."""
    ensure_dirs(root)
    out: list[dict] = []
    cutoff = time.time() - STALE_AFTER_SECONDS
    for entry in sessions_dir(root).iterdir():
        if not entry.name.endswith(".json"):
            continue
        try:
            data = json.loads(entry.read_text())
        except (OSError, ValueError):
            continue
        last_seen = data.get("last_seen_at")
        try:
            if last_seen:
                ts = dt.datetime.fromisoformat(last_seen).timestamp()
            else:
                ts = 0
        except (ValueError, TypeError):
            ts = 0
        if ts < cutoff:
            # Stale — GC the entry and its matching offset file.
            try:
                entry.unlink()
            except FileNotFoundError:
                pass
            sid = data.get("session_id") or entry.stem
            try:
                offset_file(root, sid).unlink()
            except FileNotFoundError:
                pass
            continue
        out.append(data)
    return out


def get_session(root: Path, session_id: str) -> dict | None:
    """Return the registry record for a session, or None if not registered."""
    path = session_file(root, session_id)
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return None


def is_registered(root: Path, session_id: str) -> bool:
    return session_file(root, session_id).exists()


def auto_register_from_env(root: Path) -> None:
    """Best-effort: if a host session env var is set, register this
    session AND mark it as evo-engaged. Called from evo CLI's main()
    on every command — running any `evo` subcommand is the engagement
    signal.

    The engagement transition (false → true) seeds the workspace
    offset to the current tail so directives queued before the
    session engaged don't replay on this session. This is the
    "never reach sessions that never engaged" guarantee — once a
    session does engage, only post-engagement directives can deliver.

    No-op if no host env var is set, or if root isn't a workspace.
    """
    if not inject_root(root).parent.exists():
        # No active run dir — not a workspace; nothing to register against.
        return
    detected = detect_session()
    if not detected:
        return
    host, sid = detected
    # If running as a subagent, EVO_EXP_ID is set by the dispatch parent.
    exp_id = os.environ.get("EVO_EXP_ID")
    register_session(root, sid, host, exp_id=exp_id)
    # Local import to avoid a circular module dependency
    # (queue imports atomic_write_json from evo.core; registry doesn't
    # need queue at module load time).
    from .queue import init_offset_to_latest
    transitioned = mark_engaged(root, sid)
    if transitioned:
        init_offset_to_latest(root, sid)

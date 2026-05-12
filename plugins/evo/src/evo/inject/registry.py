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
    session_file,
    sessions_dir,
)

REGISTRY_SCHEMA_VERSION = 1
STALE_AFTER_SECONDS = 30 * 60  # 30 minutes; tune via config later

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
    update `last_seen_at` if present. Caller is responsible for only
    calling this when there's an active workspace.
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
    }
    atomic_write_json(path, data)


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
    session. Called from evo CLI's main() on every command. No-op if
    no host env var is set, or if root isn't a workspace.
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

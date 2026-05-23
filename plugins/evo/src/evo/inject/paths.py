"""Filesystem paths for inject queues, registry, offsets, markers.

All paths anchor under `<run_dir>/inject/` so they don't collide with
existing workspace state. Callers pass the workspace root; this module
calls `workspace_path()` (active run) for every resolution.
"""

from __future__ import annotations

from pathlib import Path

from evo.core import workspace_path


def inject_root(root: Path) -> Path:
    return workspace_path(root) / "inject"


def sessions_dir(root: Path) -> Path:
    return inject_root(root) / "sessions"


def session_file(root: Path, session_id: str) -> Path:
    return sessions_dir(root) / f"{session_id}.json"


def events_dir(root: Path) -> Path:
    return inject_root(root) / "events"


def workspace_events_path(root: Path) -> Path:
    return events_dir(root) / "workspace.jsonl"


def exp_events_path(root: Path, exp_id: str) -> Path:
    return events_dir(root) / f"{exp_id}.jsonl"


def offsets_dir(root: Path) -> Path:
    return inject_root(root) / "offsets"


def offset_file(root: Path, session_id: str) -> Path:
    return offsets_dir(root) / f"{session_id}.json"


def markers_dir(root: Path) -> Path:
    return inject_root(root) / "markers"


def marker_file(root: Path, session_id: str) -> Path:
    return markers_dir(root) / f"{session_id}.flag"


def optimize_mode_dir(root: Path) -> Path:
    """Side channel for optimize_mode state — empty flag files keyed by
    session_id. The Rust hook (and other hot-path readers) check
    existence in ~50µs instead of opening + parsing the session JSON.
    Source of truth is still `inject/sessions/<sid>.json#optimize_mode`;
    this directory is a cache that mark/unmark keep in sync.
    """
    return inject_root(root) / "optimize_mode"


def optimize_mode_flag_file(root: Path, session_id: str) -> Path:
    return optimize_mode_dir(root) / f"{session_id}.flag"


def delivered_dir(root: Path) -> Path:
    """L1 ACK: where drain writes a record per directive emitted."""
    return inject_root(root) / "delivered"


def delivered_file(root: Path, event_id: str) -> Path:
    return delivered_dir(root) / f"{event_id}.json"


def acks_dir(root: Path) -> Path:
    """L2 ACK: where `evo ack <id>` writes confirmation that the model
    saw and processed a directive."""
    return inject_root(root) / "acks"


def ack_file(root: Path, event_id: str) -> Path:
    return acks_dir(root) / f"{event_id}.json"


def ensure_dirs(root: Path) -> None:
    """Create all subdirectories under inject_root. Idempotent."""
    for d in (
        sessions_dir(root),
        events_dir(root),
        offsets_dir(root),
        markers_dir(root),
        optimize_mode_dir(root),
        delivered_dir(root),
        acks_dir(root),
    ):
        d.mkdir(parents=True, exist_ok=True)

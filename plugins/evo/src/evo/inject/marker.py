"""Marker files: existence flag for "queue has new events for this session".

Empty file on disk. Hot-path bash hook does a single `[ -f ... ]` test;
present means drain. Hook unlinks after consumption.
"""

from __future__ import annotations

from pathlib import Path

from .paths import ensure_dirs, marker_file


def touch(root: Path, session_id: str) -> None:
    ensure_dirs(root)
    path = marker_file(root, session_id)
    # Idempotent: open with O_CREAT, no truncate. Touch is enough.
    path.touch(exist_ok=True)


def exists(root: Path, session_id: str) -> bool:
    return marker_file(root, session_id).exists()


def unlink(root: Path, session_id: str) -> None:
    path = marker_file(root, session_id)
    try:
        path.unlink()
    except FileNotFoundError:
        pass

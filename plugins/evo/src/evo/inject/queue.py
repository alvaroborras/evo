"""Append-only event queues + per-session offset tracking.

Two queue files:
    workspace.jsonl    -- events for orchestrator-class sessions
    <exp_id>.jsonl     -- events for a specific subagent exp_id

Append uses O_APPEND for atomicity at the line level. Readers tolerate
trailing partial lines (stop at last newline).

Offsets are stored per session_id, scoped to the queue type.
"""

from __future__ import annotations

import datetime as dt
import json
import os
from pathlib import Path

from evo.core import atomic_write_json

from .paths import (
    ensure_dirs,
    exp_events_path,
    offset_file,
    workspace_events_path,
)

QUEUE_SCHEMA_VERSION = 1


def _ulid() -> str:
    """Best-effort monotonic ULID-ish id from time + os.urandom.

    Hex encoding (16 chars per byte) — fixed-width and sort-preserving
    in pure ASCII, unlike base32 whose alphabet (`A-Z2-7`) sorts
    non-monotonically because `'2'` (ASCII 50) precedes `'A'` (ASCII 65).
    Fixed-width prefix means lexicographic sort matches numeric order.
    """
    ts_ms = int(dt.datetime.now(dt.timezone.utc).timestamp() * 1000)
    rand = os.urandom(10)
    payload = ts_ms.to_bytes(6, "big") + rand
    return payload.hex().upper()


def _now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def _append_jsonl(path: Path, record: dict) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(record, separators=(",", ":")) + "\n"
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
    try:
        os.write(fd, line.encode("utf-8"))
    finally:
        os.close(fd)
    return record["id"]


def append_workspace_event(root: Path, text: str, **fields) -> str:
    """Append a directive to the workspace queue. `fields` is reserved
    for future metadata (source, sender, etc.) — not load-bearing today."""
    ensure_dirs(root)
    record = {
        "schema_version": QUEUE_SCHEMA_VERSION,
        "id": _ulid(),
        "ts": _now_iso(),
        "text": text,
    }
    record.update(fields)
    return _append_jsonl(workspace_events_path(root), record)


def append_exp_event(root: Path, exp_id: str, text: str, **fields) -> str:
    """Append a directive to a subagent-scoped queue."""
    ensure_dirs(root)
    record = {
        "schema_version": QUEUE_SCHEMA_VERSION,
        "id": _ulid(),
        "ts": _now_iso(),
        "text": text,
    }
    record.update(fields)
    return _append_jsonl(exp_events_path(root, exp_id), record)


def read_events_after(path: Path, after_id: str | None) -> list[dict]:
    """Read all events from `path` whose id > after_id. Returns [] if
    file missing. Tolerates a trailing partial line (last byte not a
    newline) — skips it safely."""
    if not path.exists():
        return []
    try:
        text = path.read_text()
    except OSError:
        return []
    out: list[dict] = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            # Partial / corrupt line — skip
            continue
        rec_id = rec.get("id")
        if rec_id is None:
            continue
        if after_id is None or rec_id > after_id:
            out.append(rec)
    return out


def read_offset(root: Path, session_id: str, queue: str) -> str | None:
    """`queue` is 'workspace' or 'exp'. Returns the last_id seen, or None."""
    path = offset_file(root, session_id)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    if queue == "workspace":
        return data.get("last_workspace_event_id")
    if queue == "exp":
        return data.get("last_exp_event_id")
    return None


def write_offset(
    root: Path,
    session_id: str,
    *,
    workspace_id: str | None = None,
    exp_id: str | None = None,
) -> None:
    ensure_dirs(root)
    path = offset_file(root, session_id)
    data: dict = {}
    if path.exists():
        try:
            data = json.loads(path.read_text())
        except (OSError, ValueError):
            data = {}
    data["schema_version"] = QUEUE_SCHEMA_VERSION
    data["session_id"] = session_id
    if workspace_id is not None:
        data["last_workspace_event_id"] = workspace_id
    if exp_id is not None:
        data["last_exp_event_id"] = exp_id
    data["updated_at"] = _now_iso()
    atomic_write_json(path, data)


def init_offset_to_latest(root: Path, session_id: str) -> None:
    """Set offset to the latest event ids in workspace + this session's
    exp queue (if applicable). Called at registration time so newly
    registered sessions don't get backfilled.
    """
    workspace_path = workspace_events_path(root)
    workspace_latest = None
    if workspace_path.exists():
        events = read_events_after(workspace_path, None)
        if events:
            workspace_latest = events[-1]["id"]
    write_offset(root, session_id, workspace_id=workspace_latest)

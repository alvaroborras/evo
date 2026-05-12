"""Hermes runtime plugin — auto-discovered via pip entry-point
`hermes_agent.plugins`.

Hooks registered:
- `on_session_start`: registers the hermes session in evo's inject
  registry. Return value is ignored by hermes.
- `pre_llm_call`: drains pending events into `{"context": "..."}` —
  this IS the canonical injection point on hermes (per docs).

In-process function calls; no fork+exec. We don't use the marker
fast-path optimization because the cost of reading the queue file is
already sub-millisecond and far below model RTT.

See notes/cross-host-inject-design.md.
"""

from __future__ import annotations

from pathlib import Path

from evo.core import repo_root
from evo.inject import marker
from evo.inject.paths import inject_root, exp_events_path, workspace_events_path
from evo.inject.queue import read_events_after, read_offset, write_offset
from evo.inject.registry import get_session, register_session
from evo.inject.drain import format_directive_text


def _resolve_root() -> Path | None:
    """Return the workspace root if we're inside an evo workspace."""
    try:
        root = repo_root()
    except Exception:
        return None
    if not (root / ".evo").exists():
        return None
    if not inject_root(root).parent.exists():
        return None
    return root


def _ensure_registered(root: Path, session_id: str) -> None:
    """Register the hermes session if not already in the registry."""
    if get_session(root, session_id) is None:
        register_session(root, session_id, "hermes")


def _compute_drain_text(root: Path, session_id: str) -> str | None:
    """Read pending events for `session_id`, format, update offset, unlink
    marker. Returns the formatted text or None if nothing to deliver.
    Mirrors evo.inject.drain.drain_session minus stdout I/O."""
    sess = get_session(root, session_id)
    if sess is None:
        marker.unlink(root, session_id)
        return None
    exp_id = sess.get("exp_id")
    events: list[dict] = []
    new_workspace_offset: str | None = None
    new_exp_offset: str | None = None

    if exp_id:
        last_id = read_offset(root, session_id, "exp")
        new_events = read_events_after(exp_events_path(root, exp_id), last_id)
        events.extend(new_events)
        if new_events:
            new_exp_offset = new_events[-1]["id"]
    else:
        last_id = read_offset(root, session_id, "workspace")
        new_events = read_events_after(workspace_events_path(root), last_id)
        events.extend(new_events)
        if new_events:
            new_workspace_offset = new_events[-1]["id"]

    text = format_directive_text(events) if events else None
    if new_workspace_offset or new_exp_offset:
        write_offset(
            root,
            session_id,
            workspace_id=new_workspace_offset,
            exp_id=new_exp_offset,
        )
    marker.unlink(root, session_id)
    return text or None


def _on_session_start(session_id: str | None = None, **kwargs):
    """Register the session. No drain — hermes ignores this hook's
    return value; pre_llm_call is the only context-injection point."""
    if not session_id:
        return None
    root = _resolve_root()
    if root is None:
        return None
    _ensure_registered(root, session_id)
    return None


def _on_pre_llm_call(session_id: str | None = None, **kwargs):
    """Per-turn drain. Always reads the queue (in-process is cheap).
    Returns {"context": "..."} when there's content to inject."""
    if not session_id:
        return None
    root = _resolve_root()
    if root is None:
        return None
    _ensure_registered(root, session_id)
    text = _compute_drain_text(root, session_id)
    if text:
        return {"context": text}
    return None


def register(ctx) -> None:
    """Hermes plugin entry point — invoked once at plugin load."""
    ctx.register_hook("on_session_start", _on_session_start)
    ctx.register_hook("pre_llm_call", _on_pre_llm_call)

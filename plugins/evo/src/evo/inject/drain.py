"""Drain entry point: read events for a session, format host-specific
output, update offset, unlink marker.

Invoked by the bash hot-path script via `python3 -m evo.drain` only
after the marker file confirmed there's something to deliver.

Output goes to stdout in the format the host expects:
    Claude Code / Codex: {"hookSpecificOutput": {"hookEventName": "...", "additionalContext": "..."}}
    hermes:              {"context": "..."}
    opencode:            JSON describing the mutation; in-process plugin
                         interprets it and applies to the right hook input.

Hosts call it differently — Claude Code and Codex shell-exec the bash
hook which exec's `python3 -m evo.drain`. hermes and opencode plugins
are in-process and call into Python/TS equivalents directly; for those
hosts this module's logic is mirrored, not invoked via subprocess.

Per `notes/cross-host-inject-design.md`.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from . import marker, queue
from .paths import (
    exp_events_path,
    inject_root,
    workspace_events_path,
)
from .registry import get_session


HOST_HOOK_EVENT_NAMES = {
    "claude-code": ("PreToolUse", "UserPromptSubmit", "SessionStart", "PostToolUse"),
    "codex": ("PreToolUse", "UserPromptSubmit", "SessionStart", "PostToolUse"),
}


def _detect_host_from_env() -> str:
    """Best-effort host detection from env. Default 'claude-code'.

    Codex exposes the session as CODEX_THREAD_ID (not CODEX_SESSION_ID)
    on codex-cli 0.130. Keep this in sync with
    registry.HOST_SESSION_ENV_VARS.
    """
    if os.environ.get("CLAUDE_CODE_SESSION_ID"):
        return "claude-code"
    if os.environ.get("CODEX_THREAD_ID"):
        return "codex"
    if os.environ.get("HERMES_SESSION_ID"):
        return "hermes"
    if os.environ.get("OPENCODE_SESSION_ID"):
        return "opencode"
    return "claude-code"


def _detect_hook_event_from_stdin() -> str | None:
    """Hook stdin payloads include hook_event_name. Read it best-effort."""
    if sys.stdin.isatty():
        return None
    try:
        raw = sys.stdin.read()
    except Exception:  # noqa: BLE001
        return None
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return None
    return data.get("hook_event_name") or data.get("hookEventName")


def format_directive_text(events: list[dict]) -> str:
    """Format events as a single text block to splice into the agent's
    next turn. Keeps it terse — agents treat additionalContext as
    authoritative, so we want minimum noise.

    Single event type today: user directives via `evo direct`. The SKILL
    documents what `[evo direct]` means; we just emit a stable prefix.
    """
    lines = []
    for ev in events:
        text = ev.get("text", "")
        if text:
            lines.append(f"[evo direct] {text}")
    return "\n".join(lines)


def emit_for_host(host: str, hook_event: str | None, text: str) -> None:
    """Write the host-specific JSON payload to stdout."""
    if not text:
        sys.stdout.write("{}")
        return
    if host in ("claude-code", "codex"):
        # Both honor the same envelope shape. Default to PreToolUse if
        # we couldn't read it from stdin.
        evt = hook_event or "PreToolUse"
        payload = {
            "hookSpecificOutput": {
                "hookEventName": evt,
                "additionalContext": text,
            }
        }
        sys.stdout.write(json.dumps(payload, separators=(",", ":")))
        return
    if host == "hermes":
        payload = {"context": text}
        sys.stdout.write(json.dumps(payload, separators=(",", ":")))
        return
    # opencode and other in-process hosts: this entry point shouldn't
    # normally be invoked there. Fall through to a generic envelope.
    sys.stdout.write(json.dumps({"text": text}, separators=(",", ":")))


def drain_session(root: Path, session_id: str, host: str | None = None, hook_event: str | None = None) -> int:
    """Read events for `session_id`, format, emit, update offset,
    unlink marker. Returns 0 on success."""
    if not inject_root(root).exists():
        sys.stdout.write("{}")
        return 0
    sess = get_session(root, session_id)
    if sess is None:
        # Session somehow not registered but marker existed. Be lenient.
        marker.unlink(root, session_id)
        sys.stdout.write("{}")
        return 0

    host = host or sess.get("host") or _detect_host_from_env()
    # Codex and Claude Code use the same hookSpecificOutput envelope.
    # If host is "unknown" (e.g. legacy registry entry), default to that
    # envelope since it's the more common case for shell-hook hosts.
    if host == "unknown":
        host = "claude-code"
    exp_id = sess.get("exp_id")

    events: list[dict] = []
    new_workspace_offset: str | None = None
    new_exp_offset: str | None = None

    if exp_id:
        # Subagent: drain its scoped queue only
        last_id = queue.read_offset(root, session_id, "exp")
        new_events = queue.read_events_after(exp_events_path(root, exp_id), last_id)
        events.extend(new_events)
        if new_events:
            new_exp_offset = new_events[-1]["id"]
    else:
        # Orchestrator-class session: drain workspace queue
        last_id = queue.read_offset(root, session_id, "workspace")
        new_events = queue.read_events_after(workspace_events_path(root), last_id)
        events.extend(new_events)
        if new_events:
            new_workspace_offset = new_events[-1]["id"]

    text = format_directive_text(events)
    emit_for_host(host, hook_event, text)

    # Update offset and unlink marker — only after successful emit
    if new_workspace_offset or new_exp_offset:
        queue.write_offset(
            root,
            session_id,
            workspace_id=new_workspace_offset,
            exp_id=new_exp_offset,
        )
    marker.unlink(root, session_id)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="evo.drain")
    parser.add_argument("--run-dir", required=True, help="Path to .evo/run_*/ directory")
    parser.add_argument("--session", required=True, help="session_id to drain")
    parser.add_argument("--host", default=None, help="host name (claude-code/codex/hermes/opencode); auto-detected if omitted")
    args = parser.parse_args(argv)

    run_dir = Path(args.run_dir)
    # The drain logic uses workspace_path() internally via paths.py;
    # but here we receive run_dir directly. We need root. Walk up from
    # run_dir/.. to find the workspace root (the dir CONTAINING .evo/).
    # run_dir is .../.evo/run_*; parent.parent is the workspace root.
    if run_dir.parent.name == ".evo":
        root = run_dir.parent.parent
    else:
        # Fallback: assume run_dir itself is under .evo/ at depth 1
        root = run_dir.parent.parent

    hook_event = _detect_hook_event_from_stdin()
    return drain_session(root, args.session, host=args.host, hook_event=hook_event)


if __name__ == "__main__":
    sys.exit(main())

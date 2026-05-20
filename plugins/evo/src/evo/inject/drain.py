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
import datetime as dt
import json
import os
import sys
from pathlib import Path

from . import marker, queue
from .paths import (
    exp_events_path,
    inject_root,
    session_file,
    workspace_events_path,
)
from .registry import get_session, register_session

# Hook event names that signal a fresh session — drain unconditionally on
# these to catch directives queued before the session existed. Covers both
# Claude Code's PascalCase and Cursor's camelCase spelling.
_SESSION_START_EVENTS = ("SessionStart", "sessionStart")


def _drain_debug(**fields) -> None:
    """Append a diagnostic line to ~/.cursor/evo-drain.log, but only when the
    opt-in sentinel ~/.cursor/.evo-drain-debug exists (or EVO_DRAIN_DEBUG is
    set). Used to diagnose why a directive isn't reaching a Cursor IDE
    session — shows the hook payload shape and where the drain decided to
    bail. Never logs in normal operation; failures are swallowed."""
    try:
        sentinel = Path.home() / ".cursor" / ".evo-drain-debug"
        if not sentinel.exists() and not os.environ.get("EVO_DRAIN_DEBUG"):
            return
        rec = {"ts": dt.datetime.now().isoformat(timespec="seconds"), **fields}
        log = Path.home() / ".cursor" / "evo-drain.log"
        with log.open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec) + "\n")
    except Exception:  # noqa: BLE001 — diagnostics must never break the hook
        pass


HOST_HOOK_EVENT_NAMES = {
    "claude-code": ("PreToolUse", "UserPromptSubmit", "SessionStart", "PostToolUse"),
    "codex": ("PreToolUse", "UserPromptSubmit", "SessionStart", "PostToolUse"),
    # Cursor: sessionStart only registers; the IDE drops additional_context,
    # so directives are delivered on stop via followup_message (auto-submitted
    # as a visible message).
    "cursor": ("sessionStart", "stop"),
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


def _read_stdin_payload() -> dict:
    """Read the host's hook stdin payload as a dict. Returns {} when stdin
    is a tty, empty, or not JSON. stdin can only be consumed once, so this
    is the single read point — all fields (hook event, session id,
    workspace roots) are derived from the returned dict."""
    if sys.stdin.isatty():
        return {}
    try:
        raw = sys.stdin.read()
    except Exception:  # noqa: BLE001
        return {}
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


def _hook_event_from_payload(payload: dict) -> str | None:
    return payload.get("hook_event_name") or payload.get("hookEventName")


def _resolve_root_from_payload(payload: dict) -> Path | None:
    """Locate the workspace root (dir containing `.evo/`) for hosts whose
    hook command runs from outside the project. Cursor's user-level
    `~/.cursor/hooks.json` runs from `~/.cursor/`, not the repo, so cwd is
    useless — the project path arrives in the payload's `workspace_roots`.
    Falls back to walking up from cwd. Returns None if no `.evo/` is found.
    """
    candidates: list[Path] = []
    roots = payload.get("workspace_roots")
    if isinstance(roots, list):
        candidates.extend(Path(r) for r in roots if isinstance(r, str) and r)
    candidates.append(Path.cwd())
    for start in candidates:
        cur = start
        while True:
            if (cur / ".evo").is_dir():
                return cur
            if cur.parent == cur:
                break
            cur = cur.parent
    return None


def _self_contained_gate(
    root: Path, session_id: str, host: str, hook_event: str | None
) -> bool:
    """Replicate the Rust hot-path's gate for hosts wired directly to
    `evo-drain` (no `evo-hook-drain` binary in front). Returns True when the
    caller should proceed to drain.

    On a session-start event: register the session and seed its offset to the
    current queue tail, then return False — DON'T drain. On Cursor the only
    output sessionStart can return (additional_context) is silently dropped by
    the IDE, so draining here would just consume directives (advance the
    offset, clear the marker) before the stop hook could deliver them. Seeding
    the offset also avoids replaying directives queued before this session.

    For other events (e.g. stop): require both a registered session and a
    marker file, then drain.
    """
    if hook_event in _SESSION_START_EVENTS:
        if not session_file(root, session_id).exists():
            register_session(root, session_id, host)
            queue.init_offset_to_latest(root, session_id)
        return False
    if not session_file(root, session_id).exists():
        return False
    return marker.exists(root, session_id)


def format_directive_text(events: list[dict]) -> str:
    """Format events as a single text block to splice into the agent's
    next turn.

    Wraps each event with the `[EVO DIRECTIVE]` / `[END EVO DIRECTIVE]`
    banner pair. The banner is the authenticity signal — `optimize` and
    `subagent` skills tell the agent that text inside this banner is
    user-authoritative (issued via `evo direct`), not tool-output prompt
    injection. Without the banner, models like gpt-5 / opus-4-7 may
    refuse the directive as suspicious.
    """
    lines = []
    for ev in events:
        text = ev.get("text", "")
        if text:
            lines.append("[EVO DIRECTIVE]")
            lines.append(text)
            lines.append("[END EVO DIRECTIVE]")
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
    if host == "cursor":
        # Cursor's hook output contract differs from Claude Code's: there is
        # no hookSpecificOutput envelope. `sessionStart` and `postToolUse`
        # honor {"additional_context": ...}; `stop`/`subagentStop` honor
        # {"followup_message": ...}. `beforeSubmitPrompt` is informational
        # only (cannot inject), so it is never wired as a drain channel.
        if hook_event in ("stop", "subagentStop"):
            sys.stdout.write(json.dumps({"followup_message": text}, separators=(",", ":")))
        else:
            sys.stdout.write(json.dumps({"additional_context": text}, separators=(",", ":")))
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
    """Two invocation modes:

    1. Front-ended by the `evo-hook-drain` Rust binary (claude-code/codex):
       it passes `--run-dir` and `--session` and has already done the marker
       gate, so this just drains.
    2. Self-contained (cursor): the host's hooks.json calls `evo-drain
       --host cursor` directly with no Rust binary in front. `--run-dir` and
       `--session` are omitted; they're resolved from the hook stdin payload
       (`workspace_roots`, `conversation_id`) and the marker gate runs here.
    """
    parser = argparse.ArgumentParser(prog="evo.drain")
    parser.add_argument("--run-dir", default=None, help="Path to .evo/run_*/ directory (omit for self-contained hosts)")
    parser.add_argument("--session", default=None, help="session_id to drain (omit to read from stdin payload)")
    parser.add_argument("--host", default=None, help="host name (claude-code/codex/hermes/opencode/cursor); auto-detected if omitted")
    args = parser.parse_args(argv)

    payload = _read_stdin_payload()
    hook_event = _hook_event_from_payload(payload)

    # Mode 1: Rust-driven — run-dir + session supplied, gate already done.
    if args.run_dir:
        run_dir = Path(args.run_dir)
        # run_dir is .../.evo/run_*; the workspace root is its grandparent.
        root = run_dir.parent.parent
        return drain_session(root, args.session, host=args.host, hook_event=hook_event)

    # Mode 2: self-contained — resolve everything from args + stdin payload.
    # Key on conversation_id: it's present in EVERY Cursor hook event, whereas
    # session_id only appears in sessionStart. Keying on session_id would
    # register the session under one id at sessionStart and then look up a
    # different id at postToolUse (where session_id is absent), so mid-run
    # directives would never be delivered.
    host = args.host or "cursor"
    session = args.session or payload.get("conversation_id") or payload.get("session_id")
    root = _resolve_root_from_payload(payload)
    if not session or root is None or not inject_root(root).parent.exists():
        _drain_debug(stage="resolve", host=host, hook_event=hook_event,
                     payload_keys=sorted(payload.keys()), session=session,
                     root=str(root) if root else None, decision="bail")
        sys.stdout.write("{}")
        return 0
    registered = session_file(root, session).exists()
    has_marker = marker.exists(root, session)
    gate = _self_contained_gate(root, session, host, hook_event)
    _drain_debug(stage="gate", host=host, hook_event=hook_event, session=session,
                 root=str(root), registered_before=registered, marker=has_marker,
                 gate=gate)
    if not gate:
        sys.stdout.write("{}")
        return 0
    return drain_session(root, session, host=host, hook_event=hook_event)


if __name__ == "__main__":
    sys.exit(main())

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
import shlex
import sys
from pathlib import Path

from . import marker, queue
from .paths import (
    exp_events_path,
    inject_root,
    offset_file,
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
    # PreToolUse / PostToolUse / UserPromptSubmit / SessionStart use the
    # hookSpecificOutput.additionalContext envelope; Stop / SubagentStop use
    # the {decision: "block", reason: ...} envelope which Claude Code and
    # Codex both feed back as the next continuation prompt (verified via
    # the ralph-loop plugin's stop-hook.sh).
    "claude-code": ("PreToolUse", "PostToolUse", "UserPromptSubmit", "SessionStart", "Stop", "SubagentStop"),
    "codex": ("PreToolUse", "PostToolUse", "UserPromptSubmit", "SessionStart", "Stop", "SubagentStop"),
    # Cursor: sessionStart + beforeSubmitPrompt register the session; preToolUse
    # delivers mid-turn for SHELL tools only (updated_input echo into stdout);
    # every non-shell tool defers (no consume) and is delivered at turn end via
    # stop -> followup_message. subagentStop fires at Task-subagent turn-end
    # (Cursor 1.7+), same envelope as stop. (additional_context is dropped by
    # the IDE, and agent_message-on-deny consumes without the agent acting —
    # both verified.)
    "cursor": ("sessionStart", "beforeSubmitPrompt", "preToolUse", "stop", "subagentStop"),
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


_DELIVER_EVENTS = ("stop", "subagentStop", "preToolUse")


def _cursor_tool_class(tool_name: str | None) -> str:
    """Classify a Cursor preToolUse tool_name. Only 'shell' has a working
    mid-turn delivery channel — rewrite the command via updated_input so the
    directive prints to stdout (the tool result the model reads); the command
    still runs. Every other tool DEFERS (no consume) and is delivered at the
    turn-end stop: deny+agent_message was tried and consumes the directive
    without the agent acting on it. The 'edit'/'other' split is retained only
    for debug logging. Tool names vary by Cursor version — match substrings.
    """
    t = (tool_name or "").lower()
    if any(k in t for k in ("shell", "bash", "terminal", "run_command", "runterminalcmd")):
        return "shell"
    if any(k in t for k in ("edit", "write", "create_file", "search_replace", "str_replace", "applypatch", "apply_patch")):
        return "edit"
    return "other"


import re as _re

_EVO_CMD_RE = _re.compile(r"^\s*evo(\s|$)")


# Per-host invocation patterns for /evo:optimize. Anchored to prompt
# start so a brief or quoted text containing the invocation mid-string
# doesn't accidentally match. Patterns are case-insensitive (the user
# may type /EVO:OPTIMIZE or /Evo:Optimize).
#
# Each host's value is a list of regexes; the prompt matches if any
# regex matches.
_OPTIMIZE_INVOCATION_PATTERNS: dict[str, list[_re.Pattern[str]]] = {
    "claude-code": [_re.compile(r"^\s*/evo:optimize\b", _re.IGNORECASE)],
    "codex": [
        _re.compile(r"^\s*\$evo:optimize\b", _re.IGNORECASE),
        _re.compile(r"^\s*\$evo\s+optimize\b", _re.IGNORECASE),
    ],
    "cursor": [_re.compile(r"^\s*/optimize\b", _re.IGNORECASE)],
    # hermes / opencode / openclaw / pi: TBD — confirm exact invocation
    # syntax per host. Leaving empty for now means /optimize won't
    # auto-flip on those hosts; manual `evo` commands during the loop
    # will still trigger the existing engagement filter, just not the
    # optimize_mode-specific behaviors (policy nudge, stop continuation).
    "hermes": [_re.compile(r"^\s*/optimize\b", _re.IGNORECASE)],
    "opencode": [_re.compile(r"^\s*/optimize\b", _re.IGNORECASE)],
    "openclaw": [_re.compile(r"^\s*/optimize\b", _re.IGNORECASE)],
    "pi": [_re.compile(r"^\s*/optimize\b", _re.IGNORECASE)],
}


def _extract_user_prompt(payload: dict | None) -> str:
    """Pull the user's prompt text out of a hook payload. Different hosts
    name the field differently. Returns "" if nothing recognizable."""
    if not payload:
        return ""
    for key in ("prompt", "user_prompt", "userPrompt", "message", "input"):
        val = payload.get(key)
        if isinstance(val, str) and val:
            return val
    return ""


def _maybe_mark_optimize_from_prompt(
    root: Path,
    session_id: str,
    host: str,
    hook_event: str | None,
    payload: dict | None,
) -> None:
    """If the host hook payload looks like a /evo:optimize invocation,
    flip optimize_mode on the session record.

    Runs on UserPromptSubmit-like events (UserPromptSubmit, sessionStart,
    beforeSubmitPrompt) for any host. Subagent fence is enforced inside
    `mark_optimize_mode` — it refuses to flag a session with exp_id set,
    so a subagent receiving a brief that quotes /evo:optimize won't be
    mistakenly tagged.
    """
    # Only check on prompt-submit-shaped events. Tool-call events have
    # no user prompt to match against.
    prompt_events = (
        "UserPromptSubmit", "userPromptSubmit",
        "beforeSubmitPrompt", "SessionStart", "sessionStart",
    )
    if hook_event not in prompt_events:
        return
    patterns = _OPTIMIZE_INVOCATION_PATTERNS.get(host, [])
    if not patterns:
        return
    prompt = _extract_user_prompt(payload)
    if not prompt:
        return
    if not any(p.search(prompt) for p in patterns):
        return
    # mark_optimize_mode is idempotent and refuses to flag subagents.
    from .registry import mark_optimize_mode
    mark_optimize_mode(root, session_id)


def _maybe_mark_engaged_from_shell(
    root: Path,
    session_id: str,
    host: str,
    hook_event: str | None,
    payload: dict | None,
) -> None:
    """For self-contained hosts (currently Cursor): if the agent's about
    to run an `evo` shell command, mark the session as evo-engaged and
    seed the workspace offset to the queue tail. Mirrors what
    `auto_register_from_env` does for hosts that route engagement
    through the Python CLI path.
    """
    if host != "cursor":
        return
    if hook_event != "preToolUse":
        return
    if _cursor_tool_class((payload or {}).get("tool_name")) != "shell":
        return
    cmd = ((payload or {}).get("tool_input") or {}).get("command", "")
    if not isinstance(cmd, str) or not _EVO_CMD_RE.match(cmd):
        return
    # Idempotent: mark_engaged returns False after the first transition.
    from .registry import mark_engaged
    if mark_engaged(root, session_id):
        queue.init_offset_to_latest(root, session_id)


def _self_contained_gate(
    root: Path, session_id: str, host: str, hook_event: str | None,
    tool_name: str | None = None,
    tool_input: dict | None = None,
) -> bool:
    """Gate for hosts wired directly to `evo-drain` (no `evo-hook-drain`
    binary in front). Returns True when the caller should proceed to drain.

    Registers the session on the FIRST event of any kind, seeding its offset
    to the current queue tail. `sessionStart` only fires for brand-new chats —
    a *resumed* Cursor chat never fires it, so registration must also happen on
    the other wired events (beforeSubmitPrompt, stop); otherwise the session
    stays unregistered and `evo direct` can never reach it. Seeding the offset
    avoids replaying directives queued before this session existed.

    Delivery events are `_DELIVER_EVENTS` (preToolUse / stop / subagentStop).
    Everything else (sessionStart, beforeSubmitPrompt) is register-only — the
    IDE drops additional_context so there's nothing to deliver, and draining
    there would just consume directives. On `preToolUse`, only a SHELL tool
    delivers (updated_input echo); every non-shell tool is DEFERRED (return
    False, no consume) so the directive waits (peek-don't-pop) for the next
    shell call or the turn-end stop — deny+agent_message consumes without
    delivering on non-shell tools.
    """
    fresh = not session_file(root, session_id).exists()
    if fresh:
        register_session(root, session_id, host)
        queue.init_offset_to_latest(root, session_id)
    if hook_event not in _DELIVER_EVENTS:
        return False  # register-only (sessionStart, beforeSubmitPrompt, …)

    # Steering bypass for orchestrator-class sessions in optimize_mode:
    #   - stop/subagentStop: always-fire stop nudge needs drain to run.
    #   - preToolUse: only when the tool is on the deny list. Letting
    #     drain_session run on a non-denied tool would consume queued
    #     directives by emitting `{additional_context: …}` — which the
    #     IDE drops — silently losing the directive.
    sess = get_session(root, session_id)
    if sess and sess.get("optimize_mode") and not sess.get("exp_id"):
        if hook_event in ("stop", "subagentStop"):
            return True
        if hook_event == "preToolUse" and _is_denied_in_optimize_mode(tool_name, tool_input):
            return True

    if hook_event == "preToolUse" and _cursor_tool_class(tool_name) != "shell":
        # Only shell delivers mid-turn (updated_input echo into stdout, which
        # the model reliably reads). For every other tool, deny+agent_message
        # CONSUMES the directive without actually delivering it (verified: a
        # Read deny clears the marker but the agent never gets the message).
        # So defer (no consume) — the directive waits for the next shell call
        # or the turn-end `stop`, both of which deliver reliably.
        return False
    if fresh:
        return False  # just registered on this event; nothing marked yet
    return marker.exists(root, session_id)


def format_directive_text(events: list[dict]) -> str:
    """Format events as a single text block to splice into the agent's
    next turn.

    Wraps each event with the `[EVO DIRECTIVE id=<id>]` / `[END EVO DIRECTIVE]`
    banner pair. The banner is the authenticity signal — `optimize` and
    `subagent` skills tell the agent that text inside this banner is
    user-authoritative (issued via `evo direct`), not tool-output prompt
    injection. Without the banner, models like gpt-5 / opus-4-7 may
    refuse the directive as suspicious.

    The trailing `evo ack <id>` instruction tells the agent how to ack
    the directive after acting on it; the CLI command writes
    inject/acks/<id>.json which `evo direct status` and `evo direct --wait`
    surface. Acks are best-effort — models sometimes forget — but presence
    of an ack is a positive signal that the directive landed.
    """
    lines = []
    for ev in events:
        text = ev.get("text", "")
        if not text:
            continue
        ev_id = ev.get("id", "")
        if ev_id:
            lines.append(f"[EVO DIRECTIVE id={ev_id}]")
            lines.append(text)
            lines.append(f"[END EVO DIRECTIVE — when done, run: evo ack {ev_id}]")
        else:
            # Legacy fallback for events without an id (shouldn't occur
            # with the current queue, but be defensive).
            lines.append("[EVO DIRECTIVE]")
            lines.append(text)
            lines.append("[END EVO DIRECTIVE]")
    return "\n".join(lines)


def _write_delivery_records(
    root: Path,
    events: list[dict],
    session_id: str,
    host: str,
    hook_event: str | None,
) -> None:
    """L1 ACK: record per-event delivery. Best-effort; never raises.

    Writes one file per event delivered, keyed by event id. Multiple
    sessions delivering the same event each leave their own record
    keyed by sid (the file's session_id field); since file names are
    keyed only on event_id, later writes for the same event overwrite
    earlier ones — `evo direct status` queries the file to know "this
    event was delivered to at least one session," not exhaustive routing.
    Good enough for the diagnostic role.
    """
    from .paths import delivered_file, delivered_dir
    try:
        delivered_dir(root).mkdir(parents=True, exist_ok=True)
        now = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
        for ev in events:
            ev_id = ev.get("id")
            if not ev_id:
                continue
            rec = {
                "event_id": ev_id,
                "session_id": session_id,
                "host": host,
                "hook_event": hook_event,
                "delivered_at": now,
            }
            delivered_file(root, ev_id).write_text(json.dumps(rec))
    except OSError:  # pragma: no cover — never break the hot path
        pass


def emit_for_host(host: str, hook_event: str | None, text: str, payload: dict | None = None) -> None:
    """Write the host-specific JSON payload to stdout. `payload` is the raw
    hook stdin (needed by the cursor preToolUse branch for tool_name/command)."""
    if not text:
        sys.stdout.write("{}")
        return
    if host in ("claude-code", "codex"):
        # Stop / SubagentStop don't support additionalContext — they must
        # return `{decision: "block", reason: text}`, which both hosts feed
        # back as the next continuation prompt (verified via the ralph-loop
        # plugin's stop-hook.sh on Claude Code; documented for Codex).
        if hook_event in ("Stop", "SubagentStop"):
            out = {"decision": "block", "reason": text}
            sys.stdout.write(json.dumps(out, separators=(",", ":")))
            return
        # All other events (PreToolUse / PostToolUse / UserPromptSubmit /
        # SessionStart) honor the same hookSpecificOutput envelope. Default
        # to PreToolUse if we couldn't read it from stdin.
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
        # Cursor delivery, routed around the IDE's broken additional_context:
        #   stop/subagentStop -> followup_message (auto-submitted at turn end)
        #   preToolUse + shell -> updated_input: prepend an echo of the directive
        #     so it lands in the command's stdout (the tool result the model
        #     reads); command still runs (exit code preserved by ordering last).
        # Non-shell preToolUse never reaches here — the gate defers it (deny+
        # agent_message consumes without delivering, so we let stop deliver).
        p = payload or {}
        if hook_event in ("stop", "subagentStop"):
            out: dict = {"followup_message": text}
        elif hook_event == "preToolUse" and _cursor_tool_class(p.get("tool_name")) == "shell":
            tool_input = dict(p.get("tool_input") or {})
            orig = tool_input.get("command", "")
            tool_input["command"] = "printf '%s\\n' " + shlex.quote(text) + " ; " + orig
            out = {"permission": "allow", "updated_input": tool_input}
        else:
            out = {"additional_context": text}
        sys.stdout.write(json.dumps(out, separators=(",", ":")))
        return
    # opencode and other in-process hosts: this entry point shouldn't
    # normally be invoked there. Fall through to a generic envelope.
    sys.stdout.write(json.dumps({"text": text}, separators=(",", ":")))


def drain_session(root: Path, session_id: str, host: str | None = None, hook_event: str | None = None, payload: dict | None = None) -> int:
    """Read events for `session_id`, format, emit, update offset,
    unlink marker. Returns 0 on success. `payload` is the raw hook stdin,
    passed through to emit_for_host (cursor preToolUse needs tool_name/command)."""
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

    # Seed offset to the current queue tail for sessions that don't yet
    # have an offset file. Closes the SessionStart-unconditional-drain
    # leak: the Rust binary skips the marker check on SessionStart and
    # hands off to this drain regardless, so without seeding, a fresh
    # session would backfill every event in workspace.jsonl — bypassing
    # the engagement filter. Contract: a session only sees events queued
    # after it registered.
    if not offset_file(root, session_id).exists():
        queue.init_offset_to_latest(root, session_id)

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

    # Policy block: orchestrator in optimize_mode tried a denied tool.
    # Hard-deny on odd-numbered violations (1, 3, 5, …); even ones pass.
    # Short-circuits the normal emit — agent sees the deny + banner.
    if _should_policy_block(root, session_id, sess, host, hook_event, payload):
        sys.stdout.write(json.dumps(_policy_block_envelope(host), separators=(",", ":")))
        # Don't unlink marker / advance offset on a policy block — the
        # inject directive (if any) hasn't actually been delivered to
        # the model; the tool was denied before that point. Leave the
        # state so the next non-violating tool call can drain it.
        return 0

    # Cursor non-shell preToolUse: the IDE drops `additional_context`,
    # so we'd consume queued directives without delivering them. This
    # path is only reachable in optimize_mode (the gate normally defers
    # non-shell preToolUse). The policy block didn't fire (even-numbered
    # violation under the alternating cadence), so we let the tool
    # through but must NOT consume directives.
    if (
        host == "cursor"
        and hook_event in ("preToolUse", "PreToolUse")
        and _cursor_tool_class(payload.get("tool_name") if payload else None) != "shell"
    ):
        sys.stdout.write("{}")
        return 0

    # Stop-nudge: if this is a Stop/SubagentStop on an orchestrator in
    # optimize_mode, augment (or override) the emit with a continuation
    # prompt so the agent keeps driving the loop autonomously. Always
    # fires while optimize_mode is on — escape via `evo exit-optimize-mode`.
    nudge_text = _maybe_stop_nudge_text(root, session_id, sess, host, hook_event)
    if nudge_text:
        # Combine pending directive text with the nudge so queued
        # `evo direct` injections aren't dropped when the stop hook
        # fires. The nudge envelope still drives self-continuation.
        combined = (text + "\n\n" + nudge_text) if text else nudge_text
        emit_for_host(host, hook_event, combined, payload)
    else:
        emit_for_host(host, hook_event, text, payload)

    # L1 ACK: record delivery for each event so `evo direct status <id>`
    # can show whether the drain emitted the directive to a session.
    if events:
        _write_delivery_records(root, events, session_id, host, hook_event)

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


# ---------------------------------------------------------------------------
# Policy nudge — block orchestrator strays from /optimize protocol
# ---------------------------------------------------------------------------


# Deny list — file-mutation tool names across hosts. Exact-name match.
# Tools NOT in this set (TodoWrite, WebSearch, Read, Glob, Grep, MCP
# tools, etc.) never count as a violation.
_DENY_TOOL_NAMES = frozenset({
    # claude-code / codex
    "edit", "write", "notebookedit", "notebook_edit",
    "multiedit", "multi_edit",
    # cursor
    "edit_file", "create_file", "search_replace",
    "str_replace", "applypatch", "apply_patch", "delete_file",
    # opencode / openclaw / pi / hermes variants
    "file_write", "file_edit",
})


# Shell-execution tool names. The bash-pattern deny check is only run
# when the tool name is in this set.
_BASH_TOOL_NAMES = frozenset({
    "bash", "shell", "exec",
    "run_terminal_cmd", "runterminalcmd",
    "run_command", "terminal",
    "execute_code", "execute",
})


# Per-segment hard-deny patterns: mutating verbs anchored to the start
# of a command segment. After splitting on shell separators, each
# segment's leading verb is checked here. The leading `^\s*` (optional
# `nohup`, optional `/path/to/`) handles `nohup rm -rf …` and
# `/usr/bin/sed -i …`. This anchoring is what prevents `grep -R rm src`
# from being denied because of the substring `rm`. For verbs whose
# mutating form depends on a specific flag (sed/awk/perl/curl), the
# flag is matched via a non-greedy `[^|&;]*?` so option-order variants
# all trip (`sed -E -i …` and `sed -e '...' -i …` both deny).
_SEGMENT_DENY_RE = _re.compile(
    r"^\s*(?:nohup\s+)?(?:\S*/)?"
    r"(?:"
        # tee writing to a file (`cmd | tee /path` after pipe split).
        r"tee\b(?:\s+-[aiu]+)*\s+[^\s|&<>]+"
        # sed/perl in-place editors — `-i` (with optional clustered flags
        # like `-ri` / `-iE`) or `--in-place`. Non-greedy preamble so
        # `sed -E -i …` and `sed -e '...' -i …` both match.
        r"|sed\b[^|&;]*?\s-[a-zA-Z]*i[a-zA-Z]*\b"
        r"|sed\b[^|&;]*?\s--in-place\b"
        r"|perl\b[^|&;]*?\s-[a-zA-Z]*i[a-zA-Z]*\b"
        r"|awk\b[^|&;]*?\s-i\s+inplace\b"
        # File system mutations (verbs that ALWAYS write to disk).
        r"|(?:mv|cp|rm|mkdir|rmdir|touch|chmod|chown|chgrp|ln|rsync)(?:\s|$)"
        # dd writing a file: dd of=… (anywhere in argv).
        r"|dd\b[^|&;]*?\bof="
        # curl writing a file: -o file / -O / --output / --remote-name.
        r"|curl\b[^|&;]*?\s-[a-zA-Z]*[oO][a-zA-Z=]*(?:\s|$)"
        r"|curl\b[^|&;]*?\s--output(?:=|\s)"
        r"|curl\b[^|&;]*?\s--remote-name\b"
        # wget (default behavior writes a file).
        r"|wget(?:\s|$)"
        r"|patch(?:\s|$)"
        r"|install(?:\s|$)"
        r"|truncate(?:\s|$)"
        # git mutating subcommands. `stash` matches bare/push/save/pop/
        # apply/drop/clear/create/store; `stash list` and `stash show`
        # are read-only and excluded via negative lookahead. Allows
        # global options before the subcommand so `git -C path checkout`
        # and `git --git-dir=… reset` are caught (the non-greedy `(?:…)*?`
        # consumes any `-X`/`-X val`/`--long`/`--long=val` repetition).
        r"|git\b(?:\s+(?:-[a-zA-Z]\S*|--[a-z][a-z-]*(?:=\S+)?)(?:\s+\S+)?)*?\s+(?:apply|checkout|restore|reset|clean|switch|merge|rebase|am|stash(?!\s+(?:list|show)\b)|cherry-pick|pull|clone|revert|worktree)\b"
        # Interactive editors (write on save).
        r"|(?:vim|vi|nano|emacs)(?:\s|$)"
    r")"
)


# Redirect patterns: stdout/stderr/fd writes to a file. Subagent-spawn
# idioms legitimately use these for logging (`> /tmp/agent.log 2>&1`),
# so they only fire as a deny outside the host-spawn-prefix exemption.
_REDIRECT_DENY_RE = _re.compile(
    r"(?:"
        # Standalone redirect `cmd > file` / `cmd >> file`. Excludes
        # input `<`, digit-prefix `\d+>`, `&>` (aggregate).
        r"(?<![<\d&])>>?\s*[^\s|&<>;]+"
        # fd-qualified redirect: `2>err.log`, `10>>out.log`.
        # Excludes `2>&1` fd-to-fd duplication.
        r"|\b\d+>>?\s*(?!&)[^\s|&<>;]+"
        # Bash aggregate redirect: `&>file` / `&>>file`.
        r"|&>>?\s*(?!&)[^\s|&<>;]+"
        # Force-clobber redirect: `>|file`
        r"|>\|\s*[^\s|&<>;]+"
    r")"
)


# Host-spawn prefixes — these legitimately include `> /tmp/log 2>&1 &`
# for background subagent logging. The redirect-deny is suppressed for
# clean (no chained, no substitution) invocations starting with these.
_HOST_SPAWN_PREFIX_RE = _re.compile(
    r"^\s*(?:nohup\s+)?"
    r"(?:"
        r"claude(?:\s|$)"
        r"|codex(?:\s|$)"
        r"|cursor-agent(?:\s|$)"
        r"|opencode(?:\s|$)"
        r"|hermes(?:\s|$)"
        r"|openclaw(?:\s|$)"
        r"|pi(?:\s|$)"
        r"|pi-coding-agent(?:\s|$)"
    r")"
)


# evo command prefix. evo subcommands shouldn't legitimately need stdout
# redirects, so even though the prefix is "safe" for not chaining, a
# redirect after it should still fire — orchestrator dumping state to
# a file is the manual-workflow we're trying to discourage.
_EVO_PREFIX_RE = _re.compile(r"^\s*evo(?:\s|$)")


# Command separators that chain a second command. If any appear
# unquoted, the safe-prefix exemption is dropped. Bare `&` (not the
# trailing background marker, not part of `&&` / `>&`).
_UNQUOTED_SEPARATOR_RE = _re.compile(
    r"[;\n]"
    r"|&&|\|\|"
    r"|\|(?!\|)"
    r"|(?<![>&])&(?![&>])(?!\s*$)"
)


def _split_segments(cmd: str) -> list[str]:
    """Split a sanitized shell command on unquoted separators into
    individual command segments. Used so the segment-deny regex can be
    anchored to start-of-segment instead of relying on `\\b` which
    over-matches (`grep -R rm src` would otherwise match `rm`).
    """
    return _UNQUOTED_SEPARATOR_RE.split(cmd)


def _extract_substitution_bodies(seg: str) -> list[str]:
    """Walk `seg` honoring shell quote state, and yield bodies of:
      - `$(...)` command substitution (balanced parens)
      - backtick command substitution
      - `<(...)` / `>(...)` process substitution (balanced parens)

    Single-quoted regions are skipped — single quotes are inert in
    bash and the contents are literal. Double-quoted regions ARE
    scanned because substitution fires inside double quotes.

    This must run on the RAW (pre-inert-strip) command. If we ran it
    after `_strip_inert_quoted`, a body like `sh -c 'rm -rf /tmp/x'`
    embedded in a substitution would have its quoted body nuked to
    `sh -c ''` before we ever saw it.
    """
    bodies: list[str] = []
    i = 0
    n = len(seg)
    state = "default"  # "default" | "sq" | "dq"

    def _find_balanced_paren_close(start: int) -> int:
        """Return index just past `)` for a `(` at `start-1` (i.e.
        `start` is one past the open paren). Tracks nested parens AND
        quote state inside. -1 if unbalanced."""
        depth = 1
        k = start
        inner = "default"
        while k < n and depth > 0:
            cc = seg[k]
            if inner == "sq":
                if cc == "'":
                    inner = "default"
                k += 1
                continue
            if inner == "dq":
                if cc == "\\" and k + 1 < n:
                    k += 2
                    continue
                if cc == '"':
                    inner = "default"
                    k += 1
                    continue
                # Fall through — inside dq, $() still tracked.
            if cc == "\\" and k + 1 < n:
                k += 2
                continue
            if cc == "'" and inner == "default":
                inner = "sq"
            elif cc == '"' and inner == "default":
                inner = "dq"
            elif cc == "(":
                depth += 1
            elif cc == ")":
                depth -= 1
            k += 1
        return k if depth == 0 else -1

    while i < n:
        c = seg[i]
        if state == "sq":
            if c == "'":
                state = "default"
            i += 1
            continue
        if state == "dq":
            if c == "\\" and i + 1 < n:
                i += 2
                continue
            if c == '"':
                state = "default"
                i += 1
                continue
            # Inside dq: $() / backtick still fire. Fall through.
        if c == "\\" and i + 1 < n:
            i += 2
            continue
        if c == "'" and state == "default":
            state = "sq"
            i += 1
            continue
        if c == '"' and state == "default":
            state = "dq"
            i += 1
            continue
        # $(...) command substitution. Skip `$((...))` arithmetic
        # expansion — that body is a math expression, never a shell
        # command, and recursing into `(1 > 0)` would false-positive
        # the redirect-deny on the `>`.
        if c == "$" and i + 1 < n and seg[i + 1] == "(":
            if i + 2 < n and seg[i + 2] == "(":
                # Arithmetic — advance past `$((` so the rest of the
                # walker sees the inner parens as ordinary text.
                i += 3
                continue
            end = _find_balanced_paren_close(i + 2)
            if end != -1:
                bodies.append(seg[i + 2 : end - 1])
                i = end
                continue
        # <(...) and >(...) process substitution. Only at default state;
        # inside double quotes bash treats `"<(cmd)"` as literal text
        # (no subshell). Inside single quotes is also inert.
        if c in ("<", ">") and i + 1 < n and seg[i + 1] == "(" and state == "default":
            end = _find_balanced_paren_close(i + 2)
            if end != -1:
                bodies.append(seg[i + 2 : end - 1])
                i = end
                continue
        # Backtick command substitution (no nesting).
        if c == "`" and state != "sq":
            j = i + 1
            while j < n and seg[j] != "`":
                if seg[j] == "\\" and j + 1 < n:
                    j += 2
                    continue
                j += 1
            if j < n:
                bodies.append(seg[i + 1 : j])
                i = j + 1
                continue
        i += 1
    return bodies


_SHELL_INTERPRETERS = frozenset({"bash", "sh", "zsh", "dash", "ash"})


def _unwrap_shell_c_arguments(cmd: str) -> str:
    """If `cmd` contains `bash -c "..."` / `sh -c '...'` etc., append
    the quoted argument body to the command so the deny scan inspects
    its contents. Uses shlex to handle every shell option form
    (`-o pipefail -c '…'`, `-eo pipefail -c '…'`, `--login -c '…'`,
    `-ic '…'`, etc.). On parse failure (unbalanced quoting), returns
    `cmd` unchanged.
    """
    import shlex
    try:
        tokens = shlex.split(cmd, posix=True)
    except ValueError:
        return cmd
    if not tokens:
        return cmd
    appended: list[str] = []
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        name = tok.rstrip("/").rsplit("/", 1)[-1]
        if name in _SHELL_INTERPRETERS:
            j = i + 1
            while j < len(tokens):
                t = tokens[j]
                if t == "-c":
                    if j + 1 < len(tokens):
                        appended.append(tokens[j + 1])
                    break
                # Combined short-opt block containing `c` (e.g. `-ic`,
                # `-lc`, `-ce`, `-ceu`): the next token is the script.
                if (
                    t.startswith("-")
                    and not t.startswith("--")
                    and len(t) > 1
                    and "c" in t[1:]
                ):
                    if j + 1 < len(tokens):
                        appended.append(tokens[j + 1])
                    break
                j += 1
        i += 1
    if not appended:
        return cmd
    return cmd + " ; " + " ; ".join(appended)


def _strip_inert_quoted(cmd: str) -> str:
    """Remove single- and double-quoted substrings whose contents are
    inert (no command substitution). Used to kill `echo "x > y"` false
    positives without opening a `echo "$(rm)"` bypass.

    Single-quoted strings: always inert (no expansion inside bash).
    Double-quoted strings: stripped only if they don't contain `$(` or
    backticks (command substitution still fires inside double quotes).

    Also erases `$((...))` arithmetic regions — the body is a math
    expression, never a shell command, and `>` / `<` inside arithmetic
    are comparison operators, not redirects. This prevents the segment
    scan from FP'ing on `echo "$((1 > 0))"`.
    """
    cmd = _re.sub(r"'[^']*'", "''", cmd)

    def _replace_dq(match: _re.Match) -> str:
        body = match.group(0)
        if "$(" in body or "`" in body:
            return body
        return '""'

    cmd = _re.sub(r'"(?:[^"\\]|\\.)*"', _replace_dq, cmd)

    # Erase $((...)) arithmetic. Walk balanced parens — regex can't
    # easily handle nesting (e.g. `$(($(date +%s) > 0))`).
    out: list[str] = []
    i = 0
    n = len(cmd)
    while i < n:
        if (
            cmd[i] == "$"
            and i + 2 < n
            and cmd[i + 1] == "("
            and cmd[i + 2] == "("
        ):
            depth = 2
            j = i + 3
            while j < n and depth > 0:
                if cmd[j] == "(":
                    depth += 1
                elif cmd[j] == ")":
                    depth -= 1
                j += 1
            if depth == 0:
                i = j
                continue
        out.append(cmd[i])
        i += 1
    return "".join(out)


def _is_denied_in_optimize_mode(
    tool_name: str | None,
    tool_input: dict | None,
) -> bool:
    """Return True if this tool call is on the optimize-mode deny list.

    Either:
      - tool_name is a known file-mutation tool, OR
      - tool_name is shell-execution AND, after sanitization + segment
        split, any segment matches a hard-deny pattern, OR a non-exempt
        segment contains a file redirect, OR a command substitution
        body recursively denies.

    Safe-prefix rules (per-segment):
      - Host-spawn prefixes (`claude`, `codex`, …) exempt the redirect
        check so `nohup claude --print 'x' > /tmp/log 2>&1 &` passes.
      - `evo` prefix does NOT exempt redirects — orchestrator dumping
        state to a file is a manual-workflow stray.
      - Command substitution (`$(…)` / backticks) is recursively scanned
        as a nested bash command; only mutations inside trip a deny,
        so `echo "$(pwd)"` passes but `echo "$(rm -rf /tmp/x)"` denies.
      - Chained commands (`evo … ; rm`) are scanned segment-by-segment;
        each segment's exemption is independent.
    """
    if not tool_name:
        return False
    t = tool_name.lower()
    if t in _DENY_TOOL_NAMES:
        return True
    if t not in _BASH_TOOL_NAMES:
        return False

    cmd = (tool_input or {}).get("command", "") if isinstance(tool_input, dict) else ""
    if not isinstance(cmd, str) or not cmd:
        return False

    # Unwrap `bash -c "..."` so its inner script body is scannable.
    prepared = _unwrap_shell_c_arguments(cmd)

    # Substitution recursion runs on the RAW prepared command. If we
    # ran it after `_strip_inert_quoted`, a body like
    # `sh -c 'rm -rf /tmp/x'` embedded in a substitution would have its
    # quoted argument body nuked to `sh -c ''` before recursion.
    for body in _extract_substitution_bodies(prepared):
        if _is_denied_in_optimize_mode("Bash", {"command": body}):
            return True

    # Now sanitize for the outer scan: strip inert single/double quoted
    # regions to kill `echo "x > y"` false positives. Substitution
    # markers in `'$(rm)'` (single quotes) reduce to `''` — fine, since
    # single-quoted text is literal and never executed.
    sanitized = _strip_inert_quoted(prepared)

    # Per-segment scan.
    for segment in _split_segments(sanitized):
        seg = segment.strip()
        if not seg:
            continue

        # Hard-deny verbs anchored to segment start.
        if _SEGMENT_DENY_RE.match(seg):
            return True

        # Host-spawn segments may redirect for logging; evo + others
        # cannot. Plain commands (pytest, python bench.py, ls, git log)
        # with no mutating verb and no redirect pass through.
        if _HOST_SPAWN_PREFIX_RE.match(seg):
            continue
        if _REDIRECT_DENY_RE.search(seg):
            return True

    return False


_POLICY_NUDGE_TEMPLATE = """[EVO POLICY]
Preventative block. You may have strayed from /evo:optimize protocol.

When /optimize is active, the orchestrator should NOT edit files directly
or run experiment commands by hand. The protocol is: write briefs, spawn
subagents, let them do the edits and runs, then read the results.

If you need to wait for subagents to finish, use `evo wait` — it blocks
until any experiment concludes, max 1h.

Manual edits and shell commands that mutate files are discouraged unless
evo doesn't already expose the operation. If this block was actually
warranted (rare), run `evo exit-optimize-mode` to disable the safety
nudges, then retry the tool.
[END EVO POLICY]
"""


def _policy_state_file(root: Path, session_id: str) -> Path:
    """Per-session policy bookkeeping: violation_count."""
    return inject_root(root) / "policy_state" / f"{session_id}.json"


def _read_policy_state(root: Path, session_id: str) -> dict:
    p = _policy_state_file(root, session_id)
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text())
    except (OSError, ValueError):
        return {}


def _write_policy_state(root: Path, session_id: str, data: dict) -> None:
    p = _policy_state_file(root, session_id)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data))


_PRE_TOOL_EVENTS = ("PreToolUse", "preToolUse")


def _should_policy_block(
    root: Path, session_id: str, sess: dict | None, host: str | None,
    hook_event: str | None, payload: dict | None,
) -> bool:
    """Decide whether to hard-deny the current tool call as a policy
    violation. Increments the per-session violation counter; returns
    True on every odd-numbered violation (1, 3, 5, …).

    Conditions for considering a tool call a violation:
      - hook_event is the host's pre-tool event name. claude-code /
        codex send `PreToolUse`; cursor sends `preToolUse` (camelCase).
        Both forms accepted via `_PRE_TOOL_EVENTS`.
      - session is orchestrator-class (no exp_id).
      - optimize_mode is true on the session record.
      - tool is on the deny list (edit-tools, or bash with a mutating
        command pattern outside the safe-prefix exemption).
    """
    if hook_event not in _PRE_TOOL_EVENTS:
        return False
    if not sess:
        return False
    if sess.get("exp_id"):
        return False  # subagent — exempt
    if not sess.get("optimize_mode"):
        return False
    if not payload:
        return False

    tool_name = payload.get("tool_name") or ""
    tool_input = payload.get("tool_input") or {}

    if not _is_denied_in_optimize_mode(tool_name, tool_input):
        return False

    # Alternating cadence: block on odd-numbered violations (1, 3, 5, …),
    # let even-numbered ones through. The agent gets a nudge, then a
    # try-again chance, then a nudge again — gives it room to either
    # comply or genuinely override (e.g. by invoking `evo
    # exit-optimize-mode`).
    state = _read_policy_state(root, session_id)
    count = int(state.get("violation_count", 0)) + 1
    state["violation_count"] = count
    state["last_violation_tool"] = tool_name
    _write_policy_state(root, session_id, state)
    return count % 2 == 1


def _policy_block_envelope(host: str | None) -> dict:
    """Per-host hard-deny envelope.

    - claude-code / codex: `{permission: deny, reason: …}` — the standard
      Claude Code preToolUse contract; both honor `reason` as the
      model-facing explanation.
    - cursor: `{permission: deny, agent_message: …}` — verified against
      the Cursor hooks contract (`~/.cursor/skills-cursor/create-hook/
      SKILL.md`): preToolUse output fields are `permission`,
      `user_message`, `agent_message`, `updated_input`. `reason` is NOT
      a documented field and was being silently dropped by cursor.
    """
    if host == "cursor":
        return {"permission": "deny", "agent_message": _POLICY_NUDGE_TEMPLATE}
    return {"permission": "deny", "reason": _POLICY_NUDGE_TEMPLATE}


# ---------------------------------------------------------------------------
# Stop-hook self-continuation nudge for /optimize orchestrators
# ---------------------------------------------------------------------------

_STOP_EVENTS = ("Stop", "SubagentStop", "stop", "subagentStop")


_STOP_NUDGE_TEMPLATE = """[EVO LOOP]
You are driving /evo:optimize autonomously. Don't stop, and don't ask
the user for direction — they explicitly invoked /optimize for hands-off
operation. They'll intervene via `evo direct` if they need to.

If subagents are still running: run `evo wait` to block until the next
experiment concludes (or up to 1h). Then resume planning.

If subagents are done and you have unread results: read `evo scratchpad`,
update annotations as needed, and plan + spawn the next round.

Stop only if `evo status` shows the budget exhausted or you've hit the
stall limit. If so, print a final summary first. To suppress this
continuation loop for a legitimate one-off task, run
`evo exit-optimize-mode`.
[END EVO LOOP]
"""


def _maybe_stop_nudge_text(
    root: Path,
    session_id: str,
    sess: dict | None,
    host: str | None,
    hook_event: str | None,
) -> str | None:
    """Return continuation-prompt text if this Stop/SubagentStop should
    re-prompt the agent, or None to let the agent stop normally.

    Always fires when conditions hold. The user explicitly invoked
    /optimize for autonomous operation — letting the agent stop on its
    own defeats the point. The escape hatch is `evo exit-optimize-mode`.

    Conditions:
      - hook event must be Stop or SubagentStop (case-insensitive
        across hosts — claude-code/codex use PascalCase, cursor uses
        camelCase).
      - session must be orchestrator-class (no exp_id).
      - optimize_mode must be true on the session record.
      - host must have a working stop-continuation envelope:
        claude-code/codex use `{decision: "block", reason: …}`; cursor
        uses `{followup_message: …}` (auto-submitted at turn end).
        emit_for_host dispatches the right shape per host.
    """
    if hook_event not in _STOP_EVENTS:
        return None
    if not sess:
        return None
    if sess.get("exp_id"):
        return None  # subagent — not our session to force-continue
    if not sess.get("optimize_mode"):
        return None
    if host not in ("claude-code", "codex", "cursor"):
        return None  # no known stop-continuation envelope on this host
    return _STOP_NUDGE_TEMPLATE


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
        # Detect /evo:optimize invocation from the prompt payload, before
        # we drain. Idempotent + subagent-safe inside mark_optimize_mode.
        if args.session and args.host:
            _maybe_mark_optimize_from_prompt(
                root, args.session, args.host, hook_event, payload,
            )
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
    tool_name = payload.get("tool_name")
    tool_input = payload.get("tool_input") or {}
    registered = session_file(root, session).exists()
    has_marker = marker.exists(root, session)
    # Cursor has no session-id env var that Python `auto_register_from_env`
    # can detect, so the engagement signal can't be set via the CLI path
    # used by other hosts. Detect it here instead: if the agent runs any
    # `evo …` shell command, flip engagement on this session's record.
    _maybe_mark_engaged_from_shell(root, session, host, hook_event, payload)
    gate = _self_contained_gate(root, session, host, hook_event, tool_name, tool_input)
    # Detect /optimize invocation from the prompt payload for cursor's
    # self-contained path. Must run AFTER the gate because the gate is
    # what lazily registers the cursor session on first event (without
    # registration, mark_optimize_mode finds no session file). The
    # mark_optimize_mode helper is idempotent + refuses to flag subagents.
    _maybe_mark_optimize_from_prompt(root, session, host, hook_event, payload)
    _drain_debug(stage="gate", host=host, hook_event=hook_event, session=session,
                 root=str(root), tool_name=tool_name,
                 tool_class=_cursor_tool_class(tool_name) if hook_event == "preToolUse" else None,
                 registered_before=registered, marker=has_marker, gate=gate)
    if not gate:
        sys.stdout.write("{}")
        return 0
    return drain_session(root, session, host=host, hook_event=hook_event, payload=payload)


if __name__ == "__main__":
    sys.exit(main())

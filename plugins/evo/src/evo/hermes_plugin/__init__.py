"""Hermes runtime plugin — auto-discovered via pip entry-point
`hermes_agent.plugins`.

Hooks registered:

- `on_session_start`: registers the hermes session in evo's inject
  registry.
- `pre_llm_call`: per-turn drain of pending `evo direct` events. The
  only hook whose return value hermes honors as `{"context": "..."}`
  (appended to the current turn's user message — see
  hermes-agent run_agent.py:721-740).
- `pre_tool_call`: synchronous deny gate. Returning
  `{"action": "block", "message": ...}` short-circuits the tool and
  feeds `message` back to the model as the tool error (see
  hermes_cli/plugins.py:88-90 + model_tools.py:60-62). Alternating
  cadence — block on odd violations (1, 3, 5, …), pass on even.
- `on_session_end`: stop nudge. Calls `ctx.inject_message(...)` to
  enqueue a follow-up user turn (hermes_cli/plugins.py:359-383). The
  background process_loop picks it up and starts another `chat()`
  iteration — the same mechanism `/goal` uses internally
  (cli.py:9126-9240).

Gateway-mode caveat: `ctx.inject_message` requires a `_cli_ref`. In
gateway mode it returns False and logs "no CLI reference"; the
stop-nudge is a no-op there. Deny gate is unaffected.

Session-id handling: `pre_tool_call` receives `task_id`, not
`session_id`. We stash the session id from `on_session_start` /
`pre_llm_call` so `pre_tool_call` can resolve the right session
record.

See notes/cross-host-inject-design.md.
"""

from __future__ import annotations

from pathlib import Path

from evo.core import repo_root
from evo.inject import marker
from evo.inject.paths import inject_root, exp_events_path, workspace_events_path
from evo.inject.queue import read_events_after, read_offset, write_offset
from evo.inject.registry import get_session, mark_engaged, register_session
from evo.inject.drain import (
    _POLICY_NUDGE_TEMPLATE,
    _STOP_NUDGE_TEMPLATE,
    _is_denied_in_optimize_mode,
    _maybe_mark_optimize_from_prompt,
    _read_policy_state,
    _write_policy_state,
    format_directive_text,
)


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
    """Register the hermes session if not already in the registry, and
    mark it as evo-engaged.

    The hermes plugin process IS the orchestrator — there is no separate
    `evo`-command engagement signal (under /optimize the orchestrator
    dispatches every evo command to subagents, so `auto_register_from_env`
    never flips engagement). Without engaging here, `evo direct` filters
    this session out (skipped_unengaged) and mid-run directives never
    reach hermes. Hermes uses no Rust binary, so this is its only
    engagement path. `register_session(engage=True)` engages a fresh
    record; `mark_engaged` covers an existing record that registered
    before this fix. Both refuse to engage a subagent (exp_id-bearing)
    record. On the false→true transition the offset is seeded to the
    queue tail so pre-engagement directives don't replay."""
    if get_session(root, session_id) is None:
        register_session(root, session_id, "hermes", engage=True)
        return
    from evo.inject.queue import init_offset_to_latest
    if mark_engaged(root, session_id):
        init_offset_to_latest(root, session_id)


def _compute_drain_text(root: Path, session_id: str) -> str | None:
    """Read pending events for `session_id`, format, update offset, unlink
    marker. Returns the formatted text or None if nothing to deliver."""
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


# Stash the most-recent session_id from on_session_start / pre_llm_call
# so `pre_tool_call` (which gets `task_id`, not `session_id`, and which
# may not match the evo registry session id) can resolve the right
# session record.
_LAST_SESSION_ID: str | None = None


def _stash_session(session_id: str | None) -> None:
    global _LAST_SESSION_ID
    if session_id:
        _LAST_SESSION_ID = session_id


def _record_violation_and_should_block(
    root: Path, session_id: str, tool_name: str | None,
) -> bool:
    """Increment the per-session violation counter; return True on every
    odd-numbered violation (1, 3, 5, …). Mirrors `_should_policy_block`
    in drain.py — same cadence and state shape across hosts.
    """
    state = _read_policy_state(root, session_id)
    count = int(state.get("violation_count", 0)) + 1
    state["violation_count"] = count
    state["last_violation_tool"] = tool_name or ""
    _write_policy_state(root, session_id, state)
    return count % 2 == 1


def _on_session_start(session_id: str | None = None, **kwargs):
    """Register the session. No drain — hermes ignores this hook's
    return value; pre_llm_call is the only context-injection point."""
    if not session_id:
        return None
    _stash_session(session_id)
    root = _resolve_root()
    if root is None:
        return None
    _ensure_registered(root, session_id)
    return None


def _on_pre_llm_call(session_id: str | None = None, **kwargs):
    """Per-turn drain of pending `evo direct` events.

    Delivery: appended to the current turn's user message as `{"context":
    ...}`. Considered switching to `ctx.inject_message(role="user")` for
    a fresh user-role turn — and that's the right path for interactive
    `hermes chat` — but `inject_message` queues to `cli._pending_input`,
    which only the interactive process_loop drains. The release-smoke
    test drives hermes with `chat -q -Q` (single-shot non-interactive)
    where `process_loop` isn't running, so a queued inject_message
    never reaches the model. Context-append keeps the directive
    visible to the current turn for the single-shot case.

    Also auto-arms `optimize_mode` if the user's prompt looks like
    `/optimize`. Hermes has no UserPromptSubmit hook; pre_llm_call is
    the per-turn analogue.
    """
    if not session_id:
        return None
    _stash_session(session_id)
    root = _resolve_root()
    if root is None:
        return None
    _ensure_registered(root, session_id)
    _maybe_mark_optimize_from_prompt(
        root, session_id, "hermes", "userPromptSubmit", kwargs,
    )

    directive_text = _compute_drain_text(root, session_id)
    if not directive_text:
        return None
    return {"context": f"--- evo directive ---\n\n{directive_text}"}


def _on_pre_tool_call(
    tool_name: str | None = None,
    args: dict | None = None,
    task_id: str | None = None,
    **kwargs,
):
    """Synchronous deny gate. On odd-numbered violations, return
    `{"action": "block", "message": ...}` — hermes short-circuits the
    tool and feeds the message back to the model as the tool error
    (hermes_cli/plugins.py:88-90, model_tools.py:60-62).
    """
    session_id = kwargs.get("session_id") or _LAST_SESSION_ID
    if not session_id:
        return None
    root = _resolve_root()
    if root is None:
        return None
    sess = get_session(root, session_id)
    if not sess:
        return None
    if sess.get("exp_id"):
        return None  # subagent — exempt
    if not sess.get("optimize_mode"):
        return None
    if not _is_denied_in_optimize_mode(tool_name, args):
        return None
    if _record_violation_and_should_block(root, session_id, tool_name):
        return {"action": "block", "message": _POLICY_NUDGE_TEMPLATE}
    return None


# Plugin context, stashed at register() so `on_session_end` can call
# `ctx.inject_message(...)` to push a follow-up user message back into
# the CLI's `_pending_input` queue.
_PLUGIN_CTX = None


def _on_session_end(session_id: str | None = None, **kwargs):
    """Stop nudge — enqueue a follow-up user turn so the `/optimize`
    loop keeps running. CLI mode only (gateway has no `_cli_ref`).
    """
    if not session_id or _PLUGIN_CTX is None:
        return None
    root = _resolve_root()
    if root is None:
        return None
    sess = get_session(root, session_id)
    if not sess:
        return None
    if sess.get("exp_id"):
        return None  # subagent — not our session to force-continue
    if not sess.get("optimize_mode"):
        return None
    try:
        _PLUGIN_CTX.inject_message(_STOP_NUDGE_TEMPLATE, role="user")
    except Exception:
        pass
    return None


def register(ctx) -> None:
    global _PLUGIN_CTX
    _PLUGIN_CTX = ctx
    ctx.register_hook("on_session_start", _on_session_start)
    ctx.register_hook("pre_llm_call", _on_pre_llm_call)
    ctx.register_hook("pre_tool_call", _on_pre_tool_call)
    ctx.register_hook("on_session_end", _on_session_end)

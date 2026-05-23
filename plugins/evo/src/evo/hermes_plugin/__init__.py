"""Hermes runtime plugin — auto-discovered via pip entry-point
`hermes_agent.plugins`.

KNOWN LIMITATIONS (hermes hook contract, not bugs in this plugin):

  1. Cannot block denied tool calls. Hermes's `pre_tool_call` is
     observer-only — return value is ignored per upstream docs
     ("Use cases: ... blocking dangerous operations (print a warning)").
     We record the violation and inject the policy banner on the NEXT
     pre_llm_call. The agent self-corrects on its next iteration, but
     the offending tool has already executed by then. On claude-code /
     codex / cursor / opencode / openclaw / pi the gate is synchronous;
     here it is reactive. Same `optimize_mode` + alternating cadence
     gating; weaker enforcement.

  2. No always-fire stop nudge. Hermes has no turn-end hook whose
     return value can force a new turn. `post_llm_call` exists but its
     return is ignored. If the agent stops, nothing here continues it.
     For `hermes chat -q ... -Q` batch mode this is a hard limit; for
     interactive sessions the user's next prompt resumes naturally.

  Both limitations are upstream API constraints. They're documented
  here so that a session starting with `/optimize` on hermes is known
  to operate under weaker steering than on the other hosts.

Hooks registered (verified against hermes-agent 0.10's `VALID_HOOKS`):

- `on_session_start`: registers the hermes session in evo's inject
  registry. Return value is ignored by hermes.
- `pre_llm_call`: drains pending events at turn start AND injects the
  policy nudge banner if violations were recorded since the last turn.
  This is the only hook whose return value hermes honors (as
  `{"context": "..."}`).
- `pre_tool_call`: observer-only on hermes (return ignored). Records
  optimize-mode violations to a state file. The nudge is delivered
  the next time `pre_llm_call` fires.

Why not mid-turn delivery: hermes has no hook that can mutate model
input mid-tool-loop. `pre_tool_call` is observer-only; `post_tool_call`
return is ignored. So directives queued during a turn are delivered at
the NEXT turn's `pre_llm_call`. Single-turn (`hermes chat -q ... -Q`)
runs have no next turn, so mid-turn-queued directives wait until the
next `hermes chat` invocation. (This was misrepresented in the previous
plugin which registered a `transform_tool_result` hook hermes doesn't
fire — verified absent from `VALID_HOOKS` in hermes-agent 0.10.)

Session-id handling: `pre_tool_call` receives `task_id` (the hermes
session identifier), not `session_id`. We stash the session id from
`on_session_start` / `pre_llm_call` so `pre_tool_call` can resolve which
session record to update. Single hermes process = single active session
at a time, so this is safe; for subagents that fork their own session,
the parent's drain remains correct because `pre_tool_call` only fires
for the current agent's tools.

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
from evo.inject.drain import (
    _POLICY_NUDGE_TEMPLATE,
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
    """Register the hermes session if not already in the registry."""
    if get_session(root, session_id) is None:
        register_session(root, session_id, "hermes")


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


def _record_violation(root: Path, session_id: str, tool_name: str | None) -> None:
    """Increment the per-session violation counter and mark a pending
    nudge. Called from `pre_tool_call` for denied tools under optimize_mode.

    Storage: piggybacks on the same `_policy_state_file` used by
    claude-code/cursor/codex so cadence and counter shape stay
    consistent across hosts.
    """
    state = _read_policy_state(root, session_id)
    count = int(state.get("violation_count", 0)) + 1
    state["violation_count"] = count
    state["last_violation_tool"] = tool_name or ""
    state["nudge_pending"] = True
    _write_policy_state(root, session_id, state)


def _consume_pending_nudge(root: Path, session_id: str) -> str | None:
    """At `pre_llm_call`, return the policy nudge banner IF a violation
    is pending AND the current count satisfies the alternating cadence
    (odd-numbered violations nudge; even ones pass silently). Resets
    `nudge_pending` after.
    """
    state = _read_policy_state(root, session_id)
    if not state.get("nudge_pending"):
        return None
    count = int(state.get("violation_count", 0))
    state["nudge_pending"] = False
    _write_policy_state(root, session_id, state)
    if count % 2 == 1:
        return _POLICY_NUDGE_TEMPLATE
    return None


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
    """Per-turn drain + policy nudge delivery.

    Two channels combined into the `{"context": ...}` injection:
      1. Policy nudge banner — if `pre_tool_call` recorded a denied-tool
         violation since the last pre_llm_call AND the count satisfies
         the alternating cadence, prepend the EVO POLICY banner.
      2. Pending directives (`evo direct`) — appended.

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
    # Auto-arm optimize_mode on /optimize. Synthetic event name so the
    # shared matcher (which gates on hook_event ∈ prompt_events) accepts.
    _maybe_mark_optimize_from_prompt(
        root, session_id, "hermes", "userPromptSubmit", kwargs,
    )

    nudge = _consume_pending_nudge(root, session_id)
    directive_text = _compute_drain_text(root, session_id)

    if not nudge and not directive_text:
        return None
    # Banner first so it stays visible even if context is truncated.
    parts: list[str] = []
    if nudge:
        parts.append("--- evo policy ---")
        parts.append(nudge)
    if directive_text:
        parts.append("--- evo directive ---")
        parts.append(directive_text)
    return {"context": "\n\n".join(parts)}


def _on_pre_tool_call(
    tool_name: str | None = None,
    args: dict | None = None,
    task_id: str | None = None,
    **kwargs,
):
    """Observer for tool calls. Hermes ignores the return value, so we
    can't block here — we record the violation so the next `pre_llm_call`
    can deliver the nudge.

    Fires once per tool execution (verified against hermes-agent
    `model_tools.py#handle_function_call`). Parallel tool calls fire it
    N times.
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
    _record_violation(root, session_id, tool_name)
    return None


def register(ctx) -> None:
    """Hermes plugin entry point — invoked once at plugin load.

    Only hooks listed in hermes-agent's `VALID_HOOKS` are registered.
    `transform_tool_result` (used by older evo versions) is NOT a real
    hermes hook and was silently dropped — confirmed against
    hermes-agent 0.10's `hermes_cli/plugins.py`.
    """
    ctx.register_hook("on_session_start", _on_session_start)
    ctx.register_hook("pre_llm_call", _on_pre_llm_call)
    ctx.register_hook("pre_tool_call", _on_pre_tool_call)

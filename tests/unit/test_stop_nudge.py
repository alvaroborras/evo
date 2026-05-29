"""Tests for the stop-hook self-continuation nudge.

When the orchestrator (in /optimize) emits a `Stop` (or `SubagentStop`)
event, the drain injects a `{"decision": "block", "reason": ...}`
envelope that re-prompts the agent to keep going — using `evo wait` to
block on subagent results or continuing to plan the next round.

Always-fire: the user explicitly invoked /optimize for autonomous
operation. There is no progress gate; the escape hatch is
`evo exit-optimize-mode`.

No mocks of real impls. Tests drive `drain_session` directly with
synthesized payloads + real on-disk state.

Run: pytest tests/unit/test_stop_nudge.py -v
"""

from __future__ import annotations

import io
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "plugins" / "evo" / "src"))

from evo.inject.registry import (
    register_session, mark_engaged, mark_optimize_mode, mark_autonomous,
)


def _init_git_repo(root: Path) -> None:
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.email", "t@evo"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.name", "T"], cwd=root, check=True)
    subprocess.run(["git", "config", "commit.gpgsign", "false"], cwd=root, check=True)
    (root / "README.md").write_text("x\n")
    subprocess.run(["git", "add", "."], cwd=root, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=root, check=True)


def _make_workspace(root: Path) -> Path:
    _init_git_repo(root)
    from evo.core import init_workspace
    init_workspace(
        root, target="agent.py", benchmark="python bench.py",
        metric="max", gate=None,
    )
    run_dir = next(iter((root / ".evo").glob("run_*")))
    (run_dir / "experiments").mkdir(parents=True, exist_ok=True)
    return run_dir


def _drain_stop(root: Path, sid: str, host: str = "claude-code",
                hook_event: str = "Stop") -> dict:
    """Drive drain_session with a Stop event; return the parsed emitted JSON."""
    from evo.inject.drain import drain_session
    buf = io.StringIO()
    with patch("sys.stdout", buf):
        drain_session(root, sid, host=host, hook_event=hook_event)
    out = buf.getvalue().strip()
    return json.loads(out) if out else {}


class _Base(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name).resolve()
        self.run_dir = _make_workspace(self.root)

    def tearDown(self):
        self._tmp.cleanup()


# ---------------------------------------------------------------------------
# Positive: orchestrator + optimize_mode + Stop → block with continuation
# ---------------------------------------------------------------------------

class TestStopNudgeFires(_Base):

    def test_orchestrator_in_optimize_mode_is_nudged_on_stop(self):
        register_session(self.root, "orch", "claude-code")
        mark_engaged(self.root, "orch")
        mark_optimize_mode(self.root, "orch")
        mark_autonomous(self.root, "orch")

        out = _drain_stop(self.root, "orch", hook_event="Stop")
        self.assertEqual(out.get("decision"), "block",
                         f"Stop nudge must emit decision:block; got {out!r}")
        reason = out.get("reason") or ""
        self.assertIn("optimize", reason.lower(),
                      f"reason must mention optimize protocol; got {reason!r}")
        self.assertIn("evo wait", reason.lower(),
                      "reason must mention evo wait as the wait primitive")

    def test_orchestrator_in_optimize_mode_is_nudged_on_subagent_stop(self):
        """SubagentStop on the orchestrator session (when its Task subagent
        ends) should also keep the orchestrator going."""
        register_session(self.root, "orch", "claude-code")
        mark_engaged(self.root, "orch")
        mark_optimize_mode(self.root, "orch")
        mark_autonomous(self.root, "orch")

        out = _drain_stop(self.root, "orch", hook_event="SubagentStop")
        self.assertEqual(out.get("decision"), "block")
        self.assertIn("optimize", (out.get("reason") or "").lower())

    def test_codex_orchestrator_also_nudged(self):
        register_session(self.root, "cdx", "codex")
        mark_engaged(self.root, "cdx")
        mark_optimize_mode(self.root, "cdx")
        mark_autonomous(self.root, "cdx")
        out = _drain_stop(self.root, "cdx", host="codex", hook_event="Stop")
        self.assertEqual(out.get("decision"), "block")


# ---------------------------------------------------------------------------
# Always-fire: no progress gate. Repeated Stops keep nudging until the
# session leaves optimize_mode (via `evo exit-optimize-mode`).
# ---------------------------------------------------------------------------

class TestStopNudgeAlwaysFires(_Base):

    def test_two_consecutive_stops_both_nudge(self):
        register_session(self.root, "orch", "claude-code")
        mark_engaged(self.root, "orch")
        mark_optimize_mode(self.root, "orch")
        mark_autonomous(self.root, "orch")

        first = _drain_stop(self.root, "orch", hook_event="Stop")
        self.assertEqual(first.get("decision"), "block", "first Stop nudges")

        second = _drain_stop(self.root, "orch", hook_event="Stop")
        self.assertEqual(
            second.get("decision"), "block",
            "second Stop must also nudge — always-fire while optimize_mode is on"
        )

    def test_many_consecutive_stops_all_nudge(self):
        register_session(self.root, "orch", "claude-code")
        mark_engaged(self.root, "orch")
        mark_optimize_mode(self.root, "orch")
        mark_autonomous(self.root, "orch")

        for i in range(5):
            out = _drain_stop(self.root, "orch", hook_event="Stop")
            self.assertEqual(
                out.get("decision"), "block",
                f"Stop #{i + 1} must nudge while optimize_mode is on"
            )

    def test_exit_optimize_mode_stops_nudging(self):
        """Once `evo exit-optimize-mode` flips the flag off, Stop no longer
        forces continuation."""
        from evo.inject.registry import unmark_optimize_mode

        register_session(self.root, "orch", "claude-code")
        mark_engaged(self.root, "orch")
        mark_optimize_mode(self.root, "orch")
        mark_autonomous(self.root, "orch")
        first = _drain_stop(self.root, "orch", hook_event="Stop")
        self.assertEqual(first.get("decision"), "block")

        unmark_optimize_mode(self.root, "orch")

        out = _drain_stop(self.root, "orch", hook_event="Stop")
        self.assertNotEqual(
            out.get("decision"), "block",
            "after exit-optimize-mode, Stop must let the agent actually stop"
        )


# ---------------------------------------------------------------------------
# Negative: don't nudge in cases where we shouldn't
# ---------------------------------------------------------------------------

class TestStopNudgeDoesNotFire(_Base):

    def test_non_optimize_mode_session_not_nudged(self):
        register_session(self.root, "other", "claude-code")
        mark_engaged(self.root, "other")
        # optimize_mode NOT set
        out = _drain_stop(self.root, "other", hook_event="Stop")
        self.assertNotEqual(out.get("decision"), "block",
                            "non-optimize-mode session must not be force-continued")

    def test_optimize_mode_without_autonomous_not_nudged(self):
        """Default /optimize (optimize_mode on, autonomous NOT armed) must
        let the agent stop naturally — the stop-nudge is opt-in via
        `evo autonomous on`."""
        register_session(self.root, "orch", "claude-code")
        mark_engaged(self.root, "orch")
        mark_optimize_mode(self.root, "orch")
        # autonomous NOT armed
        out = _drain_stop(self.root, "orch", hook_event="Stop")
        self.assertNotEqual(
            out.get("decision"), "block",
            "optimize_mode without autonomous must stop naturally (opt-in nudge)"
        )

    def test_subagent_not_nudged(self):
        """Subagents must NOT be force-continued. They have their own
        bounded scope; the orchestrator owns the loop. Forces
        optimize_mode true on the subagent record (by direct write —
        mark_optimize_mode refuses subagents) to verify the exp_id
        guard is the actual blocker, independent of optimize_mode."""
        register_session(self.root, "sub", "claude-code", exp_id="exp_0042")
        mark_engaged(self.root, "sub")
        # Bypass mark_optimize_mode (which exempts subagents) and force
        # optimize_mode=true directly. Now BOTH guards would have to be
        # honored — the exp_id one is what catches it.
        from evo.inject.paths import session_file
        sf = session_file(self.root, "sub")
        data = json.loads(sf.read_text())
        data["optimize_mode"] = True
        data["optimize_mode_at"] = "2026-01-01T00:00:00Z"
        sf.write_text(json.dumps(data))

        out = _drain_stop(self.root, "sub", hook_event="SubagentStop")
        self.assertNotEqual(out.get("decision"), "block",
                            "subagent must not be force-continued even with optimize_mode flipped on")

    def test_pre_tool_use_event_not_treated_as_stop(self):
        """The stop-nudge should ONLY fire on Stop/SubagentStop. A PreToolUse
        on the same session must emit the normal envelope (or nothing)."""
        register_session(self.root, "orch", "claude-code")
        mark_engaged(self.root, "orch")
        mark_optimize_mode(self.root, "orch")
        mark_autonomous(self.root, "orch")
        out = _drain_stop(self.root, "orch", hook_event="PreToolUse")
        self.assertNotEqual(out.get("decision"), "block",
                            "PreToolUse must not trigger stop-nudge")


# ---------------------------------------------------------------------------
# Reason content: autonomous-driving language + escape hatch
# ---------------------------------------------------------------------------

class TestStopNudgeReasonContent(_Base):

    def test_reason_signals_autonomy(self):
        register_session(self.root, "orch", "claude-code")
        mark_engaged(self.root, "orch")
        mark_optimize_mode(self.root, "orch")
        mark_autonomous(self.root, "orch")
        out = _drain_stop(self.root, "orch", hook_event="Stop")
        reason = (out.get("reason") or "").lower()
        self.assertTrue(
            "autonom" in reason or "hands-off" in reason or "user" in reason,
            f"reason should signal autonomous driving; got: {reason!r}"
        )

    def test_reason_mentions_evo_wait(self):
        register_session(self.root, "orch", "claude-code")
        mark_engaged(self.root, "orch")
        mark_optimize_mode(self.root, "orch")
        mark_autonomous(self.root, "orch")
        out = _drain_stop(self.root, "orch", hook_event="Stop")
        self.assertIn("evo wait", (out.get("reason") or "").lower())

    def test_reason_mentions_escape_hatch(self):
        """The continuation prompt must surface `evo exit-optimize-mode` so
        the agent knows how to legitimately end the loop."""
        register_session(self.root, "orch", "claude-code")
        mark_engaged(self.root, "orch")
        mark_optimize_mode(self.root, "orch")
        mark_autonomous(self.root, "orch")
        out = _drain_stop(self.root, "orch", hook_event="Stop")
        self.assertIn("evo exit-optimize-mode", (out.get("reason") or "").lower())


# ---------------------------------------------------------------------------
# Combined emit: pending directives must not be dropped by the nudge
# ---------------------------------------------------------------------------

class TestStopNudgePreservesQueuedDirectives(_Base):

    def test_pending_evo_direct_text_included_in_stop_reason(self):
        """When a user issues `evo direct 'try X'` and then the
        orchestrator emits Stop, the directive must still reach the
        agent. The nudge alone would drop it because the offset advances
        as part of normal drain."""
        from evo.inject import queue

        register_session(self.root, "orch", "claude-code")
        mark_engaged(self.root, "orch")
        mark_optimize_mode(self.root, "orch")
        mark_autonomous(self.root, "orch")

        # Enqueue an evo-direct event onto the workspace queue.
        queue.append_workspace_event(self.root, "TRY_COSINE_SIM_NOW")
        # Drop a marker so the drain looks at the queue.
        from evo.inject import marker
        marker.touch(self.root, "orch")

        out = _drain_stop(self.root, "orch", hook_event="Stop")
        reason = out.get("reason") or ""

        self.assertEqual(out.get("decision"), "block", "stop nudge still fires")
        self.assertIn(
            "TRY_COSINE_SIM_NOW", reason,
            "pending evo-direct text must be included in the stop reason "
            "so the user's intervention isn't dropped",
        )


# ---------------------------------------------------------------------------
# Autonomous arming: cursor shell-observation + exit-optimize-mode clears it
# ---------------------------------------------------------------------------

class TestAutonomousArming(_Base):
    """Cursor (and the other no-env-var hosts) can't arm autonomous via the
    `evo autonomous on` CLI (no session env var for detect_session). The
    Python drain instead OBSERVES the command on a cursor shell preToolUse
    and arms in-process. These tests cover that path + the disarm paths."""

    def _observe(self, sid: str, command: str) -> None:
        from evo.inject.drain import _maybe_mark_autonomous_from_shell
        payload = {"tool_name": "shell", "tool_input": {"command": command}}
        _maybe_mark_autonomous_from_shell(self.root, sid, "cursor", "preToolUse", payload)

    def _autonomous(self, sid: str) -> bool:
        from evo.inject.registry import get_session
        return bool((get_session(self.root, sid) or {}).get("autonomous"))

    def test_cursor_arms_on_evo_autonomous_on(self):
        from evo.inject.registry import register_session, mark_optimize_mode
        register_session(self.root, "cur1", "cursor")
        mark_optimize_mode(self.root, "cur1")
        self._observe("cur1", "evo autonomous on")
        self.assertTrue(self._autonomous("cur1"),
                        "observing `evo autonomous on` must arm autonomous on cursor")
        # And the nudge now fires (cursor uses followup_message).
        out = _drain_stop(self.root, "cur1", host="cursor", hook_event="stop")
        self.assertIn("optimize", (out.get("followup_message") or "").lower())

    def test_cursor_disarms_on_evo_autonomous_off(self):
        from evo.inject.registry import register_session, mark_optimize_mode, mark_autonomous
        register_session(self.root, "cur1", "cursor")
        mark_optimize_mode(self.root, "cur1")
        mark_autonomous(self.root, "cur1")
        self._observe("cur1", "evo autonomous off")
        self.assertFalse(self._autonomous("cur1"), "`evo autonomous off` must disarm")

    def test_non_evo_command_does_not_arm(self):
        from evo.inject.registry import register_session, mark_optimize_mode
        register_session(self.root, "cur1", "cursor")
        mark_optimize_mode(self.root, "cur1")
        self._observe("cur1", "evo status")          # not an autonomous command
        self._observe("cur1", "echo autonomous on")  # prose, not the evo command
        self.assertFalse(self._autonomous("cur1"),
                         "only the literal `evo autonomous on` command arms it")

    def test_exit_optimize_mode_clears_autonomous(self):
        from evo.inject.registry import (
            register_session, mark_optimize_mode, mark_autonomous, get_session,
            unmark_optimize_mode, unmark_autonomous,
        )
        register_session(self.root, "orch", "claude-code")
        mark_optimize_mode(self.root, "orch")
        mark_autonomous(self.root, "orch")
        # exit-optimize-mode clears BOTH (cmd_exit_optimize_mode calls both).
        unmark_optimize_mode(self.root, "orch")
        unmark_autonomous(self.root, "orch")
        rec = get_session(self.root, "orch") or {}
        self.assertFalse(rec.get("optimize_mode"))
        self.assertFalse(rec.get("autonomous"))


if __name__ == "__main__":
    unittest.main()

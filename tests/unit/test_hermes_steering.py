"""Tests for the hermes steering port.

Hermes hook contract (verified against hermes-agent
`hermes_cli/plugins.py#VALID_HOOKS`):
  - `pre_tool_call` — return `{"action": "block", "message": ...}` to
    short-circuit a tool. Verified at hermes_cli/plugins.py:88-90 and
    model_tools.py:60-62.
  - `pre_llm_call` — return `{"context": "..."}` to append text to the
    current turn's user message (run_agent.py:721-740).
  - `on_session_end` — observer-style return, but `ctx.inject_message`
    queues a follow-up turn (plugins.py:359-383; CLI loop picks it up
    at cli.py:14000-14082, same mechanism `/goal` uses at
    cli.py:9126-9240).

Tests drive the hook functions directly with real on-disk session
state. No mocks of drain or registry.

Run: pytest tests/unit/test_hermes_steering.py -v
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "plugins" / "evo" / "src"))

from evo.inject.registry import (
    register_session, mark_engaged, mark_optimize_mode, mark_subagents_only,
    mark_autonomous,
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
    return next(iter((root / ".evo").glob("run_*")))


class _Base(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name).resolve()
        self.run_dir = _make_workspace(self.root)
        import os
        self._orig_cwd = os.getcwd()
        os.chdir(self.root)
        # Reset module-level state between tests so register() runs
        # under the test fixture's stub ctx, not a previous test's.
        import evo.hermes_plugin as hp
        hp._PLUGIN_CTX = None
        hp._LAST_SESSION_ID = None

    def tearDown(self):
        import os
        os.chdir(self._orig_cwd)
        self._tmp.cleanup()


# ---------------------------------------------------------------------------
# Verify hermes plugin only registers hooks that hermes actually fires
# ---------------------------------------------------------------------------

class TestRegistersOnlyValidHooks(unittest.TestCase):

    # Match hermes-agent's hermes_cli/plugins.py#VALID_HOOKS.
    HERMES_VALID_HOOKS = frozenset({
        "pre_tool_call", "post_tool_call",
        "pre_llm_call", "post_llm_call",
        "pre_api_request", "post_api_request",
        "on_session_start", "on_session_end",
        "on_session_finalize", "on_session_reset",
    })

    def test_register_only_uses_valid_hook_names(self):
        from evo.hermes_plugin import register

        registered: list[str] = []

        class _FakeCtx:
            def register_hook(self, name, cb):
                registered.append(name)

        register(_FakeCtx())
        for name in registered:
            self.assertIn(
                name, self.HERMES_VALID_HOOKS,
                f"hermes plugin registers {name!r}, which is NOT in hermes-agent "
                f"VALID_HOOKS. The hook will never fire."
            )

    def test_register_includes_session_end_for_stop_nudge(self):
        from evo.hermes_plugin import register

        registered: list[str] = []

        class _FakeCtx:
            def register_hook(self, name, cb):
                registered.append(name)

        register(_FakeCtx())
        self.assertIn("on_session_end", registered,
                      "on_session_end must be registered — it's the stop-nudge entry point")

    def test_transform_tool_result_not_registered(self):
        """Regression: a previous evo plugin registered
        `transform_tool_result`, which hermes does not fire."""
        from evo.hermes_plugin import register

        registered: list[str] = []

        class _FakeCtx:
            def register_hook(self, name, cb):
                registered.append(name)

        register(_FakeCtx())
        self.assertNotIn("transform_tool_result", registered)


# ---------------------------------------------------------------------------
# pre_tool_call deny gate: synchronous block on odd violations
# ---------------------------------------------------------------------------

class TestPreToolCallDenies(_Base):

    def test_denied_tool_returns_block_envelope(self):
        from evo.hermes_plugin import _on_pre_tool_call

        register_session(self.root, "h1", "hermes")
        mark_engaged(self.root, "h1")
        mark_optimize_mode(self.root, "h1")
        mark_subagents_only(self.root, "h1")  # deny-gate is opt-in

        out = _on_pre_tool_call(
            tool_name="file_write", args={"file_path": "/x"},
            task_id="h1", session_id="h1",
        )
        self.assertIsInstance(out, dict, "denied tool must return a dict")
        self.assertEqual(out.get("action"), "block",
                         f"odd-numbered violation must block; got {out!r}")
        self.assertIn("EVO POLICY", out.get("message", ""),
                      "block message must carry the policy banner")

    def test_denied_tool_increments_counter(self):
        from evo.hermes_plugin import _on_pre_tool_call
        from evo.inject.drain import _read_policy_state

        register_session(self.root, "h1", "hermes")
        mark_engaged(self.root, "h1")
        mark_optimize_mode(self.root, "h1")
        mark_subagents_only(self.root, "h1")  # deny-gate is opt-in

        _on_pre_tool_call(
            tool_name="file_write", args={"file_path": "/x"},
            task_id="h1", session_id="h1",
        )
        state = _read_policy_state(self.root, "h1")
        self.assertEqual(state.get("violation_count"), 1)
        self.assertEqual(state.get("last_violation_tool"), "file_write")

    def test_alternating_cadence(self):
        from evo.hermes_plugin import _on_pre_tool_call

        register_session(self.root, "h1", "hermes")
        mark_engaged(self.root, "h1")
        mark_optimize_mode(self.root, "h1")
        mark_subagents_only(self.root, "h1")  # deny-gate is opt-in

        # #1 odd → block
        out1 = _on_pre_tool_call(tool_name="file_write", args={"file_path": "/a"},
                                 task_id="h1", session_id="h1")
        self.assertEqual(out1.get("action"), "block",
                         f"violation #1 must block; got {out1!r}")

        # #2 even → pass
        out2 = _on_pre_tool_call(tool_name="file_write", args={"file_path": "/b"},
                                 task_id="h1", session_id="h1")
        self.assertIsNone(out2,
                          f"violation #2 must pass under alternating cadence; got {out2!r}")

        # #3 odd → block
        out3 = _on_pre_tool_call(tool_name="file_write", args={"file_path": "/c"},
                                 task_id="h1", session_id="h1")
        self.assertEqual(out3.get("action"), "block",
                         f"violation #3 must block; got {out3!r}")

    def test_denied_bash_command_blocks(self):
        from evo.hermes_plugin import _on_pre_tool_call

        register_session(self.root, "h1", "hermes")
        mark_engaged(self.root, "h1")
        mark_optimize_mode(self.root, "h1")
        mark_subagents_only(self.root, "h1")  # deny-gate is opt-in

        out = _on_pre_tool_call(
            tool_name="terminal", args={"command": "sed -i s/a/b/ f.py"},
            task_id="h1", session_id="h1",
        )
        self.assertEqual(out.get("action"), "block")

    def test_non_denied_tool_passes(self):
        from evo.hermes_plugin import _on_pre_tool_call
        from evo.inject.drain import _read_policy_state

        register_session(self.root, "h1", "hermes")
        mark_engaged(self.root, "h1")
        mark_optimize_mode(self.root, "h1")
        mark_subagents_only(self.root, "h1")  # deny-gate is opt-in

        for tn in ("read_file", "list_dir", "web_search", "grep"):
            out = _on_pre_tool_call(
                tool_name=tn, args={"path": "/x"},
                task_id="h1", session_id="h1",
            )
            self.assertIsNone(out, f"non-denied tool {tn!r} must not block")
        state = _read_policy_state(self.root, "h1")
        self.assertEqual(state.get("violation_count", 0), 0)

    def test_outside_optimize_mode_passes(self):
        from evo.hermes_plugin import _on_pre_tool_call

        register_session(self.root, "h1", "hermes")
        mark_engaged(self.root, "h1")
        # optimize_mode NOT set
        out = _on_pre_tool_call(
            tool_name="file_write", args={}, task_id="h1", session_id="h1",
        )
        self.assertIsNone(out, "no optimize_mode → must not block")

    def test_optimize_mode_without_subagents_only_allows_edits(self):
        """Default flip: /optimize alone (optimize_mode on, subagents_only
        off) must NOT block orchestrator edits. The deny-gate is opt-in,
        armed only by `evo subagents-only on`."""
        from evo.hermes_plugin import _on_pre_tool_call

        register_session(self.root, "h1", "hermes")
        mark_engaged(self.root, "h1")
        mark_optimize_mode(self.root, "h1")
        # subagents_only NOT armed
        for _ in range(3):
            out = _on_pre_tool_call(
                tool_name="file_write", args={"file_path": "/x"},
                task_id="h1", session_id="h1",
            )
            self.assertIsNone(
                out, "optimize_mode without subagents_only must allow edits")

    def test_subagent_passes(self):
        from evo.hermes_plugin import _on_pre_tool_call
        from evo.inject.paths import session_file

        register_session(self.root, "h_sub", "hermes", exp_id="exp_0001")
        mark_engaged(self.root, "h_sub")
        # Force optimize_mode true (mark_optimize_mode refuses subagents).
        sf = session_file(self.root, "h_sub")
        data = json.loads(sf.read_text())
        data["optimize_mode"] = True
        sf.write_text(json.dumps(data))

        out = _on_pre_tool_call(
            tool_name="file_write", args={}, task_id="h_sub", session_id="h_sub",
        )
        self.assertIsNone(out, "subagent must be exempt from deny gate")


# ---------------------------------------------------------------------------
# pre_llm_call: directive drain + auto-arm
# ---------------------------------------------------------------------------

class TestPreLlmCallDrain(_Base):

    def test_no_pending_state_returns_none(self):
        from evo.hermes_plugin import _on_pre_llm_call

        register_session(self.root, "h1", "hermes")
        mark_engaged(self.root, "h1")
        mark_optimize_mode(self.root, "h1")
        mark_subagents_only(self.root, "h1")  # deny-gate is opt-in

        out = _on_pre_llm_call(session_id="h1")
        self.assertIsNone(out, "no queued directive → no injection")

    def test_queued_directive_delivered(self):
        from evo.hermes_plugin import _on_pre_llm_call
        from evo.inject import queue, marker

        register_session(self.root, "h1", "hermes")
        mark_engaged(self.root, "h1")

        queue.append_workspace_event(self.root, "TRY_COSINE")
        marker.touch(self.root, "h1")

        out = _on_pre_llm_call(session_id="h1")
        self.assertIsNotNone(out)
        self.assertIn("TRY_COSINE", out.get("context", ""))


class TestHermesAutoArmOptimizeMode(_Base):

    def test_pre_llm_call_with_optimize_prompt_arms_mode(self):
        from evo.hermes_plugin import _on_pre_llm_call
        from evo.inject.registry import get_session

        _on_pre_llm_call(session_id="h_arm", prompt="/optimize fix the bug")
        sess = get_session(self.root, "h_arm")
        self.assertIsNotNone(sess)
        self.assertTrue(sess.get("optimize_mode"),
                        f"/optimize must auto-arm optimize_mode; got {sess!r}")

    def test_pre_llm_call_without_optimize_prompt_does_not_arm(self):
        from evo.hermes_plugin import _on_pre_llm_call
        from evo.inject.registry import get_session

        _on_pre_llm_call(session_id="h_noarm", prompt="what is the weather")
        sess = get_session(self.root, "h_noarm")
        self.assertIsNotNone(sess)
        self.assertFalse(sess.get("optimize_mode"),
                         f"non-/optimize prompt must not arm; got {sess!r}")


# ---------------------------------------------------------------------------
# on_session_end: stop nudge via ctx.inject_message
# ---------------------------------------------------------------------------

class TestOnSessionEnd(_Base):

    def _install_stub_ctx(self) -> list[tuple[str, str]]:
        """Register the plugin against a stub ctx that records
        inject_message calls. Returns the recording list."""
        from evo.hermes_plugin import register
        recorded: list[tuple[str, str]] = []

        class _StubCtx:
            def register_hook(self, name, cb):
                pass

            def inject_message(self, content, role="user"):
                recorded.append((role, content))
                return True

        register(_StubCtx())
        return recorded

    def test_session_end_injects_stop_nudge_when_autonomous(self):
        from evo.hermes_plugin import _on_session_end

        recorded = self._install_stub_ctx()
        register_session(self.root, "h1", "hermes")
        mark_engaged(self.root, "h1")
        mark_optimize_mode(self.root, "h1")
        mark_autonomous(self.root, "h1")  # stop-nudge is opt-in

        _on_session_end(session_id="h1")
        self.assertEqual(len(recorded), 1,
                         f"autonomous session end must inject 1 message; got {recorded!r}")
        role, content = recorded[0]
        self.assertEqual(role, "user")
        self.assertIn("EVO LOOP", content,
                      "stop nudge must carry the EVO LOOP banner")

    def test_session_end_without_autonomous_is_noop(self):
        """optimize_mode on but autonomous NOT armed → no stop nudge. The
        loop is opt-in; default /optimize lets the agent stop naturally."""
        from evo.hermes_plugin import _on_session_end

        recorded = self._install_stub_ctx()
        register_session(self.root, "h1", "hermes")
        mark_engaged(self.root, "h1")
        mark_optimize_mode(self.root, "h1")
        # autonomous NOT armed

        _on_session_end(session_id="h1")
        self.assertEqual(recorded, [],
                         "optimize_mode without autonomous → no stop nudge")

    def test_session_end_outside_optimize_mode_is_noop(self):
        from evo.hermes_plugin import _on_session_end

        recorded = self._install_stub_ctx()
        register_session(self.root, "h1", "hermes")
        mark_engaged(self.root, "h1")
        # optimize_mode NOT set

        _on_session_end(session_id="h1")
        self.assertEqual(recorded, [],
                         "no optimize_mode → no stop nudge")

    def test_session_end_in_subagent_is_noop(self):
        from evo.hermes_plugin import _on_session_end
        from evo.inject.paths import session_file

        recorded = self._install_stub_ctx()
        register_session(self.root, "h_sub", "hermes", exp_id="exp_0001")
        mark_engaged(self.root, "h_sub")
        sf = session_file(self.root, "h_sub")
        data = json.loads(sf.read_text())
        data["optimize_mode"] = True
        sf.write_text(json.dumps(data))

        _on_session_end(session_id="h_sub")
        self.assertEqual(recorded, [],
                         "subagent session end must not push a parent-loop nudge")


if __name__ == "__main__":
    unittest.main()

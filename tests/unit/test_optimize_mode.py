"""Unit tests for optimize_mode session-record state + CLI commands.

The optimize_mode flag tags a session as the orchestrator currently
driving /evo:optimize. It controls:
  - the policy nudge (denies orchestrator Edit/Write/non-evo-Bash calls)
  - the Stop-hook self-continuation (re-prompts the orchestrator on Stop)

It's set automatically via UserPromptSubmit pattern-matching (covered by
test_optimize_detection.py) and can be exited via `evo exit-optimize-mode`.
This file covers the registry-level state and the exit command.

No mocks — real session records on real tempdir.

Run: pytest tests/unit/test_optimize_mode.py -v
"""

from __future__ import annotations

import argparse
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "plugins" / "evo" / "src"))

from evo.inject.paths import session_file
from evo.inject.registry import register_session


def _init_git_repo(root: Path) -> None:
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.email", "t@evo"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.name", "T"], cwd=root, check=True)
    subprocess.run(["git", "config", "commit.gpgsign", "false"], cwd=root, check=True)
    (root / "README.md").write_text("x\n")
    subprocess.run(["git", "add", "."], cwd=root, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=root, check=True)


def _make_workspace(root: Path) -> None:
    _init_git_repo(root)
    from evo.core import init_workspace
    init_workspace(
        root, target="agent.py", benchmark="python bench.py",
        metric="max", gate=None,
    )


def _clear_host_env() -> None:
    for v in ("CLAUDE_CODE_SESSION_ID", "CODEX_THREAD_ID",
              "HERMES_SESSION_ID", "OPENCODE_SESSION_ID", "EVO_EXP_ID"):
        os.environ.pop(v, None)


def _read_record(root: Path, sid: str) -> dict:
    return json.loads(session_file(root, sid).read_text())


# ---------------------------------------------------------------------------
# Schema: new session-record fields
# ---------------------------------------------------------------------------

class TestOptimizeModeSchema(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name).resolve()
        _make_workspace(self.root)

    def tearDown(self):
        self._tmp.cleanup()

    def test_new_session_defaults_optimize_mode_false(self):
        register_session(self.root, "s1", "claude-code")
        rec = _read_record(self.root, "s1")
        self.assertIn("optimize_mode", rec,
                      "schema must include optimize_mode field")
        self.assertFalse(rec["optimize_mode"])
        self.assertIsNone(rec.get("optimize_mode_at"))


# ---------------------------------------------------------------------------
# mark_optimize_mode helper (registry-level)
# ---------------------------------------------------------------------------

class TestMarkOptimizeMode(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name).resolve()
        _make_workspace(self.root)

    def tearDown(self):
        self._tmp.cleanup()

    def test_flips_false_to_true(self):
        from evo.inject.registry import mark_optimize_mode
        register_session(self.root, "s1", "claude-code")
        transitioned = mark_optimize_mode(self.root, "s1")
        self.assertTrue(transitioned, "first call must report a transition")
        rec = _read_record(self.root, "s1")
        self.assertTrue(rec["optimize_mode"])
        self.assertIsNotNone(rec["optimize_mode_at"])

    def test_idempotent_second_call_returns_false(self):
        from evo.inject.registry import mark_optimize_mode
        register_session(self.root, "s1", "claude-code")
        mark_optimize_mode(self.root, "s1")
        second = mark_optimize_mode(self.root, "s1")
        self.assertFalse(second, "second call must not be a transition")

    def test_no_op_on_unregistered_session(self):
        from evo.inject.registry import mark_optimize_mode
        result = mark_optimize_mode(self.root, "ghost")
        self.assertFalse(result, "missing session must return False, not raise")

    def test_subagent_session_cannot_be_flagged(self):
        """A session with exp_id set is a subagent context; mark_optimize_mode
        must refuse to flag it (subagents are never orchestrators)."""
        from evo.inject.registry import mark_optimize_mode
        register_session(self.root, "sub_sid", "claude-code", exp_id="exp_0042")
        result = mark_optimize_mode(self.root, "sub_sid")
        self.assertFalse(result, "subagent sessions must not be flagged")
        rec = _read_record(self.root, "sub_sid")
        self.assertFalse(rec["optimize_mode"])


# ---------------------------------------------------------------------------
# unmark_optimize_mode helper
# ---------------------------------------------------------------------------

class TestUnmarkOptimizeMode(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name).resolve()
        _make_workspace(self.root)

    def tearDown(self):
        self._tmp.cleanup()

    def test_flips_true_to_false(self):
        from evo.inject.registry import mark_optimize_mode, unmark_optimize_mode
        register_session(self.root, "s1", "claude-code")
        mark_optimize_mode(self.root, "s1")
        transitioned = unmark_optimize_mode(self.root, "s1")
        self.assertTrue(transitioned)
        rec = _read_record(self.root, "s1")
        self.assertFalse(rec["optimize_mode"])
        self.assertIsNone(rec["optimize_mode_at"])

    def test_idempotent_on_already_false(self):
        from evo.inject.registry import unmark_optimize_mode
        register_session(self.root, "s1", "claude-code")
        result = unmark_optimize_mode(self.root, "s1")
        self.assertFalse(result, "no-op return False on already-false")


# ---------------------------------------------------------------------------
# optimize_mode side-channel flag file — fast-path signal for the Rust hook
# ---------------------------------------------------------------------------

class TestOptimizeModeFlagFile(unittest.TestCase):
    """The Rust hook decides whether to hand off to Python by stat()'ing
    `inject/optimize_mode/<sid>.flag`. mark/unmark keep this cache in
    sync with the source-of-truth field in `inject/sessions/<sid>.json`."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name).resolve()
        _make_workspace(self.root)

    def tearDown(self):
        self._tmp.cleanup()

    def test_mark_drops_flag_file(self):
        from evo.inject.registry import mark_optimize_mode
        from evo.inject.paths import optimize_mode_flag_file
        register_session(self.root, "s1", "claude-code")
        self.assertFalse(optimize_mode_flag_file(self.root, "s1").exists(),
                         "flag must not exist before mark")
        mark_optimize_mode(self.root, "s1")
        self.assertTrue(optimize_mode_flag_file(self.root, "s1").exists(),
                        "mark_optimize_mode must drop the flag file")

    def test_unmark_removes_flag_file(self):
        from evo.inject.registry import mark_optimize_mode, unmark_optimize_mode
        from evo.inject.paths import optimize_mode_flag_file
        register_session(self.root, "s1", "claude-code")
        mark_optimize_mode(self.root, "s1")
        self.assertTrue(optimize_mode_flag_file(self.root, "s1").exists())
        unmark_optimize_mode(self.root, "s1")
        self.assertFalse(optimize_mode_flag_file(self.root, "s1").exists(),
                         "unmark_optimize_mode must remove the flag file")

    def test_mark_does_not_drop_flag_for_subagent(self):
        """Subagents (exp_id set) are never tagged orchestrator; the flag
        file must not appear for them."""
        from evo.inject.registry import mark_optimize_mode
        from evo.inject.paths import optimize_mode_flag_file
        register_session(self.root, "sub", "claude-code", exp_id="exp_0001")
        result = mark_optimize_mode(self.root, "sub")
        self.assertFalse(result, "subagent mark must no-op")
        self.assertFalse(optimize_mode_flag_file(self.root, "sub").exists(),
                         "subagent must not have a flag file")

    def test_self_heal_recreates_missing_flag_on_second_mark(self):
        """If the flag file is removed externally but optimize_mode is
        still true in the JSON, the next `mark_optimize_mode` call must
        re-create the flag even though it returns False (no transition)."""
        from evo.inject.registry import mark_optimize_mode
        from evo.inject.paths import optimize_mode_flag_file
        register_session(self.root, "s1", "claude-code")
        mark_optimize_mode(self.root, "s1")
        flag = optimize_mode_flag_file(self.root, "s1")
        flag.unlink()
        self.assertFalse(flag.exists())
        # Second mark returns False (already true) but must re-create flag.
        result = mark_optimize_mode(self.root, "s1")
        self.assertFalse(result, "no transition since already true")
        self.assertTrue(flag.exists(),
                        "self-heal must re-create the missing flag file")

    def test_unmark_clears_leaked_flag_even_if_json_missing(self):
        """If the session JSON was wiped but the flag leaked, unmark
        should still clean up the flag."""
        from evo.inject.registry import unmark_optimize_mode
        from evo.inject.paths import (
            optimize_mode_dir,
            optimize_mode_flag_file,
        )
        # Drop a leaked flag with no corresponding session JSON.
        optimize_mode_dir(self.root).mkdir(parents=True, exist_ok=True)
        flag = optimize_mode_flag_file(self.root, "ghost")
        flag.touch()
        self.assertTrue(flag.exists())
        unmark_optimize_mode(self.root, "ghost")
        self.assertFalse(flag.exists(),
                         "unmark must clear a leaked flag even without JSON")


# ---------------------------------------------------------------------------
# evo exit-optimize-mode CLI
# ---------------------------------------------------------------------------

class TestExitOptimizeModeCli(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name).resolve()
        _make_workspace(self.root)
        self._old_cwd = Path.cwd()
        os.chdir(self.root)

    def tearDown(self):
        os.chdir(self._old_cwd)
        self._tmp.cleanup()

    def test_clears_flag_when_invoked_in_engaged_session(self):
        from evo.cli import cmd_exit_optimize_mode
        from evo.inject.registry import mark_optimize_mode

        register_session(self.root, "live_sid", "claude-code")
        mark_optimize_mode(self.root, "live_sid")

        buf = io.StringIO()
        with patch.dict(os.environ, {"CLAUDE_CODE_SESSION_ID": "live_sid"}, clear=False):
            _clear_host_env()
            os.environ["CLAUDE_CODE_SESSION_ID"] = "live_sid"
            with patch("sys.stdout", buf):
                rc = cmd_exit_optimize_mode(argparse.Namespace())

        self.assertEqual(rc, 0)
        rec = _read_record(self.root, "live_sid")
        self.assertFalse(rec["optimize_mode"])

    def test_idempotent_when_already_clear(self):
        from evo.cli import cmd_exit_optimize_mode
        register_session(self.root, "noop_sid", "claude-code")

        buf = io.StringIO()
        with patch.dict(os.environ, {"CLAUDE_CODE_SESSION_ID": "noop_sid"}, clear=False):
            _clear_host_env()
            os.environ["CLAUDE_CODE_SESSION_ID"] = "noop_sid"
            with patch("sys.stdout", buf):
                rc = cmd_exit_optimize_mode(argparse.Namespace())

        self.assertEqual(rc, 0, "should succeed cleanly even if flag was already off")

    def test_no_session_env_var_returns_error(self):
        from evo.cli import cmd_exit_optimize_mode
        _clear_host_env()
        buf = io.StringIO()
        with patch("sys.stderr", buf):
            rc = cmd_exit_optimize_mode(argparse.Namespace())
        self.assertNotEqual(rc, 0,
                            "no host session detected → must report error, not silently succeed")


if __name__ == "__main__":
    unittest.main()

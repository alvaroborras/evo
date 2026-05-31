"""Tests for the workspace-level per-experiment timeout.

`--per-exp-timeout` was added as a required flag on `evo init` so every
workspace declares the wall-clock budget for `evo run` up front. `evo
run --timeout N` overrides per-call. Workspaces initialized before the
flag existed fall back to a legacy 1800s with a one-line warning.

Covers:
  - init_workspace persists per_exp_timeout into config.json
  - init_workspace rejects non-positive values
  - _resolve_run_timeout precedence: --timeout > workspace > legacy 1800
  - legacy fallback emits the warning to stderr
  - `evo config set per-exp-timeout` updates an existing workspace
  - argparse rejects `evo init` without --per-exp-timeout
"""
from __future__ import annotations

import argparse
import io
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from evo.cli import _resolve_run_timeout, cmd_config_set
from evo.core import init_workspace, load_config


def _args(field: str, value: str) -> argparse.Namespace:
    return argparse.Namespace(field=field, value=value)


def _init_git_repo(root: Path) -> None:
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.email", "t@evo"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.name", "T"], cwd=root, check=True)
    subprocess.run(["git", "config", "commit.gpgsign", "false"], cwd=root, check=True)
    (root / "README.md").write_text("x\n")
    subprocess.run(["git", "add", "."], cwd=root, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=root, check=True)


class TestInitWorkspacePersists(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name).resolve()
        _init_git_repo(self.root)

    def tearDown(self):
        self._tmp.cleanup()

    def test_per_exp_timeout_persisted_when_provided(self):
        init_workspace(
            self.root, target="t.py", benchmark="python bench.py",
            metric="max", gate=None, per_exp_timeout=3600,
        )
        self.assertEqual(load_config(self.root)["per_exp_timeout"], 3600)

    def test_per_exp_timeout_absent_when_not_provided(self):
        """init_workspace called without per_exp_timeout (legacy callers) does
        not write the field. _resolve_run_timeout's legacy fallback kicks in."""
        init_workspace(
            self.root, target="t.py", benchmark="python bench.py",
            metric="max", gate=None,
        )
        self.assertNotIn("per_exp_timeout", load_config(self.root))

    def test_rejects_zero(self):
        with self.assertRaisesRegex(RuntimeError, "positive"):
            init_workspace(
                self.root, target="t.py", benchmark="python bench.py",
                metric="max", gate=None, per_exp_timeout=0,
            )

    def test_rejects_negative(self):
        with self.assertRaisesRegex(RuntimeError, "positive"):
            init_workspace(
                self.root, target="t.py", benchmark="python bench.py",
                metric="max", gate=None, per_exp_timeout=-1,
            )


class TestResolveRunTimeout(unittest.TestCase):
    def test_per_call_override_wins(self):
        args = argparse.Namespace(timeout=900)
        config = {"per_exp_timeout": 3600}
        self.assertEqual(_resolve_run_timeout(args, config), 900)

    def test_workspace_default_when_no_override(self):
        args = argparse.Namespace(timeout=None)
        config = {"per_exp_timeout": 3600}
        self.assertEqual(_resolve_run_timeout(args, config), 3600)

    def test_legacy_fallback_when_neither_present(self):
        args = argparse.Namespace(timeout=None)
        config: dict = {}
        captured = io.StringIO()
        with patch.object(sys, "stderr", captured):
            timeout = _resolve_run_timeout(args, config)
        self.assertEqual(timeout, 1800)
        self.assertIn("per-exp-timeout", captured.getvalue())
        self.assertIn("1800", captured.getvalue())

    def test_workspace_default_int_coerced(self):
        """config.json may have the value as int or string (json round-trip);
        either should work."""
        args = argparse.Namespace(timeout=None)
        config = {"per_exp_timeout": "2400"}
        self.assertEqual(_resolve_run_timeout(args, config), 2400)


class TestConfigSetPerExpTimeout(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name).resolve()
        _init_git_repo(self.root)
        init_workspace(
            self.root, target="t.py", benchmark="python bench.py",
            metric="max", gate=None,
        )
        self._old_cwd = Path.cwd()
        os.chdir(self.root)

    def tearDown(self):
        os.chdir(self._old_cwd)
        self._tmp.cleanup()

    def test_sets_per_exp_timeout(self):
        cmd_config_set(_args("per-exp-timeout", "5400"))
        self.assertEqual(load_config(self.root)["per_exp_timeout"], 5400)

    def test_rejects_zero(self):
        with self.assertRaisesRegex(RuntimeError, "positive"):
            cmd_config_set(_args("per-exp-timeout", "0"))

    def test_rejects_negative(self):
        with self.assertRaisesRegex(RuntimeError, "positive"):
            cmd_config_set(_args("per-exp-timeout", "-30"))

    def test_rejects_non_integer(self):
        with self.assertRaisesRegex(RuntimeError, "positive integer"):
            cmd_config_set(_args("per-exp-timeout", "abc"))


class TestInitArgparseRequiresFlag(unittest.TestCase):
    """End-to-end argparse check: `evo init ...` without --per-exp-timeout
    must exit non-zero. Catches regressions where the required=True flag
    gets accidentally removed."""

    def test_init_without_per_exp_timeout_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            _init_git_repo(root)
            r = subprocess.run(
                [sys.executable, "-m", "evo.cli", "init",
                 "--target", "t.py",
                 "--benchmark", "true",
                 "--metric", "max",
                 "--host", "claude-code"],
                cwd=root, capture_output=True, text=True,
            )
            self.assertNotEqual(r.returncode, 0)
            self.assertIn("--per-exp-timeout", r.stderr)


if __name__ == "__main__":
    unittest.main()

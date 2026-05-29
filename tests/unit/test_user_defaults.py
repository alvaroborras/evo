"""Tests for cross-project user defaults (`~/.evo/defaults.json`) and the
`evo defaults` CLI. EVO_HOME is redirected to a temp dir so the real home
is never touched.
"""
from __future__ import annotations

import argparse
import json
import os
import tempfile
import unittest
from pathlib import Path

from evo import user_defaults
from evo.cli import cmd_defaults_set


class TestUserDefaults(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._home = Path(self._tmp.name).resolve() / "evo-home"
        self._prev = os.environ.get("EVO_HOME")
        os.environ["EVO_HOME"] = str(self._home)

    def tearDown(self):
        if self._prev is None:
            os.environ.pop("EVO_HOME", None)
        else:
            os.environ["EVO_HOME"] = self._prev
        self._tmp.cleanup()

    # --- module ---------------------------------------------------------

    def test_evo_home_override_used(self):
        self.assertEqual(user_defaults.global_evo_dir(), self._home)
        self.assertEqual(
            user_defaults.global_defaults_path(), self._home / "defaults.json"
        )

    def test_get_unset_is_none(self):
        self.assertIsNone(user_defaults.get_user_default("autonomous"))
        self.assertIsNone(user_defaults.get_user_default("subagents_only"))

    def test_set_then_get_roundtrip(self):
        user_defaults.set_user_default("autonomous", True)
        user_defaults.set_user_default("subagents_only", False)
        self.assertIs(user_defaults.get_user_default("autonomous"), True)
        self.assertIs(user_defaults.get_user_default("subagents_only"), False)

    def test_set_creates_file_with_expected_shape(self):
        user_defaults.set_user_default("autonomous", True)
        path = user_defaults.global_defaults_path()
        self.assertTrue(path.exists(), "defaults.json must be created")
        data = json.loads(path.read_text())
        self.assertEqual(data["autonomous"], True)

    def test_set_does_not_clobber_other_key(self):
        user_defaults.set_user_default("autonomous", True)
        user_defaults.set_user_default("subagents_only", True)
        self.assertIs(user_defaults.get_user_default("autonomous"), True)

    def test_unknown_key_rejected(self):
        with self.assertRaises(ValueError):
            user_defaults.get_user_default("nope")
        with self.assertRaises(ValueError):
            user_defaults.set_user_default("nope", True)

    def test_default_is_off_when_no_home_dir_exists(self):
        # Fresh EVO_HOME that was never written — reads must not crash.
        self.assertIsNone(user_defaults.get_user_default("autonomous"))
        self.assertEqual(user_defaults.load_user_defaults(), {})

    # --- CLI ------------------------------------------------------------

    def test_cli_set_get_roundtrip(self):
        cmd_defaults_set(argparse.Namespace(field="autonomous", value="on"))
        self.assertIs(user_defaults.get_user_default("autonomous"), True)
        cmd_defaults_set(argparse.Namespace(field="subagents-only", value="off"))
        self.assertIs(user_defaults.get_user_default("subagents_only"), False)

    def test_cli_set_rejects_bad_value(self):
        with self.assertRaises(RuntimeError):
            cmd_defaults_set(argparse.Namespace(field="autonomous", value="maybe"))


if __name__ == "__main__":
    unittest.main()

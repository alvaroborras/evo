"""Unit tests for the pre/post gate-phase split.

Gates can register as `phase="pre"` (runs before the benchmark; failure
aborts the run with no benchmark spend) or `phase="post"` (runs after
the benchmark; needs result.json to evaluate). Missing phase defaults
to "post" — preserves behavior of gates registered before the split.

No mocks: real graph mutation, real `_split_gates_by_phase` over real
gate dicts. Run-path integration coverage lives in tests/e2e/.

Run: pytest tests/unit/test_gate_phases.py -v
"""

from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "plugins" / "evo" / "src"))


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
        root, target="agent.py", benchmark="echo hi",
        metric="max", gate=None,
    )


class TestAddGatePhase(unittest.TestCase):
    """add_gate persists the phase and rejects invalid values."""

    def test_phase_pre_persists(self):
        from evo.core import add_gate, load_graph
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            _make_workspace(root)
            entry = add_gate(root, "root", "anti_cheat", "true", phase="pre")
            self.assertEqual(entry["phase"], "pre")
            graph = load_graph(root)
            gates = graph["nodes"]["root"]["gates"]
            self.assertEqual(gates[0]["phase"], "pre")

    def test_phase_defaults_to_post(self):
        from evo.core import add_gate
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            _make_workspace(root)
            entry = add_gate(root, "root", "score_check", "true")
            self.assertEqual(entry["phase"], "post",
                             "default must be 'post' to preserve pre-split behavior")

    def test_invalid_phase_rejected(self):
        from evo.core import add_gate
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            _make_workspace(root)
            with self.assertRaises(ValueError) as ctx:
                add_gate(root, "root", "bad", "true", phase="middle")
            self.assertIn("phase", str(ctx.exception))


class TestSplitGatesByPhase(unittest.TestCase):
    """_split_gates_by_phase partitions an inherited gate list."""

    def test_pre_post_partitioned(self):
        from evo.cli import _split_gates_by_phase
        gates = [
            {"name": "a", "phase": "pre"},
            {"name": "b", "phase": "post"},
            {"name": "c", "phase": "pre"},
        ]
        pre, post = _split_gates_by_phase(gates)
        self.assertEqual([g["name"] for g in pre], ["a", "c"])
        self.assertEqual([g["name"] for g in post], ["b"])

    def test_missing_phase_treated_as_post(self):
        """Backward compat: gates persisted before the split lack a `phase`
        field. They must run after the benchmark (their original behavior),
        not be silently dropped or rerouted to pre."""
        from evo.cli import _split_gates_by_phase
        gates = [{"name": "legacy", "command": "true"}]  # no phase field
        pre, post = _split_gates_by_phase(gates)
        self.assertEqual(pre, [])
        self.assertEqual([g["name"] for g in post], ["legacy"])

    def test_empty_input(self):
        from evo.cli import _split_gates_by_phase
        pre, post = _split_gates_by_phase([])
        self.assertEqual(pre, [])
        self.assertEqual(post, [])


class TestGateListIncludesPhase(unittest.TestCase):
    """`evo gate list` JSON includes the phase so users can audit what's
    pre vs post without inspecting the graph directly."""

    def test_list_output_carries_phase(self):
        import argparse
        import io
        import json
        import os
        from unittest.mock import patch
        from evo.cli import cmd_gate
        from evo.core import add_gate

        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            _make_workspace(root)
            add_gate(root, "root", "pre_gate", "true", phase="pre")
            add_gate(root, "root", "post_gate", "true", phase="post")
            prev = os.getcwd()
            os.chdir(root)
            try:
                buf = io.StringIO()
                with patch("sys.stdout", buf):
                    rc = cmd_gate(argparse.Namespace(
                        gate_action="list", exp_id="root",
                    ))
                self.assertEqual(rc, 0)
                payload = json.loads(buf.getvalue())
                by_name = {g["name"]: g for g in payload}
                self.assertEqual(by_name["pre_gate"]["phase"], "pre")
                self.assertEqual(by_name["post_gate"]["phase"], "post")
            finally:
                os.chdir(prev)


if __name__ == "__main__":
    unittest.main()

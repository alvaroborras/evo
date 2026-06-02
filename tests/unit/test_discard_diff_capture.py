"""Tests for the discard-time diff capture (#57).

`_capture_discard_time_diff` snapshots the experiment branch vs its
parent into <experiment_dir>/diff.patch before
`delete_discarded_experiment` removes the worktree + branch. Without
this, code changes a subagent made (train.py rewrites, helper-script
edits) are lost on discard -- only the one-sentence `discard_reason`
survives.

Real filesystem, real git operations. The helper invokes the existing
`capture_experiment_diff` from core.py which shells out to `git diff`.
"""
from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "plugins" / "evo" / "src"))

from evo.cli import _capture_discard_time_diff  # noqa: E402


def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git"] + list(args), cwd=cwd, capture_output=True, text=True, check=True,
    )


def _init_repo_with_workspace(root: Path) -> tuple[Path, str]:
    """Set up a minimal evo workspace with one committed file. Returns
    (worktree_path, parent_commit_sha)."""
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "t@evo")
    _git(root, "config", "user.name", "T")
    _git(root, "config", "commit.gpgsign", "false")
    (root / "agent.py").write_text("x = 1\n")
    (root / "bench.sh").write_text("echo '{\"score\": 0.0}'\n")
    (root / "bench.sh").chmod(0o755)
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "initial")
    parent_sha = _git(root, "rev-parse", "HEAD").stdout.strip()

    from evo.core import init_workspace
    init_workspace(
        root, target="agent.py", benchmark="./bench.sh",
        metric="max", gate=None,
    )

    # The graph carries a root node with the initial commit as parent.
    return root, parent_sha


def _make_experiment_with_changes(root: Path, exp_id: str, parent_sha: str, changes: dict[str, str]) -> Path:
    """Create a worktree on a new branch off `parent_sha` with `changes`
    applied + committed. Returns the worktree path."""
    branch = f"evo/run_0000/{exp_id}"
    worktree = root / ".evo" / "run_0000" / "worktrees" / exp_id
    worktree.parent.mkdir(parents=True, exist_ok=True)
    _git(root, "worktree", "add", "-q", "-b", branch, str(worktree), parent_sha)
    for rel, content in changes.items():
        path = worktree / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
    _git(worktree, "add", "-A")
    _git(worktree, "commit", "-q", "-m", f"changes for {exp_id}")
    return worktree


class TestCaptureDiscardTimeDiff(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name).resolve()

    def tearDown(self):
        self._tmp.cleanup()

    def _node_and_graph(self, exp_id: str, worktree: Path, parent_id: str, parent_sha: str) -> tuple[dict, dict]:
        node = {
            "id": exp_id, "parent": parent_id, "children": [],
            "status": "active", "score": None, "hypothesis": "test",
            "branch": f"evo/run_0000/{exp_id}",
            "worktree": str(worktree),
        }
        graph = {"nodes": {parent_id: {"id": parent_id, "commit": parent_sha, "branch": "main"}, exp_id: node}}
        return node, graph

    # ---- positive: diff lands ----

    def test_writes_diff_patch_for_branch_with_commits(self):
        root, parent_sha = _init_repo_with_workspace(self.root)
        worktree = _make_experiment_with_changes(
            root, "exp_0001", parent_sha,
            {"train.py": "import torch\n# new training script\n"},
        )
        node, graph = self._node_and_graph("exp_0001", worktree, "root", parent_sha)

        result = _capture_discard_time_diff(root, "exp_0001", node, graph)

        self.assertIsNotNone(result)
        self.assertTrue(result.exists())
        text = result.read_text()
        self.assertIn("train.py", text)
        self.assertIn("import torch", text)

    def test_captures_modifications_not_just_additions(self):
        root, parent_sha = _init_repo_with_workspace(self.root)
        # Modify an existing tracked file.
        worktree = _make_experiment_with_changes(
            root, "exp_0002", parent_sha,
            {"agent.py": "x = 2  # changed\n"},
        )
        node, graph = self._node_and_graph("exp_0002", worktree, "root", parent_sha)

        result = _capture_discard_time_diff(root, "exp_0002", node, graph)
        text = result.read_text()
        self.assertIn("-x = 1", text)
        self.assertIn("+x = 2", text)

    def test_diff_lands_at_canonical_experiment_path(self):
        root, parent_sha = _init_repo_with_workspace(self.root)
        worktree = _make_experiment_with_changes(
            root, "exp_0003", parent_sha,
            {"data.py": "data = [1, 2, 3]\n"},
        )
        node, graph = self._node_and_graph("exp_0003", worktree, "root", parent_sha)

        result = _capture_discard_time_diff(root, "exp_0003", node, graph)

        from evo.core import experiment_result_path
        expected = experiment_result_path(root, "exp_0003").parent / "diff.patch"
        self.assertEqual(result, expected)

    # ---- skip cases: returns None, no raise ----

    def test_returns_none_when_worktree_missing(self):
        # Discard runs against an already-cleaned-up worktree path.
        root, parent_sha = _init_repo_with_workspace(self.root)
        node, graph = self._node_and_graph(
            "exp_0004", Path("/nonexistent/worktree"), "root", parent_sha,
        )
        result = _capture_discard_time_diff(root, "exp_0004", node, graph)
        self.assertIsNone(result)

    def test_returns_none_when_parent_ref_missing(self):
        # Graph has the experiment but no parent commit recorded.
        root, parent_sha = _init_repo_with_workspace(self.root)
        worktree = _make_experiment_with_changes(
            root, "exp_0005", parent_sha,
            {"foo.py": "x = 1\n"},
        )
        node = {
            "id": "exp_0005", "parent": "ghost_parent", "children": [],
            "worktree": str(worktree),
        }
        # No `commit` or `branch` on the parent -- no ref to diff against.
        graph = {"nodes": {"ghost_parent": {"id": "ghost_parent"}, "exp_0005": node}}
        result = _capture_discard_time_diff(root, "exp_0005", node, graph)
        self.assertIsNone(result)

    def test_returns_none_when_node_has_no_parent_id(self):
        root, parent_sha = _init_repo_with_workspace(self.root)
        worktree = _make_experiment_with_changes(
            root, "exp_0006", parent_sha,
            {"foo.py": "x = 1\n"},
        )
        node = {"id": "exp_0006", "parent": None, "worktree": str(worktree)}
        graph = {"nodes": {"exp_0006": node}}
        result = _capture_discard_time_diff(root, "exp_0006", node, graph)
        self.assertIsNone(result)

    # ---- failure isolation: discard never blocks ----

    def test_swallows_unexpected_exception(self):
        # Pass a graph that will trigger an exception inside capture
        # (e.g. parent_ref pointing at a non-existent SHA).
        root, parent_sha = _init_repo_with_workspace(self.root)
        worktree = _make_experiment_with_changes(
            root, "exp_0007", parent_sha,
            {"foo.py": "x = 1\n"},
        )
        node = {
            "id": "exp_0007", "parent": "broken_parent", "worktree": str(worktree),
        }
        graph = {
            "nodes": {
                "broken_parent": {"commit": "deadbeefdeadbeefdeadbeefdeadbeefdeadbeef"},
                "exp_0007": node,
            }
        }
        # Should not raise -- discard observability is best-effort.
        result = _capture_discard_time_diff(root, "exp_0007", node, graph)
        self.assertIsNone(result)


if __name__ == "__main__":
    unittest.main()

"""Unit tests for the concurrent-attempt guard and `evo abort`.

The guard stamps the driver PID into attempt_state.json on the initial
write. A second `evo run` invocation reads it and refuses if the PID is
alive. `evo abort <exp_id>` reads the same PID and SIGTERMs it (with
SIGKILL escalation after --timeout).

Real subprocesses, real filesystem. The "live PID" used in tests is
this test process itself — guaranteed alive for the duration of the
check. A dead PID is simulated by stamping the highest unused PID and
hoping it stays dead for the few milliseconds the check takes (we use
99999999 — far above any real PID range).

Run: pytest tests/unit/test_concurrent_run_guard.py -v
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


def _make_active_node_with_pid(root: Path, exp_id: str, attempt: int, pid: int) -> None:
    """Inject an `active` node with attempt_state.json carrying `pid`.

    Bypasses the real run lifecycle — we only need the persisted state
    shape so the guard / abort can read it.
    """
    from evo.core import load_graph, save_graph, attempt_dir
    from evo.cli import _write_attempt_state
    graph = load_graph(root)
    graph["nodes"][exp_id] = {
        "id": exp_id, "parent": "root", "children": [],
        "status": "active", "score": None, "hypothesis": "guard-test",
        "branch": f"evo/run_0000/{exp_id}", "commit": None,
        "current_attempt": attempt,
        # worktree must be present and exist (the guard runs before
        # worktree usage so a stub path is fine for the unit test)
        "worktree": str(root / "fake-worktree"),
    }
    (root / "fake-worktree").mkdir(exist_ok=True)
    graph["nodes"]["root"]["children"] = [exp_id]
    save_graph(root, graph)
    attempt_dir(root, exp_id, attempt).mkdir(parents=True, exist_ok=True)
    _write_attempt_state(
        root, exp_id, attempt,
        phase="initializing", status="running",
        started_at="2026-01-01T00:00:00+00:00",
        extra={"pid": pid},
    )


class TestPidStampedInAttemptState(unittest.TestCase):
    """The PID stamp is the foundation of both the guard and abort. Verify
    the write actually persists the field — covers the schema contract
    without needing a real `evo run`."""

    def test_write_attempt_state_persists_pid(self):
        from evo.cli import _write_attempt_state, _read_attempt_state
        from evo.core import attempt_dir
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            _make_workspace(root)
            attempt_dir(root, "exp_0000", 1).mkdir(parents=True, exist_ok=True)
            _write_attempt_state(
                root, "exp_0000", 1,
                phase="initializing", status="running",
                started_at="2026-01-01T00:00:00+00:00",
                extra={"pid": 12345},
            )
            state = _read_attempt_state(root, "exp_0000", 1)
            self.assertEqual(state["pid"], 12345)


class TestCmdAbort(unittest.TestCase):
    """cmd_abort reads the stamped PID and signals it. Tests use a child
    sleep subprocess as the kill target — that's a real process under our
    control whose lifecycle we can observe."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name).resolve()
        _make_workspace(self.root)
        self._prev_cwd = Path.cwd()
        os.chdir(self.root)

    def tearDown(self):
        os.chdir(self._prev_cwd)
        self._tmp.cleanup()

    def _run_abort(self, exp_id: str, timeout: float = 2.0, force: bool = False) -> tuple[int, str, str]:
        from evo.cli import cmd_abort
        out = io.StringIO()
        err = io.StringIO()
        with patch("sys.stdout", out), patch("sys.stderr", err):
            rc = cmd_abort(argparse.Namespace(
                exp_id=exp_id, timeout=timeout, force=force,
            ))
        return rc, out.getvalue(), err.getvalue()

    def test_abort_sigterms_live_driver(self):
        """Spawn a sleep child, stamp its PID, abort it. Verify the
        signal reached the process (it exited) and abort reports the PID.
        Whether it exits on SIGTERM or gets escalated to SIGKILL after
        the grace period depends on macOS zombie reaping timing; both
        outcomes mean the abort succeeded."""
        # Portable long-lived child (no dependency on a `sleep` binary,
        # which Windows lacks).
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
        try:
            _make_active_node_with_pid(self.root, "exp_0001", 1, child.pid)
            rc, out, _ = self._run_abort("exp_0001", timeout=2.0)
            child.wait(timeout=3)  # reap the zombie
            self.assertEqual(rc, 0)
            self.assertIn(str(child.pid), out)
            self.assertIn("SIGTERM sent", out)
            self.assertIsNotNone(child.poll(), "child should have exited")
        finally:
            if child.poll() is None:
                child.kill()
                child.wait()

    def test_abort_noop_when_node_not_active(self):
        """Non-active node → friendly no-op, rc=0, no signal sent."""
        from evo.core import load_graph, save_graph
        graph = load_graph(self.root)
        graph["nodes"]["exp_0002"] = {
            "id": "exp_0002", "parent": "root", "children": [],
            "status": "committed", "score": 0.5,
        }
        graph["nodes"]["root"]["children"] = ["exp_0002"]
        save_graph(self.root, graph)
        rc, _, err = self._run_abort("exp_0002")
        self.assertEqual(rc, 0)
        self.assertIn("not active", err)

    def test_abort_errors_when_no_pid_stamped(self):
        """Active node with no PID in attempt_state → useful error."""
        _make_active_node_with_pid(self.root, "exp_0003", 1, 0)  # 0 = unstamped
        rc, _, err = self._run_abort("exp_0003")
        self.assertEqual(rc, 1)
        self.assertIn("no driver PID", err)

    def test_abort_clean_when_pid_already_dead(self):
        """A pre-dead PID — abort reports it and exits 0 (no signal sent)."""
        # Spawn a process that exits immediately to get a guaranteed-dead PID
        # (portable; no dependency on a `true` binary).
        child = subprocess.Popen([sys.executable, "-c", ""])
        child.wait()
        dead_pid = child.pid
        _make_active_node_with_pid(self.root, "exp_0004", 1, dead_pid)
        rc, out, _ = self._run_abort("exp_0004")
        self.assertEqual(rc, 0)
        self.assertIn("already not alive", out)


class TestIsPidAlive(unittest.TestCase):
    """_is_pid_alive must be correct on every OS. On Windows os.kill(pid, 0)
    is CTRL_C_EVENT, not a liveness probe, so the helper has a dedicated
    native path — exercise both live and dead PIDs here on whatever OS runs."""

    def test_live_process_reports_alive(self):
        from evo.cli import _is_pid_alive
        self.assertTrue(_is_pid_alive(os.getpid()))

    def test_dead_process_reports_not_alive(self):
        from evo.cli import _is_pid_alive
        child = subprocess.Popen([sys.executable, "-c", ""])
        child.wait()
        self.assertFalse(_is_pid_alive(child.pid))

    def test_nonpositive_pid_is_not_alive(self):
        from evo.cli import _is_pid_alive
        self.assertFalse(_is_pid_alive(0))
        self.assertFalse(_is_pid_alive(-1))


class TestSdkActiveRunsRegistry(unittest.TestCase):
    """Belt-and-suspenders against in-process double-Run. The harness-level
    PID guard catches the cross-invocation case; this catches a benchmark
    that accidentally instantiates two Run() in the same process."""

    def test_second_run_with_same_experiment_id_raises(self):
        sys.path.insert(0, str(REPO_ROOT / "sdk" / "python" / "src"))
        from evo_agent import Run
        from evo_agent._run import _ACTIVE_RUNS
        # Isolate from any leakage by clearing the registry first.
        _ACTIVE_RUNS.clear()
        run1 = Run(experiment_id="exp-concurrent")
        try:
            with self.assertRaises(RuntimeError) as ctx:
                Run(experiment_id="exp-concurrent")
            self.assertIn("already active", str(ctx.exception))
        finally:
            run1.finish()

    def test_finish_releases_slot(self):
        sys.path.insert(0, str(REPO_ROOT / "sdk" / "python" / "src"))
        from evo_agent import Run
        from evo_agent._run import _ACTIVE_RUNS
        _ACTIVE_RUNS.clear()
        run1 = Run(experiment_id="exp-finish-then-rerun")
        run1.finish()
        # Slot must be free now — a second Run() is allowed.
        run2 = Run(experiment_id="exp-finish-then-rerun")
        run2.finish()


if __name__ == "__main__":
    unittest.main()

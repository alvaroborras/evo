"""Integration tests for the dashboard supervisor.

The supervisor is a subprocess that owns the Flask dashboard's lifecycle:
captures its stdout/stderr to a rotated log, respawns on unexpected
exits with capped backoff. These tests spawn the real supervisor as a
subprocess and verify the externally-observable contract (log files
written, PID files managed, signal-based shutdown cleans up).

Real subprocesses, real filesystem. No mocks — that's the same
discipline as test_evo_wait.py.

Run: pytest tests/unit/test_dashboard_supervisor.py -v
"""

from __future__ import annotations

import os
import signal
import socket
import subprocess
import sys
import tempfile
import time
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


def _pick_free_port() -> int:
    """Bind to port 0, read the assigned port, close. Race window with
    other processes is small enough for test use."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _wait_for_file(path: Path, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.exists():
            return True
        time.sleep(0.1)
    return False


def _wait_for(predicate, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.1)
    return False


class TestSupervisorLifecycle(unittest.TestCase):
    """Spawn supervisor, verify it spawns dashboard + writes logs + cleans
    up on SIGTERM. Each test gets a fresh workspace + port so they can
    run in parallel."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name).resolve()
        _make_workspace(self.root)
        self.port = _pick_free_port()
        self._sup_proc: subprocess.Popen | None = None

    def tearDown(self):
        if self._sup_proc and self._sup_proc.poll() is None:
            self._sup_proc.terminate()
            try:
                self._sup_proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self._sup_proc.kill()
        self._tmp.cleanup()

    def _start_supervisor(self) -> subprocess.Popen:
        env = {
            **os.environ,
            "EVO_DASHBOARD_PORT": str(self.port),
            "EVO_SUPERVISOR_ROOT": str(self.root),
        }
        proc = subprocess.Popen(
            [sys.executable, "-m", "evo.dashboard_supervisor"],
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        self._sup_proc = proc
        return proc

    def test_supervisor_writes_pid_files_and_dashboard_log(self):
        """Externally observable contract: supervisor.pid + dashboard.pid
        appear, dashboard.log captures Flask startup banner."""
        sup = self._start_supervisor()
        edir = self.root / ".evo"
        self.assertTrue(_wait_for_file(edir / "supervisor.pid"))
        self.assertTrue(_wait_for_file(edir / "dashboard.pid"))
        self.assertTrue(_wait_for_file(edir / "dashboard.log"))
        # The Flask dev-server banner is the canonical "I am up" signal.
        # We don't pin the exact bytes (depends on Flask version) — just
        # confirm something was captured.
        self.assertTrue(_wait_for(
            lambda: (edir / "dashboard.log").stat().st_size > 0
        ))
        sup.terminate()
        sup.wait(timeout=5)

    def test_sigterm_cleans_up_pid_files(self):
        """SIGTERM to supervisor → child terminated → finally block runs
        → pid files removed. The lock file stays (it's the file the
        portalocker context manager held; flock auto-released)."""
        sup = self._start_supervisor()
        edir = self.root / ".evo"
        self.assertTrue(_wait_for_file(edir / "supervisor.pid"))
        sup.send_signal(signal.SIGTERM)
        rc = sup.wait(timeout=10)
        self.assertEqual(rc, 0)
        self.assertFalse((edir / "supervisor.pid").exists(),
                         "supervisor.pid must be removed on clean shutdown")
        self.assertFalse((edir / "dashboard.pid").exists(),
                         "dashboard.pid must be removed on clean shutdown")

    def test_second_supervisor_refuses_when_first_is_running(self):
        """The advisory_lock guard means a second supervisor for the same
        workspace fails to acquire and exits non-zero. Without it, two
        supervisors would each spawn their own dashboard and fight for
        the same PID file."""
        sup1 = self._start_supervisor()
        edir = self.root / ".evo"
        self.assertTrue(_wait_for_file(edir / "supervisor.pid"))

        env = {
            **os.environ,
            "EVO_DASHBOARD_PORT": str(self.port),
            "EVO_SUPERVISOR_ROOT": str(self.root),
        }
        sup2 = subprocess.Popen(
            [sys.executable, "-m", "evo.dashboard_supervisor"],
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        rc2 = sup2.wait(timeout=10)
        self.assertNotEqual(rc2, 0,
                            "second supervisor must refuse (lock contention)")

        # First supervisor unaffected.
        self.assertIsNone(sup1.poll(), "first supervisor must keep running")
        sup1.terminate()
        sup1.wait(timeout=5)


class TestSupervisorBackoffOnCrashLoop(unittest.TestCase):
    """If the dashboard crashes immediately on every spawn, the supervisor
    must give up after RAPID_FAILURE_THRESHOLD failures and write the
    sentinel — not retry forever."""

    def test_dead_sentinel_written_on_crash_loop(self):
        """Force the dashboard to crash on startup by pointing its port
        to a value that fails to bind. The supervisor should exit and
        write `.evo/dashboard.dead`."""
        with tempfile.TemporaryDirectory() as d:
            root = Path(d).resolve()
            _make_workspace(root)
            # Bind to a privileged port (low number, will fail without root)
            # to force the dashboard to crash on every spawn. Use a port
            # that's guaranteed-busy (we hold a listening socket on it).
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as blocker:
                blocker.bind(("127.0.0.1", 0))
                blocker.listen(1)
                port = blocker.getsockname()[1]
                env = {
                    **os.environ,
                    "EVO_DASHBOARD_PORT": str(port),
                    "EVO_SUPERVISOR_ROOT": str(root),
                }
                # Override backoff to make the test quick — set the
                # schedule to all-zero via a tiny monkey-patch wrapper.
                # Simpler: rely on the default schedule but bound the
                # supervisor wait. The default 1+2+4+8+16=31s for 5
                # rapid failures exceeds reasonable test budgets.
                # Instead, patch BACKOFF_SCHEDULE_SECONDS via env? Not
                # currently configurable. For now we just confirm the
                # supervisor eventually writes the sentinel — give it
                # generous time, kill the supervisor if it doesn't.
                sup = subprocess.Popen(
                    [sys.executable, "-m", "evo.dashboard_supervisor"],
                    env=env,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
                try:
                    # The supervisor will hit 5 failures inside 60s and
                    # exit with rc=2 + dashboard.dead sentinel.
                    # 1+2+4+8+16 = 31s of backoff + ~5x crash overhead.
                    # Cap the wait at 45s.
                    rc = sup.wait(timeout=45)
                    self.assertEqual(
                        rc, 2,
                        "crash-loop bailout must exit rc=2"
                    )
                    self.assertTrue(
                        (root / ".evo" / "dashboard.dead").exists(),
                        "dashboard.dead sentinel must be written on bailout"
                    )
                finally:
                    if sup.poll() is None:
                        sup.kill()
                        sup.wait(timeout=5)


if __name__ == "__main__":
    unittest.main()

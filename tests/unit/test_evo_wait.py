"""Unit tests for `evo wait` — blocking primitive for the orchestrator.

The orchestrator (in /optimize) spawns subagents in the background and
needs to wait for their results. Pre-v0.4.4 it would write ad-hoc bash
polling loops; v0.4.4 gives it `evo wait` as a first-class primitive.

Contract:
  - polls every 1s for `experiments/<id>/outcome.json` writes/updates
  - exits 0 + prints what changed on the first detected experiment
    conclusion
  - exits 124 (POSIX timeout convention) on timeout
  - --timeout defaults to 3600 (1h) and is capped at 3600 so an agent
    can't accidentally block forever
  - no --watch flag — fixed set of paths the orchestrator cares about

Real filesystem, real subprocess for `evo wait`, no implementation mocks.

Run: pytest tests/unit/test_evo_wait.py -v
"""

from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
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


class TestEvoWait(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name).resolve()
        self.run_dir = _make_workspace(self.root)
        self._old_cwd = Path.cwd()
        os.chdir(self.root)

    def tearDown(self):
        os.chdir(self._old_cwd)
        self._tmp.cleanup()

    def _run_wait(self, timeout: float, expect_rc: int | None = None) -> tuple[int, str]:
        """Run cmd_wait directly (in-process) and capture output."""
        from evo.cli import cmd_wait
        import argparse
        buf = io.StringIO()
        with patch("sys.stdout", buf):
            rc = cmd_wait(argparse.Namespace(timeout=timeout))
        return rc, buf.getvalue()

    def test_returns_124_on_timeout_with_no_changes(self):
        rc, out = self._run_wait(timeout=0.3)
        self.assertEqual(rc, 124, f"want 124 (timeout), got {rc}; output: {out!r}")
        self.assertTrue(
            "timed out" in out.lower() or "timeout" in out.lower(),
            f"output must mention timeout; got: {out!r}",
        )

    def test_returns_0_when_outcome_json_appears_during_wait(self):
        result: dict = {}

        def writer() -> None:
            time.sleep(0.5)  # let wait start polling
            exp_dir = self.run_dir / "experiments" / "exp_0042"
            exp_dir.mkdir(parents=True, exist_ok=True)
            (exp_dir / "outcome.json").write_text(json.dumps({"score": 1.0}))

        t = threading.Thread(target=writer, daemon=True)
        t.start()
        rc, out = self._run_wait(timeout=10.0)
        t.join(timeout=2)

        self.assertEqual(rc, 0, f"want 0 on change, got {rc}; output: {out!r}")
        self.assertIn("exp_0042", out, f"output must name the changed experiment: {out!r}")

    def test_returns_0_when_outcome_json_updates(self):
        """Existing outcome.json mtime changes (e.g., re-run committing
        new attempt) should also wake the wait."""
        exp_dir = self.run_dir / "experiments" / "exp_0001"
        exp_dir.mkdir(parents=True, exist_ok=True)
        outcome = exp_dir / "outcome.json"
        outcome.write_text(json.dumps({"score": 0.5}))
        # Backdate the mtime so the baseline doesn't immediately register
        old_t = time.time() - 60
        os.utime(outcome, (old_t, old_t))

        def updater() -> None:
            time.sleep(0.5)
            outcome.write_text(json.dumps({"score": 0.8}))

        t = threading.Thread(target=updater, daemon=True)
        t.start()
        rc, out = self._run_wait(timeout=10.0)
        t.join(timeout=2)

        self.assertEqual(rc, 0)
        self.assertIn("exp_0001", out)

    def test_timeout_capped_at_24h(self):
        """A user/agent passing a too-large --timeout is silently capped to 24h.
        Confirmed via the cmd_wait function caps the value internally; we
        don't actually wait — we just verify the cap logic. Cap was 1h in
        v0.4.x; raised to 24h alongside the process/log-growth/gpu watch
        extension so long external waits (a 10h training run) are expressible."""
        from evo.cli import _wait_timeout_seconds, _WAIT_TIMEOUT_CAP
        self.assertEqual(_WAIT_TIMEOUT_CAP, 24 * 3600)
        self.assertEqual(_wait_timeout_seconds(3600), 3600)
        self.assertEqual(_wait_timeout_seconds(86400), 86400)
        self.assertEqual(_wait_timeout_seconds(99999), 86400)   # capped
        self.assertEqual(_wait_timeout_seconds(-5), 1)          # min floor 1s
        self.assertEqual(_wait_timeout_seconds(0), 1)
        self.assertEqual(_wait_timeout_seconds(120), 120)

    def test_no_evo_workspace_returns_error(self):
        """Run outside an evo workspace — should fail cleanly, not hang."""
        import argparse
        from evo.cli import cmd_wait
        with tempfile.TemporaryDirectory() as elsewhere:
            os.chdir(elsewhere)
            try:
                rc = cmd_wait(argparse.Namespace(timeout=10.0))
                self.assertNotEqual(rc, 0, "must fail outside workspace")
            finally:
                os.chdir(self.root)

    def test_non_workspace_watches_work_outside_workspace(self):
        """process / log-growth / gpu-* watches don't read or write .evo/ —
        running them outside an evo workspace must succeed, not bail with
        the workspace-required error.

        Regression guard for issue #53. Uses --for log-growth (no zombie /
        reaping subtleties like --for process=<child-pid> has under pytest).
        """
        import argparse
        from evo.cli import cmd_wait
        with tempfile.TemporaryDirectory() as elsewhere:
            os.chdir(elsewhere)
            try:
                # log file exists, never grows -> stalls after threshold
                log = Path(elsewhere) / "test.log"
                log.write_text("static\n")
                out_buf = io.StringIO()
                err_buf = io.StringIO()
                with patch("sys.stdout", out_buf), patch("sys.stderr", err_buf):
                    rc = cmd_wait(argparse.Namespace(
                        wait_for=[f"log-growth={log}"],
                        count=None, timeout=6.0, stall_threshold=2,
                        poll_interval=1, json_out=True,
                    ))
                self.assertEqual(
                    rc, 0,
                    f"log-growth watch outside workspace must succeed; "
                    f"got {rc}; stderr={err_buf.getvalue()!r}",
                )
                self.assertIn("log-stalled", out_buf.getvalue())
                # Specifically, must not have bailed with the workspace error.
                self.assertNotIn("not in an evo workspace", err_buf.getvalue())
            finally:
                os.chdir(self.root)

    def test_bare_experiment_dir_creation_does_not_wake_wait(self):
        """A bare `mkdir experiments/<id>/` (subagent allocating a worktree
        before benchmarking) must NOT wake the wait. The orchestrator only
        cares about terminal transitions; per-task traces, attempt-state
        pings, and pre-benchmark dir creation are in-flight noise. (The
        snapshot tracks outcome.json files, not bare dirs.)"""
        def starter() -> None:
            time.sleep(0.5)
            (self.run_dir / "experiments" / "exp_0099").mkdir(parents=True)

        t = threading.Thread(target=starter, daemon=True)
        t.start()
        rc, _ = self._run_wait(timeout=2.0)
        t.join(timeout=2)

        self.assertEqual(rc, 124, "bare dir creation is in-flight noise; must time out, not wake")

    def test_discarded_experiment_wakes_wait_and_names_it(self):
        """`evo discard` deletes the experiment dir wholesale, so its
        outcome.json key disappears from the snapshot. wait must wake on
        that deletion (terminal transition) and the summary must name the
        discarded exp_id — not fall through to a generic message."""
        exp_dir = self.run_dir / "experiments" / "exp_0007"
        exp_dir.mkdir(parents=True, exist_ok=True)
        outcome = exp_dir / "outcome.json"
        outcome.write_text(json.dumps({"outcome": "committed", "score": 0.4}))
        # Backdate so the baseline picks it up before we delete.
        old_t = time.time() - 60
        os.utime(outcome, (old_t, old_t))

        def discarder() -> None:
            time.sleep(0.5)
            import shutil
            shutil.rmtree(exp_dir)

        t = threading.Thread(target=discarder, daemon=True)
        t.start()
        rc, out = self._run_wait(timeout=10.0)
        t.join(timeout=2)

        self.assertEqual(rc, 0, f"discard must wake the wait; got {rc}, out={out!r}")
        self.assertIn("exp_0007", out,
                      "summary must name the discarded experiment, not "
                      "fall through to a generic 'experiments dir changed'")
        self.assertIn("discarded", out.lower(),
                      "summary must say the transition type so the orchestrator "
                      "can branch on it without parsing the exp dir state")


class TestEvoWaitDefaultWatchesBoth(unittest.TestCase):
    """No --for: wait watches BOTH experiments and ideators, wakes on
    whichever changes first. Existing test_returns_0_when_outcome_json_*
    cases (TestEvoWait) cover the experiment side of this -- here we
    cover that the default also wakes on ideator proposals AND that
    --count > 1 without --for is rejected."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name).resolve()
        self.run_dir = _make_workspace(self.root)
        (self.run_dir / "ideator").mkdir(parents=True, exist_ok=True)
        self.proposals = self.run_dir / "ideator" / "proposals.jsonl"
        self._old_cwd = Path.cwd()
        os.chdir(self.root)

    def tearDown(self):
        os.chdir(self._old_cwd)
        self._tmp.cleanup()

    def _run_wait(self, **kwargs) -> tuple[int, str, str]:
        """Run cmd_wait with kwargs as the namespace; capture stdout AND stderr."""
        from evo.cli import cmd_wait
        import argparse
        ns_kwargs = {"wait_for": None, "count": None, "timeout": 1.0}
        ns_kwargs.update(kwargs)
        out_buf = io.StringIO()
        err_buf = io.StringIO()
        with patch("sys.stdout", out_buf), patch("sys.stderr", err_buf):
            rc = cmd_wait(argparse.Namespace(**ns_kwargs))
        return rc, out_buf.getvalue(), err_buf.getvalue()

    def test_default_wakes_on_ideator_proposal_too(self):
        """No --for: ideator proposal landing should wake the default wait."""
        def appender() -> None:
            time.sleep(0.4)
            with self.proposals.open("a") as f:
                f.write('{"brief":"x","hypothesis":"y"}\n')

        t = threading.Thread(target=appender, daemon=True)
        t.start()
        rc, out, err = self._run_wait(timeout=5.0)
        t.join(timeout=2)
        self.assertEqual(rc, 0, f"default wait must wake on proposal; got {rc}")
        self.assertIn("ideator proposal", out)

    def test_default_wakes_on_experiment_outcome_too(self):
        """Existing experiment-wake path still works under the new default."""
        def writer() -> None:
            time.sleep(0.4)
            exp_dir = self.run_dir / "experiments" / "exp_0099"
            exp_dir.mkdir(parents=True, exist_ok=True)
            (exp_dir / "outcome.json").write_text(json.dumps({"score": 1.0}))

        t = threading.Thread(target=writer, daemon=True)
        t.start()
        rc, out, err = self._run_wait(timeout=5.0)
        t.join(timeout=2)
        self.assertEqual(rc, 0)
        self.assertIn("exp_0099", out)

    def test_default_timeout_message_names_both_sources(self):
        rc, out, err = self._run_wait(timeout=0.3)
        self.assertEqual(rc, 124)
        # message should mention both subjects so the agent knows what was watched
        self.assertIn("experiment or ideator", out)

    def test_count_without_for_is_rejected(self):
        """--count > 1 without --for is ambiguous; CLI must reject."""
        rc, out, err = self._run_wait(count=3, timeout=0.3)
        self.assertEqual(rc, 2, f"want 2 (usage error); got {rc}; err={err!r}")
        self.assertIn("--count > 1 requires exactly one --for", err)

    def test_count_1_without_for_is_allowed(self):
        """--count=1 (or unset) is the default; should still work without --for."""
        rc, out, err = self._run_wait(count=1, timeout=0.3)
        self.assertEqual(rc, 124, f"timeout expected; got {rc}; out={out!r}")


class TestEvoWaitForIdeators(unittest.TestCase):
    """The --for ideators path: orchestrator blocks on proposals.jsonl
    line growth instead of experiment-dir activity. Each ideator subagent
    appends ALL its proposals in one final write at the end of its run,
    so line growth IS the completion signal."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name).resolve()
        self.run_dir = _make_workspace(self.root)
        (self.run_dir / "ideator").mkdir(parents=True, exist_ok=True)
        self.proposals = self.run_dir / "ideator" / "proposals.jsonl"
        self._old_cwd = Path.cwd()
        os.chdir(self.root)

    def tearDown(self):
        os.chdir(self._old_cwd)
        self._tmp.cleanup()

    def _run_wait(self, *, count: int = 1, timeout: float = 1.0) -> tuple[int, str]:
        from evo.cli import cmd_wait
        import argparse
        buf = io.StringIO()
        with patch("sys.stdout", buf):
            # `--for` is now action="append"; the namespace value is a list.
            # Passing a bare string here would be iterated character-by-character
            # by the new parser ('i', 'd', 'e', ...), failing as unknown form.
            rc = cmd_wait(argparse.Namespace(
                wait_for=["ideators"], count=count, timeout=timeout,
            ))
        return rc, buf.getvalue()

    def test_timeout_when_no_proposals_arrive(self):
        rc, out = self._run_wait(count=1, timeout=0.3)
        self.assertEqual(rc, 124, f"want 124 (timeout), got {rc}; output: {out!r}")
        self.assertIn("ideators", out)
        self.assertIn("timed out", out)

    def test_returns_0_when_proposals_appended_during_wait(self):
        def appender() -> None:
            time.sleep(0.4)
            with self.proposals.open("a") as f:
                f.write('{"brief":"failure_analysis","hypothesis":"x"}\n')

        t = threading.Thread(target=appender, daemon=True)
        t.start()
        rc, out = self._run_wait(count=1, timeout=5.0)
        t.join(timeout=2)
        self.assertEqual(rc, 0, f"want 0 on proposal arrival; got {rc}; out={out!r}")
        self.assertIn("1 new ideator proposal", out)

    def test_count_3_returns_only_after_third_proposal(self):
        """With --count 3, single-line bumps should NOT return; only the
        third bump satisfies the wait."""
        def appender() -> None:
            for i in range(3):
                time.sleep(0.3)
                with self.proposals.open("a") as f:
                    f.write(f'{{"brief":"b{i}","hypothesis":"x{i}"}}\n')

        t = threading.Thread(target=appender, daemon=True)
        t.start()
        rc, out = self._run_wait(count=3, timeout=5.0)
        t.join(timeout=2)
        self.assertEqual(rc, 0, f"want 0 after 3rd proposal; got {rc}; out={out!r}")
        self.assertIn("3 new ideator proposal", out)

    def test_count_3_partial_progress_on_timeout(self):
        """If only 1 of 3 expected proposals lands before timeout, exit 124
        but surface partial progress so the caller can decide to proceed."""
        def appender() -> None:
            time.sleep(0.2)
            with self.proposals.open("a") as f:
                f.write('{"brief":"b","hypothesis":"x"}\n')

        t = threading.Thread(target=appender, daemon=True)
        t.start()
        rc, out = self._run_wait(count=3, timeout=1.0)
        t.join(timeout=2)
        self.assertEqual(rc, 124, f"want 124 (timeout w/ partial); got {rc}; out={out!r}")
        self.assertIn("partial", out, f"timeout output must surface partial count: {out!r}")
        self.assertIn("1/3", out, f"output must show partial vs target count: {out!r}")

    def test_baseline_existing_lines_dont_satisfy_wait(self):
        """If proposals.jsonl already has 2 lines when wait starts, that
        baseline must NOT satisfy --count 1 -- only NEW additions count."""
        with self.proposals.open("w") as f:
            f.write('{"brief":"old1","hypothesis":"x"}\n')
            f.write('{"brief":"old2","hypothesis":"x"}\n')

        rc, out = self._run_wait(count=1, timeout=0.3)
        self.assertEqual(rc, 124,
            f"existing lines must be baseline-only; want 124, got {rc}; out={out!r}")

    def test_existing_baseline_plus_new_proposal_wakes(self):
        """Baseline at 2 lines; ideator adds a 3rd line; wait --count 1 returns."""
        with self.proposals.open("w") as f:
            f.write('{"brief":"old1","hypothesis":"x"}\n')
            f.write('{"brief":"old2","hypothesis":"x"}\n')

        def appender() -> None:
            time.sleep(0.4)
            with self.proposals.open("a") as f:
                f.write('{"brief":"new","hypothesis":"y"}\n')

        t = threading.Thread(target=appender, daemon=True)
        t.start()
        rc, out = self._run_wait(count=1, timeout=5.0)
        t.join(timeout=2)
        self.assertEqual(rc, 0, f"new proposal beyond baseline must wake; out={out!r}")
        self.assertIn("1 new ideator proposal", out)


if __name__ == "__main__":
    unittest.main()

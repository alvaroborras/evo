"""End-to-end test for `evo direct` directive delivery (#58).

The queue side was validated by alpha.7 #49 (fanout=1 on engaged sessions).
The delivery side -- hook fires -> drain reads events -> splices
[EVO DIRECTIVE id=...] banner into hook response -> writes
delivered/<id>-<sid>.json -- was never tested. Existing tests in
test_hook_drain.py use a fake evo-drain shim that just prints {}, so the
real drain.py codepath has zero coverage.

This module exercises the full pipeline: cmd_direct (queue) ->
real Rust hook-drain binary (handoff) -> real Python drain_session
(delivery). Failures here pinpoint where the chain breaks.
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
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "plugins" / "evo" / "src"))

HOOK_NAME = "evo-hook-drain.exe" if sys.platform == "win32" else "evo-hook-drain"
HOOK_PATH = REPO_ROOT / "plugins" / "evo" / "bin" / HOOK_NAME


def _init_workspace(root: Path) -> str:
    """Init a minimal evo workspace at `root`. Returns the run id."""
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.email", "t@evo"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.name", "T"], cwd=root, check=True)
    subprocess.run(["git", "config", "commit.gpgsign", "false"], cwd=root, check=True)
    (root / "agent.py").write_text("x = 1\n")
    subprocess.run(["git", "add", "-A"], cwd=root, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=root, check=True)

    from evo.core import init_workspace
    return init_workspace(
        root, target="agent.py", benchmark="echo hi",
        metric="max", gate=None, host="claude-code",
    )


def _register_engaged_session(root: Path, sid: str) -> None:
    """Register a session as engaged (the post-baseline normal state)."""
    from evo.inject.registry import register_session
    register_session(root, sid, "claude-code", engage=True)


def _queue_directive(root: Path, text: str) -> str:
    """Run cmd_direct programmatically. Returns event id."""
    from evo.cli import cmd_direct
    args = argparse.Namespace(args=[text], wait=False, wait_timeout=60)
    prev_cwd = Path.cwd()
    os.chdir(root)
    out = io.StringIO()
    try:
        with redirect_stdout(out):
            cmd_direct(args)
    finally:
        os.chdir(prev_cwd)
    # cmd_direct prints "directive queued (id=<EVENTID>, fanout=N ...)"
    text_out = out.getvalue()
    # Pull id out
    import re
    m = re.search(r"id=([A-F0-9]+)", text_out)
    assert m, f"expected directive id in output: {text_out!r}"
    return m.group(1)


def _run_hook(cwd: Path, payload: dict) -> subprocess.CompletedProcess:
    """Invoke the real hook-drain binary with the given payload."""
    return subprocess.run(
        [str(HOOK_PATH)],
        input=json.dumps(payload).encode("utf-8"),
        cwd=str(cwd),
        capture_output=True,
        timeout=15,
    )


class TestDirectiveDeliveryE2E(unittest.TestCase):
    """Integration: queue a directive, fire a hook, verify banner + delivered file."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name).resolve()
        self.run_id = _init_workspace(self.root)
        # `run_id` is already the full dir name (e.g. "run_0000").
        self.run_dir = self.root / ".evo" / self.run_id
        # Use the path helpers for inject subdirs so we always match what
        # registry/queue/marker write to.
        from evo.inject.paths import sessions_dir, markers_dir, events_dir, delivered_dir
        self.sessions_dir = sessions_dir(self.root)
        self.markers_dir = markers_dir(self.root)
        self.events_dir = events_dir(self.root)
        self.delivered_dir = delivered_dir(self.root)
        self.sid = "test-session-e2e-001"
        _register_engaged_session(self.root, self.sid)

    def tearDown(self):
        self._tmp.cleanup()

    # ---- queue side (should pass since alpha.7 #49) ----

    def test_cmd_direct_writes_event_to_workspace_jsonl(self):
        event_id = _queue_directive(self.root, "test directive A")
        events_file = self.events_dir / "workspace.jsonl"
        self.assertTrue(events_file.exists(), f"workspace.jsonl missing")
        content = events_file.read_text()
        self.assertIn(event_id, content)
        self.assertIn("test directive A", content)

    def test_cmd_direct_touches_marker_for_engaged_session(self):
        _queue_directive(self.root, "test directive B")
        marker = self.markers_dir / f"{self.sid}.flag"
        self.assertTrue(marker.exists(),
                        f"marker not touched at {marker} -- queue side failed")

    # ---- delivery side (the bug we're investigating) ----

    def test_hook_fires_after_directive_queued_emits_banner(self):
        """The core integration: queue -> hook fires -> banner in response."""
        event_id = _queue_directive(self.root, "course correction text")

        # Sanity-check queue state
        marker = self.markers_dir / f"{self.sid}.flag"
        self.assertTrue(marker.exists(), "marker not written by cmd_direct")

        # Fire a PostToolUse hook (the most common trigger)
        payload = {
            "hook_event_name": "PostToolUse",
            "session_id": self.sid,
            "tool_name": "Bash",
            "tool_input": {"command": "evo status"},
        }
        proc = _run_hook(self.root, payload)

        # The hook should hand off to drain.py, which should emit the
        # banner in the additionalContext envelope.
        self.assertEqual(proc.returncode, 0,
                         f"hook nonzero exit: stderr={proc.stderr.decode(errors='replace')}")
        stdout = proc.stdout.decode("utf-8", errors="replace")

        # Diagnostic on failure: dump full state
        diag = (
            f"\nstdout={stdout!r}\nstderr={proc.stderr.decode(errors='replace')!r}\n"
            f"events={list(self.events_dir.glob('*'))}\n"
            f"delivered={list(self.delivered_dir.glob('*')) if self.delivered_dir.exists() else 'NO DIR'}\n"
            f"marker_after={marker.exists()}\n"
        )
        self.assertIn("EVO DIRECTIVE", stdout,
                      f"banner not in hook response -- delivery broken.{diag}")
        self.assertIn(event_id, stdout,
                      f"event id {event_id} not in hook response.{diag}")
        self.assertIn("course correction text", stdout,
                      f"directive text not in hook response.{diag}")

    def test_delivered_file_written_after_hook_fires(self):
        event_id = _queue_directive(self.root, "delivery marker test")
        payload = {
            "hook_event_name": "PostToolUse",
            "session_id": self.sid,
            "tool_name": "Bash",
            "tool_input": {"command": "evo status"},
        }
        proc = _run_hook(self.root, payload)
        self.assertEqual(proc.returncode, 0)
        self.assertTrue(self.delivered_dir.exists(),
                        f"delivered/ dir not created after hook fire")
        delivered_files = list(self.delivered_dir.glob("*"))
        self.assertGreater(len(delivered_files), 0,
                           f"no delivery record written after hook -- delivery side broken. "
                           f"stdout={proc.stdout!r}")
        # At least one of them should mention our event id
        all_content = "\n".join(p.read_text() for p in delivered_files if p.is_file())
        self.assertIn(event_id, all_content,
                      f"event id {event_id} not in any delivery record")

    def test_marker_unlinked_after_successful_delivery(self):
        _queue_directive(self.root, "marker unlink test")
        marker = self.markers_dir / f"{self.sid}.flag"
        self.assertTrue(marker.exists())
        payload = {
            "hook_event_name": "PostToolUse",
            "session_id": self.sid,
            "tool_name": "Bash",
            "tool_input": {"command": "evo status"},
        }
        _run_hook(self.root, payload)
        # After successful delivery, marker should be unlinked so it
        # doesn't keep re-firing the drain.
        self.assertFalse(marker.exists(),
                         "marker not unlinked after delivery -- hook will re-fire")

    # ---- engagement filter (should already work via #49 fix) ----

    def test_unengaged_session_does_NOT_receive_directive(self):
        """If a session never engaged, the directive shouldn't fan out to it."""
        # Create a second, unengaged session
        unengaged_sid = "test-session-unengaged"
        from evo.inject.registry import register_session
        register_session(self.root, unengaged_sid, "claude-code", engage=False)

        _queue_directive(self.root, "should reach only engaged session")

        # Engaged session's marker SHOULD be touched
        engaged_marker = self.markers_dir / f"{self.sid}.flag"
        self.assertTrue(engaged_marker.exists())
        # Unengaged session's marker should NOT be touched
        unengaged_marker = self.markers_dir / f"{unengaged_sid}.flag"
        self.assertFalse(unengaged_marker.exists(),
                         "unengaged session should not have been woken")


if __name__ == "__main__":
    unittest.main()

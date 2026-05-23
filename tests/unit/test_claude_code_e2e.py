"""End-to-end live test: real evo-hook-drain (Rust) → real evo-drain (Python)
on a real workspace, exercising the full /evo:optimize lifecycle as
claude-code would.

Verifies the correctness fix from the optimize_mode flag-file work:
  - /evo:optimize via UserPromptSubmit arms the flag
  - PreToolUse Edit denies with the EVO POLICY banner
  - Stop emits the EVO LOOP envelope (decision:block, reason)
  - `evo exit-optimize-mode` clears the flag
  - PreToolUse Edit after exit returns {} (no deny)

Run: pytest tests/unit/test_claude_code_e2e.py -v
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
HOOK_PATH = REPO_ROOT / "plugins" / "evo" / "bin" / "evo-hook-drain"
EVO_DRAIN = shutil.which("evo-drain")


def _has_workspace_evo() -> bool:
    """The test needs the venv-installed evo-drain to be the one resolved
    on PATH (the editable install), not the system one which may be a
    stale release."""
    if not EVO_DRAIN:
        return False
    # Spot-check: the editable evo-drain should have --host accepted.
    r = subprocess.run([EVO_DRAIN, "--help"], capture_output=True, text=True)
    return "--host" in r.stdout


@unittest.skipIf(not HOOK_PATH.exists(), "Rust hook binary not built")
@unittest.skipIf(not _has_workspace_evo(),
                 "evo-drain on PATH lacks --host (likely old install)")
class TestClaudeCodeEndToEnd(unittest.TestCase):
    """Drive the full pipeline as the claude-code hook would.

    Uses the freshly built Rust hook (plugins/evo/bin/evo-hook-drain)
    and whatever evo-drain is on PATH. Real subprocesses; no mocks."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name).resolve()
        subprocess.run(["git", "init", "-q"], cwd=self.root, check=True)
        subprocess.run(["git", "config", "user.email", "t@evo"], cwd=self.root, check=True)
        subprocess.run(["git", "config", "user.name", "T"], cwd=self.root, check=True)
        subprocess.run(["git", "config", "commit.gpgsign", "false"], cwd=self.root, check=True)
        (self.root / "README.md").write_text("x\n")
        subprocess.run(["git", "add", "."], cwd=self.root, check=True)
        subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=self.root, check=True)
        # init workspace
        sys.path.insert(0, str(REPO_ROOT / "plugins" / "evo" / "src"))
        from evo.core import init_workspace
        init_workspace(
            self.root, target="agent.py", benchmark="python bench.py",
            metric="max", gate=None,
        )

    def tearDown(self):
        self._tmp.cleanup()

    def _fire(self, payload: dict) -> tuple[int, str]:
        """Pipe `payload` (as JSON) into the Rust hook from cwd=self.root.
        Returns (exit_code, stdout_text)."""
        r = subprocess.run(
            [str(HOOK_PATH)],
            input=json.dumps(payload).encode(),
            cwd=str(self.root),
            capture_output=True,
            timeout=15,
        )
        return r.returncode, r.stdout.decode()

    def _flag_path(self, sid: str) -> Path:
        return (
            self.root / ".evo" / "run_0000" / "inject" / "optimize_mode"
            / f"{sid}.flag"
        )

    def test_full_optimize_lifecycle(self):
        sid = "live-cc"

        # SessionStart — registers session.
        rc, _ = self._fire({"hook_event_name": "SessionStart", "session_id": sid})
        self.assertEqual(rc, 0)
        self.assertFalse(self._flag_path(sid).exists(),
                         "SessionStart alone must not arm optimize_mode")

        # UserPromptSubmit /evo:optimize — arms the flag.
        rc, _ = self._fire({
            "hook_event_name": "UserPromptSubmit",
            "session_id": sid,
            "prompt": "/evo:optimize improve x",
        })
        self.assertEqual(rc, 0)
        self.assertTrue(self._flag_path(sid).exists(),
                        "/evo:optimize must arm the optimize_mode flag")

        # PreToolUse Edit — must DENY.
        rc, out = self._fire({
            "hook_event_name": "PreToolUse",
            "session_id": sid,
            "tool_name": "Edit",
            "tool_input": {"file_path": "/tmp/x"},
        })
        self.assertEqual(rc, 0)
        result = json.loads(out)
        hso = result.get("hookSpecificOutput") or {}
        self.assertEqual(
            hso.get("permissionDecision"), "deny",
            f"PreToolUse Edit under /optimize must deny; got {result!r}"
        )
        self.assertIn("EVO POLICY", hso.get("permissionDecisionReason", ""))

        # Stop — must emit decision:block with EVO LOOP banner.
        rc, out = self._fire({
            "hook_event_name": "Stop",
            "session_id": sid,
        })
        self.assertEqual(rc, 0)
        result = json.loads(out)
        self.assertEqual(result.get("decision"), "block")
        self.assertIn("EVO LOOP", result.get("reason", ""))

        # exit-optimize-mode — clears the flag.
        env = os.environ.copy()
        env["CLAUDE_CODE_SESSION_ID"] = sid
        r = subprocess.run(
            ["evo", "exit-optimize-mode"],
            cwd=str(self.root),
            env=env,
            capture_output=True,
            text=True,
            timeout=10,
        )
        self.assertEqual(r.returncode, 0, f"exit failed: {r.stderr!r}")
        self.assertFalse(self._flag_path(sid).exists(),
                         "exit-optimize-mode must remove the flag file")

        # PreToolUse Edit again — must NOT deny (flag cleared).
        rc, out = self._fire({
            "hook_event_name": "PreToolUse",
            "session_id": sid,
            "tool_name": "Edit",
            "tool_input": {"file_path": "/tmp/x"},
        })
        self.assertEqual(rc, 0)
        result = json.loads(out)
        hso = result.get("hookSpecificOutput") or {}
        self.assertNotEqual(
            hso.get("permissionDecision"), "deny",
            f"after exit, Edit must pass; got {result!r}"
        )

    def test_non_denied_tool_fast_exits_without_optimize_mode(self):
        """Outside optimize_mode, PreToolUse with any tool must fast-exit
        (no policy deny, no Python invocation cost)."""
        sid = "live-cc-2"
        self._fire({"hook_event_name": "SessionStart", "session_id": sid})
        rc, out = self._fire({
            "hook_event_name": "PreToolUse",
            "session_id": sid,
            "tool_name": "Edit",
            "tool_input": {"file_path": "/tmp/x"},
        })
        self.assertEqual(rc, 0)
        self.assertEqual(out.strip(), "{}",
                         f"PreToolUse without optimize_mode must fast-exit; got {out!r}")

    def test_read_in_optimize_mode_passes(self):
        """Read is not on the deny list; it must pass even under optimize_mode."""
        sid = "live-cc-3"
        self._fire({"hook_event_name": "SessionStart", "session_id": sid})
        self._fire({
            "hook_event_name": "UserPromptSubmit",
            "session_id": sid,
            "prompt": "/evo:optimize",
        })
        rc, out = self._fire({
            "hook_event_name": "PreToolUse",
            "session_id": sid,
            "tool_name": "Read",
            "tool_input": {"file_path": "/tmp/x"},
        })
        self.assertEqual(rc, 0)
        result = json.loads(out)
        hso = result.get("hookSpecificOutput") or {}
        self.assertNotEqual(
            hso.get("permissionDecision"), "deny",
            "Read must not be denied even in optimize_mode"
        )


if __name__ == "__main__":
    unittest.main()

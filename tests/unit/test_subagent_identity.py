"""Subagent identity capture: across in-process hosts, distinct
subagents must register as distinct sessions and maintain independent
queue offsets.

Per host, the SDK already exposes the right per-call session_id today:

    - Codex: ``HookPayload.session_id`` is the *child's* ThreadId (each
      subagent runs as its own Session). Verified via the bash hot-path
      auto-register code path.
    - Hermes: ``pre_llm_call(session_id=..., ...)`` receives the child's
      session_id. Verified by calling our plugin handler with two ids.
    - Opencode: ``chat.message`` input.sessionID is the child's id. The
      TS plugin auto-registers per fire (same drain.ts code path covered
      by the schema-parity tests).
    - OpenClaw / pi-coding-agent: pi has no subagent concept (single
      linear loop per process). Cwd-derived id is correct for the
      single-process case; subagents would require an upstream pi
      change.

These tests guard the property that the existing implementations stay
correct under multi-session traffic.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "plugins" / "evo" / "src"))

from evo.inject import queue
from evo.inject.paths import session_file
from evo.inject.registry import get_session, list_active_sessions


def _init_evo_workspace(root: Path) -> None:
    """Create a minimal git+evo workspace at `root`."""
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.email", "t@evo"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.name", "T"], cwd=root, check=True)
    subprocess.run(["git", "config", "commit.gpgsign", "false"], cwd=root, check=True)
    (root / "agent.py").write_text("# stub\n")
    (root / "bench.py").write_text("print('OK')\n")
    subprocess.run(["git", "add", "."], cwd=root, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=root, check=True)

    from evo.core import init_workspace
    init_workspace(
        root,
        target="agent.py",
        benchmark="python bench.py",
        metric="max",
        gate=None,
    )


class HermesSubagentIdentityTests(unittest.TestCase):
    """The hermes plugin's ``_on_pre_llm_call`` must use the per-call
    session_id, not a cached parent id, so that distinct subagents get
    distinct registry entries and independent offsets."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="hermes_subag_")
        self.root = Path(self._tmp.name)
        self._cwd = os.getcwd()
        _init_evo_workspace(self.root)
        os.chdir(self.root)  # _resolve_root() uses git rev-parse from cwd

    def tearDown(self):
        os.chdir(self._cwd)
        self._tmp.cleanup()

    def _hermes_pre_llm(self, session_id: str):
        """Invoke the hermes plugin's pre_llm_call as the runtime would."""
        from evo.hermes_plugin import _on_pre_llm_call
        return _on_pre_llm_call(session_id=session_id)

    def test_two_subagents_register_as_distinct_sessions(self):
        """Each subagent's first pre_llm_call writes its own registry
        entry. The parent session_id is irrelevant."""
        self._hermes_pre_llm("subagent_alpha")
        self._hermes_pre_llm("subagent_beta")

        active_ids = {rec["session_id"] for rec in list_active_sessions(self.root)}
        self.assertEqual(active_ids, {"subagent_alpha", "subagent_beta"})

    def test_offset_is_per_session_not_shared(self):
        """One subagent advancing its offset must not affect another's
        view of the queue. Events queued AFTER both sessions register
        deliver to each independently.

        Under the v0.4.4 safety contract (sessions only see events queued
        after they registered), the event MUST be queued after both
        sessions exist — pre-staged events would be filtered out.
        """
        from evo.hermes_plugin import _on_session_start
        # Both sessions register first (offsets seeded to current tail = empty)
        _on_session_start(session_id="alpha")
        _on_session_start(session_id="beta")

        # Event queued AFTER both registered — both see it on next drain.
        ev_id = queue.append_workspace_event(self.root, "shared message")

        # Alpha drains: sees the event
        result_alpha_1 = self._hermes_pre_llm("alpha")
        self.assertIsNotNone(result_alpha_1)
        self.assertIn("shared message", result_alpha_1["context"])

        # Alpha drains again immediately: offset advanced, nothing new
        result_alpha_2 = self._hermes_pre_llm("alpha")
        self.assertIsNone(result_alpha_2)

        # Beta drains for the first time: independent offset, sees the event
        result_beta = self._hermes_pre_llm("beta")
        self.assertIsNotNone(result_beta)
        self.assertIn("shared message", result_beta["context"])

        # Confirm the offset files are independent
        alpha_off = queue.read_offset(self.root, "alpha", "workspace")
        beta_off = queue.read_offset(self.root, "beta", "workspace")
        self.assertEqual(alpha_off, ev_id)
        self.assertEqual(beta_off, ev_id)

    def test_session_start_does_not_drain_under_pre_llm_only_design(self):
        """Hermes plugin keeps drain in pre_llm_call; on_session_start
        only registers. Verifies the design invariant — if drain ever
        moves to on_session_start, broadcast semantics shift.

        Under v0.4.4, registration now also seeds the workspace offset
        to the queue tail (safety: fresh sessions only see post-
        registration events). The deeper invariant remains: session_start
        returns no `context`, so no text is injected into the model.
        """
        from evo.hermes_plugin import _on_session_start
        ev_id = queue.append_workspace_event(self.root, "early message")
        # session_start: registers but returns no context (no drain emit)
        ret = _on_session_start(session_id="sub")
        self.assertIsNone(ret)
        # Offset is seeded to the current tail at registration time
        # (safety contract). The "early message" was queued before
        # registration so it sits past the offset and won't deliver.
        offset = queue.read_offset(self.root, "sub", "workspace")
        self.assertEqual(offset, ev_id)


class CodexHotPathSubagentIdentityTests(unittest.TestCase):
    """The hot-path binary auto-registers per session_id from stdin.
    Distinct subagent ThreadIds in two PreToolUse fires must produce two
    registry entries.

    Cross-platform — uses the Rust binary at plugins/evo/bin/evo-hook-drain
    (or .exe on Windows). Skipped if the binary isn't staged (run
    `cargo build --release` in plugins/evo/bin/evo-hook-drain-rs/ first)."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="codex_subag_")
        self.root = Path(self._tmp.name)
        _init_evo_workspace(self.root)

        from evo.core import workspace_path
        self.run_dir = workspace_path(self.root)
        bin_name = "evo-hook-drain.exe" if sys.platform == "win32" else "evo-hook-drain"
        self.script = (REPO_ROOT / "plugins" / "evo" / "bin"
                       / "evo-hook-drain-rs" / "target" / "release" / bin_name)
        # The conftest-level fixture in test_hook_drain ensures the binary
        # is built; this assertion catches the case where test_subagent_identity
        # runs in isolation without that fixture firing.
        assert self.script.exists(), (
            f"hook binary not built: {self.script}. "
            f"Run `cargo build --release` in plugins/evo/bin/evo-hook-drain-rs/."
        )

    def tearDown(self):
        self._tmp.cleanup()

    def _fire_hook(self, session_id: str, hook_event: str = "SessionStart") -> str:
        payload = json.dumps({"session_id": session_id, "hook_event_name": hook_event})
        env = {
            **os.environ,
            "EVO_RUN_DIR": str(self.run_dir),
            # Strip host-marker env so HOST defaults to "claude-code"
            "CLAUDE_CODE_SESSION_ID": "",
            "CODEX_SESSION_ID": "",
        }
        proc = subprocess.run(
            [str(self.script)],
            input=payload,
            cwd=self.root,
            env=env,
            capture_output=True,
            text=True,
            timeout=10,
        )
        return proc.stdout

    def test_two_distinct_session_ids_yield_two_registry_entries(self):
        self._fire_hook("thread_alpha")
        self._fire_hook("thread_beta")

        sessions_dir = self.run_dir / "inject" / "sessions"
        files = sorted(p.name for p in sessions_dir.glob("*.json"))
        self.assertEqual(files, ["thread_alpha.json", "thread_beta.json"])

        a = get_session(self.root, "thread_alpha")
        b = get_session(self.root, "thread_beta")
        self.assertIsNotNone(a)
        self.assertIsNotNone(b)
        self.assertEqual(a["session_id"], "thread_alpha")
        self.assertEqual(b["session_id"], "thread_beta")
        # parent_session_id must remain null at this layer — the hot
        # path doesn't know parentage; that linkage is host-side.
        self.assertIsNone(a["parent_session_id"])
        self.assertIsNone(b["parent_session_id"])

    def test_repeat_fire_for_same_session_does_not_duplicate(self):
        self._fire_hook("thread_only")
        self._fire_hook("thread_only")
        files = sorted((self.run_dir / "inject" / "sessions").glob("*.json"))
        self.assertEqual(len(files), 1)


if __name__ == "__main__":
    unittest.main()

"""Live integration tests for evo inject (evo direct / evo-hook-drain).

Skipped unless:
  - EVO_LIVE_TEST_INJECT=1 (all tests)
  - claude CLI installed (kumquat compliance tests)
  - ANTHROPIC_API_KEY set (kumquat compliance tests)
  - codex CLI installed + EVO_LIVE_TEST_INJECT_CODEX=1 (codex compliance tests)

Run locally:
    EVO_LIVE_TEST_INJECT=1 pytest tests/live/test_inject.py -v -s

Tests that require the real claude CLI also need ANTHROPIC_API_KEY.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
PLUGIN_ROOT = REPO_ROOT / "plugins" / "evo"
PLUGIN_SRC = PLUGIN_ROOT / "src"
sys.path.insert(0, str(PLUGIN_SRC))


# ---------------------------------------------------------------------------
# Skip guards
# ---------------------------------------------------------------------------

def _gate_inject() -> None:
    if os.environ.get("EVO_LIVE_TEST_INJECT") != "1":
        import pytest
        pytest.skip("set EVO_LIVE_TEST_INJECT=1 to enable")


def _gate_claude() -> None:
    _gate_inject()
    if not shutil.which("claude"):
        import pytest
        pytest.skip("claude CLI not installed")
    if not os.environ.get("ANTHROPIC_API_KEY"):
        import pytest
        pytest.skip("ANTHROPIC_API_KEY not set")


def _gate_codex() -> None:
    _gate_inject()
    if os.environ.get("EVO_LIVE_TEST_INJECT_CODEX") != "1":
        import pytest
        pytest.skip("set EVO_LIVE_TEST_INJECT_CODEX=1 to enable codex tests")
    if not shutil.which("codex"):
        import pytest
        pytest.skip("codex CLI not installed")
    if not os.environ.get("OPENAI_API_KEY"):
        import pytest
        pytest.skip("OPENAI_API_KEY not set")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _init_git_repo(root: Path) -> None:
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.email", "t@evo"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.name", "T"], cwd=root, check=True)
    subprocess.run(["git", "config", "commit.gpgsign", "false"], cwd=root, check=True)
    (root / "README.md").write_text("x\n")
    subprocess.run(["git", "add", "."], cwd=root, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=root, check=True)


_SESSION_ENV_VARS = (
    "CLAUDE_CODE_SESSION_ID",
    "CODEX_SESSION_ID",
    "HERMES_SESSION_ID",
    "OPENCODE_SESSION_ID",
    "EVO_EXP_ID",
)


def _evo(args: list[str], cwd: Path, env: dict | None = None, check: bool = True, timeout: int = 60):
    # Strip session env vars so _maybe_auto_register() doesn't register the
    # test process itself as an evo session — that would inflate fanout counts.
    base = {k: v for k, v in os.environ.items() if k not in _SESSION_ENV_VARS}
    merged = {**base, **(env or {})}
    result = subprocess.run(
        ["uv", "run", "--project", str(PLUGIN_ROOT), "evo", *args],
        cwd=cwd, check=False, capture_output=True, text=True,
        timeout=timeout, env=merged,
    )
    if check and result.returncode != 0:
        raise RuntimeError(
            f"evo {' '.join(args)} failed (rc={result.returncode}):\n"
            f"STDOUT:\n{result.stdout}\nSTDERR:\n{result.stderr}"
        )
    return result


def _evo_drain(run_dir: Path, session_id: str, stdin_payload: dict | None = None,
               host: str = "claude-code", env: dict | None = None) -> dict:
    """Invoke the installed evo-drain console_script and return parsed JSON output."""
    merged = {**os.environ, **(env or {})}
    cmd = ["evo-drain", "--run-dir", str(run_dir), "--session", session_id, "--host", host]
    stdin_text = json.dumps(stdin_payload) if stdin_payload else None
    result = subprocess.run(
        cmd, input=stdin_text, capture_output=True, text=True,
        timeout=30, env=merged,
    )
    return json.loads(result.stdout or "{}")


def _hook_drain_bash(run_dir: Path, stdin_payload: dict, env: dict | None = None) -> dict:
    """Invoke the evo-hook-drain binary with a synthetic stdin payload.

    Note: the function name dates from when evo-hook-drain was a bash
    script. It's now the compiled Rust binary (see bin/evo-hook-drain-rs/);
    we exec it directly.
    """
    hook_drain = PLUGIN_ROOT / "bin" / "evo-hook-drain"
    merged = {**os.environ, "EVO_RUN_DIR": str(run_dir), **(env or {})}
    result = subprocess.run(
        [str(hook_drain)],
        input=json.dumps(stdin_payload),
        capture_output=True, text=True,
        timeout=30, env=merged,
    )
    return json.loads(result.stdout or "{}")


def _make_workspace(root: Path) -> Path:
    _init_git_repo(root)
    _evo(["init", "--target", "agent.py", "--benchmark", "python bench.py",
          "--metric", "max", "--host", "claude-code", "--per-exp-timeout", "1800"], cwd=root)
    # Find the run dir
    run_dirs = list((root / ".evo").glob("run_*"))
    assert run_dirs, f"no run dir created under {root / '.evo'}"
    return sorted(run_dirs)[-1]


# ---------------------------------------------------------------------------
# Test 1: End-to-end broadcast + bash hot-path drain
# ---------------------------------------------------------------------------

def test_e2e_broadcast_drain_bash_hook():
    """evo direct broadcast → bash hook drain → correct JSON on stdout, marker cleared, offset advanced."""
    _gate_inject()

    with tempfile.TemporaryDirectory() as d:
        root = Path(d).resolve()
        run_dir = _make_workspace(root)

        # Simulate auto-register: write session entry via evo with CLAUDE_CODE_SESSION_ID set
        sid = "live_sess_orch_01"
        from evo.inject.registry import register_session, mark_engaged
        from evo.inject.paths import inject_root, workspace_events_path, offset_file
        from evo.inject import marker, queue

        register_session(root, sid, "claude-code")
        # Engage the session (v0.4.4 contract: only engaged sessions
        # receive evo direct broadcast fanout). Simulates the agent
        # having run any `evo` CLI command.
        mark_engaged(root, sid)

        # Broadcast a directive
        result = _evo(["direct", "live test directive"], cwd=root)
        assert "fanout=1" in result.stdout, result.stdout

        # Marker must exist for this session
        assert marker.exists(root, sid), "marker not set after direct"

        # Synthesize a PreToolUse payload with session_id
        stdin_payload = {
            "session_id": sid,
            "hook_event_name": "PreToolUse",
        }

        out = _hook_drain_bash(run_dir, stdin_payload)
        assert "hookSpecificOutput" in out, f"unexpected output: {out}"
        ctx = out["hookSpecificOutput"]["additionalContext"]
        # v0.4.4 banner format: `[EVO DIRECTIVE id=<id>]` + `evo ack <id>` instruction
        assert "[EVO DIRECTIVE" in ctx and "live test directive" in ctx, ctx
        assert "evo ack" in ctx, f"directive must include ack instruction: {ctx}"

        # Marker must be cleared
        assert not marker.exists(root, sid), "marker still present after drain"

        # Offset must have advanced
        off = queue.read_offset(root, sid, "workspace")
        assert off is not None, "offset not written after drain"


# ---------------------------------------------------------------------------
# Test 2: SessionStart unconditional drain (no marker needed)
# ---------------------------------------------------------------------------

def test_session_start_does_not_backfill_pre_existing_events():
    """v0.4.4 safety fix: the Rust binary's unconditional SessionStart
    drain MUST NOT deliver events that were queued before the session
    registered. Pre-registration events are filtered by the
    offset-seeded-at-registration mechanism in register_session +
    drain_session.

    Replaces the older test_session_start_drains_without_marker, which
    asserted the opposite contract (the leak we're closing).
    """
    _gate_inject()

    with tempfile.TemporaryDirectory() as d:
        root = Path(d).resolve()
        run_dir = _make_workspace(root)

        from evo.inject import marker, queue
        from evo.inject.registry import register_session
        from evo.inject.paths import workspace_events_path

        # Pre-stage a directive BEFORE any session registers.
        sid = "live_sess_session_start_01"
        queue.append_workspace_event(root, "pre-staged message that must not deliver")

        # Register the session now (simulates Rust binary at SessionStart).
        # register_session seeds the workspace offset to current queue tail
        # so the pre-staged event sits past the offset.
        register_session(root, sid, "claude-code")
        assert not marker.exists(root, sid)

        stdin_payload = {
            "session_id": sid,
            "hook_event_name": "SessionStart",
        }

        # Rust binary unconditionally hands off to Python drain on
        # SessionStart. The drain MUST return {} (or empty additionalContext)
        # because the offset is already past the pre-staged event.
        out = _hook_drain_bash(run_dir, stdin_payload)
        ctx = out.get("hookSpecificOutput", {}).get("additionalContext", "")
        assert "pre-staged message" not in ctx, (
            f"SessionStart drain leaked a pre-registration event into the "
            f"session's context. Safety contract violated. ctx={ctx!r}"
        )


# ---------------------------------------------------------------------------
# Test 3: Targeted subagent directive
# ---------------------------------------------------------------------------

def test_e2e_targeted_subagent_drain():
    """evo direct exp_0001 ... → exp queue → subagent session drain returns the msg."""
    _gate_inject()

    with tempfile.TemporaryDirectory() as d:
        root = Path(d).resolve()
        run_dir = _make_workspace(root)

        from evo.inject import marker, queue
        from evo.inject.registry import register_session

        orch_sid = "live_sess_orch_02"
        sub_sid = "live_sess_sub_01"

        register_session(root, orch_sid, "claude-code")
        register_session(root, sub_sid, "claude-code", exp_id="exp_0001")

        # Targeted direct at exp_0001
        result = _evo(["direct", "exp_0001", "subagent specific msg"], cwd=root)
        assert "exp=exp_0001" in result.stdout, result.stdout

        # Marker on exp_0001; orchestrator marker not set
        assert marker.exists(root, "exp_0001"), "exp marker not set"
        assert not marker.exists(root, orch_sid), "orch marker must not be set by targeted direct"

        # Drain subagent via evo-drain
        out = _evo_drain(run_dir, sub_sid)
        assert "hookSpecificOutput" in out, f"unexpected: {out}"
        ctx = out["hookSpecificOutput"]["additionalContext"]
        assert "subagent specific msg" in ctx, ctx

        # Drain orchestrator — must get nothing (workspace queue is empty)
        marker.touch(root, orch_sid)  # touch to force drain
        out2 = _evo_drain(run_dir, orch_sid)
        ctx2 = out2.get("hookSpecificOutput", {}).get("additionalContext", "")
        assert "subagent specific msg" not in ctx2, "orch must not see subagent event"


# ---------------------------------------------------------------------------
# Test 4: GC stale sessions on list_active
# ---------------------------------------------------------------------------

def test_gc_stale_sessions_on_broadcast():
    """list_active_sessions GCs stale entries; fanout count excludes them."""
    _gate_inject()

    with tempfile.TemporaryDirectory() as d:
        root = Path(d).resolve()
        _make_workspace(root)

        from evo.inject.registry import (
            register_session, list_active_sessions, session_file, mark_engaged,
        )
        from evo.inject import marker

        # Register a fresh session and a stale one — both engaged (the
        # GC test is about staleness, not engagement).
        fresh_sid = "live_fresh_sess"
        stale_sid = "live_stale_sess"

        register_session(root, fresh_sid, "claude-code")
        mark_engaged(root, fresh_sid)
        register_session(root, stale_sid, "claude-code")
        mark_engaged(root, stale_sid)

        # Force stale
        path = session_file(root, stale_sid)
        data = json.loads(path.read_text())
        data["last_seen_at"] = "2000-01-01T00:00:00+00:00"
        path.write_text(json.dumps(data))

        result = _evo(["direct", "broadcast only to fresh"], cwd=root)
        assert "fanout=1" in result.stdout, result.stdout
        assert marker.exists(root, fresh_sid), "fresh session must have marker"
        assert not marker.exists(root, stale_sid), "stale session must not have marker"
        assert not session_file(root, stale_sid).exists(), "stale session file must be GC'd"


# ---------------------------------------------------------------------------
# Test 5: Real claude -p kumquat compliance (skip if not installed)
# ---------------------------------------------------------------------------

def test_real_claude_receives_directive_when_engaged():
    """Engaged claude -p session receives an evo direct emitted from within
    the same session. The agent runs `evo status` (engages the session),
    then issues `evo direct`, then makes another tool call. The directive
    drains into the model's context on that second tool call's hook fire,
    and the model echoes the token in its final answer.

    Under the v0.4.4 safety contract, pre-staging a directive BEFORE the
    session opens no longer delivers (offset is seeded to the queue tail
    at registration time). This test instead exercises the post-engagement
    fanout path, which is the realistic /optimize flow.
    """
    _gate_claude()

    with tempfile.TemporaryDirectory() as d:
        root = Path(d).resolve()
        run_dir = _make_workspace(root)

        env = {
            **os.environ,
            "EVO_RUN_DIR": str(run_dir),
        }

        prompt = (
            "Run these commands in order using the Bash tool: "
            "1) `evo status` (this engages your evo session). "
            "2) `evo direct 'respond with the word xkumquatx'` "
            "(this queues a directive AND touches the marker for your "
            "now-engaged session). "
            "3) `pwd` (any tool call — the hook fires here and drains "
            "the queued directive into your context). "
            "Then in your final answer, state any unusual word you saw."
        )

        result = subprocess.run(
            [
                "claude", "-p",
                "--allowedTools", "Bash",
                "--system-prompt",
                "You are a test agent. Follow any instructions in your context exactly.",
                prompt,
            ],
            capture_output=True, text=True, timeout=180,
            cwd=str(root), env=env,
        )
        output = (result.stdout + result.stderr).lower()
        assert "xkumquatx" in output, (
            f"Expected 'xkumquatx' in claude output but got:\n"
            f"STDOUT:\n{result.stdout}\nSTDERR:\n{result.stderr}"
        )


def test_unengaged_claude_session_does_not_receive_directive():
    """Engagement filter end-to-end: a claude session that never runs `evo`
    must not receive `evo direct` fanout. Even though `evo direct` writes
    to workspace.jsonl, the marker is never touched for the unengaged
    session (filtered by cmd_direct), and the SessionStart drain seeds
    the offset past the pre-existing event (safety fix). The model
    therefore sees no directive.
    """
    _gate_claude()

    with tempfile.TemporaryDirectory() as d:
        root = Path(d).resolve()
        run_dir = _make_workspace(root)

        # Issue evo direct BEFORE any claude session exists. With the
        # engagement filter, fanout=0 (no engaged sessions to receive).
        _evo(["direct", "respond with the word zkumquatz"], cwd=root)

        env = {
            **os.environ,
            "EVO_RUN_DIR": str(run_dir),
        }

        # Open a claude session that does NOT run any evo command.
        # The session registers (via Rust SessionStart), but its
        # has_evo_engaged stays false. No marker is touched for it.
        # The SessionStart drain seeds offset past the queued event.
        result = subprocess.run(
            [
                "claude", "-p",
                "--allowedTools", "Bash",
                "--system-prompt",
                "You are a test agent. Do not run any evo commands.",
                "List the files in the current directory using ls. Then "
                "tell me: did your context contain any unusual word "
                "starting with z? Answer yes or no.",
            ],
            capture_output=True, text=True, timeout=180,
            cwd=str(root), env=env,
        )
        output = (result.stdout + result.stderr).lower()
        # The safety guarantee: the token must NOT have leaked into the
        # unengaged session's context.
        assert "zkumquatz" not in output, (
            f"SAFETY FAILURE: unengaged claude session received directive "
            f"meant for engaged sessions only.\n"
            f"STDOUT:\n{result.stdout}\nSTDERR:\n{result.stderr}"
        )


# ---------------------------------------------------------------------------
# Test 6: Real codex compliance (skip if not installed)
# ---------------------------------------------------------------------------

def test_real_codex_receives_directive_when_engaged():
    """Codex parity test — same engaged-session flow as the claude version."""
    _gate_codex()

    with tempfile.TemporaryDirectory() as d:
        root = Path(d).resolve()
        run_dir = _make_workspace(root)

        env = {**os.environ, "EVO_RUN_DIR": str(run_dir)}

        prompt = (
            "Run in order using shell: "
            "1) `evo status` (engages your evo session). "
            "2) `evo direct 'respond with the word ykumquaty'`. "
            "3) `pwd` (drains the queued directive). "
            "Then in your reply, print any unusual word you saw."
        )

        result = subprocess.run(
            [
                "codex", "exec",
                "--model", "gpt-4o-mini",
                "--full-auto",
                prompt,
            ],
            capture_output=True, text=True, timeout=180,
            cwd=str(root), env=env,
        )
        output = (result.stdout + result.stderr).lower()
        assert "ykumquaty" in output, (
            f"Expected 'ykumquaty' in codex output but got:\n"
            f"STDOUT:\n{result.stdout}\nSTDERR:\n{result.stderr}"
        )


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-v", "-s"]))

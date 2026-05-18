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
    """Invoke evo-hook-drain bash script with a synthetic stdin payload."""
    hook_drain = PLUGIN_ROOT / "bin" / "evo-hook-drain"
    merged = {**os.environ, "EVO_RUN_DIR": str(run_dir), **(env or {})}
    result = subprocess.run(
        ["bash", str(hook_drain)],
        input=json.dumps(stdin_payload),
        capture_output=True, text=True,
        timeout=30, env=merged,
    )
    return json.loads(result.stdout or "{}")


def _make_workspace(root: Path) -> Path:
    _init_git_repo(root)
    _evo(["init", "--target", "agent.py", "--benchmark", "python bench.py",
          "--metric", "max", "--host", "claude-code"], cwd=root)
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
        from evo.inject.registry import register_session
        from evo.inject.paths import inject_root, workspace_events_path, offset_file
        from evo.inject import marker, queue

        register_session(root, sid, "claude-code")

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
        assert "[EVO DIRECTIVE]" in ctx and "live test directive" in ctx, ctx

        # Marker must be cleared
        assert not marker.exists(root, sid), "marker still present after drain"

        # Offset must have advanced
        off = queue.read_offset(root, sid, "workspace")
        assert off is not None, "offset not written after drain"


# ---------------------------------------------------------------------------
# Test 2: SessionStart unconditional drain (no marker needed)
# ---------------------------------------------------------------------------

def test_session_start_drains_without_marker():
    """Directive queued before session registers; SessionStart fires and drains
    the backlog even with no marker."""
    _gate_inject()

    with tempfile.TemporaryDirectory() as d:
        root = Path(d).resolve()
        run_dir = _make_workspace(root)

        from evo.inject import marker, queue
        from evo.inject.registry import register_session
        from evo.inject.paths import workspace_events_path

        # Pre-stage a directive before session registers
        sid = "live_sess_session_start_01"
        queue.append_workspace_event(root, "pre-staged message")

        # Register session now (simulating SessionStart)
        register_session(root, sid, "claude-code")

        # No marker set — SessionStart hook drains unconditionally
        assert not marker.exists(root, sid), "marker should not exist yet"

        stdin_payload = {
            "session_id": sid,
            "hook_event_name": "SessionStart",
        }

        out = _hook_drain_bash(run_dir, stdin_payload)
        # SessionStart hook should drain the workspace queue via bash script's
        # unconditional path, then call evo-drain which sees the pre-staged event.
        assert "hookSpecificOutput" in out, f"expected hook output, got: {out}"
        ctx = out["hookSpecificOutput"]["additionalContext"]
        assert "pre-staged message" in ctx, ctx


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

        from evo.inject.registry import register_session, list_active_sessions, session_file
        from evo.inject import marker

        # Register a fresh session and a stale one
        fresh_sid = "live_fresh_sess"
        stale_sid = "live_stale_sess"

        register_session(root, fresh_sid, "claude-code")
        register_session(root, stale_sid, "claude-code")

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

def test_real_claude_receives_directive():
    """Issue evo direct with a nonsense token ('kumquat'); verify claude -p
    picks it up via SessionStart drain and echoes it back."""
    _gate_claude()

    with tempfile.TemporaryDirectory() as d:
        root = Path(d).resolve()
        run_dir = _make_workspace(root)

        # Pre-stage a directive with a token claude can't have seen
        from evo.inject import queue
        queue.append_workspace_event(root, "respond with the word: xkumquatx")

        # Start claude -p with the hook configured to run evo-hook-drain
        # Use the hooks/ directory path to locate the evo-hook-drain binary.
        hook_cmd = str(PLUGIN_ROOT / "bin" / "evo-hook-drain")
        env = {
            **os.environ,
            "EVO_RUN_DIR": str(run_dir),
        }

        result = subprocess.run(
            [
                "claude", "-p",
                "--allowedTools", "Bash",
                "--system-prompt",
                "You are a test agent. Follow any instructions in your context exactly.",
                "What word was mentioned in your context?",
            ],
            capture_output=True, text=True, timeout=120,
            cwd=str(root), env=env,
        )
        output = (result.stdout + result.stderr).lower()
        assert "xkumquatx" in output, (
            f"Expected 'xkumquatx' in claude output but got:\n{result.stdout}\n{result.stderr}"
        )


# ---------------------------------------------------------------------------
# Test 6: Real codex compliance (skip if not installed)
# ---------------------------------------------------------------------------

def test_real_codex_receives_directive():
    """Issue evo direct; verify codex exec picks it up via SessionStart drain."""
    _gate_codex()

    with tempfile.TemporaryDirectory() as d:
        root = Path(d).resolve()
        run_dir = _make_workspace(root)

        from evo.inject import queue
        queue.append_workspace_event(root, "respond with the word: ykumquaty")

        env = {**os.environ, "EVO_RUN_DIR": str(run_dir)}

        result = subprocess.run(
            [
                "codex", "exec",
                "--model", "gpt-4o-mini",
                "--full-auto",
                "What word was in your context? Print it verbatim.",
            ],
            capture_output=True, text=True, timeout=120,
            cwd=str(root), env=env,
        )
        output = (result.stdout + result.stderr).lower()
        assert "ykumquaty" in output, (
            f"Expected 'ykumquaty' in codex output but got:\n{result.stdout}\n{result.stderr}"
        )


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-v", "-s"]))

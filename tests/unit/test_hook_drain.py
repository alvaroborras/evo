"""Subprocess tests for bin/evo-hook-drain — the cross-platform Rust binary.

Covers:
  - Binary exists at the expected per-platform path
  - Fast-path latency (regression guard: median <40ms — Rust cold-start
    is ~2-5ms; budget covers CI runner noise)
  - Branch 1: bare `evo-drain` on PATH → spawn it
  - Branch 3 (fallback): evo-drain not on PATH → actionable error
  - SessionStart drift warning when cache version != marketplace clone version
  - SessionStart proactive warning when evo-drain not on PATH

The Rust source lives at plugins/evo/bin/evo-hook-drain-rs/. Tests exec
the release binary straight from the cargo target dir (CI builds it in
ci.yml's `unit-tests` job; locally run `cargo build --release` inside
the Rust crate). plugins/evo/bin/evo-hook-drain is the committed
shell-script fallback, not the binary.
"""

from __future__ import annotations

import os
import statistics
import subprocess
import sys
import time
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parent.parent.parent
HOOK_NAME = "evo-hook-drain.exe" if sys.platform == "win32" else "evo-hook-drain"
HOOK_PATH = (REPO_ROOT / "plugins" / "evo" / "bin" / "evo-hook-drain-rs"
             / "target" / "release" / HOOK_NAME)
PAYLOAD_PRETOOL = b'{"session_id":"test-sid","hook_event_name":"PreToolUse"}'
PAYLOAD_SESSION_START = b'{"session_id":"test-sid","hook_event_name":"SessionStart"}'

# The binary's existence at HOOK_PATH is guaranteed by an autouse
# session-scoped fixture in tests/unit/conftest.py — it builds via
# `cargo build --release` if missing. Tests assert the result without
# guarding for the missing case.


def _scaffold_evo_run(tmp_path: Path, sid: str = "test-sid", with_marker: bool = True) -> Path:
    """Set up a fake .evo/run_test/ that pushes the script past all fast-exits."""
    run = tmp_path / ".evo" / "run_test"
    (run / "inject" / "sessions").mkdir(parents=True)
    (run / "inject" / "markers").mkdir(parents=True)
    (run / "inject" / "sessions" / f"{sid}.json").write_text(
        '{"schema_version":1,"session_id":"' + sid + '","host":"claude-code"}'
    )
    if with_marker:
        (run / "inject" / "markers" / f"{sid}.flag").touch()
    return tmp_path


def _run_hook(cwd: Path, payload: bytes, path_env: str | None = None) -> subprocess.CompletedProcess:
    env = os.environ.copy()
    if path_env is not None:
        env["PATH"] = path_env
    return subprocess.run(
        [str(HOOK_PATH)],
        input=payload,
        cwd=str(cwd),
        env=env,
        capture_output=True,
        timeout=10,
    )


def test_hook_path_exists():
    assert HOOK_PATH.exists(), f"hook binary not staged at {HOOK_PATH}"


def test_fast_path_no_evo_dir_exits_clean(tmp_path):
    """No .evo/ in cwd → fast-exit, stdout {}, exit 0."""
    r = _run_hook(tmp_path, PAYLOAD_PRETOOL)
    assert r.returncode == 0
    assert r.stdout.strip() == b"{}"
    assert r.stderr == b""


def test_fast_path_no_session_id_exits_clean(tmp_path):
    """No session_id in payload, no env var → fast-exit."""
    env_no_sid = {k: v for k, v in os.environ.items()
                  if k not in {"CLAUDE_CODE_SESSION_ID", "CODEX_THREAD_ID",
                               "HERMES_SESSION_ID", "OPENCODE_SESSION_ID"}}
    r = subprocess.run(
        [str(HOOK_PATH)], input=b'{"hook_event_name":"PreToolUse"}',
        cwd=str(tmp_path), env=env_no_sid, capture_output=True, timeout=10,
    )
    assert r.returncode == 0
    assert r.stdout.strip() == b"{}"


def test_fast_path_latency_under_40ms_median(tmp_path):
    """Regression guard: fast-path median must stay tight.

    Rust cold-start is ~2-5ms across platforms. Budget set at 40ms to
    absorb CI runner noise but tight enough to catch accidental fork /
    network / runtime-init regressions.
    """
    times_ms = []
    for _ in range(30):
        t0 = time.perf_counter()
        r = _run_hook(tmp_path, PAYLOAD_PRETOOL)
        times_ms.append((time.perf_counter() - t0) * 1000)
        assert r.returncode == 0
    median_ms = statistics.median(times_ms)
    assert median_ms < 40.0, (
        f"fast-path regressed: median={median_ms:.2f}ms (budget <40ms). "
        f"Check evo-hook-drain.rs wasn't accidentally given network or fork work."
    )


def _write_fake_drain(fake_bin: Path) -> Path:
    """Write a fake evo-drain that just prints {} and exits 0."""
    fake_bin.mkdir(exist_ok=True)
    if sys.platform == "win32":
        # On Windows, .cmd shims trip Node 20's CVE-2024-27980 security
        # fix but the Rust binary spawns them fine via CreateProcess.
        # A real Python console_script installed via pip/uv ends up as
        # an .exe shim — both forms work.
        (fake_bin / "evo-drain.cmd").write_text(
            "@echo off\r\necho {}\r\nexit /b 0\r\n"
        )
        return fake_bin / "evo-drain.cmd"
    drain = fake_bin / "evo-drain"
    drain.write_text("#!/bin/bash\necho '{}'\nexit 0\n")
    drain.chmod(0o755)
    return drain


def _path_separator() -> str:
    return ";" if sys.platform == "win32" else ":"


def _base_path() -> str:
    """A minimal PATH for tests that need to find OS basics but not evo-drain."""
    if sys.platform == "win32":
        return os.environ.get("SystemRoot", "C:\\Windows") + "\\System32"
    return "/usr/bin:/bin"


def test_branch_1_bare_evo_drain_exec(tmp_path):
    """When evo-drain is on PATH, hook spawns it (exit 0 from fake)."""
    _scaffold_evo_run(tmp_path)
    fake_bin = tmp_path / "fake-bin"
    _write_fake_drain(fake_bin)
    path_env = f"{fake_bin}{_path_separator()}{_base_path()}"
    r = _run_hook(tmp_path, PAYLOAD_PRETOOL, path_env=path_env)
    assert r.returncode == 0
    assert r.stdout.strip() == b"{}"


def test_branch_3_no_drain_emits_actionable_error(tmp_path):
    """evo-drain missing → exit 1 with install hint on stderr."""
    _scaffold_evo_run(tmp_path)
    r = _run_hook(tmp_path, PAYLOAD_PRETOOL, path_env=_base_path())
    assert r.returncode == 1
    assert b"install evo-hq-cli" in r.stderr
    assert b"uv tool install evo-hq-cli" in r.stderr
    assert r.stdout.strip() == b"{}"


def test_session_start_warns_when_drain_missing(tmp_path):
    """SessionStart fires → proactive warning that evo-drain isn't on PATH."""
    _scaffold_evo_run(tmp_path)
    r = _run_hook(tmp_path, PAYLOAD_SESSION_START, path_env=_base_path())
    assert b"install evo-hq-cli to enable mid-run inject" in r.stderr


def test_session_start_emits_cache_stale_warning(tmp_path):
    """Stage marketplace clone with newer version than 'cache' → warning."""
    fake_home = tmp_path / "home"
    cache_root = fake_home / ".claude/plugins/cache/evo-hq-evo/evo/0.4.0"
    mkt_root = fake_home / ".claude/plugins/marketplaces/evo-hq-evo/plugins/evo"
    (cache_root / ".claude-plugin").mkdir(parents=True)
    (mkt_root / ".claude-plugin").mkdir(parents=True)
    (cache_root / ".claude-plugin/plugin.json").write_text(
        '{"name":"evo","version":"0.4.0"}'
    )
    (mkt_root / ".claude-plugin/plugin.json").write_text(
        '{"name":"evo","version":"0.4.1"}'
    )
    # Copy the binary into the fake cache so its parent dir resolves to
    # `.../plugins/cache/...` (that's what triggers the drift detection).
    (cache_root / "bin").mkdir()
    import shutil
    fake_hook = cache_root / "bin" / HOOK_NAME
    shutil.copy2(HOOK_PATH, fake_hook)
    _scaffold_evo_run(tmp_path)
    # HOME on Windows is USERPROFILE; set both for portability.
    env = {**os.environ, "HOME": str(fake_home), "USERPROFILE": str(fake_home),
           "PATH": _base_path()}
    r = subprocess.run(
        [str(fake_hook)], input=PAYLOAD_SESSION_START,
        cwd=str(tmp_path), env=env, capture_output=True, timeout=10,
    )
    assert b"plugin cache is stale" in r.stderr
    assert b"running 0.4.0" in r.stderr
    assert b"marketplace has 0.4.1" in r.stderr
    assert b"evo update --force" in r.stderr


def test_session_start_silent_when_drain_present(tmp_path):
    """SessionStart with evo-drain on PATH → no nudge, drain runs."""
    _scaffold_evo_run(tmp_path)
    fake_bin = tmp_path / "fake-bin"
    _write_fake_drain(fake_bin)
    path_env = f"{fake_bin}{_path_separator()}{_base_path()}"
    r = _run_hook(tmp_path, PAYLOAD_SESSION_START, path_env=path_env)
    assert b"install evo-hq-cli" not in r.stderr
    assert r.returncode == 0


# ---------------------------------------------------------------------------
# optimize_mode flag-file gate — the actual policy-gate correctness fix.
# Without this, claude-code/codex tool events fast-exit (no marker) even
# when the policy gate should fire. The flag file is the signal.
# ---------------------------------------------------------------------------


def _write_recording_drain(fake_bin: Path) -> Path:
    """Fake evo-drain that records its argv to a sibling `argv.log` file
    and prints {}. Used to assert what the Rust hook passes through."""
    fake_bin.mkdir(exist_ok=True)
    if sys.platform == "win32":
        # Mirror the bash shim's per-arg output. `%*` would dump every
        # arg space-joined onto one line, defeating per-arg assertions
        # like `args.index("--host")`. Loop with `shift` so each arg
        # lands on its own line, matching `printf "%s\n" "$@"`.
        drain = fake_bin / "evo-drain.cmd"
        argv_log = (fake_bin / "argv.log").as_posix()
        drain.write_text(
            "@echo off\r\n"
            f'type nul > "{argv_log}"\r\n'
            ":loop\r\n"
            'if "%~1"=="" goto done\r\n'
            f'>> "{argv_log}" echo %~1\r\n'
            "shift\r\n"
            "goto loop\r\n"
            ":done\r\n"
            "echo {}\r\n"
            "exit /b 0\r\n"
        )
        return drain
    drain = fake_bin / "evo-drain"
    drain.write_text(
        "#!/bin/bash\n"
        f'printf "%s\\n" "$@" > "{fake_bin / "argv.log"}"\n'
        "echo '{}'\nexit 0\n"
    )
    drain.chmod(0o755)
    return drain


def _scaffold_with_opt_flag(tmp_path: Path, sid: str = "test-sid") -> Path:
    """Scaffold a session WITHOUT a marker but WITH the optimize_mode
    flag file present."""
    run = tmp_path / ".evo" / "run_test"
    (run / "inject" / "sessions").mkdir(parents=True)
    (run / "inject" / "markers").mkdir(parents=True)
    (run / "inject" / "optimize_mode").mkdir(parents=True)
    (run / "inject" / "sessions" / f"{sid}.json").write_text(
        '{"schema_version":1,"session_id":"' + sid + '","host":"claude-code"}'
    )
    (run / "inject" / "optimize_mode" / f"{sid}.flag").touch()
    return tmp_path


def test_opt_flag_present_pretooluse_hands_off(tmp_path):
    """When optimize_mode flag exists and event is PreToolUse, hand off
    to drain even without a marker. This is the policy-gate correctness
    fix: the Python drain enforces the deny list."""
    _scaffold_with_opt_flag(tmp_path)
    fake_bin = tmp_path / "fake-bin"
    _write_fake_drain(fake_bin)
    path_env = f"{fake_bin}{_path_separator()}{_base_path()}"
    r = _run_hook(tmp_path, PAYLOAD_PRETOOL, path_env=path_env)
    assert r.returncode == 0
    # Fake drain prints {} — if it ran, that's what we get on stdout.
    assert r.stdout.strip() == b"{}"


def test_opt_flag_present_stop_hands_off(tmp_path):
    """Stop event with optimize_mode flag → hand off (stop nudge)."""
    _scaffold_with_opt_flag(tmp_path)
    fake_bin = tmp_path / "fake-bin"
    _write_fake_drain(fake_bin)
    path_env = f"{fake_bin}{_path_separator()}{_base_path()}"
    r = _run_hook(
        tmp_path,
        b'{"session_id":"test-sid","hook_event_name":"Stop"}',
        path_env=path_env,
    )
    assert r.returncode == 0
    assert r.stdout.strip() == b"{}"


def test_opt_flag_present_subagent_stop_hands_off(tmp_path):
    _scaffold_with_opt_flag(tmp_path)
    fake_bin = tmp_path / "fake-bin"
    _write_fake_drain(fake_bin)
    path_env = f"{fake_bin}{_path_separator()}{_base_path()}"
    r = _run_hook(
        tmp_path,
        b'{"session_id":"test-sid","hook_event_name":"SubagentStop"}',
        path_env=path_env,
    )
    assert r.returncode == 0


def test_subagent_context_skips_drain_even_with_marker(tmp_path):
    """When the hook payload contains `agent_id` (non-empty), the call
    originated from a Task-tool subagent and the hook must fast-exit
    instead of draining the queue — even though the parent's marker
    exists and the session_id is the parent's.

    Claude-code (and codex) Task-tool subagents inherit the parent's
    CLAUDE_CODE_SESSION_ID env var, so without this guard the subagent's
    PreToolUse fire would resolve to the parent's session, drain the
    parent's queue, and the additionalContext would land in the
    subagent's API call instead of the orchestrator's. Net effect:
    directive eaten by a subagent doing its narrow task; orchestrator
    never sees it.

    Empirically verified discriminator: claude-code includes
    `agent_id` + `agent_type` in PreToolUse payloads for subagent
    tool calls only. Orchestrator-context tool calls have these fields
    absent.
    """
    _scaffold_evo_run(tmp_path)  # marker present
    fake_bin = tmp_path / "fake-bin"
    _write_recording_drain(fake_bin)
    path_env = f"{fake_bin}{_path_separator()}{_base_path()}"
    payload = (
        b'{"session_id":"test-sid","hook_event_name":"PreToolUse",'
        b'"agent_id":"a892a53b6207","agent_type":"general-purpose",'
        b'"tool_name":"Edit"}'
    )
    r = _run_hook(tmp_path, payload, path_env=path_env)
    assert r.returncode == 0
    assert r.stdout.strip() == b"{}"
    assert not (fake_bin / "argv.log").exists(), (
        "drain must NOT be invoked when agent_id indicates subagent "
        "context — directive belongs to the orchestrator"
    )


def test_main_context_without_agent_id_still_drains(tmp_path):
    """The companion to the above — orchestrator tool calls (no
    `agent_id` field) must continue to drain normally."""
    _scaffold_evo_run(tmp_path)  # marker present
    fake_bin = tmp_path / "fake-bin"
    _write_recording_drain(fake_bin)
    path_env = f"{fake_bin}{_path_separator()}{_base_path()}"
    payload = (
        b'{"session_id":"test-sid","hook_event_name":"PreToolUse",'
        b'"tool_name":"Bash","tool_input":{"command":"evo status"}}'
    )
    r = _run_hook(tmp_path, payload, path_env=path_env)
    assert r.returncode == 0
    assert (fake_bin / "argv.log").exists(), (
        "drain must be invoked when agent_id is absent (orchestrator)"
    )


def test_empty_agent_id_treated_as_main(tmp_path):
    """If claude-code ever serialises `agent_id` as an empty string
    instead of omitting the field, treat it as orchestrator context
    (not subagent) — the discriminator is presence + non-empty."""
    _scaffold_evo_run(tmp_path)
    fake_bin = tmp_path / "fake-bin"
    _write_recording_drain(fake_bin)
    path_env = f"{fake_bin}{_path_separator()}{_base_path()}"
    payload = (
        b'{"session_id":"test-sid","hook_event_name":"PreToolUse",'
        b'"agent_id":"","tool_name":"Bash"}'
    )
    r = _run_hook(tmp_path, payload, path_env=path_env)
    assert r.returncode == 0
    assert (fake_bin / "argv.log").exists(), (
        "empty agent_id must NOT fence — only present + non-empty counts"
    )


def test_opt_flag_absent_pretooluse_fast_exits(tmp_path):
    """Without optimize_mode flag AND without marker, PreToolUse fast-exits.
    This is the cost-saving fast path — no Python invocation for normal
    tool calls outside /optimize."""
    # Use the marker-less scaffold (no opt_flag, no marker).
    _scaffold_evo_run(tmp_path, with_marker=False)
    fake_bin = tmp_path / "fake-bin"
    _write_recording_drain(fake_bin)
    path_env = f"{fake_bin}{_path_separator()}{_base_path()}"
    r = _run_hook(tmp_path, PAYLOAD_PRETOOL, path_env=path_env)
    assert r.returncode == 0
    assert r.stdout.strip() == b"{}"
    # Recording drain should NOT have been invoked (no argv.log).
    assert not (fake_bin / "argv.log").exists(), (
        "drain must NOT be invoked on PreToolUse outside optimize_mode"
    )


def test_userpromptsubmit_hands_off_when_opt_flag_missing(tmp_path):
    """UserPromptSubmit without optimize_mode flag → hand off (might
    detect /optimize in the prompt)."""
    _scaffold_evo_run(tmp_path, with_marker=False)
    fake_bin = tmp_path / "fake-bin"
    _write_recording_drain(fake_bin)
    path_env = f"{fake_bin}{_path_separator()}{_base_path()}"
    r = _run_hook(
        tmp_path,
        b'{"session_id":"test-sid","hook_event_name":"UserPromptSubmit","prompt":"/optimize do x"}',
        path_env=path_env,
    )
    assert r.returncode == 0
    # drain WAS invoked — argv.log exists.
    assert (fake_bin / "argv.log").exists(), (
        "drain must be invoked on UserPromptSubmit to detect /optimize"
    )


def test_userpromptsubmit_fast_exits_when_opt_flag_already_set(tmp_path):
    """UserPromptSubmit + optimize_mode already on → fast exit (no
    need to re-detect /optimize, already armed)."""
    _scaffold_with_opt_flag(tmp_path)
    fake_bin = tmp_path / "fake-bin"
    _write_recording_drain(fake_bin)
    path_env = f"{fake_bin}{_path_separator()}{_base_path()}"
    r = _run_hook(
        tmp_path,
        b'{"session_id":"test-sid","hook_event_name":"UserPromptSubmit","prompt":"more work"}',
        path_env=path_env,
    )
    assert r.returncode == 0
    assert r.stdout.strip() == b"{}"
    # drain NOT invoked — optimize already on, no need to re-detect.
    assert not (fake_bin / "argv.log").exists(), (
        "drain must NOT be invoked for UserPromptSubmit when optimize_mode "
        "is already on (skip the prompt-pattern re-check)"
    )


def test_marker_alone_still_hands_off(tmp_path):
    """Backward compat: marker file alone (no optimize_mode flag) still
    triggers handoff. This is the original `evo direct` delivery path."""
    _scaffold_evo_run(tmp_path, with_marker=True)
    fake_bin = tmp_path / "fake-bin"
    _write_recording_drain(fake_bin)
    path_env = f"{fake_bin}{_path_separator()}{_base_path()}"
    r = _run_hook(tmp_path, PAYLOAD_PRETOOL, path_env=path_env)
    assert r.returncode == 0
    assert (fake_bin / "argv.log").exists(), (
        "marker present must trigger handoff regardless of optimize_mode"
    )


def test_handoff_passes_host_arg(tmp_path):
    """The Rust hook must pass `--host <host>` to evo-drain so the
    Python side knows which envelope to emit."""
    _scaffold_evo_run(tmp_path, with_marker=True)
    fake_bin = tmp_path / "fake-bin"
    _write_recording_drain(fake_bin)
    path_env = f"{fake_bin}{_path_separator()}{_base_path()}"
    r = _run_hook(tmp_path, PAYLOAD_PRETOOL, path_env=path_env)
    assert r.returncode == 0
    argv_log = fake_bin / "argv.log"
    assert argv_log.exists()
    args = argv_log.read_text().splitlines()
    assert "--host" in args, f"--host must be in argv, got {args}"
    host_idx = args.index("--host")
    assert host_idx + 1 < len(args), "--host must have a value"
    host_val = args[host_idx + 1]
    # claude-code is the default when stdin doesn't carry a host hint.
    assert host_val in {"claude-code", "codex", "hermes", "opencode"}, (
        f"--host value should be a known host, got {host_val!r}"
    )


def test_codex_env_var_overrides_path_fragment(tmp_path):
    """Codex's stdin payload doesn't always include `.codex/` path
    fragments. When CODEX_THREAD_ID is set, host must be detected as
    `codex` regardless of path fragments — otherwise the codex
    `$evo:optimize` pattern matcher never runs."""
    sid = "codex-sess-123"
    _scaffold_evo_run(tmp_path, sid=sid, with_marker=True)
    fake_bin = tmp_path / "fake-bin"
    _write_recording_drain(fake_bin)
    env = os.environ.copy()
    env["PATH"] = f"{fake_bin}{_path_separator()}{_base_path()}"
    # Set CODEX_THREAD_ID; clear all other host env vars.
    for v in ("CLAUDE_CODE_SESSION_ID", "HERMES_SESSION_ID", "OPENCODE_SESSION_ID"):
        env.pop(v, None)
    env["CODEX_THREAD_ID"] = sid
    payload = f'{{"session_id":"{sid}","hook_event_name":"PreToolUse"}}'.encode()
    r = subprocess.run(
        [str(HOOK_PATH)],
        input=payload, cwd=str(tmp_path), env=env,
        capture_output=True, timeout=10,
    )
    assert r.returncode == 0
    args = (fake_bin / "argv.log").read_text().splitlines()
    host_idx = args.index("--host")
    assert args[host_idx + 1] == "codex", (
        f"CODEX_THREAD_ID env should imply host=codex; got --host {args[host_idx + 1]!r}"
    )


def test_claude_code_env_var_implies_host(tmp_path):
    """CLAUDE_CODE_SESSION_ID set → host=claude-code."""
    sid = "claude-sess-456"
    _scaffold_evo_run(tmp_path, sid=sid, with_marker=True)
    fake_bin = tmp_path / "fake-bin"
    _write_recording_drain(fake_bin)
    env = os.environ.copy()
    env["PATH"] = f"{fake_bin}{_path_separator()}{_base_path()}"
    for v in ("CODEX_THREAD_ID", "HERMES_SESSION_ID", "OPENCODE_SESSION_ID"):
        env.pop(v, None)
    env["CLAUDE_CODE_SESSION_ID"] = sid
    payload = f'{{"session_id":"{sid}","hook_event_name":"PreToolUse"}}'.encode()
    r = subprocess.run(
        [str(HOOK_PATH)],
        input=payload, cwd=str(tmp_path), env=env,
        capture_output=True, timeout=10,
    )
    assert r.returncode == 0
    args = (fake_bin / "argv.log").read_text().splitlines()
    host_idx = args.index("--host")
    assert args[host_idx + 1] == "claude-code"


def test_nested_env_payload_session_id_wins(tmp_path):
    """When both CLAUDE_CODE_SESSION_ID and CODEX_THREAD_ID are set,
    the session_id in the stdin payload must take precedence."""
    sid_from_payload = "payload-wins-sid"
    # Scaffold with the payload sid as the registered session.
    _scaffold_evo_run(tmp_path, sid=sid_from_payload, with_marker=True)
    fake_bin = tmp_path / "fake-bin"
    _write_recording_drain(fake_bin)
    env = os.environ.copy()
    env["PATH"] = f"{fake_bin}{_path_separator()}{_base_path()}"
    env["CLAUDE_CODE_SESSION_ID"] = "env-claude-sid"
    env["CODEX_THREAD_ID"] = "env-codex-sid"
    payload = (
        f'{{"session_id":"{sid_from_payload}","hook_event_name":"PreToolUse"}}'
    ).encode()
    r = subprocess.run(
        [str(HOOK_PATH)],
        input=payload,
        cwd=str(tmp_path),
        env=env,
        capture_output=True,
        timeout=10,
    )
    assert r.returncode == 0
    args = (fake_bin / "argv.log").read_text().splitlines()
    sid_idx = args.index("--session")
    assert args[sid_idx + 1] == sid_from_payload, (
        f"payload session_id must win over env; got --session {args[sid_idx + 1]!r}"
    )


def test_nested_env_host_matches_payload_sid_owner(tmp_path):
    """When both CODEX_THREAD_ID and CLAUDE_CODE_SESSION_ID are set with
    DIFFERENT values, and the payload sid matches CODEX_THREAD_ID, host
    must be `codex` — NOT the first-listed env var. Detection by
    value-match, not presence."""
    payload_sid = "codex-matched-sid"
    _scaffold_evo_run(tmp_path, sid=payload_sid, with_marker=True)
    fake_bin = tmp_path / "fake-bin"
    _write_recording_drain(fake_bin)
    env = os.environ.copy()
    env["PATH"] = f"{fake_bin}{_path_separator()}{_base_path()}"
    env["CLAUDE_CODE_SESSION_ID"] = "claude-sid-different"
    env["CODEX_THREAD_ID"] = payload_sid  # matches payload
    payload = (
        f'{{"session_id":"{payload_sid}","hook_event_name":"PreToolUse"}}'
    ).encode()
    r = subprocess.run(
        [str(HOOK_PATH)],
        input=payload, cwd=str(tmp_path), env=env,
        capture_output=True, timeout=10,
    )
    assert r.returncode == 0
    args = (fake_bin / "argv.log").read_text().splitlines()
    host_idx = args.index("--host")
    assert args[host_idx + 1] == "codex", (
        f"host must follow value-match: payload sid matches CODEX_THREAD_ID, "
        f"so host=codex; got {args[host_idx + 1]!r}"
    )
    sid_idx = args.index("--session")
    assert args[sid_idx + 1] == payload_sid


def test_session_start_honors_evo_exp_id_env(tmp_path):
    """When EVO_EXP_ID is set in the subagent's env at SessionStart,
    the Rust register_session must record it on the session JSON so
    the first drain routes to the exp queue, not the workspace queue."""
    sid = "subagent-cc"
    # Workspace exists but session not yet registered.
    run = tmp_path / ".evo" / "run_test"
    (run / "inject" / "sessions").mkdir(parents=True)

    fake_bin = tmp_path / "fake-bin"
    _write_fake_drain(fake_bin)
    env = os.environ.copy()
    env["PATH"] = f"{fake_bin}{_path_separator()}{_base_path()}"
    env["EVO_EXP_ID"] = "exp_0042"

    r = subprocess.run(
        [str(HOOK_PATH)],
        input=f'{{"session_id":"{sid}","hook_event_name":"SessionStart"}}'.encode(),
        cwd=str(tmp_path),
        env=env,
        capture_output=True,
        timeout=10,
    )
    assert r.returncode == 0
    rec_path = run / "inject" / "sessions" / f"{sid}.json"
    assert rec_path.exists()
    import json
    rec = json.loads(rec_path.read_text())
    assert rec.get("exp_id") == "exp_0042", (
        f"Rust register_session must honor EVO_EXP_ID; got {rec!r}"
    )


def test_rust_writes_iso_timestamp_python_can_parse(tmp_path):
    """Rust's iso8601_utc_now must emit a form Python 3.10+
    `datetime.fromisoformat()` can parse. The earlier `Z`-suffix form
    was rejected on 3.10, causing list_active_sessions to GC valid
    sessions as stale — which broke `evo direct --to exp_id` fanout
    because the target subagent's record had just been deleted."""
    sid = "ts-parse-sess"
    run = tmp_path / ".evo" / "run_test"
    (run / "inject" / "sessions").mkdir(parents=True)

    fake_bin = tmp_path / "fake-bin"
    _write_fake_drain(fake_bin)
    env = os.environ.copy()
    env["PATH"] = f"{fake_bin}{_path_separator()}{_base_path()}"

    r = subprocess.run(
        [str(HOOK_PATH)],
        input=f'{{"session_id":"{sid}","hook_event_name":"SessionStart"}}'.encode(),
        cwd=str(tmp_path), env=env, capture_output=True, timeout=10,
    )
    assert r.returncode == 0
    import json
    import datetime as dt
    rec = json.loads((run / "inject" / "sessions" / f"{sid}.json").read_text())
    # Both timestamp fields must be parseable on the supported Python
    # version range (>=3.10 per pyproject).
    for field in ("registered_at", "last_seen_at"):
        ts_str = rec[field]
        # The actual fix: writer should use `+00:00`, NOT `Z`.
        assert not ts_str.endswith("Z"), (
            f"Rust ISO timestamp uses Z suffix which Python 3.10 "
            f"fromisoformat() doesn't accept; got {ts_str!r}"
        )
        # Sanity: parses without error.
        dt.datetime.fromisoformat(ts_str)


def test_legacy_session_record_exp_id_is_NOT_auto_migrated(tmp_path):
    """Deliberate non-migration: pre-v0.4.4 subagent records have
    exp_id=null but new code does NOT auto-fill it from EVO_EXP_ID.
    Rationale: a parent orchestrator that somehow inherited
    EVO_EXP_ID in its env would silently get demoted to subagent and
    lose /evo:optimize steering. Pre-v0.4.4 subagents are short-lived
    anyway; users restart them to pick up the new exp_id flow."""
    sid = "legacy-sub"
    run = tmp_path / ".evo" / "run_test"
    (run / "inject" / "sessions").mkdir(parents=True)
    (run / "inject" / "markers").mkdir(parents=True)

    import json
    legacy = {
        "schema_version": 1,
        "session_id": sid,
        "host": "claude-code",
        "pid": 99999,
        "registered_at": "2025-12-01T00:00:00+00:00",
        "last_seen_at": "2025-12-01T00:00:00+00:00",
        "exp_id": None,
        "parent_session_id": None,
    }
    (run / "inject" / "sessions" / f"{sid}.json").write_text(json.dumps(legacy))

    venv_bin = REPO_ROOT / ".venv" / "bin"
    if not (venv_bin / "evo-drain").exists():
        return
    env = os.environ.copy()
    env["PATH"] = f"{venv_bin}{_path_separator()}{_base_path()}"
    env["EVO_EXP_ID"] = "exp_should_not_propagate"

    r = subprocess.run(
        [str(HOOK_PATH)],
        input=f'{{"session_id":"{sid}","hook_event_name":"UserPromptSubmit","prompt":"continue"}}'.encode(),
        cwd=str(tmp_path), env=env, capture_output=True, timeout=15,
    )
    assert r.returncode == 0
    rec = json.loads((run / "inject" / "sessions" / f"{sid}.json").read_text())
    # The exp_id MUST stay null — never auto-promote on existing records.
    assert rec.get("exp_id") is None, (
        f"existing record's exp_id must NOT be silently overwritten by env; "
        f"got {rec.get('exp_id')!r}. Auto-migration would risk demoting "
        f"an orchestrator whose env carried a stale EVO_EXP_ID."
    )


def test_session_start_engages_orchestrator(tmp_path):
    """SessionStart on a hook host IS the orchestrator engagement signal.
    The Rust register_session must write has_evo_engaged: true so the
    Python `evo direct` broadcast doesn't filter the orchestrator out as
    skipped_unengaged. Regression for the release-smoke fanout=0 failure
    on claude-code/codex under /optimize."""
    sid = "orch-engage-cc"
    run = tmp_path / ".evo" / "run_test"
    (run / "inject" / "sessions").mkdir(parents=True)

    fake_bin = tmp_path / "fake-bin"
    _write_fake_drain(fake_bin)
    env = os.environ.copy()
    env["PATH"] = f"{fake_bin}{_path_separator()}{_base_path()}"
    env.pop("EVO_EXP_ID", None)

    r = subprocess.run(
        [str(HOOK_PATH)],
        input=f'{{"session_id":"{sid}","hook_event_name":"SessionStart"}}'.encode(),
        cwd=str(tmp_path), env=env, capture_output=True, timeout=10,
    )
    assert r.returncode == 0
    import json
    rec = json.loads((run / "inject" / "sessions" / f"{sid}.json").read_text())
    assert rec.get("has_evo_engaged") is True, (
        f"Rust register_session must engage the orchestrator at SessionStart; "
        f"got {rec!r}"
    )
    assert rec.get("engaged_at") is not None


def test_session_start_does_not_engage_subagent(tmp_path):
    """A subagent registration (EVO_EXP_ID set) must NOT engage — a
    subagent never joins the workspace loop on its own."""
    sid = "sub-no-engage-cc"
    run = tmp_path / ".evo" / "run_test"
    (run / "inject" / "sessions").mkdir(parents=True)

    fake_bin = tmp_path / "fake-bin"
    _write_fake_drain(fake_bin)
    env = os.environ.copy()
    env["PATH"] = f"{fake_bin}{_path_separator()}{_base_path()}"
    env["EVO_EXP_ID"] = "exp_0042"

    r = subprocess.run(
        [str(HOOK_PATH)],
        input=f'{{"session_id":"{sid}","hook_event_name":"SessionStart"}}'.encode(),
        cwd=str(tmp_path), env=env, capture_output=True, timeout=10,
    )
    assert r.returncode == 0
    import json
    rec = json.loads((run / "inject" / "sessions" / f"{sid}.json").read_text())
    assert rec.get("exp_id") == "exp_0042"
    assert rec.get("has_evo_engaged") is False, (
        f"subagent registration must not engage; got {rec!r}"
    )


def test_session_start_writes_null_exp_id_without_env(tmp_path):
    """No EVO_EXP_ID env → exp_id stays null (orchestrator-class)."""
    sid = "orchestrator-cc"
    run = tmp_path / ".evo" / "run_test"
    (run / "inject" / "sessions").mkdir(parents=True)

    fake_bin = tmp_path / "fake-bin"
    _write_fake_drain(fake_bin)
    env = os.environ.copy()
    env["PATH"] = f"{fake_bin}{_path_separator()}{_base_path()}"
    env.pop("EVO_EXP_ID", None)

    r = subprocess.run(
        [str(HOOK_PATH)],
        input=f'{{"session_id":"{sid}","hook_event_name":"SessionStart"}}'.encode(),
        cwd=str(tmp_path),
        env=env,
        capture_output=True,
        timeout=10,
    )
    assert r.returncode == 0
    rec_path = run / "inject" / "sessions" / f"{sid}.json"
    import json
    rec = json.loads(rec_path.read_text())
    assert rec.get("exp_id") is None


def test_resume_userpromptsubmit_lazy_registers_session(tmp_path):
    """Resumed claude-code sessions don't fire SessionStart, so the
    session may not be in the registry when UserPromptSubmit arrives.
    The Rust hook must lazy-register on UserPromptSubmit and hand off
    to Python so the /optimize matcher can fire."""
    sid = "resumed-sess"
    # Build a workspace WITHOUT scaffolding a session file (simulates
    # resume against a workspace where this session never ran SessionStart).
    run = tmp_path / ".evo" / "run_test"
    (run / "inject" / "sessions").mkdir(parents=True)
    (run / "inject" / "markers").mkdir(parents=True)
    # No session file. No marker. No opt flag.

    fake_bin = tmp_path / "fake-bin"
    _write_recording_drain(fake_bin)
    path_env = f"{fake_bin}{_path_separator()}{_base_path()}"
    r = _run_hook(
        tmp_path,
        f'{{"session_id":"{sid}","hook_event_name":"UserPromptSubmit","prompt":"/evo:optimize"}}'.encode(),
        path_env=path_env,
    )
    assert r.returncode == 0
    # Session file should now exist (lazy-registered).
    assert (run / "inject" / "sessions" / f"{sid}.json").exists(), (
        "UserPromptSubmit on unregistered session must lazy-register"
    )
    # Drain should have been invoked.
    assert (fake_bin / "argv.log").exists(), (
        "lazy-registered UserPromptSubmit must still hand off to drain"
    )


def test_resume_pretooluse_still_fast_exits_when_unregistered(tmp_path):
    """For PreToolUse (and other non-SessionStart, non-UserPromptSubmit
    events), an unregistered session still fast-exits. We only lazy-
    register on the prompt path so optimize-mode detection works; other
    events with no session record stay quiet."""
    sid = "ghost-sess"
    run = tmp_path / ".evo" / "run_test"
    (run / "inject" / "sessions").mkdir(parents=True)
    (run / "inject" / "markers").mkdir(parents=True)

    fake_bin = tmp_path / "fake-bin"
    _write_recording_drain(fake_bin)
    path_env = f"{fake_bin}{_path_separator()}{_base_path()}"
    r = _run_hook(
        tmp_path,
        f'{{"session_id":"{sid}","hook_event_name":"PreToolUse"}}'.encode(),
        path_env=path_env,
    )
    assert r.returncode == 0
    assert r.stdout.strip() == b"{}"
    # No drain invocation for unregistered non-prompt event.
    assert not (fake_bin / "argv.log").exists()


def test_nested_env_host_falls_back_when_no_env_matches(tmp_path):
    """Payload sid + no env var matches it → fall back to path-fragment
    detection. With no `.codex/` etc. in the payload, default is claude-code."""
    payload_sid = "orphan-sid"
    _scaffold_evo_run(tmp_path, sid=payload_sid, with_marker=True)
    fake_bin = tmp_path / "fake-bin"
    _write_recording_drain(fake_bin)
    env = os.environ.copy()
    env["PATH"] = f"{fake_bin}{_path_separator()}{_base_path()}"
    env["CODEX_THREAD_ID"] = "unrelated-codex-sid"
    env["CLAUDE_CODE_SESSION_ID"] = "unrelated-claude-sid"
    payload = (
        f'{{"session_id":"{payload_sid}","hook_event_name":"PreToolUse"}}'
    ).encode()
    r = subprocess.run(
        [str(HOOK_PATH)],
        input=payload, cwd=str(tmp_path), env=env,
        capture_output=True, timeout=10,
    )
    assert r.returncode == 0
    args = (fake_bin / "argv.log").read_text().splitlines()
    host_idx = args.index("--host")
    # Path doesn't contain `.codex/` etc., so default is claude-code.
    assert args[host_idx + 1] == "claude-code", (
        f"no env match + no path fragment → claude-code default; got {args[host_idx + 1]!r}"
    )

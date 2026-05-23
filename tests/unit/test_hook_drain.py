"""Subprocess tests for bin/evo-hook-drain — the cross-platform Rust binary.

Covers:
  - Binary exists at the expected per-platform path
  - Fast-path latency (regression guard: median <40ms — Rust cold-start
    is ~2-5ms; budget covers CI runner noise)
  - Branch 1: bare `evo-drain` on PATH → spawn it
  - Branch 3 (fallback): evo-drain not on PATH → actionable error
  - SessionStart drift warning when cache version != marketplace clone version
  - SessionStart proactive warning when evo-drain not on PATH

The Rust source lives at plugins/evo/bin/evo-hook-drain-rs/. Tests
require the release binary built into the plugin bin/ via `cargo build
--release` + a copy step (CI does this in ci.yml `unit-tests` job;
locally run `cargo build --release` inside the Rust crate then copy the
binary to plugins/evo/bin/).
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
HOOK_PATH = REPO_ROOT / "plugins" / "evo" / "bin" / HOOK_NAME
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
        # On Windows the .cmd shim's argv path is awkward to record; use
        # a simpler Python-via-py shim. The cargo-built binary spawns
        # CreateProcess regardless.
        drain = fake_bin / "evo-drain.cmd"
        drain.write_text(
            f"@echo off\r\n"
            f'(echo %*) > "{(fake_bin / "argv.log").as_posix()}"\r\n'
            f"echo {{}}\r\nexit /b 0\r\n"
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

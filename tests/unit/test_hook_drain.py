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

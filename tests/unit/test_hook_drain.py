"""Subprocess tests for bin/evo-hook-drain — the bash hot path.

Covers:
  - Fast-path latency (regression guard <5ms median; design budget is 5-7ms p99)
  - Branch 1: bare `evo-drain` on PATH → exec it
  - Branch 3 (fallback): neither evo-drain nor uv available → actionable error
  - SessionStart drift warning when cache version != marketplace clone version
  - SessionStart proactive warning when evo-drain not on PATH

Each test scaffolds a fake .evo/ run dir and invokes the script via
subprocess with explicit PATH control.
"""

from __future__ import annotations

import os
import statistics
import subprocess
import sys
import time
from pathlib import Path

import pytest

IS_WINDOWS = sys.platform == "win32"

# Cross-platform invocation: spawn the script through `bash` explicitly
# instead of relying on the shebang. Native Windows doesn't honor shebangs
# and raises OSError [WinError 193] on direct exec, but GitHub's
# windows-latest ships Git Bash on PATH (the workflow already sets
# `shell: bash` by default), so `bash <script>` runs the real script on
# every CI platform — the tests stay honest on Windows.
BASH = "bash"

# Two tests can't run meaningfully on Git Bash for Windows:
#   - the 5ms latency budget is a POSIX-bash contract; Git Bash cold
#     startup on Windows is ~50-150ms, so the budget assertion would only
#     measure shell overhead, not the hot path.
#   - tests that construct a custom PATH (Branch 1 / Branch 3 fallbacks,
#     SessionStart drain-present / drain-missing variants) depend on
#     POSIX PATH semantics: `:` as separator and absolute paths like
#     /usr/bin:/bin. On Windows, `:` collides with drive letters and the
#     POSIX paths don't exist as written, so the tests would assert on
#     MSYS translation quirks rather than hook behavior.
skip_on_windows_latency = pytest.mark.skipif(
    IS_WINDOWS,
    reason="5ms budget is a POSIX-bash contract; Git Bash startup on Windows is ~50-150ms",
)
skip_on_windows_posix_path = pytest.mark.skipif(
    IS_WINDOWS,
    reason="test constructs POSIX PATH (`:` separator, /usr/bin) that doesn't translate to MSYS on Windows",
)


REPO_ROOT = Path(__file__).resolve().parent.parent.parent
HOOK_PATH = REPO_ROOT / "plugins" / "evo" / "bin" / "evo-hook-drain"
PAYLOAD_PRETOOL = b'{"session_id":"test-sid","hook_event_name":"PreToolUse"}'
PAYLOAD_SESSION_START = b'{"session_id":"test-sid","hook_event_name":"SessionStart"}'


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
        [BASH, str(HOOK_PATH)],
        input=payload,
        cwd=str(cwd),
        env=env,
        capture_output=True,
        timeout=10,
    )


def test_hook_path_exists():
    assert HOOK_PATH.exists(), f"hook not found at {HOOK_PATH}"


def test_bash_syntax_valid():
    """Catch typos before they hit users."""
    r = subprocess.run(["bash", "-n", str(HOOK_PATH)], capture_output=True)
    assert r.returncode == 0, f"bash syntax error: {r.stderr.decode()}"


def test_fast_path_no_evo_dir_exits_clean(tmp_path):
    """No .evo/ in cwd → line 48 fast-exit, stdout {}, exit 0."""
    r = _run_hook(tmp_path, PAYLOAD_PRETOOL)
    assert r.returncode == 0
    assert r.stdout.strip() == b"{}"
    assert r.stderr == b""


def test_fast_path_no_session_id_exits_clean(tmp_path):
    """No session_id in payload, no env var → line 27 fast-exit."""
    # Strip env vars that would provide a fallback session id
    env_no_sid = {k: v for k, v in os.environ.items()
                  if k not in {"CLAUDE_CODE_SESSION_ID", "CODEX_THREAD_ID",
                               "HERMES_SESSION_ID", "OPENCODE_SESSION_ID"}}
    r = subprocess.run(
        [BASH, str(HOOK_PATH)], input=b'{"hook_event_name":"PreToolUse"}',
        cwd=str(tmp_path), env=env_no_sid, capture_output=True, timeout=10,
    )
    assert r.returncode == 0
    assert r.stdout.strip() == b"{}"


@skip_on_windows_latency
def test_fast_path_latency_under_5ms_median(tmp_path):
    """Regression guard: fast-path median must stay under design budget.

    Script comment says ~5-7ms p99; we check median<5ms with 30 iterations
    so CI flakes don't dominate. If anyone ever adds work to the hot path,
    this test fails.
    """
    times_ms = []
    for _ in range(30):
        t0 = time.perf_counter()
        r = subprocess.run(
            [BASH, str(HOOK_PATH)], input=PAYLOAD_PRETOOL,
            cwd=str(tmp_path), capture_output=True, timeout=5,
        )
        times_ms.append((time.perf_counter() - t0) * 1000)
        assert r.returncode == 0
    median_ms = statistics.median(times_ms)
    assert median_ms < 5.0, (
        f"fast-path regressed: median={median_ms:.2f}ms (budget <5ms). "
        f"Check evo-hook-drain wasn't accidentally given network or fork work."
    )


@skip_on_windows_posix_path
def test_branch_1_bare_evo_drain_exec(tmp_path, monkeypatch):
    """When evo-drain is on PATH, hook execs it (exit 0 from fake)."""
    _scaffold_evo_run(tmp_path)
    # Make a fake evo-drain on PATH that just prints {} and exits 0
    fake_bin = tmp_path / "fake-bin"
    fake_bin.mkdir()
    fake_drain = fake_bin / "evo-drain"
    fake_drain.write_text("#!/bin/bash\necho '{}'\nexit 0\n")
    fake_drain.chmod(0o755)
    path_env = f"{fake_bin}:/usr/bin:/bin"
    r = _run_hook(tmp_path, PAYLOAD_PRETOOL, path_env=path_env)
    assert r.returncode == 0
    assert r.stdout.strip() == b"{}"


@skip_on_windows_posix_path
def test_branch_3_no_drain_no_uv_emits_actionable_error(tmp_path):
    """Neither evo-drain nor uv → exit 1 with install hint on stderr."""
    _scaffold_evo_run(tmp_path)
    # Empty PATH (just core dirs without evo-drain or uv)
    r = _run_hook(tmp_path, PAYLOAD_PRETOOL, path_env="/usr/bin:/bin")
    assert r.returncode == 1
    assert b"install evo-hq-cli" in r.stderr
    assert b"uv tool install evo-hq-cli" in r.stderr
    assert r.stdout.strip() == b"{}"  # still emit valid JSON for hook contract


@skip_on_windows_posix_path
def test_session_start_warns_when_drain_missing(tmp_path):
    """SessionStart fires → proactive warning that evo-drain isn't on PATH."""
    _scaffold_evo_run(tmp_path)
    r = _run_hook(tmp_path, PAYLOAD_SESSION_START, path_env="/usr/bin:/bin")
    # SessionStart drains unconditionally, so we'll hit Branch 3 + the
    # proactive nudge. Both go to stderr.
    assert b"install evo-hq-cli to enable mid-run inject" in r.stderr


@skip_on_windows_posix_path
def test_session_start_emits_cache_stale_warning(tmp_path, monkeypatch):
    """Stage marketplace clone with newer version than 'cache' → warning."""
    # Build a fake Claude Code layout: cache at 0.4.0, marketplace at 0.4.1.
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
    # Copy the real hook into the fake cache so its BASH_SOURCE path is
    # under .claude/plugins/cache/ (that's what triggers the host-path
    # detection inside the SessionStart drift block).
    (cache_root / "bin").mkdir()
    fake_hook = cache_root / "bin" / "evo-hook-drain"
    fake_hook.write_text(HOOK_PATH.read_text())
    fake_hook.chmod(0o755)
    # Stage .evo to push past fast-exits
    _scaffold_evo_run(tmp_path)
    env = {**os.environ, "HOME": str(fake_home), "PATH": "/usr/bin:/bin"}
    r = subprocess.run(
        [BASH, str(fake_hook)], input=PAYLOAD_SESSION_START,
        cwd=str(tmp_path), env=env, capture_output=True, timeout=10,
    )
    assert b"plugin cache is stale" in r.stderr
    assert b"running 0.4.0" in r.stderr
    assert b"marketplace has 0.4.1" in r.stderr
    assert b"evo update --force" in r.stderr


@skip_on_windows_posix_path
def test_session_start_silent_when_drain_present(tmp_path):
    """SessionStart with evo-drain on PATH → no nudge, drain runs."""
    _scaffold_evo_run(tmp_path)
    fake_bin = tmp_path / "fake-bin"
    fake_bin.mkdir()
    fake_drain = fake_bin / "evo-drain"
    fake_drain.write_text("#!/bin/bash\necho '{}'\nexit 0\n")
    fake_drain.chmod(0o755)
    r = _run_hook(tmp_path, PAYLOAD_SESSION_START,
                  path_env=f"{fake_bin}:/usr/bin:/bin")
    assert b"install evo-hq-cli" not in r.stderr
    assert r.returncode == 0

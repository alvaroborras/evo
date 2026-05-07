"""Live test: cmd_gc tears down a leaked E2B sandbox via the new dispatcher.

Regression coverage for the fix landed in c64a8a7 ("gc: per-node dispatch +
cross-backend orphan sweep"). Before that fix, cmd_gc skipped every remote
node because of a host-side worktree.exists() check, so RemoteBackend.gc()
was unreachable and stale sandboxes accumulated and kept billing.

This test exercises the real dispatch end-to-end:
  1. Provision a real E2B sandbox via `evo new --remote e2b`.
  2. Capture its provider native_id and confirm `is_alive` returns True.
  3. Inject `leased_by: None` into remote_state.json (simulating a stale
     sandbox: lease was released but the container is still running).
  4. Invoke `evo gc` as a subprocess.
  5. Confirm the sandbox is torn down on the provider side
     (provider.is_alive returns False or raises) AND that the state
     entry is gone.

Skipped unless BOTH `EVO_LIVE_TEST_E2B=1` AND `E2B_API_KEY` are set.
Requires the optional `e2b` SDK.
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


def _gate() -> None:
    if os.environ.get("EVO_LIVE_TEST_E2B") != "1":
        print("SKIPPED (set EVO_LIVE_TEST_E2B=1 to enable)")
        sys.exit(0)
    if not os.environ.get("E2B_API_KEY"):
        print("SKIPPED (set E2B_API_KEY to enable)")
        sys.exit(0)
    try:
        import e2b  # noqa: F401
    except ImportError:
        print("SKIPPED (e2b SDK not installed)")
        sys.exit(0)


def _evo(args: list[str], cwd: Path, *, check: bool = True, timeout: int = 600):
    result = subprocess.run(
        ["uv", "run", "--project", str(PLUGIN_ROOT), "evo", *args],
        cwd=cwd, check=False, capture_output=True, text=True, timeout=timeout,
    )
    if check and result.returncode != 0:
        raise RuntimeError(
            f"evo {' '.join(args)} failed (rc={result.returncode}):\n"
            f"STDOUT:\n{result.stdout}\nSTDERR:\n{result.stderr}"
        )
    return result


def _build_repo(workdir: Path) -> Path:
    repo = workdir / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=repo, check=True)
    (repo / "agent.py").write_text("STATE = 'baseline'\n", encoding="utf-8")
    (repo / "eval.py").write_text(
        "import os, json\n"
        "from pathlib import Path\n"
        "p = Path(os.environ['EVO_RESULT_PATH'])\n"
        "p.parent.mkdir(parents=True, exist_ok=True)\n"
        "p.write_text(json.dumps({'score': 1.0, 'tasks': {}}))\n"
        "print(json.dumps({'score': 1.0}))\n",
        encoding="utf-8",
    )
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "baseline"], cwd=repo, check=True)
    return repo


def _read_remote_state(repo: Path) -> tuple[Path, dict]:
    """Find and return (path, contents) of the E2B remote_state file."""
    state_dir = repo / ".evo" / "run_0000" / "backend_state"
    candidates = list(state_dir.glob("remote-*.json"))
    assert len(candidates) == 1, f"expected one remote_state file, got {candidates}"
    path = candidates[0]
    return path, json.loads(path.read_text(encoding="utf-8"))


def _is_alive_via_provider(native_id: str) -> bool:
    """Probe whether the E2B sandbox still exists by attempting to connect.
    Returns True if it responds, False if the SDK reports gone."""
    from evo.backends.sandbox_providers.e2b import E2BProvider
    from evo.backends.protocol import SandboxHandle
    provider = E2BProvider({})
    handle = SandboxHandle(
        provider="e2b", base_url="", bearer_token="",
        native_id=native_id, metadata={},
    )
    try:
        return provider.is_alive(handle)
    except Exception:
        return False


def test_remote_gc_tears_down_leaked_sandbox() -> None:
    workdir = Path(tempfile.mkdtemp(prefix="evo-e2b-gc-"))
    repo = _build_repo(workdir)
    native_id: str | None = None

    try:
        _evo(
            ["init", "--target", "agent.py",
             "--benchmark", "python eval.py",
             "--metric", "max", "--host", "generic"],
            cwd=repo,
        )
        print("--- evo init OK ---")

        # Provision a real E2B sandbox by allocating an experiment.
        t0 = time.monotonic()
        _evo(
            ["new", "--parent", "root", "-m", "gc-leak-test",
             "--remote", "e2b",
             "--provider-config", "template=base,timeout_seconds=300"],
            cwd=repo,
            timeout=300,
        )
        print(f"--- evo new (provisions sandbox): {time.monotonic() - t0:.1f}s ---")

        # Capture the sandbox's native_id from remote_state.
        state_path, state = _read_remote_state(repo)
        assert state["sandboxes"], f"no sandbox provisioned: {state}"
        sandbox = state["sandboxes"][0]
        native_id = sandbox["native_id"]
        print(f"--- provisioned sandbox native_id={native_id} ---")
        assert _is_alive_via_provider(native_id), \
            "freshly-provisioned sandbox should be alive"
        print("--- confirmed sandbox is alive on provider ---")

        # Simulate the leak: clear leased_by but leave the sandbox running.
        # This is exactly the state cmd_gc must reach via the new dispatcher.
        # (Before the fix, it would never call RemoteBackend.gc for remote
        #  nodes because of the host-side worktree.exists() filter.)
        state["sandboxes"][0]["leased_by"] = None
        state_path.write_text(json.dumps(state), encoding="utf-8")
        print("--- injected leak (leased_by=None) ---")

        # The fix under test: cmd_gc dispatches per node["backend"] and
        # additionally calls backend.sweep_orphans which tears down stale
        # remote sandboxes.
        t0 = time.monotonic()
        gc_out = _evo(["gc"], cwd=repo, timeout=120)
        print(f"--- evo gc: {time.monotonic() - t0:.1f}s ---")
        print(gc_out.stdout.strip())

        # Verify state is cleared
        state_after = json.loads(state_path.read_text(encoding="utf-8"))
        assert not any(
            sb.get("native_id") == native_id for sb in state_after["sandboxes"]
        ), f"sandbox {native_id} should have been removed from state, got {state_after}"
        print(f"--- state confirms {native_id} removed ---")

        # Verify the container is actually gone provider-side.
        # Allow up to ~10s for E2B to propagate the kill.
        gone = False
        for attempt in range(10):
            if not _is_alive_via_provider(native_id):
                gone = True
                break
            time.sleep(1)
        assert gone, (
            f"sandbox {native_id} still alive on E2B after evo gc — "
            f"the dispatch fix didn't reach RemoteBackend.gc / "
            f"provider.tear_down"
        )
        print(f"--- confirmed {native_id} torn down on E2B ---")

        # Mark so the finally-block cleanup doesn't try to nuke twice
        native_id = None
    finally:
        # Backstop: if anything left the sandbox alive, tear it down via
        # `evo reset` so we don't leak billable containers.
        try:
            if native_id is not None and _is_alive_via_provider(native_id):
                print(f"--- backstop: tearing down {native_id} via evo reset ---")
                _evo(["reset", "--yes"], cwd=repo, check=False)
        except Exception as exc:
            print(f"--- backstop cleanup error: {exc} ---")
        shutil.rmtree(workdir, ignore_errors=True)


def main() -> None:
    _gate()
    test_remote_gc_tears_down_leaked_sandbox()
    print("ALL OK")


if __name__ == "__main__":
    main()

"""Live test: cmd_gc tears down a leaked remote sandbox via the new dispatcher.

Regression coverage for the fix landed in c64a8a7 ("gc: per-node dispatch +
cross-backend orphan sweep"). Before that fix, cmd_gc skipped every remote
node because of a host-side worktree.exists() check, so RemoteBackend.gc()
was unreachable and stale sandboxes accumulated and kept billing.

The same scenario is exercised against each provider:
  1. Provision a real sandbox via `evo new --remote <provider>`.
  2. Capture its provider native_id and confirm `is_alive` returns True.
  3. Inject `leased_by: None` into remote_state.json (simulating a stale
     sandbox: lease was released but the container is still running).
  4. Invoke `evo gc` as a subprocess.
  5. Confirm the sandbox is torn down on the provider side
     (`is_alive` returns False) AND that the state entry is gone.

Each provider has its own env gate:
  - E2B:   EVO_LIVE_TEST_E2B=1 + E2B_API_KEY
  - Modal: EVO_LIVE_TEST_MODAL=1 + ~/.modal.toml authenticated (or
                                    MODAL_TOKEN_ID / MODAL_TOKEN_SECRET)
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
from typing import Callable

REPO_ROOT = Path(__file__).resolve().parents[2]
PLUGIN_ROOT = REPO_ROOT / "plugins" / "evo"
PLUGIN_SRC = PLUGIN_ROOT / "src"
sys.path.insert(0, str(PLUGIN_SRC))


# --------------------------------------------------------------------------- #
# Per-provider gates                                                          #
# --------------------------------------------------------------------------- #

def _gate_e2b() -> None:
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


def _gate_modal() -> None:
    if os.environ.get("EVO_LIVE_TEST_MODAL") != "1":
        print("SKIPPED (set EVO_LIVE_TEST_MODAL=1 to enable)")
        sys.exit(0)
    try:
        import modal  # noqa: F401
    except ImportError:
        print("SKIPPED (modal SDK not installed)")
        sys.exit(0)
    # Verify modal is authenticated (either via env tokens or ~/.modal.toml)
    try:
        import modal
        app = modal.App("evo-gc-test-auth-probe")
        with app.run():
            pass
    except Exception as exc:
        print(f"SKIPPED (modal auth failed: {exc})")
        sys.exit(0)


# --------------------------------------------------------------------------- #
# Per-provider is_alive probes                                                #
# --------------------------------------------------------------------------- #

def _e2b_is_alive(native_id: str) -> bool:
    from evo.backends.sandbox_providers.e2b import E2BProvider
    from evo.backends.protocol import SandboxHandle
    handle = SandboxHandle(
        provider="e2b", base_url="", bearer_token="",
        native_id=native_id, metadata={},
    )
    try:
        return E2BProvider({}).is_alive(handle)
    except Exception:
        return False


def _modal_is_alive(native_id: str) -> bool:
    from evo.backends.sandbox_providers.modal import ModalProvider
    from evo.backends.protocol import SandboxHandle
    handle = SandboxHandle(
        provider="modal", base_url="", bearer_token="",
        native_id=native_id, metadata={},
    )
    try:
        return ModalProvider({}).is_alive(handle)
    except Exception:
        return False


# --------------------------------------------------------------------------- #
# Shared scenario                                                             #
# --------------------------------------------------------------------------- #

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
    state_dir = repo / ".evo" / "run_0000" / "backend_state"
    candidates = list(state_dir.glob("remote-*.json"))
    assert len(candidates) == 1, f"expected one remote_state file, got {candidates}"
    path = candidates[0]
    return path, json.loads(path.read_text(encoding="utf-8"))


def _run_gc_leak_scenario(
    provider_label: str,
    new_args: list[str],
    is_alive_fn: Callable[[str], bool],
    *,
    teardown_propagation_seconds: int = 10,
) -> None:
    """The shared end-to-end test body.

    1. evo init + evo new (provisions real sandbox via given provider)
    2. Capture native_id from remote_state, confirm sandbox alive
    3. Inject leak (leased_by=None)
    4. Run `evo gc`
    5. Verify state cleared and provider confirms sandbox gone
    """
    workdir = Path(tempfile.mkdtemp(prefix=f"evo-{provider_label}-gc-"))
    repo = _build_repo(workdir)
    native_id: str | None = None

    try:
        _evo(
            ["init", "--target", "agent.py",
             "--benchmark", "python eval.py",
             "--metric", "max", "--host", "generic", "--per-exp-timeout", "1800"],
            cwd=repo,
        )
        print(f"--- [{provider_label}] evo init OK ---")

        t0 = time.monotonic()
        _evo(
            ["new", "--parent", "root", "-m", f"{provider_label}-gc-leak", *new_args],
            cwd=repo, timeout=300,
        )
        print(f"--- [{provider_label}] evo new (provisions sandbox): "
              f"{time.monotonic() - t0:.1f}s ---")

        state_path, state = _read_remote_state(repo)
        assert state["sandboxes"], f"no sandbox provisioned: {state}"
        sandbox = state["sandboxes"][0]
        native_id = sandbox["native_id"]
        print(f"--- [{provider_label}] sandbox native_id={native_id} ---")
        assert is_alive_fn(native_id), \
            f"freshly-provisioned {provider_label} sandbox should be alive"
        print(f"--- [{provider_label}] confirmed alive on provider ---")

        state["sandboxes"][0]["leased_by"] = None
        state_path.write_text(json.dumps(state), encoding="utf-8")
        print(f"--- [{provider_label}] injected leak (leased_by=None) ---")

        t0 = time.monotonic()
        gc_out = _evo(["gc"], cwd=repo, timeout=120)
        print(f"--- [{provider_label}] evo gc: {time.monotonic() - t0:.1f}s ---")
        print(gc_out.stdout.strip())

        state_after = json.loads(state_path.read_text(encoding="utf-8"))
        assert not any(
            sb.get("native_id") == native_id for sb in state_after["sandboxes"]
        ), f"sandbox {native_id} still in state after gc: {state_after}"
        print(f"--- [{provider_label}] state confirms {native_id} removed ---")

        gone = False
        for _ in range(teardown_propagation_seconds):
            if not is_alive_fn(native_id):
                gone = True
                break
            time.sleep(1)
        assert gone, (
            f"sandbox {native_id} still alive on {provider_label} after evo gc — "
            f"the dispatch fix didn't reach RemoteBackend.gc / provider.tear_down"
        )
        print(f"--- [{provider_label}] confirmed {native_id} torn down on provider ---")

        native_id = None  # success — nothing for the finally-block to reap
    finally:
        try:
            if native_id is not None and is_alive_fn(native_id):
                print(f"--- [{provider_label}] backstop: tearing down "
                      f"{native_id} via evo reset ---")
                _evo(["reset", "--yes"], cwd=repo, check=False)
        except Exception as exc:
            print(f"--- [{provider_label}] backstop cleanup error: {exc} ---")
        shutil.rmtree(workdir, ignore_errors=True)


# --------------------------------------------------------------------------- #
# Scenario A: graph-entry-deleted leak                                        #
# --------------------------------------------------------------------------- #
# Realistic causes: `evo reset --yes` interrupted mid-cleanup, manual
# graph.json edit, partial-write recovery. The sandbox state file still
# carries an entry whose `leased_by` exp_id no longer exists in the graph.
# The per-node loop in cmd_gc can't see this (no node to iterate on);
# only sweep_orphans catches it.
# --------------------------------------------------------------------------- #

def _run_gc_scenario_a(
    provider_label: str,
    new_args: list[str],
    is_alive_fn: Callable[[str], bool],
    *,
    teardown_propagation_seconds: int = 10,
) -> None:
    workdir = Path(tempfile.mkdtemp(prefix=f"evo-{provider_label}-gcA-"))
    repo = _build_repo(workdir)
    native_id: str | None = None

    try:
        _evo(
            ["init", "--target", "agent.py",
             "--benchmark", "python eval.py",
             "--metric", "max", "--host", "generic", "--per-exp-timeout", "1800"],
            cwd=repo,
        )
        _evo(["new", "--parent", "root", "-m", f"{provider_label}-A", *new_args],
             cwd=repo, timeout=300)

        state_path, state = _read_remote_state(repo)
        sandbox = state["sandboxes"][0]
        native_id = sandbox["native_id"]
        leased_by_exp = (sandbox.get("leased_by") or {}).get("exp_id")
        assert is_alive_fn(native_id)
        print(f"--- [A:{provider_label}] sandbox {native_id} provisioned, "
              f"leased_by={leased_by_exp} ---")

        # Delete the experiment node from graph.json. Sandbox stays leased
        # to a now-missing exp_id — the realistic post-reset / partial-write
        # leak shape.
        graph_path = repo / ".evo" / "run_0000" / "graph.json"
        graph = json.loads(graph_path.read_text(encoding="utf-8"))
        del graph["nodes"][leased_by_exp]
        graph["nodes"]["root"]["children"] = [
            cid for cid in graph["nodes"]["root"].get("children", [])
            if cid != leased_by_exp
        ]
        graph_path.write_text(json.dumps(graph), encoding="utf-8")
        print(f"--- [A:{provider_label}] removed {leased_by_exp} from graph; "
              f"sandbox now orphaned ---")

        gc_out = _evo(["gc"], cwd=repo, timeout=120)
        print(gc_out.stdout.strip())

        state_after = json.loads(state_path.read_text(encoding="utf-8"))
        assert not any(sb.get("native_id") == native_id
                       for sb in state_after["sandboxes"]), \
            f"sandbox {native_id} still in state after gc: {state_after}"

        gone = False
        for _ in range(teardown_propagation_seconds):
            if not is_alive_fn(native_id):
                gone = True
                break
            time.sleep(1)
        assert gone, (
            f"orphaned sandbox {native_id} still alive on {provider_label} "
            f"after evo gc — sweep_orphans didn't catch the missing-graph-entry case"
        )
        print(f"--- [A:{provider_label}] orphaned {native_id} torn down ---")
        native_id = None
    finally:
        try:
            if native_id is not None and is_alive_fn(native_id):
                print(f"--- [A:{provider_label}] backstop reset ---")
                _evo(["reset", "--yes"], cwd=repo, check=False)
        except Exception as exc:
            print(f"--- [A:{provider_label}] backstop error: {exc} ---")
        shutil.rmtree(workdir, ignore_errors=True)


# --------------------------------------------------------------------------- #
# Scenario B: failed-status node with cleared lease                           #
# --------------------------------------------------------------------------- #
# Realistic cause: orchestrator marked the node as failed and cleared the
# lease, but crashed before tear_down could fire. Per-node dispatch in
# cmd_gc sees status="failed", calls backend.gc(node), which iterates
# sandboxes and tears down ones with leased_by=None.
# --------------------------------------------------------------------------- #

def _run_gc_scenario_b(
    provider_label: str,
    new_args: list[str],
    is_alive_fn: Callable[[str], bool],
    *,
    teardown_propagation_seconds: int = 10,
) -> None:
    workdir = Path(tempfile.mkdtemp(prefix=f"evo-{provider_label}-gcB-"))
    repo = _build_repo(workdir)
    native_id: str | None = None

    try:
        _evo(
            ["init", "--target", "agent.py",
             "--benchmark", "python eval.py",
             "--metric", "max", "--host", "generic", "--per-exp-timeout", "1800"],
            cwd=repo,
        )
        _evo(["new", "--parent", "root", "-m", f"{provider_label}-B", *new_args],
             cwd=repo, timeout=300)

        state_path, state = _read_remote_state(repo)
        sandbox = state["sandboxes"][0]
        native_id = sandbox["native_id"]
        leased_by_exp = (sandbox.get("leased_by") or {}).get("exp_id")
        assert is_alive_fn(native_id)
        print(f"--- [B:{provider_label}] sandbox {native_id} provisioned ---")

        # Mark the node failed AND clear the lease — simulates orchestrator
        # crash between _mark_failed and tear_down.
        graph_path = repo / ".evo" / "run_0000" / "graph.json"
        graph = json.loads(graph_path.read_text(encoding="utf-8"))
        graph["nodes"][leased_by_exp]["status"] = "failed"
        graph_path.write_text(json.dumps(graph), encoding="utf-8")
        state["sandboxes"][0]["leased_by"] = None
        state_path.write_text(json.dumps(state), encoding="utf-8")
        print(f"--- [B:{provider_label}] {leased_by_exp} status=failed, lease cleared ---")

        gc_out = _evo(["gc"], cwd=repo, timeout=120)
        print(gc_out.stdout.strip())

        state_after = json.loads(state_path.read_text(encoding="utf-8"))
        assert not any(sb.get("native_id") == native_id
                       for sb in state_after["sandboxes"]), \
            f"sandbox {native_id} still in state after gc: {state_after}"

        gone = False
        for _ in range(teardown_propagation_seconds):
            if not is_alive_fn(native_id):
                gone = True
                break
            time.sleep(1)
        assert gone, (
            f"failed-node sandbox {native_id} still alive on {provider_label} "
            f"after evo gc — per-node dispatch didn't reach RemoteBackend.gc"
        )
        print(f"--- [B:{provider_label}] failed-node {native_id} torn down ---")
        native_id = None
    finally:
        try:
            if native_id is not None and is_alive_fn(native_id):
                print(f"--- [B:{provider_label}] backstop reset ---")
                _evo(["reset", "--yes"], cwd=repo, check=False)
        except Exception as exc:
            print(f"--- [B:{provider_label}] backstop error: {exc} ---")
        shutil.rmtree(workdir, ignore_errors=True)


# --------------------------------------------------------------------------- #
# Per-provider test functions                                                 #
# --------------------------------------------------------------------------- #

E2B_NEW_ARGS = ["--remote", "e2b",
                "--provider-config", "template=base,timeout_seconds=300"]
MODAL_NEW_ARGS = ["--remote", "modal"]
MODAL_TEARDOWN_SECONDS = 20  # Modal needs a few extra seconds to propagate


def test_remote_gc_e2b() -> None:
    _gate_e2b()
    _run_gc_leak_scenario(
        "e2b", E2B_NEW_ARGS, _e2b_is_alive,
    )


def test_remote_gc_modal() -> None:
    _gate_modal()
    _run_gc_leak_scenario(
        "modal", MODAL_NEW_ARGS, _modal_is_alive,
        teardown_propagation_seconds=MODAL_TEARDOWN_SECONDS,
    )


def test_remote_gc_e2b_orphaned_after_graph_delete() -> None:
    _gate_e2b()
    _run_gc_scenario_a(
        "e2b", E2B_NEW_ARGS, _e2b_is_alive,
    )


def test_remote_gc_modal_orphaned_after_graph_delete() -> None:
    _gate_modal()
    _run_gc_scenario_a(
        "modal", MODAL_NEW_ARGS, _modal_is_alive,
        teardown_propagation_seconds=MODAL_TEARDOWN_SECONDS,
    )


def test_remote_gc_e2b_failed_node_with_no_lease() -> None:
    _gate_e2b()
    _run_gc_scenario_b(
        "e2b", E2B_NEW_ARGS, _e2b_is_alive,
    )


def test_remote_gc_modal_failed_node_with_no_lease() -> None:
    _gate_modal()
    _run_gc_scenario_b(
        "modal", MODAL_NEW_ARGS, _modal_is_alive,
        teardown_propagation_seconds=MODAL_TEARDOWN_SECONDS,
    )


def main() -> None:
    """Run all provider tests that are gate-eligible."""
    ran_any = False
    if (os.environ.get("EVO_LIVE_TEST_E2B") == "1"
            and os.environ.get("E2B_API_KEY")):
        test_remote_gc_e2b()
        test_remote_gc_e2b_orphaned_after_graph_delete()
        test_remote_gc_e2b_failed_node_with_no_lease()
        ran_any = True
    if os.environ.get("EVO_LIVE_TEST_MODAL") == "1":
        test_remote_gc_modal()
        test_remote_gc_modal_orphaned_after_graph_delete()
        test_remote_gc_modal_failed_node_with_no_lease()
        ran_any = True
    if not ran_any:
        print("SKIPPED (no provider gates set)")
        sys.exit(0)
    print("ALL OK")


if __name__ == "__main__":
    main()

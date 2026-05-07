"""Tests for cmd_gc dispatch across worktree / pool / remote.

The current cmd_gc has three code-traceable bugs the new dispatcher fixes:

  1. Remote nodes are silently skipped because the host-side
     `worktree.exists()` filter (cli.py:2812) returns False for any path
     that lives inside a sandbox container — they never exist on the
     orchestrator host. So `RemoteBackend.gc()` is never called and
     stale sandboxes leak.

  2. Hybrid workspaces (some nodes use --backend worktree, others
     --backend remote via per-experiment override) only get the
     workspace-default backend cleaned. Nodes assigned a different
     backend leak.

  3. Resources whose graph entry has been deleted (e.g., post-`evo
     reset`, manually-edited graph) are invisible to the per-node
     loop — there's no node to iterate. They leak forever.

Each test below sets up the bug, asserts the broken behavior is what
we currently produce (so the test fails after the fix lands and we
can re-assert the post-fix behavior). After the fix, each test will
invert: assert the cleanup actually happened.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import tempfile
import unittest
from pathlib import Path


def _init_git_repo(root: Path) -> str:
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.email", "test@evo"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=root, check=True)
    subprocess.run(["git", "config", "commit.gpgsign", "false"], cwd=root, check=True)
    (root / "README.md").write_text("init\n")
    subprocess.run(["git", "add", "."], cwd=root, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=root, check=True)
    return subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=root, check=True, capture_output=True, text=True,
    ).stdout.strip()


class _RecordingProvider:
    """SandboxProvider stub that records tear_down calls for assertion.

    Implements only the methods cmd_gc + RemoteBackend.gc need to call
    (tear_down). Provision is never called — tests inject sandbox state
    directly into remote_state.json.
    """

    name = "test-recorder"

    def __init__(self):
        self.tore_down: list[str] = []

    def provision(self, spec):
        raise NotImplementedError("tests don't provision")

    def tear_down(self, handle) -> None:
        self.tore_down.append(handle.native_id)

    def is_alive(self, handle) -> bool:
        return True

    def build_client(self, handle):
        raise NotImplementedError("tests don't run")


def _make_node(exp_id: str, parent: str, status: str, backend: str = "worktree", **kw) -> dict:
    base = {
        "id": exp_id, "parent": parent, "children": [], "status": status,
        "hypothesis": kw.get("hypothesis", f"hyp for {exp_id}"),
        "created_at": "2026-01-01T00:00:00Z",
        "updated_at": "2026-01-01T00:00:00Z",
        "eval_epoch": 1, "score": kw.get("score"),
        "branch": f"evo/run_0000/{exp_id}",
        "worktree": str(Path("/tmp") / f"evo-mock-{exp_id}"),
        "commit": kw.get("commit"),
        "pruned_reason": None, "gates": [],
        "current_attempt": 0, "notes": [],
        "backend": backend,
    }
    base.update(kw)
    return base


def _build_workspace(root: Path, nodes: dict, *, default_backend: str = "worktree") -> Path:
    """Build a minimal .evo/run_0000/ workspace; nodes is {id: node_dict}."""
    from evo import core
    evo_dir = root / ".evo"
    run_dir = evo_dir / "run_0000"
    run_dir.mkdir(parents=True, exist_ok=True)
    (evo_dir / "meta.json").write_text(json.dumps({"active": "run_0000", "next_run": 1}))
    (run_dir / "config.json").write_text(json.dumps({
        "metric": "max",
        "execution_backend": default_backend,
        "current_eval_epoch": 1,
    }))
    graph = core.default_graph()
    for nid, node_data in nodes.items():
        graph["nodes"][nid] = node_data
        parent = node_data.get("parent")
        if parent and parent in graph["nodes"]:
            graph["nodes"][parent].setdefault("children", []).append(nid)
    (run_dir / "graph.json").write_text(json.dumps(graph))
    (run_dir / "annotations.json").write_text(json.dumps({"annotations": []}))
    (run_dir / "infra_log.json").write_text(json.dumps({"events": []}))
    return run_dir


def _run_gc(root: Path) -> dict:
    """Invoke cmd_gc and return parsed JSON output."""
    from evo.cli import cmd_gc
    import os
    prev = os.getcwd()
    os.chdir(root)
    try:
        # Capture stdout
        import io
        import sys
        buf = io.StringIO()
        old_stdout = sys.stdout
        sys.stdout = buf
        try:
            ns = argparse.Namespace()
            cmd_gc(ns)
        finally:
            sys.stdout = old_stdout
        try:
            return json.loads(buf.getvalue())
        except json.JSONDecodeError:
            return {"raw_output": buf.getvalue()}
    finally:
        os.chdir(prev)


class TestRemoteSandboxLeak(unittest.TestCase):
    """Bug 1: cmd_gc never calls RemoteBackend.gc() because the host-side
    `worktree.exists()` filter rejects every remote node (their worktree
    paths live inside containers, not on the host)."""

    def test_remote_node_with_released_lease_is_not_gced(self):
        """A remote sandbox whose lease was released (leased_by=None)
        but whose container is still alive — RemoteBackend.gc() exists
        and can reclaim it via provider.tear_down. cmd_gc must invoke
        it. Today's cmd_gc never reaches the backend.gc call for remote.
        """
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            _init_git_repo(root)

            # A committed remote node with a sandbox-internal worktree path.
            run_dir = _build_workspace(root, {
                "exp_0001": _make_node(
                    "exp_0001", "root", "committed",
                    backend="remote",
                    score=0.7, commit="abc123",
                    worktree="/workspace/repo",  # in-sandbox path
                ),
            }, default_backend="remote")

            # Inject remote_state.json with a stale (lease-released) sandbox.
            recorder = _RecordingProvider()
            from evo.backends import remote as remote_mod
            from evo.backends.protocol import SandboxHandle

            # Compute the same state_key the backend would use
            from evo.backends.state_keys import backend_state_key
            state_key = backend_state_key("remote", {
                "provider": "test-recorder",
                "provider_config": {},
            })
            state_path = run_dir / f"remote_state__{state_key}.json"
            # Try to find the actual state file path the backend expects
            # by importing remote_state directly.
            from evo.backends import remote_state
            state_path = remote_state.remote_state_path(root, state_key)
            state_path.parent.mkdir(parents=True, exist_ok=True)
            state_path.write_text(json.dumps({
                "provider": "test-recorder",
                "provider_config": {},
                "next_id": 1,
                "sandboxes": [
                    {
                        "id": 0,
                        "native_id": "stale-container-xyz",
                        "leased_by": None,  # lease was released
                        "provisioned_at": "2026-01-01T00:00:00Z",
                        "base_url": "http://x",
                        "bearer_token": "tok",
                    }
                ]
            }))

            # Configure the workspace to use our recording provider so
            # backend.gc() calls go to a known-mock.
            from evo import core
            config_path = run_dir / "config.json"
            config = json.loads(config_path.read_text())
            config["execution_backend"] = "remote"
            config["execution_backend_config"] = {
                "provider": "test-recorder",
                "provider_config": {},
            }
            config_path.write_text(json.dumps(config))

            # Patch the backend loader to inject our recorder
            import evo.backends as backends_pkg
            real_loader = backends_pkg.load_backend

            def mock_loader(rt, *, node=None, explicit_name=None,
                            explicit_config=None, workspace_config=None):
                from evo.backends.remote import RemoteSandboxBackend
                return RemoteSandboxBackend(
                    provider=recorder,
                    provider_name="test-recorder",
                    provider_config={},
                )

            backends_pkg.load_backend = mock_loader
            try:
                _run_gc(root)
            finally:
                backends_pkg.load_backend = real_loader

            # POST-FIX: the recorder should have torn down the stale sandbox
            self.assertIn(
                "stale-container-xyz", recorder.tore_down,
                f"Expected RemoteBackend.gc to tear down the stale sandbox; "
                f"got tore_down={recorder.tore_down}. "
                f"This is the bug: cmd_gc skips remote nodes via "
                f"worktree.exists() filter at cli.py:2812."
            )


class TestHybridWorkspace(unittest.TestCase):
    """Bug 2: cmd_gc loads only the workspace-default backend. A workspace
    with mixed backends (per-experiment --backend overrides) only gets
    one backend cleaned."""

    def test_hybrid_worktree_and_remote(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            _init_git_repo(root)
            run_dir = _build_workspace(root, {
                # worktree-backed node (default)
                "exp_0001": _make_node(
                    "exp_0001", "root", "committed",
                    backend="worktree", score=0.7, commit="abc",
                ),
                # remote-backed node (override)
                "exp_0002": _make_node(
                    "exp_0002", "root", "committed",
                    backend="remote", score=0.7, commit="def",
                    worktree="/workspace/repo",
                ),
            }, default_backend="worktree")

            # Set up remote_state with a sandbox to reclaim
            from evo.backends import remote_state
            from evo.backends.state_keys import backend_state_key
            state_key = backend_state_key("remote", {
                "provider": "test-recorder", "provider_config": {},
            })
            state_path = remote_state.remote_state_path(root, state_key)
            state_path.parent.mkdir(parents=True, exist_ok=True)
            state_path.write_text(json.dumps({
                "provider": "test-recorder",
                "provider_config": {},
                "next_id": 1,
                "sandboxes": [{
                    "id": 0, "native_id": "hybrid-stale",
                    "leased_by": None,
                    "provisioned_at": "2026-01-01T00:00:00Z",
                    "base_url": "http://x", "bearer_token": "t",
                }]
            }))

            recorder = _RecordingProvider()
            import evo.backends as backends_pkg
            real_loader = backends_pkg.load_backend

            def mock_loader(rt, *, node=None, explicit_name=None,
                            explicit_config=None, workspace_config=None):
                # Dispatch on requested backend
                want = explicit_name
                if node is not None and node.get("backend"):
                    want = node["backend"]
                if not want and workspace_config:
                    want = workspace_config.get("execution_backend")
                if want == "remote":
                    from evo.backends.remote import RemoteSandboxBackend
                    return RemoteSandboxBackend(
                        provider=recorder, provider_name="test-recorder",
                        provider_config={},
                    )
                return real_loader(rt, node=node, explicit_name=explicit_name,
                                   explicit_config=explicit_config,
                                   workspace_config=workspace_config)

            backends_pkg.load_backend = mock_loader
            try:
                _run_gc(root)
            finally:
                backends_pkg.load_backend = real_loader

            self.assertIn(
                "hybrid-stale", recorder.tore_down,
                f"Hybrid workspace: cmd_gc must clean ALL backends in use, "
                f"not just the default. Got tore_down={recorder.tore_down}."
            )


class TestPoolOrphanLease(unittest.TestCase):
    """Bug 3: a pool slot lease pointing at an exp_id that's no longer in
    the graph (e.g., post-`evo reset` survivor, manually-edited graph)
    is invisible to the per-node loop in cmd_gc — there's no node to
    iterate."""

    def test_pool_orphan_lease_cleared_by_gc(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            _init_git_repo(root)
            slot_a = root / "slot-a"
            slot_b = root / "slot-b"
            for slot in (slot_a, slot_b):
                subprocess.run(["git", "clone", "-q", str(root), str(slot)],
                               check=True)
            run_dir = _build_workspace(root, {}, default_backend="pool")

            # Inject a pool_state.json with a lease pointing at an
            # exp_id that doesn't exist in the graph (orphan from a
            # vanished node).
            from evo.backends import pool_state
            from evo.backends.state_keys import backend_state_key
            state_key = backend_state_key("pool", {"slots": [str(slot_a), str(slot_b)]})
            state_path = pool_state.pool_state_path(root, state_key)
            state_path.parent.mkdir(parents=True, exist_ok=True)
            state_path.write_text(json.dumps({
                "slots": [
                    {"path": str(slot_a),
                     "leased_by": {"exp_id": "exp_VANISHED", "pid": 99999,
                                   "leased_at": "2026-01-01T00:00:00Z"},
                     "last_branch": None},
                    {"path": str(slot_b), "leased_by": None,
                     "last_branch": None},
                ]
            }))

            # Configure pool backend
            config_path = run_dir / "config.json"
            config = json.loads(config_path.read_text())
            config["execution_backend"] = "pool"
            config["execution_backend_config"] = {"slots": [str(slot_a), str(slot_b)]}
            config_path.write_text(json.dumps(config))

            _run_gc(root)

            # POST-FIX: orphan lease should have been cleared
            after = json.loads(state_path.read_text())
            slot_a_record = next(s for s in after["slots"] if s["path"] == str(slot_a))
            self.assertIsNone(
                slot_a_record["leased_by"],
                f"Pool orphan lease must be cleared by cmd_gc; got {slot_a_record['leased_by']}. "
                f"The current cmd_gc per-node loop can't see this because the orphan "
                f"exp_id has no graph entry."
            )


if __name__ == "__main__":
    unittest.main()

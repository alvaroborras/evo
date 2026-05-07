from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from . import DISTRIBUTION_NAME, __version__
from .core import (
    SUPPORTED_HOSTS,
    _load_meta,
    add_gate,
    add_workspace_note,
    append_annotation,
    append_infra_event,
    ascii_tree,
    atomic_write_json,
    attempt_dir,
    attempt_log_path,
    attempt_outcome_path,
    attempt_traces_dir,
    collect_gates_from_path,
    compare_scores,
    config_path,
    current_branch,
    delete_discarded_experiment,
    evo_dir,
    experiment_log_path,
    experiment_result_path,
    experiments_dir_for,
    fill_command_template,
    frontier_nodes,
    get_host,
    graph_path,
    init_workspace,
    list_all_notes,
    load_annotations,
    load_config,
    load_graph,
    lock_file_for,
    mark_comparison_blocked,
    maybe_commit_worktree,
    node_target_path,
    load_result,
    parse_diff_patch,
    parse_score,
    path_to_node,
    project_path,
    relative_target,
    remove_gate,
    repo_root,
    reset_runtime_state,
    resolve_runtime_env,
    runtime_env_summary,
    save_config,
    set_host,
    update_node,
    utc_now,
    worktrees_path,
    workspace_path,
    allocate_experiment,
    remove_worktree_only,
    render_git_diff,
)
from .locking import advisory_lock
from .scratchpad import build_scratchpad


def _require_workspace(root: Path) -> tuple[dict, dict]:
    config = load_config(root)
    if not config:
        raise RuntimeError("workspace is not initialized; run `uv run evo init ...` first")
    return config, load_graph(root)


def _read_node(root: Path, exp_id: str) -> dict:
    graph = load_graph(root)
    try:
        return graph["nodes"][exp_id]
    except KeyError as exc:
        raise RuntimeError(f"unknown experiment: {exp_id}") from exc


def _anchor_commit_ref(root: Path, run_id: str, exp_id: str, commit: str) -> None:
    """Pin <commit> via `refs/evo-anchor/<run_id>/<exp_id>` so it survives
    `git branch -D` (which `evo discard` runs). Without this anchor, a
    discarded committed node's commit becomes unreachable and is vulnerable
    to git GC.

    Hard-fails on update-ref errors. The pre-fix call used check=False and
    silently swallowed failures, leaving the orchestrator's ODB with the
    commit objects but no ref keeping them alive.
    """
    result = subprocess.run(
        ["git", "update-ref", f"refs/evo-anchor/{run_id}/{exp_id}", commit],
        cwd=root, check=False, capture_output=True, text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"failed to anchor commit {commit} via "
            f"refs/evo-anchor/{run_id}/{exp_id}: "
            f"{result.stderr.strip() or 'unknown error'}"
        )


def _resolve_parent_score(graph: dict, parent_id: str) -> float | None:
    if parent_id == "root":
        return None
    parent = graph["nodes"][parent_id]
    return parent.get("score")


def _update_graph_and_write(root: Path, graph: dict) -> None:
    with advisory_lock(lock_file_for(graph_path(root))):
        atomic_write_json(graph_path(root), graph)


def _pick_free_port(preferred: int, max_tries: int = 20) -> int:
    """Find a free TCP port on 127.0.0.1, starting from *preferred* and
    incrementing by 1 on collision. Raises if nothing free in *max_tries*."""
    import socket
    for offset in range(max_tries):
        port = preferred + offset
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 0)
            try:
                s.bind(("127.0.0.1", port))
                return port
            except OSError:
                continue
    raise RuntimeError(
        f"no free port in range {preferred}..{preferred + max_tries - 1}"
    )


def _start_dashboard_background(root: Path, port: int = 8080) -> None:
    """Start the dashboard as a background process.

    Probes for a free port starting at *port* (auto-increments on collision),
    writes the actual port to .evo/dashboard.port, and prints a clickable URL.
    """
    pid_file = evo_dir(root) / "dashboard.pid"
    port_file = evo_dir(root) / "dashboard.port"

    # If already running, surface the existing URL instead of starting a second.
    if pid_file.exists():
        try:
            pid = int(pid_file.read_text().strip())
            os.kill(pid, 0)
            existing = port_file.read_text().strip() if port_file.exists() else str(port)
            print(f"Dashboard live: http://127.0.0.1:{existing} (pid {pid})")
            return
        except (OSError, ValueError):
            pid_file.unlink(missing_ok=True)

    actual_port = _pick_free_port(port)

    env = os.environ.copy()
    env["EVO_DASHBOARD_PORT"] = str(actual_port)

    proc = subprocess.Popen(
        [sys.executable, "-m", "evo.dashboard"],
        cwd=root,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
        env=env,
    )
    pid_file.write_text(str(proc.pid))
    port_file.write_text(str(actual_port))
    note = "" if actual_port == port else f" (port {port} busy, bumped to {actual_port})"
    print(f"Dashboard live: http://127.0.0.1:{actual_port} (pid {proc.pid}){note}")


def _parse_provider_config_arg(raw: str | None) -> dict[str, str]:
    config: dict[str, str] = {}
    if not raw:
        return config
    for pair in raw.split(","):
        pair = pair.strip()
        if not pair:
            continue
        if "=" not in pair:
            raise RuntimeError(
                f"--provider-config entries must be key=value, got {pair!r}"
            )
        key, _, value = pair.partition("=")
        config[key.strip()] = value.strip()
    return config


def _parse_workspaces_arg(raw: str | None) -> list[str]:
    if not raw:
        return []
    return [p.strip() for p in raw.split(",") if p.strip()]


def _parse_remote_spec(spec: str) -> tuple[str, dict[str, str]]:
    """Parse `--remote <spec>` shorthand into provider name + config."""
    if spec == "modal":
        return "modal", {}
    if spec.startswith("ssh:"):
        host_spec = spec[len("ssh:"):]
        if not host_spec:
            raise RuntimeError("--remote ssh:... requires a non-empty host spec")
        config: dict[str, str] = {"host": host_spec}
        host, sep, port = host_spec.rpartition(":")
        if sep and host and port.isdigit():
            config = {"host": host, "port": port}
        return "ssh", config
    return spec, {}


def _resolve_backend_cli_args(
    *,
    root: Path,
    backend: str | None,
    workspaces_raw: str | None,
    provider: str | None,
    provider_config_raw: str | None,
    remote: str | None,
    require_backend: bool,
) -> tuple[str | None, dict[str, Any] | None]:
    """Normalize backend CLI flags into `(name, config)`."""
    workspaces = _parse_workspaces_arg(workspaces_raw)
    provider_config = _parse_provider_config_arg(provider_config_raw)

    if remote:
        if provider:
            raise RuntimeError(
                "--remote cannot be combined with --provider; it already picks a provider."
            )
        if workspaces:
            raise RuntimeError("--remote cannot be combined with --workspaces.")
        if backend and backend != "remote":
            raise RuntimeError("--remote implies --backend remote.")
        backend = "remote"
        remote_provider, remote_config = _parse_remote_spec(remote)
        provider = remote_provider
        provider_config = {**remote_config, **provider_config}

    if backend is None:
        if workspaces:
            raise RuntimeError(
                "--workspaces is only valid with --backend pool."
            )
        if provider:
            raise RuntimeError(
                "--provider is only valid with --backend remote."
            )
        if provider_config:
            raise RuntimeError(
                "--provider-config is only valid with --backend remote."
            )
        if require_backend:
            raise RuntimeError("backend is required")
        return None, None

    if backend == "worktree":
        if workspaces:
            raise RuntimeError(
                "--workspaces is only valid with --backend pool. "
                "Did you mean: --backend pool --workspaces ...?"
            )
        if provider:
            raise RuntimeError(
                "--provider is only valid with --backend remote."
            )
        if provider_config:
            raise RuntimeError(
                "--provider-config is only valid with --backend remote."
            )
        return "worktree", {}

    if backend == "pool":
        if provider:
            raise RuntimeError(
                "--provider is only valid with --backend remote."
            )
        if provider_config:
            raise RuntimeError(
                "--provider-config is only valid with --backend remote."
            )
        if not workspaces:
            raise RuntimeError("--backend pool requires --workspaces /a,/b,/c")
        _validate_pool_slots(root, workspaces)
        return "pool", {"slots": workspaces}

    if backend == "remote":
        if workspaces:
            raise RuntimeError(
                "--workspaces is only valid with --backend pool, not remote."
            )
        if not provider:
            raise RuntimeError(
                "--backend remote requires --provider <name> or --remote <spec>."
            )
        return "remote", {
            "provider": provider,
            "provider_config": provider_config,
        }

    raise RuntimeError(f"unknown backend {backend!r}")


def cmd_init(args: argparse.Namespace) -> int:
    root = repo_root()
    if args.metric not in {"max", "min"}:
        raise RuntimeError("--metric must be `max` or `min`")
    if args.host not in SUPPORTED_HOSTS:
        allowed = ", ".join(sorted(SUPPORTED_HOSTS))
        raise RuntimeError(f"--host must be one of: {allowed}")

    if args.commit_strategy is not None:
        commit_strategy = args.commit_strategy
    else:
        commit_strategy = "all"

    run_id = init_workspace(
        root,
        target=args.target,
        benchmark=args.benchmark,
        metric=args.metric,
        gate=args.gate,
        host=args.host,
        commit_strategy=commit_strategy,
        project_name=args.name,
    )
    if args.instrumentation_mode:
        meta_file = evo_dir(root) / "meta.json"
        meta = json.loads(meta_file.read_text(encoding="utf-8"))
        meta["instrumentation_mode"] = args.instrumentation_mode
        atomic_write_json(meta_file, meta)
    _start_dashboard_background(root, port=args.port)
    print(
        f"Initialized evo workspace {run_id} at {workspace_path(root)} "
        f"(host={args.host}, commit_strategy={commit_strategy})"
    )
    return 0


def _validate_pool_slots(root: Path, slot_paths: list[str]) -> None:
    """Sanity-check each pool slot at init time.

    Each path must exist, be a git working tree, and share an `origin`
    remote URL with the main repo. Slots are also canonicalized
    (`Path.resolve`) to detect duplicates, symlink aliases, the main repo
    itself, and overlapping/nested slot directories -- all of which would
    cause two experiments to share one physical checkout.

    Raises RuntimeError with a per-slot diagnostic on any failure.
    """
    main_origin = subprocess.run(
        ["git", "remote", "get-url", "origin"],
        cwd=root,
        check=False,
        capture_output=True,
        text=True,
    ).stdout.strip()
    repo_canonical = root.resolve()

    canonical: list[Path] = []
    for idx, path_str in enumerate(slot_paths):
        slot = Path(path_str)
        if not slot.is_absolute():
            raise RuntimeError(f"--workspaces[{idx}] must be absolute: {path_str}")
        if not slot.exists():
            raise RuntimeError(f"--workspaces[{idx}] does not exist: {slot}")

        slot_canonical = slot.resolve()

        # Reject the main repo itself: leasing it would let the next
        # `evo new` run `git checkout -B evo/...` against the user's working
        # branch -- silent data loss.
        if slot_canonical == repo_canonical:
            raise RuntimeError(
                f"--workspaces[{idx}] resolves to the main repo ({slot_canonical}). "
                f"A pool slot must be a separate clone; using main would let evo "
                f"check out experiment branches over your working tree."
            )

        if not (slot_canonical / ".git").exists():
            raise RuntimeError(
                f"--workspaces[{idx}] is not a git working tree: {slot_canonical}"
            )
        if main_origin:
            slot_origin = subprocess.run(
                ["git", "remote", "get-url", "origin"],
                cwd=slot_canonical,
                check=False,
                capture_output=True,
                text=True,
            ).stdout.strip()
            if slot_origin and slot_origin != main_origin:
                raise RuntimeError(
                    f"--workspaces[{idx}] origin mismatch: {slot_origin} vs main {main_origin}"
                )

        # Reject duplicates and overlap. Two slot paths must not resolve to
        # the same directory, and one slot must not be nested inside another.
        for j, prior in enumerate(canonical):
            if slot_canonical == prior:
                raise RuntimeError(
                    f"--workspaces[{idx}] ({slot_canonical}) is a duplicate of "
                    f"--workspaces[{j}] (same canonical path or symlink alias). "
                    f"Each slot must be a distinct directory."
                )
            try:
                slot_canonical.relative_to(prior)
                raise RuntimeError(
                    f"--workspaces[{idx}] ({slot_canonical}) is nested inside "
                    f"--workspaces[{j}] ({prior}). Pool slots cannot overlap."
                )
            except ValueError:
                pass
            try:
                prior.relative_to(slot_canonical)
                raise RuntimeError(
                    f"--workspaces[{idx}] ({slot_canonical}) contains "
                    f"--workspaces[{j}] ({prior}). Pool slots cannot overlap."
                )
            except ValueError:
                pass
        canonical.append(slot_canonical)


def cmd_host(args: argparse.Namespace) -> int:
    root = repo_root()
    action = args.host_action
    if action == "show":
        host = get_host(root)
        print(host if host else "<not set>")
        return 0
    if action == "set":
        set_host(root, args.value)
        print(f"host set to {args.value}")
        return 0
    raise RuntimeError(f"unknown host action: {action}")


def cmd_config(args: argparse.Namespace) -> int:
    if args.config_action == "show":
        return cmd_config_show(args)
    if args.config_action == "set":
        return cmd_config_set(args)
    if args.config_action == "backend":
        return cmd_config_backend(args)
    if args.config_action == "runtime":
        return cmd_config_runtime(args)
    raise RuntimeError(f"unknown config action: {args.config_action}")


def _redact_config_value(value: Any) -> Any:
    if isinstance(value, dict):
        redacted = {}
        for key, child in value.items():
            key_lower = str(key).lower()
            if key_lower in {
                "api_key",
                "bearer_token",
                "token",
                "secret",
                "password",
                "private_key",
                "ssh_private_key",
                "key",
            }:
                redacted[key] = "<redacted>" if child else child
            else:
                redacted[key] = _redact_config_value(child)
        return redacted
    if isinstance(value, list):
        return [_redact_config_value(item) for item in value]
    return value


def cmd_config_show(args: argparse.Namespace) -> int:
    root = repo_root()
    config, _graph = _require_workspace(root)
    data = _redact_config_value(config)
    if getattr(args, "json", False):
        print(json.dumps(data, indent=2))
        return 0

    print(f"target: {data.get('target', '')}")
    print(f"project_name: {data.get('project_name') or root.name}")
    print(f"benchmark: {data.get('benchmark', '')}")
    print(f"metric: {data.get('metric', '')}")
    print(f"host: {get_host(root) or '<not set>'}")
    print(f"commit_strategy: {data.get('commit_strategy', 'all')}")
    print(f"execution_backend: {data.get('execution_backend', 'worktree')}")
    backend_config = data.get("execution_backend_config") or {}
    if backend_config:
        print(f"execution_backend_config: {json.dumps(backend_config, sort_keys=True)}")
    runtime = data.get("runtime") or {}
    if runtime:
        print(f"runtime: {json.dumps(runtime, sort_keys=True)}")
    runtime_env = runtime_env_summary(root, config)
    print(f"runtime_env.inherit_shell: {str(runtime_env['inherit_shell']).lower()}")
    print(f"runtime_env.dotenv_sources: {len(runtime_env['dotenv'])}")
    return 0


def cmd_config_set(args: argparse.Namespace) -> int:
    root = repo_root()
    _require_workspace(root)
    with advisory_lock(lock_file_for(config_path(root))):
        config = load_config(root)
        if args.field == "project-name":
            config["project_name"] = args.value
        elif args.field == "target":
            config["target"] = args.value
        elif args.field == "benchmark":
            config["benchmark"] = args.value
        elif args.field == "metric":
            if args.value not in {"max", "min"}:
                raise RuntimeError("metric must be 'max' or 'min'")
            config["metric"] = args.value
        elif args.field == "commit-strategy":
            if args.value not in {"all", "tracked-only"}:
                raise RuntimeError("commit-strategy must be 'all' or 'tracked-only'")
            config["commit_strategy"] = args.value
        else:
            raise RuntimeError(f"unknown config field: {args.field}")
        atomic_write_json(config_path(root), config)
    print(f"config {args.field} set")
    return 0


def _ensure_runtime_config(config: dict[str, Any]) -> dict[str, Any]:
    runtime = config.setdefault("runtime", {})
    runtime.setdefault("prepare", None)
    runtime.setdefault("before_run", None)
    runtime.setdefault("prefix", None)
    return runtime


def _normalize_runtime_value(value: str | None) -> str | None:
    if value is None:
        return None
    stripped = value.strip()
    return stripped or None


def cmd_config_runtime(args: argparse.Namespace) -> int:
    root = repo_root()
    _require_workspace(root)

    if args.runtime_action == "show":
        config = load_config(root)
        runtime = _ensure_runtime_config(config)
        if getattr(args, "json", False):
            print(json.dumps(runtime, indent=2))
            return 0
        print(f"prepare: {runtime.get('prepare') or '<not set>'}")
        print(f"before_run: {runtime.get('before_run') or '<not set>'}")
        print(f"prefix: {runtime.get('prefix') or '<not set>'}")
        return 0

    if args.runtime_action == "set":
        updates = {
            key: _normalize_runtime_value(getattr(args, key))
            for key in ("prepare", "before_run", "prefix")
            if getattr(args, key) is not None
        }
        if not updates:
            raise RuntimeError("config runtime set requires at least one runtime flag")
        with advisory_lock(lock_file_for(config_path(root))):
            config = load_config(root)
            runtime = _ensure_runtime_config(config)
            runtime.update(updates)
            atomic_write_json(config_path(root), config)
        changed = ", ".join(sorted(updates))
        print(f"runtime config set: {changed}")
        return 0

    raise RuntimeError(f"unknown runtime action: {args.runtime_action}")


def cmd_config_backend(args: argparse.Namespace) -> int:
    from .backends import (
        backend_spec_for_node,
        backend_spec_from_config,
        backend_state_key,
        load_backend,
    )

    root = repo_root()
    _require_workspace(root)
    backend_name, backend_config = _resolve_backend_cli_args(
        root=root,
        backend=args.backend,
        workspaces_raw=args.workspaces,
        provider=args.provider,
        provider_config_raw=args.provider_config,
        remote=args.remote,
        require_backend=True,
    )
    assert backend_name is not None
    assert backend_config is not None

    with advisory_lock(lock_file_for(graph_path(root))):
        config = load_config(root)
        graph = load_graph(root)
        old_name, old_config = backend_spec_from_config(config)

        # Validate that the target backend is constructible before mutating
        # config.json. This catches missing provider SDKs early.
        load_backend(
            root,
            explicit_name=backend_name,
            explicit_config=backend_config,
        )

        if (backend_name, backend_config) != (old_name, old_config):
            blocking: list[str] = []
            for node_id, node in graph["nodes"].items():
                if node_id == "root":
                    continue
                if node.get("status") not in {"pending", "active", "evaluated", "failed"}:
                    continue
                node_name, node_config = backend_spec_for_node(
                    root,
                    node,
                    workspace_config=config,
                )
                if (node_name, node_config) == (old_name, old_config):
                    blocking.append(node_id)
            if blocking:
                raise RuntimeError(
                    "cannot change workspace default backend while experiments "
                    f"with the old backend are still in flight: {', '.join(blocking)}"
                )

        config["execution_backend"] = backend_name
        if backend_config:
            config["execution_backend_config"] = backend_config
        else:
            config.pop("execution_backend_config", None)
        save_config(root, config)

        if backend_name == "pool":
            from .backends import pool_state

            pool_state.init_state(
                root,
                list(backend_config.get("slots", [])),
                backend_state_key(backend_name, backend_config),
            )

    if backend_name == "pool":
        summary = f"backend set to pool ({len(backend_config.get('slots', []))} slots)"
    elif backend_name == "remote":
        summary = (
            f"backend set to remote (provider={backend_config['provider']})"
        )
    else:
        summary = "backend set to worktree"
    print(summary)
    return 0


def _ensure_runtime_env_config(config: dict[str, Any]) -> dict[str, Any]:
    runtime_env = config.setdefault("runtime_env", {})
    runtime_env.setdefault("inherit_shell", True)
    runtime_env.setdefault("dotenv", [])
    return runtime_env


def cmd_env(args: argparse.Namespace) -> int:
    root = repo_root()
    config, _graph = _require_workspace(root)

    if args.env_action == "show":
        summary = runtime_env_summary(root, config)
        if getattr(args, "json", False):
            print(json.dumps(summary, indent=2))
            return 0
        print(f"inherit_shell: {str(summary['inherit_shell']).lower()}")
        print(f"resolved_keys: {summary['resolved_key_count']} configured/present")
        for source in summary["dotenv"]:
            status = "present" if source["exists"] else "missing"
            if source["mode"] == "allow":
                allow = ",".join(source.get("keys", [])) or "(none)"
                resolved = ",".join(source.get("resolved_keys", [])) or "(none)"
                print(
                    f"dotenv: {source['path']} mode=allow keys={allow} "
                    f"resolved={resolved} ({status})"
                )
            else:
                count = len(source.get("resolved_keys", []))
                print(f"dotenv: {source['path']} mode=all keys={count} ({status})")
        return 0

    with advisory_lock(lock_file_for(config_path(root))):
        config = load_config(root)
        runtime_env = _ensure_runtime_env_config(config)

        if args.env_action == "inherit-shell":
            runtime_env["inherit_shell"] = args.value == "on"
            atomic_write_json(config_path(root), config)
            print(f"inherit_shell set to {str(runtime_env['inherit_shell']).lower()}")
            return 0

        if args.env_action == "load":
            if args.all:
                entry = {"path": args.path, "mode": "all"}
            else:
                keys = [k.strip() for k in args.allow.split(",") if k.strip()]
                if not keys:
                    raise RuntimeError("--allow requires at least one key")
                entry = {"path": args.path, "mode": "allow", "keys": keys}

            sources = list(runtime_env.get("dotenv", []) or [])
            replaced = False
            for idx, source in enumerate(sources):
                if isinstance(source, dict) and source.get("path") == args.path:
                    sources[idx] = entry
                    replaced = True
                    break
            if not replaced:
                sources.append(entry)
            runtime_env["dotenv"] = sources
            atomic_write_json(config_path(root), config)
            action = "updated" if replaced else "added"
            if entry["mode"] == "allow":
                print(f"{action} dotenv {args.path} (allow: {','.join(entry['keys'])})")
            else:
                print(f"{action} dotenv {args.path} (all keys)")
            return 0

        if args.env_action == "clear":
            runtime_env["dotenv"] = []
            atomic_write_json(config_path(root), config)
            print("cleared runtime dotenv sources")
            return 0

    raise RuntimeError(f"unknown env action: {args.env_action}")


# ---------------------------------------------------------------------------
# evo workspace -- pool slot inspection and stale-lease release
# ---------------------------------------------------------------------------


def cmd_workspace_status(args: argparse.Namespace) -> int:
    """Show pool slot occupancy. Errors clearly when pool mode is not active."""
    root = repo_root()
    config = load_config(root)
    if config.get("execution_backend") != "pool":
        print(
            f"ERROR: workspace subcommand only applies in pool mode "
            f"(execution_backend={config.get('execution_backend', 'worktree')!r}).",
            file=sys.stderr,
        )
        return 1

    from .backends import backend_state_key, pool_state

    backend_config = dict(config.get("execution_backend_config", {}) or {})
    state = pool_state.read_state(root, backend_state_key("pool", backend_config))
    commit_strategy = config.get("commit_strategy", "all")
    if args.json:
        print(json.dumps({**state, "commit_strategy": commit_strategy}, indent=2))
        return 0

    print(f"commit_strategy: {commit_strategy}")
    # Human-readable table. Lease is keyed by experiment status, not by the
    # transient `evo new` PID -- a lease persists across the multi-process
    # lifecycle (allocate, dispatch, run) until the experiment commits or is
    # discarded. PID is recorded for diagnostics only.
    rows = []
    rows.append(("SLOT", "PATH", "LEASED BY", "BRANCH"))
    for slot in state["slots"]:
        lease = slot.get("leased_by")
        if lease is None:
            leased_by = "(idle)"
        else:
            leased_by = f"{lease['exp_id']} (pid {lease['pid']})"
        last_branch = slot.get("last_branch") or "(never leased)"
        rows.append((str(slot["id"]), slot["path"], leased_by, f"last: {last_branch}"))
    widths = [max(len(r[c]) for r in rows) for c in range(4)]
    for row in rows:
        print(
            "  ".join(str(cell).ljust(widths[i]) for i, cell in enumerate(row))
        )
    return 0


def cmd_workspace_release(args: argparse.Namespace) -> int:
    """Manually clear a slot's lease. For stale-lease recovery only."""
    root = repo_root()
    config = load_config(root)
    if config.get("execution_backend") != "pool":
        print("ERROR: workspace subcommand only applies in pool mode.", file=sys.stderr)
        return 1
    from .backends import backend_state_key, pool_state

    target = args.slot_id
    backend_config = dict(config.get("execution_backend_config", {}) or {})
    with pool_state.locked_state(root, backend_state_key("pool", backend_config)) as state:
        slot = next((s for s in state["slots"] if s["id"] == target), None)
        if slot is None:
            print(f"ERROR: slot {target} not in pool", file=sys.stderr)
            return 1
        if slot.get("leased_by") is None:
            print(f"slot {target} ({slot['path']}) was already free")
            return 0
        prior = slot["leased_by"]
        slot["leased_by"] = None
    print(
        f"released slot {target} ({slot['path']}); was held by "
        f"{prior['exp_id']} (pid {prior['pid']})"
    )
    return 0


# ---------------------------------------------------------------------------
# evo dispatch — fork-cache child spawning (claude-code only)
# ---------------------------------------------------------------------------


def _forks_dir(root: Path) -> Path:
    """Per-run forks directory, under the active run dir. `evo reset` blows
    away the whole run dir so fork job state doesn't leak across resets."""
    return workspace_path(root) / "forks"


def _job_dir(root: Path, exp_id: str) -> Path:
    return _forks_dir(root) / exp_id


def _job_meta_path(root: Path, exp_id: str) -> Path:
    return _job_dir(root, exp_id) / "meta.json"


def _job_status_path(root: Path, exp_id: str) -> Path:
    return _job_dir(root, exp_id) / "status"


def _job_result_path(root: Path, exp_id: str) -> Path:
    return _job_dir(root, exp_id) / "result.json"


def _write_status(root: Path, exp_id: str, status: str) -> None:
    p = _job_status_path(root, exp_id)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(status + "\n", encoding="utf-8")


def _read_status(root: Path, exp_id: str) -> str:
    p = _job_status_path(root, exp_id)
    if not p.exists():
        return "<missing>"
    return p.read_text(encoding="utf-8").strip()


def _is_pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        # pid exists but isn't ours — treat as alive
        return True
    except OSError:
        # Windows: os.kill(pid, 0) on a dead pid raises generic OSError
        # (errno EINVAL from OpenProcess), not ProcessLookupError.
        return False


def _settle_job(root: Path, exp_id: str) -> str:
    """Read meta+status; if status=running but pid is dead, transition to
    'done' (or 'error' if the experiment node ended up failed). Idempotent.
    Returns the final status string."""
    status = _read_status(root, exp_id)
    if status not in ("running",):
        return status
    meta_p = _job_meta_path(root, exp_id)
    if not meta_p.exists():
        return status
    meta = json.loads(meta_p.read_text(encoding="utf-8"))
    pid = int(meta.get("pid", 0) or 0)
    if pid and _is_pid_alive(pid):
        return "running"
    # Subprocess gone — settle the job.
    new_status = "done"
    # Check the experiment's actual outcome to color the status.
    try:
        result_p = experiment_result_path(root, exp_id)
        if result_p.exists():
            outcome = json.loads(result_p.read_text(encoding="utf-8"))
            if outcome.get("status") in ("failed",):
                new_status = "error"
    except Exception:  # noqa: BLE001
        pass
    _write_status(root, exp_id, new_status)
    # Stamp the child's session_id onto the experiment node so future
    # dispatches of THIS node's children can lineage-fork from it. For
    # foreground dispatches the session_id is already in meta (set when
    # spawn_child returned). For background dispatches we extract it from
    # the captured stdout log here, on the done transition.
    try:
        sid = meta.get("child_session_id")
        if not sid and meta.get("stdout_log"):
            sid = _extract_session_id_from_log(Path(meta["stdout_log"]))
        if sid:
            _stamp_session_id(root, exp_id, sid)
    except Exception:  # noqa: BLE001
        pass
    # Materialize result.json combining meta + outcome (best-effort).
    try:
        result_summary = {
            "exp_id": exp_id,
            "parent_id": meta.get("parent_id"),
            "host": meta.get("host"),
            "started_at": meta.get("started_at"),
            "ended_at": utc_now(),
            "status": new_status,
        }
        if result_p.exists():
            result_summary["outcome"] = json.loads(result_p.read_text(encoding="utf-8"))
        atomic_write_json(_job_result_path(root, exp_id), result_summary)
    except Exception:  # noqa: BLE001
        pass
    return new_status


def _extract_session_id_from_log(log_path: Path) -> str | None:
    """Parse a `claude -p --output-format json` stdout log for session_id.
    Tolerates both single-JSON and JSONL formats. Used when settling a
    background dispatch -- foreground already has session_id in meta."""
    if not log_path.exists():
        return None
    try:
        text = log_path.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    if not text:
        return None
    # Try single JSON value first.
    try:
        parsed = json.loads(text)
        events = parsed if isinstance(parsed, list) else [parsed]
    except json.JSONDecodeError:
        events = []
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    for ev in events:
        if isinstance(ev, dict) and ev.get("type") == "system" and ev.get("subtype") == "init":
            sid = ev.get("session_id")
            if sid:
                return sid
    for ev in events:
        if isinstance(ev, dict) and ev.get("type") == "result":
            sid = ev.get("session_id")
            if sid:
                return sid
    return None


def _stamp_session_id(root: Path, exp_id: str, session_id: str) -> None:
    """Persist child session_id on the experiment node for lineage forking.
    Idempotent: setting the same value is a no-op."""

    def _apply(node: dict, _graph: dict) -> None:
        if node.get("session_id") != session_id:
            node["session_id"] = session_id
            node["session_runtime"] = "claude-code"

    update_node(root, exp_id, _apply)


def cmd_dispatch(args: argparse.Namespace) -> int:
    """Top-level `evo dispatch` dispatcher — routes to the action handler."""
    action = args.dispatch_action
    if action == "run":
        return _cmd_dispatch_run(args)
    if action == "wait":
        return _cmd_dispatch_wait(args)
    if action == "list":
        return _cmd_dispatch_list(args)
    if action == "status":
        return _cmd_dispatch_status(args)
    if action == "kill":
        return _cmd_dispatch_kill(args)
    raise RuntimeError(f"unknown dispatch action: {action}")


def _cmd_dispatch_run(args: argparse.Namespace) -> int:
    from .dispatch import (
        DispatchNotSupportedError,
        ExplorerSpawnError,
        dispatch_child,
    )
    root = repo_root()
    _require_workspace(root)  # surface the standard "not initialized" error
    try:
        out = dispatch_child(
            root,
            parent_id=args.parent,
            brief=args.message,
            budget=args.budget,
            explore_context=args.explore_context,
            refresh_explorer=args.refresh_explorer,
            background=args.background,
            job_dir_factory=lambda exp_id: _job_dir(root, exp_id),
        )
    except DispatchNotSupportedError as exc:
        # User-facing guidance, exit 2 to distinguish from generic failure.
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    except ExplorerSpawnError as exc:
        print(f"ERROR: explorer spawn failed: {exc}", file=sys.stderr)
        return 1

    exp_id = out["exp_id"]
    # Write meta + status atomically before returning so that wait/status
    # commands see consistent state even if the orchestrator races.
    meta = {
        "exp_id": exp_id,
        "parent_id": out["parent_id"],
        "host": "claude-code",
        "explorer_session_id": out["explorer_session_id"],
        "lineage": out.get("lineage", False),
        "child_session_id": out.get("session_id"),
        "worktree": out["worktree"],
        "brief": args.message,
        "budget": args.budget,
        "background": out.get("background", False),
        "started_at": out.get("started_at", utc_now()),
        "pid": out.get("pid"),
        "stdout_log": out.get("stdout_log"),
        "stderr_log": out.get("stderr_log"),
    }
    atomic_write_json(_job_meta_path(root, exp_id), meta)

    if args.background:
        _write_status(root, exp_id, "running")
        print(json.dumps({"job_id": exp_id, "exp_id": exp_id, "pid": out.get("pid"),
                          "explorer_session_id": out["explorer_session_id"]}))
        return 0

    # Foreground: dispatch_child has already returned with exit_code/usage.
    # Write status=running first so _settle_job has something to transition;
    # without it _settle_job sees "<missing>" and short-circuits, leaving
    # `evo dispatch list/status` reporting <missing> for completed foreground
    # jobs.
    _write_status(root, exp_id, "running")
    _settle_job(root, exp_id)
    print(json.dumps({
        "job_id": exp_id,
        "exp_id": exp_id,
        "exit_code": out.get("exit_code"),
        "session_id": out.get("session_id"),
        "explorer_session_id": out["explorer_session_id"],
        "lineage": out.get("lineage", False),
        "usage": out.get("usage", {}),
    }, indent=2))
    return 0 if out.get("exit_code", 1) == 0 else 1


def _cmd_dispatch_wait(args: argparse.Namespace) -> int:
    import time
    root = repo_root()
    _require_workspace(root)

    if args.job_ids:
        targets = list(args.job_ids)
    else:
        # All currently-running jobs in this workspace.
        targets = []
        if _forks_dir(root).exists():
            for child in _forks_dir(root).iterdir():
                if child.is_dir() and _read_status(root, child.name) == "running":
                    targets.append(child.name)

    if not targets:
        print("no running jobs")
        return 0

    pending = set(targets)
    poll_interval = float(os.environ.get("EVO_DISPATCH_WAIT_INTERVAL", "0.25"))
    rc = 0
    while pending:
        for exp_id in list(pending):
            status = _settle_job(root, exp_id)
            if status not in ("running",):
                pending.discard(exp_id)
                row = {"exp_id": exp_id, "status": status}
                # Pull a score / outcome line if available
                try:
                    rp = experiment_result_path(root, exp_id)
                    if rp.exists():
                        out = json.loads(rp.read_text(encoding="utf-8"))
                        row["outcome_status"] = out.get("status")
                        row["score"] = out.get("score")
                except Exception:  # noqa: BLE001
                    pass
                if not args.quiet:
                    print(json.dumps(row))
                if status == "error":
                    rc = 1
        if pending:
            time.sleep(poll_interval)
    return rc


def _cmd_dispatch_list(args: argparse.Namespace) -> int:
    root = repo_root()
    _require_workspace(root)
    rows: list[dict] = []
    if _forks_dir(root).exists():
        for child in sorted(_forks_dir(root).iterdir()):
            if not child.is_dir():
                continue
            exp_id = child.name
            status = _settle_job(root, exp_id)  # opportunistically advances stale entries
            meta_p = _job_meta_path(root, exp_id)
            if not meta_p.exists():
                continue
            meta = json.loads(meta_p.read_text(encoding="utf-8"))
            if args.running and status != "running":
                continue
            rows.append({
                "exp_id": exp_id,
                "status": status,
                "parent_id": meta.get("parent_id"),
                "started_at": meta.get("started_at"),
                "brief": (meta.get("brief") or "")[:60],
                "pid": meta.get("pid"),
            })
    if args.recent:
        rows = rows[-args.recent:]
    print(json.dumps(rows, indent=2))
    return 0


def _cmd_dispatch_status(args: argparse.Namespace) -> int:
    root = repo_root()
    _require_workspace(root)
    exp_id = args.job_id
    if not _job_dir(root, exp_id).exists():
        print(f"ERROR: no fork job for {exp_id}", file=sys.stderr)
        return 1
    status = _settle_job(root, exp_id)
    meta_p = _job_meta_path(root, exp_id)
    meta = json.loads(meta_p.read_text(encoding="utf-8")) if meta_p.exists() else {}
    out = {
        "exp_id": exp_id,
        "status": status,
        "meta": meta,
    }
    rp = _job_result_path(root, exp_id)
    if rp.exists():
        out["result"] = json.loads(rp.read_text(encoding="utf-8"))
    print(json.dumps(out, indent=2))
    return 0


def _cmd_dispatch_kill(args: argparse.Namespace) -> int:
    import signal
    root = repo_root()
    _require_workspace(root)
    exp_id = args.job_id
    meta_p = _job_meta_path(root, exp_id)
    if not meta_p.exists():
        print(f"ERROR: no fork job for {exp_id}", file=sys.stderr)
        return 1
    meta = json.loads(meta_p.read_text(encoding="utf-8"))
    pid = int(meta.get("pid") or 0)
    if not pid or not _is_pid_alive(pid):
        print(f"job {exp_id} not running")
        return 0
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    _write_status(root, exp_id, "killed")
    print(f"sent SIGTERM to pid {pid}; status=killed")
    return 0


def cmd_new(args: argparse.Namespace) -> int:
    root = repo_root()
    config, graph = _require_workspace(root)
    if args.parent not in graph["nodes"]:
        raise RuntimeError(f"unknown parent: {args.parent}")
    backend_name, backend_config = _resolve_backend_cli_args(
        root=root,
        backend=args.backend,
        workspaces_raw=args.workspaces,
        provider=args.provider,
        provider_config_raw=args.provider_config,
        remote=args.remote,
        require_backend=False,
    )
    backend_override = None
    if backend_name is not None:
        backend_override = {"name": backend_name, "config": backend_config or {}}
    node = allocate_experiment(
        root,
        parent_id=args.parent,
        hypothesis=args.message,
        backend_override=backend_override,
    )
    target = node_target_path(root, config, node)
    print(json.dumps({"id": node["id"], "worktree": node["worktree"], "target": str(target)}, indent=2))
    return 0


def _fetch_remote_artifacts(
    executor: Any,
    sandbox_result_path: str,
    sandbox_traces_dir: str,
    local_result_path: Path,
    local_traces_dir: Path,
) -> None:
    """After a remote benchmark/gate run, copy result.json + traces back
    to the orchestrator's attempts/NNN/ directory so downstream readers
    (`evo traces`, dashboard, score-salvage) see the same artifact shape
    as a local run."""
    # result.json
    if executor.file_exists(sandbox_result_path):
        try:
            blob = executor.read_bytes(sandbox_result_path)
            local_result_path.write_bytes(blob)
        except Exception:
            pass  # best-effort
    # traces dir
    local_traces_dir.mkdir(parents=True, exist_ok=True)
    for attempt in range(5):
        try:
            executor.fetch_dir(sandbox_traces_dir, local_traces_dir)
        except Exception:
            pass  # best-effort; salvage may still find what was already pulled
        if any(local_traces_dir.glob("*.json")):
            break
        if attempt < 4:
            time.sleep(0.25)


def _run_command(command: str, cwd: Path, env: dict[str, str], stdout_path: Path, stderr_path: Path, timeout: int | None = None) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        command,
        cwd=cwd,
        shell=True,
        env=env,
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    if stdout_path == stderr_path:
        combined = (result.stdout or "")
        if result.stderr:
            if combined and not combined.endswith("\n"):
                combined += "\n"
            combined += result.stderr
        stdout_path.write_text(combined, encoding="utf-8")
    else:
        stdout_path.write_text(result.stdout or "", encoding="utf-8")
        stderr_path.write_text(result.stderr or "", encoding="utf-8")
    return result


def _finalize_result(root: Path, exp_id: str, node: dict, score: float | None, status: str, extra: dict | None = None) -> None:
    payload = {
        "experiment_id": exp_id,
        "score": score,
        "status": status,
        "timestamp": utc_now(),
        "eval_epoch": node.get("eval_epoch"),
    }
    if extra:
        payload.update(extra)
    atomic_write_json(experiment_result_path(root, exp_id), payload)


def _remote_stream_journals(root: Path, exp_id: str, attempt: int) -> list[dict]:
    """Return remote stream journals written beside attempt log files.

    These sidecars are diagnostic/recovery metadata. A corrupt sidecar should
    not prevent result finalization, so unreadable files are ignored.
    """
    base = attempt_dir(root, exp_id, attempt)
    journals: list[dict] = []
    for path in sorted(base.glob("*.remote.json")):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(payload, dict):
            continue
        payload = dict(payload)
        try:
            payload["journal_path"] = str(path.relative_to(root))
        except ValueError:
            payload["journal_path"] = str(path)
        journals.append(payload)
    return journals


def _remote_stream_journal_for_log(log_path: Path) -> dict | None:
    path = log_path.with_name(log_path.name + ".remote.json")
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def _remote_infra_error_for_log(log_path: Path) -> str | None:
    journal = _remote_stream_journal_for_log(log_path)
    if not journal or journal.get("state") != "failed_infra":
        return None
    return str(journal.get("error") or "remote status unavailable")


def _resumable_remote_benchmark_journal(
    root: Path,
    exp_id: str,
    attempt: int,
) -> dict | None:
    journal = _remote_stream_journal_for_log(
        attempt_dir(root, exp_id, attempt) / "benchmark.log"
    )
    if not journal or journal.get("state") not in {"running", "exited"}:
        return None
    if not journal.get("process_id") or not isinstance(journal.get("command"), list):
        return None
    return journal


def _attempt_state_path(root: Path, exp_id: str, attempt: int) -> Path:
    return attempt_dir(root, exp_id, attempt) / "attempt_state.json"


def _read_attempt_state(root: Path, exp_id: str, attempt: int) -> dict | None:
    path = _attempt_state_path(root, exp_id, attempt)
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def _write_attempt_state(
    root: Path,
    exp_id: str,
    attempt: int,
    *,
    phase: str,
    status: str,
    started_at: str,
    extra: dict | None = None,
) -> None:
    path = _attempt_state_path(root, exp_id, attempt)
    prior = _read_attempt_state(root, exp_id, attempt) or {}
    payload = {
        **prior,
        "experiment_id": exp_id,
        "attempt": attempt,
        "phase": phase,
        "status": status,
        "started_at": prior.get("started_at") or started_at,
        "updated_at": utc_now(),
    }
    if extra:
        payload.update(extra)
    atomic_write_json(path, payload)


_ATTEMPT_PHASE_ORDER = {
    "initializing": 0,
    "runtime_prepare": 1,
    "runtime_before_run": 2,
    "benchmark": 3,
    "artifacts": 4,
    "gates": 5,
    "commit": 6,
    "finalize": 7,
    "complete": 8,
    "failed": 99,
}


def _attempt_phase_index(phase: str | None) -> int:
    return _ATTEMPT_PHASE_ORDER.get(phase or "", -1)


def _write_attempt_outcome(
    root: Path,
    exp_id: str,
    attempt: int,
    outcome: str,
    *,
    node: dict,
    started_at: str,
    score: float | None = None,
    benchmark: dict | None = None,
    gates: list[dict] | None = None,
    runtime: list[dict] | None = None,
    error: str | None = None,
    commit: str | None = None,
    parent_score: float | None = None,
    metric: str | None = None,
) -> None:
    finished = utc_now()
    payload = {
        "experiment_id": exp_id,
        "attempt": attempt,
        "outcome": outcome,
        "hypothesis": node.get("hypothesis"),
        "parent_id": node.get("parent"),
        "parent_score": parent_score,
        "metric": metric,
        "score": score,
        "started_at": started_at,
        "finished_at": finished,
        "benchmark": benchmark,
        "runtime": runtime or [],
        "gates": gates or [],
        "error": error,
        "commit": commit,
    }
    remote_streams = _remote_stream_journals(root, exp_id, attempt)
    if remote_streams:
        payload["remote_streams"] = remote_streams
    attempt_state = _read_attempt_state(root, exp_id, attempt)
    if attempt_state:
        payload["attempt_state"] = attempt_state
    parsed_diff = parse_diff_patch(root, exp_id, attempt)
    if parsed_diff:
        payload["change_files"] = parsed_diff["files"]
    atomic_write_json(attempt_outcome_path(root, exp_id, attempt), payload)


def _resolve_workspace_exp_id(args: argparse.Namespace) -> str:
    """Return --exp-id, or error.

    Strict by design: no env-var fallback, no implicit "active" experiment.
    Multiple subagents run concurrent experiments; a silent default
    would let one subagent operate on another's container by accident.
    The orchestrator tells each subagent its exp_id via brief prose;
    the subagent passes --exp-id on every call.
    """
    explicit = getattr(args, "exp_id", None)
    if explicit:
        return explicit
    raise RuntimeError(
        "evo workspace op: --exp-id is required. Every workspace-touching "
        "command must name its experiment explicitly. The orchestrator "
        "tells each subagent which experiment is theirs; pass that as "
        "--exp-id <id> on every evo bash/read/write/edit/glob/grep call."
    )


def _open_workspace_executor(args: argparse.Namespace):
    """Resolve exp_id, load backend, return (root, node, executor_ctxmgr).

    Caller uses `with executor_ctxmgr as executor:`. Hits the same code
    path that cmd_run uses, so behavior is identical -- local subprocess
    in worktree/pool mode, sandbox-agent HTTP in remote mode.
    """
    from .workspace_executor import workspace_executor_for
    from .backends import load_backend

    root = repo_root()
    config, _graph = _require_workspace(root)
    exp_id = _resolve_workspace_exp_id(args)
    node = _read_node(root, exp_id)
    backend = load_backend(root, node=node, workspace_config=config)
    return root, node, workspace_executor_for(backend, root, node)


def cmd_ws_bash(args: argparse.Namespace) -> int:
    """Run a shell command in the experiment's workspace (local or remote)."""
    _root, node, executor_ctxmgr = _open_workspace_executor(args)
    cmd = ["sh", "-c", args.command]
    cwd = args.cwd or node["worktree"]
    timeout = args.timeout if args.timeout and args.timeout > 0 else None
    with executor_ctxmgr as executor:
        result = executor.run(cmd, cwd=cwd, env=None, timeout=timeout)
    if result.stdout:
        sys.stdout.write(result.stdout)
        if not result.stdout.endswith("\n"):
            sys.stdout.write("\n")
    if result.stderr:
        sys.stderr.write(result.stderr)
        if not result.stderr.endswith("\n"):
            sys.stderr.write("\n")
    return result.exit_code if result.exit_code is not None else 124


def cmd_ws_read(args: argparse.Namespace) -> int:
    """Read a file from the experiment's workspace; write content to stdout."""
    _root, _node, executor_ctxmgr = _open_workspace_executor(args)
    with executor_ctxmgr as executor:
        if not executor.file_exists(args.path):
            print(f"ERROR: not found: {args.path}", file=sys.stderr)
            return 1
        sys.stdout.buffer.write(executor.read_bytes(args.path))
    return 0


def cmd_ws_write(args: argparse.Namespace) -> int:
    """Write a file in the experiment's workspace; content from stdin or
    --content flag (the latter for shell-friendly single-line writes)."""
    _root, _node, executor_ctxmgr = _open_workspace_executor(args)
    if args.content is not None:
        content = args.content
    else:
        content = sys.stdin.read()
    with executor_ctxmgr as executor:
        executor.write_text(args.path, content)
    return 0


def cmd_ws_edit(args: argparse.Namespace) -> int:
    """Search-replace in a file inside the experiment's workspace.

    Supports two input modes:
      - --old / --new flags (simple, single-line)
      - --json-stdin: read {"old": ..., "new": ..., "replace_all": bool}
        from stdin (multi-line content with proper escaping)
    """
    if args.json_stdin:
        spec = json.loads(sys.stdin.read())
        old = spec["old"]
        new = spec["new"]
        replace_all = bool(spec.get("replace_all", False))
    else:
        if args.old is None or args.new is None:
            raise RuntimeError(
                "evo edit: pass --old/--new flags, or --json-stdin with "
                "{'old':..., 'new':..., 'replace_all':...} on stdin"
            )
        old = args.old
        new = args.new
        replace_all = bool(args.replace_all)

    _root, _node, executor_ctxmgr = _open_workspace_executor(args)
    with executor_ctxmgr as executor:
        try:
            content = executor.read_text(args.path)
        except Exception as exc:
            print(f"ERROR: could not read {args.path}: {exc}", file=sys.stderr)
            return 1
        if old not in content:
            print(
                f"ERROR: --old string not found in {args.path}; no edit applied",
                file=sys.stderr,
            )
            return 1
        if not replace_all and content.count(old) > 1:
            print(
                f"ERROR: --old string is not unique in {args.path} "
                f"(found {content.count(old)} occurrences). "
                f"Pass --replace-all to substitute all, or include more "
                f"surrounding context to make the match unique.",
                file=sys.stderr,
            )
            return 1
        new_content = (
            content.replace(old, new) if replace_all
            else content.replace(old, new, 1)
        )
        executor.write_text(args.path, new_content)
    return 0


def cmd_ws_glob(args: argparse.Namespace) -> int:
    """List filenames matching a pattern in the experiment's workspace.

    Implementation is intentionally simple: shells out to `find ... -name
    <pattern>` so semantics match agents' expectations. Pattern is a
    standard glob (`*.py`, `**/*.md`, etc.).
    """
    _root, node, executor_ctxmgr = _open_workspace_executor(args)
    base = args.path or node["worktree"]
    with executor_ctxmgr as executor:
        # `find -name <pat>` only matches the basename; for **/-style
        # patterns we use `-path` instead.
        flag = "-path" if "/" in args.pattern else "-name"
        result = executor.run(
            ["find", str(base), flag, args.pattern],
            cwd=str(base),
        )
    if result.stdout:
        sys.stdout.write(result.stdout)
    if (result.exit_code or 0) != 0 and result.stderr:
        sys.stderr.write(result.stderr)
    return result.exit_code if result.exit_code is not None else 1


def cmd_ws_grep(args: argparse.Namespace) -> int:
    """Search file content with ripgrep in the experiment's workspace.

    Falls back to `grep -r` if `rg` isn't on PATH inside the workspace.
    """
    _root, node, executor_ctxmgr = _open_workspace_executor(args)
    base = args.path or node["worktree"]
    with executor_ctxmgr as executor:
        which = executor.run(["which", "rg"], cwd=str(base))
        if (which.exit_code or 1) == 0 and which.stdout.strip():
            cmd = ["rg", "--no-heading", "--line-number", args.pattern, str(base)]
        else:
            cmd = ["grep", "-rn", args.pattern, str(base)]
        result = executor.run(cmd, cwd=str(base))
    if result.stdout:
        sys.stdout.write(result.stdout)
    if (result.exit_code or 0) not in (0, 1) and result.stderr:
        # rg/grep exit 1 when no matches; that's normal, not an error.
        sys.stderr.write(result.stderr)
    return result.exit_code if result.exit_code is not None else 2


def _block_if_epoch_requires_baseline(root: Path, parent_id: str, no_compare: bool) -> None:
    if no_compare:
        return
    config = load_config(root)
    if config.get("comparison_blocked") and parent_id != "root":
        raise RuntimeError("comparison is blocked for the current eval epoch until a new root baseline is committed")


def cmd_run(args: argparse.Namespace) -> int:
    root = repo_root()
    config, graph = _require_workspace(root)
    node = _read_node(root, args.exp_id)
    if getattr(args, "check", False):
        if not node.get("worktree"):
            print(f"ERROR: {args.exp_id} has no worktree to check", file=sys.stderr)
            return 1
        from .workspace_executor import workspace_executor_for
        from .backends import load_backend
        backend = load_backend(root, node=node, workspace_config=config)
        with workspace_executor_for(backend, root, node) as executor:
            return _cmd_run_check(args, root, config, graph, node, executor)

    if node.get("status") not in (None, "pending", "active", "evaluated", "failed"):
        print(f"ERROR: {args.exp_id} has status '{node['status']}' -- cannot run again", file=sys.stderr)
        return 1
    _block_if_epoch_requires_baseline(root, node["parent"], no_compare=False)

    max_attempts = int(config.get("max_attempts", 3))
    evaluated_attempts = int(node.get("evaluated_attempts", 0))
    if evaluated_attempts >= max_attempts:
        print(
            f"ERROR: {args.exp_id} exhausted {evaluated_attempts}/{max_attempts} attempts. "
            f"Discard with `evo discard {args.exp_id} --reason \"...\"` or branch elsewhere.",
            file=sys.stderr,
        )
        return 1

    # Open the workspace executor for the lifetime of this run. Worktree
    # and pool backends resolve to a LocalExecutor (subprocess + Path);
    # the remote backend resolves to a RemoteExecutor that routes shell
    # and fs ops through sandbox-agent over HTTP.
    from .workspace_executor import workspace_executor_for
    from .backends import load_backend
    backend = load_backend(root, node=node, workspace_config=config)
    try:
        with workspace_executor_for(backend, root, node) as executor:
            return _cmd_run_impl(
                args, root, config, graph, node, backend, executor,
                max_attempts=max_attempts, evaluated_attempts=evaluated_attempts,
            )
    except Exception as exc:  # noqa: BLE001
        if getattr(backend, "name", None) == "remote" and node.get("status") == "active":
            attempt_n = int(node.get("current_attempt") or 0)
            started_at = str(
                (_read_attempt_state(root, args.exp_id, attempt_n) or {}).get("started_at")
                or utc_now()
            )
            error_msg = f"remote_infra_failure:{exc}"

            def _mark_failed(current_node: dict, _graph: dict) -> None:
                current_node["status"] = "failed"
                current_node["error"] = error_msg

            update_node(root, args.exp_id, _mark_failed)
            if attempt_n > 0:
                _write_attempt_state(
                    root, args.exp_id, attempt_n,
                    phase="failed", status="failed", started_at=started_at,
                    extra={"error": error_msg},
                )
                _write_attempt_outcome(
                    root, args.exp_id, attempt_n, "failed",
                    node=node, started_at=started_at, error=error_msg,
                    parent_score=_resolve_parent_score(graph, node["parent"]),
                    metric=config.get("metric"),
                )
            try:
                from .backends import DiscardCtx as _DCtx
                failed_node = dict(node)
                failed_node["status"] = "failed"
                backend.release_lease(_DCtx(root=root, node=failed_node))
            except Exception:
                pass
            print(f"FAILED {args.exp_id} {error_msg}")
            return 1
        raise


def _next_check_dir(root: Path, exp_id: str) -> tuple[int, Path]:
    checks_root = experiments_dir_for(root, exp_id) / "checks"
    checks_root.mkdir(parents=True, exist_ok=True)
    existing = [
        int(path.name)
        for path in checks_root.iterdir()
        if path.is_dir() and path.name.isdigit()
    ]
    check_n = (max(existing) + 1) if existing else 1
    check_dir = checks_root / f"{check_n:03d}"
    check_dir.mkdir(parents=True, exist_ok=False)
    return check_n, check_dir


def _runtime_env_for_attempt(
    root: Path,
    config: dict,
    *,
    exp_id: str,
    attempt_label: str,
    worktree: Path,
    env_traces_dir: str,
    env_result_path: str,
    env_checkpoint_dir: str,
) -> dict[str, str]:
    env = resolve_runtime_env(root, config)
    env["EVO_TRACES_DIR"] = env_traces_dir
    env["EVO_WORKTREE"] = str(worktree)
    env["EVO_EXPERIMENT_ID"] = exp_id
    env["EVO_ATTEMPT"] = attempt_label
    env["EVO_RESULT_PATH"] = env_result_path
    env["EVO_CHECKPOINT_DIR"] = env_checkpoint_dir
    return env


def _apply_runtime_prefix(config: dict, command: str) -> str:
    prefix = ((config.get("runtime") or {}).get("prefix") or "").strip()
    if not prefix:
        return command
    return f"{prefix} {command}"


def _runtime_hook_command(config: dict, name: str) -> str | None:
    runtime = config.get("runtime") or {}
    command = runtime.get(name)
    if not isinstance(command, str):
        return None
    command = command.strip()
    return command or None


def _run_runtime_hook(
    executor: Any,
    config: dict,
    name: str,
    *,
    root: Path,
    worktree: Path,
    target: Path,
    env: dict[str, str],
    timeout: float | None,
    log_path: Path,
) -> dict | None:
    command = _runtime_hook_command(config, name)
    if command is None:
        return None
    resolved = fill_command_template(command, target=target, worktree=worktree)
    hook_env = {k: v for k, v in env.items() if not k.startswith("EVO_")}
    result = executor.stream(
        ["sh", "-c", resolved],
        cwd=worktree,
        env=hook_env,
        timeout=timeout,
        stdout_path=log_path,
        stderr_path=log_path,
    )
    record = {
        "name": name,
        "command": resolved,
        "returncode": result.exit_code,
        "timed_out": result.timed_out,
        "log": str(log_path.relative_to(root)) if log_path.is_relative_to(root) else str(log_path),
    }
    if result.timed_out:
        raise RuntimeError(f"runtime_{name}_timeout")
    if (result.exit_code or 0) != 0:
        raise RuntimeError(f"runtime_{name}_exit_{result.exit_code}")
    return record


def _inherited_gate_specs(config: dict, graph: dict, parent_id: str) -> tuple[list[dict], dict[str, str]]:
    inherited_gates = collect_gates_from_path(graph, parent_id)
    if config.get("gate"):
        inherited_gates.insert(0, {"name": "_init_gate", "command": config["gate"]})

    gate_origins: dict[str, str] = {}
    for chain_node in path_to_node(graph, parent_id):
        for g in chain_node.get("gates", []):
            gate_origins.setdefault(g["name"], chain_node["id"])
    if config.get("gate"):
        gate_origins.setdefault("_init_gate", "config")
    return inherited_gates, gate_origins


def _cmd_run_check(
    args: argparse.Namespace,
    root: Path,
    config: dict,
    graph: dict,
    node: dict,
    executor: Any,
) -> int:
    check_n, check_dir = _next_check_dir(root, args.exp_id)
    started_at = utc_now()
    worktree = Path(node["worktree"])
    target = node_target_path(root, config, node)
    traces_dir = check_dir / "traces"
    traces_dir.mkdir(parents=True, exist_ok=True)
    checkpoint_dir = check_dir / "checkpoints"
    checkpoint_dir.mkdir(parents=True, exist_ok=True)
    benchmark_log = check_dir / "benchmark.log"
    benchmark_err = check_dir / "benchmark_err.log"
    result_path = check_dir / "result.json"
    remote = executor.is_remote

    if remote:
        sandbox_traces_dir = f"{worktree}/.evo/check_traces"
        sandbox_result_path = f"{worktree}/.evo/check_result.json"
        sandbox_checkpoint_dir = f"{worktree}/.evo/check_checkpoints"
        run_cwd: Path | str = worktree
        env_traces_dir = sandbox_traces_dir
        env_result_path = sandbox_result_path
        env_checkpoint_dir = sandbox_checkpoint_dir
        executor.run(
            ["rm", "-rf", sandbox_traces_dir, sandbox_result_path, sandbox_checkpoint_dir],
            cwd=worktree,
        )
        executor.run(["mkdir", "-p", sandbox_traces_dir, sandbox_checkpoint_dir], cwd=worktree)
    else:
        sandbox_traces_dir = ""
        sandbox_result_path = ""
        sandbox_checkpoint_dir = ""
        run_cwd = root
        env_traces_dir = str(traces_dir.resolve())
        env_result_path = str(result_path.resolve())
        env_checkpoint_dir = str((check_dir / "checkpoints").resolve())

    benchmark_cmd = _apply_runtime_prefix(
        config,
        fill_command_template(config["benchmark"], target=target, worktree=worktree),
    )
    env = _runtime_env_for_attempt(
        root,
        config,
        exp_id=args.exp_id,
        attempt_label=f"check-{check_n:03d}",
        worktree=worktree,
        env_traces_dir=env_traces_dir,
        env_result_path=env_result_path,
        env_checkpoint_dir=env_checkpoint_dir,
    )
    gate_records: list[dict] = []
    runtime_records: list[dict] = []
    benchmark_record: dict | None = None
    status = "failed"
    score: float | None = None
    error: str | None = None
    try:
        for hook_name, log_name in (
            ("prepare", "runtime_prepare.log"),
            ("before_run", "runtime_before_run.log"),
        ):
            record = _run_runtime_hook(
                executor,
                config,
                hook_name,
                root=root,
                worktree=worktree,
                target=target,
                env=env,
                timeout=args.timeout,
                log_path=check_dir / log_name,
            )
            if record is not None:
                runtime_records.append(record)

        bench = executor.stream(
            ["sh", "-c", benchmark_cmd],
            cwd=run_cwd, env=env, timeout=args.timeout,
            stdout_path=benchmark_log, stderr_path=benchmark_err,
            mirror_remote_dir=sandbox_traces_dir if remote else None,
            mirror_local_dir=traces_dir if remote else None,
            mirror_dirs=[(sandbox_checkpoint_dir, checkpoint_dir)] if remote else None,
        )
        if bench.timed_out:
            raise RuntimeError("benchmark_timeout")
        if (bench.exit_code or 0) != 0:
            benchmark_record = {"command": benchmark_cmd, "returncode": bench.exit_code, "result": None}
            if remote:
                _fetch_remote_artifacts(executor, sandbox_result_path, sandbox_traces_dir, result_path, traces_dir)
                infra_error = _remote_infra_error_for_log(benchmark_log)
                if infra_error is not None:
                    raise RuntimeError(f"remote_infra_failure:{infra_error}")
            raise RuntimeError(f"benchmark_exit_{bench.exit_code}")
        if remote:
            _fetch_remote_artifacts(executor, sandbox_result_path, sandbox_traces_dir, result_path, traces_dir)
        if not result_path.exists():
            raise RuntimeError("missing_result_json")
        score, parsed = load_result(result_path, bench.stdout)
        benchmark_record = {"command": benchmark_cmd, "returncode": 0, "result": parsed}

        inherited_gates, gate_origins = _inherited_gate_specs(config, graph, node["parent"])
        gate_env = {k: v for k, v in env.items() if not k.startswith("EVO_")}
        gate_failures: list[str] = []
        for g in inherited_gates:
            gate_cmd = _apply_runtime_prefix(
                config,
                fill_command_template(g["command"], target=target, worktree=worktree),
            )
            gate_log_file = check_dir / f"gate_{g['name']}.log"
            gate_result = executor.stream(
                ["sh", "-c", gate_cmd],
                cwd=run_cwd, env=gate_env, timeout=args.timeout,
                stdout_path=gate_log_file, stderr_path=gate_log_file,
            )
            passed = not gate_result.timed_out and (gate_result.exit_code or 0) == 0
            record = {
                "name": g["name"],
                "from": gate_origins.get(g["name"], "config"),
                "command": gate_cmd,
                "passed": passed,
                "returncode": gate_result.exit_code,
            }
            if gate_result.timed_out:
                record["error"] = "gate_timeout"
            gate_records.append(record)
            if not passed:
                gate_failures.append(g["name"])
        if gate_failures:
            raise RuntimeError(f"gate_failed:{','.join(gate_failures)}")
        status = "passed"
        print(f"CHECK_PASSED {args.exp_id} score={score} artifacts={check_dir}")
        return 0
    except Exception as exc:  # noqa: BLE001
        error = str(exc)
        print(f"CHECK_FAILED {args.exp_id} {error} artifacts={check_dir}")
        return 1
    finally:
        payload = {
            "experiment_id": args.exp_id,
            "check": check_n,
            "status": status,
            "score": score,
            "started_at": started_at,
            "finished_at": utc_now(),
            "benchmark": benchmark_record,
            "gates": gate_records,
            "runtime": runtime_records,
            "error": error,
        }
        atomic_write_json(check_dir / "check.json", payload)


def _cmd_run_impl(
    args: argparse.Namespace,
    root: Path,
    config: dict,
    graph: dict,
    node: dict,
    backend: Any,
    executor: Any,
    *,
    max_attempts: int,
    evaluated_attempts: int,
) -> int:
    # Shisa-kanko ack for tracked-only mode: when the worktree has any
    # untracked, non-gitignored files, the agent must affirm with
    # --i-staged-new-files that they have either staged any new source files
    # or intentionally left them out. The check runs before _mark_active so
    # an inadmissible run does not mutate node state.
    commit_strategy = config.get("commit_strategy", "all")
    if commit_strategy == "tracked-only":
        worktree = Path(node["worktree"])
        untracked_result = executor.run(
            ["git", "ls-files", "--others", "--exclude-standard"],
            cwd=worktree,
        )
        if untracked_result.exit_code != 0:
            print(
                f"ERROR: pre-flight `git ls-files` failed in {worktree}: "
                f"{untracked_result.stderr[:500]}",
                file=sys.stderr,
            )
            return 1
        untracked = [line for line in untracked_result.stdout.splitlines() if line.strip()]
        ack = getattr(args, "i_staged_new_files", None)
        if untracked and ack != "yes":
            print(
                f"ERROR: {args.exp_id} cannot run: commit_strategy=tracked-only and "
                f"{len(untracked)} untracked file(s) in worktree {worktree}:",
                file=sys.stderr,
            )
            for path in untracked:
                print(f"  {path}", file=sys.stderr)
            print(
                "\nFor each file: if it's a new source file, `git add` it. "
                "If it's warm state (build artifacts, deps, weights), leave "
                "it untracked -- it will persist in the slot but stay out of "
                "the experiment commit. Then re-run with "
                "`--i-staged-new-files yes`.",
                file=sys.stderr,
            )
            if ack is not None and ack != "yes":
                print(
                    f"\n(--i-staged-new-files received value {ack!r}; the only "
                    f"accepted value is 'yes'. This is intentional -- it forces "
                    f"a second deliberate affirmation that the staging step ran.)",
                    file=sys.stderr,
                )
            return 1

    resume_journal: dict | None = None
    resume_state: dict | None = None
    resume_existing_attempt = False
    resume_attempt = int(node.get("current_attempt") or 0)
    if executor.is_remote and node.get("status") == "active" and resume_attempt > 0:
        resume_state = _read_attempt_state(root, args.exp_id, resume_attempt)
        resume_journal = _resumable_remote_benchmark_journal(
            root, args.exp_id, resume_attempt
        )
        if resume_state is None and resume_journal is None:
            print(
                "ERROR: active remote attempt has no recovery metadata; "
                "use `evo discard ...` or branch a fresh experiment.",
                file=sys.stderr,
            )
            return 1
        resume_existing_attempt = True

    # Bumped even on failed runs so NNN subdirs never collide. A resumable
    # active remote attempt keeps its original attempt number.
    attempt_n = resume_attempt if resume_existing_attempt else int(node.get("current_attempt", 0)) + 1
    started_at = str(
        (resume_state or {}).get("started_at")
        or (resume_journal or {}).get("started_at")
        or utc_now()
    )

    if not resume_existing_attempt:
        def _mark_active(current_node: dict, _graph: dict) -> None:
            current_node["status"] = "active"
            current_node["current_attempt"] = attempt_n

        update_node(root, args.exp_id, _mark_active)

    worktree = Path(node["worktree"])
    target = node_target_path(root, config, node)
    exp_dir = experiments_dir_for(root, args.exp_id)
    a_dir = attempt_dir(root, args.exp_id, attempt_n)
    a_dir.mkdir(parents=True, exist_ok=True)
    traces_dir = attempt_traces_dir(root, args.exp_id, attempt_n)
    traces_dir.mkdir(parents=True, exist_ok=True)
    checkpoint_dir = a_dir / "checkpoints"
    checkpoint_dir.mkdir(parents=True, exist_ok=True)
    benchmark_log = a_dir / "benchmark.log"
    benchmark_err = a_dir / "benchmark_err.log"
    result_path = a_dir / "result.json"
    metric = config["metric"]
    parent_score = _resolve_parent_score(graph, node["parent"])

    # In remote mode the workspace lives inside the sandbox; the benchmark
    # writes to sandbox-local paths and we fetch artifacts back to local
    # disk afterwards. In local mode these are all the same paths.
    remote = executor.is_remote
    if remote:
        # Sandbox-internal paths anchored under the backend-provided
        # workspace root. Modal uses /workspace/repo; manual and SSH may
        # resolve elsewhere.
        sandbox_traces_dir = f"{worktree}/.evo/traces"
        sandbox_result_path = f"{worktree}/.evo/result.json"
        sandbox_checkpoint_dir = f"{worktree}/.evo/checkpoints/{args.exp_id}/{attempt_n:03d}"
        run_cwd: Path | str = worktree
        env_traces_dir = sandbox_traces_dir
        env_result_path = sandbox_result_path
        env_checkpoint_dir = sandbox_checkpoint_dir
        # Pre-create the traces dir inside the sandbox so the benchmark
        # can write into it without checking exists().
        executor.run(
            ["mkdir", "-p", sandbox_traces_dir, sandbox_checkpoint_dir],
            cwd=worktree,
        )
    else:
        sandbox_checkpoint_dir = ""
        run_cwd = root
        env_traces_dir = str(traces_dir.resolve())
        env_result_path = str(result_path.resolve())
        env_checkpoint_dir = str(checkpoint_dir.resolve())

    benchmark_cmd = _apply_runtime_prefix(
        config,
        fill_command_template(config["benchmark"], target=target, worktree=worktree),
    )
    # Build env from the configured runtime sources. Values are resolved fresh
    # for every attempt and then injected into local or remote processes.
    env = _runtime_env_for_attempt(
        root,
        config,
        exp_id=args.exp_id,
        attempt_label=str(attempt_n),
        worktree=worktree,
        env_traces_dir=env_traces_dir,
        env_result_path=env_result_path,
        env_checkpoint_dir=env_checkpoint_dir,
    )
    if resume_existing_attempt:
        _write_attempt_state(
            root, args.exp_id, attempt_n,
            phase=str((resume_state or {}).get("phase") or "unknown"),
            status="resuming",
            started_at=started_at,
            extra={
                "resumed_at": utc_now(),
                "checkpoint_dir": str(checkpoint_dir),
                "remote_checkpoint_dir": sandbox_checkpoint_dir if remote else None,
            },
        )
    else:
        _write_attempt_state(
            root, args.exp_id, attempt_n,
            phase="initializing",
            status="running",
            started_at=started_at,
            extra={
                "checkpoint_dir": str(checkpoint_dir),
                "remote_checkpoint_dir": sandbox_checkpoint_dir if remote else None,
            },
        )

    # Captured before the benchmark runs so it survives crashes too.
    # Use parent commit hash rather than branch ref: branch names like
    # "main" exist locally but not necessarily in pool slots or remote
    # sandbox checkouts, while commit hashes are stable. For root
    # experiments, node["commit"] holds the parent commit hash recorded
    # at allocate time (overwritten only when the experiment itself
    # commits, but at that point we're past this codepath).
    if node["parent"] == "root":
        parent_ref = node.get("commit") or current_branch(root)
    else:
        parent_node = _read_node(root, node["parent"])
        parent_ref = parent_node.get("commit") or parent_node["branch"]
    diff_text = render_git_diff(
        root, parent_ref, worktree, relative_target(config), executor=executor,
    )
    (a_dir / "diff.patch").write_text(diff_text, encoding="utf-8")

    gate_records: list[dict] = []
    runtime_records: list[dict] = []
    benchmark_record: dict | None = None
    resume_phase = str((resume_state or {}).get("phase") or "")
    benchmark_completed = (
        resume_existing_attempt
        and _attempt_phase_index(resume_phase) >= _attempt_phase_index("artifacts")
        and result_path.exists()
    )

    try:
        for hook_name, log_name in (
            ("prepare", "runtime_prepare.log"),
            ("before_run", "runtime_before_run.log"),
        ):
            phase_name = f"runtime_{hook_name}"
            if (
                resume_existing_attempt
                and _attempt_phase_index(resume_phase) > _attempt_phase_index(phase_name)
            ):
                continue
            _write_attempt_state(
                root, args.exp_id, attempt_n,
                phase=phase_name, status="running", started_at=started_at,
            )
            record = _run_runtime_hook(
                executor,
                config,
                hook_name,
                root=root,
                worktree=worktree,
                target=target,
                env=env,
                timeout=args.timeout,
                log_path=a_dir / log_name,
            )
            if record is not None:
                runtime_records.append(record)
            _write_attempt_state(
                root, args.exp_id, attempt_n,
                phase=phase_name, status="completed", started_at=started_at,
            )

        # Run benchmark via sh -c so the user-provided command string
        # (with placeholders interpolated) executes through a shell, same
        # semantics as before.
        if benchmark_completed:
            print(
                f"RECOVERING {args.exp_id} attempt={attempt_n} "
                f"phase={resume_phase} state={(resume_state or {}).get('status')}"
            )
            bench = None
        elif resume_journal:
            print(
                f"RECOVERING {args.exp_id} attempt={attempt_n} "
                f"process={resume_journal['process_id']} "
                f"state={resume_journal.get('state')}"
            )
            _write_attempt_state(
                root, args.exp_id, attempt_n,
                phase="benchmark", status="attaching", started_at=started_at,
                extra={"process_id": resume_journal.get("process_id")},
            )
            bench = executor.attach(
                str(resume_journal["process_id"]),
                cmd=[str(part) for part in resume_journal["command"]],
                cwd=str(resume_journal.get("cwd") or run_cwd),
                env=env,
                timeout=args.timeout,
                stdout_path=benchmark_log,
                stderr_path=benchmark_err,
                mirror_remote_dir=sandbox_traces_dir if remote else None,
                mirror_local_dir=traces_dir if remote else None,
                mirror_dirs=[(sandbox_checkpoint_dir, checkpoint_dir)] if remote else None,
                append=False,
            )
        else:
            _write_attempt_state(
                root, args.exp_id, attempt_n,
                phase="benchmark", status="running", started_at=started_at,
            )
            bench = executor.stream(
                ["sh", "-c", benchmark_cmd],
                cwd=run_cwd, env=env, timeout=args.timeout,
                stdout_path=benchmark_log, stderr_path=benchmark_err,
                mirror_remote_dir=sandbox_traces_dir if remote else None,
                mirror_local_dir=traces_dir if remote else None,
                mirror_dirs=[(sandbox_checkpoint_dir, checkpoint_dir)] if remote else None,
            )
        if bench is not None and bench.timed_out:
            raise RuntimeError("benchmark_timeout")
        if bench is not None and (bench.exit_code or 0) != 0:
            benchmark_record = {"command": benchmark_cmd, "returncode": bench.exit_code, "result": None}
            # Try to fetch result.json + traces back even on failure --
            # the benchmark may have written something useful before
            # crashing.
            if remote:
                _fetch_remote_artifacts(executor, sandbox_result_path,
                                        sandbox_traces_dir, result_path, traces_dir)
                executor.fetch_dir(sandbox_checkpoint_dir, checkpoint_dir)
                infra_error = _remote_infra_error_for_log(benchmark_log)
                if infra_error is not None:
                    raise RuntimeError(f"remote_infra_failure:{infra_error}")
            raise RuntimeError(f"benchmark_exit_{bench.exit_code}")

        # Pull result.json + traces from the sandbox (no-op for local).
        if remote and not benchmark_completed:
            _write_attempt_state(
                root, args.exp_id, attempt_n,
                phase="artifacts", status="running", started_at=started_at,
            )
            _fetch_remote_artifacts(executor, sandbox_result_path,
                                    sandbox_traces_dir, result_path, traces_dir)
            executor.fetch_dir(sandbox_checkpoint_dir, checkpoint_dir)
            _write_attempt_state(
                root, args.exp_id, attempt_n,
                phase="artifacts", status="completed", started_at=started_at,
            )
        elif not benchmark_completed:
            _write_attempt_state(
                root, args.exp_id, attempt_n,
                phase="benchmark", status="completed", started_at=started_at,
            )

        bench_stdout = bench.stdout if bench is not None else (
            benchmark_log.read_text(encoding="utf-8", errors="replace")
            if benchmark_log.exists() else ""
        )
        score, parsed = load_result(result_path, bench_stdout)
        benchmark_record = {"command": benchmark_cmd, "returncode": 0, "result": parsed}
        _write_attempt_state(
            root, args.exp_id, attempt_n,
            phase="artifacts" if remote else "benchmark",
            status="completed", started_at=started_at,
            extra={"score": score},
        )

        gate_passed = True
        gate_failures: list[str] = []

        inherited_gates, gate_origins = _inherited_gate_specs(config, graph, node["parent"])

        # Strip EVO_* so an SDK-using gate can't clobber result.json or
        # the benchmark's task_*.json traces.
        gate_env = {k: v for k, v in env.items() if not k.startswith("EVO_")}

        _write_attempt_state(
            root, args.exp_id, attempt_n,
            phase="gates", status="running", started_at=started_at,
        )
        for g in inherited_gates:
            gate_cmd = _apply_runtime_prefix(
                config,
                fill_command_template(g["command"], target=target, worktree=worktree),
            )
            gate_log_file = a_dir / f"gate_{g['name']}.log"
            gate_result = executor.stream(
                ["sh", "-c", gate_cmd],
                cwd=run_cwd, env=gate_env, timeout=args.timeout,
                stdout_path=gate_log_file, stderr_path=gate_log_file,
                mirror_dirs=[(sandbox_checkpoint_dir, checkpoint_dir)] if remote else None,
            )
            if gate_result.timed_out:
                gate_records.append({
                    "name": g["name"],
                    "from": gate_origins.get(g["name"], "config"),
                    "command": gate_cmd,
                    "passed": False,
                    "returncode": None,
                    "error": "gate_timeout",
                })
                raise RuntimeError(f"gate_timeout:{g['name']}")
            passed = (gate_result.exit_code or 0) == 0
            gate_records.append({
                "name": g["name"],
                "from": gate_origins.get(g["name"], "config"),
                "command": gate_cmd,
                "passed": passed,
                "returncode": gate_result.exit_code,
            })
            if not passed:
                gate_failures.append(g["name"])
                gate_passed = False

        if gate_failures:
            print(f"GATE_FAILED {' '.join(gate_failures)}")
        _write_attempt_state(
            root, args.exp_id, attempt_n,
            phase="gates", status="completed", started_at=started_at,
            extra={"gate_failures": gate_failures},
        )

        keep = compare_scores(metric, score, parent_score) and gate_passed
        if keep:
            _write_attempt_state(
                root, args.exp_id, attempt_n,
                phase="commit", status="running", started_at=started_at,
            )
            commit = maybe_commit_worktree(
                node,
                node.get("hypothesis", "experiment"),
                commit_strategy=commit_strategy,
                executor=executor,
            )
            # In remote mode the commit lives only in the sandbox's git db.
            # Fetch it back to the orchestrator's repo so children
            # branching off this experiment can resolve the parent commit.
            # Skip when commit == parent (no source changes; benchmark
            # only wrote to traces / result.json which are gitignored
            # or outside the worktree). The parent is already local.
            if remote and commit:
                base_commit = (
                    parent_ref if node["parent"] == "root" else (
                        _read_node(root, node["parent"]).get("commit") or parent_ref
                    )
                )
                if commit != base_commit:
                    from .git_bundle import fetch_commit_from_sandbox
                    from .backends import remote_state as _rs
                    sandbox_record = next(
                        (s for s in _rs.read_state(root, backend.state_key)["sandboxes"]
                         if (s.get("leased_by") or {}).get("exp_id") == args.exp_id),
                        None,
                    )
                    bundle_dir = (sandbox_record or {}).get("bundle_dir")
                    fetch_commit_from_sandbox(
                        executor.client,                    # type: ignore[attr-defined]
                        local_repo=root,
                        base_commit=base_commit,
                        head_commit=commit,
                        sandbox_repo=str(worktree),
                        bundle_dir=bundle_dir,
                    )

            # For pool: fetch the commit from the slot's git store into the
            # main repo. Pool commits otherwise live only in slot directories
            # and aren't reachable from `cwd=root` for anchor refs, recovery,
            # or cross-slot child allocation.
            if commit:
                backend_kind = config.get("execution_backend") or "worktree"
                if backend_kind == "pool":
                    base_commit = (
                        parent_ref if node["parent"] == "root" else (
                            _read_node(root, node["parent"]).get("commit") or parent_ref
                        )
                    )
                    if commit != base_commit:
                        subprocess.run(
                            ["git", "-c", "protocol.file.allow=always",
                             "fetch", str(worktree), commit],
                            cwd=root, check=True, capture_output=True, text=True,
                        )

                # Anchor the commit via `refs/evo-anchor/<run>/<exp>` so it
                # survives `git branch -D` (which `evo discard` runs). At this
                # point the commit is in the main repo for all three backends:
                # worktree commits are made there directly; remote commits
                # were fetched above via fetch_commit_from_sandbox; pool
                # commits were just fetched from the slot.
                if backend_kind in ("worktree", "remote", "pool"):
                    meta = _load_meta(root)
                    run_id = meta.get("active", "run_0000")
                    _anchor_commit_ref(root, run_id, args.exp_id, commit)

            def _mark_committed(current_node: dict, _graph: dict) -> None:
                current_node["status"] = "committed"
                current_node["score"] = score
                current_node["commit"] = commit
                current_node["benchmark_result"] = parsed
                current_node["gate_result"] = gate_passed
                current_node["gate_failures"] = gate_failures

            update_node(root, args.exp_id, _mark_committed)
            if config.get("comparison_blocked") and node["parent"] == "root":
                mark_comparison_blocked(root, False)
            _finalize_result(root, args.exp_id, node, score, "committed", {"commit": commit})
            _write_attempt_state(
                root, args.exp_id, attempt_n,
                phase="complete", status="committed", started_at=started_at,
                extra={"commit": commit, "score": score},
            )
            _write_attempt_outcome(
                root, args.exp_id, attempt_n, "committed",
                node=node, started_at=started_at, score=score,
                benchmark=benchmark_record, runtime=runtime_records, gates=gate_records,
                commit=commit, parent_score=parent_score, metric=metric,
            )
            # Release the workspace lease on transition into `committed`.
            # Worktree backend: no-op. Pool backend: returns the slot to the
            # free queue. Failed and evaluated transitions retain the lease.
            from .backends import DiscardCtx as _DCtx, load_backend as _lb
            committed_node = dict(node)
            committed_node["status"] = "committed"
            committed_node["commit"] = commit
            _lb(root, node=committed_node, workspace_config=config).release_lease(
                _DCtx(root=root, node=committed_node)
            )
            delta = "" if parent_score is None else f" ({'+' if metric == 'max' else ''}{score - parent_score:.4f} vs parent)"
            print(f"COMMITTED {args.exp_id} {score}{delta}")
            return 0

        def _mark_evaluated(current_node: dict, _graph: dict) -> None:
            current_node["status"] = "evaluated"
            current_node["score"] = score
            current_node["benchmark_result"] = parsed
            current_node["gate_result"] = gate_passed
            current_node["gate_failures"] = gate_failures
            current_node["evaluated_attempts"] = int(current_node.get("evaluated_attempts", 0)) + 1

        update_node(root, args.exp_id, _mark_evaluated)
        _finalize_result(root, args.exp_id, node, score, "evaluated")
        _write_attempt_state(
            root, args.exp_id, attempt_n,
            phase="complete", status="evaluated", started_at=started_at,
            extra={"score": score},
        )
        _write_attempt_outcome(
            root, args.exp_id, attempt_n, "evaluated",
            node=node, started_at=started_at, score=score,
            benchmark=benchmark_record, runtime=runtime_records, gates=gate_records,
            parent_score=parent_score, metric=metric,
        )
        remaining = max_attempts - (evaluated_attempts + 1)
        suffix = f" ({remaining} attempts remaining)" if remaining > 0 else " (no attempts remaining -- retry blocked)"
        reason = []
        if not gate_passed:
            reason.append(f"gate_failed={','.join(gate_failures)}")
        if not compare_scores(metric, score, parent_score):
            reason.append(f"score_regressed (parent={parent_score})")
        print(f"EVALUATED {args.exp_id} score={score} {' '.join(reason)}{suffix}")
        return 0
    except Exception as exc:  # noqa: BLE001
        # Try to salvage score from traces written before failure
        salvaged_score = None
        salvaged_result = None
        try:
            trace_files = sorted(traces_dir.glob("*.json"))
            if trace_files:
                task_scores = {}
                for tf in trace_files:
                    t = json.loads(tf.read_text(encoding="utf-8"))
                    task_scores[t["task_id"]] = t.get("score", 0.0)
                if task_scores:
                    salvaged_score = round(sum(task_scores.values()) / len(task_scores), 4)
                    salvaged_result = {"score": salvaged_score, "tasks": task_scores}
        except Exception:
            pass

        error_msg = str(exc)

        def _mark_failed(current_node: dict, _graph: dict) -> None:
            current_node["status"] = "failed"
            current_node["error"] = error_msg
            if salvaged_score is not None:
                current_node["score"] = salvaged_score
                current_node["benchmark_result"] = salvaged_result

        update_node(root, args.exp_id, _mark_failed)
        _finalize_result(root, args.exp_id, node, salvaged_score, "failed", {"error": str(exc)})
        _write_attempt_state(
            root, args.exp_id, attempt_n,
            phase="failed", status="failed", started_at=started_at,
            extra={"error": error_msg, "score": salvaged_score},
        )
        _write_attempt_outcome(
            root, args.exp_id, attempt_n, "failed",
            node=node, started_at=started_at, score=salvaged_score,
            benchmark=benchmark_record, runtime=runtime_records, gates=gate_records,
            error=error_msg, parent_score=parent_score, metric=metric,
        )
        print(f"FAILED {args.exp_id} {exc}")
        return 1


def _record_done_result(root: Path, args: argparse.Namespace) -> int:
    config, graph = _require_workspace(root)
    node = _read_node(root, args.exp_id)
    if node.get("status") not in (None, "pending", "active", "evaluated", "failed"):
        print(f"ERROR: {args.exp_id} has status '{node['status']}' -- cannot record again", file=sys.stderr)
        return 1
    # `evo done` is the manual recording path; it mirrors `evo run`'s
    # attempt-scoped artifact layout so that `evo traces` and the dashboard
    # surface manually-recorded traces the same way as locally-run ones.
    attempt_n = int(node.get("current_attempt", 0)) + 1

    def _bump_attempt(current_node: dict, _graph: dict) -> None:
        current_node["current_attempt"] = attempt_n

    update_node(root, args.exp_id, _bump_attempt)
    a_dir = attempt_dir(root, args.exp_id, attempt_n)
    a_dir.mkdir(parents=True, exist_ok=True)
    if args.traces:
        traces_dir = attempt_traces_dir(root, args.exp_id, attempt_n)
        traces_dir.mkdir(parents=True, exist_ok=True)
        source = Path(args.traces)
        if source.is_dir():
            for path in source.iterdir():
                if path.is_file():
                    shutil.copy2(path, traces_dir / path.name)
    if args.no_compare:
        def _mark_failed(current_node: dict, _graph: dict) -> None:
            current_node["status"] = "failed"
            current_node["score"] = args.score
        update_node(root, args.exp_id, _mark_failed)
        _finalize_result(root, args.exp_id, node, args.score, "failed", {"recorded_only": True})
        print(f"RECORDED {args.exp_id} score={args.score} (no compare)")
        return 0

    _block_if_epoch_requires_baseline(root, node["parent"], no_compare=False)
    parent_score = _resolve_parent_score(graph, node["parent"])
    metric = config["metric"]
    keep = compare_scores(metric, args.score, parent_score)
    if config.get("comparison_blocked") and node["parent"] == "root":
        mark_comparison_blocked(root, False)
    status = "committed" if keep else "evaluated"

    def _mark(current_node: dict, _graph: dict) -> None:
        current_node["status"] = status
        current_node["score"] = args.score
        if status == "evaluated":
            current_node["evaluated_attempts"] = int(current_node.get("evaluated_attempts", 0)) + 1

    update_node(root, args.exp_id, _mark)
    _finalize_result(root, args.exp_id, node, args.score, status, {"recorded_only": True})
    if status == "committed":
        from .backends import DiscardCtx as _DCtx, load_backend as _lb
        committed_node = {**node, "status": "committed"}
        _lb(root, node=committed_node, workspace_config=config).release_lease(
            _DCtx(root=root, node=committed_node)
        )
    print(f"{status.upper()} {args.exp_id} {args.score}")
    return 0


def cmd_done(args: argparse.Namespace) -> int:
    return _record_done_result(repo_root(), args)


def cmd_discard(args: argparse.Namespace) -> int:
    root = repo_root()
    graph = load_graph(root)
    node = _read_node(root, args.exp_id)
    status = node.get("status")
    force = bool(getattr(args, "force", False))

    # Guard: committed nodes must not be discarded — destroying their branch
    # ref orphans the commit (vulnerable to git GC). The orchestrator should
    # use `evo prune` instead, which preserves branch+commit while marking
    # the lineage as exhausted.
    if status == "committed":
        raise RuntimeError(
            f"cannot discard committed node {args.exp_id}: discarding would "
            f"destroy its branch ref and risk orphaning the commit. "
            f"Use `evo prune {args.exp_id} --reason \"...\"` instead — "
            f"prune marks the lineage exhausted while preserving the commit "
            f"so it can be branched from later (or restored via `evo restore`)."
        )

    # Guard: active nodes are mid-run; discard is racy because the running
    # `evo run` process may still write outcomes that contradict the discard.
    # Require explicit --force to acknowledge.
    if status == "active" and not force:
        raise RuntimeError(
            f"cannot discard active node {args.exp_id}: a benchmark may be "
            f"in flight. Wait for the run to finish, or pass --force to "
            f"override (the running process may still write a final outcome "
            f"that contradicts this discard)."
        )

    # Guard: discarding a node with live (non-discarded) children orphans
    # those children's parent reference. Mirror cmd_gc's existing guard.
    live_children = [
        cid for cid in node.get("children", [])
        if cid in graph["nodes"]
        and graph["nodes"][cid].get("status") != "discarded"
    ]
    if live_children:
        raise RuntimeError(
            f"cannot discard {args.exp_id}: it has {len(live_children)} "
            f"non-discarded child experiment(s) ({', '.join(live_children[:3])}"
            f"{', ...' if len(live_children) > 3 else ''}). "
            f"Discard or commit-and-prune those first."
        )

    def _mark(current_node: dict, _graph: dict) -> None:
        current_node["status"] = "discarded"
        current_node["discard_reason"] = args.reason

    update_node(root, args.exp_id, _mark)
    _finalize_result(root, args.exp_id, node, node.get("score"), "discarded", {"reason": args.reason})
    delete_discarded_experiment(root, node)
    print(f"DISCARDED {args.exp_id}: {args.reason}")
    return 0


def cmd_restore(args: argparse.Namespace) -> int:
    """Bring a pruned or discarded node back into play.

    pruned → committed: status flip; frontier surfaces it again.
    discarded → committed: verify `refs/evo-anchor/<run>/<exp>` exists in the main
        repo (the anchor written at commit time), recreate the regular
        branch ref `refs/heads/<branch>` from it, mark the worktree field
        as stale (it was deleted by discard; next `evo new --parent` will
        re-allocate), and flip status. If the anchor is missing — meaning
        the commit was lost (git GC, or a backend that didn't anchor) —
        error and point at `experiments/<id>/attempts/NNN/diff.patch` for
        manual replay.
    """
    root = repo_root()
    node = _read_node(root, args.exp_id)
    status = node.get("status")

    if status == "pruned":
        def _unprune(current_node: dict, _graph: dict) -> None:
            current_node["status"] = "committed"
            current_node["pruned_reason"] = None

        update_node(root, args.exp_id, _unprune)
        print(f"RESTORED {args.exp_id}: pruned → committed")
        return 0

    if status == "discarded":
        commit = node.get("commit")
        if not commit:
            raise RuntimeError(
                f"cannot restore {args.exp_id}: no commit hash recorded "
                f"(node was likely never committed)."
            )
        meta = _load_meta(root)
        run_id = meta.get("active") or "run_0000"
        anchor_ref = f"refs/evo-anchor/{run_id}/{args.exp_id}"

        # Verify the anchor ref exists and points at the recorded commit.
        anchor_check = subprocess.run(
            ["git", "rev-parse", anchor_ref],
            cwd=root, check=False, capture_output=True, text=True,
        )
        if anchor_check.returncode != 0:
            # No anchor → fall back to direct commit lookup
            commit_check = subprocess.run(
                ["git", "cat-file", "-e", commit],
                cwd=root, check=False, capture_output=True, text=True,
            )
            if commit_check.returncode != 0:
                # Commit is gone too. Point at the diff.patch.
                attempt = node.get("current_attempt", 1)
                diff_path = (
                    f"experiments/{args.exp_id}/attempts/{int(attempt):03d}/diff.patch"
                )
                raise RuntimeError(
                    f"cannot restore {args.exp_id}: anchor ref {anchor_ref} "
                    f"is missing AND commit {commit} is no longer reachable "
                    f"in git (likely garbage-collected). The change content "
                    f"may still be recoverable from {diff_path} (apply it "
                    f"manually to the parent and create a fresh commit)."
                )
            # Commit exists but no anchor — keep going; we'll write the
            # anchor as part of restoration.

        # Recreate the regular branch from the commit so future
        # `evo new --parent <id>` can fork from it cleanly.
        branch = node.get("branch")
        if branch:
            # If the branch happens to still exist (e.g. discard's
            # `git branch -D` failed silently in the past), don't error —
            # just point it at the right commit.
            subprocess.run(
                ["git", "branch", "-f", branch, commit],
                cwd=root, check=True, capture_output=True, text=True,
            )

        # If the anchor was missing, write it now (restoration is the
        # right moment to re-anchor — we just confirmed the commit exists).
        if anchor_check.returncode != 0:
            _anchor_commit_ref(root, run_id, args.exp_id, commit)

        def _undiscard(current_node: dict, _graph: dict) -> None:
            current_node["status"] = "committed"
            current_node["discard_reason"] = None
            # Mark the worktree as stale; the next allocator on this
            # parent will re-resolve.
            current_node["worktree_stale"] = True

        update_node(root, args.exp_id, _undiscard)
        print(
            f"RESTORED {args.exp_id}: discarded → committed (branch={branch} "
            f"recreated from {commit[:8]})"
        )
        return 0

    raise RuntimeError(
        f"cannot restore {args.exp_id}: status is {status!r}; "
        f"only `pruned` and `discarded` nodes can be restored."
    )


def cmd_prune(args: argparse.Namespace) -> int:
    root = repo_root()

    def _mark(current_node: dict, _graph: dict) -> None:
        # Prune accepts committed AND evaluated nodes:
        # - committed: "this lineage is exhausted; preserve the commit but
        #   stop branching from here"
        # - evaluated: "this didn't promote, but the score is informative;
        #   don't branch from it either"
        # Active/failed/discarded/pruned all stay forbidden.
        if current_node.get("status") not in ("committed", "evaluated"):
            raise RuntimeError(
                f"only committed or evaluated nodes can be pruned; "
                f"{args.exp_id} is {current_node.get('status')!r}"
            )
        current_node["status"] = "pruned"
        current_node["pruned_reason"] = args.reason

    update_node(root, args.exp_id, _mark)
    print(f"PRUNED {args.exp_id}: {args.reason}")
    return 0


def cmd_gc(args: argparse.Namespace) -> int:
    """Reclaim resources held by terminated experiments.

    Two passes:
      1. Per-node — for each node in {committed, failed, pruned} with no
         active children, dispatch to ITS backend (via node["backend"])
         and call backend.gc(node). Hybrid workspaces (mixed backends
         per node via `evo new --backend ...`) are handled because each
         node's resources go to the backend that allocated them.
      2. Cross-backend orphan sweep — for every backend present in the
         workspace (graph nodes + workspace default), call
         backend.sweep_orphans(live_exp_ids). This catches resources
         whose graph entry has vanished entirely (stale leases, leaked
         remote sandboxes, abandoned worktree directories).

    Pass 1 is bounded by graph size; pass 2 is bounded by per-backend
    state-file size.
    """
    from .backends import (
        DiscardCtx as _DCtx,
        backend_spec_for_node,
        load_backend as _lb,
    )

    root = repo_root()
    config = load_config(root)
    graph = load_graph(root)
    live_exp_ids: set[str] = {nid for nid in graph["nodes"] if nid != "root"}

    removed: list[str] = []
    backends_used: dict[tuple[str, str], Any] = {}

    def _resolve_backend(node: dict | None):
        """Pick the backend for `node` (or workspace default if None).
        Cached on (name, frozenset(config_items)) so we instantiate once
        per distinct backend in this gc run."""
        if node is not None:
            spec_name, spec_config = backend_spec_for_node(root, node, workspace_config=config)
        else:
            from .backends import backend_spec_from_config
            spec_name, spec_config = backend_spec_from_config(config)
        key = (spec_name, json.dumps(spec_config, sort_keys=True))
        if key in backends_used:
            return backends_used[key]
        backend = _lb(root, node=node, explicit_name=spec_name,
                     explicit_config=spec_config, workspace_config=config)
        backends_used[key] = backend
        return backend

    # Pass 1: per-node gc, dispatching by node["backend"]
    for node in graph["nodes"].values():
        if node["id"] == "root":
            continue
        if node.get("status") not in {"committed", "failed", "pruned"}:
            continue
        children = [graph["nodes"][cid] for cid in node.get("children", [])
                    if cid in graph["nodes"]]
        if any(child.get("status") == "active" for child in children):
            continue
        try:
            backend = _resolve_backend(node)
        except Exception:
            continue
        try:
            if backend.gc(_DCtx(root=root, node=node)):
                removed.append(node["id"])
        except Exception:
            # Best-effort cleanup; one node's failure doesn't block others.
            pass

    # Pass 2: cross-backend orphan sweep. Walk every backend referenced
    # by any node, plus the workspace-default backend, so we sweep state
    # for resources whose graph entry has vanished.
    swept: dict[str, list[str]] = {}
    distinct_specs: dict[tuple[str, str], dict | None] = {}
    for node in graph["nodes"].values():
        if node["id"] == "root":
            continue
        try:
            spec_name, spec_config = backend_spec_for_node(root, node, workspace_config=config)
            key = (spec_name, json.dumps(spec_config, sort_keys=True))
            distinct_specs[key] = node
        except Exception:
            continue
    # Always include workspace default
    try:
        from .backends import backend_spec_from_config
        spec_name, spec_config = backend_spec_from_config(config)
        key = (spec_name, json.dumps(spec_config, sort_keys=True))
        distinct_specs.setdefault(key, None)
    except Exception:
        pass

    for (name, _key), example_node in distinct_specs.items():
        try:
            backend = _resolve_backend(example_node)
            if hasattr(backend, "sweep_orphans"):
                ids = backend.sweep_orphans(root, live_exp_ids)
                if ids:
                    swept[name] = swept.get(name, []) + list(ids)
        except Exception:
            pass

    output: dict[str, Any] = {"removed": removed}
    if swept:
        output["swept"] = swept
    print(json.dumps(output, indent=2))
    return 0


def _stop_dashboard(root: Path) -> None:
    """Stop the background dashboard if running."""
    pid_file = evo_dir(root) / "dashboard.pid"
    port_file = evo_dir(root) / "dashboard.port"
    if pid_file.exists():
        try:
            pid = int(pid_file.read_text().strip())
            os.kill(pid, 15)  # SIGTERM
        except (OSError, ValueError):
            pass
        pid_file.unlink(missing_ok=True)
    port_file.unlink(missing_ok=True)


def cmd_reset(args: argparse.Namespace) -> int:
    if not args.yes:
        raise RuntimeError("reset is destructive; re-run with --yes")
    root = repo_root()
    _stop_dashboard(root)
    reset_runtime_state(root)
    print("Reset evo runtime state")
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    root = repo_root()
    config, graph = _require_workspace(root)
    metric = config["metric"]
    nodes = [node for node in graph["nodes"].values() if node["id"] != "root"]
    committed = [node for node in nodes if node.get("status") == "committed"]
    best = None
    if committed:
        scores = [float(node["score"]) for node in committed if node.get("score") is not None]
        best = max(scores) if metric == "max" else min(scores)
    print(
        f"metric={metric} epoch={config.get('current_eval_epoch', 1)} "
        f"experiments={len(nodes)} committed={sum(1 for n in nodes if n.get('status') == 'committed')} "
        f"evaluated={sum(1 for n in nodes if n.get('status') == 'evaluated')} "
        f"discarded={sum(1 for n in nodes if n.get('status') == 'discarded')} "
        f"failed={sum(1 for n in nodes if n.get('status') == 'failed')} "
        f"active={sum(1 for n in nodes if n.get('status') == 'active')} best={best}"
    )
    return 0


def cmd_tree(args: argparse.Namespace) -> int:
    root = repo_root()
    config, graph = _require_workspace(root)
    print(ascii_tree(graph, config["metric"]))
    return 0


def _format_frontier_help() -> str:
    from . import frontier_strategies as fs
    lines = [
        "evo frontier -- return frontier nodes (committed leaves) ranked by a selection strategy.",
        "",
        "Usage:",
        "  evo frontier                                    # use configured strategy",
        "  evo frontier --strategy <kind>                   # override for this call only",
        "  evo frontier --strategy <kind> --params '<json>' # override with custom params",
        "  evo frontier --seed <int>                        # pin rng for reproducible stochastic picks",
        "  evo frontier --help-strategies                   # this text",
        "",
        "Strategy is read from `.evo/config.json` under `frontier_strategy`.",
        "Set it once via the dashboard's strategy panel (top bar) or by editing the config directly.",
        "Every call appends an event to `.evo/infra_log.json` with kind=frontier.",
        "",
        "Available strategies:",
        "",
    ]
    for kind, spec in fs.FRONTIER_STRATEGIES.items():
        lines.append(f"  {kind}  -- {spec['label']}")
        lines.append(f"    {spec['description']}")
        if spec["params"]:
            lines.append("    params:")
            for p in spec["params"]:
                lines.append(
                    f"      {p['name']} ({p['type']}, {p['min']}..{p['max']}, default {p['default']})"
                    f"  -- {p['label']}"
                )
        else:
            lines.append("    params: none")
        lines.append("")
    lines.append("Output envelope: {\"strategy\": {...}, \"generated_at\": \"...\", \"nodes\": [...], \"seed\": <int>}")
    lines.append("Each node carries: id, score, eval_epoch (as \"epoch\"), hypothesis, rank.")
    return "\n".join(lines)


def cmd_frontier(args: argparse.Namespace) -> int:
    from . import frontier_strategies as fs
    if getattr(args, "help_strategies", False):
        print(_format_frontier_help())
        return 0
    root = repo_root()
    config, graph = _require_workspace(root)

    raw_nodes = frontier_nodes(graph)
    # Normalize each node to the minimal shape pickers/logs consume.
    summaries = [
        {
            "id": n["id"],
            "score": n.get("score"),
            "eval_epoch": n.get("eval_epoch"),
            "hypothesis": n.get("hypothesis"),
        }
        for n in raw_nodes
    ]

    # Resolve strategy: CLI overrides > config > default.
    strategy = fs.resolve_from_config(config)
    if getattr(args, "strategy", None):
        params = strategy["params"]
        if getattr(args, "params", None):
            try:
                params = json.loads(args.params)
            except json.JSONDecodeError as exc:
                print(f"ERROR: --params must be JSON: {exc}", file=sys.stderr)
                return 1
        strategy = fs.validate_frontier_strategy({"kind": args.strategy, "params": params})

    # Load per-experiment outcomes for strategies that need per-task vectors.
    outcomes: dict[str, dict] = {}
    if strategy["kind"] == "pareto_per_task":
        for n in raw_nodes:
            attempt = n.get("current_attempt")
            if not attempt:
                continue
            path = attempt_outcome_path(root, n["id"], int(attempt))
            if path.exists():
                try:
                    outcomes[n["id"]] = json.loads(path.read_text(encoding="utf-8"))
                except json.JSONDecodeError:
                    pass

    metric = config.get("metric", "max")
    try:
        ranked, seed_used = fs.pick(
            summaries, strategy, metric,
            outcomes=outcomes,
            seed=args.seed if getattr(args, "seed", None) is not None else None,
        )
    except ValueError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    envelope = {
        "strategy": strategy,
        "generated_at": utc_now(),
        "nodes": ranked,
    }
    # Seed only included when the strategy is stochastic, to keep deterministic
    # runs noise-free.
    if strategy["kind"] in {"epsilon_greedy", "softmax", "pareto_per_task"}:
        envelope["seed"] = seed_used

    fs.append_frontier_log(root, strategy, [n["id"] for n in ranked],
                           seed=envelope.get("seed"))

    print(json.dumps(envelope, indent=2))
    return 0


def cmd_scratchpad(args: argparse.Namespace) -> int:
    root = repo_root()
    print(build_scratchpad(root))
    return 0


def cmd_awaiting(args: argparse.Namespace) -> int:
    """List evaluated-but-not-decided nodes (the Awaiting Decision drill-down)."""
    root = repo_root()
    graph = load_graph(root)
    evaluated = [
        n for n in graph["nodes"].values()
        if n.get("status") == "evaluated"
    ]
    evaluated.sort(key=lambda n: n.get("updated_at", ""), reverse=True)
    print(json.dumps(evaluated, indent=2))
    return 0


def cmd_discards(args: argparse.Namespace) -> int:
    """List discarded nodes, optionally filtered by substring match on hypothesis."""
    root = repo_root()
    graph = load_graph(root)
    discarded = [
        n for n in graph["nodes"].values()
        if n.get("status") == "discarded"
    ]
    if getattr(args, "like", None):
        needle = args.like.lower()
        discarded = [
            n for n in discarded
            if needle in (n.get("hypothesis") or "").lower()
        ]
    discarded.sort(key=lambda n: n.get("updated_at", ""), reverse=True)
    print(json.dumps(discarded, indent=2))
    return 0


def cmd_show(args: argparse.Namespace) -> int:
    """Aggregate full state for one experiment node into one JSON blob.

    Replaces the multi-call pattern of `evo path` + reading outcomes +
    `evo annotations --exp X` + `evo diff X`. The agent never has to
    read graph state directly — everything it might want about one node
    is here in one call.
    """
    root = repo_root()
    graph = load_graph(root)
    if args.exp_id not in graph["nodes"]:
        raise RuntimeError(f"unknown experiment: {args.exp_id}")
    node = graph["nodes"][args.exp_id]

    chain = path_to_node(graph, args.exp_id)
    chain_ids = [n["id"] for n in chain]

    # Walk to find branch root (the child of root that begins this lineage).
    branch_root = args.exp_id
    cursor = node
    while cursor.get("parent") not in (None, "root"):
        branch_root = cursor["parent"]
        cursor = graph["nodes"][cursor["parent"]]
    if node.get("parent") == "root":
        branch_root = args.exp_id

    parent_score = None
    if node.get("parent") and node["parent"] in graph["nodes"]:
        parent_score = graph["nodes"][node["parent"]].get("score")

    score_delta = None
    if node.get("score") is not None and parent_score is not None:
        score_delta = round(node["score"] - parent_score, 6)

    # Collect attempts: outcome.json + raw diff text + change_files for each.
    attempts: list[dict[str, Any]] = []
    current = int(node.get("current_attempt", 0))
    for n in range(1, current + 1):
        outcome_path = attempt_outcome_path(root, args.exp_id, n)
        if not outcome_path.exists():
            continue
        try:
            outcome = json.loads(outcome_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        # Precompute the per-attempt score delta vs parent so the agent
        # doesn't have to subtract; mirrors the top-level `score_delta`.
        a_score = outcome.get("score")
        a_parent = outcome.get("parent_score")
        a_delta = (
            round(a_score - a_parent, 6)
            if (a_score is not None and a_parent is not None) else None
        )
        attempt_payload: dict[str, Any] = {
            "n": n,
            "outcome": outcome,
            "score_delta": a_delta,
            "change_files": outcome.get("change_files", []),
        }
        # Include the raw diff text + line counts so the agent doesn't
        # have to follow up with `evo diff`.
        diff_path = (experiments_dir_for(root, args.exp_id)
                     / "attempts" / f"{n:03d}" / "diff.patch")
        if diff_path.exists():
            try:
                attempt_payload["diff"] = diff_path.read_text(encoding="utf-8")
                parsed = parse_diff_patch(root, args.exp_id, n)
                if parsed:
                    attempt_payload["diff_added"] = parsed["added"]
                    attempt_payload["diff_removed"] = parsed["removed"]
            except OSError:
                pass
        attempts.append(attempt_payload)

    # Annotations filtered to this exp.
    all_annotations = load_annotations(root).get("annotations", [])
    annotations = [a for a in all_annotations if a.get("experiment_id") == args.exp_id]

    payload = {
        "id": args.exp_id,
        "hypothesis": node.get("hypothesis"),
        "status": node.get("status"),
        "score": node.get("score"),
        "parent_id": node.get("parent"),
        "parent_score": parent_score,
        "score_delta": score_delta,
        "branch_root": branch_root,
        "path_to_root": chain_ids,
        "children": list(node.get("children", [])),
        "effective_gates": collect_gates_from_path(graph, args.exp_id),
        "attempts": attempts,
        "annotations": annotations,
        "notes": list(node.get("notes", [])),
        "tags": list(node.get("tags", [])),
        "discard_reason": node.get("discard_reason"),
        "pruned_reason": node.get("pruned_reason"),
    }
    print(json.dumps(payload, indent=2))
    return 0


def cmd_get(args: argparse.Namespace) -> int:
    root = repo_root()
    if args.filename:
        path = experiments_dir_for(root, args.exp_id) / args.filename
        print(path.read_text(encoding="utf-8"))
        return 0
    graph = load_graph(root)
    if args.exp_id not in graph["nodes"]:
        raise RuntimeError(f"unknown experiment: {args.exp_id}")
    node = dict(graph["nodes"][args.exp_id])
    node["own_gates"] = node.get("gates", [])
    node["gates"] = collect_gates_from_path(graph, args.exp_id)
    print(json.dumps(node, indent=2))
    return 0


def cmd_path(args: argparse.Namespace) -> int:
    root = repo_root()
    _config, graph = _require_workspace(root)
    if args.exp_id not in graph["nodes"]:
        raise RuntimeError(f"unknown experiment: {args.exp_id}")
    chain = path_to_node(graph, args.exp_id)
    for node in chain:
        score_str = f"  score={node['score']}" if node.get("score") is not None else ""
        hyp = f"  {node.get('hypothesis', '')}" if node["id"] != "root" else ""
        prefix = "  -> " if node["id"] != "root" else ""
        print(f"{prefix}{node['id']}{score_str}{hyp}")
    return 0


def cmd_diff(args: argparse.Namespace) -> int:
    root = repo_root()
    if args.other_id is None:
        node = _read_node(root, args.exp_id)
        attempt = int(node.get("current_attempt", 0))
        if attempt == 0:
            print("")
            return 0
        target = attempt_log_path(root, args.exp_id, attempt, "diff.patch")
        print(target.read_text(encoding="utf-8") if target.exists() else "")
        return 0
    config, graph = _require_workspace(root)
    node_a = _read_node(root, args.exp_id)
    node_b = _read_node(root, args.other_id)
    ref_a = node_a.get("commit") or node_a.get("branch")
    ref_b = node_b.get("commit") or node_b.get("branch")
    if not ref_a or not ref_b:
        raise RuntimeError("both experiments must have a commit or branch to diff")
    result = subprocess.run(
        ["git", "diff", ref_a, ref_b, "--", relative_target(config)],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    )
    print(result.stdout)
    return 0


def cmd_traces(args: argparse.Namespace) -> int:
    root = repo_root()
    node = _read_node(root, args.exp_id)
    attempt = int(node.get("current_attempt", 0))
    if attempt == 0:
        if args.task:
            print("")
        else:
            print("{}")
        return 0
    traces_dir = attempt_traces_dir(root, args.exp_id, attempt)
    if args.task:
        path = traces_dir / f"task_{args.task}.json"
        print(path.read_text(encoding="utf-8"))
        return 0
    payload = {}
    if traces_dir.exists():
        for path in sorted(traces_dir.glob("*.json")):
            payload[path.name] = json.loads(path.read_text(encoding="utf-8"))
    print(json.dumps(payload, indent=2))
    return 0


def cmd_annotate(args: argparse.Namespace) -> int:
    root = repo_root()
    entry = append_annotation(root, args.exp_id, args.task, args.analysis)
    print(json.dumps(entry, indent=2))
    return 0


def cmd_annotations(args: argparse.Namespace) -> int:
    root = repo_root()
    entries = load_annotations(root).get("annotations", [])
    if args.task:
        entries = [entry for entry in entries if entry.get("task_id") == args.task]
    if args.exp:
        entries = [entry for entry in entries if entry.get("experiment_id") == args.exp]
    print(json.dumps(entries, indent=2))
    return 0


def cmd_log(args: argparse.Namespace) -> int:
    root = repo_root()
    payload = sys.stdin.read()
    path = experiments_dir_for(root, args.exp_id) / args.filename
    path.write_text(payload, encoding="utf-8")
    print(str(path))
    return 0


def cmd_set(args: argparse.Namespace) -> int:
    root = repo_root()

    def _mutate(current_node: dict, _graph: dict) -> None:
        current_node.setdefault("tags", [])
        current_node.setdefault("notes", [])
        if args.tag:
            if args.tag not in current_node["tags"]:
                current_node["tags"].append(args.tag)
        if args.note:
            current_node["notes"].append({
                "text": args.note,
                "timestamp": utc_now(),
                "exp_id": current_node["id"],
            })

    node = update_node(root, args.exp_id, _mutate)
    print(json.dumps(node, indent=2))
    return 0


def cmd_note(args: argparse.Namespace) -> int:
    """Add a workspace-level note (no experiment attached)."""
    root = repo_root()
    entry = add_workspace_note(root, args.text)
    print(json.dumps(entry, indent=2))
    return 0


def cmd_notes_list(args: argparse.Namespace) -> int:
    """List all notes (workspace + per-node), most-recent first."""
    root = repo_root()
    graph = load_graph(root)
    notes = list_all_notes(graph)
    if getattr(args, "exp", None):
        notes = [n for n in notes if n.get("exp_id") == args.exp]
    if getattr(args, "workspace", False):
        notes = [n for n in notes if n.get("exp_id") is None]
    limit = getattr(args, "limit", None)
    if limit:
        notes = notes[: int(limit)]
    print(json.dumps(notes, indent=2))
    return 0


def cmd_infra(args: argparse.Namespace) -> int:
    root = repo_root()
    event = append_infra_event(root, args.message, args.breaking)
    if args.breaking:
        config = load_config(root)
        config["current_eval_epoch"] = int(config.get("current_eval_epoch", 1)) + 1
        config["comparison_blocked"] = True
        save_config(root, config)
    print(json.dumps(event, indent=2))
    return 0


def cmd_gate(args: argparse.Namespace) -> int:
    root = repo_root()
    config, graph = _require_workspace(root)

    if args.gate_action == "add":
        entry = add_gate(root, args.exp_id, args.name, args.command)
        print(json.dumps(entry, indent=2))
        return 0

    if args.gate_action == "remove":
        remove_gate(root, args.exp_id, args.name)
        print(f"Removed gate '{args.name}' from {args.exp_id}")
        return 0

    if args.gate_action == "list":
        gates = collect_gates_from_path(graph, args.exp_id)
        # Annotate each gate with the node it came from
        node_gates_map: dict[str, str] = {}
        for node in path_to_node(graph, args.exp_id):
            for g in node.get("gates", []):
                node_gates_map[g["name"]] = node["id"]
        output = []
        for g in gates:
            output.append({
                "name": g["name"],
                "command": g["command"],
                "from": node_gates_map.get(g["name"], "unknown"),
            })
        print(json.dumps(output, indent=2))
        return 0

    if args.gate_action == "check":
        return _cmd_gate_check(args, root, config, graph)

    return 1


def _cmd_gate_check(args: argparse.Namespace, root: Path, config: dict, graph: dict) -> int:
    node = _read_node(root, args.exp_id)
    if not node.get("worktree"):
        print(f"ERROR: {args.exp_id} has no worktree to check gates against", file=sys.stderr)
        return 1

    from .workspace_executor import workspace_executor_for
    from .backends import load_backend

    backend = load_backend(root, node=node, workspace_config=config)
    with workspace_executor_for(backend, root, node) as executor:
        return _cmd_gate_check_impl(args, root, config, graph, node, executor)


def _cmd_gate_check_impl(
    args: argparse.Namespace,
    root: Path,
    config: dict,
    graph: dict,
    node: dict,
    executor: Any,
) -> int:
    started_at = utc_now()
    worktree = Path(node["worktree"])
    target = node_target_path(root, config, node)
    check_n, check_dir = _next_check_dir(root, args.exp_id)
    gates_dir = check_dir / "gates"
    gates_dir.mkdir(parents=True, exist_ok=True)
    remote = executor.is_remote
    run_cwd: Path | str = worktree if remote else root
    env = resolve_runtime_env(root, config)
    runtime_records: list[dict] = []
    gate_records: list[dict] = []
    inherited_gates, gate_origins = _inherited_gate_specs(config, graph, args.exp_id)
    try:
        for hook_name, log_name in (
            ("prepare", "runtime_prepare.log"),
            ("before_run", "runtime_before_run.log"),
        ):
            record = _run_runtime_hook(
                executor,
                config,
                hook_name,
                root=root,
                worktree=worktree,
                target=target,
                env=env,
                timeout=args.timeout,
                log_path=check_dir / log_name,
            )
            if record is not None:
                runtime_records.append(record)
    except Exception as exc:  # noqa: BLE001
        payload = {
            "experiment_id": args.exp_id,
            "check": check_n,
            "status": "failed",
            "kind": "gate",
            "started_at": started_at,
            "finished_at": utc_now(),
            "runtime": runtime_records,
            "gates": [],
            "error": str(exc),
        }
        atomic_write_json(check_dir / "gate_check.json", payload)
        print(f"GATE_CHECK_FAILED {args.exp_id} {exc} artifacts={check_dir}")
        return 1
    if not inherited_gates:
        payload = {
            "experiment_id": args.exp_id,
            "check": check_n,
            "status": "passed",
            "kind": "gate",
            "started_at": started_at,
            "finished_at": utc_now(),
            "runtime": runtime_records,
            "gates": [],
            "error": None,
        }
        atomic_write_json(check_dir / "gate_check.json", payload)
        print(f"GATE_CHECK_PASSED {args.exp_id} no gates artifacts={check_dir}")
        return 0

    failures: list[str] = []
    for g in inherited_gates:
        gate_cmd = _apply_runtime_prefix(
            config,
            fill_command_template(g["command"], target=target, worktree=worktree),
        )
        log_file = gates_dir / f"{g['name']}.log"
        result = executor.stream(
            ["sh", "-c", gate_cmd],
            cwd=run_cwd, env=env, timeout=args.timeout,
            stdout_path=log_file, stderr_path=log_file,
        )
        passed = not result.timed_out and (result.exit_code or 0) == 0
        record = {
            "name": g["name"],
            "from": gate_origins.get(g["name"], "unknown"),
            "command": gate_cmd,
            "passed": passed,
            "returncode": result.exit_code,
        }
        if result.timed_out:
            record["error"] = "gate_timeout"
        gate_records.append(record)
        if not passed:
            failures.append(g["name"])

    status = "failed" if failures else "passed"
    payload = {
        "experiment_id": args.exp_id,
        "check": check_n,
        "status": status,
        "kind": "gate",
        "started_at": started_at,
        "finished_at": utc_now(),
        "runtime": runtime_records,
        "gates": gate_records,
        "error": f"gate_failed:{','.join(failures)}" if failures else None,
    }
    atomic_write_json(check_dir / "gate_check.json", payload)
    if failures:
        print(f"GATE_CHECK_FAILED {args.exp_id} {' '.join(failures)} artifacts={check_dir}")
        return 1
    print(f"GATE_CHECK_PASSED {args.exp_id} {len(gate_records)} gates artifacts={check_dir}")
    return 0


def cmd_dashboard(args: argparse.Namespace) -> int:
    from .dashboard import create_app

    root = repo_root()
    actual_port = _pick_free_port(args.port)
    (evo_dir(root) / "dashboard.port").write_text(str(actual_port))
    note = "" if actual_port == args.port else f" (port {args.port} busy, bumped to {actual_port})"
    print(f"Dashboard live: http://127.0.0.1:{actual_port}{note}", flush=True)
    app = create_app(root)
    app.run(host="127.0.0.1", port=actual_port, debug=False)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="evo")
    # Format includes the distribution name so skill checks can distinguish
    # this binary from unrelated `evo` packages on PATH.
    parser.add_argument(
        "--version",
        action="version",
        version=f"{DISTRIBUTION_NAME} {__version__}",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    init_p = sub.add_parser("init")
    init_p.add_argument("--name", help="human-readable project name for dashboard/display")
    init_p.add_argument("--target", required=True)
    init_p.add_argument("--benchmark", required=True)
    init_p.add_argument("--metric", required=True, choices=["max", "min"])
    init_p.add_argument("--gate")
    init_p.add_argument("--instrumentation-mode", choices=["sdk", "inline"])
    init_p.add_argument(
        "--host",
        required=True,
        choices=sorted(SUPPORTED_HOSTS),
        help="orchestrator runtime (claude-code/codex/opencode/openclaw/hermes/generic). "
             "Determines whether `evo dispatch` is available; other commands ignore it.",
    )
    init_p.add_argument(
        "--commit-strategy",
        choices=["all", "tracked-only"],
        default=None,
        help="commit policy for `evo run`. Default: 'all'. Override only "
             "if you know why.",
    )
    init_p.add_argument("--port", type=int, default=8080)
    init_p.set_defaults(func=cmd_init)

    host_p = sub.add_parser(
        "host",
        help="show or set the orchestrator host signature",
    )
    host_sub = host_p.add_subparsers(dest="host_action", required=True)
    host_show_p = host_sub.add_parser("show", help="print the current workspace host")
    host_show_p.set_defaults(func=cmd_host)
    host_set_p = host_sub.add_parser("set", help="update the workspace host")
    host_set_p.add_argument("value", choices=sorted(SUPPORTED_HOSTS))
    host_set_p.set_defaults(func=cmd_host)

    config_p = sub.add_parser(
        "config",
        help="mutate workspace configuration",
    )
    config_sub = config_p.add_subparsers(dest="config_action", required=True)
    config_show_p = config_sub.add_parser("show", help="show redacted workspace configuration")
    config_show_p.add_argument("--json", action="store_true", help="emit JSON")
    config_show_p.set_defaults(func=cmd_config)
    config_set_p = config_sub.add_parser("set", help="set basic workspace configuration fields")
    config_set_p.add_argument(
        "field",
        choices=["project-name", "target", "benchmark", "metric", "commit-strategy"],
    )
    config_set_p.add_argument("value")
    config_set_p.set_defaults(func=cmd_config)
    config_runtime_p = config_sub.add_parser(
        "runtime",
        help="configure workspace runtime recipe",
    )
    runtime_sub = config_runtime_p.add_subparsers(dest="runtime_action", required=True)
    runtime_show_p = runtime_sub.add_parser("show", help="show runtime prepare/before-run/prefix")
    runtime_show_p.add_argument("--json", action="store_true", help="emit JSON")
    runtime_show_p.set_defaults(func=cmd_config)
    runtime_set_p = runtime_sub.add_parser("set", help="set runtime prepare/before-run/prefix")
    runtime_set_p.add_argument("--prepare", help="command that prepares the experiment workspace")
    runtime_set_p.add_argument("--before-run", dest="before_run", help="command that resets per-attempt runtime state")
    runtime_set_p.add_argument("--prefix", help="command prefix for benchmark and gate commands")
    runtime_set_p.set_defaults(func=cmd_config)
    config_backend_p = config_sub.add_parser(
        "backend",
        help="set the workspace default execution backend",
    )
    config_backend_p.add_argument("backend", choices=["worktree", "pool", "remote"])
    config_backend_p.add_argument(
        "--workspaces",
        help="comma-separated absolute paths to pre-built workspace directories "
             "(required with backend=pool).",
    )
    config_backend_p.add_argument(
        "--provider",
        help="remote sandbox provider (required with backend=remote unless "
             "--remote is used).",
    )
    config_backend_p.add_argument(
        "--provider-config",
        help="optional comma-separated key=value pairs forwarded to the "
             "remote provider.",
    )
    config_backend_p.add_argument(
        "--remote",
        help="shorthand for backend=remote. Examples: modal, ssh:user@host, "
             "my_pkg.providers:Provider.",
    )
    config_backend_p.set_defaults(func=cmd_config_backend)

    env_p = sub.add_parser(
        "env",
        help="configure benchmark/gate runtime environment forwarding",
    )
    env_sub = env_p.add_subparsers(dest="env_action", required=True)
    env_show_p = env_sub.add_parser("show", help="show redacted runtime env metadata")
    env_show_p.add_argument("--json", action="store_true", help="emit JSON")
    env_show_p.set_defaults(func=cmd_env)
    env_inherit_p = env_sub.add_parser(
        "inherit-shell",
        help="enable or disable inheriting the orchestrator process environment",
    )
    env_inherit_p.add_argument("value", choices=["on", "off"])
    env_inherit_p.set_defaults(func=cmd_env)
    env_load_p = env_sub.add_parser("load", help="add or update a dotenv source")
    env_load_p.add_argument("path")
    env_load_mode = env_load_p.add_mutually_exclusive_group(required=True)
    env_load_mode.add_argument("--all", action="store_true", help="forward all keys from this dotenv file")
    env_load_mode.add_argument("--allow", help="comma-separated allowlist of keys to forward")
    env_load_p.set_defaults(func=cmd_env)
    env_clear_p = env_sub.add_parser("clear", help="remove all configured dotenv sources")
    env_clear_p.set_defaults(func=cmd_env)

    workspace_p = sub.add_parser(
        "workspace",
        help="inspect or release pool slots (pool mode only)",
    )
    workspace_sub = workspace_p.add_subparsers(dest="workspace_action", required=True)
    workspace_status_p = workspace_sub.add_parser("status", help="show pool slot occupancy")
    workspace_status_p.add_argument("--json", action="store_true", help="emit JSON")
    workspace_status_p.set_defaults(func=cmd_workspace_status)
    workspace_release_p = workspace_sub.add_parser(
        "release", help="manually clear a stale lease"
    )
    workspace_release_p.add_argument("slot_id", type=int)
    workspace_release_p.set_defaults(func=cmd_workspace_release)

    new_p = sub.add_parser("new")
    new_p.add_argument("--parent", required=True)
    new_p.add_argument("-m", "--message", required=True)
    new_p.add_argument(
        "--backend",
        choices=["worktree", "pool", "remote"],
        help="per-experiment backend override. Omit to use the workspace default.",
    )
    new_p.add_argument(
        "--workspaces",
        help="comma-separated absolute paths to pre-built workspace directories "
             "(required with --backend pool).",
    )
    new_p.add_argument(
        "--provider",
        help="remote sandbox provider (required with --backend remote unless "
             "--remote is used).",
    )
    new_p.add_argument(
        "--provider-config",
        help="optional comma-separated key=value pairs forwarded to the "
             "remote provider.",
    )
    new_p.add_argument(
        "--remote",
        help="shorthand for remote backend selection. Examples: modal, "
             "ssh:user@host, my_pkg.providers:Provider.",
    )
    new_p.set_defaults(func=cmd_new)

    run_p = sub.add_parser("run")
    run_p.add_argument("exp_id")
    run_p.add_argument("--timeout", type=int, default=1800)
    run_p.add_argument(
        "--check",
        action="store_true",
        help="validate benchmark/gate wiring and write check artifacts without "
             "committing, evaluating, or consuming retry budget.",
    )
    run_p.add_argument(
        "--i-staged-new-files",
        dest="i_staged_new_files",
        default=None,
        metavar="yes",
        help="declarative ack (shisa-kanko): agent must pass exactly "
             "`--i-staged-new-files yes` to affirm it has `git add`'d any new "
             "source files in the worktree, leaving warm state untracked. "
             "Required in tracked-only commit mode when the worktree has "
             "untracked, non-gitignored files. No-op in commit_strategy=all.",
    )
    run_p.set_defaults(func=cmd_run)

    done_p = sub.add_parser("done")
    done_p.add_argument("exp_id")
    done_p.add_argument("--score", type=float, required=True)
    done_p.add_argument("--traces")
    done_p.add_argument("--no-compare", action="store_true")
    done_p.set_defaults(func=cmd_done)

    # ---- Workspace operations (route to local fs OR remote sandbox) ----
    # All of these REQUIRE explicit experiment context. Resolution order:
    #   1. --exp-id <id>
    #   2. $EVO_EXPERIMENT_ID env var
    #   3. error
    # The orchestrator passes --exp-id (or sets the env var when spawning a
    # subagent process) to scope each subagent to its own experiment.
    _ws_help = (
        "(workspace op) targets the experiment given by --exp-id or "
        "$EVO_EXPERIMENT_ID. Routes to the local worktree in worktree/pool "
        "mode, or to the leased remote sandbox in remote mode."
    )

    def _add_exp_id_arg(p: argparse.ArgumentParser) -> None:
        p.add_argument(
            "--exp-id",
            dest="exp_id",
            required=True,
            help="experiment id to operate on. Required: workspace ops never "
                 "default to an 'active' experiment because subagents run "
                 "concurrent experiments and a silent default would corrupt "
                 "the wrong container.",
        )

    bash_p = sub.add_parser("bash", help=_ws_help)
    _add_exp_id_arg(bash_p)
    bash_p.add_argument("command", help="shell command to run (passed to `sh -c`)")
    bash_p.add_argument("--cwd", default=None,
                        help="cwd for the command; defaults to the experiment's "
                             "worktree path (sandbox-internal in remote mode)")
    bash_p.add_argument("--timeout", type=int, default=None,
                        help="seconds; default unbounded (the sandbox-side "
                             "timeout still applies in remote mode)")
    bash_p.set_defaults(func=cmd_ws_bash)

    read_p = sub.add_parser("read", help=_ws_help)
    _add_exp_id_arg(read_p)
    read_p.add_argument("path", help="absolute path inside the workspace")
    read_p.set_defaults(func=cmd_ws_read)

    write_p = sub.add_parser("write", help=_ws_help)
    _add_exp_id_arg(write_p)
    write_p.add_argument("path", help="absolute path inside the workspace")
    write_p.add_argument("--content", default=None,
                         help="content to write (single-line). If omitted, "
                              "reads from stdin (any size, any encoding).")
    write_p.set_defaults(func=cmd_ws_write)

    edit_p = sub.add_parser("edit", help=_ws_help)
    _add_exp_id_arg(edit_p)
    edit_p.add_argument("path", help="absolute path inside the workspace")
    edit_p.add_argument("--old", default=None, help="old string (must be unique unless --replace-all)")
    edit_p.add_argument("--new", default=None, help="new string")
    edit_p.add_argument("--replace-all", action="store_true",
                        help="replace every occurrence of --old (default: refuse if not unique)")
    edit_p.add_argument("--json-stdin", action="store_true",
                        help='read {"old":...,"new":...,"replace_all":bool} '
                             'from stdin instead of --old/--new flags. Use '
                             'this when old/new contain multi-line content '
                             'or characters that are awkward in shell escapes.')
    edit_p.set_defaults(func=cmd_ws_edit)

    glob_p = sub.add_parser("glob", help=_ws_help)
    _add_exp_id_arg(glob_p)
    glob_p.add_argument("pattern", help="glob pattern (e.g. '*.py' or '**/*.md')")
    glob_p.add_argument("--path", default=None,
                        help="search root (defaults to the experiment's worktree)")
    glob_p.set_defaults(func=cmd_ws_glob)

    grep_p = sub.add_parser("grep", help=_ws_help)
    _add_exp_id_arg(grep_p)
    grep_p.add_argument("pattern", help="search pattern (passed to ripgrep, "
                                         "or grep -r as fallback)")
    grep_p.add_argument("--path", default=None,
                        help="search root (defaults to the experiment's worktree)")
    grep_p.set_defaults(func=cmd_ws_grep)

    discard_p = sub.add_parser("discard")
    discard_p.add_argument("exp_id")
    discard_p.add_argument("--reason", required=True)
    discard_p.add_argument(
        "--force",
        action="store_true",
        help="force discard of an active node (the running benchmark may "
             "still write a final outcome that contradicts this discard)",
    )
    discard_p.set_defaults(func=cmd_discard)

    prune_p = sub.add_parser("prune")
    prune_p.add_argument("exp_id")
    prune_p.add_argument("--reason", required=True)
    prune_p.set_defaults(func=cmd_prune)

    restore_p = sub.add_parser(
        "restore",
        help="restore a pruned or discarded node back to committed",
        description="Reverse of `evo prune` and `evo discard`. "
                    "Flips status back to committed; recreates the regular "
                    "branch ref from `refs/evo-anchor/<run>/<exp>` if needed. "
                    "Discarded nodes whose commit is no longer reachable "
                    "(git GC) are pointed to their saved diff.patch.",
    )
    restore_p.add_argument("exp_id")
    restore_p.set_defaults(func=cmd_restore)

    gc_p = sub.add_parser("gc")
    gc_p.set_defaults(func=cmd_gc)

    reset_p = sub.add_parser("reset")
    reset_p.add_argument("--yes", action="store_true")
    reset_p.set_defaults(func=cmd_reset)

    status_p = sub.add_parser("status")
    status_p.set_defaults(func=cmd_status)

    tree_p = sub.add_parser("tree")
    tree_p.set_defaults(func=cmd_tree)

    frontier_p = sub.add_parser(
        "frontier",
        help="list frontier nodes ranked by the configured strategy",
        description="Return frontier nodes ranked by the configured strategy. "
                    "Run `evo frontier --help-strategies` for full descriptions of each strategy and its params.",
    )
    frontier_p.add_argument("--strategy",
                            help="override configured strategy (run --help-strategies for options)")
    frontier_p.add_argument("--params", help="JSON params for the overridden strategy, e.g. '{\"k\": 5}'")
    frontier_p.add_argument("--seed", type=int, help="rng seed for stochastic strategies (default: fresh, logged)")
    frontier_p.add_argument("--help-strategies", dest="help_strategies", action="store_true",
                            help="print detailed description of each strategy and its params, then exit")
    frontier_p.set_defaults(func=cmd_frontier)

    scratchpad_p = sub.add_parser("scratchpad")
    scratchpad_p.set_defaults(func=cmd_scratchpad)

    awaiting_p = sub.add_parser(
        "awaiting",
        help="list evaluated nodes awaiting commit/discard decision",
        description="Drill-down for the scratchpad's Awaiting Decision section. "
                    "Returns JSON of every evaluated-but-not-decided node, "
                    "sorted by updated_at descending.",
    )
    awaiting_p.set_defaults(func=cmd_awaiting)

    discards_p = sub.add_parser(
        "discards",
        help="list discarded nodes, optionally filtered by hypothesis substring",
        description="Drill-down for the scratchpad's What Not To Try section. "
                    "Returns JSON of discarded nodes; pass --like TEXT for "
                    "case-insensitive substring match on the hypothesis.",
    )
    discards_p.add_argument("--like", help="case-insensitive substring match on hypothesis text")
    discards_p.set_defaults(func=cmd_discards)

    get_p = sub.add_parser("get")
    get_p.add_argument("exp_id")
    get_p.add_argument("filename", nargs="?")
    get_p.set_defaults(func=cmd_get)

    show_p = sub.add_parser(
        "show",
        help="full state of one experiment (status, score, attempts, "
             "annotations, notes, gates, lineage) as one JSON blob",
    )
    show_p.add_argument("exp_id")
    show_p.set_defaults(func=cmd_show)

    path_p = sub.add_parser("path")
    path_p.add_argument("exp_id")
    path_p.set_defaults(func=cmd_path)

    diff_p = sub.add_parser("diff")
    diff_p.add_argument("exp_id")
    diff_p.add_argument("other_id", nargs="?")
    diff_p.set_defaults(func=cmd_diff)

    traces_p = sub.add_parser("traces")
    traces_p.add_argument("exp_id")
    traces_p.add_argument("task", nargs="?")
    traces_p.set_defaults(func=cmd_traces)

    annotate_p = sub.add_parser("annotate")
    annotate_p.add_argument("exp_id")
    annotate_p.add_argument("task", nargs="?")
    annotate_p.add_argument("analysis")
    annotate_p.set_defaults(func=cmd_annotate)

    annotations_p = sub.add_parser("annotations")
    annotations_p.add_argument("--task")
    annotations_p.add_argument("--exp")
    annotations_p.set_defaults(func=cmd_annotations)

    log_p = sub.add_parser("log")
    log_p.add_argument("exp_id")
    log_p.add_argument("filename")
    log_p.set_defaults(func=cmd_log)

    set_p = sub.add_parser("set")
    set_p.add_argument("exp_id")
    set_p.add_argument("--tag")
    set_p.add_argument("--note")
    set_p.set_defaults(func=cmd_set)

    # `evo note "<text>"` — workspace-level note (no exp_id).
    # `evo set <exp_id> --note "<text>"` remains the per-node path.
    note_p = sub.add_parser(
        "note",
        help="add a workspace-level note (cross-cutting observation, "
             "not tied to any experiment)",
    )
    note_p.add_argument("text")
    note_p.set_defaults(func=cmd_note)

    notes_p = sub.add_parser(
        "notes",
        help="list all notes (workspace + per-node), most-recent first",
    )
    notes_p.add_argument("--exp", help="filter to one experiment id")
    notes_p.add_argument("--workspace", action="store_true",
                         help="filter to workspace-level notes only")
    notes_p.add_argument("--limit", type=int,
                         help="max notes to return (default: all)")
    notes_p.set_defaults(func=cmd_notes_list)

    infra_p = sub.add_parser("infra")
    infra_p.add_argument("-m", "--message", required=True)
    infra_p.add_argument("--breaking", action="store_true")
    infra_p.set_defaults(func=cmd_infra)

    gate_p = sub.add_parser("gate")
    gate_sub = gate_p.add_subparsers(dest="gate_action", required=True)

    gate_add_p = gate_sub.add_parser("add")
    gate_add_p.add_argument("exp_id")
    gate_add_p.add_argument("--name", required=True)
    gate_add_p.add_argument("--command", required=True)
    gate_add_p.set_defaults(func=cmd_gate)

    gate_list_p = gate_sub.add_parser("list")
    gate_list_p.add_argument("exp_id")
    gate_list_p.set_defaults(func=cmd_gate)

    gate_remove_p = gate_sub.add_parser("remove")
    gate_remove_p.add_argument("exp_id")
    gate_remove_p.add_argument("--name", required=True)
    gate_remove_p.set_defaults(func=cmd_gate)
    gate_check_p = gate_sub.add_parser("check")
    gate_check_p.add_argument("exp_id")
    gate_check_p.add_argument("--timeout", type=int, default=1800)
    gate_check_p.set_defaults(func=cmd_gate)

    dashboard_p = sub.add_parser("dashboard")
    dashboard_p.add_argument("--port", type=int, default=8080)
    dashboard_p.set_defaults(func=cmd_dashboard)

    dispatch_p = sub.add_parser(
        "dispatch",
        help="spawn a child fork from a parent's cached explorer session (claude-code only)",
        description=(
            "Allocate a new experiment under a parent and run a fork-session child "
            "inheriting the parent's explorer KV cache. Available on host=claude-code "
            "only; other hosts use their native parallel-Task primitive."
        ),
    )
    dispatch_sub = dispatch_p.add_subparsers(dest="dispatch_action", required=True)

    dispatch_run_p = dispatch_sub.add_parser(
        "run",
        help="allocate an experiment and run one fork child against it",
    )
    dispatch_run_p.add_argument("--parent", required=True, help="parent experiment id")
    dispatch_run_p.add_argument("-m", "--message", required=True, help="brief / hypothesis (free-form text)")
    dispatch_run_p.add_argument("--budget", type=int, default=3, help="iteration budget for the child (default 3)")
    dispatch_run_p.add_argument(
        "--explore-context",
        default=None,
        help="optional hint for the explorer's read pass; only used when explorer is being built",
    )
    dispatch_run_p.add_argument(
        "--refresh-explorer",
        action="store_true",
        help="force rebuild of the explorer for this parent even if cache is valid",
    )
    dispatch_run_p.add_argument(
        "--background",
        action="store_true",
        help="return immediately with job_id; use `evo dispatch wait` to block",
    )
    dispatch_run_p.set_defaults(func=cmd_dispatch)

    dispatch_wait_p = dispatch_sub.add_parser(
        "wait",
        help="block until specified jobs finish; if none given, wait on all running",
    )
    dispatch_wait_p.add_argument("job_ids", nargs="*", help="exp_id of each job to wait on")
    dispatch_wait_p.add_argument("--quiet", action="store_true", help="suppress per-job completion rows")
    dispatch_wait_p.set_defaults(func=cmd_dispatch)

    dispatch_list_p = dispatch_sub.add_parser("list", help="list dispatch jobs")
    dispatch_list_p.add_argument("--running", action="store_true", help="only show running jobs")
    dispatch_list_p.add_argument("--recent", type=int, default=None, help="trim to N most recent rows")
    dispatch_list_p.set_defaults(func=cmd_dispatch)

    dispatch_status_p = dispatch_sub.add_parser("status", help="show one job's full state")
    dispatch_status_p.add_argument("job_id", help="exp_id of the job")
    dispatch_status_p.set_defaults(func=cmd_dispatch)

    dispatch_kill_p = dispatch_sub.add_parser("kill", help="SIGTERM a running job")
    dispatch_kill_p.add_argument("job_id")
    dispatch_kill_p.set_defaults(func=cmd_dispatch)

    return parser


def main(argv: list[str] | None = None) -> None:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        rc = args.func(args)
    except Exception as exc:  # noqa: BLE001
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)
    sys.exit(rc)


if __name__ == "__main__":
    main()

"""Asset registry -- workspace-level named artifact management.

Provides ``evo asset put/get/list/use/rm``.  Registry lives at
``.evo/run_NNNN/assets/registry.json``; stored materialized assets live
alongside under ``.evo/run_NNNN/assets/<name>/``.

Design decisions (per evo-hq/evo#55):
- Per-run scope (cleaned up by ``evo reset``).
- Default ``put`` registers the source path in-place (no copy); pass
  ``--copy`` to materialize a copy under the assets directory.
- ``put --exp <id>`` mirrors the asset into the producer's
  ``node.benchmark_result.artifacts[]`` for backward-compat with the
  dashboard / ``evo traces`` views.
"""
from __future__ import annotations

import re
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .core import (
    advisory_lock,
    atomic_write_json,
    load_json,
    lock_file_for,
    update_node,
    workspace_path,
)

REGISTRY_FILE = "registry.json"
ASSETS_DIR = "assets"
ASSET_NAME_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._-]{0,127}$")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

def assets_dir(root: Path) -> Path:
    """``<run_dir>/assets/``"""
    return workspace_path(root) / ASSETS_DIR


def registry_path(root: Path) -> Path:
    return assets_dir(root) / REGISTRY_FILE


def asset_storage_dir(root: Path, name: str) -> Path:
    """Canonical materialization path: ``<run_dir>/assets/<name>/``."""
    return assets_dir(root) / name


def _lock_for_assets(root: Path) -> Path:
    return lock_file_for(registry_path(root))


# ---------------------------------------------------------------------------
# Load / save
# ---------------------------------------------------------------------------

def _default_registry() -> dict[str, Any]:
    return {"version": 1, "assets": {}}


def load_registry(root: Path) -> dict[str, Any]:
    return load_json(registry_path(root), _default_registry())


def save_registry(root: Path, data: dict[str, Any]) -> None:
    atomic_write_json(registry_path(root), data)


# ---------------------------------------------------------------------------
# Name helpers
# ---------------------------------------------------------------------------

def _validate_name(name: str) -> None:
    if not ASSET_NAME_RE.match(name):
        raise RuntimeError(
            f"invalid asset name {name!r}; must be 1-128 chars matching "
            f"[a-zA-Z0-9][a-zA-Z0-9._-]*"
        )


def _env_key(name: str) -> str:
    """Convert an asset name to an env-var key: ``EVO_ASSET_<NAME>``.

    Collapses runs of non-alnum characters into a single ``_`` and upper-cases.
    ``exp_0010-final-adapter`` -> ``EVO_ASSET_EXP_0010_FINAL_ADAPTER``.
    """
    cleaned = re.sub(r"[^A-Za-z0-9]+", "_", name).upper().strip("_")
    return f"EVO_ASSET_{cleaned}"


# ---------------------------------------------------------------------------
# Producer mirror
# ---------------------------------------------------------------------------

def _mirror_to_node(root: Path, exp_id: str, asset: dict[str, Any]) -> None:
    """Append the asset to the producer experiment's ``artifacts`` list so
    the existing dashboard + ``evo traces`` views surface it.

    Mirrors the ``[{kind, name, uri, content_key, created_by}]`` shape
    documented in ``finetuning/references/glue.md``.
    """

    def _apply(node: dict[str, Any], _graph: dict[str, Any]) -> None:
        bench = node.setdefault("benchmark_result", {})
        artifacts = bench.setdefault("artifacts", [])
        # Dedupe: same (kind, name) already present -> no-op.
        if any(
            a.get("name") == asset["name"] and a.get("kind") == asset["kind"]
            for a in artifacts
        ):
            return
        artifacts.append(
            {
                "kind": asset["kind"],
                "name": asset["name"],
                "uri": asset["uri"],
                "content_key": asset["name"],
                "created_by": exp_id,
            }
        )

    update_node(root, exp_id, _apply)


# ---------------------------------------------------------------------------
# CRUD
# ---------------------------------------------------------------------------

def put(
    root: Path,
    source: str,
    *,
    name: str,
    kind: str,
    exp: str | None = None,
    tag_pairs: list[str] | None = None,
    copy: bool = False,
    size_bytes: int | None = None,
) -> dict[str, Any]:
    """Register an asset in the workspace registry.

    If *copy* is True, materializes the source under
    ``<assets_dir>/<name>/`` and records that path.  Otherwise the
    source path is stored verbatim as the ``uri``.
    """
    _validate_name(name)
    src = Path(source)
    if not src.is_absolute():
        src = Path.cwd() / src
    if not src.exists():
        raise RuntimeError(f"source path does not exist: {src}")
    stored_path: str | None = None
    uri: str
    if copy:
        dest = asset_storage_dir(root, name)
        if dest.exists():
            shutil.rmtree(dest)
        dest.parent.mkdir(parents=True, exist_ok=True)
        if src.is_dir():
            shutil.copytree(src, dest, symlinks=True)
        else:
            dest.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dest / src.name)
            dest = dest / src.name
        uri = str(dest)
        stored_path = str(dest.relative_to(assets_dir(root)))
    else:
        uri = str(src.resolve(strict=False))

    computed_size: int | None = size_bytes
    if computed_size is None:
        if src.is_file():
            try:
                computed_size = src.stat().st_size
            except OSError:
                pass
        elif src.is_dir():
            total = 0
            for f in src.rglob("*"):
                if f.is_file():
                    try:
                        total += f.stat().st_size
                    except OSError:
                        pass
            computed_size = total if total else None

    # Parse tags.
    tags: dict[str, str] = {}
    for pair in (tag_pairs or []):
        if "=" not in pair:
            raise RuntimeError(f"tag must be k=v, got {pair!r}")
        k, _, v = pair.partition("=")
        tags[k.strip()] = v.strip()

    record: dict[str, Any] = {
        "name": name,
        "kind": kind,
        "uri": uri,
        "stored_path": stored_path,
        "source_path": str(src) if copy else None,
        "produced_by": exp,
        "consumed_by": [],
        "tags": tags,
        "size_bytes": computed_size,
        "created_at": _now_iso(),
        "updated_at": _now_iso(),
    }

    with advisory_lock(_lock_for_assets(root)):
        data = load_registry(root)
        data["assets"][name] = record
        save_registry(root, data)

    # Mirror into node.artifacts[] if --exp was given.
    if exp:
        _mirror_to_node(root, exp, record)

    return record


def get(root: Path, name: str) -> dict[str, Any] | None:
    """Return the full asset record, or None if not found."""
    data = load_registry(root)
    return data["assets"].get(name)


def list_assets(
    root: Path,
    *,
    kind: str | None = None,
    tag_pairs: list[str] | None = None,
    produced_by: str | None = None,
    consumed_by: str | None = None,
) -> list[dict[str, Any]]:
    """Return matching asset records, filtered by kind, tags, or lineage."""
    data = load_registry(root)
    results: list[dict[str, Any]] = []
    tag_filter: dict[str, str] = {}
    for pair in (tag_pairs or []):
        if "=" not in pair:
            continue
        k, _, v = pair.partition("=")
        tag_filter[k.strip()] = v.strip()

    for record in data["assets"].values():
        if kind and record.get("kind") != kind:
            continue
        if produced_by and record.get("produced_by") != produced_by:
            continue
        if consumed_by and consumed_by not in (record.get("consumed_by") or []):
            continue
        tags = record.get("tags") or {}
        if any(tags.get(k) != v for k, v in tag_filter.items()):
            continue
        results.append(record)
    return results


def use(root: Path, name: str, *, exp: str) -> None:
    """Record that *exp* consumed *name*.  Idempotent."""
    with advisory_lock(_lock_for_assets(root)):
        data = load_registry(root)
        record = data["assets"].get(name)
        if record is None:
            raise RuntimeError(f"unknown asset: {name}")
        consumed = record.setdefault("consumed_by", [])
        if exp not in consumed:
            consumed.append(exp)
            record["updated_at"] = _now_iso()
            save_registry(root, data)


def remove(root: Path, name: str, *, force: bool = False) -> None:
    """Remove *name* from the registry.

    Refuses if any non-discarded experiment still references it unless
    *force* is True.  Does not delete materialized files -- caller's job.
    """
    with advisory_lock(_lock_for_assets(root)):
        data = load_registry(root)
        if name not in data["assets"]:
            raise RuntimeError(f"unknown asset: {name}")
        record = data["assets"][name]
        if not force and record.get("consumed_by"):
            # Check whether any consumer is still non-discarded.
            graph_data = load_json(
                workspace_path(root) / "graph.json", {"nodes": {}}
            )
            live_consumers = [
                eid
                for eid in record["consumed_by"]
                if graph_data["nodes"].get(eid, {}).get("status") != "discarded"
            ]
            if live_consumers:
                raise RuntimeError(
                    f"asset {name!r} is still consumed by "
                    f"{', '.join(live_consumers)}; pass --force to remove"
                )
        del data["assets"][name]
        save_registry(root, data)

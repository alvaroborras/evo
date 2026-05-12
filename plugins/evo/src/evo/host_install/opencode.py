"""Opencode plugin install — drop the bundled JS plugin into opencode's
auto-discovery directory.

Opencode discovers plugins from `~/.config/opencode/plugins/*.{ts,js}`
automatically at startup. We ship a pre-bundled `evo.js` (built via
`bun build` from `evo/opencode_plugin/index.ts`).
"""

from __future__ import annotations

import argparse
import os
import shutil
import sys
from pathlib import Path


def _opencode_plugins_dir(workspace: bool = False) -> Path:
    if workspace:
        return Path.cwd() / ".opencode" / "plugins"
    home = Path.home()
    base = Path(os.environ.get("XDG_CONFIG_HOME", str(home / ".config")))
    return base / "opencode" / "plugins"


def _bundled_plugin_source() -> Path | None:
    """The pre-bundled evo.js shipped inside the evo pip wheel."""
    here = Path(__file__).resolve().parent.parent  # evo/
    bundle = here / "opencode_plugin" / "evo.bundle.js"
    return bundle if bundle.exists() else None


def install(args: argparse.Namespace) -> int:
    src = _bundled_plugin_source()
    if src is None:
        print(
            "ERROR: bundled opencode plugin not found in this evo install.\n"
            "  Expected at evo/opencode_plugin/evo.bundle.js (built via `bun build`).",
            file=sys.stderr,
        )
        return 2

    target_dir = _opencode_plugins_dir(workspace=args.workspace)
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / "evo.js"

    shutil.copyfile(src, target)
    print(f"installed evo plugin: {target}")
    if args.workspace:
        print("Workspace-local install. Restart `opencode` in this directory to load it.")
    else:
        print("Global install. Any opencode session will auto-load the plugin at startup.")
    return 0


def uninstall(args: argparse.Namespace) -> int:
    targets = [
        _opencode_plugins_dir(workspace=False) / "evo.js",
        _opencode_plugins_dir(workspace=True) / "evo.js",
    ]
    removed = 0
    for t in targets:
        if t.exists():
            t.unlink()
            print(f"removed: {t}")
            removed += 1
    if removed == 0:
        print("evo plugin not installed for opencode (nothing to remove)")
    return 0


def doctor(args: argparse.Namespace) -> int:
    targets = [
        _opencode_plugins_dir(workspace=False) / "evo.js",
        _opencode_plugins_dir(workspace=True) / "evo.js",
    ]
    found = [t for t in targets if t.exists()]
    if not found:
        print("✗ evo plugin not installed for opencode")
        print("  Run: evo install opencode")
        return 1
    for t in found:
        print(f"✓ {t}")
    return 0

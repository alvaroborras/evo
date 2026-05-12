"""Claude Code uses its own plugin marketplace — `evo install claude-code`
just points the user at the canonical install commands."""

from __future__ import annotations

import argparse
import sys


_INSTALL_HINT = """\
Claude Code installs plugins through its own marketplace. Run inside a
claude session:

    /plugin marketplace add evo-hq/evo
    /plugin install evo@evo-hq-evo

Hooks are enabled by default; nothing else is needed.
"""


def install(args: argparse.Namespace) -> int:
    print(_INSTALL_HINT)
    return 0


def uninstall(args: argparse.Namespace) -> int:
    print("Run inside a claude session: /plugin uninstall evo@evo-hq-evo")
    return 0


def doctor(args: argparse.Namespace) -> int:
    import json
    from pathlib import Path

    home = Path.home()
    settings = home / ".claude" / "settings.json"
    if not settings.exists():
        print(f"✗ {settings} not found — Claude Code not installed?")
        return 1

    try:
        data = json.loads(settings.read_text())
    except json.JSONDecodeError as exc:
        print(f"✗ could not parse {settings}: {exc}")
        return 1

    enabled = data.get("enabledPlugins", {})
    marketplaces = data.get("extraKnownMarketplaces", {})
    if "evo@evo-hq-evo" in enabled:
        print("✓ evo@evo-hq-evo enabled in claude settings")
    else:
        print("✗ evo@evo-hq-evo not in enabledPlugins")
        print("  Run inside claude: /plugin install evo@evo-hq-evo")
        return 1

    src = marketplaces.get("evo-hq-evo", {}).get("source", {})
    src_type = src.get("source")
    if src_type == "github":
        cache = home / ".claude" / "plugins" / "marketplaces" / "evo-hq-evo"
        if cache.exists():
            print(f"✓ marketplace cached at {cache}")
        else:
            print(f"✗ source=github but no cache at {cache} — try restarting claude")
            return 1
    elif src_type == "directory":
        path = Path(src.get("path", ""))
        if path.exists():
            print(f"✓ source=directory: {path} (CC reads directly, no cache)")
        else:
            print(f"✗ source=directory points at {path} which does not exist")
            return 1
    else:
        print(f"? unknown marketplace source type: {src_type}")
    return 0

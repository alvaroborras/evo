"""OpenClaw install — marketplace command for the plugin (skills + repo
files), plus a manual settings.json edit for the pi-coding-agent extension
that powers mid-run inject. Auto-bridge between openclaw plugin install
and pi-extension loader does not exist (verified upstream)."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path


_INSTALL_HINT = """\
OpenClaw installs the plugin through its marketplace:

    openclaw plugins install evo --marketplace https://github.com/evo-hq/evo

This installs evo's skills (`/discover`, `/optimize`, etc.) into
~/.openclaw/extensions/evo/.

For mid-run inject hooks, evo's pi-extension needs to be registered
explicitly with the pi-coding-agent runtime. This command will add it
to your pi settings file automatically.
"""


def _pi_settings_file() -> Path:
    home_override = os.environ.get("OPENCLAW_HOME")
    base = Path(home_override) if home_override else Path.home() / ".openclaw"
    return base / "agents" / "main" / "agent" / "settings.json"


def _evo_extension_path() -> Path:
    home_override = os.environ.get("OPENCLAW_HOME")
    base = Path(home_override) if home_override else Path.home() / ".openclaw"
    return base / "extensions" / "evo" / "pi-extension.js"


def _bundled_pi_extension_source() -> Path | None:
    here = Path(__file__).resolve().parent.parent  # evo/
    bundle = here / "openclaw_plugin" / "evo.bundle.js"
    return bundle if bundle.exists() else None


def install(args: argparse.Namespace) -> int:
    print(_INSTALL_HINT)
    settings = _pi_settings_file()
    ext_path = _evo_extension_path()

    src = _bundled_pi_extension_source()
    if src is None:
        print(
            "ERROR: bundled openclaw pi-extension not found in this evo install.\n"
            "  Expected at evo/openclaw_plugin/evo.bundle.js (built via `bun build`).",
            file=__import__("sys").stderr,
        )
        return 2

    # Copy our bundled pi-extension into the openclaw extensions dir,
    # creating the dir if `openclaw plugins install evo` hasn't been run yet.
    ext_path.parent.mkdir(parents=True, exist_ok=True)
    import shutil
    shutil.copyfile(src, ext_path)
    print(f"installed pi-extension: {ext_path}")

    settings.parent.mkdir(parents=True, exist_ok=True)
    data: dict = {}
    if settings.exists():
        try:
            data = json.loads(settings.read_text())
        except json.JSONDecodeError:
            print(f"ERROR: could not parse {settings}", file=__import__("sys").stderr)
            return 2
    extensions = data.setdefault("extensions", [])
    target = str(ext_path)
    if target not in extensions:
        extensions.append(target)
        settings.write_text(json.dumps(data, indent=2) + "\n")
        print(f"added {target} to extensions in {settings}")
    else:
        print(f"{target} already in extensions in {settings}")
    print()
    print("Restart any running openclaw agent session to load the extension.")
    return 0


def uninstall(args: argparse.Namespace) -> int:
    settings = _pi_settings_file()
    target = str(_evo_extension_path())
    if settings.exists():
        try:
            data = json.loads(settings.read_text())
            extensions = data.get("extensions", [])
            if target in extensions:
                extensions.remove(target)
                settings.write_text(json.dumps(data, indent=2) + "\n")
                print(f"removed {target} from {settings}")
        except json.JSONDecodeError:
            pass
    print("To remove the marketplace plugin: `openclaw plugins remove evo`")
    return 0


def doctor(args: argparse.Namespace) -> int:
    home_override = os.environ.get("OPENCLAW_HOME")
    base = Path(home_override) if home_override else Path.home() / ".openclaw"
    extdir = base / "extensions" / "evo"
    settings = _pi_settings_file()
    rc = 0

    if extdir.exists():
        print(f"✓ marketplace plugin installed at {extdir}")
    else:
        print(f"✗ no plugin at {extdir}")
        print("  Run: openclaw plugins install evo --marketplace https://github.com/evo-hq/evo")
        rc = 1

    target = str(_evo_extension_path())
    if settings.exists():
        try:
            data = json.loads(settings.read_text())
            if target in data.get("extensions", []):
                print(f"✓ pi-extension registered: {target}")
            else:
                print(f"✗ pi-extension not in {settings} extensions list")
                print("  Run: evo install openclaw")
                rc = 1
        except json.JSONDecodeError:
            print(f"✗ could not parse {settings}")
            rc = 1
    else:
        print(f"✗ {settings} not found (openclaw may not have run yet)")
        rc = 1
    return rc

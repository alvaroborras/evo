"""OpenClaw install — marketplace command for the evo plugin (skills
bundle), plus drop-in install of the evo-inject native plugin (mid-run
directive delivery) and trust grants in plugins.allow."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path


_INSTALL_HINT = """\
OpenClaw installs evo as two parts:

  1. evo skills (`/discover`, `/optimize`, etc.) via the marketplace
     command — populates ~/.openclaw/extensions/evo/.

  2. evo-inject native plugin — installed to ~/.openclaw/extensions/evo-inject/
     and trusted in ~/.openclaw/openclaw.json plugins.allow. This is
     what delivers `evo direct` directives mid-conversation.
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


def _openclaw_global_config() -> Path:
    home_override = os.environ.get("OPENCLAW_HOME")
    base = Path(home_override) if home_override else Path.home() / ".openclaw"
    return base / "openclaw.json"


def _native_plugin_install_dir() -> Path:
    home_override = os.environ.get("OPENCLAW_HOME")
    base = Path(home_override) if home_override else Path.home() / ".openclaw"
    return base / "extensions" / "evo-inject"


def _native_plugin_sources() -> tuple[Path, Path, Path] | None:
    here = Path(__file__).resolve().parent.parent
    native_dir = here / "openclaw_plugin" / "native"
    manifest = native_dir / "openclaw.plugin.json"
    index_js = native_dir / "index.js"
    hook_md = native_dir / "HOOK.md"
    if not all(p.exists() for p in (manifest, index_js, hook_md)):
        return None
    return manifest, index_js, hook_md


def install(args: argparse.Namespace) -> int:
    print(_INSTALL_HINT)
    settings = _pi_settings_file()
    ext_path = _evo_extension_path()

    # Drive `openclaw plugins install evo --marketplace ...` automatically
    # (this is what populates ~/.openclaw/extensions/evo/ with the skill
    # files and .claude-plugin/plugin.json manifest). Idempotent — openclaw
    # tolerates re-installs.
    import shutil as _shutil
    if _shutil.which("openclaw") is not None:
        import subprocess as _sp
        mkt_cmd = [
            "openclaw", "plugins", "install", "evo",
            "--marketplace", "https://github.com/evo-hq/evo",
        ]
        print(f"$ {' '.join(mkt_cmd)}")
        _sp.call(mkt_cmd)
    else:
        print(
            "NOTE: `openclaw` binary not on PATH; skipping marketplace install. "
            "Install OpenClaw first: npm install -g openclaw"
        )

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

    # Trust the evo + evo-inject plugins in ~/.openclaw/openclaw.json.
    # Current openclaw refuses to load discovered non-bundled plugins
    # unless their ids are in plugins.allow.
    global_cfg = _openclaw_global_config()
    gdata: dict | None
    if global_cfg.exists():
        try:
            gdata = json.loads(global_cfg.read_text())
        except json.JSONDecodeError:
            print(f"WARNING: could not parse {global_cfg}; skipping plugins.allow",
                  file=__import__("sys").stderr)
            gdata = None
    else:
        global_cfg.parent.mkdir(parents=True, exist_ok=True)
        gdata = {}

    if gdata is not None:
        plugins = gdata.setdefault("plugins", {})
        allow = plugins.setdefault("allow", [])
        entries = plugins.setdefault("entries", {})
        global_changed = False
        for plugin_id in ("evo", "evo-inject"):
            if plugin_id not in allow:
                allow.append(plugin_id)
                global_changed = True
            entry = entries.setdefault(plugin_id, {})
            if not entry.get("enabled"):
                entry["enabled"] = True
                global_changed = True
        if global_changed:
            global_cfg.write_text(json.dumps(gdata, indent=2) + "\n")
            print(f"updated {global_cfg}: enabled 'evo' + 'evo-inject'")
        else:
            print(f"'evo' + 'evo-inject' already enabled in {global_cfg}")

    # Install the openclaw-native plugin that delivers mid-run inject
    # via the `tool_result_persist` hook. Codex bundle (extensions/evo/)
    # takes plugin-loader precedence in its dir, so the native plugin
    # lives at extensions/evo-inject/ where openclaw.plugin.json is the
    # only manifest present.
    native_sources = _native_plugin_sources()
    if native_sources is None:
        print(
            "WARNING: openclaw native plugin not bundled in this evo install.\n"
            "  Expected at evo/openclaw_plugin/native/{openclaw.plugin.json,index.js,HOOK.md}\n"
            "  Mid-run inject will not work on openclaw.",
            file=__import__("sys").stderr,
        )
    else:
        manifest_src, index_src, hook_md_src = native_sources
        native_dir = _native_plugin_install_dir()
        native_dir.mkdir(parents=True, exist_ok=True)
        import shutil as _sh
        _sh.copyfile(manifest_src, native_dir / "openclaw.plugin.json")
        _sh.copyfile(index_src, native_dir / "index.js")
        _sh.copyfile(hook_md_src, native_dir / "HOOK.md")
        print(f"installed openclaw-native inject plugin: {native_dir}/")

        import shutil as _shutil2
        if _shutil2.which("openclaw") is not None:
            import subprocess as _sp2
            for sub in (["plugins", "registry", "rebuild"], ["plugins", "list"]):
                _sp2.call(["openclaw", *sub], stdout=_sp2.DEVNULL, stderr=_sp2.DEVNULL)

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

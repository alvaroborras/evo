"""Claude Code install — drives `claude plugin` non-interactive subcommands.

The `claude` binary exposes `marketplace add`, `install`, `update`, and
`uninstall` as shell-driveable subcommands (verified in
tests/live/test_install_sandbox.py + test_release_smoke.py). So `evo install
claude-code`, `evo update claude-code`, and `evo uninstall claude-code` can
all run end-to-end without the user typing slash commands inside an
interactive Claude Code session.

The `--force` flag on update wipes the plugin cache before reinstall,
working around an upstream cache-invalidation bug
(anthropics/claude-code#14061) that leaves the cache stale after a normal
`/plugin update`. Without --force, the standard update path is used; with
--force, the path that's guaranteed to land fresh code is used.
"""

from __future__ import annotations

import argparse
import re
import shutil
import subprocess
import sys
from pathlib import Path

from ._hook_drain import ensure_hook_drain_binary


_MARKETPLACE = "evo-hq/evo"
_PLUGIN = "evo@evo-hq-evo"
_MARKETPLACE_NAME = "evo-hq-evo"


def _latest_cache_dir() -> Path | None:
    """Return the latest-version plugin cache dir, or None if not installed.
    Cache layout: ~/.claude/plugins/cache/<mkt>/evo/<version>/
    """
    root = Path.home() / ".claude" / "plugins" / "cache" / _MARKETPLACE_NAME / "evo"
    if not root.exists():
        return None
    versions = sorted(p for p in root.iterdir() if p.is_dir())
    return versions[-1] if versions else None

# Strict release-tag shape: 'X.Y.Z' optionally followed by a pre-release
# suffix ('0.4.0-alpha.5', '0.4.0a5', '0.4.0rc1'). Only this shape gets
# auto-prefixed with 'v' to match the repo's release-tag convention.
# Everything else (branch names like 'main', commit SHAs, already-v-prefixed
# tags) passes through verbatim.
_RELEASE_VERSION_RE = re.compile(r"^\d+\.\d+\.\d+([.\-+a-zA-Z0-9]*)$")


def _marketplace_source(version: str | None, from_path: str | None) -> str:
    """Return the source string for `claude plugin marketplace add`.

    Precedence: `--from-path` > `--version` > unpinned latest.
    `claude plugin marketplace add` accepts 'owner/repo@<ref>' where <ref>
    can be a tag, branch, or commit SHA (verified in tests/live/test_release_smoke.py).
    """
    if from_path:
        return from_path
    if not version:
        return _MARKETPLACE
    # Release-version shape → auto-prefix with 'v' (repo's tag convention).
    # Anything else (branch, sha, already-prefixed) passes through as-is.
    ref = f"v{version}" if _RELEASE_VERSION_RE.match(version) else version
    return f"{_MARKETPLACE}@{ref}"


def _looks_like_pypi_release(version: str) -> bool:
    """True iff `version` could plausibly resolve to an evo-hq-cli release
    on PyPI. Branches/SHAs are git-only refs that PyPI doesn't know about,
    so we don't try `uv tool install evo-hq-cli==<branch>` for them.
    """
    return bool(_RELEASE_VERSION_RE.match(version))


def _claude_bin_or_error() -> str | None:
    claude = shutil.which("claude")
    if claude is None:
        print(
            "ERROR: `claude` binary not on PATH. Install Claude Code first:\n"
            "  npm install -g @anthropic-ai/claude-code",
            file=sys.stderr,
        )
    return claude


def _run(cmd: list[str]) -> int:
    print(f"$ {' '.join(cmd)}")
    return subprocess.call(cmd)


def install(args: argparse.Namespace) -> int:
    """`claude plugin marketplace add` + `claude plugin install`.

    `from_path` (optional): local marketplace dir to install from instead of
    the GitHub repo. Useful for testing unreleased changes.
    `scope` (optional, default "user"): plugin scope.
    """
    if _claude_bin_or_error() is None:
        return 2
    source = _marketplace_source(
        getattr(args, "version", None),
        getattr(args, "from_path", None),
    )
    scope = getattr(args, "scope", None) or "user"
    rc = _run(["claude", "plugin", "marketplace", "add", source])
    if rc != 0:
        # Marketplace may already be added; try install anyway. claude reports
        # "already added" with non-zero in some versions — don't bail yet.
        print("(marketplace add returned non-zero; continuing to install)")
    rc = _run(["claude", "plugin", "install", _PLUGIN, "--scope", scope])
    if rc != 0:
        return rc
    # Stage the platform-native evo-hook-drain binary inside the cache.
    # hooks.json points at ${CLAUDE_PLUGIN_ROOT}/bin/evo-hook-drain;
    # without the binary every hook fire would be a no-op.
    plugin_dir = _latest_cache_dir()
    if plugin_dir is not None:
        ensure_hook_drain_binary(plugin_dir, force=bool(getattr(args, "force", False)))
    print(
        f"\n✓ evo installed for claude-code at scope={scope}.\n"
        "  If you're inside an active claude session, run `/reload-plugins` "
        "to pick up the changes."
    )
    return 0


def update(args: argparse.Namespace) -> int:
    """Refresh the marketplace clone, then update the installed plugin.

    With `force=True`: wipe the plugin cache and reinstall from scratch
    (workaround for anthropics/claude-code#14061, the upstream cache
    invalidation bug that leaves stale files after a normal update).
    """
    if _claude_bin_or_error() is None:
        return 2
    scope = getattr(args, "scope", None) or "user"
    force = bool(getattr(args, "force", False))
    version = getattr(args, "version", None)
    from_path = getattr(args, "from_path", None)

    # If pinning a specific version (or a local path), re-add the marketplace
    # at that ref. `claude plugin marketplace add` with an existing name is
    # idempotent + replaces the source on re-add (verified in live tests).
    if version or from_path:
        source = _marketplace_source(version, from_path)
        rc = _run(["claude", "plugin", "marketplace", "add", source])
        if rc != 0:
            print(f"(marketplace add returned non-zero for {source}; continuing)")
    else:
        # Refresh the marketplace clone — that's where the new version
        # metadata lives.
        rc = _run(["claude", "plugin", "marketplace", "update", _MARKETPLACE_NAME])
        if rc != 0:
            print("(marketplace update returned non-zero; continuing)")

    if force:
        # Sidestep the upstream cache-invalidation bug: wipe cache, reinstall.
        cache = Path.home() / ".claude" / "plugins" / "cache" / _MARKETPLACE_NAME
        if cache.exists():
            print(f"$ rm -rf {cache}")
            shutil.rmtree(cache)
        # Uninstall first so install registers cleanly. Ignore failure — the
        # plugin may not be installed at all in some recovery scenarios.
        _run(["claude", "plugin", "uninstall", _PLUGIN])
        rc = _run(["claude", "plugin", "install", _PLUGIN, "--scope", scope])
    else:
        rc = _run(["claude", "plugin", "update", _PLUGIN, "--scope", scope])

    if rc != 0:
        return rc
    # Re-stage the hook-drain binary into the freshly-installed cache.
    # --force will re-download even if a binary's already there.
    plugin_dir = _latest_cache_dir()
    if plugin_dir is not None:
        ensure_hook_drain_binary(plugin_dir, force=force)
    print(
        "\n✓ evo updated for claude-code.\n"
        "  If you're inside an active claude session, run `/reload-plugins` "
        "to pick up the changes."
    )
    return 0


def uninstall(args: argparse.Namespace) -> int:
    if _claude_bin_or_error() is None:
        return 2
    return _run(["claude", "plugin", "uninstall", _PLUGIN])


def doctor(args: argparse.Namespace) -> int:
    import json

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
    rc = 0
    if _PLUGIN in enabled:
        print(f"✓ {_PLUGIN} enabled in claude settings")
    else:
        print(f"✗ {_PLUGIN} not in enabledPlugins")
        print("  Run: evo install claude-code")
        rc = 1

    src = marketplaces.get(_MARKETPLACE_NAME, {}).get("source", {})
    src_type = src.get("source")
    if src_type == "github":
        cache = home / ".claude" / "plugins" / "marketplaces" / _MARKETPLACE_NAME
        if cache.exists():
            print(f"✓ marketplace cached at {cache}")
        else:
            print(f"✗ source=github but no cache at {cache} — try restarting claude")
            rc = 1
    elif src_type == "directory":
        path = Path(src.get("path", ""))
        if path.exists():
            print(f"✓ source=directory: {path} (CC reads directly, no cache)")
        else:
            print(f"✗ source=directory points at {path} which does not exist")
            rc = 1
    else:
        print(f"? unknown marketplace source type: {src_type}")

    # Detect cache-staleness (#35-shape): marketplace clone version vs cache.
    plugin_cache_root = home / ".claude" / "plugins" / "cache" / _MARKETPLACE_NAME / "evo"
    mkt_manifest = (
        home / ".claude" / "plugins" / "marketplaces" / _MARKETPLACE_NAME
        / "plugins" / "evo" / ".claude-plugin" / "plugin.json"
    )
    if plugin_cache_root.exists() and mkt_manifest.exists():
        installed_versions = sorted(
            p.name for p in plugin_cache_root.iterdir() if p.is_dir()
        )
        if installed_versions:
            installed = installed_versions[-1]
            try:
                mkt = json.loads(mkt_manifest.read_text())
                mkt_ver = mkt.get("version")
            except (OSError, json.JSONDecodeError):
                mkt_ver = None
            if mkt_ver and mkt_ver != installed:
                print(
                    f"✗ cache stale: installed={installed}, marketplace={mkt_ver}\n"
                    f"  Run: evo update claude-code --force"
                )
                rc = 1
            else:
                print(f"✓ cache up to date ({installed})")
    return rc

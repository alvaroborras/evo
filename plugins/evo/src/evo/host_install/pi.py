"""Pi install — thin wrapper around `pi install npm:@evo-hq/pi-evo`.

Pi (pi.dev, ``@earendil-works/pi-coding-agent``) installs packages via
its own marketplace mechanism. The evo package is published to npm
as ``@evo-hq/pi-evo`` with a ``pi`` manifest pointing at the bundled
extension and four skills, plus ``pi-subagents`` as a bundled dep so
parallel rounds work out of the box.

Resolution order for the install spec:
  1. ``--from-path <dir>`` → ``pi install <dir>/plugins/evo/npm``
     (live tests, manual installs from a clone)
  2. ``--version <X>`` → ``pi install npm:@evo-hq/pi-evo@<X>``
     (pin to a specific release or to ``alpha`` dist-tag)
  3. default → ``pi install npm:@evo-hq/pi-evo``
     (resolves to the ``latest`` dist-tag)

Migration: legacy file-copy installs (0.4.2-alpha.{1,2}) put files
under ``~/.pi/agent/extensions/evo/`` and ``~/.pi/agent/skills/evo-*/``.
Those still work — pi auto-discovers them — but they conflict with the
npm-installed package's skills. Clean them up on install.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path


_NPM_PACKAGE = "@evo-hq/pi-evo"
_LEGACY_SKILL_DIRS = ("evo-discover", "evo-optimize", "evo-subagent", "evo-infra-setup")


def _pi_base() -> Path:
    home_override = os.environ.get("PI_HOME")
    return Path(home_override) if home_override else Path.home() / ".pi"


def _pi_settings_file() -> Path:
    return _pi_base() / "agent" / "settings.json"


def _legacy_extension_path() -> Path:
    """The pre-alpha.3 file-copy location for the bundled JS extension."""
    return _pi_base() / "agent" / "extensions" / "evo" / "index.js"


def _legacy_skills_dir() -> Path:
    return _pi_base() / "agent" / "skills"


def _pi_bin_or_error() -> str | None:
    pi = shutil.which("pi")
    if pi is None:
        print(
            "ERROR: `pi` binary not on PATH. Install pi first:\n"
            "  npm install -g @earendil-works/pi-coding-agent",
            file=sys.stderr,
        )
    return pi


def _resolve_install_spec(from_path: str | None, version: str | None) -> str:
    """Map the install args to the `pi install <spec>` argument.

    `--from-path` wins over `--version` (used by live tests with a
    local repo tarball; the bundle/skills are sync'd into
    ``<from-path>/plugins/evo/npm`` by CI before publish).
    """
    if from_path:
        return str(Path(from_path) / "plugins" / "evo" / "npm")
    if version:
        return f"npm:{_NPM_PACKAGE}@{version}"
    return f"npm:{_NPM_PACKAGE}"


def _migrate_legacy_file_copy() -> None:
    """Remove pre-alpha.3 file-copy artifacts.

    0.4.2-alpha.1 and alpha.2 installed via direct file-copy into
    ``~/.pi/agent/extensions/evo/`` and ``~/.pi/agent/skills/evo-*/``.
    Now that ``@evo-hq/pi-evo`` is the canonical source, those files
    shadow (and may collide with) the npm-installed skills.

    Removes:
      - the extension path entry from settings.json#extensions[]
      - ``~/.pi/agent/extensions/evo/`` (file + dir)
      - ``~/.pi/agent/skills/evo-{discover,optimize,subagent,infra-setup}/``

    Does NOT touch the npm-installed package, ``pi-subagents``, or
    any unrelated skills/extensions.
    """
    legacy_ext = _legacy_extension_path()
    settings = _pi_settings_file()
    if settings.exists():
        try:
            data = json.loads(settings.read_text())
            extensions = data.get("extensions", []) or []
            target = str(legacy_ext)
            if target in extensions:
                extensions.remove(target)
                settings.write_text(json.dumps(data, indent=2) + "\n")
                print(f"  migrated: removed {target} from {settings}")
        except json.JSONDecodeError:
            pass

    if legacy_ext.exists():
        legacy_ext.unlink()
        print(f"  migrated: removed {legacy_ext}")
    if legacy_ext.parent.exists() and not any(legacy_ext.parent.iterdir()):
        legacy_ext.parent.rmdir()

    skills_root = _legacy_skills_dir()
    for name in _LEGACY_SKILL_DIRS:
        d = skills_root / name
        if d.exists():
            shutil.rmtree(d)
            print(f"  migrated: removed {d}")


def install(args: argparse.Namespace) -> int:
    pi = _pi_bin_or_error()
    if pi is None:
        return 2

    print("Cleaning up any pre-alpha.3 file-copy install ...")
    _migrate_legacy_file_copy()

    spec = _resolve_install_spec(
        getattr(args, "from_path", None),
        getattr(args, "version", None),
    )
    cmd = [pi, "install", spec]
    print(f"$ {' '.join(cmd)}")
    rc = subprocess.call(cmd)
    if rc != 0:
        return rc

    print(
        f"\n✓ {_NPM_PACKAGE} installed via pi.\n"
        "  Restart any running pi session to load the extension and skills."
    )
    return 0


def update(args: argparse.Namespace) -> int:
    """Update the pi-installed evo package + run the legacy-file-copy
    migration. ``pi install`` is idempotent + version-aware, so install
    and update share the same path. Without explicit ``--version``,
    pi resolves to the ``latest`` dist-tag.
    """
    return install(args)


def uninstall(args: argparse.Namespace) -> int:
    # Always sweep legacy file-copy artifacts (no-op if absent).
    _migrate_legacy_file_copy()

    pi = shutil.which("pi")
    if pi is None:
        # No pi binary → nothing more to remove from pi's own registry.
        # Legacy cleanup above already handled the file-copy case.
        return 0

    # Pi tracks packages by full spec (e.g. `npm:@evo-hq/pi-evo`) — the
    # short name alone returns "No matching package found".
    spec = f"npm:{_NPM_PACKAGE}"
    cmd = [pi, "uninstall", spec]
    print(f"$ {' '.join(cmd)}")
    rc = subprocess.call(cmd)
    # `pi uninstall` returns non-zero when the package isn't installed.
    # Treat that as success — caller's goal (package gone) is satisfied.
    if rc != 0:
        print(f"({spec} was not installed via pi; nothing to remove)")
    return 0


def doctor(args: argparse.Namespace) -> int:
    rc = 0

    pi = shutil.which("pi")
    if pi is None:
        print("✗ `pi` binary not on PATH")
        print("  Install: npm install -g @earendil-works/pi-coding-agent")
        return 1
    print(f"✓ pi binary: {pi}")

    settings = _pi_settings_file()
    if not settings.exists():
        print(f"✗ {settings} not found (pi may not have run yet)")
        return 1

    try:
        data = json.loads(settings.read_text())
    except json.JSONDecodeError as exc:
        print(f"✗ could not parse {settings}: {exc}")
        return 1

    packages = data.get("packages", []) or []
    has_pi_evo = any(_NPM_PACKAGE in p for p in packages)
    if has_pi_evo:
        print(f"✓ {_NPM_PACKAGE} installed (in {settings}#packages[])")
    else:
        print(f"✗ {_NPM_PACKAGE} not installed")
        print("  Run: evo install pi")
        rc = 1

    has_subagents = any("pi-subagents" in p for p in packages)
    if has_subagents:
        print("✓ pi-subagents installed (parallel-subagent provider)")
    else:
        # `@evo-hq/pi-evo` bundles pi-subagents as a dependency, so this
        # should be present after a successful install. Flag if missing.
        print("✗ pi-subagents not detected — `@evo-hq/pi-evo` should bundle it")
        rc = 1

    # Warn (don't fail) if legacy artifacts remain — install/update auto-sweeps.
    legacy_ext = _legacy_extension_path()
    if legacy_ext.exists():
        print(f"? legacy file-copy extension still at {legacy_ext} "
              f"(re-run `evo install pi` to migrate)")
    legacy_skills = _legacy_skills_dir()
    legacy_present = [d for d in _LEGACY_SKILL_DIRS
                      if (legacy_skills / d).exists()]
    if legacy_present:
        print(f"? legacy file-copy skills still present: {', '.join(legacy_present)} "
              f"(re-run `evo install pi` to migrate)")
    return rc

"""Fetch the platform-native evo-hook-drain binary from the GitHub release.

The hook script is a small Rust binary (~330 KB) built natively for each
platform in CI. Binaries ship as GitHub Release assets, NOT in the plugin
git tree (keeps the repo small + lets users install from `main` without
binaries present). Every host's `install(args)` calls
`ensure_hook_drain_binary(plugin_root)` to populate the plugin's bin/
directory before hooks.json's command path is exercised.

Idempotent: re-fetch only when the on-disk binary is missing or doesn't
match the installed evo-hq-cli version.
"""
from __future__ import annotations

import os
import platform
import sys
import urllib.error
import urllib.request
from pathlib import Path

from .. import __version__ as EVO_VERSION


_RELEASE_URL_TEMPLATE = (
    "https://github.com/evo-hq/evo/releases/download/v{version}/{asset}"
)


def _target_name() -> str | None:
    """Map platform.system()/machine() to the release asset suffix.

    Returns 'linux-amd64', 'linux-arm64', 'darwin' (universal: arm64 +
    x86_64 fused via lipo, so one asset works on both), or
    'windows-amd64'. None if the platform isn't supported (e.g.
    windows-arm64 — we don't ship that binary yet).
    """
    system = platform.system().lower()

    if system == "darwin":
        # Single universal binary for both Apple Silicon and Intel Macs.
        # macOS picks the right slice at exec time. Drops the arch suffix.
        return "darwin"

    if system not in ("linux", "windows"):
        return None

    machine = platform.machine().lower()
    if machine in ("x86_64", "amd64"):
        arch = "amd64"
    elif machine in ("arm64", "aarch64"):
        arch = "arm64"
    else:
        return None

    if system == "windows" and arch == "arm64":
        # Not built in CI yet — windows-arm64 runners are still in preview.
        return None

    return f"{system}-{arch}"


def _release_version_tag(version: str) -> str:
    """Translate a PEP-440 version (`0.4.1a2`) to the git tag form
    (`0.4.1-alpha.2`) used in release asset URLs.

    The version files store the git-tag form already; if someone calls
    with the PEP-440 form, normalise here.
    """
    if "a" in version and "alpha" not in version:
        head, _, tail = version.partition("a")
        return f"{head}-alpha.{tail}"
    if "b" in version and "beta" not in version:
        head, _, tail = version.partition("b")
        return f"{head}-beta.{tail}"
    if "rc" in version and "-rc" not in version:
        head, _, tail = version.partition("rc")
        return f"{head}-rc.{tail}"
    return version


def ensure_hook_drain_binary(plugin_root: Path, *, force: bool = False) -> bool:
    """Stage the evo-hook-drain binary into `<plugin_root>/bin/` if it's
    not already there. Returns True on success (file is now present and
    executable), False otherwise.

    Sources, in order of precedence:
      1. `EVO_HOOK_DRAIN_BINARY` env var — points to a local file. Used
         by tests / local-source smoke runs to bypass the GitHub
         release URL when there's no published release to fetch from.
         The file gets copied (not symlinked) so it stays valid after
         the source disappears.
      2. GitHub release asset for this evo-hq-cli version. Fetched via
         urllib (stdlib only).

    Non-fatal: a failed fetch prints a warning to stderr but doesn't
    raise. Hooks will fail at fire-time with a clearer error pointing
    the user at `evo install <host> --force` to retry.
    """
    target = _target_name()
    if target is None:
        print(
            f"WARN: evo-hook-drain binary not available for "
            f"{platform.system().lower()}-{platform.machine().lower()}. "
            f"Mid-run inject via `evo direct` will not work on this platform.",
            file=sys.stderr,
        )
        return False

    is_windows = platform.system().lower() == "windows"
    ext = ".exe" if is_windows else ""
    asset = f"evo-hook-drain-{target}{ext}"
    dest = plugin_root / "bin" / f"evo-hook-drain{ext}"

    if dest.exists() and not force:
        # Already present. Trust it — re-fetching every install would be
        # wasteful and might fail if the user is offline.
        return True

    dest.parent.mkdir(parents=True, exist_ok=True)

    # Env-var bypass for local-source / test contexts where no published
    # release exists yet. The test harness builds the binary, sets
    # EVO_HOOK_DRAIN_BINARY=<path>, then runs `evo install <host>` and
    # gets the local file copied in instead of a 404 from urllib.
    local_override = os.environ.get("EVO_HOOK_DRAIN_BINARY")
    if local_override:
        import shutil
        src = Path(local_override)
        if not src.is_file():
            print(
                f"WARN: EVO_HOOK_DRAIN_BINARY={local_override} does not "
                f"point at a regular file. Falling back to GitHub release fetch.",
                file=sys.stderr,
            )
        else:
            try:
                shutil.copyfile(src, dest)
                if not is_windows:
                    os.chmod(dest, 0o755)
                print(f"installed evo-hook-drain binary from EVO_HOOK_DRAIN_BINARY: {dest}")
                return True
            except OSError as e:
                print(
                    f"WARN: failed to copy EVO_HOOK_DRAIN_BINARY={src} to {dest}: {e}. "
                    f"Falling back to GitHub release fetch.",
                    file=sys.stderr,
                )

    version_tag = _release_version_tag(EVO_VERSION)
    url = _RELEASE_URL_TEMPLATE.format(version=version_tag, asset=asset)

    try:
        print(f"$ fetching {asset} from {url}")
        urllib.request.urlretrieve(url, str(dest))
    except (urllib.error.URLError, OSError) as e:
        print(
            f"WARN: failed to fetch evo-hook-drain binary from {url}: {e}\n"
            f"      Mid-run inject (`evo direct`) will not work until the "
            f"binary is staged at {dest}. Re-run `evo install <host> --force` "
            f"with network access to retry.",
            file=sys.stderr,
        )
        return False

    if not is_windows:
        try:
            os.chmod(dest, 0o755)
        except OSError:
            pass

    print(f"installed evo-hook-drain binary: {dest}")
    return True

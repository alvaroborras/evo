"""Stage the platform-native evo-hook-drain binary where hooks resolve it.

The hook script is a small Rust binary (~330 KB) built natively for each
platform in CI. Binaries ship as GitHub Release assets, NOT in the plugin
git tree (keeps the repo small + lets users install from `main` without
binaries present). Every host's `install(args)` calls
`ensure_hook_drain_binary(plugin_root)`, which:

  1. fetches the binary into the stable per-user location
     `$EVO_HOME/bin/` (default `~/.evo/bin/`), outside any
     host-managed directory, and
  2. copies it to `<plugin_root>/bin/evo-hook-drain`, the path
     hooks.json commands resolve.

The plugin tree commits a shell-script fallback at `bin/evo-hook-drain`
that execs the stable copy. Hosts re-stage their plugin cache from a
fresh git snapshot whenever they decide to (codex re-clones its
marketplace snapshot at session start), which drops anything evo staged
into those directories; the tracked wrapper survives every re-stage and
keeps hooks firing via the stable copy.

Idempotent: re-fetch only when the stable copy is missing, was staged by
a different evo-hq-cli version, or `force` is set.
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
_LATEST_RELEASE_URL_TEMPLATE = (
    "https://github.com/evo-hq/evo/releases/latest/download/{asset}"
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


def hook_drain_binary_name() -> str:
    """Filename of the staged binary for the current platform."""
    return "evo-hook-drain.exe" if platform.system().lower() == "windows" else "evo-hook-drain"


def stable_binary_path() -> Path:
    """Host-independent home for the fetched binary:
    `$EVO_HOME/bin/<name>` (default `~/.evo/bin/<name>`). The committed
    `bin/evo-hook-drain` wrapper in the plugin tree execs this copy.
    """
    override = os.environ.get("EVO_HOME")
    base = Path(override) if override else Path.home() / ".evo"
    return base / "bin" / hook_drain_binary_name()


def is_wrapper_script(path: Path) -> bool:
    """True when `path` holds the committed shell-script fallback rather
    than a platform-native binary."""
    try:
        with open(path, "rb") as fh:
            return fh.read(2) == b"#!"
    except OSError:
        return False


def mirror_hook_drain_binary(src_plugin_root: Path, dst_plugin_root: Path) -> bool:
    """Copy `bin/evo-hook-drain` from one plugin root to another, so a
    host re-stage from the destination (codex: marketplace snapshot;
    claude-code: marketplace clone) carries the native binary instead of
    falling back to the wrapper script.

    Best-effort: the host may wipe the destination on its next snapshot
    refresh, at which point the tracked wrapper takes over. No-op when
    src and dst resolve to the same directory (--from-path installs
    stage directly into the source tree). Returns True when the file is
    present at the destination afterwards.
    """
    import shutil

    name = hook_drain_binary_name()
    src = src_plugin_root / "bin" / name
    dst = dst_plugin_root / "bin" / name
    if not src.is_file():
        return False
    try:
        if src.resolve() == dst.resolve():
            return True
    except OSError:
        pass
    try:
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src, dst)
        if platform.system().lower() != "windows":
            os.chmod(dst, 0o755)
    except OSError as e:
        print(
            f"WARN: failed to mirror evo-hook-drain into {dst.parent}: {e}",
            file=sys.stderr,
        )
        return False
    return True


def _stage_stable_copy(asset: str, *, force: bool) -> bool:
    """Ensure the stable copy at `stable_binary_path()` exists and was
    staged by this CLI version. Returns True when a usable stable copy
    is present afterwards (even a version-stale one, if re-fetch fails:
    the binary's wire protocol is stable across minor versions).

    Sources, in order of precedence:
      1. `EVO_HOOK_DRAIN_BINARY` env var: points to a local file. Used
         by tests / local-source smoke runs to bypass the GitHub
         release URL when there's no published release to fetch from.
         The file gets copied (not symlinked) so it stays valid after
         the source disappears.
      2. GitHub release asset for this evo-hq-cli version, falling back
         to /releases/latest/. Fetched via urllib (stdlib only).
    """
    stable = stable_binary_path()
    sidecar = stable.parent / "evo-hook-drain.version"
    is_windows = platform.system().lower() == "windows"

    staged_by = sidecar.read_text().strip() if sidecar.exists() else None
    if stable.exists() and not force and staged_by == EVO_VERSION:
        return True

    stable.parent.mkdir(parents=True, exist_ok=True)

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
                shutil.copyfile(src, stable)
                if not is_windows:
                    os.chmod(stable, 0o755)
                sidecar.write_text(EVO_VERSION + "\n")
                print(f"installed evo-hook-drain binary from EVO_HOOK_DRAIN_BINARY: {stable}")
                return True
            except OSError as e:
                print(
                    f"WARN: failed to copy EVO_HOOK_DRAIN_BINARY={src} to {stable}: {e}. "
                    f"Falling back to GitHub release fetch.",
                    file=sys.stderr,
                )

    version_tag = _release_version_tag(EVO_VERSION)
    versioned_url = _RELEASE_URL_TEMPLATE.format(version=version_tag, asset=asset)
    latest_url = _LATEST_RELEASE_URL_TEMPLATE.format(asset=asset)

    # Try the exact-version release first. Pre-release / alpha versions often
    # don't have a corresponding GitHub Release tagged yet (release builds
    # are only cut at stable bumps), so a 404 here is expected during alpha
    # cycles. Fall back to /releases/latest/, which GitHub redirects to the
    # most recent stable release.
    last_err: Exception | None = None
    for url in (versioned_url, latest_url):
        try:
            print(f"$ fetching {asset} from {url}")
            urllib.request.urlretrieve(url, str(stable))
            if url == latest_url and url != versioned_url:
                print(
                    f"NOTE: used /releases/latest/ fallback because the "
                    f"version-tagged release v{version_tag} doesn't exist on "
                    f"GitHub yet. Binary is wire-compatible across minor "
                    f"versions; mid-run inject will work.",
                )
            break
        except (urllib.error.URLError, OSError) as e:
            last_err = e
            # urlretrieve on 404 raises urllib.error.HTTPError (subclass of URLError).
            # Continue to the next URL in the fallback chain.
            continue
    else:
        if stable.exists():
            print(
                f"WARN: could not refresh evo-hook-drain "
                f"(tried {versioned_url} and {latest_url}): {last_err}\n"
                f"      Keeping the existing copy at {stable} "
                f"(wire-compatible across minor versions).",
                file=sys.stderr,
            )
            return True
        print(
            f"WARN: failed to fetch evo-hook-drain binary "
            f"(tried {versioned_url} and {latest_url}): {last_err}\n"
            f"      Mid-run inject (`evo direct`) will not work until the "
            f"binary is staged at {stable}. Re-run `evo install <host> --force` "
            f"with network access to retry.",
            file=sys.stderr,
        )
        return False

    if not is_windows:
        try:
            os.chmod(stable, 0o755)
        except OSError:
            pass
    sidecar.write_text(EVO_VERSION + "\n")
    print(f"installed evo-hook-drain binary: {stable}")
    return True


def ensure_hook_drain_binary(plugin_root: Path, *, force: bool = False,
                             overwrite_wrapper: bool = True) -> bool:
    """Stage the binary at the stable location and make
    `<plugin_root>/bin/evo-hook-drain` a working hook entry point.
    Returns True when hooks at `plugin_root` will fire.

    `overwrite_wrapper=False` leaves a committed wrapper script in place
    at the destination (used for --from-path installs, where the
    destination is the user's source tree and replacing the tracked
    wrapper with a binary would dirty their checkout); the wrapper execs
    the stable copy, so hooks still work.

    Non-fatal: a failed fetch prints a warning to stderr but doesn't
    raise.
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

    stable_ok = _stage_stable_copy(asset, force=force)
    stable = stable_binary_path()
    dest = plugin_root / "bin" / hook_drain_binary_name()
    dest.parent.mkdir(parents=True, exist_ok=True)

    dest_exists = dest.exists()
    dest_is_wrapper = dest_exists and is_wrapper_script(dest)
    should_copy = stable_ok and (
        not dest_exists
        or (dest_is_wrapper and overwrite_wrapper)
        or (not dest_is_wrapper and force)
    )
    if should_copy:
        import shutil
        try:
            shutil.copyfile(stable, dest)
            if not is_windows:
                os.chmod(dest, 0o755)
            print(f"staged evo-hook-drain binary: {dest}")
        except OSError as e:
            print(
                f"WARN: failed to stage evo-hook-drain at {dest}: {e}",
                file=sys.stderr,
            )

    if not dest.exists():
        return False
    if is_wrapper_script(dest) and not stable_ok:
        print(
            f"WARN: {dest} is the fallback wrapper and no binary is staged "
            f"at {stable}, so hooks will no-op. Re-run "
            f"`evo install <host> --force` with network access.",
            file=sys.stderr,
        )
        return False
    return True

"""Daily PyPI freshness check and legacy-install detection.

Both checks run at most once per 24h (cached in ~/.cache/evo/version-check.json)
and are silent on any failure. They surface a single line on stderr when
there's something for the user to act on.

Both are skippable via the EVO_SKIP_VERSION_CHECK env var (set in CI,
sandboxes, anywhere network noise is unwanted).

The PyPI check has a 2-second hard timeout so a slow PyPI never blocks
the user's command.
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

_CACHE_TTL_SECONDS = 86400  # 24 hours
_HTTP_TIMEOUT_SECONDS = 2
_PYPI_URL = "https://pypi.org/pypi/evo-hq-cli/json"


def _cache_path() -> Path:
    base = Path(os.environ.get("XDG_CACHE_HOME", str(Path.home() / ".cache")))
    return base / "evo" / "version-check.json"


def _read_cache() -> dict | None:
    p = _cache_path()
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text())
    except (OSError, json.JSONDecodeError):
        return None


def _write_cache(data: dict) -> None:
    p = _cache_path()
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(data))
    except OSError:
        pass  # cache failures are non-fatal


def _parse_version(v: str) -> tuple:
    """Split 'X.Y.Z[-suffix]' into a tuple of ints for comparison.

    Tolerates pre-release suffixes (a1, b2, rc1, -alpha.5) by stripping
    everything from the first non-digit run in each segment. Good enough
    for ordering stable releases — pre-releases compare as their stable
    base (0.4.0a5 == 0.4.0), which is the right default for "is there a
    newer version" prompts.
    """
    if not v:
        return ()
    clean = v.split("-")[0].split("+")[0]
    parts = []
    for segment in clean.split("."):
        digits = ""
        for c in segment:
            if c.isdigit():
                digits += c
            else:
                break
        parts.append(int(digits) if digits else 0)
    return tuple(parts)


def _is_newer(latest: str, running: str) -> bool:
    return _parse_version(latest) > _parse_version(running)


def _fetch_pypi_latest() -> str | None:
    """Return latest evo-hq-cli version from PyPI, or None on any failure.

    Uses urllib (stdlib) so this works even if `requests` somehow isn't
    available (it's a declared dep, but defensive coding is cheap here).
    """
    try:
        import urllib.request
        req = urllib.request.Request(
            _PYPI_URL,
            headers={"Accept": "application/json", "User-Agent": "evo-hq-cli/version-check"},
        )
        with urllib.request.urlopen(req, timeout=_HTTP_TIMEOUT_SECONDS) as resp:
            data = json.loads(resp.read())
        return data.get("info", {}).get("version")
    except Exception:
        return None


def maybe_check_pypi(running_version: str) -> None:
    """Print a one-line nudge to stderr if a newer evo-hq-cli is on PyPI.

    Caches the result for 24h. Silent on network failure. No-op if
    EVO_SKIP_VERSION_CHECK is set.
    """
    if os.environ.get("EVO_SKIP_VERSION_CHECK"):
        return

    cached = _read_cache()
    now = int(time.time())
    latest = None
    if cached and cached.get("pypi_checked_at", 0) + _CACHE_TTL_SECONDS > now:
        latest = cached.get("pypi_latest")
    else:
        latest = _fetch_pypi_latest()
        if latest is None:
            return  # network failed; stay silent, retry tomorrow
        merged = dict(cached or {})
        merged["pypi_latest"] = latest
        merged["pypi_checked_at"] = now
        _write_cache(merged)

    if latest and _is_newer(latest, running_version):
        print(
            f"evo: v{latest} available (you have v{running_version}). Run: evo update",
            file=sys.stderr,
        )


# Known legacy install paths from versions ≤ 0.4.0. Each entry is a path
# whose presence indicates the user has a pre-0.4.1 install that's
# affected by #36 or #35.
_LEGACY_PATHS = [
    # Pre-0.4.1 Claude Code cache (any sub-version) — only flag if the
    # cache has unpatched evo-hook-drain (no SessionStart drift block).
    Path.home() / ".claude" / "plugins" / "cache" / "evo-hq-evo" / "evo",
    # Codex pre-rename marketplace (was evo-hq-evo, renamed to evo-hq in 0.4.0)
    Path.home() / ".codex" / "plugins" / "cache" / "evo-hq-evo",
]


def _has_unpatched_hook(plugin_root: Path) -> bool:
    """Return True if the cached evo-hook-drain at this plugin_root is
    affected by #36 (bare `exec evo-drain` with no SessionStart drift
    check). Heuristic: grep for the post-fix sentinel string.
    """
    hook = plugin_root / "bin" / "evo-hook-drain"
    if not hook.exists():
        return False
    try:
        text = hook.read_text()
    except OSError:
        return False
    # Post-0.4.1 hooks always contain the SessionStart drift block; if
    # it's absent the install predates the fix.
    return "SessionStart drift checks" not in text


def maybe_detect_legacy() -> None:
    """Print a one-line nudge if legacy v0.4.0-or-earlier installs are
    detected on disk. Cached 24h to avoid spamming.
    """
    if os.environ.get("EVO_SKIP_VERSION_CHECK"):
        return

    cached = _read_cache()
    now = int(time.time())
    if cached and cached.get("legacy_checked_at", 0) + _CACHE_TTL_SECONDS > now:
        return  # already checked today

    found = []
    # Special-case Claude Code cache: only flag versions whose bin/evo-hook-drain
    # is unpatched. A correctly-updated 0.4.1+ install lives in the same path.
    cc_cache = Path.home() / ".claude" / "plugins" / "cache" / "evo-hq-evo" / "evo"
    if cc_cache.exists():
        for version_dir in cc_cache.iterdir():
            if version_dir.is_dir() and _has_unpatched_hook(version_dir):
                found.append(version_dir)

    # Other legacy paths: just check existence.
    codex_legacy = Path.home() / ".codex" / "plugins" / "cache" / "evo-hq-evo"
    if codex_legacy.exists():
        found.append(codex_legacy)

    merged = dict(cached or {})
    merged["legacy_checked_at"] = now
    _write_cache(merged)

    if found:
        print(
            f"evo: {len(found)} legacy install(s) detected. Run: evo update --force",
            file=sys.stderr,
        )

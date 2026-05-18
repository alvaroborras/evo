"""Unit tests for evo.version_check — PyPI freshness check + legacy detection.

All tests mock the network. No HTTP traffic, all sub-millisecond.
"""

from __future__ import annotations

import json
import time
from unittest import mock

import pytest

from evo import version_check


@pytest.fixture
def fake_cache(tmp_path, monkeypatch):
    """Isolate the version-check cache to a tmp dir."""
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    monkeypatch.delenv("EVO_SKIP_VERSION_CHECK", raising=False)
    return tmp_path / "evo" / "version-check.json"


def test_parse_version_basic():
    assert version_check._parse_version("0.4.1") == (0, 4, 1)
    assert version_check._parse_version("1.0.0") == (1, 0, 0)


def test_parse_version_handles_prerelease_suffix():
    # 0.4.0a5 and 0.4.0-alpha.5 both compare as 0.4.0 base.
    assert version_check._parse_version("0.4.0a5") == (0, 4, 0)
    assert version_check._parse_version("0.4.0-alpha.5") == (0, 4, 0)


def test_parse_version_handles_double_digit_segments():
    # Regression guard: lexicographic string compare would mis-order these.
    assert version_check._is_newer("0.4.10", "0.4.2") is True
    assert version_check._is_newer("0.4.2", "0.4.10") is False


def test_is_newer_equal_returns_false():
    assert version_check._is_newer("0.4.0", "0.4.0") is False


def test_maybe_check_pypi_fetches_on_first_call(fake_cache, capsys):
    with mock.patch.object(version_check, "_fetch_pypi_latest", return_value="0.4.1") as m:
        version_check.maybe_check_pypi("0.4.0")
    assert m.call_count == 1
    err = capsys.readouterr().err
    assert "v0.4.1 available" in err
    assert "evo update" in err
    assert fake_cache.exists()


def test_maybe_check_pypi_uses_cache_within_24h(fake_cache, capsys):
    # First call: fetches, writes cache
    with mock.patch.object(version_check, "_fetch_pypi_latest", return_value="0.4.1"):
        version_check.maybe_check_pypi("0.4.0")
    capsys.readouterr()  # drain
    # Second call: should hit cache, no fetch
    with mock.patch.object(version_check, "_fetch_pypi_latest", return_value="DIFFERENT") as m:
        version_check.maybe_check_pypi("0.4.0")
    assert m.call_count == 0
    # But should still emit the same nudge (cached version is 0.4.1, running is 0.4.0)
    err = capsys.readouterr().err
    assert "v0.4.1 available" in err


def test_maybe_check_pypi_refreshes_after_ttl(fake_cache, capsys):
    # Stage stale cache (>24h old)
    fake_cache.parent.mkdir(parents=True, exist_ok=True)
    fake_cache.write_text(json.dumps({
        "pypi_latest": "0.4.0",
        "pypi_checked_at": int(time.time()) - 90000,  # >24h ago
    }))
    with mock.patch.object(version_check, "_fetch_pypi_latest", return_value="0.5.0") as m:
        version_check.maybe_check_pypi("0.4.0")
    assert m.call_count == 1  # re-fetched
    assert "v0.5.0 available" in capsys.readouterr().err


def test_maybe_check_pypi_silent_on_network_failure(fake_cache, capsys):
    with mock.patch.object(version_check, "_fetch_pypi_latest", return_value=None):
        version_check.maybe_check_pypi("0.4.0")  # must not raise
    assert capsys.readouterr().err == ""


def test_maybe_check_pypi_silent_when_up_to_date(fake_cache, capsys):
    with mock.patch.object(version_check, "_fetch_pypi_latest", return_value="0.4.0"):
        version_check.maybe_check_pypi("0.4.0")
    assert capsys.readouterr().err == ""


def test_maybe_check_pypi_skipped_by_env_var(fake_cache, monkeypatch, capsys):
    monkeypatch.setenv("EVO_SKIP_VERSION_CHECK", "1")
    with mock.patch.object(version_check, "_fetch_pypi_latest", return_value="0.4.1") as m:
        version_check.maybe_check_pypi("0.4.0")
    assert m.call_count == 0
    assert capsys.readouterr().err == ""


def test_maybe_detect_legacy_skipped_by_env_var(fake_cache, monkeypatch, capsys, tmp_path):
    monkeypatch.setenv("EVO_SKIP_VERSION_CHECK", "1")
    # Even with a legacy path present, env var skips the check
    monkeypatch.setattr(version_check, "Path", type(tmp_path))
    version_check.maybe_detect_legacy()
    assert capsys.readouterr().err == ""


def test_maybe_detect_legacy_silent_when_nothing_legacy(fake_cache, monkeypatch, capsys, tmp_path):
    # Point Home to empty tmp_path — no legacy dirs exist there
    monkeypatch.setenv("HOME", str(tmp_path))
    version_check.maybe_detect_legacy()
    assert capsys.readouterr().err == ""


def test_has_unpatched_hook_detects_old_script(tmp_path):
    plugin_root = tmp_path / "evo-fake-install"
    bin_dir = plugin_root / "bin"
    bin_dir.mkdir(parents=True)
    # Old-style hook: no SessionStart drift block sentinel
    (bin_dir / "evo-hook-drain").write_text("#!/bin/bash\nexec evo-drain $@\n")
    assert version_check._has_unpatched_hook(plugin_root) is True


def test_has_unpatched_hook_recognizes_new_script(tmp_path):
    plugin_root = tmp_path / "evo-fake-install"
    bin_dir = plugin_root / "bin"
    bin_dir.mkdir(parents=True)
    # New-style hook contains the sentinel
    (bin_dir / "evo-hook-drain").write_text(
        "#!/bin/bash\n# 4a. SessionStart drift checks\necho test\n"
    )
    assert version_check._has_unpatched_hook(plugin_root) is False


def test_has_unpatched_hook_returns_false_when_missing(tmp_path):
    assert version_check._has_unpatched_hook(tmp_path / "nonexistent") is False

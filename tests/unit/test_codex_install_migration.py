"""Tests for codex installer's legacy-marketplace cleanup.

Backstory: codex.py pre-fix used different marketplace names based on
install mode — `evo-hq` (the GitHub owner) for PyPI mode and
`evo-hq-evo` (marketplace.json's `name` field) for `--from-path` mode.
Users hitting both modes ended up with two parallel `[plugins."evo@<X>"]`
registrations. Both fired hooks; the older one failed exit 127 because
its cache dir never had the binary staged.

The fix:
  1. Always read marketplace.json's `name` field as the canonical mkt
     name (no mode-dependent branching).
  2. On every install, scan config.toml for `evo@<other>` entries that
     don't match the target name and remove them + their cache dirs.

These tests exercise (2) — synthetic stale configs that mirror real
breakage. Cleanup is text-based regex on TOML lines (avoiding a TOML
parser dep) so the tests check both that bad entries go AND that
unrelated entries stay.
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "plugins" / "evo" / "src"))


class _Base(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.codex_home = Path(self._tmp.name) / ".codex"
        self.codex_home.mkdir(parents=True)
        self.cfg_path = self.codex_home / "config.toml"
        self._prev_codex_home = os.environ.get("CODEX_HOME")
        os.environ["CODEX_HOME"] = str(self.codex_home)

    def tearDown(self):
        if self._prev_codex_home is None:
            os.environ.pop("CODEX_HOME", None)
        else:
            os.environ["CODEX_HOME"] = self._prev_codex_home
        self._tmp.cleanup()

    def _write_config(self, body: str) -> None:
        self.cfg_path.write_text(body)

    def _read_config(self) -> str:
        return self.cfg_path.read_text()

    def _make_cache(self, mkt: str, version: str = "0.4.3") -> Path:
        d = self.codex_home / "plugins" / "cache" / mkt / "evo" / version
        d.mkdir(parents=True)
        return d


class TestLegacyCleanup(_Base):

    def test_removes_stale_evo_hq_plugin_block(self):
        from evo.host_install.codex import _cleanup_legacy_codex_registrations

        self._write_config(
            '[plugins."evo@evo-hq"]\n'
            'enabled = true\n'
            '\n'
            '[plugins."evo@evo-hq-evo"]\n'
            'enabled = true\n'
        )
        _cleanup_legacy_codex_registrations(target_mkt="evo-hq-evo")
        text = self._read_config()
        self.assertNotIn('"evo@evo-hq"', text)
        self.assertIn('"evo@evo-hq-evo"', text)

    def test_removes_stale_marketplace_block(self):
        from evo.host_install.codex import _cleanup_legacy_codex_registrations

        self._write_config(
            '[plugins."evo@evo-hq"]\n'
            'enabled = true\n'
            '\n'
            '[plugins."evo@evo-hq-evo"]\n'
            'enabled = true\n'
            '\n'
            '[marketplaces.evo-hq]\n'
            'source = "https://github.com/evo-hq/evo.git"\n'
        )
        _cleanup_legacy_codex_registrations(target_mkt="evo-hq-evo")
        text = self._read_config()
        self.assertNotIn("[marketplaces.evo-hq]", text)

    def test_removes_stale_hook_state_entries(self):
        from evo.host_install.codex import _cleanup_legacy_codex_registrations

        self._write_config(
            '[plugins."evo@evo-hq"]\n'
            'enabled = true\n'
            '\n'
            '[plugins."evo@evo-hq-evo"]\n'
            'enabled = true\n'
            '\n'
            '[hooks.state."evo@evo-hq:hooks/hooks.json:pre_tool_use:0:0"]\n'
            'trusted_hash = "sha256:abc"\n'
            '\n'
            '[hooks.state."evo@evo-hq-evo:hooks/hooks.json:pre_tool_use:0:0"]\n'
            'trusted_hash = "sha256:def"\n'
        )
        _cleanup_legacy_codex_registrations(target_mkt="evo-hq-evo")
        text = self._read_config()
        self.assertNotIn('"evo@evo-hq:hooks', text)
        self.assertIn('"evo@evo-hq-evo:hooks', text)

    def test_removes_stale_cache_directory(self):
        from evo.host_install.codex import _cleanup_legacy_codex_registrations

        self._write_config(
            '[plugins."evo@evo-hq"]\n'
            'enabled = true\n'
            '\n'
            '[plugins."evo@evo-hq-evo"]\n'
            'enabled = true\n'
        )
        stale_cache = self._make_cache("evo-hq")
        target_cache = self._make_cache("evo-hq-evo")

        _cleanup_legacy_codex_registrations(target_mkt="evo-hq-evo")
        self.assertFalse(stale_cache.exists(),
                         "stale evo-hq cache dir must be removed")
        self.assertTrue(target_cache.exists(),
                        "target evo-hq-evo cache dir must remain")

    def test_preserves_unrelated_plugins_and_sections(self):
        """Cleanup must only touch evo@<other> registrations. Other
        plugins (e.g. claude-code's other marketplaces) and unrelated
        sections must remain intact."""
        from evo.host_install.codex import _cleanup_legacy_codex_registrations

        self._write_config(
            '[features]\n'
            'plugin_hooks = true\n'
            '\n'
            '[plugins."evo@evo-hq"]\n'
            'enabled = true\n'
            '\n'
            '[plugins."evo@evo-hq-evo"]\n'
            'enabled = true\n'
            '\n'
            '[plugins."computer-use@openai-bundled"]\n'
            'enabled = true\n'
            '\n'
            '[plugins."github@openai-curated"]\n'
            'enabled = true\n'
            '\n'
            '[marketplaces.openai-bundled]\n'
            'source_type = "local"\n'
            '\n'
            '[tui]\n'
            'status_line = ["model-with-reasoning"]\n'
        )
        _cleanup_legacy_codex_registrations(target_mkt="evo-hq-evo")
        text = self._read_config()
        self.assertNotIn('"evo@evo-hq"', text)
        self.assertIn('"evo@evo-hq-evo"', text)
        self.assertIn('"computer-use@openai-bundled"', text)
        self.assertIn('"github@openai-curated"', text)
        self.assertIn("[marketplaces.openai-bundled]", text)
        self.assertIn("[tui]", text)
        self.assertIn("[features]", text)

    def test_noop_when_no_legacy_entries(self):
        from evo.host_install.codex import _cleanup_legacy_codex_registrations

        original = (
            '[plugins."evo@evo-hq-evo"]\n'
            'enabled = true\n'
            '\n'
            '[plugins."github@openai-curated"]\n'
            'enabled = true\n'
        )
        self._write_config(original)
        _cleanup_legacy_codex_registrations(target_mkt="evo-hq-evo")
        # File untouched when nothing to clean.
        self.assertEqual(self._read_config(), original)

    def test_noop_when_no_config_file(self):
        """First-time install: config.toml may not yet exist."""
        from evo.host_install.codex import _cleanup_legacy_codex_registrations

        self.assertFalse(self.cfg_path.exists())
        # Should silently return, not raise.
        _cleanup_legacy_codex_registrations(target_mkt="evo-hq-evo")
        self.assertFalse(self.cfg_path.exists())

    def test_handles_multiple_legacy_names(self):
        """If a config has three different evo registrations (e.g. user
        bounced between several pre-rename versions), remove all that
        don't match target."""
        from evo.host_install.codex import _cleanup_legacy_codex_registrations

        self._write_config(
            '[plugins."evo@evo-hq"]\n'
            'enabled = true\n'
            '\n'
            '[plugins."evo@old-alpha"]\n'
            'enabled = true\n'
            '\n'
            '[plugins."evo@evo-hq-evo"]\n'
            'enabled = true\n'
        )
        self._make_cache("evo-hq")
        self._make_cache("old-alpha")
        self._make_cache("evo-hq-evo")

        _cleanup_legacy_codex_registrations(target_mkt="evo-hq-evo")
        text = self._read_config()
        self.assertNotIn('"evo@evo-hq"', text)
        self.assertNotIn('"evo@old-alpha"', text)
        self.assertIn('"evo@evo-hq-evo"', text)
        self.assertFalse((self.codex_home / "plugins" / "cache" / "evo-hq").exists())
        self.assertFalse((self.codex_home / "plugins" / "cache" / "old-alpha").exists())
        self.assertTrue((self.codex_home / "plugins" / "cache" / "evo-hq-evo").exists())


if __name__ == "__main__":
    unittest.main()

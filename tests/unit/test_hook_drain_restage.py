"""Hook-drain binary must survive a host plugin re-stage.

Hosts re-stage the plugin cache by copying the marketplace snapshot:
codex does it on its own when the snapshot changes; claude-code on
`claude plugin update`. The snapshot is a git tree without the binary
(release assets aren't committed), so before the mirror fix a re-stage
silently dropped `bin/evo-hook-drain` from the cache and every hook
fired exit 127 — with `evo doctor` passing right up until the re-stage.

The fix: installers mirror the fetched binary into the snapshot, so a
re-stage carries it. These tests run the real installer file-copy paths
in sandboxed CODEX_HOME / CLAUDE_CONFIG_DIR roots, with
EVO_HOOK_DRAIN_BINARY pointing at a local file (the documented bypass
for the GitHub release fetch), then simulate the host's re-stage and
assert the binary is still there.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "plugins" / "evo" / "src"))

from evo.host_install import claude_code, codex  # noqa: E402
from evo.host_install._hook_drain import (  # noqa: E402
    hook_drain_binary_name,
    mirror_hook_drain_binary,
)

HOOK_NAME = hook_drain_binary_name()


class _SandboxBase(unittest.TestCase):
    """Temp home + EVO_HOOK_DRAIN_BINARY pointing at a fake binary."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        fake_binary = self.root / "fake-evo-hook-drain"
        fake_binary.write_text("#!/bin/sh\necho '{}'\n")
        fake_binary.chmod(0o755)
        self._saved_env = {
            k: os.environ.get(k)
            for k in ("EVO_HOOK_DRAIN_BINARY", "CODEX_HOME", "CLAUDE_CONFIG_DIR")
        }
        os.environ["EVO_HOOK_DRAIN_BINARY"] = str(fake_binary)

    def tearDown(self):
        for k, v in self._saved_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        self._tmp.cleanup()


class TestCodexRestageSurvival(_SandboxBase):

    def setUp(self):
        super().setUp()
        self.codex_home = self.root / ".codex"
        os.environ["CODEX_HOME"] = str(self.codex_home)
        # Minimal marketplace snapshot, as `codex plugin marketplace add`
        # leaves it: git tree contents, no binary in bin/.
        self.snapshot = self.codex_home / ".tmp" / "marketplaces" / "evo-hq"
        plugin = self.snapshot / "plugins" / "evo"
        (self.snapshot / ".claude-plugin").mkdir(parents=True)
        (self.snapshot / ".claude-plugin" / "marketplace.json").write_text(
            json.dumps({"name": "evo-hq-evo", "owner": {"name": "evo-hq"}})
        )
        (plugin / ".codex-plugin").mkdir(parents=True)
        (plugin / ".codex-plugin" / "plugin.json").write_text(
            json.dumps({"name": "evo", "version": "9.9.9"})
        )
        (plugin / "bin").mkdir()
        (plugin / "bin" / "evo").write_text("#!/bin/sh\n")
        (self.codex_home / "config.toml").write_text(
            "[features]\nplugin_hooks = true\n"
        )

    def _cache_plugin_dir(self) -> Path:
        return self.codex_home / "plugins" / "cache" / "evo-hq" / "evo" / "9.9.9"

    def test_install_stages_binary_into_cache_and_snapshot(self):
        rc = codex._install_via_filecopy(None)
        self.assertEqual(rc, 0)
        self.assertTrue((self._cache_plugin_dir() / "bin" / HOOK_NAME).exists())
        self.assertTrue(
            (self.snapshot / "plugins" / "evo" / "bin" / HOOK_NAME).exists()
        )

    def test_binary_survives_codex_restage(self):
        """The regression: codex wipes the cache dir and re-copies the
        snapshot. With the binary mirrored into the snapshot, the
        re-staged cache still has it."""
        self.assertEqual(codex._install_via_filecopy(None), 0)
        cache_dir = self._cache_plugin_dir()
        shutil.rmtree(cache_dir)
        shutil.copytree(self.snapshot / "plugins" / "evo", cache_dir)
        restaged = cache_dir / "bin" / HOOK_NAME
        self.assertTrue(
            restaged.exists(),
            "codex re-stage from the snapshot dropped evo-hook-drain "
            "(hooks would exit 127)",
        )
        self.assertTrue(os.access(restaged, os.X_OK))

    def test_doctor_warns_when_snapshot_binary_missing(self):
        import argparse
        import contextlib
        import io

        self.assertEqual(codex._install_via_filecopy(None), 0)
        (self.snapshot / "plugins" / "evo" / "bin" / HOOK_NAME).unlink()
        # Plugin entry so doctor proceeds past the config checks.
        cfg = self.codex_home / "config.toml"
        cfg.write_text(
            cfg.read_text() + '\n[plugins."evo@evo-hq"]\nenabled = true\n'
        )
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = codex.doctor(argparse.Namespace())
        self.assertIn("not mirrored in the marketplace snapshot", out.getvalue())
        # Latent issue only — doctor must still pass so `evo update`
        # (which skips unhealthy hosts) can re-mirror it.
        self.assertEqual(rc, 0)


class TestClaudeCodeCloneMirror(_SandboxBase):

    def setUp(self):
        super().setUp()
        self.claude_home = self.root / ".claude"
        os.environ["CLAUDE_CONFIG_DIR"] = str(self.claude_home)
        self.cache_plugin = (
            self.claude_home / "plugins" / "cache" / "evo-hq-evo" / "evo" / "9.9.9"
        )
        (self.cache_plugin / "bin").mkdir(parents=True)
        self.clone_plugin = (
            self.claude_home / "plugins" / "marketplaces" / "evo-hq-evo"
            / "plugins" / "evo"
        )
        (self.clone_plugin / "bin").mkdir(parents=True)

    def test_stage_mirrors_into_marketplace_clone(self):
        claude_code._stage_hook_drain(None)
        self.assertTrue((self.cache_plugin / "bin" / HOOK_NAME).exists())
        self.assertTrue((self.clone_plugin / "bin" / HOOK_NAME).exists())

    def test_binary_survives_claude_plugin_update_restage(self):
        """`claude plugin update` re-copies the clone over the cache."""
        claude_code._stage_hook_drain(None)
        shutil.rmtree(self.cache_plugin)
        shutil.copytree(self.clone_plugin, self.cache_plugin)
        self.assertTrue(
            (self.cache_plugin / "bin" / HOOK_NAME).exists(),
            "claude plugin update re-stage dropped evo-hook-drain",
        )

    def test_from_path_does_not_touch_clone(self):
        src_tree = self.root / "src-marketplace"
        (src_tree / "plugins" / "evo" / "bin").mkdir(parents=True)
        claude_code._stage_hook_drain(str(src_tree))
        self.assertTrue(
            (src_tree / "plugins" / "evo" / "bin" / HOOK_NAME).exists()
        )
        self.assertFalse((self.clone_plugin / "bin" / HOOK_NAME).exists())


class TestMirrorHelper(_SandboxBase):

    def test_missing_source_returns_false(self):
        src = self.root / "a"
        dst = self.root / "b"
        (src / "bin").mkdir(parents=True)
        self.assertFalse(mirror_hook_drain_binary(src, dst))

    def test_same_dir_is_noop_true(self):
        src = self.root / "a"
        (src / "bin").mkdir(parents=True)
        binary = src / "bin" / HOOK_NAME
        binary.write_text("#!/bin/sh\n")
        binary.chmod(0o755)
        self.assertTrue(mirror_hook_drain_binary(src, src))

    def test_copies_and_marks_executable(self):
        src = self.root / "a"
        dst = self.root / "b"
        (src / "bin").mkdir(parents=True)
        (src / "bin" / HOOK_NAME).write_text("#!/bin/sh\necho hi\n")
        self.assertTrue(mirror_hook_drain_binary(src, dst))
        out = dst / "bin" / HOOK_NAME
        self.assertTrue(out.exists())
        if sys.platform != "win32":
            self.assertTrue(os.access(out, os.X_OK))


if __name__ == "__main__":
    unittest.main()

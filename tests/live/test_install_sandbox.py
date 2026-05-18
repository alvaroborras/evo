"""Live install verification — runs evo's per-host install commands on
fresh e2b sandboxes and asserts ``evo doctor <host>`` passes.

Catches the kind of regression that unit tests can't see: PyPI/npm
versions diverging, marketplace path layouts changing, native CLIs
adding interactive prompts, etc.

Slow (~10 min per host). Gated.

Run all:
    EVO_LIVE_TEST_INSTALL_SANDBOX=1 \\
    E2B_API_KEY=... \\
    pytest tests/live/test_install_sandbox.py -v -s

Run one host:
    EVO_LIVE_TEST_INSTALL_SANDBOX=1 \\
    E2B_API_KEY=... \\
    pytest tests/live/test_install_sandbox.py::test_opencode -v -s

OpenClaw needs the custom ``evo-test-4g`` template (default 1GB sandbox
OOMs during ``npm install -g openclaw``). Build it once:
    python -c "from e2b import Template; \\
        Template.build(Template().from_ubuntu_image('22.04'), \\
        name='evo-test-4g', memory_mb=4096, cpu_count=2)"

Hermes is xfail until evo-hq-cli ≥0.4.0 ships to PyPI; the install
verifies the entry-point that 0.3.x doesn't ship.
"""

from __future__ import annotations

import os
import shutil
import sys
import tarfile
import tempfile
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
PLUGIN_ROOT = REPO_ROOT / "plugins" / "evo"


def _gate() -> None:
    if os.environ.get("EVO_LIVE_TEST_INSTALL_SANDBOX") != "1":
        pytest.skip("set EVO_LIVE_TEST_INSTALL_SANDBOX=1 to enable")
    if not os.environ.get("E2B_API_KEY"):
        pytest.skip("E2B_API_KEY not set")
    try:
        import e2b  # noqa: F401
    except ImportError:
        pytest.skip("e2b SDK not installed (`pip install 'e2b>=2.20'`)")


def _make_evo_tarball(out: Path) -> None:
    """Bundle the local ``plugins/evo`` source tree for sandbox upload."""
    def filt(tar):
        skip = (".git", ".venv", "node_modules", "__pycache__", "build",
                "dist", ".pytest_cache", ".egg-info")
        if any(s in tar.name for s in skip):
            return None
        return tar

    with tarfile.open(out, "w:gz") as tar:
        tar.add(str(PLUGIN_ROOT), arcname="evo-plugin", filter=filt)


@pytest.fixture(scope="session")
def evo_tarball():
    _gate()
    with tempfile.NamedTemporaryFile(suffix=".tar.gz", delete=False) as f:
        path = Path(f.name)
    try:
        _make_evo_tarball(path)
        yield path
    finally:
        path.unlink(missing_ok=True)


class _Harness:
    """Wraps an e2b sandbox with helpers: upload, run-with-streaming, kill.

    Tests use this through the ``sandbox`` / ``sandbox_4g`` fixtures."""

    def __init__(self, sbx, evo_tarball: Path):
        self.sbx = sbx
        self._uploaded = False
        self._evo_tarball = evo_tarball
        self._sudo = "sudo " if self.run("whoami").strip() != "root" else ""

    def upload_evo(self) -> None:
        if self._uploaded:
            return
        self.sbx.files.write("/tmp/evo-plugin.tar.gz", self._evo_tarball.read_bytes())
        self.run("tar -xzf /tmp/evo-plugin.tar.gz -C /tmp/")
        self._uploaded = True

    def run(self, cmd: str, *, timeout: int = 180, must_succeed: bool = True) -> str:
        short = cmd[:90] + ("…" if len(cmd) > 90 else "")
        print(f"$ {short}", flush=True)
        r = self.sbx.commands.run(
            cmd,
            timeout=timeout,
            on_stdout=lambda x: print(f"  | {x.rstrip()}", flush=True),
            on_stderr=lambda x: print(f"  ! {x.rstrip()}", flush=True),
        )
        if must_succeed and r.exit_code != 0:
            raise AssertionError(
                f"command failed (exit {r.exit_code}): {short}\nstderr: {r.stderr[-500:]}"
            )
        return r.stdout

    def install_base_deps(self) -> None:
        """apt deps + uv + evo CLI from local source."""
        self.upload_evo()
        self.run(
            f"{self._sudo}apt-get update -qq && {self._sudo}apt-get install -y "
            f"--no-install-recommends git curl ca-certificates python3 python3-venv "
            f">/dev/null",
            timeout=300,
        )
        self.run("curl -LsSf https://astral.sh/uv/install.sh | sh > /tmp/uv.log 2>&1",
                 timeout=120)
        self.run(
            "export PATH=$HOME/.local/bin:$PATH; "
            "uv tool install /tmp/evo-plugin > /tmp/evo-tool-install.log 2>&1 || "
            "(cat /tmp/evo-tool-install.log; exit 1)",
            timeout=300,
        )
        self.run("export PATH=$HOME/.local/bin:$PATH; evo --version")

    def install_node(self, version: str = "22") -> None:
        """Install Node from NodeSource (apt's nodejs is too old for some hosts)."""
        self.run(
            f"curl -fsSL https://deb.nodesource.com/setup_{version}.x "
            f"| {self._sudo}bash - >/tmp/node-setup.log 2>&1",
            timeout=120,
        )
        self.run(f"{self._sudo}apt-get install -y nodejs >/dev/null", timeout=180)


def _spawn(template: str | None = None, timeout: int = 1200):
    from e2b import Sandbox
    return Sandbox.create(template=template, timeout=timeout)


@pytest.fixture
def sandbox(evo_tarball):
    _gate()
    sbx = _spawn(timeout=1200)
    h = _Harness(sbx, evo_tarball)
    try:
        h.install_base_deps()
        yield h
    finally:
        try:
            sbx.kill()
        except Exception:  # noqa: BLE001
            pass


@pytest.fixture
def sandbox_4g(evo_tarball):
    """Larger sandbox for hosts whose npm install OOMs on the default 1GB."""
    _gate()
    sbx = _spawn(template="evo-test-4g", timeout=1500)
    h = _Harness(sbx, evo_tarball)
    try:
        h.install_base_deps()
        yield h
    finally:
        try:
            sbx.kill()
        except Exception:  # noqa: BLE001
            pass


# ---------------------------------------------------------------------------
# Per-host install verifications
# ---------------------------------------------------------------------------


def test_opencode(sandbox):
    """Opencode: official installer + `evo install opencode`."""
    sandbox.run(
        "curl -fsSL https://opencode.ai/install | bash > /tmp/opencode.log 2>&1",
        timeout=240,
    )
    sandbox.run("export PATH=$HOME/.opencode/bin:$PATH; opencode --version")
    sandbox.run(
        "export PATH=$HOME/.local/bin:$HOME/.opencode/bin:$PATH; evo install opencode"
    )
    sandbox.run(
        "export PATH=$HOME/.local/bin:$HOME/.opencode/bin:$PATH; evo doctor opencode"
    )
    sandbox.run(
        "export PATH=$HOME/.local/bin:$HOME/.opencode/bin:$PATH; evo update opencode"
    )
    sandbox.run(
        "export PATH=$HOME/.local/bin:$HOME/.opencode/bin:$PATH; evo doctor opencode"
    )


def test_claude_code(sandbox):
    """Claude Code: npm install + unified `evo install claude-code`,
    then `evo update claude-code` to verify the update path."""
    sandbox.install_node("22")
    sandbox.run(
        f"{sandbox._sudo}npm install -g @anthropic-ai/claude-code > /tmp/cc.log 2>&1",
        timeout=300,
    )
    sandbox.run("claude --version")
    sandbox.run("export PATH=$HOME/.local/bin:$PATH; evo install claude-code",
                timeout=180)
    sandbox.run("export PATH=$HOME/.local/bin:$PATH; evo doctor claude-code")
    sandbox.run("export PATH=$HOME/.local/bin:$PATH; evo update claude-code",
                timeout=180)
    sandbox.run("export PATH=$HOME/.local/bin:$PATH; evo doctor claude-code")


def test_codex(sandbox):
    """Codex: npm install + unified `evo install codex`,
    then `evo update codex` to verify the update path."""
    sandbox.install_node("22")
    sandbox.run(
        f"{sandbox._sudo}npm install -g @openai/codex > /tmp/codex.log 2>&1",
        timeout=300,
    )
    sandbox.run("codex --version")
    sandbox.run("export PATH=$HOME/.local/bin:$PATH; evo install codex",
                timeout=180)
    sandbox.run("export PATH=$HOME/.local/bin:$PATH; evo doctor codex")
    sandbox.run("export PATH=$HOME/.local/bin:$PATH; evo update codex",
                timeout=180)
    sandbox.run("export PATH=$HOME/.local/bin:$PATH; evo doctor codex")


def test_openclaw(sandbox_4g):
    """OpenClaw: npm install (heavy) + unified `evo install openclaw`.

    Post-0.4.1: `evo install openclaw` drives `openclaw plugins install
    evo --marketplace ...` itself before setting up the pi-extension.
    """
    sandbox_4g.install_node("22")
    sandbox_4g.run(
        f"{sandbox_4g._sudo}npm install -g openclaw > /tmp/openclaw.log 2>&1",
        timeout=600,
    )
    sandbox_4g.run("openclaw --version")
    sandbox_4g.run("export PATH=$HOME/.local/bin:$PATH; evo install openclaw",
                   timeout=300)
    sandbox_4g.run("export PATH=$HOME/.local/bin:$PATH; evo doctor openclaw")
    sandbox_4g.run("export PATH=$HOME/.local/bin:$PATH; evo update openclaw",
                   timeout=300)
    sandbox_4g.run("export PATH=$HOME/.local/bin:$PATH; evo doctor openclaw")


# ---------------------------------------------------------------------------
# Upgrade verification: 0.4.0 → current release via documented migration path
# ---------------------------------------------------------------------------

# Target version for the upgrade flow. For stable releases both forms
# below are identical; for pre-releases they diverge (PyPI normalises
# 0.4.1-alpha.2 to 0.4.1a2 while the plugin marketplace tag stays
# v0.4.1-alpha.2). Keep both so the test catches normalisation drift
# whenever the target is a pre-release.
_TARGET_PEP440 = "0.4.1"
_TARGET_TAG = "0.4.1"


def _install_evo_cli(sandbox, pypi_version: str):
    """Install evo-hq-cli at the given PyPI version. Returns evo --version."""
    sandbox.run(
        "export PATH=$HOME/.local/bin:$PATH; uv tool uninstall evo-hq-cli",
        timeout=60,
    )
    sandbox.run(
        f"export PATH=$HOME/.local/bin:$PATH; "
        f"uv tool install 'evo-hq-cli=={pypi_version}'",
        timeout=300,
    )
    return sandbox.run("export PATH=$HOME/.local/bin:$PATH; evo --version").strip()


def _run_documented_upgrade(sandbox, host: str):
    """Run the documented migration one-liner (alpha-pinned form):

        uv tool install --force 'evo-hq-cli==<PEP440>' && \
        evo update <host> --version <TAG> --force
    """
    sandbox.run(
        f"export PATH=$HOME/.local/bin:$PATH; "
        f"uv tool install --force 'evo-hq-cli=={_TARGET_PEP440}'",
        timeout=300,
    )
    v_cli = sandbox.run("export PATH=$HOME/.local/bin:$PATH; evo --version").strip()
    assert _TARGET_TAG in v_cli, f"expected {_TARGET_TAG} in {v_cli!r}"

    sandbox.run(
        f"export PATH=$HOME/.local/bin:$PATH; "
        f"evo update {host} --version {_TARGET_TAG} --force",
        timeout=300,
    )
    sandbox.run(f"export PATH=$HOME/.local/bin:$PATH; evo doctor {host}")


def test_upgrade_claude_code(sandbox):
    """0.4.0 → alpha for claude-code via the documented migration one-liner.

    0.4.0's `evo install claude-code` was a print-only stub, so we simulate
    the 0.4.0 install path manually (claude plugin marketplace add +
    plugin install), then test the upgrade lands at the alpha cache."""
    v0 = _install_evo_cli(sandbox, "0.4.0")
    assert "0.4.0" in v0 and "0.4.1" not in v0, f"expected 0.4.0, got {v0!r}"

    sandbox.install_node("22")
    sandbox.run(
        f"{sandbox._sudo}npm install -g @anthropic-ai/claude-code > /tmp/cc.log 2>&1",
        timeout=300,
    )
    sandbox.run("claude plugin marketplace add evo-hq/evo", timeout=120)
    sandbox.run("claude plugin install evo@evo-hq-evo --scope user", timeout=120)
    sandbox.run("export PATH=$HOME/.local/bin:$PATH; evo doctor claude-code")
    pre = sandbox.run(
        "grep version $HOME/.claude/plugins/cache/evo-hq-evo/evo/*/.claude-plugin/plugin.json"
    )
    assert "0.4.0" in pre and "0.4.1" not in pre, f"expected 0.4.0 cache, got {pre!r}"

    _run_documented_upgrade(sandbox, "claude-code")

    post = sandbox.run(
        "grep version $HOME/.claude/plugins/cache/evo-hq-evo/evo/*/.claude-plugin/plugin.json"
    )
    assert _TARGET_TAG in post, f"expected {_TARGET_TAG} in cache, got {post!r}"


def test_upgrade_codex(sandbox):
    """0.4.0 → alpha for codex."""
    v0 = _install_evo_cli(sandbox, "0.4.0")
    assert "0.4.0" in v0 and "0.4.1" not in v0, f"expected 0.4.0, got {v0!r}"

    sandbox.install_node("22")
    sandbox.run(
        f"{sandbox._sudo}npm install -g @openai/codex > /tmp/codex.log 2>&1",
        timeout=300,
    )
    # 0.4.0's `evo install codex` printed instructions; the equivalent
    # bootstrap is the codex marketplace add + plugin install.
    sandbox.run("codex plugin marketplace add evo-hq/evo 2>&1 | tail -3", timeout=120)
    sandbox.run("export PATH=$HOME/.local/bin:$PATH; evo install codex", timeout=180)
    sandbox.run("export PATH=$HOME/.local/bin:$PATH; evo doctor codex")

    _run_documented_upgrade(sandbox, "codex")


def test_upgrade_opencode(sandbox):
    """0.4.0 → alpha for opencode."""
    v0 = _install_evo_cli(sandbox, "0.4.0")
    assert "0.4.0" in v0 and "0.4.1" not in v0, f"expected 0.4.0, got {v0!r}"

    sandbox.run(
        "curl -fsSL https://opencode.ai/install | bash > /tmp/opencode.log 2>&1",
        timeout=240,
    )
    sandbox.run(
        "export PATH=$HOME/.local/bin:$HOME/.opencode/bin:$PATH; evo install opencode"
    )
    sandbox.run(
        "export PATH=$HOME/.local/bin:$HOME/.opencode/bin:$PATH; evo doctor opencode"
    )

    _run_documented_upgrade(sandbox, "opencode")


def test_upgrade_openclaw(sandbox_4g):
    """0.4.0 → alpha for openclaw.

    0.4.0 installed a pi-extension that current openclaw no longer fires
    (`before_provider_request` removed upstream). The upgrade replaces it
    with the new evo-inject native plugin (`tool_result_persist`). The
    test verifies the upgrade succeeds and doctor passes — the
    end-to-end inject verification lives in release-smoke."""
    v0 = _install_evo_cli(sandbox_4g, "0.4.0")
    assert "0.4.0" in v0 and "0.4.1" not in v0, f"expected 0.4.0, got {v0!r}"

    sandbox_4g.install_node("22")
    sandbox_4g.run(
        f"{sandbox_4g._sudo}npm install -g openclaw > /tmp/openclaw.log 2>&1",
        timeout=600,
    )
    sandbox_4g.run(
        "openclaw plugins install evo --marketplace https://github.com/evo-hq/evo "
        "2>&1 | tail -5",
        timeout=120,
    )
    sandbox_4g.run("export PATH=$HOME/.local/bin:$PATH; evo install openclaw",
                   timeout=300)

    _run_documented_upgrade(sandbox_4g, "openclaw")


def test_upgrade_hermes(sandbox):
    """0.4.0 → alpha for hermes.

    Hermes loads evo via the `hermes_agent.plugins` entry-point, so the
    plugin version IS the evo-hq-cli version — no separate marketplace
    cache to invalidate. Upgrade is purely the CLI bump.
    """
    v0 = _install_evo_cli(sandbox, "0.4.0")
    assert "0.4.0" in v0 and "0.4.1" not in v0, f"expected 0.4.0, got {v0!r}"

    sandbox.run(
        "curl -fsSL "
        "https://raw.githubusercontent.com/NousResearch/hermes-agent/main/scripts/install.sh "
        "| bash > /tmp/hermes.log 2>&1",
        timeout=900,
    )
    sandbox.run("export PATH=$HOME/.local/bin:$PATH; hermes --version")
    sandbox.run("export PATH=$HOME/.local/bin:$PATH; evo install hermes")
    sandbox.run("export PATH=$HOME/.local/bin:$PATH; evo doctor hermes")

    _run_documented_upgrade(sandbox, "hermes")


def test_hermes(sandbox):
    """Hermes: official installer + `evo install hermes --from-path` against
    the local source tarball.

    The README's plain ``evo install hermes`` pulls evo-hq-cli from PyPI;
    until 0.4.0 ships, that pulls the older 0.3.x without the
    ``hermes_agent.plugins`` entry-point. Live tests bypass PyPI by
    installing from the uploaded local source via ``--from-path``."""
    sandbox.run(
        "curl -fsSL "
        "https://raw.githubusercontent.com/NousResearch/hermes-agent/main/scripts/install.sh "
        "| bash > /tmp/hermes.log 2>&1",
        timeout=900,
    )
    sandbox.run("export PATH=$HOME/.local/bin:$PATH; hermes --version")
    sandbox.run(
        "export PATH=$HOME/.local/bin:$PATH; "
        "evo install hermes --from-path /tmp/evo-plugin"
    )
    sandbox.run("export PATH=$HOME/.local/bin:$PATH; evo doctor hermes")
    sandbox.run(
        "export PATH=$HOME/.local/bin:$PATH; "
        "evo update hermes --from-path /tmp/evo-plugin"
    )
    sandbox.run("export PATH=$HOME/.local/bin:$PATH; evo doctor hermes")

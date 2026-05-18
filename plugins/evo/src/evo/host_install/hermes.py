"""Hermes runtime plugin install — installs evo-hq-cli into hermes's venv
+ enables the plugin in config.yaml. Hermes's `plugins enable` only sees
directory plugins, not entry-point plugins, so we edit the config directly.
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path


def _hermes_venv_python() -> Path | None:
    """Best-effort detection of hermes's venv python."""
    home = Path.home()
    candidates = [
        home / ".hermes" / "hermes-agent" / "venv" / "bin" / "python3",
        home / ".hermes" / "hermes-agent" / ".venv" / "bin" / "python3",
    ]
    for c in candidates:
        if c.exists():
            return c
    # Walk hermes binary's shebang.
    hermes = shutil.which("hermes")
    if hermes:
        try:
            line = Path(hermes).read_text().splitlines()[0]
            if line.startswith("#!"):
                p = Path(line[2:].strip())
                if p.exists():
                    return p
        except Exception:
            pass
    return None


def _hermes_config_yaml() -> Path:
    home_override = os.environ.get("HERMES_HOME")
    base = Path(home_override) if home_override else Path.home() / ".hermes"
    return base / "config.yaml"


_SKILLS = ("discover", "optimize", "subagent", "infra-setup")


def install(args: argparse.Namespace) -> int:
    py = _hermes_venv_python()
    if py is None:
        print(
            "ERROR: could not locate hermes's venv python.\n"
            "  Tried ~/.hermes/hermes-agent/venv/bin/python3 and the hermes shebang.\n"
            "  Install hermes first: see https://hermes-agent.nousresearch.com/",
            file=sys.stderr,
        )
        return 2

    # Install evo skills via the `hermes skills` CLI. Skipped when
    # --from-path is set (live tests don't have github access from the
    # sandbox, and pull skills from the uploaded source instead).
    if not getattr(args, "from_path", None) and not getattr(args, "from_source", False):
        if shutil.which("hermes") is not None:
            # Pin to --version's git tag when set. hermes accepts
            # `owner/repo@<ref>/path/to/skill` per test_release_smoke.py:115.
            version = getattr(args, "version", None)
            ref_suffix = ""
            if version:
                import re as _re
                ref = f"v{version}" if _re.match(r"^\d+\.\d+\.\d+", version) else version
                ref_suffix = f"@{ref}"
            for skill in _SKILLS:
                # `discover` needs --force to bypass the SKILL.md scanner,
                # which flags evo's own install examples.
                extra = ["--force"] if skill == "discover" else []
                cmd = ["hermes", "skills", "install",
                       f"evo-hq/evo{ref_suffix}/plugins/evo/skills/{skill}", "-y", *extra]
                print(f"$ {' '.join(cmd)}")
                subprocess.call(cmd)
        else:
            print("NOTE: `hermes` binary not on PATH; skipping skill installs.")

    # Bootstrap pip if missing.
    if subprocess.run([str(py), "-m", "pip", "--version"], capture_output=True).returncode != 0:
        print("hermes venv has no pip — bootstrapping via ensurepip…", flush=True)
        rc = subprocess.run([str(py), "-m", "ensurepip"]).returncode
        if rc != 0:
            print(f"ERROR: ensurepip failed (rc={rc})", file=sys.stderr)
            return rc

    # Install evo-hq-cli into the hermes venv.
    #   --from-path <dir>: pip install <dir> (any local source: tarball
    #     extract, git checkout, etc.). Used by live tests + manual installs.
    #   --from-source:     editable install from the running evo's package
    #     root. Works only when evo runs from a real checkout (parents[3]
    #     resolves to plugins/evo/); for uv-tool installs this picks up the
    #     tool's site-packages dir, which is wrong — pass --from-path instead.
    #   default:           pip install evo-hq-cli==<running version> (PyPI).
    #                      Pinning the running version (rather than bare
    #                      `evo-hq-cli`) handles pre-releases — pip skips
    #                      `0.4.0a10` from a bare `pip install evo-hq-cli`
    #                      (needs --pre), so users who installed an alpha
    #                      via `uv tool install evo-hq-cli==0.4.0-alpha.N`
    #                      would otherwise see hermes drop back to the last
    #                      stable (0.3.3) and the hermes_agent.plugins
    #                      entry-point check would fail.
    target_path = getattr(args, "from_path", None)
    if target_path:
        target = str(target_path)
        cmd = [str(py), "-m", "pip", "install", target]
        print(f"installing evo-hq-cli from {target}…", flush=True)
    elif getattr(args, "from_source", False):
        repo_root = Path(__file__).resolve().parents[3]
        target = str(repo_root)
        cmd = [str(py), "-m", "pip", "install", "-e", target]
        print(f"installing evo-hq-cli (editable) from {target}…", flush=True)
    else:
        from evo import __version__ as _evo_version
        spec = f"evo-hq-cli=={_evo_version}"
        cmd = [str(py), "-m", "pip", "install", spec]
        print(f"installing {spec} into hermes venv via pip…", flush=True)

    rc = subprocess.run(cmd).returncode
    if rc != 0:
        print(f"ERROR: pip install failed (rc={rc})", file=sys.stderr)
        return rc

    # Verify the entry-point actually got registered. Catches the case
    # where pip "succeeded" but installed an older PyPI version that
    # doesn't ship the hermes plugin.
    check = subprocess.run(
        [str(py), "-c", "import importlib.metadata as m; "
         "eps = m.entry_points(group='hermes_agent.plugins'); "
         "print(next((e.name for e in eps if e.name == 'evo'), 'MISSING'))"],
        capture_output=True,
        text=True,
    )
    if "evo" not in (check.stdout or "") or "MISSING" in (check.stdout or ""):
        print(
            "ERROR: pip install reported success but the hermes_agent.plugins "
            "entry-point for 'evo' is not registered.\n"
            "  This usually means PyPI shipped an older evo-hq-cli without "
            "the hermes plugin.\n"
            "  Try: evo install hermes --from-source  (from a v0.4.0+ checkout)",
            file=sys.stderr,
        )
        return 2

    # Enable the plugin in config.yaml.
    cfg = _hermes_config_yaml()
    try:
        import yaml
    except ImportError:
        # yaml isn't a hard dep of evo; fall back to text edit (yaml is in
        # hermes's venv anyway, so most users are fine).
        print(
            "WARNING: pyyaml not available — please add 'evo' to plugins.enabled in "
            f"{cfg} manually.",
            file=sys.stderr,
        )
        return 0

    data: dict = {}
    if cfg.exists():
        try:
            data = yaml.safe_load(cfg.read_text()) or {}
        except yaml.YAMLError as exc:
            print(f"ERROR: could not parse {cfg}: {exc}", file=sys.stderr)
            return 2
    plugins = data.setdefault("plugins", {})
    enabled = plugins.setdefault("enabled", [])
    if "evo" not in enabled:
        enabled.append("evo")
        cfg.parent.mkdir(parents=True, exist_ok=True)
        cfg.write_text(yaml.safe_dump(data, default_flow_style=False))
        print(f"added 'evo' to plugins.enabled in {cfg}", flush=True)
    else:
        print(f"'evo' already enabled in {cfg}", flush=True)

    print()
    print("evo plugin installed for hermes.")
    print("Restart any running hermes gateway: `hermes gateway restart`")
    return 0


def uninstall(args: argparse.Namespace) -> int:
    py = _hermes_venv_python()
    if py is not None:
        print("removing evo-hq-cli from hermes venv…", flush=True)
        subprocess.run([str(py), "-m", "pip", "uninstall", "-y", "evo-hq-cli"]).returncode

    cfg = _hermes_config_yaml()
    if cfg.exists():
        try:
            import yaml
            data = yaml.safe_load(cfg.read_text()) or {}
            enabled = data.get("plugins", {}).get("enabled", [])
            if "evo" in enabled:
                enabled.remove("evo")
                cfg.write_text(yaml.safe_dump(data, default_flow_style=False))
                print(f"removed 'evo' from plugins.enabled in {cfg}", flush=True)
        except Exception:
            pass

    print("evo plugin uninstalled from hermes.")
    return 0


def doctor(args: argparse.Namespace) -> int:
    py = _hermes_venv_python()
    if py is None:
        print("✗ hermes venv not found")
        return 1
    print(f"✓ hermes venv python: {py}")

    rc = subprocess.run(
        [str(py), "-c", "import importlib.metadata as m; "
         "eps = m.entry_points(group='hermes_agent.plugins'); "
         "print('evo' if any(e.name == 'evo' for e in eps) else 'MISSING')"],
        capture_output=True,
        text=True,
    )
    if "evo" in (rc.stdout or "") and "MISSING" not in (rc.stdout or ""):
        print("✓ evo entry-point registered in hermes venv")
    else:
        print("✗ evo entry-point NOT registered in hermes venv")
        print("  Run: evo install hermes")
        return 1

    cfg = _hermes_config_yaml()
    if cfg.exists():
        try:
            import yaml
            data = yaml.safe_load(cfg.read_text()) or {}
            enabled = data.get("plugins", {}).get("enabled", [])
            if "evo" in enabled:
                print(f"✓ 'evo' in plugins.enabled ({cfg})")
            else:
                print(f"✗ 'evo' NOT in plugins.enabled ({cfg})")
                return 1
        except Exception as exc:
            print(f"✗ could not parse config.yaml: {exc}")
            return 1
    else:
        print(f"✗ config.yaml not found at {cfg}")
        return 1
    return 0

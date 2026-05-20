"""Cursor install — two parts, mirroring the cross-host shape:

  1. evo skills (`discover`/`optimize`/`subagent`/`infra-setup`) via the
     cross-host ``npx skills add ... --agent cursor``. Cursor auto-discovers
     SKILL.md from its skill dirs and shows them in the ``/`` menu.

  2. Mid-run inject (``evo direct``) via Cursor-native hooks. We write
     ``~/.cursor/hooks.json`` entries that run ``evo-drain --host cursor`` on
     ``sessionStart`` (registers the session) and ``stop`` (delivers the
     directive as a ``followup_message`` that the IDE auto-submits as a
     visible message). The IDE silently drops ``additional_context``, so
     postToolUse/sessionStart context injection is not used. Unlike Claude
     Code / Codex, Cursor needs no ``evo-hook-drain`` binary in front: its
     hooks.json command talks to ``evo-drain`` directly, which resolves the
     run dir and session from the hook stdin payload.

Why not reuse the Claude Code compat layer (`.claude/settings.json`)?
Cursor can load Claude Code hooks, but ``UserPromptSubmit`` maps to
``beforeSubmitPrompt`` (informational-only, cannot inject) and the plugin
hook path depends on ``CLAUDE_PLUGIN_ROOT`` which Cursor doesn't set. Native
``~/.cursor/hooks.json`` keeps Cursor config in Cursor's namespace and uses
the events that actually deliver context.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

# Same shape claude_code._RELEASE_VERSION_RE uses to decide whether to
# auto-prefix with `v` for the GitHub tag URL.
_RELEASE_VERSION_RE = re.compile(r"^\d+\.\d+\.\d+([.\-+a-zA-Z0-9]*)$")

# Events evo wires in the IDE:
#   - sessionStart: registers the session (no delivery — the IDE drops
#     additional_context, so there's no point draining here).
#   - stop: fires at the end of each agent turn and returns followup_message,
#     which the IDE auto-submits as a visible new message. This is the only
#     channel that actually reaches the chat.
# postToolUse/beforeSubmitPrompt are not wired: their only output
# (additional_context) is silently dropped by Cursor's IDE agent.
_INJECT_EVENTS = ("sessionStart", "stop")


def _cursor_base() -> Path:
    home_override = os.environ.get("CURSOR_HOME")
    return Path(home_override) if home_override else Path.home() / ".cursor"


def _cursor_hooks_file() -> Path:
    return _cursor_base() / "hooks.json"


def _skills_source(from_path: str | None, version: str | None) -> str:
    """Source spec for ``npx skills add`` (identical resolution to the
    opencode adapter): local path > tagged release URL > default branch."""
    if from_path:
        return from_path
    if version:
        ref = f"v{version}" if _RELEASE_VERSION_RE.match(version) else version
        return f"https://github.com/evo-hq/evo.git#{ref}"
    return "evo-hq/evo"


def _install_skills(source: str) -> None:
    """Run ``npx -y skills add <source> --agent cursor -g -y``.

    Skips with a warning when npx isn't on PATH. Cursor auto-discovers the
    installed SKILL.md files and lists them in the ``/`` menu.
    """
    npx = shutil.which("npx")
    if npx is None:
        print(
            "WARNING: `npx` not on PATH; skipping skill install.\n"
            "  Install Node 22+ (https://nodejs.org), then re-run "
            "`evo install cursor`.",
            file=sys.stderr,
        )
        return
    cmd = [npx, "-y", "skills", "add", source, "--agent", "cursor", "-g", "-y"]
    print(f"$ {' '.join(cmd)}")
    subprocess.call(cmd)


def _drain_command() -> str:
    """The shell command Cursor runs on each inject event. Resolve the
    absolute ``evo-drain`` path so the hook works regardless of the shell's
    PATH at fire time; fall back to the bare name if not yet on PATH."""
    drain = shutil.which("evo-drain") or "evo-drain"
    return f"{drain} --host cursor"


def _is_evo_entry(entry: dict) -> bool:
    return isinstance(entry, dict) and "evo-drain" in str(entry.get("command", ""))


def _write_inject_hooks() -> Path:
    """Merge evo's inject hooks into ``~/.cursor/hooks.json`` without
    clobbering the user's existing hooks. Idempotent: re-running refreshes
    the evo entries (e.g. an updated evo-drain path) in place."""
    hooks_file = _cursor_hooks_file()
    hooks_file.parent.mkdir(parents=True, exist_ok=True)

    data: dict = {}
    if hooks_file.exists():
        try:
            data = json.loads(hooks_file.read_text())
        except json.JSONDecodeError:
            print(
                f"WARNING: could not parse {hooks_file}; leaving it untouched. "
                "Inject will not work until it's valid JSON.",
                file=sys.stderr,
            )
            return hooks_file
    if not isinstance(data, dict):
        data = {}

    data.setdefault("version", 1)
    hooks = data.setdefault("hooks", {})
    if not isinstance(hooks, dict):
        hooks = {}
        data["hooks"] = hooks

    command = _drain_command()
    for event in _INJECT_EVENTS:
        arr = hooks.setdefault(event, [])
        if not isinstance(arr, list):
            arr = []
            hooks[event] = arr
        # Drop any stale evo entry, then add the current one — keeps the
        # command (absolute drain path) fresh across reinstalls.
        arr[:] = [e for e in arr if not _is_evo_entry(e)]
        arr.append({"command": command})

    hooks_file.write_text(json.dumps(data, indent=2) + "\n")
    print(f"wired inject hooks in {hooks_file} ({', '.join(_INJECT_EVENTS)} -> {command})")
    return hooks_file


def install(args: argparse.Namespace) -> int:
    source = _skills_source(
        getattr(args, "from_path", None),
        getattr(args, "version", None),
    )
    print(f"Installing evo skills via npx skills add (source: {source}) ...")
    _install_skills(source)

    print("\nWiring mid-run inject hooks ...")
    _write_inject_hooks()

    if shutil.which("evo-drain") is None:
        print(
            "\nNOTE: `evo-drain` is not on PATH yet. Install the CLI so the "
            "inject hook can fire:\n  uv tool install evo-hq-cli",
            file=sys.stderr,
        )

    print(
        "\n✓ evo installed for cursor.\n"
        "  Skills appear in the `/` menu. Restart any open Cursor session "
        "(IDE Agent or `cursor-agent`) to load the hooks."
    )
    return 0


def update(args: argparse.Namespace) -> int:
    """``npx skills add`` is idempotent and the hook writer refreshes in
    place, so update and install share a path."""
    return install(args)


def uninstall(args: argparse.Namespace) -> int:
    hooks_file = _cursor_hooks_file()
    if hooks_file.exists():
        try:
            data = json.loads(hooks_file.read_text())
        except json.JSONDecodeError:
            data = None
        if isinstance(data, dict) and isinstance(data.get("hooks"), dict):
            removed = False
            # Scan ALL events, not just current _INJECT_EVENTS, so evo entries
            # left under a previously-wired event (e.g. postToolUse) are cleaned.
            for event in list(data["hooks"].keys()):
                arr = data["hooks"].get(event)
                if isinstance(arr, list):
                    kept = [e for e in arr if not _is_evo_entry(e)]
                    if len(kept) != len(arr):
                        removed = True
                    if kept:
                        data["hooks"][event] = kept
                    else:
                        del data["hooks"][event]
            if removed:
                hooks_file.write_text(json.dumps(data, indent=2) + "\n")
                print(f"removed evo inject hooks from {hooks_file}")
            else:
                print(f"no evo inject hooks found in {hooks_file}")
    else:
        print(f"no {hooks_file} (nothing to remove)")
    print("To remove the skills: `npx skills remove evo-hq/evo --agent cursor` "
          "(or delete them from your Cursor skills dir).")
    return 0


def _skill_roots() -> list[Path]:
    """Directories Cursor discovers skills from. `npx skills add --agent
    cursor` may target any of these depending on CLI version, so doctor
    checks all of them leniently."""
    home = Path.home()
    return [
        _cursor_base() / "skills",
        home / ".agents" / "skills",
        Path.cwd() / ".cursor" / "skills",
        Path.cwd() / ".agents" / "skills",
    ]


def doctor(args: argparse.Namespace) -> int:
    rc = 0

    # 1. evo-drain reachable — without it the hook fires into the void.
    if shutil.which("evo-drain"):
        print(f"✓ evo-drain on PATH: {shutil.which('evo-drain')}")
    else:
        print("✗ evo-drain not on PATH (inject hook cannot fire)")
        print("  Run: uv tool install evo-hq-cli")
        rc = 1

    # 2. inject hooks wired in ~/.cursor/hooks.json.
    hooks_file = _cursor_hooks_file()
    if hooks_file.exists():
        try:
            data = json.loads(hooks_file.read_text())
            hooks = data.get("hooks", {}) if isinstance(data, dict) else {}
            wired = [
                ev for ev in _INJECT_EVENTS
                if any(_is_evo_entry(e) for e in hooks.get(ev, []) or [])
            ]
            if set(wired) == set(_INJECT_EVENTS):
                print(f"✓ inject hooks wired in {hooks_file} ({', '.join(_INJECT_EVENTS)})")
            else:
                missing = [ev for ev in _INJECT_EVENTS if ev not in wired]
                print(f"✗ inject hooks missing in {hooks_file}: {', '.join(missing)}")
                print("  Run: evo install cursor")
                rc = 1
        except json.JSONDecodeError:
            print(f"✗ could not parse {hooks_file}")
            rc = 1
    else:
        print(f"✗ no {hooks_file}")
        print("  Run: evo install cursor")
        rc = 1

    # 3. skills discoverable (lenient — exact dir depends on skills CLI version).
    found_skill = None
    for root in _skill_roots():
        for name in ("optimize", "discover"):
            if (root / name / "SKILL.md").exists():
                found_skill = root / name
                break
        if found_skill:
            break
    if found_skill:
        print(f"✓ evo skills installed (e.g. {found_skill})")
    else:
        print("? evo skills not found in known Cursor skill dirs "
              f"({', '.join(str(r) for r in _skill_roots())})")
        print("  Run: evo install cursor  (needs `npx` / Node on PATH)")
        # Not a hard failure: the skills CLI may install elsewhere on some
        # versions, and the agent can still load skills it discovers.
    return rc

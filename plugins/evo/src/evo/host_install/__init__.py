"""Host plugin install adapters — `evo install <host>` dispatches here.

Each host module exposes:
    install(args) -> int       # 0 success
    uninstall(args) -> int
    doctor(args) -> int        # verify install: paths, configs, basic load

Hosts that have native marketplaces (Claude Code, Codex, OpenClaw) point
the user at the marketplace command. Hosts where evo's setup needs
multiple steps (Hermes, Opencode) implement the steps here.

See notes/cross-host-inject-design.md.
"""

from __future__ import annotations

from . import claude_code, codex, hermes, opencode, openclaw

ADAPTERS = {
    "claude-code": claude_code,
    "codex": codex,
    "hermes": hermes,
    "opencode": opencode,
    "openclaw": openclaw,
}

# Single source of truth for host names. CLI argparse choices and
# anything else that enumerates supported hosts should read this.
SUPPORTED_HOSTS = sorted(ADAPTERS)


def get(host: str):
    if host not in ADAPTERS:
        valid = ", ".join(SUPPORTED_HOSTS)
        raise ValueError(f"unknown host {host!r} — try one of: {valid}")
    return ADAPTERS[host]

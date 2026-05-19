# @evo-hq/pi-evo

Evo plugin for [pi-coding-agent](https://pi.dev) — adds the `/discover`,
`/optimize`, `/subagent`, and `/infra-setup` skills, plus a `before_provider_request`
extension that consumes `evo direct` mid-run inject messages.

## Install

```bash
pi install npm:@evo-hq/pi-evo
```

Pi auto-registers the extension under `~/.pi/agent/extensions/` and discovers
the skills under `~/.pi/agent/skills/`. No additional setup steps.

A parallel-subagent provider ([`pi-subagents`](https://pi.dev/packages/pi-subagents))
is bundled — pi's default toolkit has no fanout primitive, and evo's `optimize`
skill needs one to run multiple experiments per round.

## What ships in this package

| Path | Purpose |
|---|---|
| `extensions/evo/index.js` | Bundled JS that hooks `before_provider_request`, drains the workspace inject queue, appends `[evo direct]` directives to the outgoing LLM payload |
| `skills/discover/` | First-run setup: explore repo, propose optimization dimensions, build benchmark, run first experiment |
| `skills/optimize/` | The search loop: parallel subagents form hypotheses, edit, get scored, frontier picks next branch |
| `skills/subagent/` | Per-experiment brief contract for the optimize round's fanout |
| `skills/infra-setup/` | Provider matrix for remote-sandbox backends (Modal, E2B, Daytona, etc.) |

## Versioning

This package is versioned in lockstep with the [evo-hq/evo](https://github.com/evo-hq/evo)
release line. The skills + bundled extension are sync'd from `plugins/evo/` in
the source repo at publish time (see `scripts/sync-from-source.sh`).

## Source

[github.com/evo-hq/evo](https://github.com/evo-hq/evo) (Apache-2.0)

<p align="center">
  <img src="assets/banner.png" alt="evo banner" width="100%" />
</p>

# evo

Autoresearch orchestrator for your codebase.
*Inspired by [Karpathy's autoresearch](https://github.com/karpathy/nanochat).*

Runs on Claude Code, Codex, OpenClaw, Hermes, or Opencode. Experiments
run locally or on remote sandboxes — Modal, E2B, Daytona, AWS, Azure, SSH.

Point it at a repo. evo explores the code, instruments a benchmark, and
runs an optimization loop — spawning subagents **in parallel**, each in
its own isolated workspace, forming a hypothesis and editing toward it.
The orchestrator collects results, decides which branch to extend next
via a **configurable frontier strategy** (argmax, top-K, ε-greedy,
softmax, or GEPA-inspired Pareto-per-task), keeps what improves the
score, discards what doesn't. Runs until you stop it.

## What it looks like

<p align="center">
  <img src="assets/dashboard.png" alt="evo dashboard" width="100%" />
</p>

## Quickstart

**1. Install the CLI** (Claude Code bundles its own — skip unless you want
a remote backend).

```bash
uv tool install evo-hq-cli              # or: pipx install evo-hq-cli
evo --version
```

For remote backends, install with the matching provider extra:

```bash
uv tool install 'evo-hq-cli[modal]'
# available: [modal], [e2b], [daytona], [aws], [azure], [all]
```

**2. Add the plugin to your host.**

Claude Code (run inside Claude):

```
/plugin marketplace add evo-hq/evo
/plugin install evo@evo-hq-evo
```

Codex (requires 0.122.0+ — `npm install -g @openai/codex@latest`):

```bash
codex plugin marketplace add evo-hq/evo
evo install codex
```

Then trust the evo hooks: start `codex`, run `/hooks`, trust each evo
hook. Without this, `evo direct` mid-run directives won't reach the
agent; skills and the rest of evo still work. For non-interactive setups
(CI, `codex exec`), add `--trust-hooks` to `evo install codex` to skip
the manual review.

OpenClaw:

```bash
openclaw plugins install evo --marketplace https://github.com/evo-hq/evo
evo install openclaw
```

Hermes (skills install per-skill; runtime plugin via pip entry-point):

```bash
hermes skills install evo-hq/evo/plugins/evo/skills/discover -y --force
hermes skills install evo-hq/evo/plugins/evo/skills/optimize -y
hermes skills install evo-hq/evo/plugins/evo/skills/subagent -y
hermes skills install evo-hq/evo/plugins/evo/skills/infra-setup -y
evo install hermes
```

`--force` on `discover` bypasses the SKILL.md scanner — it flags evo's
own install examples.

Opencode:

```bash
npx skills add evo-hq/evo --agent opencode -g
evo install opencode
```

Opencode's `task` tool is batch-parallel (all subagents in one assistant
turn return together when the slowest finishes), not background-with-
notification like the other four hosts. evo's optimize loop works fine —
rounds complete batch-wise — but reactive workflows that act on early
completions before the slowest finishes aren't supported on opencode.

Verify any install: `evo doctor <host>`.

**3. Run.**

```
/evo:discover
/evo:optimize
```

Invocation prefix varies by host — see `evo --help`.

`optimize` accepts optional parameters:

| Parameter | Default | Description |
|-----------|---------|-------------|
| `subagents` | 5 | Number of parallel subagents per round |
| `budget` | 5 | Max iterations each subagent can run within its branch |
| `stall` | 5 | Consecutive rounds with no improvement before auto-stopping |

Example (Claude Code): `/evo:optimize subagents=3 budget=10 stall=3`.

## How it works

### Parallel

The orchestrator fans subagents out simultaneously. Each runs in its own
isolated workspace, picks up shared state (failure traces, annotations,
discarded hypotheses), forms its own hypothesis, edits, and runs the
benchmark. If a subagent has iteration budget left and sees a follow-up,
it iterates on its branch within the same round.

### Frontier strategy

After each round, the orchestrator picks which committed branch to
extend next. Pluggable:

- **argmax** — always extend the best score
- **top_k** — round-robin among the K best
- **epsilon_greedy** — best most of the time, random sometimes
- **softmax** — sample weighted by score
- **pareto_per_task** — keep specialists the aggregate hides, inspired
  by [GEPA](https://arxiv.org/abs/2507.19457)

Set in the dashboard's Frontier tab — strategy descriptions and params
are shown inline.

### Cross-cutting scans

Between rounds, [RLM](https://arxiv.org/abs/2512.24601)-inspired scan
subagents read trace batches in parallel and surface compound failure
patterns — gate-failure intersections, semantic root causes — that the
next round's hypotheses can target directly.

### Gating

Regression tests or safety checks wire up as a gate. An experiment that
doesn't pass gets discarded, even if its score improves.

## Where experiments run

| Backend | Where | Install |
|---|---|---|
| **worktree** *(default)* | local git worktree per experiment | included |
| **pool** | reuse a fixed set of local workspaces | included |
| **ssh** | your own SSH host | included |
| **modal** | Modal serverless cloud | `uv tool install 'evo-hq-cli[modal]'` |
| **e2b** | E2B cloud sandboxes | `uv tool install 'evo-hq-cli[e2b]'` |
| **daytona** | Daytona cloud workspaces | `uv tool install 'evo-hq-cli[daytona]'` |
| **aws** | AWS EC2 sandboxes | `uv tool install 'evo-hq-cli[aws]'` |
| **azure** | Azure VMs | `uv tool install 'evo-hq-cli[azure]'` |

Pick and configure in the dashboard's Backend tab.

## Dashboard

Starts automatically with `evo:discover` (or `evo init`). The agent
surfaces the URL in chat:

```
Dashboard live: http://127.0.0.1:8080 (pid 12345)
```

If `8080` is busy, evo auto-increments (`8081`, `8082`, …) and prints
the actual port. Start it manually with:

```bash
uv run --project /path/to/evo/plugins/evo evo dashboard --port 8080
```

The chosen port is persisted to `.evo/dashboard.port` so repeat runs
re-use it.

## Dev install

For working on evo itself (not just using it):

```bash
git clone https://github.com/evo-hq/evo
cd evo
uv run --project plugins/evo evo --version
```

`uv run` resolves dependencies on first use — no `pip install` step.

The SDKs live in separate packages:

- `sdk/python/` — `evo-hq-agent`, Python 3.10+, zero deps. Tests: `cd sdk/python && uv run --with pytest pytest test/`.
- `sdk/node/` — `@evo-hq/evo-agent`, Node 18+, zero deps. Tests: `cd sdk/node && npm test`.

## License

Licensed under the [Apache License 2.0](LICENSE).

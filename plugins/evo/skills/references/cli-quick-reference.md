# Evo CLI Quick Reference

Use this when you need to operate an evo workspace. The CLI orchestrates
experiments; the Agent SDK instruments benchmark code.

## Mental Model

- `evo init` sets up a workspace and starts the dashboard.
- `evo new` allocates an experiment under a parent node.
- `evo run` executes benchmark + inherited gates and commits if the
  score improves and gates pass.
- `evo run --check` validates wiring without mutating experiment state.
- `evo scratchpad` is your bounded view of current state.
- `evo gate ...` defines branch policy; gates inherit down the tree.
- `evo config runtime ...` and `evo env ...` describe runtime state.
- Workspace ops (`bash/read/write/edit/glob/grep`) are the portable way
  to touch experiment files — required for remote backends, recommended
  for local so the same code works regardless of backend.

## Reading workspace state

| What | How | Why |
| --- | --- | --- |
| Worktrees (`worktrees/<exp>/...`) | `Read`, `grep`, `Bash` directly | Just code under git. |
| `.evo/project.md` | `Read` directly | Agent's persistent project notes (you write it, you read it). |
| Per-attempt artifacts: `outcome.json`, `traces/task_*.json`, `diff.patch` under `.evo/run_*/experiments/<exp>/attempts/<NNN>/` | `Read`/`grep` directly for cross-experiment scans; `evo show <id>` for one node | Immutable once written. Bulk reads beat N CLI subprocesses. |
| Graph state (nodes, status, scores, parents, notes) | `evo show <id>`, `evo awaiting`, `evo discards`, `evo notes`, `evo scratchpad` | Lock-managed; schema may shift; getters survive layout changes. |
| Config (`config.json`) | `evo config show`, `evo config get <field>`, `evo config backend show`, `evo config runtime show`, `evo env show` | Lock-managed; concurrent dashboard writes possible. |
| Infra event log | `evo infra log` | Has a getter; no need to find the file. |

**All writes go through the CLI** — `evo config set`, `evo new`, `evo run`,
`evo discard`, `evo restore`, `evo gate add`, `evo env load`, `evo set`,
`evo annotate`, `evo note`, `evo infra event`. Hand-editing `graph.json` /
`config.json` races with the dashboard and bypasses validation.

The exception: `.evo/project.md` is agent-authored — write it with the `Write`
tool when you need to update it.

## Setup

```bash
evo init \
  --name "<project name>" \
  --target <entrypoint-file> \
  --benchmark "<command using {worktree} and/or {target}>" \
  --metric <max|min> \
  --host <claude-code|codex|opencode|openclaw|hermes|generic> \
  [--instrumentation-mode <sdk|inline>] \
  [--gate "<command>"] \
  [--commit-strategy <all|tracked-only>]
```

- `--name` is dashboard display text. Existing unnamed workspaces fall back to
  the repo directory name.
- `--target` is the evaluation entrypoint passed to `{target}`. It is not the
  entire optimization boundary.
- `--benchmark` is the command evo runs. Use `{worktree}` for files created in
  experiment branches.
- `--host` records the orchestrator runtime; it controls whether `dispatch` is
  available.

## Configuration

```bash
evo config show [--json]                           # full redacted dump
evo config get <field> [--json]                    # one field
evo config set <field> <value>                     # mutate one field
```

Settable / gettable fields:

```
project-name | target | benchmark | metric | commit-strategy
max-attempts | gate | frontier-strategy
```

Examples:

```bash
evo config set metric max
evo config set max-attempts 6
evo config set gate "pytest -q"            # empty string clears
evo config set frontier-strategy epsilon_greedy
evo config set frontier-strategy '{"kind": "top_k", "params": {"k": 4}}'

evo config get metric                       # -> max
evo config get frontier-strategy --json     # -> {"kind": "...", "params": {...}}
```

Always go through the CLI; do not hand-edit `.evo/` JSON files (advisory locks
exist for a reason and the dashboard may be writing concurrently).

### Configurable fields

| Field                  | Setter                              | Reader                              | Notes                                                  |
| ---------------------- | ----------------------------------- | ----------------------------------- | ------------------------------------------------------ |
| `project_name`         | `evo config set project-name`       | `evo config get project-name`       |                                                        |
| `target`               | `evo config set target`             | `evo config get target`             | Path the orchestrator edits.                           |
| `benchmark`            | `evo config set benchmark`          | `evo config get benchmark`          | Command that emits a score.                            |
| `metric`               | `evo config set metric`             | `evo config get metric`             | `max` or `min`.                                        |
| `commit_strategy`      | `evo config set commit-strategy`    | `evo config get commit-strategy`    | `all` or `tracked-only`.                               |
| `max_attempts`         | `evo config set max-attempts`       | `evo config get max-attempts`       | Per-experiment retry cap. Default 3.                   |
| `gate`                 | `evo config set gate`               | `evo config get gate`               | Workspace-default gate. Per-node gates: `evo gate add`. |
| `frontier_strategy`    | `evo config set frontier-strategy`  | `evo config get frontier-strategy`  | Kinds: `argmax`, `top_k`, `epsilon_greedy`, `softmax`, `pareto_per_task`. |
| `runtime` recipe       | `evo config runtime set`            | `evo config runtime show`           | `--prepare`, `--before-run`, `--prefix`.               |
| `runtime_env`          | `evo env load/inherit-shell/clear`  | `evo env show`                      | Separate top-level command.                            |
| `execution_backend`    | `evo config backend <name>`         | `evo config backend show`           | `worktree`, `pool`, `remote`.                          |
| `current_eval_epoch`   | `evo infra event --breaking`        | `evo infra log`                     | Advances on breaking events; blocks cross-epoch comparisons until next run. |
| `comparison_blocked`   | `evo infra event --breaking`        | `evo config show --json`            | Cleared after a successful run.                        |
| `repo_root`, `workspace_dir`, `worktrees_dir`, `initialized_at` | (none) | `evo config show --json` | Init-only; do not edit.            |

Host runtime (orchestrator) lives in `meta.json`, not `config.json`. Read with
`evo host show`, set with `evo host set <claude|codex|cursor>`.

## Runtime Recipe

```bash
evo config runtime show [--json]
evo config runtime set \
  [--prepare "<cmd>"] \
  [--before-run "<cmd>"] \
  [--prefix "<cmd>"]
```

- `prepare` runs in the experiment workspace before benchmark/gates.
- `before-run` runs in the experiment workspace before each attempt.
- `prefix` prepends benchmark and gate commands, e.g. `uv run` or `pnpm exec`.
- Use this instead of hard-coding local paths like `{worktree}/.venv/bin/python`.

## Runtime Env

```bash
evo env show [--json]
evo env inherit-shell <on|off>
evo env load <path> --all
evo env load <path> --allow KEY1,KEY2
evo env clear
```

- Env values resolve fresh on each `evo run`.
- Config stores source metadata and key names, not secret values.
- Dotenv files are read by the orchestrator and injected into local/remote
  process env. Remote workers do not read your local `.env` file directly.
- Gates receive runtime env but not `EVO_*` artifact variables.


## Backends

```bash
evo config backend show [--json]
evo config backend worktree
evo config backend pool --workspaces /abs/slot-a,/abs/slot-b
evo config backend remote --provider <provider> [--provider-config k=v,...]
```

Per-experiment overrides are also available on `evo new`:

```bash
evo new --parent <id> -m "<hypothesis>" --backend remote --provider e2b
evo new --parent <id> -m "<hypothesis>" --remote modal
```

Provider auth and SDK packages are separate from benchmark runtime env.

## Experiment Lifecycle

```bash
evo new --parent <parent_id> -m "<hypothesis>"
evo run <exp_id> [--timeout <seconds>]
evo run <exp_id> --check [--timeout <seconds>]
evo done <exp_id> --score <float> [--traces <dir>] [--no-compare]
evo discard <exp_id> --reason "<why>" [--force]
evo prune <exp_id> [--reason "<why>"]
evo restore <exp_id>
evo gc
```

Lifecycle command rules:

- `evo discard` is for non-committed nodes (active/evaluated/failed).
  Refuses `committed` (use `evo prune` instead). Refuses `active` without
  `--force`. Refuses any node with non-discarded children.
- `evo prune` accepts `committed` or `evaluated` nodes. Marks the lineage
  exhausted; the result stays available for `evo restore` later.
  `--reason` is optional — omit it for routine round-N cleanups (a stderr
  warning notes the omission); pass one for one-off prunes whose context
  isn't obvious from the parent prune.
- `evo restore` reverts a prune or discard. Discarded nodes can be
  restored as long as the result hasn't been garbage-collected; if it
  has, the error message tells you where to find the saved diff.
- `evo gc` reclaims disk by freeing worktree directories from finished
  nodes. Run it periodically; not part of the experiment-iteration flow.

Outcomes:

- `COMMITTED`: score improved and gates passed; node is kept.
- `EVALUATED`: run completed but score regressed or gates failed; inspect and
  either retry the same node or discard it.
- `FAILED`: infra/runtime/benchmark crash; does not consume retry budget.

`evo done` is for externally scored runs only. Do not call it after a successful
`evo run`.

## Gates

```bash
evo gate add <node_id> --name <name> --command "<cmd>" [--phase pre|post]
evo gate list <node_id>
evo gate remove <node_id> --name <name>
evo gate check <node_id> [--timeout <seconds>]
```

- Gates are node-scoped policy and inherit to descendants.
- `--phase pre` runs the gate before the benchmark. Failure aborts the
  run with no benchmark spend. Use for checks decidable from the
  worktree alone (cheat detection, file-hash invariants, eval-data
  presence). Default is `post` (after benchmark; needs benchmark
  output to evaluate — e.g. score regression, output schema).
- `evo run exp_N` evaluates inherited gates: pre-gates before the
  benchmark, post-gates after.
- Gate pass/fail is exit-code based only. A command that prints a low
  score and exits 0 passes. Use tests or `--min-score` style gates that
  exit non-zero on regression.
- `evo gate check` runs all gates regardless of phase (forensic) and
  does not mutate node state.

## Inspection

```bash
evo status                                        # one-liner: metric, best, counts
evo scratchpad                                    # bounded state digest
evo show <exp_id>                                 # full state of one experiment
evo tree                                          # full tree (no bounding)
evo frontier [--strategy <kind>] [--params '<json>'] [--seed <n>]
evo path <exp_id>                                 # root-to-node chain
evo diff <exp_id> [other_id]                      # diff vs parent or between two
evo traces <exp_id> [task_id]                     # per-task trace detail
evo get <exp_id> [filename]                       # raw artifact read
evo log <exp_id> <filename>                       # raw log read
evo awaiting                                      # evaluated nodes pending decision
evo discards [--like "<text>"]                    # discarded nodes, searchable
evo annotations [--task <id>] [--exp <id>]        # per-experiment analyses
evo notes [--exp <id>] [--workspace] [--limit N]  # all notes, recent first
```

## Annotation & Notes

```bash
evo annotate <exp_id> [task_id] "<analysis>"      # per-experiment, attempt-time
evo set <exp_id> --note "<text>" [--tag <tag>]    # per-node, orchestrator
evo note "<text>"                                  # workspace-level, untied
evo notes [--exp <id>] [--workspace] [--limit N]   # read notes
evo infra event -m "<message>" [--breaking]        # record infra/strategy event
evo infra log [--limit N]                          # read recorded events
```

- Subagents annotate their own experiments before discard so the lesson
  outlives the worktree.
- Orchestrators attach per-node notes for cross-cutting findings tied to
  a specific node, and write workspace notes for round-level observations
  not tied to any one experiment.

## Loop control (for the /optimize orchestrator)

```bash
evo wait [--timeout SEC]      # block until any experiment reaches a
                              # terminal state (committed / evaluated /
                              # failed / discarded). Per-task traces and
                              # other in-flight writes are ignored.
                              # default 3600, capped at 3600 (1h).
                              # exit 0 with one-line summary on transition,
                              # 124 on timeout.

evo exit-optimize-mode        # halt the optimize-mode protocol for this
                              # session: clears the safety nudge flag,
                              # discards any `active` experiments, reports
                              # orphan `evo run` PIDs, and prints the
                              # remaining halt steps (host TaskStop for
                              # subagents — evo can't reach the host
                              # runtime — and any leftover stragglers).
```

`evo wait` is the primitive the orchestrator uses to block on subagent
results — replaces ad-hoc bash polling loops. `optimize_mode` is set
automatically when the user invokes `/evo:optimize` (or the host's
equivalent); no enter command needed.

While `optimize_mode` is active, the runtime enforces:
- File-mutation tools (Edit / Write / NotebookEdit, etc.) are denied on
  the 1st violation and every 5th after, with a banner reminding the
  orchestrator to spawn subagents instead.
- Bash commands that aren't `evo …`, a host-spawn (claude/codex/cursor-
  agent/opencode/hermes/pi/openclaw), or read-only inspection (git, ls,
  cat, find, grep, …) are denied on the same cadence.
- On `Stop` / `SubagentStop`, the orchestrator is re-prompted with a
  continuation instruction (use `evo wait` to block, plan the next
  round, etc.). The loop self-suppresses if no new experiment commits
  between two consecutive Stop fires (so the agent can actually stop
  when it's done).

## Mid-run directives

```bash
evo direct "<text>"                          # broadcast to engaged orchestrator sessions
evo direct <exp_id> "<text>"                 # targeted at a specific subagent
evo direct "<text>" --wait                   # block until any session acks (exit 3 on timeout)
evo direct "<text>" --wait --wait-timeout 30 # custom timeout in seconds (default 60)
evo direct-status <event_id>                 # show queue / delivery / ack state for one directive
evo ack <event_id>                           # called BY the agent after acting on a directive
```

Agents see directives as a banner in their context:

```
[EVO DIRECTIVE id=01HX7K…]
<text>
[END EVO DIRECTIVE — when done, run: evo ack 01HX7K…]
```

The banner is user-authoritative — treat its content as a new user turn,
override earlier constraints it contradicts, and run `evo ack <id>` after
acting on it so `evo direct-status` and `evo direct --wait` can report
success.

Fanout output prints `fanout=N, skipped_unengaged=M, skipped_subagent=K`.
Sessions that have never run an `evo` command (registered at SessionStart
but otherwise idle) are filtered out — only "engaged" sessions on
supported hosts receive broadcast directives.

## Workspace Ops

Use these when an experiment may be remote, or when the orchestrator gave you
an explicit experiment id:

```bash
evo bash --exp-id <exp_id> "<command>" [--cwd <path>] [--timeout <seconds>]
evo read --exp-id <exp_id> <path>
evo write --exp-id <exp_id> <path> [--content "<text>"]
evo edit --exp-id <exp_id> <path> --old "<old>" --new "<new>" [--replace-all]
evo edit --exp-id <exp_id> <path> --json-stdin
evo glob --exp-id <exp_id> "<pattern>" [--path <dir>]
evo grep --exp-id <exp_id> "<pattern>" [--path <dir>]
```

`--exp-id` is required by design. Concurrent subagents may own different
remote containers; there is no safe default active experiment.

For local worktree/pool backends, native file tools are fine if you use the
actual worktree path returned by `evo new`.

## Common Mistakes

- Do not hand-edit config JSON; use `evo config ...`, `evo env ...`, or
  dashboard settings.
- Do not create `mktemp` validation wrappers; use `evo run --check` or
  `evo gate check`.
- Do not assume `.venv`, `node_modules`, caches, or downloaded assets exist in
  experiment worktrees. Use `evo config runtime`.
- Do not copy `.env` into worktrees or sandboxes; use `evo env`.
- Do not register decorative gates that exit 0 on failure.
- Do not use native file tools against remote worktree paths; use workspace ops.
- Do not run from inside an experiment worktree; run `evo` from the main repo
  root unless using workspace ops with explicit `--exp-id`.

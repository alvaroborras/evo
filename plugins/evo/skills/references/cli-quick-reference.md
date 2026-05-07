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
evo config show [--json]
evo config set project-name "<name>"
evo config set target <path>
evo config set benchmark "<command>"
evo config set metric <max|min>
evo config set commit-strategy <all|tracked-only>
```

Do not hand-edit config files; use `evo config set` or the dashboard.

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
evo prune <exp_id> --reason "<why>"
evo restore <exp_id>
evo gc
```

Lifecycle command rules:

- `evo discard` is for non-committed nodes (active/evaluated/failed).
  Refuses `committed` (use `evo prune` instead). Refuses `active` without
  `--force`. Refuses any node with non-discarded children.
- `evo prune` accepts `committed` or `evaluated` nodes. Marks the lineage
  exhausted; the result stays available for `evo restore` later.
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
evo gate add <node_id> --name <name> --command "<cmd>"
evo gate list <node_id>
evo gate remove <node_id> --name <name>
evo gate check <node_id> [--timeout <seconds>]
```

- Gates are node-scoped policy and inherit to descendants.
- `evo run exp_N` evaluates gates inherited from the parent path.
- Gate pass/fail is exit-code based only. A command that prints a low score and
  exits 0 passes. Use tests or `--min-score` style gates that exit non-zero on
  regression.
- `evo gate check` validates gates without running the benchmark and does
  not mutate node state.

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
evo infra -m "<message>" [--breaking]             # infra/strategy events
```

- Subagents annotate their own experiments before discard so the lesson
  outlives the worktree.
- Orchestrators attach per-node notes for cross-cutting findings tied to
  a specific node, and write workspace notes for round-level observations
  not tied to any one experiment.

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

## Dispatch

```bash
evo dispatch run --parent <id> -m "<brief>" [--budget N] [--background]
evo dispatch wait [job_ids...] [--quiet]
evo dispatch list [--running] [--recent N]
evo dispatch status <job_id>
evo dispatch kill <job_id>
```

`dispatch` is async subagent spawning, available on `claude-code` only.
It is not background benchmark execution — `evo run` is always a blocking
evaluation transaction.

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

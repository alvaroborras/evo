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
  --per-exp-timeout <seconds> \
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
- `--per-exp-timeout` is the workspace default wall-clock cap for each `evo run`
  (seconds). Override per-call with `evo run --timeout N`. Pick based on what
  the benchmark actually costs; update later with
  `evo config set per-exp-timeout <seconds>`.

## Configuration

```bash
evo config show [--json]                           # full redacted dump
evo config get <field> [--json]                    # one field
evo config set <field> <value>                     # mutate one field
```

Settable / gettable fields:

```
project-name | target | benchmark | metric | commit-strategy
max-attempts | gate | frontier-strategy | default-orchestrator | task-skills
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
| `task_skills`          | `evo config set task-skills`        | `evo config get task-skills`        | Comma-separated evo skill name(s) a builder loads for this task's category (e.g. `finetuning`). Set by `discover`; read by prose subagents + workflow lanes. Empty = subagent protocol suffices. |
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
evo new --parent <parent_id> -m "<hypothesis>" [--from-artifact <exp[:label]>]
evo run <exp_id> [--timeout <seconds>] [--force]    # --timeout overrides workspace per-exp-timeout
evo run <exp_id> --check [--timeout <seconds>]      # same override semantics for check phase
evo abort <exp_id> [--timeout <seconds>] [--force]  # stop a mid-run experiment (driver + subprocess tree)
evo done <exp_id> --score <float> [--traces <dir>] [--no-compare]
evo discard <exp_id> --reason "<why>" [--failure-class build|eval|hypothesis] [--force]
evo prune <exp_id> [--reason "<why>"]
evo restore <exp_id>
evo gc
```

Lifecycle command rules:

- `evo run` refuses to start a second attempt while another attempt for
  the same `exp_id` has an alive driver PID (silent concurrent attempts
  multiply API spend by N). Pass `--force` to bypass when you know the
  prior driver is gone but its state wasn't reclaimed (e.g. recycled
  PID). Remote backend skips the guard — its resume logic handles
  `status=active` natively.
- `evo abort <exp_id>` SIGTERMs the driver process AND its descendant
  subprocess tree (the benchmark/training child and its children),
  captured before the parent is signalled so nothing orphans. If the
  driver doesn't exit within `--timeout` seconds (default 5), escalates
  to SIGKILL on whatever's still alive. `--force` skips the grace period.
  Workers fully detached into a new session (setsid/nohup) still escape —
  for those the recipe must honour SIGTERM. Use it to stop an experiment
  heading toward failure mid-run; a partial artifact written to
  `EVO_CHECKPOINT_DIR` survives for reuse. `abort` does not mutate graph
  state; classify + park the node afterward with `evo discard`.
- `evo discard` is for non-committed nodes (active/evaluated/failed).
  Refuses `committed` (use `evo prune` instead). Refuses `active` without
  `--force`. Refuses any node with non-discarded children. `--failure-class
  build|eval|hypothesis` records why it failed (routes reuse vs branch —
  see **Artifacts & reuse**); declared artifacts are preserved before the
  worktree is deleted.
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

## Artifacts & reuse

A benchmark/recipe can DECLARE reusable outputs so they survive a discard and
can seed later experiments. Category-agnostic: the artifact is whatever the
recipe produces (checkpoint, adapter, retrieval index, compiled prompt, …) —
never assume a specific name like `final_model/`.

- **Declare**: name the output in the benchmark result's `artifacts` field —
  `{"score": 0.42, "artifacts": {"checkpoint": "<path>"}}` (a `label→path`
  dict, or a list of `{name, path}`). Write durable outputs to
  `EVO_CHECKPOINT_DIR` (it lives under the experiment record, outside the
  worktree, so it survives between-attempt cleanup and discard).
- **Preserve**: `evo discard` copies declared worktree artifacts out before
  deleting the worktree, and records already-persistent ones (e.g. under
  `EVO_CHECKPOINT_DIR`) — both land in the discard manifest as reusable.
- **Classify**: `evo discard --failure-class build|eval|hypothesis` records
  why it failed: `build` (artifact step broke → fix + retry/resume), `eval`
  (artifact good, scoring/serving wrong → preserve + retest, no rebuild),
  `hypothesis` (ran clean, didn't help → branch a new direction). Stored on
  the node so the orchestrator can route reuse vs branch.
- **Reuse**: `evo new --parent <id> --from-artifact <exp[:label]>` seeds a new
  experiment from a preserved artifact. Its absolute path is exposed to the
  recipe as `EVO_SEED_ARTIFACT` — warm-start / eval-retest instead of
  rebuilding from scratch. Use `exp:label` when an experiment preserved more
  than one artifact.

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
evo wait [--for <target>] ... [--count N] [--timeout DUR]
         [--stall-threshold DUR] [--poll-interval DUR] [--json]
                              # --for is repeatable. Targets:
                              #   experiments       (workspace; new commit lands)
                              #   ideators          (workspace; new proposal lands)
                              #   process=<pid>     (PID exits)
                              #   log-growth=<path> (file stalls for --stall-threshold)
                              #   gpu-active        (GPU util rises above 0)
                              #   gpu-idle          (GPU util drops to 0)
                              # No --for = legacy default: watches BOTH experiments
                              # and ideators; wakes on whichever first.
                              # --count N (requires exactly one --for experiments|ideators)
                              # blocks until N additional items of that kind land.
                              # --timeout: duration string (60m, 2h) or int seconds;
                              # default 1h, max 24h.
                              # --stall-threshold default 2m. --poll-interval default 5s.
                              # --json emits structured exit (exit_reason, triggered_by,
                              # per-watch state) instead of the one-line summary.
                              # Exit: 0 on match, 124 on timeout, 2 on usage error.
                              # See `references/evo-wait.md` for full semantics + JSON shape.

evo autonomous on|off         # arm/disarm the stop-nudge (keep-going
                              # loop). Off by default. Run `on` when
                              # /optimize was invoked with `autonomous`.

evo subagents-only on|off     # arm/disarm the orchestrator-edit deny-gate.
                              # Off by default (orchestrator edits allowed).
                              # Run `on` when /optimize was invoked with
                              # `subagents-only`.

evo exit-optimize-mode        # halt the optimize-mode protocol for this
                              # session: clears optimize_mode + both opt-in
                              # flags (autonomous, subagents-only), discards
                              # any `active` experiments, reports orphan
                              # `evo run` PIDs, and prints the remaining
                              # halt steps (host TaskStop for subagents —
                              # evo can't reach the host runtime — and any
                              # leftover stragglers).
```

`evo wait` is the primitive the orchestrator uses to block on subagent
results — replaces ad-hoc bash polling loops. `optimize_mode` is set
automatically when the user invokes `/evo:optimize` (or the host's
equivalent); no enter command needed.

`optimize_mode` runs the protocol but enforces nothing on its own; the
two enforcement behaviors are separate opt-ins armed by command:

- **Stop-nudge** (after `evo autonomous on`): on `Stop` / `SubagentStop`,
  the orchestrator is re-prompted with a continuation instruction (use
  `evo wait` to block, plan the next round, etc.). The loop
  self-suppresses if no new experiment commits between two consecutive
  Stop fires (so the agent can actually stop when it's done). Without it,
  the loop does not force-continue across turn boundaries.
- **Orchestrator-edit deny-gate** (after `evo subagents-only on`):
  file-mutation tools (Edit / Write / NotebookEdit, etc.) are denied on
  the 1st violation and every 5th after, with a banner reminding the
  orchestrator to spawn subagents instead. Bash commands that aren't
  `evo …`, a host-spawn (claude/codex/cursor-agent/opencode/hermes/pi/
  openclaw), or read-only inspection (git, ls, cat, find, grep, …) are
  denied on the same cadence. Subagent sessions (with an `exp_id`) are
  never gated. Without it, orchestrator edits are allowed.

## Mid-run directives

```bash
evo direct "<text>"                          # broadcast to engaged orchestrator sessions
evo direct <exp_id> "<text>"                 # targeted at a specific subagent
evo direct "<text>" --wait                   # block until any session acks (exit 3 on timeout)
evo direct "<text>" --wait --wait-timeout 30 # custom timeout in seconds (default 60)
evo direct-status <event_id>                 # show queue / delivery / ack state for one directive
evo ack <event_id>                           # run BY the agent to confirm it received a directive
```

Agents see directives as a banner in their context:

```
[EVO DIRECTIVE id=01HX7K…]
<text>
[END EVO DIRECTIVE — run `evo ack 01HX7K…` to confirm you have received this message, then proceed]
```

The banner is user-authoritative — treat its content as a new user turn,
override earlier constraints it contradicts, and run `evo ack <id>` as soon
as you receive it (then proceed) so `evo direct-status` and `evo direct
--wait` can report success.

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

## Dashboard

`evo init` spawns a supervisor subprocess that owns the Flask dashboard's
lifecycle. The supervisor:

- Captures dashboard stdout/stderr to `.evo/dashboard.log` via a size-rotated
  handler (5 MB × 3 backups). When the dashboard dies, the log has the
  traceback.
- Respawns the dashboard on unexpected exit with capped exponential
  backoff (1, 2, 4, 8, 16, 30 seconds).
- Bails out after 5 rapid failures within 60s of startup and writes
  `.evo/dashboard.dead` with a one-line diagnostic. Tail
  `.evo/dashboard.log` for the underlying error.
- Logs its own activity to `.evo/supervisor.log` (rotated, 512 KB × 2).

State files under `.evo/`:

| File | Owner | Lifetime |
|---|---|---|
| `supervisor.pid` | supervisor | written on lock acquire; removed on clean shutdown |
| `supervisor.lock` | supervisor | held for lifetime; flock released on exit |
| `supervisor.log` | supervisor | append-only, rotated |
| `dashboard.pid` | supervisor | rewritten on each respawn; removed on clean shutdown |
| `dashboard.port` | `evo init` | actual bound port (may differ from requested if 8080 was busy) |
| `dashboard.log` | supervisor | dashboard stdout+stderr, rotated |
| `dashboard.dead` | supervisor | written only when backoff gives up; check this on "dashboard didn't come back" |

`_stop_dashboard` (run on `evo reset` etc.) signals the supervisor first
so it doesn't respawn the dashboard mid-stop. Cross-platform: POSIX
`setsid` via `start_new_session`, Windows
`DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP | CREATE_NO_WINDOW`.

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

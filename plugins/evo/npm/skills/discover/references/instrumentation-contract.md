# Instrumentation contract

Inline instrumentation is a file-and-env contract, not a library. `evo run` sets two environment variables, runs your benchmark command as a subprocess in whatever language it's written in, and reads back JSON files. Any language that can read an env var and write a JSON file satisfies it — implement it directly in the benchmark's own language.

The `inline_instrumentation.py` and `inline_instrumentation.js` helpers in this directory are ready-made reference implementations of this contract. For a Python or Node benchmark, paste one in. For any other language (Go, Rust, Ruby, Java, C++, shell, ...), implement the contract below in that language — it's ~10-15 lines.

## Environment (set by `evo run`)

| Variable | Meaning | If unset |
|---|---|---|
| `EVO_RESULT_PATH` | Absolute path to write the final result JSON | Print the result JSON to stdout instead |
| `EVO_TRACES_DIR` | Directory to write per-task traces into | Skip traces (score still works; the dashboard just shows less) |
| `EVO_EXPERIMENT_ID` | The experiment id, for stamping traces | Use `"unknown"` |

## Result JSON (required)

Write one JSON object — to `$EVO_RESULT_PATH`, or stdout if that's unset:

```json
{
  "score": 0.6,
  "tasks": {"t1": 0.8, "t2": 0.4},
  "started_at": "2026-05-30T09:00:00+00:00",
  "ended_at": "2026-05-30T09:01:00+00:00"
}
```

- `score` (number) is the only required field. `evo run --check` fails if the result is missing, empty, or has no numeric `score`.
- `tasks` (map of task id -> score) drives per-task selection strategies. Include it when the benchmark has discrete tasks.
- `started_at` / `ended_at` are optional ISO-8601 timestamps.
- Add `tasks_meta` only when a task's optimization direction differs from the benchmark's top-level `--metric`: `"tasks_meta": {"latency_ms": {"direction": "min"}}`.

**Exit code:** 0 on successful completion, even when the score is low. Exit non-zero only on infrastructure failure (missing data, import/compile error). A low score is data, not a crash. (Score-threshold *gates* are the exception — see SKILL.md step 8.)

## Per-task trace (recommended)

For each task, write `$EVO_TRACES_DIR/task_<id>.json` as the task finishes, so the dashboard streams progress live:

```json
{
  "experiment_id": "exp_0000",
  "task_id": "t1",
  "score": 0.8,
  "ended_at": "2026-05-30T09:00:30+00:00"
}
```

- `task_id` and `score` are the substance; `experiment_id` and `ended_at` are for display.
- Optional fields, include what helps debugging: `status` (a `"passed"`/`"failed"` display label — set it from the benchmark's own pass criterion, whatever that is for this score scale; the engine does not read it or impose a threshold), `summary`, `failure_reason`, `log`, `direction`.

## Atomic result publish (the one non-obvious rule)

Publish the result file with claim-then-rename, not a plain write:

1. Atomically create-exclusive the result path to claim it (`O_EXCL` / open mode `"wx"`). If it already exists, fail loudly — a second writer in the same attempt is a wiring bug, not something to overwrite.
2. Write the payload to `<result_path>.tmp`.
3. Rename `.tmp` over the result path.

This makes duplicate writers fail fast and ensures a crash mid-publish leaves an empty claimed file (which `load_result` treats as a failed run) rather than a half-written JSON that parses wrong. The `.py`/`.js` helpers show the exact calls. Per-task traces don't need this — they're write-once-per-id.

## Validate the wiring

The contract is language-agnostic, and so is the check. From the main repo root:

```bash
evo run <exp_id> --check
```

This runs the real benchmark command and asserts the result artifact exists, is non-empty, and carries a numeric `score` — regardless of what language produced it. It's the authoritative "is the wiring correct" test; passing it means evo can read your harness. Inspect the artifacts with `evo show <exp_id>`.

## Minimal shell reference

A complete inline implementation for a shell harness, to show how small the contract is:

```bash
# ... benchmark runs, computes per-task scores ...
mkdir -p "$EVO_TRACES_DIR"
printf '{"task_id":"t1","score":0.8}' > "$EVO_TRACES_DIR/task_t1.json"

# atomic publish of the result
score=0.8
if [ -n "$EVO_RESULT_PATH" ]; then
  ( set -o noclobber; : > "$EVO_RESULT_PATH" ) || { echo "result already claimed" >&2; exit 1; }
  printf '{"score":%s,"tasks":{"t1":%s}}' "$score" "$score" > "$EVO_RESULT_PATH.tmp"
  mv "$EVO_RESULT_PATH.tmp" "$EVO_RESULT_PATH"
else
  printf '{"score":%s,"tasks":{"t1":%s}}' "$score" "$score"
fi
```

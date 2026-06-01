# `evo wait` — synchronization primitive for the orchestrator

A bounded blocking call. Use it instead of `while true; do sleep N; tail; done` polling.

## Surface

```
evo wait [--for <target>] ... [--count N] [--timeout DUR]
         [--stall-threshold DUR] [--poll-interval DUR] [--json]
```

`--for` is repeatable. Each `--for` adds one watch target; the wait returns on the first matching condition.

| `--for` target | Watches for | Workspace required |
|---|---|---|
| `experiments` | a new committed experiment lands in the workspace's graph | yes |
| `ideators` | new proposals land in `.evo/run_<id>/ideator/proposals.jsonl` | yes |
| `process=<pid>` | the PID exits | no |
| `log-growth=<path>` | the file stops growing for `--stall-threshold` (the file is "stalled") | no |
| `gpu-active` | GPU utilization rises above 0 | no |
| `gpu-idle` | GPU utilization drops to 0 | no |

With no `--for`, `evo wait` watches BOTH `experiments` and `ideators` (legacy default; preserved for backwards compatibility). `--count N` (requires `--for experiments` or `--for ideators` exactly once) blocks until N additional items of that kind land.

## Options

- `--timeout DUR` — hard ceiling on the wait. Duration string (`60m`, `2h`, `30s`) or integer seconds. Default 1h. Max 24h.
- `--stall-threshold DUR` — how long a log file or GPU must show no progress before the wait declares it "stalled". Default 2m.
- `--poll-interval DUR` — how often to recheck conditions. Default 5s.
- `--json` — emit structured JSON on stdout instead of the legacy one-line summary.

## Exit codes

- `0` — at least one `--for` condition matched (the legacy commit-arrival path also returns 0).
- `124` — timeout reached without any condition matching.
- `2` — argparse / usage error (unknown `--for` value, etc.).

## JSON output (`--json`)

```json
{
  "exit_reason": "process-exited"
                | "log-stalled"
                | "gpu-active" | "gpu-idle"
                | "experiments-arrived" | "ideators-arrived"
                | "timed-out",
  "waited_seconds": 372,
  "process": {"pid": 1234, "alive": false, "exit_code": null},
  "log":     {"path": "...", "size": 12345,
              "grew_in_last_window": false, "last_line": "..."},
  "gpu":     {"util": 0, "mem_used_mb": 38000},
  "triggered_by": {"kind": "process", "pid": 1234, "summary": "pid 1234 no longer alive"}
}
```

`exit_code` is `null` for `process=` watches because `evo wait` is rarely the pid's parent; liveness is via `kill(pid, 0)`, the exit code can't be captured without being the parent. If `nvidia-smi` is not on PATH, `gpu` is reported as `{"note": "nvidia-smi unavailable"}` and gpu-* watches are skipped (other watches still apply).

## Examples

```bash
# wait until a training process exits, with a 90-minute ceiling
evo wait --for process=$TRAIN_PID --timeout 90m --json

# wait until the train log stops growing for 5 minutes OR the GPU goes idle,
# whichever first; 2-hour ceiling
evo wait --for log-growth=/path/to/train.log \
         --for gpu-idle \
         --stall-threshold 5m --timeout 2h --json

# block the optimize loop for up to 3 new ideator proposals or 15 minutes
evo wait --for ideators --count 3 --timeout 900

# block until ANY of: subagent commits, ideator proposes, or 10-minute cap
evo wait --timeout 600
```

## Use this instead of polling

The pattern this replaces:

```bash
# anti-pattern
while true; do
  tail -n 5 $LOG
  sleep 60
done
```

Problems with the anti-pattern: blocks the agent indefinitely if the process dies (the log stops growing, the loop interprets "no delta" as "still running"); no exit-reason signal; no bounded timeout; can't be combined with multiple watch conditions.

`evo wait` is the bounded, structured replacement. The polling-discipline sections in `evo:discover` and `evo:optimize` point here.

## Multiple `--for` semantics

Multiple `--for` flags combine with OR — the wait returns on the FIRST matching condition. The returned `exit_reason` and `triggered_by` identify which condition fired. Order of checks per poll iteration: workspace targets first (experiments, ideators), then process, then log, then GPU.

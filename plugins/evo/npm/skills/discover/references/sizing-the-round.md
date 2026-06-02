# Sizing the round

How to pick **round width** (`subagents`) and **per-branch depth** (`budget`) for a round. Apply this before the first round and re-check it if the backend or benchmark changes.

The governing idea: **round width is resource-bound.** Spawning N subagents means up to N benchmark runs executing at once. If one run already saturates the binding resource, more runs don't go faster — they contend, thrash, or OOM. So:

```
width = min(backend_ceiling, resource_width)
```

## 1. Backend ceiling

Check `evo config backend show`.

- **worktree** (default): every experiment runs on the *same machine*. The ceiling is what that one machine can run concurrently without contention — set by `resource_width` below, not by a fixed number.
- **pool**: hard ceiling = slot count (`evo workspace status`). Exceeding it makes later `evo new` calls fail with `PoolExhausted`. Never set width above the slots.
- **remote**: each experiment runs in its own container, so the local machine isn't the limit. Ceiling is provider quota / concurrency cap / cost tolerance.

## 2. Resource width — what one benchmark run saturates

Read `.evo/project.md`'s resource-profile line first (discover records it). If it's missing or thin, infer from the benchmark command, the target's imports, and a quick probe (`nvidia-smi`, core count, the benchmark's own docs). Then size to whatever one run consumes:

| Binding resource of one run | Round width on a shared machine |
|---|---|
| Exclusive accelerator — needs the whole GPU/TPU, or a pinned device | **1** with one device; **K** with K devices that can be pinned per worktree |
| Memory-heavy | `floor(total_RAM / per_run_peak)` with headroom |
| Exclusive port, singleton service, shared DB, or a shared mutable fixture | **1**, unless the harness parameterizes that resource per worktree |
| External API rate limit or real $-per-run | cap width to stay under the limit / within budget |
| CPU-light, in-memory, fully isolated | wider — up to core count, capped ~5–8 to keep the round legible |

When a run needs an exclusive resource, serializing benchmark *execution* (width 1) is correct even though the *edits* are independent — on the worktree backend `evo run` executes the benchmark in-place, so concurrent `evo run` means concurrent benchmark processes on that one resource.

**Latency / timing / throughput benchmarks deserve a per-workspace judgment call, not a fixed answer.** When the metric IS time, jitter, or rate, sibling-process CPU/cache/memory-bandwidth pressure can BIAS the measurement (not just add noise) — and the orchestrator may then promote a "winner" that's just a contention artifact. But this doesn't always happen, and harness softeners (warmup, min-over-N batches, outlier rejection) reduce the risk. Things to weigh case-by-case before picking width:
- How big is the optimization's expected effect vs. the variance the harness reports under parallel runs? If the effect is much larger than measurement jitter, modest parallelism is fine.
- How much of the benchmark's wall-clock is the actual timed section? Long edit/compile phases overlap safely; only the timed section needs isolation.
- Can a winner be cheaply re-confirmed solo before being promoted? If yes, going wider for exploration with a solo-confirm gate is reasonable.
- Does the harness already filter contention (e.g., reject batches with outlier jitter)?

If unsure, start narrower and widen once you've confirmed measurements are stable. Width 1 is the safe default for *unknown* timing-sensitive benchmarks; don't apply it reflexively when the workspace has data that says otherwise.

## 3. Depth — `budget` (iterations per subagent within its branch)

Depth trades exploration against spend, and keys off cost per run, not concurrency:

- Cheap, fast, deterministic benchmark → larger budget; let a promising branch iterate several times before the orchestrator re-plans.
- Expensive, slow, or noisy benchmark → smaller budget; re-plan sooner so spend tracks signal. For noisy benchmarks, deep single-branch iteration over-fits to lucky runs.
- ~5 is a reasonable midpoint when nothing argues otherwise.

## 4. Default and override

- **Unknown profile, light isolated run** → fall back to width 5, budget 5.
- **Unsure, but a shared exclusive resource is plausible** (the benchmark touches a GPU, a port, a DB) → serialize (width 1). Under-subscribing wastes a little wall-clock; over-subscribing corrupts results or crashes the round.
- **The user gave an explicit value** (`/optimize subagents=N budget=N`, or said it in plain language) → honor it, over the heuristic. They know their hardware.

State the width/budget you chose and the one-line reason in your opening message, so the sizing is visible (e.g. "width 1 — benchmark needs the single GPU; budget 6 — runs are cheap and deterministic").

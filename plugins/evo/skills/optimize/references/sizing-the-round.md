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
| **Latency / timing / throughput measurement** — the metric IS time, jitter, or rate | **1**. Sibling-process CPU/cache/memory-bandwidth pressure corrupts the measurement even when scoring code is "light." Harness softeners (warmup, min-over-N batches, outlier rejection) reduce noise but don't eliminate contention BIAS — the contended measurement is systematically biased up, not just noisier. A "winner" found at width > 1 can be a contention artifact. |
| External API rate limit or real $-per-run | cap width to stay under the limit / within budget |
| CPU-light, in-memory, fully isolated, **measurement not timing-based** | wider — up to core count, capped ~5–8 to keep the round legible |

When a run needs an exclusive resource, serializing benchmark *execution* (width 1) is correct even though the *edits* are independent — on the worktree backend `evo run` executes the benchmark in-place, so concurrent `evo run` means concurrent benchmark processes on that one resource.

**The CPU-light row is the one most often misapplied.** Before picking it, ask: "is what I'm measuring corruptible by other processes running on the same machine?" If yes (latency, throughput, energy, jitter, memory-bandwidth-bound throughput, GC-pause-sensitive work, anything where the metric reflects shared-hardware state) → use the latency row, NOT the CPU-light row.

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

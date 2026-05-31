---
name: ideator
description: Generate new experiment proposals by cross-cutting analysis of the experiment graph plus targeted literature/web scans. Spawned in parallel as multiple briefs (failure analysis, literature, frontier extrapolation) that reconcile via append-only proposals file. Use when the user invokes /evo:ideator, the optimize loop wants fresh directions after a stall, or after every N committed experiments.
argument-hint: "--brief <failure_analysis|literature|frontier_extrapolation> [--k <count>]"
evo_version: 0.5.0-alpha.2
---

# Ideator

Internal procedure for `evo:ideator`. Generates ranked experiment proposals that the orchestrator reads at its next `evo new` decision.

Unlike the verifier (which judges one experiment in front of it), the ideator does **cross-cutting analysis across the full experiment graph** plus targeted external research.

The ideator is designed to run in PARALLEL: the orchestrator spawns multiple ideator subagents with different briefs, each contributes proposals to a shared append-only file, and the orchestrator reconciles them at decision time.

## Host conventions

Same as other evo skills. Uses `evo` CLI, file reads, and (for the literature brief) web fetch / web search tools provided by the host. Hosts without web tools can still run the failure-analysis and frontier-extrapolation briefs.

## Briefs

Each ideator invocation takes ONE brief argument. The orchestrator picks which briefs to spawn based on what state the run is in.

### `--brief failure_analysis`

Read the last N discarded or failed experiments. Find shared causes the orchestrator may have missed.

Inputs:
- `evo discards` -- list of discarded experiments with their `discard_reason`
- For each, `evo show <id>` and the per-experiment `attempts/<n>/benchmark_err.log`, `outcome.json`, `gate_<name>.log`

Procedure:
1. Group by failure mode (OOM, dependency error, API drift, timeout, gate fail, etc.).
2. For each cluster of >=2 failures with the same root cause, write one proposal: "before more experiments are run, fix <root cause>". This is meta-work, not a new training direction -- the orchestrator may decide to spawn a maintenance subagent rather than a new `evo new`.
3. For each cluster, also write one proposal: an experiment that AVOIDS the failure mode by a clean alternative path (e.g., "tried LoRA r=64 three times, all OOM -- propose LoRA r=16 with gradient_checkpointing").

Output: 0-5 proposals depending on how many distinct failure clusters exist.

### `--brief literature`

Targeted web/arxiv scan for techniques relevant to the workspace's domain, filtered against what's been tried.

Inputs:
- Workspace `project_name` and `.evo/project.md` for the domain
- Full `evo graph` for the list of tried hypotheses
- Web tools (`WebFetch`, `WebSearch`) from the host

Procedure:
1. Read `.evo/project.md` and `evo show root` for the optimization target (e.g., "Qwen3-4B-Base on AIME 2025 via post-training").
2. Query arxiv-listings / recent blog posts / paper summaries for the most recent techniques in the domain (last 6-12 months). Two or three search queries, not exhaustive.
3. For each technique found, check against the workspace graph: has anything along these lines already been tried? Skip if yes.
4. Write 2-4 proposals, each with: technique name, paper/post link, mechanism summary, expected cost (small/medium/large), specific configuration to try.

Output: 2-4 proposals. The orchestrator weighs these against in-graph proposals -- novelty alone doesn't beat a strong frontier extrapolation.

### `--brief frontier_extrapolation`

Of the committed experiments, find the steepest score gradient and propose deeper variants.

Inputs:
- `evo frontier` -- the Pareto-optimal committed experiments
- `evo path <best_committed_id>` -- the root-to-best lineage
- For each lineage step, the hypothesis + score + diff vs parent

Procedure:
1. Compute per-step score deltas along the best path. Identify the step(s) with the largest positive delta -- those represent productive directions.
2. For each productive direction, propose 1-2 deeper variants:
   - **Scale**: same technique, more of it (e.g., went from LoRA r=8 to r=16 with +2%; propose r=32 and r=64)
   - **Combine**: same technique applied to a complementary axis (e.g., LoRA on attention worked; propose adding LoRA on MLP)
   - **Refine**: same technique with hyperparameter sweep around the winning config
3. Avoid proposing variants that are already in the graph (check children of the productive step).

Output: 2-3 proposals. Frontier-extrapolation proposals are usually higher-confidence than literature proposals -- they're grounded in observed gradients.

## Output format

All briefs write to a shared append-only file: `.evo/run_<run_id>/ideator/proposals.jsonl`.

One JSON object per line:

```json
{
  "generated_at": "2026-05-31T18:30:00+00:00",
  "brief": "frontier_extrapolation",
  "based_on_experiments": ["exp_0003", "exp_0005"],
  "hypothesis": "<one-sentence specific proposal>",
  "mechanism": "<why this should help, 1-3 sentences>",
  "expected_cost": "small|medium|large",
  "expected_score_uplift": "<range, e.g. +2-5% absolute>",
  "data_needed": ["dataset id 1", "dataset id 2"],
  "differentiation_from_existing": "<what makes this distinct from what was already tried>",
  "source": "<url if literature; null otherwise>"
}
```

Append-only: the file may accumulate hundreds of proposals across a long run. The orchestrator filters on `generated_at` (newer than last check) and `differentiation_from_existing` (not duplicative).

## Concurrency and reconciliation

The orchestrator typically spawns three parallel ideator subagents:

```python
# In the orchestrator's evo:optimize loop, on stall or every N=5 commits:
Task(subagent_type="general-purpose",
     description="evo:ideator failure analysis",
     prompt=<ideator skill body with --brief failure_analysis filled in>)
Task(subagent_type="general-purpose",
     description="evo:ideator literature",
     prompt=<ideator skill body with --brief literature filled in>)
Task(subagent_type="general-purpose",
     description="evo:ideator frontier",
     prompt=<ideator skill body with --brief frontier_extrapolation filled in>)
```

Each runs ~5-10 min, independently, in its own context. None blocks the orchestrator's main loop -- the orchestrator continues normal optimize work (spawning experiment subagents, reading results) and checks the proposals file at its next `evo new` decision.

When the orchestrator picks the next experiment:

1. Read `proposals.jsonl`, filter for `generated_at > last_read_at`
2. Discard proposals whose `differentiation_from_existing` is weak (the proposed config is already in the graph, or differs only trivially)
3. Rank remaining by `expected_score_uplift` × confidence (frontier_extrapolation > failure_analysis > literature, all else equal)
4. The top 1-2 proposals get spawned as the next `evo new`s; the rest stay in the queue

The proposals file is the reconciliation surface -- no other coordination between parallel ideators.

## What the ideator deliberately does NOT do

- **Doesn't run experiments** -- proposes them. Execution is the subagent's job.
- **Doesn't modify the graph or `.evo/config.json`** -- only writes to `proposals.jsonl`. The orchestrator decides what to act on.
- **Doesn't verify experiments after the fact** -- that's the verifier's job.
- **Doesn't enforce a maximum proposal count** -- generates whatever the briefs find. The orchestrator filters at consumption time.

## When the orchestrator should spawn ideators

The optimize skill body specifies the cadence. Common triggers:

- **Periodic**: every N=5 committed experiments since the last ideator run
- **Stall**: `evo frontier` hasn't moved (best score unchanged) in M=3 consecutive commits
- **Failure cluster**: M=3 consecutive discards with related root causes (the failure_analysis brief in particular)
- **User-triggered**: the user invokes `/evo:ideator` directly when they want fresh ideas mid-run

## Examples

### Frontier extrapolation finds a scaling direction

```bash
evo:ideator --brief frontier_extrapolation --k 2
# Reads frontier -- best path is root -> exp_0002 (LoRA r=8 +1.2%) -> exp_0005 (LoRA r=16 +3.1%)
# Identifies "LoRA rank scaling" as productive direction (delta growing with rank)
# Writes 2 proposals:
#   1. LoRA r=32, expected +1-3% over r=16, medium cost
#   2. LoRA r=64 with gradient_checkpointing (avoid OOM), expected +1-4%, medium cost
```

### Literature surfaces an untried technique

```bash
evo:ideator --brief literature --k 3
# Reads workspace project_name = "AIME2025-Qwen3-4B"
# Searches: "math reasoning post-training 2026", "Qwen3 finetuning verifier reward"
# Finds: GRPO with verifier reward (AIME answers are integers; perfect for verifiable RL)
# Cross-checks graph: no RL attempts in the workspace yet
# Writes 1 proposal:
#   1. GRPO with verifier reward, mechanism: integer-answer verifier as binary reward,
#      large cost (~3h), expected +5-15% absolute, source: <arxiv link>
```

### Failure analysis catches a shared root cause

```bash
evo:ideator --brief failure_analysis
# Reads last 5 discards: exp_0001, exp_0002, exp_0004 all OOM at step 1
# All share: LoRA r >= 64 with full attention layers + gradient_checkpointing off
# Writes 2 proposals:
#   1. (meta) "Before more experiments: add gradient_checkpointing=True to the
#       baseline train.py template so future experiments inherit it"
#   2. (alternative) "LoRA r=32 with gradient_checkpointing=True -- avoids the
#       OOM cluster while keeping most of the expressiveness gain over r=16"
```

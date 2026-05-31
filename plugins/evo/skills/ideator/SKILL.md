---
name: ideator
description: Internal protocol for the evo experiment-ideator. Loaded by an ideator subagent spawned from /optimize when fresh directions are needed (stall, failure cluster, or every N committed experiments). Each spawn runs ONE brief (failure_analysis / literature / frontier_extrapolation); all briefs append to a shared proposals file the orchestrator reconciles. Not user-invokable; the orchestrator does not load it either.
argument-hint: "--brief <failure_analysis|literature|frontier_extrapolation> [--k <count>]"
evo_version: 0.5.0-alpha.1
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

Multi-source web/research scan for techniques relevant to the workspace's domain, filtered against what's already been tried in this run.

This brief is the closest thing in evo to a generic research agent: it acts like a focused web search that surfaces *actionable* proposals (technique + code + config) rather than just a reading list. Borrowed structurally from systems like HuggingFace's ml-intern -- multi-source, separate-context, multi-tool.

Inputs:
- Workspace `project_name`, `.evo/project.md` for domain context
- `evo graph` (full) for the list of tried hypotheses + their outcomes
- Web tools: `WebSearch`, `WebFetch` (or your host's equivalents)

Procedure:

1. **Frame the search.** Read `.evo/project.md` and `evo show root` to extract: the optimization target, the base model / system being optimized, the metric. Write a one-sentence brief for yourself: "I'm looking for techniques to improve <target> on <metric>, given that <prior approaches> have already been tried (with outcomes ...)."

2. **Scan multiple sources in parallel** -- different sources surface different kinds of signal:

   | Source | Query shape | Surfaces |
   |---|---|---|
   | **arXiv** | `site:arxiv.org [domain] [recent month]` | Newest techniques; methodology depth |
   | **HuggingFace Papers** | `site:huggingface.co/papers [domain]` | Curated; community discussion + replication notes |
   | **HuggingFace Hub** | `site:huggingface.co/datasets [domain]` or `models [base]` | Available data/checkpoints to skip data prep |
   | **GitHub code search** | `site:github.com [technique keyword] [base model]` | Working implementations; whether technique has been built |
   | **GitHub issues** | `site:github.com/issues [technique] improvement OR worked` | Practitioner anecdotes ("LoRA r=64 gave +5% on my task") |
   | **GitHub PRs** | `site:github.com/pulls [framework] [technique]` | Active in-flight work; pre-release techniques |
   | **Recent blog posts** | unfiltered web search, last 6 months | Honest writeups about what actually worked |

   Aim for 5-8 total searches across sources. Don't exhaustively crawl any one source; this is signal-gathering, not a literature review.

3. **Due diligence on each candidate.** Before turning a finding into a proposal:
   - **Paper sources**: WebFetch the abstract + main results. Confirm the claimed improvement is in the headline results, not buried in an appendix. Note sample size + benchmark used.
   - **GitHub repos**: WebFetch the README. Check: when was the last commit, are there open issues complaining about it not working, does the README claim quantitative improvements with reproducible config.
   - **Issues/PRs**: read the actual thread, not just the title. Look for "I confirmed this" / "didn't reproduce" follow-ups.
   - Discard candidates that look promising but lack a concrete config or runnable code -- proposals need to be actionable.

4. **Filter against the workspace graph.** For each surviving candidate, check `evo graph` for any prior experiment with a similar hypothesis (use `evo discards --like "<keyword>"` for fast string match, then read `evo show <id>` for the full hypothesis). Skip duplicates and trivial variations. The orchestrator's reconciler does a second-pass dedup, but the ideator should catch the obvious ones to avoid wasted proposals.

5. **Rank surviving candidates** by:
   - **Has-code signal**: working implementation > paper-only > anecdote-only
   - **Replication signal**: multiple independent sources reporting it > single source
   - **Specificity**: precise config (concrete hyperparams, named datasets, runnable commands) > vague high-level technique names
   - **Recency**: newer often better for post-training, but a 6-month-old paper with a working repo beats a 1-week-old paper with no code

6. **Write 2-4 proposals** -- top of the ranking. Each includes the full provenance so the orchestrator can verify before spending compute (see Output format below).

Output: 2-4 proposals. The orchestrator weighs these against in-graph proposals -- novelty alone doesn't beat a strong frontier extrapolation, but a well-cited technique with working code and an unexplored direction often beats both.

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
  "brief": "frontier_extrapolation|failure_analysis|literature",
  "based_on_experiments": ["exp_0003", "exp_0005"],
  "hypothesis": "<one-sentence specific proposal>",
  "mechanism": "<why this should help, 1-3 sentences>",
  "expected_cost": "small|medium|large",
  "expected_score_uplift": "<range, e.g. +2-5% absolute>",
  "data_needed": ["dataset id 1", "dataset id 2"],
  "differentiation_from_existing": "<what makes this distinct from what was already tried>",

  "sources": [
    {"kind": "paper", "url": "https://arxiv.org/abs/...", "claim": "<headline result quoted>"},
    {"kind": "repo",  "url": "https://github.com/.../...", "last_commit": "2026-04", "stars": 1240,
     "claim": "<README quote on improvement / config>"},
    {"kind": "issue", "url": "https://github.com/.../issues/...", "claim": "<practitioner's reported delta>"},
    {"kind": "blog",  "url": "https://...", "claim": "<key sentence>"}
  ],
  "confidence_signals": {
    "has_runnable_code": true,
    "replicated_across_sources": 2,
    "specificity": "high|medium|low",
    "recency_months": 4
  }
}
```

`sources` and `confidence_signals` are only required for the `literature` brief. `failure_analysis` and `frontier_extrapolation` derive their evidence from in-graph data (`based_on_experiments` is sufficient provenance) and may leave them null. The orchestrator uses `confidence_signals` to weight proposals at reconciliation -- a paper-only finding with `replicated_across_sources=1, has_runnable_code=false` ranks below a frontier-extrapolation proposal grounded in observed gradients.

Append-only: the file may accumulate hundreds of proposals across a long run. The orchestrator filters on `generated_at` (newer than last check) and `differentiation_from_existing` (not duplicative).

## Concurrency and reconciliation

The orchestrator spawns one ideator subagent per brief using its host's parallel-subagent tool (same mechanism as experiment subagents -- see `evo:optimize` step 5 for the per-host spawn commands). The orchestrator does NOT read this skill -- it only knows the skill name and which brief to pass. Each spawned subagent loads the skill itself as its first action.

Each subagent's prompt MUST start with the literal sentence:

> "First, load and follow the **evo ideator skill** (named `ideator` under the evo plugin in your host's skill registry — use your host's skill loader, not a filesystem path) with args `--brief <failure_analysis|literature|frontier_extrapolation>`. Append all your proposals as JSONL lines (single final write at the end) to `.evo/run_*/ideator/proposals.jsonl`, then exit."

Substitute the actual brief value when spawning. Each spawn gets exactly ONE brief.

Each runs ~5-10 min, independently, in its own context. The orchestrator chooses whether to block on them or fire-and-continue -- see the optimize skill's step 6b for the policy. In either case, the orchestrator blocks/checks via `evo wait`:

```bash
# Block until N ideator proposals have landed since wait started (caps at --timeout)
evo wait --for ideators --count 3 --timeout 900
# Exit 0 = ready; exit 124 = timeout (proposals may be partial -- check the file)
```

`evo wait` watches `proposals.jsonl` for new lines. Each ideator's terminal action is appending its proposals; so line growth IS the completion signal. No separate done-file or session-id bookkeeping needed.

When the orchestrator picks the next experiment:

1. Read `proposals.jsonl`, filter for `generated_at > last_read_at`
2. Discard proposals whose `differentiation_from_existing` is weak (the proposed config is already in the graph, or differs only trivially)
3. Rank remaining by `expected_score_uplift` × confidence (frontier_extrapolation > failure_analysis > literature, all else equal)
4. The top 1-2 proposals get spawned as the next `evo new`s; the rest stay in the queue

The proposals file is the reconciliation surface -- no other coordination between parallel ideators.

## Append-at-end discipline (recommended)

Each ideator subagent SHOULD hold its proposals in memory while running, then append ALL of them to `proposals.jsonl` in a SINGLE FINAL WRITE at the end of its work, rather than streaming them as they're produced.

Reasons:
- **Failure atomicity.** A crashed mid-stream ideator leaves ambiguous partial output: did 2 proposals arrive because the ideator finished early with 2 ideas, or because it crashed after writing 2? Single-write-at-end means "if you see proposals from this ideator, the ideator finished successfully" -- the orchestrator can trust each line.
- **Per-ideator atomicity for the reconciler.** The orchestrator's reconciliation step at brief-writing dedupes against the graph. If proposals stream out, the reconciler may see (and act on) proposal 1 before proposal 2 lands -- and proposal 2 might supersede proposal 1.

`evo wait --for ideators --count N` counts NEW LINES added to `proposals.jsonl` since wait started, NOT ideator completions. So if you spawn 3 ideators that each produce ~3 proposals, `--count 9` waits for all of them to finish; `--count 1` returns as soon as any ideator finishes its single final write (regardless of how many proposals were in it). Pick N based on what you actually need from the round.

Use atomic append (write to a temp file, then `cat tmp >> proposals.jsonl`) if your host's file tools don't guarantee multi-line write atomicity.

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

### Literature surfaces an untried technique (with full provenance)

```bash
evo:ideator --brief literature
# Reads workspace project_name + .evo/project.md to identify target + base system
# Searches across sources in parallel (5-8 total queries):
#   - arxiv: "<target-domain> <related-technique> <recent year>"
#   - HF Papers: "site:huggingface.co/papers <target-domain> <recent year>"
#   - GitHub code: "site:github.com <technique-keyword> <base-framework> implementation"
#   - GitHub issues: "site:github.com <technique> improvement OR worked"
#   - blog: "<technique> <target-domain>" (unfiltered, last 6mo)
# Finds N candidates across sources; counts how many sources each shows up in
# Due diligence per candidate:
#   - WebFetch paper abstract: confirms claimed delta is in headline results
#   - WebFetch top GitHub repo README: last-commit recency, claimed config, stars
#   - WebFetch supporting issue/PR threads: practitioner replication
# Cross-check evo graph: skip candidates whose hypothesis matches a prior experiment
# Ranks surviving candidates by has_runnable_code > replication > specificity > recency
# Writes 2-4 proposals with full sources=[...] and confidence_signals={...}
```

## Generic web-research mode

The literature brief doubles as a generic web-research agent when the orchestrator wants targeted external signal without spawning a full ideation round. Invoke it directly with a focused query:

```bash
evo:ideator --brief literature
# Pass a focused query via the orchestrator's brief override mechanism, e.g.
# "Search specifically for: how others handle <specific failure mode> when training
#  <base model> on <data type>. Focus on sources from the last 3 months."
```

The procedure is the same -- multi-source scan, due diligence, ranked proposals -- but the orchestrator narrows the brief to a specific question rather than the broad "what could we try next."

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

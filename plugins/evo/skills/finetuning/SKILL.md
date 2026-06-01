---
name: finetuning
description: This skill should be used when picking or diagnosing a training move (SFT, LoRA, DPO/KTO/ORPO, RFT, GRPO/PPO/RLOO, RLHF), or when the user mentions fine-tuning, post-training, training recipe, reward design, or weight updates. Decision tree by reward shape, smoke-run gate, three failure diagnostics, five false-progress patterns. Provider recipes and I/O contract in references/.
evo_version: 0.4.4-alpha.3
---

# Finetuning

Priors, not rules. Only firm guardrails: held-out eval you never train on, no leakage, trust evo's recorded numbers over the run's self-report. Override anything else against the gate.

## Pick the technique by reward shape

Decide on the reward first, technique second. Choosing the comfortable technique over the matching one is the most common failure.

| Reward shape | Technique |
|---|---|
| Verifiable (exact match, unit tests, parser-decidable) | **RL** (GRPO / RLOO / PPO) — reward includes format, so the model learns to emit verifier-acceptable shape |
| Preference pairs (chosen vs rejected) | **DPO / KTO / ORPO** — cheaper than full RL, no rollouts |
| Demonstrations only (curated traces, chat data) | **SFT** — install format/tone/capability the base lacks |
| Have a scorer + want SFT stability | **RFT** — sample, filter by reward, SFT on survivors |

"SFT-then-RL" is not a law. For a competent base model on a verifiable benchmark, RL-from-base often beats SFT-then-RL end-to-end.

## Before committing the budget: smoke-run

Run the full pipeline on ~10 examples for ~1 minute. Must produce: a checkpoint the benchmark can load AND a non-zero eval on a held-out item. If not, the recipe is broken — fix it, don't scale it. dtype mismatch, tokenizer/template drift, OOM at this batch size, empty artifacts dir despite falling loss — all surface on 10 examples. Running longer doesn't surface them differently, just more expensively.

## Three diagnostics

**Stuck at 0 on a verifiable benchmark after 2+ SFT runs.** Technique class is wrong, not the recipe. Pivot to RL with the verifier as reward; SFT loss can be healthy while the model emits unparseable output.

**Base scores below random before any training (knowledge-heavy benchmark).** Model lacks the knowledge, not the format. Post-training shapes existing knowledge; it does not install new knowledge. Right axis: continued pre-training on a domain corpus, distillation from a stronger model that has the knowledge, or retrieval-augmented inference.

**`delta <= 0` across several committed train moves.** Method exhausted on this target. Try a different method, change the data, or improve the harness instead of the weights.

## What never counts as progress

Five patterns produce a number going up without the model improving. See `references/false-progress.md` for examples + detection.

1. Training on the held-out set — direct or transitive (public instruction datasets sometimes contain eval-derived items).
2. Embedding eval items in "synthetic" data, even renamed or paraphrased.
3. Generating training data conditioned on per-eval-item failure logs.
4. Submitting a checkpoint you didn't train (off-the-shelf instruct model; parent's checkpoint unchanged).
5. Training a different objective than the verifier scores.

The verifier should catch these. List is here so the train move doesn't produce them.

## Surviving session compaction

Write the dataset URL, method choice, user-imposed constraints, and hyperparameters you converged on to `methodlog.md` in the experiment worktree. One line each. Re-read after any context reset, before the next train move. Prevents silent dataset swaps between experiments and re-running ablations.

## Numbers that matter (in order)

1. A reward you trust — verifiable beats a learned reward (which gets hacked).
2. A held-out eval you never train on.
3. On-policy freshness for RL — train on current policy's samples, not stale ones.
4. LoRA LR ~10x full-FT; rank 32 is a fine default. LoRA ~ full-FT for RL and small-data SFT; lags on large SFT.

Method/provider-specific numbers (LR, KL, group size) live in the recipe under `references/`.

## References

- `references/glue.md` — training I/O contract: what evo provides, what to emit.
- `references/trace-schema.md` — `TrainingTrace` JSON shapes.
- `references/diagnostics.md` — `held_out_score` / `delta` / `reward_saturation` / `generalization_gap`.
- `references/false-progress.md` — the five patterns, examples + detection.
- `references/{sft,rl,serving}/` — provider recipes (`sft/tinker.md`, `rl/art.md`, `serving/vllm.md`).

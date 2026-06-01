---
name: finetuning
description: When-to-do-what for the weight-update (training) axis -- terse method triggers, what actually matters, and the rules that are NOT laws. Load when planning or diagnosing a train move. Provider recipes and the data/trace/glue contract are in references/.
evo_version: 0.4.4-alpha.3
---

# Finetuning

Training is an activity you write in the experiment worktree; the benchmark loads the produced checkpoint by convention and emits a scalar score. evo ships no per-framework adapter. How to call a given backend, the data/trace shapes, and how to read a run all live in `references/` -- don't inline them here.

Pick the backend that fits the environment, using only what `final_model` can run on there: a **managed service** if one's available and allowed (`sft/tinker.md`, `rl/art.md` — these also serve the checkpoint), or **local single-GPU** training with whatever's installed (e.g. TRL/PEFT) plus `serving/vllm.md` to serve the adapter. A self-contained GPU box with no external services is the local path.

The only **firm** things are evo's guardrails: a held-out eval you never train on, and no eval-data leakage (both are gates), and trust evo's recorded numbers over a run's self-report. Everything below is a prior to apply with judgment and override against the gate.

## When to do what

Decide by **reward shape first**, not by familiarity. The training method that fits your reward beats a more-comfortable method that doesn't.

- **Reward is verifiable** (exact-match integer answers, unit-test pass/fail, deterministic checker, parser-decidable) -- start with **RL (GRPO/RLOO/PPO)**. The reward shapes correctness AND output-format simultaneously: the model gets reward only when its output is BOTH right AND in the format the verifier can parse. SFT on the same task often trains good reasoning but the model never learns to emit answers in the verifier-expected shape -- you end up at score 0.0 even with healthy training loss, because reasoning != verifier-acceptance. Math benchmarks with integer answers (AIME, GSM8K), code benchmarks with unit tests (HumanEval, MBPP), and any benchmark where "did I get the right answer" is mechanically checkable fall here. **This is where SOTA reasoning models actually come from in 2026** -- DeepSeek-R1, Qwen3 reasoning, etc. all use verifiable RL, not SFT alone.

- **Reward is taste / preference pairs** (chosen vs rejected, RLHF-style labels) -- use **DPO / KTO / ORPO**. Cheaper than full RL, no rollouts. Right when you have pair labels but no verifier.

- **No reward, just demonstrations** (curated trajectories, expert traces, chat data) -- use **SFT**. The classic "install a capability the base lacks" case: format, tone, chat. Default when the only signal you have is "here's what good looks like."

- **You can score outputs but want SFT's stability** -- use **RFT** (rejection-sampling / STaR): generate samples, filter by reward, SFT on the survivors. Cheap, stable, often a strong warm-start before full RL.

**Diagnostic for "stuck at 0.0 on a verifiable benchmark":** if you've run 2+ SFT experiments and committed score is still 0.0 (the verifier accepts no outputs), the technique-class is wrong, not the recipe. Pivot to RL with the verifier-as-reward -- more SFT data won't fix it. Verifiable-reward RL gets format-acceptance for free because the reward includes it.

## What actually matters (roughly in order)

1. A reward you trust -- verifiable beats a learned reward (which gets hacked).
2. A held-out eval you never train on.
3. On-policy freshness for RL -- train on the current policy's samples, not stale ones.
4. LoRA LR is ~10x full-fine-tune; rank matters less than people think (32 is a fine default). LoRA ~= full-FT for RL and small-data SFT; it lags on large SFT sets.

Method- and provider-specific numbers (LR, KL, group size) live in the recipe you pick under `references/`.

## Not laws

Don't state these as rules -- they're situational, decide empirically against the gate:

- "Always SFT before RL" -- **RL-from-base works for strong base models** (Qwen3-Base, Llama-3-Base scale and up). SFT is a warm-start when the base can't produce parseable outputs at all; for verifiable-reward benchmarks with a competent base, GRPO-from-base often beats SFT-then-GRPO end-to-end.
- "Only RL with verifiable rewards" -- preference-RL on a learned reward is valid where correctness isn't checkable.
- "Always keep the KL penalty" -- some reasoning-RL runs drop it; it's a tunable.
- Fixed β / rank / LR as universal -- scale- and task-dependent.

## Reading a run

`references/diagnostics.md` -- evo records `held_out_score`/`delta`, `reward_saturation`, `generalization_gap`; interpret against those (`delta <= 0` means the move didn't help).

## References

- `references/glue.md` -- write the training activity (what evo provides, what to emit).
- `references/trace-schema.md` -- rollout + `TrainingTrace` JSON shapes.
- `references/diagnostics.md` -- what evo computes + how to read it.
- `references/{sft,rl,serving}/` -- provider recipes (`sft/tinker.md`, `rl/art.md`, `serving/vllm.md`).

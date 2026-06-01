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

## Research the literature before the first commit

The decision tree above is the structural prior. The empirical answer for *this* model on *this* benchmark usually has a recent paper, blog, or HF Space recipe behind it -- and what beats baseline on a 4B base model in 2026 is not what the agent's pre-training data captures. Before picking the technique for `exp_0001` (the first experiment after baseline), invoke `evo:ideator` with a `literature` brief:

```
Task(
    subagent_type="evo:ideator",
    prompt="brief=literature\n"
           "model_family=<e.g. Qwen3-4B-Base, Llama-3.1-8B-Base>\n"
           "benchmark=<name + URL/paper if known>\n"
           "objective=<one line: what beats baseline looks like>\n"
           "constraints=<budget, data sources allowed, gated models forbidden, etc>"
)
```

The ideator returns ranked proposals with references (arXiv, HF Hub, GitHub, blogs). Read them before picking from the reward-shape table. A paper showing GRPO-from-base works on `<model_family>` for a similar verifiable benchmark beats applying the table cold.

Run this **once before `exp_0001`**, and again whenever the optimize loop hits a plateau (the "stuck across distinct techniques" diagnostic below). Not every subsequent experiment needs a literature pass -- the table + diagnostics carry the rest.

## Before committing the budget: smoke-run

Run the full pipeline on ~10 examples for ~1 minute. Must produce: a checkpoint the benchmark can load AND a non-zero eval on a held-out item. If not, the recipe is broken — fix it, don't scale it. dtype mismatch, tokenizer/template drift, OOM at this batch size, empty artifacts dir despite falling loss — all surface on 10 examples. Running longer doesn't surface them differently, just more expensively.

## Four diagnostics

**Stuck at 0 on a verifiable benchmark after 2+ SFT runs.** Technique class is wrong, not the recipe. Pivot to RL with the verifier as reward; SFT loss can be healthy while the model emits unparseable output.

**Base scores below random before any training (knowledge-heavy benchmark).** Model lacks the knowledge, not the format. Post-training shapes existing knowledge; it does not install new knowledge. Right axis: continued pre-training on a domain corpus, distillation from a stronger model that has the knowledge, or retrieval-augmented inference.

**`delta <= 0` across several committed train moves.** Method exhausted on this target. Try a different method, change the data, or improve the harness instead of the weights.

**Stuck at the same non-zero score across 3+ experiments spanning distinct techniques.** When 3+ committed experiments — across structurally different techniques (e.g. SFT, GRPO, RFT) — all land at the same non-zero score, the bottleneck is not the training method. The most common cause is a train↔verifier objective mismatch: the model has learned to emit answers in one format, but the verifier expects a different one. Examples: training data uses `\boxed{X}` but the verifier prompt requests `ANSWER: X` (or vice versa); training uses one chat template, eval uses another; training optimizes step-by-step CoT but the verifier wants the answer alone.

Diagnostic action: spot-check 3 training examples and 3 eval-prompt examples side by side. If a perfect-score training example would NOT pass the verifier (or vice versa), the objective is mismatched. Realign the training data format to the verifier's expected output, OR change the eval prompt (if rules allow). Do NOT try a fourth training-technique variant before doing this spot-check.

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

## Warm-start from parent (default for non-root experiments)

`evo run` populates the `EVO_PARENT_POLICY` env var pointing at the parent experiment's checkpoint URI. The training script should warm-start from this checkpoint by default for any non-root experiment, rather than re-training from base. Re-training from base for every experiment burns the budget on duplicated work and prevents the experiment tree from accumulating capability across generations.

Concrete pattern:

```python
parent_policy = os.environ.get("EVO_PARENT_POLICY")
if parent_policy and os.path.exists(parent_policy):
    print(f"warm-starting from parent: {parent_policy}")
    model = AutoModelForCausalLM.from_pretrained(parent_policy, ...)
else:
    print("no parent policy; loading base")
    model = AutoModelForCausalLM.from_pretrained(BASE_MODEL, ...)
```

Override only when the brief explicitly asks for a fresh-from-base ablation (e.g. comparing a new technique against the base, or when parent is suspected of overfitting in a way the current method should not inherit). The full I/O contract is in `references/glue.md`.

## Cache expensive intermediates

LoRA adapters, filtered/curated datasets, tokenized datasets, computed embeddings, generated rollouts -- expensive to produce, large, and gitignored. They don't ride the experiment branch. They also don't have to be rebuilt per experiment.

Write expensive artifacts to a stable, workspace-level path; check for them first, compute only on miss. Subsequent experiments (siblings, descendants, or re-runs of the same experiment after a worktree clean) read the same path.

Convention: under `.evo/cache/`, sibling to `run_<NNNN>/`. Already gitignored (via `.evo/` in the workspace's git excludes). Survives across runs -- it's not nested inside any `run_<id>/`, so `evo new`/`evo run`/`evo reset` don't touch it.

Pattern:

```python
import os
from pathlib import Path
# walk up from cwd to find the workspace root (the dir that has .evo/)
def _workspace_root() -> Path:
    p = Path.cwd().resolve()
    for d in [p, *p.parents]:
        if (d / ".evo").is_dir():
            return d
    raise RuntimeError("not inside an evo workspace")

cache = _workspace_root() / ".evo" / "cache" / "datasets"
cache.mkdir(parents=True, exist_ok=True)
# Cache key embeds every input that changes the artifact: dataset name,
# filter recipe version, tokenizer, max length, etc. Different recipe ->
# different key, so a sibling experiment with a different filter keeps
# its own cache without trampling yours.
key = cache / "numina-cot-r1-filter-v2-qwen3-tok-3072.arrow"
if key.exists():
    ds = datasets.Dataset.load_from_disk(str(key))
else:
    ds = build_and_filter_dataset()
    ds.save_to_disk(str(key))
```

High-value caches (not exhaustive): curated/tokenized training corpora (tokenization is the slow part on millions of rows); LoRA adapters produced by prior experiments that a sibling might warm-start from (the parent path is already handled by `EVO_PARENT_POLICY` above; this is for sibling-reachable named adapters); computed embeddings, retrieval indexes, precomputed eval-time generations.

Don't duplicate the HuggingFace Hub cache (`~/.cache/huggingface/`). That handles `from_pretrained` downloads automatically and is user-level, already shared across all experiments.

Anti-pattern: writing the artifact inside the experiment's worktree (`<worktree>/some_cache/`). Worktrees are gitignored for these files, the artifact doesn't propagate to descendants via the git tree, and a worktree clean / gc removes it. Use the workspace-level `.evo/cache/` instead.

A first-class named registry (`evo asset put/get/list/use`) for these is tracked in issue #55. The path convention above is the lightweight version anyone can adopt today.

## References

- `references/glue.md` — training I/O contract: what evo provides, what to emit.
- `references/trace-schema.md` — `TrainingTrace` JSON shapes.
- `references/diagnostics.md` — `held_out_score` / `delta` / `reward_saturation` / `generalization_gap`.
- `references/false-progress.md` — the five patterns, examples + detection.
- `references/{sft,rl,serving}/` — provider recipes (`sft/tinker.md`, `rl/art.md`, `serving/vllm.md`).

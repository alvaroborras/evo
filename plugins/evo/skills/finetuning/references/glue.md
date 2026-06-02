# Writing the training activity (glue)

This is the I/O contract for an already-chosen technique. Invoke the `evo:finetuning` Skill first if you haven't yet -- the technique pick (SFT / DPO / RFT / GRPO / ...) lives in the skill body's reward-shape decision tree, not here.

You write this per task in the experiment worktree. evo provides inputs by convention; you produce a checkpoint + traces; the benchmark loads the checkpoint and emits a scalar score.

## Inputs evo provides (by convention)

- `EVO_DATASET` — path to the assembled scored-trajectory JSONL (train split only;
  selection already applied). See `trace-schema.md`.
- `EVO_PARENT_POLICY` — for a root experiment, the base model id; for any
  non-root experiment, the local path to the parent experiment's checkpoint.
  The training script **must** warm-start from this when the value is a
  checkpoint path. Re-training from base on every experiment burns the budget
  on duplicated work and breaks capability accumulation across the tree.
- `EVO_RUN_DIR` / `EVO_ARTIFACTS_DIR` — where to write the checkpoint + traces.
- Held-out data is **not** provided — evo scores on it independently.

### Warm-start pattern

```python
parent_policy = os.environ.get("EVO_PARENT_POLICY")
if parent_policy and os.path.exists(parent_policy):
    model = AutoModelForCausalLM.from_pretrained(parent_policy, ...)
else:
    model = AutoModelForCausalLM.from_pretrained(BASE_MODEL, ...)
```

Branch on `os.path.exists` rather than presence-only — for the root
experiment the value is a model id, not a path, so `from_pretrained` should
treat it as the base.

## What you produce

1. A **checkpoint artifact** under the artifacts dir, recorded on the result as
   `artifacts: [{kind: "lora_adapter"|"checkpoint", uri, content_key, created_by}]`.
2. `train_summary.json` — the `TrainingTrace` setup/dynamics fields.
3. `metrics.jsonl` — step-indexed `{step, loss, reward, kl, grad_norm, lr}`.

## Benchmark by convention

The benchmark loads the active checkpoint (from the consumed artifact / the policy
env the harness reads) and emits the normal scalar score. A child experiment that
`consumes` the checkpoint is scored exactly like any other node — score stays the spine.

## Rules

- Compute loss on **assistant tokens only** (set `trainable`); keep groups intact.
- Train and serve with the **same chat template / tokenizer** — serving drift silently tanks LoRA quality.
- Never fetch or evaluate on held-out scenarios — evo owns that boundary.

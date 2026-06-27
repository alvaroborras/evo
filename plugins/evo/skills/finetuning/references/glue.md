# Writing the training activity (glue)

This is the I/O contract for an already-chosen technique. Invoke the `evo:finetuning` Skill first if you haven't yet -- the technique pick (SFT / DPO / RFT / GRPO / ...) lives in the skill body's reward-shape decision tree, not here.

You write this per task in the experiment worktree. evo provides inputs by convention; you produce a checkpoint + traces; the benchmark loads the checkpoint and emits a scalar score.

## Inputs evo provides (by convention)

- `EVO_DATASET` — path to the assembled scored-trajectory JSONL (train split only;
  selection already applied). See `trace-schema.md`.
- `EVO_SEED_ARTIFACT` — set only when the orchestrator branched this experiment
  from a committed/preserved checkpoint via `evo new --from-artifact <exp[:label]>`;
  the local path to that artifact (also mirrored as `EVO_PARENT_POLICY` for
  back-compat with recipes that read that name). Warm-start from it when present;
  if unset, load the base model. Re-training from base when a usable seed exists
  burns the budget and breaks capability accumulation across the tree.
- `EVO_CHECKPOINT_DIR` — durable output location for the checkpoint you produce
  (lives under the experiment record, survives between-attempt cleanup and
  discard). Write the reusable checkpoint here so it can be declared + later
  seeded; the worktree itself is ephemeral.
- `EVO_METRICS_PATH` — pre-created path for `metrics.jsonl`. Append step-indexed
  metrics (`{step, loss, reward, kl, grad_norm, lr}`) here for live dashboard
  visibility. The dashboard polls this file every 5 seconds.
- `EVO_SAMPLES_PATH` — pre-created path for `samples.jsonl`. Append generated
  samples (`{text, output, ...}`) here for live preview in the dashboard.
- Held-out data is **not** provided — evo scores on it independently.

### Warm-start pattern

```python
seed = os.environ.get("EVO_SEED_ARTIFACT") or os.environ.get("EVO_PARENT_POLICY")
if seed and os.path.exists(seed):
    model = AutoModelForCausalLM.from_pretrained(seed, ...)
else:
    model = AutoModelForCausalLM.from_pretrained(BASE_MODEL, ...)
```

`EVO_SEED_ARTIFACT` is unset for a from-base experiment, so absence ⇒ load the
base model. (Load onto the GPU directly — `device_map={"": 0}` / `.to("cuda")`;
never `device_map="auto"` for training, which offloads to CPU or crashes the
backward pass.)

## What you produce

1. A **checkpoint artifact** under the artifacts dir, recorded on the result as
   `artifacts: [{kind: "lora_adapter"|"checkpoint", uri, content_key, created_by}]`.
2. `train_summary.json` — the `TrainingTrace` setup/dynamics fields.
3. `metrics.jsonl` — step-indexed `{step, loss, reward, kl, grad_norm, lr}`.
   Append to `EVO_METRICS_PATH` for live dashboard visibility.
4. `samples.jsonl` (optional) — generated samples for live preview.
   Append to `EVO_SAMPLES_PATH`.

## Benchmark by convention

The benchmark loads the active checkpoint (from the consumed artifact / the policy
env the harness reads) and emits the normal scalar score. A child experiment that
`consumes` the checkpoint is scored exactly like any other node — score stays the spine.

## Rules

- Compute loss on **assistant tokens only** (set `trainable`); keep groups intact.
- Train and serve with the **same chat template / tokenizer** — serving drift silently tanks LoRA quality.
- Never fetch or evaluate on held-out scenarios — evo owns that boundary.

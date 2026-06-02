# Observability: streaming training metrics live

Why this is here: a long-running training script is observability-blind until the experiment commits to evo. Without a live tracker, the user can't tell if loss is converging, if the GPU went idle, or if the recipe is silently broken. Wire a tracker by default. Detect from env; don't invent one.

## Detection prior

Apply the first match. Skip if no env vars are set — don't install a tracker the user didn't opt into.

| Env var present | Tracker | Tracker URL/dashboard |
|---|---|---|
| `WANDB_API_KEY` | `wandb` | https://wandb.ai (or `WANDB_BASE_URL` if set) |
| `TRACKIO_SPACE_ID` | `trackio` | `https://huggingface.co/spaces/<TRACKIO_SPACE_ID>` |
| `MLFLOW_TRACKING_URI` | `mlflow` | value of the URI |

Two are mutually exclusive — wire the one whose env var is set, not both. If multiple are set, prefer the explicit choice the user pointed at most recently in the conversation, otherwise wandb > trackio > mlflow.

## Hugging Face: TRL `report_to`

If the training uses TRL (`SFTTrainer`, `DPOTrainer`, `GRPOTrainer`, etc.) just pass `report_to`:

```python
from trl import SFTConfig, SFTTrainer

config = SFTConfig(
    output_dir="final_model",
    report_to="trackio",                       # or "wandb", "mlflow", "none"
    run_name=os.environ.get("EVO_EXPERIMENT_ID", "exp_unknown"),
    # ... other args
)
trainer = SFTTrainer(model=..., args=config, ...)
trainer.train()
```

TRL handles the init, the per-step `log`, the final `finish` — nothing else needed. `report_to="none"` (or omitting the field) disables tracking; useful for smoke runs.

## Custom training loops

If you're not on TRL, you call the tracker yourself:

```python
import os

if os.environ.get("TRACKIO_SPACE_ID"):
    import trackio as tracker
    tracker.init(
        project="evo-runs",
        name=os.environ.get("EVO_EXPERIMENT_ID", "exp_unknown"),
        space_id=os.environ["TRACKIO_SPACE_ID"],
        config={"lr": lr, "batch_size": bs, ...},   # hyperparams snapshot
    )
elif os.environ.get("WANDB_API_KEY"):
    import wandb as tracker
    tracker.init(project="evo-runs", name=os.environ.get("EVO_EXPERIMENT_ID", "exp_unknown"), config={...})
else:
    tracker = None

# in your training loop:
for step, batch in enumerate(loader):
    loss = train_step(batch)
    if tracker:
        tracker.log({"loss": loss, "step": step, "lr": current_lr})

if tracker:
    tracker.finish()
```

## What to log

Minimum: `loss`, `step`, `lr`. Add when relevant:

- `grad_norm` (catch exploding/vanishing gradients)
- `entropy` (RL: policy collapse early-warning)
- `kl_div` (RL: drift from reference policy)
- `mean_token_accuracy` (SFT: cheap signal that the model is fitting)
- `eval/score` if you run a quick mini-eval mid-training
- `tokens_seen` or `examples_seen` (compare across runs with different batch sizes)

For RL also log per-rollout: `reward_mean`, `reward_std`, `rollout_length`. These move fast and surface reward hacking early.

## Naming runs

Use `EVO_EXPERIMENT_ID` as the run name. Each experiment then shows up as its own line in the tracker dashboard, and the dashboard's parent-grouping (when supported) reflects evo's tree. Falling back to `exp_unknown` or a timestamp is fine if the env var is missing.

## Don't double-instrument

If you're already calling `trainer.train()` with `report_to="trackio"`, do NOT also call `trackio.log(...)` yourself in a callback — you'll get duplicate metrics. TRL owns the lifecycle in that case. The custom-loop pattern is only for non-TRL training code.

## Anti-patterns

- Always wiring wandb regardless of env. The user may not have a wandb account, may be on a private network, or may be running smoke runs and not want noise. Detect first.
- Hardcoding a project name like `"my-experiment"`. The dashboard fills up with runs that don't say which experiment they belong to. Use `EVO_EXPERIMENT_ID`.
- Logging every single training step. For short steps (< 100ms), batch to every 10–50. Otherwise the tracker rate-limits or drops events silently.
- Forgetting `tracker.finish()`. Some trackers buffer and the final ~30 seconds of metrics get lost. Always call finish at the end of training (or use `with tracker.run(...):` if the tracker exposes a context manager).

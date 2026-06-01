---
name: ideator
description: Deprecated -- the ideator moved from a skill to a subagent. Loading this skill returns a pointer to the new invocation path. No behavior change for callers that follow the redirect.
evo_version: 0.5.0-alpha.2
---

# Ideator (moved)

The ideator is now a subagent at `plugins/evo/agents/ideator.md`. Invoke it via the host's Task tool, one spawn per brief:

```
Task(subagent_type="evo:ideator",
     prompt="workspace=<abs path>\nbrief=<failure_analysis|literature|frontier_extrapolation>")
```

Each spawn runs ONE brief, appends proposals as JSONL lines to `.evo/run_<run_id>/ideator/proposals.jsonl`, and returns a JSON summary. The orchestrator reconciles proposals at the next round's brief-writing time. `evo wait --for ideators --count N` still works as the synchronization primitive.

This stub is kept so any older callsite that resolves `evo:ideator` through the skill loader gets a clear pointer rather than a not-found error.

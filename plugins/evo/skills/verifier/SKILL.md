---
name: verifier
description: Deprecated -- the verifier moved from a skill to a subagent. Loading this skill returns a pointer to the new invocation path. No behavior change for callers that follow the redirect.
evo_version: 0.5.0-alpha.3
---

# Verifier (moved)

The verifier is now a subagent at `plugins/evo/agents/verifier.md`. Invoke it via the host's Task tool instead of loading this skill:

```
Task(subagent_type="evo:verifier",
     prompt="workspace=<abs path>\nexperiment_id=<exp_id>\nphase=<pre|post>")
```

The subagent returns a JSON report with `passed` and `findings`, and writes the same verdict as an `evo annotation` on the target experiment. Same audit checks (test-set leakage, benchmark sanity, gate coverage, artifact reality, duration sanity, reproducibility); read-only.

This stub is kept so any older callsite that resolves `evo:verifier` through the skill loader gets a clear pointer rather than a not-found error.

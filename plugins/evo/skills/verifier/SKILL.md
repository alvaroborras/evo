---
name: verifier
description: Audit a single experiment for design-time cheating (pre-phase) or result-time validity (post-phase). Run as a precondition for evo run and as a gate before commit. Use when the user invokes /evo:verifier, the subagent protocol calls verifier before evo run or before commit, or you need to sanity-check an experiment that completed suspiciously fast or with implausible scores.
argument-hint: "--phase <pre|post> --target <exp_id>"
evo_version: 0.5.0-alpha.5
---

# Verifier

Internal procedure for `evo:verifier`. Audits one experiment for issues that the optimizer wouldn't catch on its own -- test-set leakage in training data, no-op `final_model/` artifacts, cache short-circuits in eval, score-implausibility, etc.

Two phases, both scoped to a single experiment:

- **`--phase pre`** runs BEFORE `evo run`. Static analysis only -- the experiment hasn't executed yet. Catches design-time cheating (training on test data, deliberately undersampled eval, missing gates) before any compute is burned.
- **`--phase post`** runs AFTER `evo run` completes, BEFORE the subagent commits. Inspects the actual results -- benchmark duration vs. cohort, score reproducibility, artifact reality. Catches result-time cheating (cached eval output, no-op model, fabricated score).

## Host conventions

Same as the discover/optimize skills -- runs on any host that implements the Agent Skills spec. The verifier itself uses only the `evo` CLI, file reads, and (optionally) a subprocess call to re-run a sample. No host-specific divergences.

## Output contract

The verifier writes its verdict to the target experiment as an `evo annotation` so the dashboard, the orchestrator, and later verifier runs can read it:

```bash
evo annotation add <exp_id> --type verification \
  --json '{"phase": "pre", "verdict": "pass", "checks": {...}, "notes": "..."}'
```

`verdict` is one of `pass`, `fail`, `warn`. `checks` is a structured map of each individual check's result. `notes` is freeform agent-readable text.

Exit code: 0 on PASS or WARN, 1 on FAIL. The subagent uses the exit code to decide whether to proceed.

## Phase `pre` -- pre-`evo run` static analysis

Cheap (~30s). The experiment's worktree exists with the agent's proposed train.py / benchmark config / gates / hypothesis, but `evo run` hasn't executed yet.

### Inputs

- `<exp_id>` -- the experiment about to be run
- Read its worktree path from `evo show <exp_id>`
- Read the workspace's `.evo/project.md` for declared test-set patterns

### Checks

1. **Test-set leakage in training data.** Read the experiment's `train.py` (or whichever file the experiment changed -- get via `evo show <exp_id>` for the change list). Look for:
   - File path references to anything matching the workspace's test-set glob (recorded in `.evo/project.md` under "Test data identifiers" -- if missing, ask the orchestrator to fill it in)
   - HuggingFace dataset names that overlap with the test set
   - Hard-coded substrings of known test questions/answers
   - `--split test` / `split="test"` patterns
   Fail loudly when any are found; cite the line.

2. **Benchmark-command sanity.** Read the workspace's `evo config get benchmark` and the experiment's benchmark override (if any). Flag:
   - `--limit N` / `--eval-limit N` / `--max-samples N` with N < full-set size -- explicit subsetting. Warn (not fail) unless the workspace `project.md` documents an approved subset for fast iteration.
   - The experiment's benchmark substantially differs from the workspace baseline -- could be intentional (new wrapper logic) but flag for the agent to confirm.

3. **Gate coverage.** Run `evo gate list <exp_id>`. If the experiment introduces a new artifact (model checkpoint, generated code), confirm there's at least one gate that validates the artifact exists and isn't a no-op stub (look for any gate command that touches the artifact path). Warn if missing.

4. **Hypothesis specificity.** Read `evo show <exp_id>` for the `hypothesis` field. Generic hypotheses like "improve performance" or "try a different technique" should warn -- the experiment can't be evaluated against a specific prediction. Specific hypotheses (a named technique with concrete hyperparameters, named dataset / input, and a quantitative expected effect) pass.

5. **Resource-profile compliance.** Read workspace `resource_profile` if present (set via `evo config set resource-binding ...`). If `concurrent_safe=false` AND `evo status` shows any other experiment as active, warn -- a second run will OOM.

### Output

Write the verification annotation, exit 0 (PASS/WARN) or 1 (FAIL).

## Phase `post` -- post-`evo run`, pre-commit result audit

The experiment ran. The agent is about to commit. Verify the result is real before it enters the frontier.

### Inputs

- `<exp_id>` -- the experiment that just finished
- Its `benchmark_result` and per-attempt artifacts from `.evo/run_*/experiments/<exp_id>/attempts/<n>/`
- The committed cohort's durations and scores from `evo show <ancestor_ids>` (the parent chain)

### Checks

1. **Duration sanity.** Compute the cohort's median benchmark duration from the parent chain's committed experiments. If this experiment's duration is < 20% of cohort median, fail with reason "cache_short_circuit_suspected". (Tunable threshold: workspaces with a declared sanity-eval mode pre-record an expected duration band; use that when present.)

2. **Artifact reality.** Read the experiment's `gate_check` log (`attempts/<n>/gate_<name>.log` for the model-validation gate). Confirm:
   - The trained model directory referenced by the gate exists in the worktree
   - It contains `config.json`, `tokenizer_config.json`, and at least one `*.safetensors` or `*.bin` file totaling >= a workspace-declared minimum (default 100 MB; lower for known-tiny models)
   - The config's `architectures` field matches the base model declared in the workspace
   Fail if any check breaks.

3. **Score reproducibility spot-check.** Pick 2 random task_ids from the benchmark's trace files. Re-run the eval on just those 2 (subset mode if the benchmark supports it). Compare against the recorded per-task scores. If both match within tolerance, pass. If either differs, fail with the diff -- could indicate evaluator non-determinism, cached results, or fabrication.

   Skip this check when:
   - The benchmark is documented as non-deterministic (workspace config flag)
   - The benchmark doesn't support subset re-runs (warn, don't fail)

4. **Gate compliance.** For every registered gate, confirm `attempts/<n>/result.json` records a non-null `passed` field. A null/missing gate result indicates the gate was skipped (bug or sabotage). Fail.

5. **Hypothesis vs. outcome.** Read `hypothesis` and `benchmark_result.score`. The hypothesis often states an expected effect ("+3-5% over baseline"). If the actual score lies far outside the prediction band, warn -- could be a great surprise or could be measurement error worth investigating. Not a fail.

### Output

Write the verification annotation, exit 0 (PASS/WARN) or 1 (FAIL).

If the verdict is FAIL, the subagent should discard the experiment with:

```bash
evo discard <exp_id> --reason "verifier post-phase fail: <one-line summary>"
```

The verification annotation persists so future ideator runs can learn from why experiments were rejected.

## Integration

- **From `evo:subagent`**: the subagent protocol calls verifier as a precondition before `evo run` (pre-phase) AND before commit (post-phase). See the subagent skill body for the exact call sites.
- **From the orchestrator (`evo:optimize`)**: occasional manual audit of an already-committed experiment that scored suspiciously high. Run with `--phase post --target <id>` after the fact; the annotation gets added as a second verification record.
- **From the doom-loop watchdog**: when the optimize loop detects N consecutive failed experiments, it can bulk-verify the last N to look for a shared invalid-design pattern.

## Examples

### Pre-phase: catch test-set leakage

```bash
# Subagent has constructed exp_0007 with a changed target file
evo:verifier --phase pre --target exp_0007
# Output:
# annotation written: phase=pre, verdict=fail
#   checks.test_set_leakage = fail
#     <target>:42 loads a path matching the workspace's declared test_set_glob
#     ("<glob from project.md>") -- training on test data invalidates the score
# exit code: 1
```

The subagent revises the target, removes the offending load, re-runs the verifier, gets pass, then proceeds to `evo run`.

### Post-phase: catch cache short-circuit

```bash
# exp_0000 committed in 99 seconds; cohort median is 1800s
evo:verifier --phase post --target exp_0000
# Output:
# annotation written: phase=post, verdict=fail
#   checks.duration_sanity = fail
#     exp_0000 ran in 99s; cohort median = 1812s (5.5% of median, threshold 20%)
#     likely cause: evaluate.py found a cached eval_results.json from the gate
#     check and re-read it instead of re-evaluating
# exit code: 1
```

## What the verifier deliberately does NOT do

- **Doesn't propose new experiments** -- that's the ideator's job. Verifier judges the experiment in front of it.
- **Doesn't run literature scans** -- only inspects the local artifacts.
- **Doesn't compare against historical baselines beyond the parent chain** -- cross-cutting analysis across the full graph is the doom-loop watchdog's job.
- **Doesn't gate on score thresholds** -- that's regular `evo gate`'s job. Verifier checks structural validity, not metric improvement.

## Failure-mode catalog (for the agent's reference)

When the verifier returns FAIL, the categories the subagent should be ready to address:

| Check | Common cause | Typical fix |
|---|---|---|
| test_set_leakage | Training data globs the test set | Filter the dataset loader to exclude test ids/files |
| duration_sanity | Cached eval result re-read instead of re-evaluated | Delete the stale `eval_results.json` before each run, or pass a fresh `--json-output-file` |
| artifact_reality | Training crashed before saving the model | Check the training error log; usually OOM, missing dep, or wrong API |
| score_reproducibility | Eval is non-deterministic OR the recorded score was fabricated | Set a seed; if score is fabricated, discard and revise |
| gate_compliance | A gate was registered but never fired | Check that the gate command's path resolves and the registered phase matches when the run executed |

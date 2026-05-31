"""Python SDK usage examples.

Install `evo-hq-agent` with this project's package manager/runtime, for example
`uv add --dev evo-hq-agent` or `python -m pip install evo-hq-agent`.

The SDK auto-reads $EVO_TRACES_DIR, $EVO_EXPERIMENT_ID, and $EVO_RESULT_PATH.
Traces flush on each report() so the dashboard can stream progress live.

**Per-task emission is the load-bearing discipline.** Loop over your N
independent items and call `run.report(task_id, score=..., ...)` ONCE PER
ITEM. Do NOT roll up to one aggregate `run.report("eval_total", score=avg)`
call -- the dashboard's per-task panel and the verifier's reproducibility
spot-check both rely on per-item traces. The example below shows the right
pattern. The Anti-pattern at the bottom is what to avoid.
"""

from evo_agent import Run, Gate


# ---- Benchmark run ----

run = Run()
try:
    for task in tasks:
        run.log(task["id"], "starting task")
        try:
            result = evaluate(task, agent)
            run.log(task["id"], {"output": result.output})
            run.report(
                task["id"],
                score=result.score,
                summary=f"reward={result.score:.2f}",
                failure_reason=None if result.passed else "task_failed",
            )
        except Exception as exc:
            run.log(task["id"], {"error": repr(exc)})
            run.report(task["id"], score=0.0, failure_reason="exception")
finally:
    run.finish()
# finish() writes score JSON to $EVO_RESULT_PATH (or stdout if unset) and one
# task_<id>.json per task under $EVO_TRACES_DIR. Catch expected per-task errors;
# an uncaught exception before finish() means evo correctly sees a crashed run.


# ---- Gate (exits 0 all-pass / 1 any-fail) ----

with Gate() as gate:
    for task in critical_tasks:
        result = evaluate(task, agent)
        gate.check(task["id"], score=result.score)


# ---- ANTI-PATTERN (do NOT do this) ----
#
# Computing the aggregate yourself and reporting it as a single task
# entry loses every diagnostic value of the per-item traces -- dashboard
# can't show which problems failed, verifier can't spot-check, ideator
# can't cluster failure modes. The SDK aggregates from per-task reports
# automatically.
#
#     # WRONG:
#     scores = [evaluate(t, agent).score for t in tasks]
#     run.report("eval_total", score=sum(scores) / len(scores))
#     run.finish()
#
# Exception: if your benchmark really is a single indivisible measurement
# (one e2e workflow with one score number), report one task AND attach
# every observable as extras.

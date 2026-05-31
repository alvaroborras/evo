// Node SDK usage example. Install: `npm install @evo-hq/evo-agent`.
//
// The SDK auto-reads $EVO_TRACES_DIR, $EVO_EXPERIMENT_ID, and
// $EVO_RESULT_PATH. Traces flush on each report() so the dashboard can
// stream progress live.
//
// **Per-task emission is the load-bearing discipline.** Loop over your N
// independent items and call run.report(id, {score, ...}) ONCE PER ITEM.
// Do NOT roll up to one aggregate `run.report("eval_total", ...)` call --
// dashboard panel + verifier reproducibility check both rely on per-item
// traces. The Anti-pattern at the bottom is what to avoid.

import { Run, Gate } from '@evo-hq/evo-agent';

// ---- Benchmark run ----

const run = new Run();
for (const task of tasks) {
  const result = await evaluate(task);
  run.log(task.id, { output: result.output });
  run.report(task.id, { score: result.score });
}
await run.finish();
// finish(): writes score JSON to $EVO_RESULT_PATH (or stdout if unset)
// and one task_<id>.json per task under $EVO_TRACES_DIR.

// ---- Gate (exits 0 all-pass / 1 any-fail) ----

const gate = new Gate();
for (const task of criticalTasks) {
  const result = await evaluate(task);
  gate.check(task.id, { score: result.score });
}
await gate.finish();

// ---- ANTI-PATTERN (do NOT do this) ----
//
// Reporting one aggregate task entry loses every diagnostic value of
// per-item traces. SDK aggregates from per-task reports automatically.
//
//     // WRONG:
//     const scores = await Promise.all(tasks.map(t => evaluate(t).then(r => r.score)));
//     run.report("eval_total", { score: scores.reduce((a, b) => a + b) / scores.length });
//     await run.finish();
//
// Exception: if your benchmark really is a single indivisible measurement,
// report one task AND attach every observable as extras.

/**
 * Inline instrumentation for Node benchmarks. Paste into the benchmark and
 * call logTask() per task + writeResult() once at the end.
 *
 * Contract:
 * - Reads EVO_TRACES_DIR, EVO_EXPERIMENT_ID, EVO_RESULT_PATH from process.env.
 * - Writes traces/task_<id>.json per task.
 * - Writes the final result JSON to EVO_RESULT_PATH, or stdout if unset.
 *
 * **Per-task emission is the load-bearing discipline.** If your benchmark
 * evaluates N independent items (per-test-case, per-document, per-sample),
 * call logTask ONCE PER ITEM with as much detail as you have (input,
 * expected, model output, timings). Do NOT roll up to one aggregate call --
 * the dashboard's per-task panel and the verifier's reproducibility check
 * both rely on per-item traces. See the USAGE EXAMPLE at the bottom.
 */

import {
  writeFileSync,
  mkdirSync,
  openSync,
  closeSync,
  renameSync,
} from "node:fs";
import { dirname, join } from "node:path";

const TRACES_DIR = process.env.EVO_TRACES_DIR || null;
const EXPERIMENT_ID = process.env.EVO_EXPERIMENT_ID || "unknown";
const RESULT_PATH = process.env.EVO_RESULT_PATH || null;
const SCORES = {};
const TASK_META = {};
const STARTED_AT = new Date().toISOString().replace(/\.\d{3}Z$/, "+00:00");

if (TRACES_DIR) mkdirSync(TRACES_DIR, { recursive: true });

/**
 * Record the result for one task. `direction` is "max" (higher is better,
 * default) or "min" (lower is better, e.g. latency). Set it only when this
 * task's direction differs from the benchmark's top-level --metric.
 * Propagates to tasks_meta in the final result JSON.
 */
export function logTask(taskId, score, { summary, failureReason, log, direction, ...extra } = {}) {
  taskId = String(taskId);
  if (direction !== undefined && direction !== "max" && direction !== "min") {
    throw new Error(`direction must be 'max' or 'min', got ${JSON.stringify(direction)}`);
  }
  SCORES[taskId] = score;
  if (direction !== undefined) TASK_META[taskId] = { direction };
  if (!TRACES_DIR) return;
  const trace = {
    experiment_id: EXPERIMENT_ID,
    task_id: taskId,
    status: score >= 0.5 ? "passed" : "failed",
    score,
    ended_at: new Date().toISOString().replace(/\.\d{3}Z$/, "+00:00"),
  };
  if (direction !== undefined) trace.direction = direction;
  if (summary !== undefined) trace.summary = summary;
  if (failureReason !== undefined) trace.failure_reason = failureReason;
  if (log !== undefined) trace.log = log;
  Object.assign(trace, extra);
  writeFileSync(join(TRACES_DIR, `task_${taskId}.json`), JSON.stringify(trace, null, 2), "utf-8");
}

export function writeResult(score) {
  const ids = Object.keys(SCORES);
  if (score === undefined) {
    score = ids.length === 0 ? 0.0 : ids.reduce((a, id) => a + SCORES[id], 0) / ids.length;
  }
  score = Math.round(score * 10000) / 10000;
  const result = {
    score,
    tasks: { ...SCORES },
    started_at: STARTED_AT,
    ended_at: new Date().toISOString().replace(/\.\d{3}Z$/, "+00:00"),
  };
  if (Object.keys(TASK_META).length > 0) {
    result.tasks_meta = Object.fromEntries(
      Object.entries(TASK_META).map(([k, v]) => [k, { ...v }])
    );
  }
  const payload = JSON.stringify(result, null, 2);
  if (RESULT_PATH) {
    mkdirSync(dirname(RESULT_PATH), { recursive: true });
    // Claim + tmp+rename: duplicate writers fail-fast; crash mid-publish
    // leaves an empty file (caught by load_result) not a partial write.
    try {
      closeSync(openSync(RESULT_PATH, "wx"));
    } catch (e) {
      if (e.code === "EEXIST") {
        throw new Error(
          `${RESULT_PATH} already exists; only one writeResult() per attempt`
        );
      }
      throw e;
    }
    const tmp = RESULT_PATH + ".tmp";
    writeFileSync(tmp, payload, "utf-8");
    renameSync(tmp, RESULT_PATH);
  } else {
    process.stdout.write(payload + "\n");
  }
  return score;
}


// === USAGE EXAMPLE (copy + adapt) ===
//
// For a benchmark scoring N independent items, emit one logTask per item.
// writeResult() with no arg aggregates from the per-task scores.
//
// async function main() {
//   const cases = await loadTestCases();              // N items
//   for (const [i, c] of cases.entries()) {
//     const output = await runUnderTest(c.input);
//     const correct = output === c.expected;
//     logTask(
//       `case_${String(i).padStart(2, "0")}`,         // stable id per item
//       correct ? 1.0 : 0.0,                          // per-item score
//       {
//         input: c.input,                             // extras land in the trace JSON
//         expected: c.expected,                       // for diagnosis later
//         actual: output,
//         elapsed_ms: c.elapsed,
//       }
//     );
//   }
//   // No arg -> writeResult averages the per-task scores
//   const final = writeResult();
//   console.log(`final: ${final.toFixed(4)}`);
// }
//
// Anti-pattern: one logTask or writeResult with the aggregate score. Loses
// the per-item detail the dashboard and verifier rely on. Don't do this:
//
//   const score = passed / total;
//   logTask("eval_total", score);                    // <-- aggregate; useless
//   writeResult(score);
//
// Exception: if the benchmark really is a single indivisible measurement
// (one e2e workflow, one perf number), emit one task with that score AND
// pass every observable as extras (timings, allocations, error log).

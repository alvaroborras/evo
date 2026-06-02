"""Tests for the tasks-in-result assertion (#56).

`_assert_tasks_aggregated` fires when a benchmark wrote 2+ per-task
trace files but its result.json has no `tasks` array. Catches rolled-own
log_task/write_result that emits per-task traces but loses the aggregate
the dashboard's per-task panel needs.

Tested as a pure helper -- both `_cmd_run_check` and `_cmd_run_impl`
call into it after `load_result()`. The assertion logic is the same in
both call sites.
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "plugins" / "evo" / "src"))

from evo.cli import _assert_tasks_aggregated  # noqa: E402


def _write_traces(traces_dir: Path, ids: list[str]) -> None:
    traces_dir.mkdir(parents=True, exist_ok=True)
    for tid in ids:
        (traces_dir / f"task_{tid}.json").write_text(
            json.dumps({"task_id": tid, "score": 0.0}), encoding="utf-8"
        )


class TestAssertTasksAggregated(unittest.TestCase):
    # ---- positive: assertion does NOT fire ----

    def test_passes_when_traces_present_and_tasks_array_present(self):
        with tempfile.TemporaryDirectory() as d:
            traces = Path(d) / "traces"
            _write_traces(traces, ["0", "1", "2"])
            parsed = {"score": 0.33, "tasks": {"0": 0.0, "1": 1.0, "2": 0.0}}
            # Should not raise.
            _assert_tasks_aggregated(traces, parsed)

    def test_passes_when_zero_traces(self):
        # Benchmarks that legitimately emit no per-task traces (e.g.
        # single-aggregate-measurement) should not be punished.
        with tempfile.TemporaryDirectory() as d:
            traces = Path(d) / "traces"
            traces.mkdir()
            parsed = {"score": 0.5}
            _assert_tasks_aggregated(traces, parsed)  # no raise

    def test_passes_when_exactly_one_trace_no_tasks_array(self):
        # Single-task benchmarks are exempt -- assertion only fires for N>1.
        # A 1-trace benchmark is plausibly an aggregate-only measurement
        # where the single trace IS the result (single perf number, e.g.).
        with tempfile.TemporaryDirectory() as d:
            traces = Path(d) / "traces"
            _write_traces(traces, ["only"])
            parsed = {"score": 0.42}
            _assert_tasks_aggregated(traces, parsed)  # no raise

    def test_passes_when_traces_dir_missing(self):
        # If the traces dir doesn't even exist (path passed in but not
        # created), .glob() returns nothing. No assertion.
        with tempfile.TemporaryDirectory() as d:
            traces = Path(d) / "nonexistent"
            parsed = {"score": 0.5}
            _assert_tasks_aggregated(traces, parsed)  # no raise

    # ---- negative: assertion FIRES (the canonical bug) ----

    def test_raises_when_multiple_traces_but_no_tasks_array(self):
        with tempfile.TemporaryDirectory() as d:
            traces = Path(d) / "traces"
            _write_traces(traces, ["0", "1", "2"])
            parsed = {"score": 0.33}  # the rolled-own write_result shape
            with self.assertRaises(RuntimeError) as ctx:
                _assert_tasks_aggregated(traces, parsed)
            self.assertIn("tasks_missing_from_result", str(ctx.exception))
            self.assertIn("3", str(ctx.exception))  # trace count surfaced
            self.assertIn("inline_instrumentation.py", str(ctx.exception))

    def test_raises_when_30_traces_no_tasks(self):
        # Mirror the real PostTrainBench failure (30 AIME problems).
        with tempfile.TemporaryDirectory() as d:
            traces = Path(d) / "traces"
            _write_traces(traces, [str(i) for i in range(30)])
            parsed = {"score": 0.0333}
            with self.assertRaises(RuntimeError) as ctx:
                _assert_tasks_aggregated(traces, parsed)
            self.assertIn("30", str(ctx.exception))

    # ---- edge cases: assertion should NOT misfire ----

    def test_passes_when_tasks_array_is_empty_dict_with_truthy_inner_value(self):
        # `tasks: {"0": 0.0}` -- single task, populated tasks array.
        # Should pass (assertion only fires when tasks is empty/missing).
        with tempfile.TemporaryDirectory() as d:
            traces = Path(d) / "traces"
            _write_traces(traces, ["0", "1"])
            parsed = {"score": 0.5, "tasks": {"0": 1.0}}  # 1 task in array, 2 traces -- ok
            _assert_tasks_aggregated(traces, parsed)  # no raise

    def test_raises_when_tasks_field_is_empty_dict(self):
        # Edge case: tasks key present but empty. Worth treating as bad
        # since "wrote N traces but emitted {} for tasks" is the same
        # broken-write_result class of bug.
        with tempfile.TemporaryDirectory() as d:
            traces = Path(d) / "traces"
            _write_traces(traces, ["0", "1", "2"])
            parsed = {"score": 0.0, "tasks": {}}
            with self.assertRaises(RuntimeError) as ctx:
                _assert_tasks_aggregated(traces, parsed)
            self.assertIn("tasks_missing_from_result", str(ctx.exception))

    def test_raises_when_tasks_field_is_empty_list(self):
        with tempfile.TemporaryDirectory() as d:
            traces = Path(d) / "traces"
            _write_traces(traces, ["0", "1", "2"])
            parsed = {"score": 0.0, "tasks": []}
            with self.assertRaises(RuntimeError):
                _assert_tasks_aggregated(traces, parsed)

    def test_raises_when_parsed_is_none_and_traces_present(self):
        # `load_result` can return parsed=None when the benchmark only
        # printed to stdout (no result.json). With N>1 traces on disk,
        # that's still the same broken-aggregation shape -- catch it.
        # In production this branch is mostly unreachable (the run path
        # raises `missing_result_json` earlier), but defending against
        # it here is cheap.
        with tempfile.TemporaryDirectory() as d:
            traces = Path(d) / "traces"
            _write_traces(traces, ["0", "1"])
            with self.assertRaises(RuntimeError) as ctx:
                _assert_tasks_aggregated(traces, None)
            self.assertIn("tasks_missing_from_result", str(ctx.exception))

    def test_passes_when_parsed_is_none_and_no_traces(self):
        # The legitimate "stdout-only benchmark with single aggregate
        # measurement" case. parsed=None but zero traces -> no raise.
        with tempfile.TemporaryDirectory() as d:
            traces = Path(d) / "traces"
            traces.mkdir()
            _assert_tasks_aggregated(traces, None)  # no raise

    def test_ignores_non_task_files_in_traces_dir(self):
        # Only `task_*.json` files count toward the trace count. Other
        # files (e.g. .DS_Store, README.md, debug logs) don't trigger.
        with tempfile.TemporaryDirectory() as d:
            traces = Path(d) / "traces"
            traces.mkdir(parents=True)
            # Single task file plus assorted noise:
            (traces / "task_0.json").write_text("{}")
            (traces / "README.md").write_text("notes")
            (traces / ".DS_Store").write_text("")
            (traces / "summary.json").write_text("{}")
            parsed = {"score": 0.5}
            # 1 task file -- assertion exempt.
            _assert_tasks_aggregated(traces, parsed)  # no raise

    def test_counts_only_task_prefix_files(self):
        # `taskoutcome_0.json` doesn't match the glob; `task_99.json` does.
        with tempfile.TemporaryDirectory() as d:
            traces = Path(d) / "traces"
            traces.mkdir(parents=True)
            (traces / "task_0.json").write_text("{}")
            (traces / "task_1.json").write_text("{}")
            (traces / "taskoutcome_0.json").write_text("{}")  # NOT matched
            (traces / "result.json").write_text("{}")
            parsed = {"score": 0.0}
            # 2 matching task files, no tasks in result -> fires
            with self.assertRaises(RuntimeError):
                _assert_tasks_aggregated(traces, parsed)


if __name__ == "__main__":
    unittest.main()

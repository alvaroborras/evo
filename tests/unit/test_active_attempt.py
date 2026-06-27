"""Tests for the active attempt and new CLI commands."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from evo.core import (
    attempt_dir,
    attempt_log_path,
    experiment_log_path,
    experiments_dir_for,
    graph_path,
    init_workspace,
    load_config,
    load_graph,
    update_node,
)


class TestActiveAttemptDir:
    def test_pre_created_files(self, tmp_path: Path) -> None:
        root = tmp_path / "workspace"
        init_workspace(
            root,
            target="train.py",
            benchmark="python eval.py",
            metric="max",
            gate=None,
            host="generic",
            per_exp_timeout=3600,
        )
        exp_id = "exp_0001"
        a_dir = attempt_dir(root, exp_id, 1)
        a_dir.mkdir(parents=True, exist_ok=True)
        # Simulate what _cmd_run_impl does
        metrics_path = a_dir / "metrics.jsonl"
        samples_path = a_dir / "samples.jsonl"
        stderr_log_path = a_dir / "stderr.log"
        for p in (metrics_path, samples_path, stderr_log_path):
            p.touch(exist_ok=True)
        assert metrics_path.exists()
        assert samples_path.exists()
        assert stderr_log_path.exists()

    def test_attempt_log_path(self, tmp_path: Path) -> None:
        root = tmp_path / "workspace"
        init_workspace(
            root,
            target="train.py",
            benchmark="python eval.py",
            metric="max",
            gate=None,
            host="generic",
            per_exp_timeout=3600,
        )
        exp_id = "exp_0001"
        path = attempt_log_path(root, exp_id, 1, "metrics.jsonl")
        expected = attempt_dir(root, exp_id, 1) / "metrics.jsonl"
        assert path == expected


class TestPrintMetricsLines:
    def test_filters_keys(self, tmp_path: Path) -> None:
        from evo.cli import _print_metrics_lines
        metrics_file = tmp_path / "metrics.jsonl"
        metrics_file.write_text(
            '{"loss": 0.5, "lr": 0.001, "epoch": 1}\n{"loss": 0.3, "lr": 0.0005, "epoch": 2}\n',
            encoding="utf-8",
        )
        import io
        import sys
        captured = io.StringIO()
        sys.stdout = captured
        try:
            fragment = _print_metrics_lines(metrics_file, offset=0, keys=["loss", "lr"])
        finally:
            sys.stdout = sys.__stdout__
        output = captured.getvalue().strip()
        lines = output.split("\n")
        assert len(lines) == 2
        parsed = [json.loads(line) for line in lines]
        assert all("loss" in p for p in parsed)
        assert all("lr" in p for p in parsed)
        assert all("epoch" not in p for p in parsed)

    def test_incomplete_line_handling(self, tmp_path: Path) -> None:
        from evo.cli import _print_metrics_lines
        metrics_file = tmp_path / "metrics.jsonl"
        metrics_file.write_text(
            '{"loss": 0.5}\n{"loss": 0.3',
            encoding="utf-8",
        )
        import io
        import sys
        captured = io.StringIO()
        sys.stdout = captured
        try:
            fragment = _print_metrics_lines(metrics_file, offset=0)
        finally:
            sys.stdout = sys.__stdout__
        output = captured.getvalue().strip()
        lines = output.split("\n")
        assert len(lines) == 1  # only complete line
        assert json.loads(lines[0]) == {"loss": 0.5}
        assert fragment == b'{"loss": 0.3'

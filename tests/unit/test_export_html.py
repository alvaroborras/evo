"""Tests for the HTML export functionality."""

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
    atomic_write_json,
)
from evo.export_html import export_experiment_html


class TestExportHtml:
    def test_export_creates_html_file(self, tmp_path: Path) -> None:
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
        config = load_config(root)
        graph = load_graph(root)
        # Create a mock experiment node
        exp_id = "exp_0001"
        exp_dir = experiments_dir_for(root, exp_id)
        exp_dir.mkdir(parents=True, exist_ok=True)
        a_dir = attempt_dir(root, exp_id, 1)
        a_dir.mkdir(parents=True, exist_ok=True)
        # Write some test data
        (a_dir / "metrics.jsonl").write_text(
            '{"loss": 0.5, "lr": 0.001}\n{"loss": 0.3, "lr": 0.0005}\n',
            encoding="utf-8",
        )
        (a_dir / "diff.patch").write_text(
            "--- a/train.py\n+++ b/train.py\n@@ -1 +1 @@\n-old\n+new\n",
            encoding="utf-8",
        )
        # Add the node to the graph
        graph["nodes"][exp_id] = {
            "id": exp_id,
            "parent": "root",
            "status": "active",
            "current_attempt": 1,
            "hypothesis": "Test hypothesis",
            "created_at": "2026-01-01T00:00:00Z",
            "updated_at": "2026-01-01T00:00:00Z",
        }
        atomic_write_json(graph_path(root), graph)

        output = exp_dir / "report.html"
        result = export_experiment_html(root, exp_id, output_path=output)
        assert result.exists()
        content = result.read_text(encoding="utf-8")
        assert exp_id in content
        assert "Test hypothesis" in content
        assert "loss" in content

    def test_export_default_output_path(self, tmp_path: Path) -> None:
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
        exp_dir = experiments_dir_for(root, exp_id)
        exp_dir.mkdir(parents=True, exist_ok=True)
        a_dir = attempt_dir(root, exp_id, 1)
        a_dir.mkdir(parents=True, exist_ok=True)

        # Add the node to the graph
        graph = load_graph(root)
        graph["nodes"][exp_id] = {
            "id": exp_id,
            "parent": "root",
            "status": "active",
            "current_attempt": 1,
            "created_at": "2026-01-01T00:00:00Z",
            "updated_at": "2026-01-01T00:00:00Z",
        }
        atomic_write_json(graph_path(root), graph)

        result = export_experiment_html(root, exp_id)
        assert result.exists()
        assert result.name == "report.html"

    def test_export_unknown_experiment(self, tmp_path: Path) -> None:
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
        with pytest.raises(ValueError, match="Unknown experiment"):
            export_experiment_html(root, "nonexistent")

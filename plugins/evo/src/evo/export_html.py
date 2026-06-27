"""HTML export for evo experiments.

Generates a self-contained HTML report for an experiment with live dashboard
state, metrics, samples, checkpoints, and diff.
"""

from __future__ import annotations

import json
import html
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def _escape(text: str) -> str:
    return html.escape(text, quote=True)


def _format_score(score: float | None) -> str:
    if score is None:
        return "N/A"
    return f"{score:.6f}"


def _render_metrics_table(metrics: list[dict]) -> str:
    if not metrics:
        return "<p>No metrics recorded yet.</p>"
    # Collect all unique keys
    all_keys: set[str] = set()
    for m in metrics:
        all_keys.update(m.keys())
    keys = sorted(all_keys)
    rows = []
    for m in metrics:
        cells = "".join(f"<td>{_escape(str(m.get(k, '')))}</td>" for k in keys)
        rows.append(f"<tr>{cells}</tr>")
    header = "".join(f"<th>{_escape(k)}</th>" for k in keys)
    return f"""
    <table class="metrics">
      <thead><tr>{header}</tr></thead>
      <tbody>{''.join(rows)}</tbody>
    </table>
    """


def _render_samples(samples: list[dict], max_lines: int = 50) -> str:
    if not samples:
        return "<p>No samples recorded yet.</p>"
    lines = []
    for s in samples[:max_lines]:
        text = s.get("text") or s.get("output") or json.dumps(s)
        lines.append(f"<pre>{_escape(text)}</pre>")
    if len(samples) > max_lines:
        lines.append(f"<p>... and {len(samples) - max_lines} more samples</p>")
    return "\n".join(lines)


def _render_checkpoints(checkpoints: list[dict]) -> str:
    if not checkpoints:
        return "<p>No checkpoints recorded yet.</p>"
    items = []
    for ckpt in checkpoints:
        size = ckpt.get("size", 0)
        if size > 1024 * 1024:
            size_str = f"{size / (1024 * 1024):.1f} MB"
        elif size > 1024:
            size_str = f"{size / 1024:.1f} KB"
        else:
            size_str = f"{size} B"
        items.append(f"<li><code>{_escape(ckpt.get('path', ''))}</code> ({size_str})</li>")
    return "<ul>" + "\n".join(items) + "</ul>"


def _render_diff(diff_text: str, max_lines: int = 500) -> str:
    if not diff_text.strip():
        return "<p>No diff available.</p>"
    lines = diff_text.splitlines()
    if len(lines) > max_lines:
        lines = lines[:max_lines]
        lines.append(f"... ({len(diff_text.splitlines()) - max_lines} more lines)")
    return f"<pre class='diff'>{''.join(html.escape(l) + '\\n' for l in lines)}</pre>"


def export_experiment_html(
    root: Path,
    exp_id: str,
    *,
    output_path: Path | None = None,
    max_log_lines: int = 50_000,
    full: bool = False,
) -> Path:
    """Generate a self-contained HTML report for an experiment.

    Args:
        root: Workspace root path.
        exp_id: Experiment ID.
        output_path: Where to write the HTML file. Defaults to <exp_dir>/report.html.
        max_log_lines: Max lines from log files to include (default 50k).
        full: If True, include all log lines (ignore max_log_lines).

    Returns:
        Path to the generated HTML file.
    """
    from .core import (
        attempt_dir,
        attempt_log_path,
        attempt_traces_dir,
        experiments_dir_for,
        load_config,
        load_graph,
    )

    config = load_config(root)
    graph = load_graph(root)
    node = graph["nodes"].get(exp_id)
    if node is None:
        raise ValueError(f"Unknown experiment: {exp_id}")

    attempt = int(node.get("current_attempt", 0))
    exp_dir = experiments_dir_for(root, exp_id)
    a_dir = attempt_dir(root, exp_id, attempt) if attempt > 0 else exp_dir

    # Collect data
    metrics: list[dict] = []
    samples: list[dict] = []
    checkpoints: list[dict] = []
    diff_text = ""
    benchmark_log = ""
    stderr_log = ""
    traces: dict[str, Any] = {}

    if attempt > 0:
        # Metrics
        metrics_path = attempt_log_path(root, exp_id, attempt, "metrics.jsonl")
        if metrics_path.exists():
            for line in metrics_path.read_text(encoding="utf-8", errors="replace").splitlines():
                line = line.strip()
                if line:
                    try:
                        metrics.append(json.loads(line))
                    except json.JSONDecodeError:
                        pass

        # Samples
        samples_path = attempt_log_path(root, exp_id, attempt, "samples.jsonl")
        if samples_path.exists():
            for line in samples_path.read_text(encoding="utf-8", errors="replace").splitlines():
                line = line.strip()
                if line:
                    try:
                        samples.append(json.loads(line))
                    except json.JSONDecodeError:
                        pass

        # Checkpoints
        ckpt_dir = a_dir / "checkpoints"
        if ckpt_dir.exists():
            for path in sorted(ckpt_dir.rglob("*")):
                if path.is_file():
                    checkpoints.append({
                        "path": str(path.relative_to(ckpt_dir)),
                        "size": path.stat().st_size,
                        "mtime": path.stat().st_mtime,
                    })

        # Diff
        diff_path = a_dir / "diff.patch"
        if diff_path.exists():
            diff_text = diff_path.read_text(encoding="utf-8", errors="replace")

        # Benchmark log
        log_path = a_dir / "benchmark.log"
        if log_path.exists():
            text = log_path.read_text(encoding="utf-8", errors="replace")
            if not full:
                lines = text.splitlines()
                if len(lines) > max_log_lines:
                    text = "\n".join(lines[-max_log_lines:])
                    text = f"... ({len(lines) - max_log_lines} lines truncated)\n{text}"
            benchmark_log = text

        # Stderr log
        err_path = a_dir / "stderr.log"
        if err_path.exists():
            text = err_path.read_text(encoding="utf-8", errors="replace")
            if not full:
                lines = text.splitlines()
                if len(lines) > max_log_lines:
                    text = "\n".join(lines[-max_log_lines:])
                    text = f"... ({len(lines) - max_log_lines} lines truncated)\n{text}"
            stderr_log = text

        # Traces
        traces_dir = attempt_traces_dir(root, exp_id, attempt)
        if traces_dir.exists():
            for path in sorted(traces_dir.glob("*.json")):
                try:
                    traces[path.name] = json.loads(path.read_text(encoding="utf-8"))
                except Exception:
                    pass

    # Render HTML
    metric = config.get("metric", "max")
    score = node.get("score")
    status = node.get("status", "unknown")
    hypothesis = node.get("hypothesis", "")
    parent = node.get("parent", "")
    created_at = node.get("created_at", "")
    updated_at = node.get("updated_at", "")

    traces_html = ""
    for name, trace in traces.items():
        traces_html += f"<h4>{_escape(name)}</h4><pre>{_escape(json.dumps(trace, indent=2))}</pre>"

    html_content = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>evo: {_escape(exp_id)} - {_escape(status)}</title>
<style>
  body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; margin: 2em; background: #fafafa; color: #333; }}
  h1 {{ border-bottom: 2px solid #4a9eff; padding-bottom: 0.5em; }}
  h2 {{ color: #555; margin-top: 2em; }}
  .meta {{ display: grid; grid-template-columns: 1fr 1fr; gap: 1em; margin: 1em 0; }}
  .meta dt {{ font-weight: bold; color: #666; }}
  .meta dd {{ margin: 0; }}
  .status {{ display: inline-block; padding: 0.25em 0.75em; border-radius: 4px; font-weight: bold; }}
  .status.committed {{ background: #d4edda; color: #155724; }}
  .status.evaluated {{ background: #fff3cd; color: #856404; }}
  .status.active {{ background: #cce5ff; color: #004085; }}
  .status.failed {{ background: #f8d7da; color: #721c24; }}
  table.metrics {{ border-collapse: collapse; width: 100%; margin: 1em 0; }}
  table.metrics th, table.metrics td {{ border: 1px solid #ddd; padding: 0.5em; text-align: left; }}
  table.metrics th {{ background: #f5f5f5; }}
  pre {{ background: #f8f9fa; padding: 1em; overflow-x: auto; border-radius: 4px; }}
  pre.diff {{ background: #fff; }}
  .section {{ background: white; border: 1px solid #e0e0e0; border-radius: 8px; padding: 1.5em; margin: 1.5em 0; }}
</style>
</head>
<body>
<h1>evo: {_escape(exp_id)}</h1>
<div class="section">
  <dl class="meta">
    <dt>Status</dt><dd><span class="status {_escape(status)}">{_escape(status)}</span></dd>
    <dt>Score ({_escape(metric)})</dt><dd>{_format_score(score)}</dd>
    <dt>Attempt</dt><dd>{attempt}</dd>
    <dt>Hypothesis</dt><dd>{_escape(hypothesis)}</dd>
    <dt>Parent</dt><dd>{_escape(parent)}</dd>
    <dt>Created</dt><dd>{_escape(created_at)}</dd>
    <dt>Updated</dt><dd>{_escape(updated_at)}</dd>
  </dl>
</div>

<div class="section">
  <h2>Metrics ({len(metrics)} lines)</h2>
  {_render_metrics_table(metrics)}
</div>

<div class="section">
  <h2>Samples ({len(samples)} lines)</h2>
  {_render_samples(samples)}
</div>

<div class="section">
  <h2>Checkpoints ({len(checkpoints)} files)</h2>
  {_render_checkpoints(checkpoints)}
</div>

<div class="section">
  <h2>Diff</h2>
  {_render_diff(diff_text)}
</div>

<div class="section">
  <h2>Benchmark Log</h2>
  <pre>{_escape(benchmark_log) if benchmark_log else '<em>Empty</em>'}</pre>
</div>

<div class="section">
  <h2>Stderr Log</h2>
  <pre>{_escape(stderr_log) if stderr_log else '<em>Empty</em>'}</pre>
</div>

<div class="section">
  <h2>Traces</h2>
  {traces_html if traces_html else '<p>No traces recorded yet.</p>'}
</div>

<hr>
<p style="color:#999; font-size:0.9em;">Generated by evo at {datetime.now(timezone.utc).isoformat()}</p>
</body>
</html>"""

    if output_path is None:
        output_path = exp_dir / "report.html"
    output_path.write_text(html_content, encoding="utf-8")
    return output_path

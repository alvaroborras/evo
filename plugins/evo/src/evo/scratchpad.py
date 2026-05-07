from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from . import frontier_strategies as fs
from .core import (
    ascii_tree,
    attempt_outcome_path,
    best_committed_node,
    best_committed_score,
    collect_gates_from_path,
    experiments_path,
    frontier_nodes,
    graph_path,
    infra_path,
    load_annotations,
    load_config,
    list_all_notes,
    load_graph,
    parse_diff_patch,
    path_to_node,
)


FRONTIER_DISPLAY_CAP = 50
AWAITING_DISPLAY_CAP = 10
ANNOTATIONS_DISPLAY_CAP = 15
RECENT_EXPERIMENTS_DISPLAY_CAP = 25
RECENT_EVALUATED_WINDOW = 20  # how far back to consider an evaluated node "recent"
                               # for the bounded tree's relevance check
NOTES_DISPLAY_CAP = 20


def _format_strategy_label(strategy: dict[str, Any]) -> str:
    kind = strategy.get("kind", "?")
    params = strategy.get("params") or {}
    if not params:
        return kind
    params_str = " ".join(f"{k}={v}" for k, v in sorted(params.items()))
    return f"{kind} {params_str}"


def _rank_frontier(root: Path, raw_frontier: list[dict[str, Any]],
                   config: dict[str, Any], metric: str) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Rank branchable nodes via the configured strategy.

    Returns (ranked_summaries, strategy). On a broken config we fall back to
    score-sorted raw frontier with a synthetic 'fallback' strategy so the
    scratchpad still renders.
    """
    summaries = [
        {
            "id": n["id"],
            "score": n.get("score"),
            "eval_epoch": n.get("eval_epoch"),
            "hypothesis": n.get("hypothesis"),
        }
        for n in raw_frontier
    ]
    try:
        strategy = fs.resolve_from_config(config)
        outcomes: dict[str, dict] = {}
        if strategy["kind"] == "pareto_per_task":
            for n in raw_frontier:
                attempt = n.get("current_attempt")
                if not attempt:
                    continue
                path = attempt_outcome_path(root, n["id"], int(attempt))
                if path.exists():
                    try:
                        outcomes[n["id"]] = json.loads(path.read_text(encoding="utf-8"))
                    except json.JSONDecodeError:
                        pass
        # seed=0 keeps the scratchpad render deterministic across calls;
        # actual dispatch uses fresh randomness.
        ranked, _ = fs.pick(summaries, strategy, metric, outcomes=outcomes, seed=0)
        return ranked, strategy
    except (ValueError, KeyError):
        ranked = sorted(
            summaries,
            key=lambda n: (-(n.get("score") if n.get("score") is not None else float("-inf")), n["id"]),
        )
        for i, n in enumerate(ranked, 1):
            n["rank"] = i
        return ranked, {"kind": "fallback", "params": {}}


def _truncate(text: str, limit: int = 240) -> str:
    compact = " ".join(text.strip().split())
    if len(compact) <= limit:
        return compact
    return compact[: limit - 3] + "..."


def _hyp_short(text: str | None, limit: int = 120) -> str:
    """Hypothesis text rendered short for inline use in tree/frontier/awaiting."""
    if not text:
        return ""
    return _truncate(text, limit=limit)


def _build_branch_root_map(graph: dict[str, Any]) -> dict[str, str]:
    """For each non-root node, the id of its top-level ancestor (the child of root
    that begins this branch). Computed once per scratchpad render so annotation
    grouping stays O(annotations) instead of O(annotations * depth)."""
    nodes = graph["nodes"]
    cache: dict[str, str] = {"root": "root"}

    def resolve(node_id: str) -> str:
        if node_id in cache:
            return cache[node_id]
        node = nodes.get(node_id)
        if not node:
            return "root"
        parent = node.get("parent")
        if parent in (None, "root"):
            cache[node_id] = node_id
            return node_id
        result = resolve(parent)
        cache[node_id] = result
        return result

    for node_id in nodes:
        resolve(node_id)
    return cache


# Single-letter status codes for the compact tree. Documented in the section
# header so agents can decode without external reference.
_STATUS_GLYPH = {
    "root": "·",
    "active": "A",
    "evaluated": "E",
    "committed": "C",
    "discarded": "D",
    "pruned": "P",
    "failed": "F",
}


def _bounded_tree(graph: dict[str, Any], metric: str,
                  branch_root_map: dict[str, str],
                  recent_evaluated_ids: set[str],
                  best_path_ids: set[str],
                  uniform_epoch: int | None = None) -> str:
    """Render the tree compactly. Two-space indent (no box-drawing chars).
    Each node line: '  exp_NNNN G score [epoch=K] [hyp]' where G is the
    single-char status glyph. ★ marker prefixes best-path nodes; the
    Best Path section is dropped because the tree carries the same info.

    Subtrees containing active / best-path / recent-evaluated descendants
    expand fully. Cold subtrees collapse to one line with descendant
    count and best-score summary (omitted when N=0)."""
    nodes = graph["nodes"]
    sign = -1 if (metric or "").lower() == "min" else 1

    relevance_cache: dict[str, bool] = {}

    def is_relevant(node_id: str) -> bool:
        if node_id in relevance_cache:
            return relevance_cache[node_id]
        node = nodes.get(node_id)
        if not node:
            relevance_cache[node_id] = False
            return False
        if node_id == "root":
            relevance_cache[node_id] = True
            return True
        if node.get("status") == "active":
            relevance_cache[node_id] = True
            return True
        if node_id in best_path_ids or node_id in recent_evaluated_ids:
            relevance_cache[node_id] = True
            return True
        for child_id in node.get("children", []):
            if child_id in nodes and is_relevant(child_id):
                relevance_cache[node_id] = True
                return True
        relevance_cache[node_id] = False
        return False

    stats_cache: dict[str, tuple[int, float | None]] = {}

    def descendant_stats(node_id: str) -> tuple[int, float | None]:
        if node_id in stats_cache:
            return stats_cache[node_id]
        count = 0
        best_score: float | None = None
        node = nodes.get(node_id)
        if not node:
            stats_cache[node_id] = (0, None)
            return 0, None
        for child_id in node.get("children", []):
            if child_id not in nodes:
                continue
            count += 1
            child = nodes[child_id]
            cs = child.get("score")
            if cs is not None:
                if best_score is None or sign * cs > sign * best_score:
                    best_score = cs
            sub_count, sub_best = descendant_stats(child_id)
            count += sub_count
            if sub_best is not None:
                if best_score is None or sign * sub_best > sign * best_score:
                    best_score = sub_best
        stats_cache[node_id] = (count, best_score)
        return count, best_score

    def label(node: dict[str, Any], collapsed: bool = False) -> str:
        glyph = _STATUS_GLYPH.get(node.get("status", ""), "?")
        marker = "★ " if node["id"] in best_path_ids and node["id"] != "root" else ""
        parts: list[str] = [f"{marker}{node['id']}", glyph]
        if node.get("score") is not None:
            parts.append(str(node["score"]))
        epoch = node.get("eval_epoch")
        if epoch is not None and epoch != uniform_epoch:
            parts.append(f"e{epoch}")
        if node.get("pruned_reason"):
            parts.append("pruned")
        if node.get("gates"):
            parts.append(f"g{len(node['gates'])}")
        if node.get("hypothesis") and node["id"] != "root":
            parts.append(_hyp_short(node["hypothesis"]))
        line = " ".join(parts)
        if collapsed:
            sub_count, sub_best = descendant_stats(node["id"])
            if sub_count > 0:
                best_str = f" best={sub_best}" if sub_best is not None else ""
                line += f" (+{sub_count}{best_str})"
        return line

    lines: list[str] = []

    def walk(node_id: str, depth: int = 0) -> None:
        node = nodes.get(node_id)
        if not node:
            return
        indent = "  " * depth
        if node_id == "root":
            lines.append(label(node))
        elif not is_relevant(node_id):
            lines.append(indent + label(node, collapsed=True))
            return
        else:
            lines.append(indent + label(node))
        children = sorted([c for c in node.get("children", []) if c in nodes])
        for child_id in children:
            walk(child_id, depth + 1)

    walk("root")
    return "\n".join(lines)


def _group_annotations_by_branch_task(
    annotations: list[dict[str, Any]],
    branch_root_map: dict[str, str],
) -> list[tuple[tuple[str, str], dict[str, Any]]]:
    """Group annotations by (branch_root, task_id), keeping only the latest per
    key. Sorted by timestamp descending so caller can take the top K most
    recent insights diverse across branches.

    Falls back to ('unknown', task_id) for annotations whose experiment_id is
    no longer in the graph (legacy data)."""
    latest: dict[tuple[str, str], dict[str, Any]] = {}
    for entry in annotations:
        exp_id = entry.get("experiment_id")
        branch = branch_root_map.get(exp_id, "unknown") if exp_id else "unknown"
        task = entry.get("task_id") or "global"
        key = (branch, task)
        existing = latest.get(key)
        if existing is None or entry.get("timestamp", "") >= existing.get("timestamp", ""):
            latest[key] = entry
    return sorted(
        latest.items(),
        key=lambda item: item[1].get("timestamp", ""),
        reverse=True,
    )


def _dedup_discarded(discarded: list[dict[str, Any]], limit: int = 15) -> list[tuple[str, int]]:
    """Deduplicate discarded hypotheses by normalized text. Returns (hypothesis, count) pairs."""
    counts: dict[str, int] = {}
    display: dict[str, str] = {}
    for node in discarded:
        hyp = node.get("hypothesis", "")
        key = " ".join(hyp.lower().split())
        counts[key] = counts.get(key, 0) + 1
        display[key] = hyp  # keep the original casing from the latest
    sorted_items = sorted(counts.items(), key=lambda item: -item[1])
    return [(display[key], count) for key, count in sorted_items[:limit]]


def build_scratchpad(root: Path) -> str:
    config = load_config(root)
    graph = load_graph(root)
    annotations = load_annotations(root).get("annotations", [])
    infra = json.loads(infra_path(root).read_text(encoding="utf-8")).get("events", []) if infra_path(root).exists() else []
    metric = config.get("metric", "max")
    committed = [node for node in graph["nodes"].values() if node.get("status") == "committed"]
    discarded = [node for node in graph["nodes"].values() if node.get("status") == "discarded"]
    evaluated = [node for node in graph["nodes"].values() if node.get("status") == "evaluated"]
    active = [node for node in graph["nodes"].values() if node.get("status") == "active"]
    best = best_committed_score(graph, metric)
    frontier = frontier_nodes(graph)
    recent_all = sorted(
        [node for node in graph["nodes"].values() if node["id"] != "root"],
        key=lambda item: item.get("updated_at", ""),
        reverse=True,
    )
    recent = recent_all[:RECENT_EXPERIMENTS_DISPLAY_CAP]
    branch_root_map = _build_branch_root_map(graph)
    recent_evaluated_ids = {
        n["id"] for n in recent_all[:RECENT_EVALUATED_WINDOW]
        if n.get("status") == "evaluated"
    }
    best_committed = best_committed_node(graph, metric)
    best_path_ids: set[str] = set()
    if best_committed and best_committed["id"] != "root":
        best_path_ids = {n["id"] for n in path_to_node(graph, best_committed["id"])}

    # Compute uniform epoch — if every non-root node shares the same epoch,
    # we can drop the per-line epoch suffix in the tree and frontier.
    epochs_seen = {n.get("eval_epoch") for n in graph["nodes"].values()
                   if n["id"] != "root" and n.get("eval_epoch") is not None}
    uniform_epoch = next(iter(epochs_seen)) if len(epochs_seen) == 1 else None

    # Status: one compact line + optional active-worker line.
    total = len(graph["nodes"]) - 1
    counts_short = (
        f"{len(committed)}c/{len(evaluated)}e/{len(discarded)}d/{len(active)}a"
    )
    epoch_now = config.get("current_eval_epoch", 1)
    epoch_str = f"e{epoch_now}" if epoch_now != 1 else ""
    lines = [
        "# Scratchpad",
        "",
        "## Status",
        f"- metric={metric} best={best} total={total} {counts_short} {epoch_str}".rstrip(),
    ]
    if active:
        lines.append(f"- {len(active)} active worker(s)")

    # Tree section header documents the status glyphs and the ★ marker so
    # the agent can decode the compact form without external reference.
    epoch_note = f", epoch={uniform_epoch} (uniform; per-node epochs omitted)" if uniform_epoch is not None else ""
    lines.extend([
        "",
        f"## Tree (status: A=active C=committed E=evaluated D=discarded P=pruned F=failed; ★=best path{epoch_note})",
        "```",
        _bounded_tree(graph, metric, branch_root_map,
                      recent_evaluated_ids, best_path_ids,
                      uniform_epoch=uniform_epoch),
        "```",
    ])
    # Best Path is now ★-marked in the tree; no separate section needed.

    # Frontier (strategy-ranked; only render if there's anything to surface)
    ranked_frontier, strategy = _rank_frontier(root, frontier, config, metric)
    if ranked_frontier:
        lines.extend(["", f"## Frontier (strategy: {_format_strategy_label(strategy)})"])
        shown = ranked_frontier[:FRONTIER_DISPLAY_CAP]
        for node in shown:
            score = node.get("score")
            ne = node.get("epoch")
            epoch_part = f" e{ne}" if ne is not None and ne != uniform_epoch else ""
            lines.append(
                f"- {node['id']} {score}{epoch_part} {_hyp_short(node.get('hypothesis'))}".rstrip()
            )
        if len(ranked_frontier) > FRONTIER_DISPLAY_CAP:
            lines.append(
                f"(+{len(ranked_frontier) - FRONTIER_DISPLAY_CAP} more — see `evo frontier`)"
            )

    if evaluated:
        lines.extend(["", "## Awaiting Decision"])
        lines.append("Gate failed or score regressed. Retry: edit + `evo run`. Abandon: `evo discard`. Lease still held.")
        evaluated_recent = sorted(
            evaluated,
            key=lambda n: n.get("updated_at", ""),
            reverse=True,
        )
        for node in evaluated_recent[:AWAITING_DISPLAY_CAP]:
            attempts = int(node.get("evaluated_attempts", 0))
            gate_failed = node.get("gate_failures") or []
            gates_part = f" gate_failed={gate_failed}" if gate_failed else ""
            lines.append(
                f"- {node['id']} {node.get('score')} a{attempts}{gates_part} "
                f"{_hyp_short(node.get('hypothesis'))}".rstrip()
            )
        if len(evaluated_recent) > AWAITING_DISPLAY_CAP:
            lines.append(
                f"(+{len(evaluated_recent) - AWAITING_DISPLAY_CAP} more — see `evo awaiting`)"
            )

    # Gates (only when present)
    root_gates = graph["nodes"].get("root", {}).get("gates", [])
    if root_gates or any(n.get("gates") for n in frontier):
        lines.extend(["", "## Gates"])
        if root_gates:
            for g in root_gates:
                lines.append(f"- {g['name']} (root): {_truncate(g['command'], 120)}")
        seen_names = {g["name"] for g in root_gates}
        for node in frontier[:10]:
            effective = collect_gates_from_path(graph, node["id"])
            for g in effective:
                if g["name"] not in seen_names:
                    seen_names.add(g["name"])
                    lines.append(f"- {g['name']} (tree): {_truncate(g['command'], 120)}")

    # Recent Experiments — only when the tree is collapsing nodes (otherwise
    # it's pure duplication of what's already in the tree section above).
    total_nonroot = len(graph["nodes"]) - 1
    if recent and total_nonroot > RECENT_EXPERIMENTS_DISPLAY_CAP:
        lines.extend(["", "## Recent Experiments"])
        for node in recent:
            glyph = _STATUS_GLYPH.get(node.get("status", ""), "?")
            parent = node.get("parent") or "root"
            lines.append(
                f"- {node['id']} ← {parent} {glyph} {node.get('score')} {_hyp_short(node.get('hypothesis'))}".rstrip()
            )

    # Annotations grouped by (branch_root, task)
    if annotations:
        grouped = _group_annotations_by_branch_task(annotations, branch_root_map)
        if grouped:
            lines.extend(["", "## Annotations"])
            for (branch, task_id), entry in grouped[:ANNOTATIONS_DISPLAY_CAP]:
                lines.append(
                    f"- {branch}/{task_id} ({entry['experiment_id']}): "
                    f"{_truncate(entry['analysis'])}"
                )
            if len(grouped) > ANNOTATIONS_DISPLAY_CAP:
                lines.append(
                    f"(+{len(grouped) - ANNOTATIONS_DISPLAY_CAP} more — see `evo annotations`)"
                )

    # What Not To Try (only when there are discards)
    if discarded:
        deduped = _dedup_discarded(discarded)
        if deduped:
            lines.extend(["", "## What Not To Try"])
            for hyp, count in deduped:
                suffix = f" (x{count})" if count > 1 else ""
                lines.append(f"- {_truncate(hyp)}{suffix}")

    # Infrastructure log (only when present)
    if infra:
        lines.extend(["", "## Infrastructure Log"])
        for event in infra[-8:]:
            suffix = " (breaking)" if event.get("breaking") else ""
            # 0.3.0 frontier events shipped with key "at" and no "message"
            # (#22). Read tolerantly so workspaces upgrading to >=0.3.1 don't
            # KeyError on the pre-existing bad events still in their log.
            ts = event.get("timestamp") or event.get("at") or "?"
            msg = event.get("message") or f"{event.get('kind', '?')} event"
            lines.append(f"- {ts}: {msg}{suffix}")

    # Notes — cross-cutting findings authored by the orchestrator (and
    # eventually humans) for future agents to consume. Surfaces both
    # per-node notes (`evo set <id> --note ...`) and workspace-level
    # notes (`evo note ...`) in a unified list, most-recent first.
    all_notes = list_all_notes(graph)
    if all_notes:
        lines.extend(["", "## Notes (cross-cutting findings for future agents)"])
        for entry in all_notes[:NOTES_DISPLAY_CAP]:
            text = (entry.get("text") or "").strip()
            if not text:
                continue
            ts = entry.get("timestamp", "")
            scope = entry.get("exp_id") or "workspace"
            lines.append(f"- [{scope} / {ts}] {text}")
        if len(all_notes) > NOTES_DISPLAY_CAP:
            lines.append(
                f"(+{len(all_notes) - NOTES_DISPLAY_CAP} more — see `evo notes`)"
            )

    # Drill-downs: tool menu shown last so the orchestrator's most-recent
    # context window includes it. Bounded sections above point here via
    # footers like "(+N more — see evo awaiting)".
    lines.extend([
        "",
        "## Drill-downs",
        "  evo show <id>           full state of one experiment",
        "  evo tree                full unbounded tree",
        "  evo path <id>           root-to-node chain with scores",
        "  evo diff <id> [other]   diff vs parent or between two",
        "  evo awaiting            evaluated nodes awaiting decision",
        "  evo discards [--like]   discarded hypotheses, searchable",
        "  evo annotations [...]   all annotations, filterable",
        "  evo notes [--exp X]     all notes (workspace + per-node), most-recent first",
        "  evo frontier [--strat]  strategy-ranked branchable nodes",
        "  evo restore <id>        revive a pruned/discarded node",
    ])
    lines.append("")
    return "\n".join(lines)



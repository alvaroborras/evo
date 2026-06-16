from __future__ import annotations

import json
import os
import shutil
import sys
from pathlib import Path
from typing import Any

from .core import (
    CONFIG_FILE,
    GRAPH_FILE,
    best_committed_node,
    best_committed_score,
    evo_dir,
    frontier_nodes,
    is_valid_result_node,
    list_runs,
)


def _detect_terminal_size(default: tuple[int, int] = (80, 24)) -> tuple[int, int]:
    """Live terminal size, ignoring stale COLUMNS/LINES env vars.

    `shutil.get_terminal_size` consults env first and only queries the TTY
    if env is unset. Some shells (or `uv run`) export COLUMNS at session
    start and never refresh on resize, so we'd report the old width. And
    when stdout is a pipe (piped, redirected, or wrapped by a host tool
    that captures output), the env fallback hits and reports 80 even
    though the controlling terminal might be wider/narrower.

    Strategy: query /dev/tty directly first — that's the controlling
    terminal and always reflects the current SIGWINCH state. Fall back to
    sys.stdout / stderr / shutil only if /dev/tty isn't available
    (Windows, daemons with no controlling tty).
    """
    for src in ("/dev/tty",):
        try:
            with open(src) as fh:
                size = os.get_terminal_size(fh.fileno())
                return size.columns, size.lines
        except (OSError, FileNotFoundError):
            pass
    for stream in (sys.stdout, sys.stderr):
        try:
            size = os.get_terminal_size(stream.fileno())
            return size.columns, size.lines
        except (OSError, AttributeError, ValueError):
            pass
    return shutil.get_terminal_size(default)


# ANSI styling. Mirrors the dashboard scatter palette:
#   committed=green, failed=red, active=purple, others=grey,
#   stair line + spine ring = amber, best dot = bright white.
RESET = "\033[0m"
BOLD = "\033[1m"
FG = {
    "green":   "\033[32m",
    "red":     "\033[31m",
    "magenta": "\033[35m",
    "yellow":  "\033[33m",
    "grey":    "\033[90m",
    "white":   "\033[97m",
}

STATUS_COLOR = {
    "committed": "green",
    "active":    "magenta",
    "failed":    "red",
    "evaluated": "grey",
    "pending":   "grey",
    "discarded": "grey",
    "pruned":    "grey",
}

# Dot-tier glyphs. Compact (smaller chars) kicks in when cells are scarce or
# experiments are packed densely, so adjacent dots don't visually collide.
# Standard is the visual ceiling — no "extra-large" tier, because at wide
# widths the connecting stair line already gives the chart enough presence.
# All glyphs chosen to be reliably width-1 in common monospace fonts.
DOT_TIERS: dict[str, dict[str, str]] = {
    "compact":  {"scored": "•", "no_score": "·", "best": "*"},
    "standard": {"scored": "●", "no_score": "○", "best": "★"},
}


def _pick_dot_tier(plot_w: int, n_exps: int) -> str:
    density = n_exps / max(1, plot_w)
    if plot_w < 40 or density > 0.4:
        return "compact"
    return "standard"


def _style(text: str, color: str | None, *, bold: bool = False, use_color: bool = True) -> str:
    if not use_color or color is None and not bold:
        return text
    prefix = ""
    if bold:
        prefix += BOLD
    if color:
        prefix += FG.get(color, "")
    if not prefix:
        return text
    return f"{prefix}{text}{RESET}"


def _running_best(graph: dict, metric: str) -> tuple[set[str], str | None]:
    """Nodes that were the cumulative best at the time they were created.

    Walk valid committed-result experiments in creation order; each one whose score
    surpasses the running best becomes a member of this set. The overall
    champion (the last entry) is also returned separately so the renderer
    can mark it with ★ and leave the rest as the amber spine.

    Different from a pure lineage walk (root → best via parent pointers):
    a lineage member can be on the spine without ever having been the best
    at its time (e.g. an early branch that was later surpassed by its own
    descendants and is now only "best-by-association"). This function
    surfaces only the time-series of actual record-holders.
    """
    nodes = graph.get("nodes", {})
    is_max = metric == "max"
    ordered = sorted(
        (n for nid, n in nodes.items() if nid != "root"),
        key=lambda n: n.get("created_at") or "",
    )
    record_set: set[str] = set()
    best_id: str | None = None
    best_score: float | None = None
    for n in ordered:
        if not is_valid_result_node(graph, n):
            continue
        s = n["score"]
        if best_score is None or (is_max and s > best_score) or (not is_max and s < best_score):
            best_id, best_score = n["id"], s
            record_set.add(best_id)
    return record_set, best_id


def _host_right_margin() -> int:
    """Extra right-margin to reserve when the output is being rendered by
    a host that wraps tool output in its own framing (borders, indents).

    Claude Code's CLI puts each tool output inside a bordered block that
    eats ~4-6 columns; without compensation a chart sized exactly to the
    terminal width wraps inside the block and looks broken. We sniff the
    env var the host sets and reserve extra space.
    """
    import os
    if os.environ.get("CLAUDECODE") == "1":
        return 6
    return 0


def _render_run(
    run: dict[str, Any],
    graph: dict[str, Any],
    cfg: dict[str, Any],
    width: int,
    plot_h: int,
    use_color: bool,
    extra_margin: int = 0,
    dot_tier: str | None = None,
) -> str:
    metric = cfg.get("metric", "max")
    is_max = metric == "max"
    exps = sorted(
        (n for nid, n in graph.get("nodes", {}).items() if nid != "root"),
        key=lambda n: n.get("created_at") or "",
    )

    header_parts = [run["id"]]
    if run.get("target"):
        header_parts.append(f"target={run['target']}")
    header_parts.append(f"metric={metric}")
    if run.get("active"):
        header_parts.append("active")
    header = "  " + " · ".join(header_parts)

    if not exps:
        return (
            _style(header, "grey", bold=True, use_color=use_color)
            + "\n    no experiments yet\n"
        )

    # Y-range from scored experiments only.
    scored = [n["score"] for n in exps if n.get("score") is not None]
    if scored:
        ymin, ymax = min(scored), max(scored)
        if ymax == ymin:
            ymax = ymin + 1
    else:
        ymin, ymax = 0.0, 1.0
    pad = (ymax - ymin) * 0.1
    ymin -= pad
    ymax += pad
    yrange = ymax - ymin or 1.0

    # Plot geometry. Each row is: LABEL(5) + " " + "┤" + ROW_CHARS(plot_w).
    # Reserve a right-side margin so the chart never quite touches the
    # right edge — protects against host wrappers (Claude Code's bordered
    # block, etc.) that frame tool output and eat columns.
    LABEL_PREFIX = 7  # " 0.86 ┤"
    right_margin = 2 + max(0, extra_margin)
    plot_w = max(20, width - LABEL_PREFIX - right_margin)
    plot_h = max(6, plot_h)

    tier = dot_tier or _pick_dot_tier(plot_w, len(exps))
    glyphs = DOT_TIERS.get(tier, DOT_TIERS["standard"])

    def col_for(i: int) -> int:
        if len(exps) == 1:
            return plot_w // 2
        return int(round(i * (plot_w - 1) / (len(exps) - 1)))

    def row_for(score: float) -> int:
        frac = (score - ymin) / yrange
        return max(0, min(plot_h - 1, int(round((1 - frac) * (plot_h - 1)))))

    # Grid cell: (char, color, bold). None color → no ANSI.
    grid: list[list[tuple[str, str | None, bool]]] = [
        [(" ", None, False)] * plot_w for _ in range(plot_h)
    ]

    spine, best_id = _running_best(graph, metric)

    # Cumulative-best stair points (committed scored only). Stop the line
    # at the champion — any committed experiments after that point can't
    # change the running best, so extending the line past ★ just draws a
    # flat ceiling that adds no information.
    best_idx = next(
        (i for i, n in enumerate(exps) if n["id"] == best_id), None
    )
    stair_pts: list[tuple[int, int]] = []
    running = None
    for i, n in enumerate(exps):
        if best_idx is not None and i > best_idx:
            break
        if not is_valid_result_node(graph, n):
            continue
        s = n["score"]
        if running is None or (is_max and s > running) or (not is_max and s < running):
            running = s
        stair_pts.append((col_for(i), row_for(running)))

    def _draw_stair() -> None:
        # Stair shape from app.js: M(x0,y0) L(x1,y0) L(x1,y1) L(x2,y1) ...
        for k in range(len(stair_pts) - 1):
            x0, y0 = stair_pts[k]
            x1, y1 = stair_pts[k + 1]
            for x in range(x0 + 1, x1):
                if 0 <= y0 < plot_h and 0 <= x < plot_w:
                    grid[y0][x] = ("─", "yellow", False)
            if 0 <= x1 < plot_w:
                if y0 == y1:
                    if 0 <= y0 < plot_h:
                        grid[y0][x1] = ("─", "yellow", False)
                else:
                    corner = "┘" if y1 < y0 else "┐"
                    if 0 <= y0 < plot_h:
                        grid[y0][x1] = (corner, "yellow", False)
                    lo, hi = (y1, y0) if y1 < y0 else (y0, y1)
                    for y in range(lo + 1, hi):
                        if 0 <= y < plot_h:
                            grid[y][x1] = ("│", "yellow", False)

    # Draw dots, lower-priority first so important markers sit on top.
    # The best dot (★) and record-holder spine must beat ordinary committed
    # dots — otherwise a regular committed sibling that lands in the same
    # cell as the champion will overwrite the ★ later in the loop.
    def order(n: dict[str, Any]) -> int:
        if n["id"] == best_id:
            return 100
        if n["id"] in spine:
            return 90
        s = n.get("status")
        return {"committed": 4, "active": 3, "failed": 2}.get(s, 1)

    # Spine-dot crowding: when the run has many record-holders relative to
    # the plot width (e.g. 67 records in 91 cols), drawing a bold yellow •
    # at every record-holder overwrites the stair-line cells underneath and
    # the climbing trajectory disappears into a dotted blur. Above the
    # crowding threshold, drop the per-record markers and let the stair
    # line speak — the line *is* the record-holder history.
    spine_density = len(spine) / max(1, plot_w)
    show_spine_dots = spine_density <= 0.25

    for i, n in sorted(enumerate(exps), key=lambda iN: order(iN[1])):
        status = n.get("status") or "pending"
        if status == "failed":
            continue  # failed experiments add visual noise (red rug along
                      # the baseline) without conveying useful structure;
                      # the summary line still reports the count.
        c = col_for(i)
        score = n.get("score")
        if score is None:
            # Phantom baseline so active / pending nodes are still visible.
            r = plot_h - 1
            ch = glyphs["no_score"]
        else:
            r = row_for(score)
            ch = glyphs["scored"]
        is_best = n["id"] == best_id
        is_spine = n["id"] in spine and not is_best
        if is_best:
            # Skip best for now; redrawn after the stair line so the ★ wins.
            continue
        elif is_spine:
            if show_spine_dots:
                grid[r][c] = (ch, "yellow", True)
            # else: leave the cell for the stair line to claim
        else:
            grid[r][c] = (ch, STATUS_COLOR.get(status, "grey"), False)

    # Draw the stair line ON TOP of cloud dots so the climbing-best trajectory
    # is visible even when the band's ceiling sits at the same row as the line
    # (which happens whenever the data model puts the cloud just below the
    # running best). A few green cells get hidden by yellow ─ — acceptable
    # tradeoff for a readable trajectory.
    _draw_stair()

    # Re-paint the champion on top so the ★ always wins.
    if best_id is not None:
        best_node = graph["nodes"].get(best_id, {})
        if best_node.get("score") is not None:
            best_i_repaint = next(
                (k for k, n in enumerate(exps) if n["id"] == best_id), None
            )
            if best_i_repaint is not None:
                grid[row_for(best_node["score"])][col_for(best_i_repaint)] = (
                    glyphs["best"], "white", True
                )

    # Y-axis tick rows (same 4-segment split as the web scatter).
    tick_count = 4
    tick_rows: dict[int, float] = {}
    for i in range(tick_count + 1):
        v = ymin + (i / tick_count) * yrange
        r = max(0, min(plot_h - 1, int(round((1 - i / tick_count) * (plot_h - 1)))))
        tick_rows[r] = v

    # Pick a label format that fits 5 chars regardless of score magnitude:
    # 0..10 → 2 decimals (" 0.86"), 10..100 → 1 decimal (" 42.5"), 100+ → 0 (" 100").
    max_abs = max(abs(ymin), abs(ymax))
    if max_abs >= 100:
        label_fmt = "{:>5.0f}"
        value_fmt = "{:.0f}"
    elif max_abs >= 10:
        label_fmt = "{:>5.1f}"
        value_fmt = "{:.1f}"
    else:
        label_fmt = "{:>5.2f}"
        value_fmt = "{:.3f}"

    # Annotate the champion: write the score next to ★ so the peak (or
    # trough, for min metric) is self-documenting. Place to the right of
    # the star if there's room; otherwise to the left. Skip if neither
    # side has enough plot cells.
    if best_id is not None:
        best_node = graph["nodes"].get(best_id, {})
        best_val = best_node.get("score")
        if best_val is not None:
            label_str = value_fmt.format(best_val)
            best_i = next(
                (k for k, n in enumerate(exps) if n["id"] == best_id), None
            )
            if best_i is not None:
                br = row_for(best_val)
                bc = col_for(best_i)
                width_needed = 1 + len(label_str)  # leading gap + text
                gap_col = -1
                if bc + 1 + width_needed <= plot_w:
                    start = bc + 2
                    gap_col = bc + 1
                elif bc - 1 - len(label_str) >= 0:
                    start = bc - 1 - len(label_str)
                    gap_col = bc - 1
                else:
                    start = -1
                if start >= 0:
                    if 0 <= gap_col < plot_w:
                        grid[br][gap_col] = (" ", None, False)
                    for k, ch in enumerate(label_str):
                        if 0 <= start + k < plot_w:
                            grid[br][start + k] = (ch, "yellow", True)

    lines = [_style(header, "grey", bold=True, use_color=use_color), ""]
    lines.extend(_build_stat_strip(graph, cfg, width, use_color))
    lines.append("")
    for r in range(plot_h):
        if r in tick_rows:
            label = label_fmt.format(tick_rows[r])
            label = _style(label, "grey", use_color=use_color)
        else:
            label = "     "
        row_chars = []
        for ch, col, bold in grid[r]:
            row_chars.append(_style(ch, col, bold=bold, use_color=use_color))
        axis = _style("┤", "grey", use_color=use_color)  # ┤
        lines.append(f"{label} {axis}{''.join(row_chars)}")

    # X-axis baseline + endpoint experiment ids.
    axis_line = "      " + _style("└" + "─" * plot_w, "grey", use_color=use_color)
    lines.append(axis_line)
    short = lambda nid: nid.replace("exp_", "")
    left_lbl = short(exps[0]["id"])
    right_lbl = short(exps[-1]["id"])
    gap = max(1, plot_w - len(left_lbl) - len(right_lbl))
    xline = "       " + left_lbl + " " * gap + right_lbl
    lines.append(_style(xline, "grey", use_color=use_color))

    # (The old one-line summary previously here is now redundant — the stat
    # strip above the chart already shows best/exps/frontier/active counts.)
    lines.append("")
    lines.extend(_build_top_table(graph, cfg, width, use_color))

    return "\n".join(lines) + "\n"


def _fmt_score(s: float | None) -> str:
    if s is None:
        return "--"
    a = abs(s)
    if a >= 100:
        return f"{s:.0f}"
    if a >= 10:
        return f"{s:.1f}"
    return f"{s:.2f}"


def _compute_stats(graph: dict, metric: str) -> dict:
    """Stats mirroring the dashboard's /api/stats card payload."""
    nodes = graph.get("nodes", {})
    exps = [n for nid, n in nodes.items() if nid != "root"]
    by_status: dict[str, int] = {}
    for n in exps:
        by_status[n.get("status") or "pending"] = by_status.get(
            n.get("status") or "pending", 0
        ) + 1

    best_node = best_committed_node(graph, metric)
    best_score = best_committed_score(graph, metric)
    best_id = best_node["id"] if best_node else None

    # Baseline = first committed child of root, in creation order.
    baseline = None
    for n in sorted(exps, key=lambda n: n.get("created_at") or ""):
        if n.get("parent") == "root" and n.get("score") is not None:
            baseline = n["score"]
            break

    frontier = len(frontier_nodes(graph))

    return {
        "total": len(exps),
        "by_status": by_status,
        "best_score": best_score,
        "best_id": best_id,
        "baseline": baseline,
        "frontier": frontier,
    }


def _build_stat_strip(graph: dict, cfg: dict, width: int, use_color: bool) -> list[str]:
    """4-card stat strip rendered as 3 stacked lines: label / value / sub."""
    metric = cfg.get("metric", "max")
    is_max = metric == "max"
    s = _compute_stats(graph, metric)

    # BEST SCORE — value + delta from baseline
    best_str = _fmt_score(s["best_score"])
    if s["best_score"] is None:
        delta_str = ""
        sub_best = "no scored runs yet"
    elif s["baseline"] is None:
        delta_str = ""
        sub_best = "no baseline yet"
    else:
        diff = s["best_score"] - s["baseline"]
        if abs(diff) < 1e-9:
            delta_str = "0%"
        elif abs(s["baseline"]) > 1e-9:
            pct = round((diff / abs(s["baseline"])) * 100)
            delta_str = f"{pct:+d}%"
        else:
            delta_str = f"{diff:+.2f}"
        sub_best = f"from {_fmt_score(s['baseline'])} baseline"

    bs = s["by_status"]
    exp_parts = []
    if bs.get("committed"):
        exp_parts.append(f"{bs['committed']} kept")
    if bs.get("failed"):
        exp_parts.append(f"{bs['failed']} err")
    if bs.get("discarded"):
        exp_parts.append(f"{bs['discarded']} skip")
    exp_sub = " · ".join(exp_parts) or "no experiments yet"

    cards = [
        ("BEST SCORE",  f"{best_str} {delta_str}".strip(), sub_best),
        ("EXPERIMENTS", str(s["total"]),                    exp_sub),
        ("FRONTIER",    str(s["frontier"]),                 "open branches"),
        ("ACTIVE",      str(bs.get("active", 0)),           "running now"),
    ]

    # Card width: divide available width across 4 cards + 3 dividers + 2-col indent.
    card_w = max(14, (width - 2 - 3 * 3) // 4)

    def _row(idx: int, color: str, bold: bool) -> str:
        cells = []
        for label, value, sub in cards:
            txt = (label, value, sub)[idx]
            if len(txt) > card_w:
                txt = txt[:card_w - 1] + "…"
            cells.append(_style(txt.ljust(card_w), color, bold=bold, use_color=use_color))
        divider = _style(" │ ", "grey", use_color=use_color)
        return "  " + divider.join(cells)

    return [
        _row(0, "grey",  False),  # labels (small caps)
        _row(1, "white", True),   # values (big number)
        _row(2, "grey",  False),  # sub
    ]


def _build_top_table(graph: dict, cfg: dict, width: int, use_color: bool,
                     top_n: int = 5) -> list[str]:
    """Top-N committed experiments by score, with id / score / parent / hypothesis."""
    nodes = graph.get("nodes", {})
    metric = cfg.get("metric", "max")
    is_max = metric == "max"
    committed = [
        n for nid, n in nodes.items()
        if nid != "root" and is_valid_result_node(graph, n)
    ]
    if not committed:
        return [
            _style("  TOP EXPERIMENTS", "grey", bold=True, use_color=use_color),
            _style("  (no committed experiments yet)", "grey", use_color=use_color),
        ]
    committed.sort(key=lambda n: n["score"], reverse=is_max)
    top = committed[:top_n]

    # Column widths: id 10, score 7, parent 10, hypothesis takes the rest.
    ID_W, SCORE_W, PARENT_W = 10, 7, 10
    indent = "  "
    used = len(indent) + ID_W + 1 + SCORE_W + 2 + PARENT_W + 2
    hyp_w = max(20, width - used)

    header = (
        f"{indent}{'id':<{ID_W}} {'score':>{SCORE_W}}  "
        f"{'parent':<{PARENT_W}}  hypothesis"
    )
    sep = (
        f"{indent}{'─'*ID_W} {'─'*SCORE_W}  "
        f"{'─'*PARENT_W}  {'─'*min(hyp_w, 40)}"
    )
    rows = [
        _style("  TOP EXPERIMENTS", "grey", bold=True, use_color=use_color),
        _style(header, "grey", use_color=use_color),
        _style(sep,    "grey", use_color=use_color),
    ]
    for n in top:
        parent = n.get("parent") or "—"
        hyp = (n.get("hypothesis") or "").strip() or "—"
        if len(hyp) > hyp_w:
            hyp = hyp[:hyp_w - 1] + "…"
        line = (
            f"{indent}"
            f"{_style(n['id'].ljust(ID_W), 'white', use_color=use_color)} "
            f"{_style(_fmt_score(n['score']).rjust(SCORE_W), 'green', bold=True, use_color=use_color)}  "
            f"{_style(parent.ljust(PARENT_W), 'grey', use_color=use_color)}  "
            f"{hyp}"
        )
        rows.append(line)
    return rows


def _load_run_payload(root: Path, run_id: str) -> tuple[dict, dict] | None:
    gp = evo_dir(root) / run_id / GRAPH_FILE
    if not gp.exists():
        return None
    try:
        graph = json.loads(gp.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    cp = evo_dir(root) / run_id / CONFIG_FILE
    cfg: dict = {}
    if cp.exists():
        try:
            cfg = json.loads(cp.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            cfg = {}
    return graph, cfg


def build_report(root: Path, *, use_color: bool | None = None,
                 size: tuple[int, int] | None = None,
                 margin: int | None = None,
                 dots: str | None = None) -> str:
    """Render the dashboard scatter as a colored terminal block.

    Spans every run in the workspace (one chart per run, stacked).
    Falls back to a legacy `.evo/graph.json` workspace if `list_runs`
    returns nothing.

    `margin` is an additional right-side margin to reserve in columns.
    When None, auto-detects from the host env (e.g. Claude Code's CLI
    frames tool output in a bordered block that eats ~6 columns).
    """
    if use_color is None:
        use_color = sys.stdout.isatty()
    cols, rows = size if size else _detect_terminal_size()
    extra_margin = margin if margin is not None else _host_right_margin()

    runs = list_runs(root)
    blocks: list[str] = []

    if runs:
        # For 1 run: use most of the terminal. For N>1: a fixed-ish per-run
        # height so the chart stays readable; let the terminal scroll.
        if len(runs) == 1:
            plot_h = max(8, rows - 6)
        else:
            plot_h = 10
        for run in runs:
            payload = _load_run_payload(root, run["id"])
            if payload is None:
                continue
            graph, cfg = payload
            blocks.append(_render_run(
                run, graph, cfg, cols, plot_h, use_color,
                extra_margin=extra_margin, dot_tier=dots,
            ))
    else:
        legacy_graph = evo_dir(root) / GRAPH_FILE
        if not legacy_graph.exists():
            return "no evo workspace found (run `evo init` first)\n"
        try:
            graph = json.loads(legacy_graph.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return "could not read .evo/graph.json\n"
        legacy_cfg = evo_dir(root) / CONFIG_FILE
        cfg: dict = {}
        if legacy_cfg.exists():
            try:
                cfg = json.loads(legacy_cfg.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                cfg = {}
        blocks.append(_render_run(
            {"id": "(workspace)", "active": True, "target": cfg.get("target", "")},
            graph, cfg, cols, max(8, rows - 6), use_color,
            extra_margin=extra_margin, dot_tier=dots,
        ))

    if not blocks:
        return "no runs with a graph.json yet\n"
    return "\n".join(blocks)

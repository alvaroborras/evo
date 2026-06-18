"""Unit tests for pure functions in evo.core.

Fast (millisecond) tests for logic that does not touch git, subprocess, or
the filesystem. Complements the slower tests/e2e.py flow tests.

Run: `python3 tests/unit/test_core.py`
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "plugins" / "evo" / "src"))

from evo.core import (  # noqa: E402
    PRUNE_KIND_EXHAUSTED,
    PRUNE_KIND_INVALID,
    best_committed_node,
    best_committed_score,
    collect_gates_from_path,
    effective_status,
    frontier_nodes,
    is_valid_result_node,
    lineage_invalidated_by,
    path_to_node,
)


def _graph(*nodes: dict) -> dict:
    return {"nodes": {n["id"]: n for n in nodes}}


def _node(id_: str, parent: str | None, gates: list[dict] | None = None, **extra) -> dict:
    return {
        "id": id_,
        "parent": parent,
        "children": [],
        "status": "committed" if id_ != "root" else "root",
        "score": None,
        "commit": None,
        "pruned_reason": None,
        "prune_kind": None,
        "gates": gates or [],
        **extra,
    }


def _linked_graph(*nodes: dict) -> dict:
    graph = _graph(*nodes)
    for node in graph["nodes"].values():
        parent = node.get("parent")
        if parent and parent in graph["nodes"]:
            graph["nodes"][parent].setdefault("children", []).append(node["id"])
    return graph


def _gate(name: str, command: str = "cmd") -> dict:
    return {"name": name, "command": command, "added_at": "2026-04-15T00:00:00Z"}


def test_path_to_node_returns_root_to_leaf_chain() -> None:
    graph = _graph(
        _node("root", None),
        _node("exp_0000", "root"),
        _node("exp_0001", "exp_0000"),
    )
    chain = [n["id"] for n in path_to_node(graph, "exp_0001")]
    assert chain == ["root", "exp_0000", "exp_0001"], chain


def test_collect_gates_empty_when_no_gates_anywhere() -> None:
    graph = _graph(_node("root", None), _node("exp_0000", "root"))
    assert collect_gates_from_path(graph, "exp_0000") == []


def test_collect_gates_inherits_root_gate() -> None:
    graph = _graph(
        _node("root", None, gates=[_gate("core_tests", "pytest -x")]),
        _node("exp_0000", "root"),
    )
    gates = collect_gates_from_path(graph, "exp_0000")
    assert [g["name"] for g in gates] == ["core_tests"]
    assert gates[0]["command"] == "pytest -x"


def test_collect_gates_unions_root_and_own_gates_in_root_to_leaf_order() -> None:
    graph = _graph(
        _node("root", None, gates=[_gate("root_gate")]),
        _node("exp_0000", "root", gates=[_gate("own_gate")]),
    )
    gates = collect_gates_from_path(graph, "exp_0000")
    assert [g["name"] for g in gates] == ["root_gate", "own_gate"]


def test_collect_gates_dedupes_by_name_keeping_ancestor_wins() -> None:
    # Same gate name declared on an ancestor and a descendant: the ancestor
    # one is kept (ancestors are walked first), the descendant redeclaration
    # is ignored. Verifies we do not surface the gate twice.
    graph = _graph(
        _node("root", None, gates=[_gate("flaky", "pytest ancestor")]),
        _node("exp_0000", "root", gates=[_gate("flaky", "pytest descendant")]),
    )
    gates = collect_gates_from_path(graph, "exp_0000")
    assert len(gates) == 1
    assert gates[0]["command"] == "pytest ancestor"


def test_collect_gates_scoped_to_ancestry_not_siblings() -> None:
    graph = _graph(
        _node("root", None, gates=[_gate("root_gate")]),
        _node("exp_0000", "root", gates=[_gate("sibling_gate")]),
        _node("exp_0001", "root"),
    )
    gates = collect_gates_from_path(graph, "exp_0001")
    assert [g["name"] for g in gates] == ["root_gate"]


def test_collect_gates_on_root_returns_own_only() -> None:
    graph = _graph(_node("root", None, gates=[_gate("core_tests")]))
    gates = collect_gates_from_path(graph, "root")
    assert [g["name"] for g in gates] == ["core_tests"]


def test_exhausted_prune_keeps_result_eligible_but_not_frontier() -> None:
    graph = _linked_graph(
        _node("root", None),
        _node(
            "exp_0000",
            "root",
            status="pruned",
            score=0.9,
            commit="abc",
            pruned_reason="no more useful children",
            prune_kind=PRUNE_KIND_EXHAUSTED,
        ),
        _node("exp_0001", "root", status="committed", score=0.7, commit="def"),
    )

    assert is_valid_result_node(graph, graph["nodes"]["exp_0000"])
    assert effective_status(graph, graph["nodes"]["exp_0000"]) == "committed"
    assert best_committed_score(graph, "max") == 0.9
    assert best_committed_node(graph, "max")["id"] == "exp_0000"
    assert [n["id"] for n in frontier_nodes(graph)] == ["exp_0001"]


def test_gate_failed_committed_node_is_excluded_from_best_result() -> None:
    graph = _linked_graph(
        _node("root", None),
        _node(
            "exp_0000",
            "root",
            status="committed",
            score=2.0,
            commit="abc",
            gate_result=False,
        ),
        _node(
            "exp_0001",
            "root",
            status="committed",
            score=1.0,
            commit="def",
            gate_result=True,
        ),
    )

    assert not is_valid_result_node(graph, graph["nodes"]["exp_0000"])
    assert is_valid_result_node(graph, graph["nodes"]["exp_0001"])
    assert best_committed_score(graph, "max") == 1.0
    assert best_committed_node(graph, "max")["id"] == "exp_0001"


def test_gate_failed_exhausted_prune_is_excluded_from_best_result() -> None:
    graph = _linked_graph(
        _node("root", None),
        _node(
            "exp_0000",
            "root",
            status="pruned",
            score=2.0,
            commit="abc",
            gate_result=False,
            pruned_reason="closed branch",
            prune_kind=PRUNE_KIND_EXHAUSTED,
        ),
        _node(
            "exp_0001",
            "root",
            status="committed",
            score=1.0,
            commit="def",
            gate_result=True,
        ),
    )

    assert not is_valid_result_node(graph, graph["nodes"]["exp_0000"])
    assert effective_status(graph, graph["nodes"]["exp_0000"]) == "committed"
    assert best_committed_score(graph, "max") == 1.0
    assert best_committed_node(graph, "max")["id"] == "exp_0001"


def test_invalid_prune_blocks_node_and_descendants_from_best_and_frontier() -> None:
    graph = _linked_graph(
        _node("root", None),
        _node(
            "exp_0000",
            "root",
            status="pruned",
            score=0.9,
            commit="abc",
            pruned_reason="score computed against wrong benchmark",
            prune_kind=PRUNE_KIND_INVALID,
        ),
        _node("exp_0001", "exp_0000", status="committed", score=1.2, commit="def"),
        _node("exp_0002", "root", status="committed", score=0.8, commit="ghi"),
    )

    assert lineage_invalidated_by(graph, "exp_0001")["id"] == "exp_0000"
    assert not is_valid_result_node(graph, graph["nodes"]["exp_0000"])
    assert not is_valid_result_node(graph, graph["nodes"]["exp_0001"])
    assert effective_status(graph, graph["nodes"]["exp_0000"]) == "invalidated"
    assert effective_status(graph, graph["nodes"]["exp_0001"]) == "lineage_blocked"
    assert best_committed_node(graph, "max")["id"] == "exp_0002"
    assert [n["id"] for n in frontier_nodes(graph)] == ["exp_0002"]


def test_legacy_pruned_node_remains_excluded_for_backwards_compatibility() -> None:
    graph = _linked_graph(
        _node("root", None),
        _node(
            "exp_0000",
            "root",
            status="pruned",
            score=1.0,
            commit="abc",
            pruned_reason="legacy graph before prune_kind",
        ),
        _node("exp_0001", "root", status="committed", score=0.5, commit="def"),
    )

    assert not is_valid_result_node(graph, graph["nodes"]["exp_0000"])
    assert effective_status(graph, graph["nodes"]["exp_0000"]) == "pruned"
    assert best_committed_node(graph, "max")["id"] == "exp_0001"


TESTS = [fn for name, fn in globals().items() if name.startswith("test_") and callable(fn)]


def main() -> int:
    failed = 0
    for fn in TESTS:
        try:
            fn()
            print(f"PASS  {fn.__name__}")
        except AssertionError as e:
            failed += 1
            print(f"FAIL  {fn.__name__}: {e}")
        except Exception as e:
            failed += 1
            print(f"ERROR {fn.__name__}: {type(e).__name__}: {e}")
    print(f"\n{len(TESTS) - failed}/{len(TESTS)} passed")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())

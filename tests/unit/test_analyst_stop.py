"""Guards the analyst STOP-signal contract in the optimize workflow.

A STOP is a diagnosed, recoverable stop — never a silent kill. Every stop the
analyst emits must carry exp + failure class + reason + fix, and the gated
enforcer (not the analyst) must abort + annotate + classify/preserve.
"""
from pathlib import Path

WORKFLOW = (
    Path(__file__).resolve().parents[2]
    / "plugins" / "evo" / "skills" / "optimize" / "workflows" / "evo-optimize.js"
)
JS = WORKFLOW.read_text()


def test_stop_signal_schema_carries_diagnosis():
    # ANALYST_FINDINGS.stops items require the full diagnosis, not just an id.
    for field in ("expId", "failureClass", "reason", "fixHint"):
        assert field in JS, f"stop signal missing {field}"
    assert "'stops'" in JS or '"stops"' in JS
    # failure class is the build/eval/hypothesis taxonomy
    for cls in ("build", "eval", "hypothesis"):
        assert cls in JS


def test_analyst_recommends_but_does_not_abort():
    # The analyst must stay read-only: it recommends stops, it does not run abort/discard itself.
    assert "do NOT run `evo abort`" in JS or "do NOT run `evo abort` / `evo discard`" in JS


def test_enforcer_aborts_annotates_and_classifies():
    # The gated enforcer is the actor: verify-active, abort the tree, annotate the diagnosis,
    # discard with the failure class (preserving the partial artifact).
    assert "function enforceStopPrompt" in JS
    assert "evo abort" in JS
    assert "evo annotate" in JS
    assert "--failure-class" in JS
    assert "evo show" in JS  # the verify-still-active guard


def test_stop_is_dispatched_and_fed_forward():
    # analystLoop consumes tick.stops -> spawns the enforcer, and the fix feeds the next brief.
    assert "tick.stops" in JS
    assert "enforce-stop" in JS
    assert "analystSignals.push" in JS

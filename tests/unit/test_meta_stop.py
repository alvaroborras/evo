"""Guards the meta agent's contract in the optimize workflow.

The meta (formerly "analyst") is the concurrent controller. It (1) STOPs doomed
experiments — a diagnosed, recoverable stop carrying exp + failure class + reason
+ fix, acted on by a gated enforcer (not the meta itself); (2) suggests next
directions (briefHints); and (3) restructures the workflow itself live via
harnessEdits (logic flow + prompts), applied free-will and audited.

These are string-contract checks over the workflow source — they pin the
behaviour without booting the full runtime.
"""
from pathlib import Path

WORKFLOW = (
    Path(__file__).resolve().parents[2]
    / "plugins" / "evo" / "skills" / "optimize" / "workflows" / "evo-optimize.js"
)
JS = WORKFLOW.read_text()
SKILL_TEXT = (WORKFLOW.parent.parent / "SKILL.md").read_text()  # skills/optimize/SKILL.md


# --------------------------------------------------------------------------- #
# Driver policy: the prose loop is the DEFAULT everywhere; the workflow is opt-in
# --------------------------------------------------------------------------- #
def test_prose_is_default_driver():
    # Policy: prose drives the loop on every host by default; the workflow is an
    # explicit opt-in on claude-code (default-orchestrator=workflow + Workflow tool
    # present). The resolved choice is persisted to config so the stop-nudge
    # suppression and `evo config get` agree.
    assert "prose loop is the default" in SKILL_TEXT
    # the workflow must remain reachable as an explicit opt-in
    assert "evo config set default-orchestrator workflow" in SKILL_TEXT
    assert "opt-in" in SKILL_TEXT and "prose" in SKILL_TEXT


# --------------------------------------------------------------------------- #
# STOP contract (unchanged behaviour, renamed analyst -> meta)
# --------------------------------------------------------------------------- #
def test_stop_signal_schema_carries_diagnosis():
    # META_FINDINGS.stops items require the full diagnosis, not just an id.
    for field in ("expId", "failureClass", "reason", "fixHint"):
        assert field in JS, f"stop signal missing {field}"
    assert "'stops'" in JS or '"stops"' in JS
    for cls in ("build", "eval", "hypothesis"):
        assert cls in JS


def test_meta_recommends_but_does_not_abort():
    # The meta stays read-only for STOPs: it recommends, it does not run abort/discard itself.
    assert "do NOT run `evo abort`" in JS


def test_enforcer_aborts_annotates_and_classifies():
    # The gated enforcer is the actor: verify-active, abort the tree, annotate, discard w/ class.
    assert "function enforceStopPrompt" in JS
    assert "evo abort" in JS
    assert "evo annotate" in JS
    assert "--failure-class" in JS
    assert "evo show" in JS  # the verify-still-active guard


def test_stop_is_dispatched_and_fed_forward():
    # metaLoop consumes tick.stops -> spawns the enforcer, and the fix feeds the next brief.
    assert "tick.stops" in JS
    assert "enforce-stop" in JS
    assert "metaSignals.push" in JS


def test_scan_clusters_on_failure_class_for_divergence():
    assert "failure_class" in JS
    assert "axis-warning" in JS
    assert "fixable plumbing" in JS


def test_run_lane_handles_external_abort_without_retry():
    assert "terminated externally" in JS
    assert "do NOT retry" in JS


# --------------------------------------------------------------------------- #
# Harness-control contract (new ability: meta restructures the workflow live)
# --------------------------------------------------------------------------- #
def test_harness_edits_in_findings_schema_with_all_ops():
    # META_FINDINGS gains harnessEdits, covering the four structural ops.
    assert "harnessEdits" in JS
    for op in ("set-knob", "toggle-phase", "set-prompt", "inject-step"):
        assert op in JS, f"harness op {op} missing from schema"
    for knob in ("width", "budget", "stall", "ideateEvery", "ideateStall"):
        assert knob in JS
    for seam in ("before-scan", "after-scan", "before-brief", "after-collect"):
        assert seam in JS


def test_meta_loop_applies_harness_edits_free_will():
    # metaLoop consumes tick.harnessEdits and applies each directly (no gate).
    assert "tick.harnessEdits" in JS
    assert "applyHarnessEdit" in JS
    assert "function applyHarnessEdit" in JS


def test_workflow_reads_harness_live():
    # The optimize loop must read the mutable harness (so meta edits take effect), not frozen consts.
    assert "const harness = {" in JS
    assert "stall < harness.stall" in JS
    assert "slice(0, harness.width)" in JS
    assert "depth < harness.budget" in JS
    assert "withHarnessPrompt(" in JS
    assert "runInjected(" in JS


def test_harness_edits_are_audited():
    # Free will still leaves a trail: editLog + the return value carries the harness audit.
    assert "harness.editLog" in JS
    assert "harnessSummary" in JS
    assert "harnessEditLog" in JS


def test_meta_must_not_edit_grader_or_verifier():
    # Scope boundary: the meta edits the SEARCH harness only; the grader/verifier stay fixed so the
    # score stays meaningful. The prompt must forbid touching them.
    assert "NEVER" in JS
    assert "grader" in JS
    for forbidden in ("benchmark", "held-out", "gate"):
        assert forbidden in JS

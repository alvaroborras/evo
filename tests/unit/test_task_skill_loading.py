"""Parity + behavior test for task-skill loading.

Guards the design invariant that BOTH execution planes load the task's category
skill(s) resolved into `task-skills`:

- prose path: the subagent skill instructs `evo config get task-skills` + load
- workflow path: the capsule (capsuleLines) injects a load instruction into
  every builder/runner lane, and orient resolves task-skills into STATE

Without this, a workflow lane (or prose subagent) reverts to base-model defaults
and reintroduces the device_map / stale-trainer-API / eval-before-build class of
mistakes. The node section actually RUNS capsuleLines from the real workflow
source to prove it emits the load instruction when instructed (state.taskSkills
set) and a category-inference fallback when not.
"""
import json
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
SKILLS = REPO / "plugins" / "evo" / "skills"
SUBAGENT = SKILLS / "subagent" / "SKILL.md"
DISCOVER = SKILLS / "discover" / "SKILL.md"
WORKFLOW = SKILLS / "optimize" / "workflows" / "evo-optimize.js"


def test_cli_has_task_skills_field():
    from evo.cli import _CONFIG_FIELD_TO_KEY
    assert _CONFIG_FIELD_TO_KEY.get("task-skills") == "task_skills"


def test_discover_writes_task_skills():
    text = DISCOVER.read_text()
    assert "evo config set task-skills" in text, "discover must record the category skill(s)"


def test_prose_subagent_loads_task_skills():
    text = SUBAGENT.read_text()
    assert "evo config get task-skills" in text, "prose subagent must look up task-skills"
    # ...and be told to load them in full
    assert "IN FULL" in text


def test_workflow_wires_capsule_into_both_lanes():
    js = WORKFLOW.read_text()
    # orient resolves task-skills into STATE
    assert "evo config get task-skills" in js
    assert "taskSkills" in js
    # both the implement and run lanes inject the capsule
    assert js.count("...capsuleLines(state)") >= 2, "capsule must be on both implement and run lanes"


@pytest.mark.skipif(shutil.which("node") is None, reason="node not available")
def test_workflow_capsule_emits_load_instruction_when_instructed():
    """Run the real capsuleLines() from evo-optimize.js and assert its output."""
    snippet = r"""
const fs = require('fs');
const src = fs.readFileSync(process.argv[1], 'utf8');
const m = src.match(/function capsuleLines\(state\) \{[\s\S]*?\n\}/);
if (!m) { console.error('capsuleLines source not found'); process.exit(2); }
const fn = new Function('return (' + m[0] + ')')();
const instructed = fn({taskSkills: ['finetuning'], knownLearnings: ['TRL renamed max_seq_length']});
const empty = fn({});
console.log(JSON.stringify({instructed, empty}));
"""
    out = subprocess.run(
        ["node", "-e", snippet, str(WORKFLOW)],
        capture_output=True, text=True, check=True,
    ).stdout
    res = json.loads(out)
    instructed = "\n".join(res["instructed"]).lower()
    empty = "\n".join(res["empty"]).lower()

    # Instructed (state.taskSkills set) -> tells the lane to load the named skill in full
    assert "finetuning" in instructed
    assert "load" in instructed and "full" in instructed
    # known learnings are carried so the lane doesn't rediscover them
    assert "trl renamed max_seq_length".lower() in instructed
    # Not instructed -> a category-inference fallback referencing project.md
    assert "project.md" in empty

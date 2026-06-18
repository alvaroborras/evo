"""Live Codex + fake-Slurm behavior harness.

Skipped unless:
  - EVO_LIVE_TEST_CODEX_SLURM=1
  - codex CLI installed

This runs real Codex agent sessions against ``scripts/codex_slurm_harness.py``.
It is intentionally not part of normal CI: the tests are slow, stochastic, and
spend live model tokens.

Run all scenarios:
    EVO_LIVE_TEST_CODEX_SLURM=1 pytest tests/live/test_codex_slurm_harness.py -v -s

Run a subset:
    EVO_LIVE_TEST_CODEX_SLURM=1 \\
    EVO_CODEX_SLURM_SCENARIOS=implicit_improve,implicit_report \\
    pytest tests/live/test_codex_slurm_harness.py -v -s
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
HARNESS = REPO_ROOT / "scripts" / "codex_slurm_harness.py"

ALL_SCENARIOS = (
    "natural",
    "strict",
    "report",
    "implicit_improve",
    "implicit_continue",
    "implicit_report",
    "implicit_resource_cap",
    "loose_ideas",
    "overload_guard",
    "directive_midrun",
    "noisy_replicates",
)


def _gate() -> None:
    if os.environ.get("EVO_LIVE_TEST_CODEX_SLURM") != "1":
        pytest.skip("set EVO_LIVE_TEST_CODEX_SLURM=1 to enable")
    if shutil.which("codex") is None:
        pytest.skip("codex CLI not installed")


def _selected_scenarios() -> set[str]:
    raw = os.environ.get("EVO_CODEX_SLURM_SCENARIOS", "").strip()
    if not raw:
        return set(ALL_SCENARIOS)
    selected = {part.strip() for part in raw.split(",") if part.strip()}
    unknown = selected.difference(ALL_SCENARIOS)
    if unknown:
        pytest.fail(
            "unknown EVO_CODEX_SLURM_SCENARIOS value(s): "
            + ", ".join(sorted(unknown))
        )
    return selected


@pytest.mark.parametrize("scenario", ALL_SCENARIOS)
def test_codex_slurm_behavior_scenario(scenario: str) -> None:
    _gate()
    if scenario not in _selected_scenarios():
        pytest.skip(f"{scenario} not selected by EVO_CODEX_SLURM_SCENARIOS")

    timeout = int(os.environ.get("EVO_CODEX_SLURM_TIMEOUT", "700"))
    if scenario == "directive_midrun":
        cmd = [
            sys.executable,
            str(HARNESS),
            "directive-midrun",
            "--timeout",
            str(timeout),
        ]
    else:
        cmd = [
            sys.executable,
            str(HARNESS),
            "run",
            "--scenario",
            scenario,
            "--timeout",
            str(timeout),
        ]

    result = subprocess.run(
        cmd,
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=timeout + 180,
    )
    print(result.stdout)
    if result.stderr:
        print(result.stderr, file=sys.stderr)
    assert result.returncode == 0

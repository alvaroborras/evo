"""Schema parity tests: Python ``evo.inject.drain`` and TS
``opencode_plugin/drain.ts`` must produce the same text + on-disk state
when fed the same fixtures.

Drift between these two implementations would silently break inject for
hosts that use one path but not the other (Hermes/CC/Codex use the
Python drain; Opencode/Openclaw use the TS drain).

Each fixture in ``tests/fixtures/inject_drain/*.json`` is run through
both implementations on freshly-materialized run dirs. The test asserts
both produce identical:

    - DrainResult fields: text, newWorkspaceOffset, newExpOffset
    - Final ``offsets/<sid>.json`` content (minus updated_at, which is
      a wall-clock timestamp)
    - Marker file existence
    - The fixture's ``expected`` block (catches the case where both
      impls have the same bug)

Run: pytest tests/unit/test_inject_parity.py -v

Requires ``bun`` on PATH for the TS path.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "plugins" / "evo" / "src"))

from evo.inject import marker, queue
from evo.inject.drain import format_directive_text
from evo.inject.paths import (
    exp_events_path,
    inject_root,
    workspace_events_path,
)
from evo.inject.registry import get_session


FIXTURES_DIR = REPO_ROOT / "tests" / "fixtures" / "inject_drain"
TS_RUNNER = FIXTURES_DIR / "_runner.ts"


def _materialize_fixture(fixture: dict, root: Path) -> Path:
    """Build a run dir at ``<root>/.evo/run_0000/`` from a fixture spec.

    Returns the workspace root (the dir CONTAINING ``.evo/``), which is
    what both drain implementations expect.
    """
    evo_dir = root / ".evo"
    run_dir = evo_dir / "run_0000"
    inject = run_dir / "inject"
    (inject / "events").mkdir(parents=True)
    (inject / "sessions").mkdir()
    (inject / "offsets").mkdir()
    (inject / "markers").mkdir()

    # Python `workspace_path(root)` reads `.evo/meta.json` to find the
    # active run. Without it, the path resolver falls back to `.evo/`
    # itself and never finds our fixture files. TS reads runDir directly
    # so this only matters for the Python pass.
    (evo_dir / "meta.json").write_text(json.dumps({"active": "run_0000"}))

    # workspace events
    if "raw_workspace_jsonl" in fixture:
        (inject / "events" / "workspace.jsonl").write_text(fixture["raw_workspace_jsonl"])
    elif fixture.get("workspace_events"):
        path = inject / "events" / "workspace.jsonl"
        with path.open("w") as f:
            for ev in fixture["workspace_events"]:
                f.write(json.dumps(ev) + "\n")

    for exp_id, events in fixture.get("exp_events", {}).items():
        path = inject / "events" / f"{exp_id}.jsonl"
        with path.open("w") as f:
            for ev in events:
                f.write(json.dumps(ev) + "\n")

    for sid, rec in fixture.get("sessions", {}).items():
        (inject / "sessions" / f"{sid}.json").write_text(json.dumps(rec))

    for sid, off in fixture.get("offsets", {}).items():
        (inject / "offsets" / f"{sid}.json").write_text(json.dumps(off))

    for sid in fixture.get("markers", []):
        (inject / "markers" / f"{sid}.flag").touch()

    return root


def _drain_python(root: Path, sid: str) -> dict:
    """Mirror of ``drainSession`` from drain.ts — returns the same
    DrainResult shape and applies the same on-disk side effects (offset
    write, marker unlink)."""
    sess = get_session(root, sid)
    if sess is None:
        marker.unlink(root, sid)
        return {"text": None, "newWorkspaceOffset": None, "newExpOffset": None}

    exp_id = sess.get("exp_id")
    new_workspace = None
    new_exp = None

    if exp_id:
        last = queue.read_offset(root, sid, "exp")
        events = queue.read_events_after(exp_events_path(root, exp_id), last)
        if events:
            new_exp = events[-1]["id"]
    else:
        last = queue.read_offset(root, sid, "workspace")
        events = queue.read_events_after(workspace_events_path(root), last)
        if events:
            new_workspace = events[-1]["id"]

    text = format_directive_text(events) if events else None
    if new_workspace or new_exp:
        queue.write_offset(
            root,
            sid,
            workspace_id=new_workspace,
            exp_id=new_exp,
        )
    marker.unlink(root, sid)
    return {
        "text": text,
        "newWorkspaceOffset": new_workspace,
        "newExpOffset": new_exp,
    }


def _drain_ts(root: Path, sid: str) -> dict:
    """Spawn ``bun run _runner.ts <runDir> <sid>`` and parse its JSON
    DrainResult output."""
    run_dir = root / ".evo" / "run_0000"
    proc = subprocess.run(
        ["bun", "run", str(TS_RUNNER), str(run_dir), sid],
        capture_output=True,
        text=True,
        check=True,
        timeout=30,
    )
    return json.loads(proc.stdout)


def _capture_disk_state(root: Path, sid: str) -> dict:
    """Capture post-drain disk state for parity comparison.

    Drops ``updated_at`` from offset files since it's a wall-clock time
    and will differ between implementations even when correct."""
    inject = inject_root(root)
    offset_path = inject / "offsets" / f"{sid}.json"
    marker_path = inject / "markers" / f"{sid}.flag"

    state = {"marker_exists": marker_path.exists(), "offset": None}
    if offset_path.exists():
        data = json.loads(offset_path.read_text())
        data.pop("updated_at", None)
        state["offset"] = data
    return state


def _check_bun_available() -> bool:
    return shutil.which("bun") is not None


class InjectDrainParityTests(unittest.TestCase):
    """Each fixture is run through both implementations on a fresh run
    dir; the test asserts they match each other AND the fixture's
    ``expected`` block."""

    @classmethod
    def setUpClass(cls):
        if not _check_bun_available():
            raise unittest.SkipTest("bun not on PATH; skipping TS parity tests")
        if not FIXTURES_DIR.exists():
            raise unittest.SkipTest(f"fixtures dir missing: {FIXTURES_DIR}")
        cls.fixtures = sorted(FIXTURES_DIR.glob("*.json"))
        if not cls.fixtures:
            raise unittest.SkipTest("no fixtures found")

    def _run_one(self, fixture_path: Path):
        fixture = json.loads(fixture_path.read_text())
        sid = fixture["session_id"]
        expected = fixture.get("expected")

        # Python pass — fresh dir
        with tempfile.TemporaryDirectory(prefix="parity_py_") as tmp:
            root_py = _materialize_fixture(fixture, Path(tmp))
            result_py = _drain_python(root_py, sid)
            disk_py = _capture_disk_state(root_py, sid)

        # TS pass — fresh dir
        with tempfile.TemporaryDirectory(prefix="parity_ts_") as tmp:
            root_ts = _materialize_fixture(fixture, Path(tmp))
            result_ts = _drain_ts(root_ts, sid)
            disk_ts = _capture_disk_state(root_ts, sid)

        # Parity: result fields
        self.assertEqual(
            result_py,
            result_ts,
            f"[{fixture_path.name}] DrainResult mismatch:\n  py={result_py}\n  ts={result_ts}",
        )

        # Parity: disk state
        self.assertEqual(
            disk_py,
            disk_ts,
            f"[{fixture_path.name}] disk state mismatch:\n  py={disk_py}\n  ts={disk_ts}",
        )

        # Sanity vs expected (catches both impls being wrong the same way)
        if expected is not None:
            self.assertEqual(result_py["text"], expected["text"], f"[{fixture_path.name}] text vs expected")
            self.assertEqual(
                result_py["newWorkspaceOffset"],
                expected["newWorkspaceOffset"],
                f"[{fixture_path.name}] newWorkspaceOffset vs expected",
            )
            self.assertEqual(
                result_py["newExpOffset"],
                expected["newExpOffset"],
                f"[{fixture_path.name}] newExpOffset vs expected",
            )
            self.assertEqual(
                disk_py["marker_exists"],
                expected["marker_after"],
                f"[{fixture_path.name}] marker_after vs expected",
            )
            self.assertEqual(
                disk_py["offset"],
                expected["offset_after"],
                f"[{fixture_path.name}] offset_after vs expected",
            )


def _make_test_method(fixture_path):
    def method(self):
        self._run_one(fixture_path)
    method.__name__ = f"test_parity_{fixture_path.stem}"
    return method


if FIXTURES_DIR.exists():
    for _fp in sorted(FIXTURES_DIR.glob("*.json")):
        setattr(
            InjectDrainParityTests,
            f"test_parity_{_fp.stem}",
            _make_test_method(_fp),
        )


if __name__ == "__main__":
    unittest.main()

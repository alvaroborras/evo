"""Tests for the opencode plugin steering port (drain.ts deny + policy).

Drives the TypeScript implementation directly via `bun run` over a small
test harness, then asserts on the JSON results. Verifies parity with the
Python deny-list logic in `evo.inject.drain`.

Run: pytest tests/unit/test_opencode_steering.py -v
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
DRAIN_TS = REPO_ROOT / "plugins" / "evo" / "src" / "evo" / "opencode_plugin" / "drain.ts"

BUN = shutil.which("bun")


@unittest.skipIf(BUN is None, "bun runtime not available")
class TestOpencodeDenyListParity(unittest.TestCase):
    """Run a curated set of (cmd, expected) cases through the TS
    `isDeniedInOptimizeMode` and assert behavior matches the Python
    equivalent. The cases are picked from the Python policy tests."""

    def _run_cases(self, cases: list[tuple[str, str, dict, bool]]) -> None:
        # cases: list of (label, tool_name, tool_input, expected_deny)
        cases_json = json.dumps([
            {"label": c[0], "tool": c[1], "input": c[2], "expected": c[3]}
            for c in cases
        ])
        script = (
            f'import {{ isDeniedInOptimizeMode }} from "{DRAIN_TS}"\n'
            f"const cases = {cases_json}\n"
            "const out = cases.map(c => ({\n"
            "    label: c.label,\n"
            "    actual: isDeniedInOptimizeMode(c.tool, c.input),\n"
            "    expected: c.expected,\n"
            "}))\n"
            "process.stdout.write(JSON.stringify(out))\n"
        )
        with tempfile.NamedTemporaryFile(mode="w", suffix=".ts", delete=False) as fp:
            fp.write(script)
            script_path = fp.name
        try:
            result = subprocess.run(
                [BUN, "run", script_path], capture_output=True, text=True, timeout=30,
            )
        finally:
            Path(script_path).unlink(missing_ok=True)
        self.assertEqual(result.returncode, 0,
                         f"bun failed: stderr={result.stderr!r}")
        outcomes = json.loads(result.stdout)
        for o in outcomes:
            self.assertEqual(
                o["actual"], o["expected"],
                f"{o['label']}: expected {o['expected']!r}, got {o['actual']!r}"
            )

    def test_tool_name_deny_list(self):
        self._run_cases([
            ("Edit denied", "Edit", {}, True),
            ("Write denied", "Write", {}, True),
            ("MultiEdit denied", "MultiEdit", {}, True),
            ("edit_file denied", "edit_file", {}, True),
            ("file_write denied", "file_write", {}, True),
            ("file_edit denied", "file_edit", {}, True),
            ("delete_file denied", "delete_file", {}, True),
            ("Read NOT denied", "Read", {}, False),
            ("Glob NOT denied", "Glob", {}, False),
            ("Grep NOT denied", "Grep", {}, False),
            ("TodoWrite NOT denied", "TodoWrite", {}, False),
            ("WebSearch NOT denied", "WebSearch", {}, False),
            ("read_file NOT denied", "read_file", {}, False),
            ("None tool NOT denied", "", {}, False),
        ])

    def test_bash_redirects_and_mutations(self):
        self._run_cases([
            ("rm denied", "bash", {"command": "rm -rf /tmp/x"}, True),
            ("sed -i denied", "shell", {"command": "sed -i s/a/b/ f"}, True),
            ("sed -E -i denied", "bash", {"command": "sed -E -i s/a/b/ f"}, True),
            ("sed -e '...' -i denied", "bash", {"command": "sed -e s/a/b/ -i f"}, True),
            ("perl -CT -i denied", "bash", {"command": "perl -CT -i -pe s/a/b/ f"}, True),
            ("tee denied", "bash", {"command": "echo x | tee /tmp/x"}, True),
            ("redirect to file denied", "bash", {"command": "python b.py > out.txt"}, True),
            ("fd redirect denied", "bash", {"command": "python b.py 2>err.log"}, True),
            ("&> aggregate denied", "bash", {"command": "make &> log.txt"}, True),
            ("2>&1 alone NOT denied", "bash", {"command": "pytest 2>&1"}, False),
            ("echo with > inside quotes NOT denied", "bash",
             {"command": 'echo "x > y"'}, False),
            ("grep -R rm NOT denied", "bash", {"command": "grep -R rm src/"}, False),
            ("python bench.py NOT denied", "bash", {"command": "python bench.py"}, False),
            ("pytest NOT denied", "bash", {"command": "pytest tests/"}, False),
            ("path-prefixed rm denied", "bash", {"command": "/usr/bin/rm -rf x"}, True),
        ])

    def test_git_mutations(self):
        self._run_cases([
            ("git checkout denied", "bash", {"command": "git checkout -- f"}, True),
            ("git stash denied", "bash", {"command": "git stash"}, True),
            ("git stash push denied", "bash", {"command": "git stash push"}, True),
            ("git stash pop denied", "bash", {"command": "git stash pop"}, True),
            ("git stash list NOT denied", "bash", {"command": "git stash list"}, False),
            ("git stash show NOT denied", "bash", {"command": "git stash show"}, False),
            ("git -C checkout denied", "bash",
             {"command": "git -C . checkout -- f"}, True),
            ("git --git-dir reset denied", "bash",
             {"command": "git --git-dir=.git reset --hard"}, True),
            ("git -C log NOT denied", "bash",
             {"command": "git -C . log --oneline"}, False),
            ("git status NOT denied", "bash", {"command": "git status"}, False),
            ("git log NOT denied", "bash", {"command": "git log -5"}, False),
        ])

    def test_safe_prefix_exemption(self):
        self._run_cases([
            ("evo status NOT denied", "bash", {"command": "evo status"}, False),
            ("evo direct NOT denied", "bash",
             {"command": "evo direct 'try x'"}, False),
            ("evo redirect denied", "bash",
             {"command": "evo status > out.txt"}, True),
            ("claude spawn with log NOT denied", "bash",
             {"command": "nohup claude --print 'brief' > /tmp/log 2>&1 &"}, False),
            ("codex exec NOT denied", "bash",
             {"command": "codex exec --full-auto 'brief'"}, False),
            ("claude ; rm denied", "bash",
             {"command": "claude --print 'x' ; rm /tmp/y"}, True),
            ("evo wait && sed -i denied", "bash",
             {"command": "evo wait && sed -i s/x/y/ f"}, True),
        ])

    def test_command_substitution_recursion(self):
        self._run_cases([
            ("echo $(rm) denied", "bash",
             {"command": 'echo "$(rm -rf /tmp/x)"'}, True),
            ("echo $(pwd) NOT denied", "bash",
             {"command": 'echo "$(pwd)"'}, False),
            ("nested substitution mutation denied", "bash",
             {"command": 'echo "$(rm $(date +%s).log)"'}, True),
            ("sh -c inside substitution denied", "bash",
             {"command": "echo \"$(sh -c 'rm -rf /tmp/x')\""}, True),
            ("arithmetic NOT denied", "bash",
             {"command": 'echo "$((1 > 0))"'}, False),
            ("process subst rm denied", "bash",
             {"command": "cat <(rm /tmp/x)"}, True),
            ("benign process subst NOT denied", "bash",
             {"command": "diff <(sort a) <(sort b)"}, False),
            ('"<(rm)" as literal NOT denied', "bash",
             {"command": 'echo "<(rm /tmp/x)"'}, False),
        ])

    def test_bash_c_unwrap(self):
        self._run_cases([
            ("bash -c 'rm' denied", "bash",
             {"command": "bash -c 'rm -rf /tmp/x'"}, True),
            ("sh -c 'mv a b' denied", "bash",
             {"command": "sh -c 'mv a b'"}, True),
            ("zsh -ic 'cp a b' denied", "bash",
             {"command": "zsh -ic 'cp a b'"}, True),
            ("bash -c 'evo status' NOT denied", "bash",
             {"command": "bash -c 'evo status'"}, False),
        ])


@unittest.skipIf(BUN is None, "bun runtime not available")
class TestOpencodeStopNudgeAndPolicyState(unittest.TestCase):
    """End-to-end policy state + stop nudge via the TS bindings."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name).resolve()
        # Real workspace + run_dir so `markOptimizeMode` finds a session.
        subprocess.run(["git", "init", "-q"], cwd=self.root, check=True)
        subprocess.run(["git", "config", "user.email", "t@evo"], cwd=self.root, check=True)
        subprocess.run(["git", "config", "user.name", "T"], cwd=self.root, check=True)
        subprocess.run(["git", "config", "commit.gpgsign", "false"], cwd=self.root, check=True)
        (self.root / "README.md").write_text("x\n")
        subprocess.run(["git", "add", "."], cwd=self.root, check=True)
        subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=self.root, check=True)
        sys.path.insert(0, str(REPO_ROOT / "plugins" / "evo" / "src"))
        from evo.core import init_workspace
        init_workspace(
            self.root, target="agent.py", benchmark="python bench.py",
            metric="max", gate=None,
        )
        self.run_dir = next(iter((self.root / ".evo").glob("run_*")))

    def tearDown(self):
        self._tmp.cleanup()

    def _bun(self, body: str) -> dict:
        script = textwrap.dedent(f"""
            import {{
                registerSession,
                markOptimizeMode,
                unmarkOptimizeMode,
                markAutonomous,
                unmarkAutonomous,
                markSubagentsOnly,
                unmarkSubagentsOnly,
                maybeMarkOptimizeFromPrompt,
                shouldPolicyBlock,
                maybeStopNudgeText,
            }} from "{DRAIN_TS}"
            const runDir = "{self.run_dir}"
            {body}
        """).strip()
        with tempfile.NamedTemporaryFile(mode="w", suffix=".ts", delete=False) as fp:
            fp.write(script)
            script_path = fp.name
        try:
            r = subprocess.run([BUN, "run", script_path],
                               capture_output=True, text=True, timeout=15)
        finally:
            Path(script_path).unlink(missing_ok=True)
        self.assertEqual(r.returncode, 0,
                         f"bun failed: stderr={r.stderr!r}")
        return json.loads(r.stdout)

    def test_alternating_cadence(self):
        result = self._bun(textwrap.dedent("""
            registerSession(runDir, "oc1", "opencode")
            markOptimizeMode(runDir, "oc1")
            markSubagentsOnly(runDir, "oc1")  // deny-gate is opt-in
            const outs = []
            for (let i = 0; i < 5; i++) {
                outs.push(shouldPolicyBlock(runDir, "oc1", "edit_file",
                                            {file_path: "/f"}))
            }
            process.stdout.write(JSON.stringify(outs))
        """))
        # Pattern: blocks at #1, #3, #5; passes at #2, #4.
        self.assertEqual(result, [True, False, True, False, True])

    def test_subagent_never_blocked(self):
        result = self._bun(textwrap.dedent("""
            registerSession(runDir, "oc_sub", "opencode", "exp_0042")
            // Force optimize_mode (mark refuses subagents).
            const fs = await import("fs")
            const path = await import("path")
            const sfile = path.join(runDir, "inject", "sessions", "oc_sub.json")
            const rec = JSON.parse(fs.readFileSync(sfile, "utf8"))
            rec.optimize_mode = true
            fs.writeFileSync(sfile, JSON.stringify(rec))
            const out = shouldPolicyBlock(runDir, "oc_sub", "edit_file", {})
            process.stdout.write(JSON.stringify(out))
        """))
        self.assertEqual(result, False)

    def test_non_optimize_mode_never_blocked(self):
        result = self._bun(textwrap.dedent("""
            registerSession(runDir, "oc_casual", "opencode")
            // optimize_mode NOT set
            const out = shouldPolicyBlock(runDir, "oc_casual", "edit_file", {})
            process.stdout.write(JSON.stringify(out))
        """))
        self.assertEqual(result, False)

    def test_optimize_mode_without_subagents_only_allows_edits(self):
        """Default flip: /optimize alone keeps optimize_mode but allows
        orchestrator edits. The deny-gate only fires once subagents-only
        is armed."""
        result = self._bun(textwrap.dedent("""
            registerSession(runDir, "oc1", "opencode")
            markOptimizeMode(runDir, "oc1")
            // subagents_only NOT armed
            const out = shouldPolicyBlock(runDir, "oc1", "edit_file", {file_path: "/f"})
            process.stdout.write(JSON.stringify(out))
        """))
        self.assertEqual(result, False)

    def test_optimize_prompt_arms_mode(self):
        result = self._bun(textwrap.dedent("""
            registerSession(runDir, "oc1", "opencode")
            maybeMarkOptimizeFromPrompt(runDir, "oc1", "opencode", "/optimize do the thing")
            const fs = await import("fs")
            const path = await import("path")
            const sfile = path.join(runDir, "inject", "sessions", "oc1.json")
            const rec = JSON.parse(fs.readFileSync(sfile, "utf8"))
            process.stdout.write(JSON.stringify(rec.optimize_mode))
        """))
        self.assertEqual(result, True)

    def test_non_optimize_prompt_does_not_arm(self):
        result = self._bun(textwrap.dedent("""
            registerSession(runDir, "oc1", "opencode")
            maybeMarkOptimizeFromPrompt(runDir, "oc1", "opencode", "what is the weather")
            const fs = await import("fs")
            const path = await import("path")
            const sfile = path.join(runDir, "inject", "sessions", "oc1.json")
            const rec = JSON.parse(fs.readFileSync(sfile, "utf8"))
            process.stdout.write(JSON.stringify(rec.optimize_mode || false))
        """))
        self.assertEqual(result, False)

    def test_stop_nudge_fires_for_orchestrator_in_optimize(self):
        result = self._bun(textwrap.dedent("""
            registerSession(runDir, "oc1", "opencode")
            markOptimizeMode(runDir, "oc1")
            markAutonomous(runDir, "oc1")
            const text = maybeStopNudgeText(runDir, "oc1")
            process.stdout.write(JSON.stringify(text !== null && text.indexOf("EVO LOOP") >= 0))
        """))
        self.assertEqual(result, True)

    def test_stop_nudge_skipped_without_autonomous(self):
        """optimize_mode set but autonomous NOT armed → no nudge (opt-in)."""
        result = self._bun(textwrap.dedent("""
            registerSession(runDir, "oc1", "opencode")
            markOptimizeMode(runDir, "oc1")
            const text = maybeStopNudgeText(runDir, "oc1")
            process.stdout.write(JSON.stringify(text))
        """))
        self.assertEqual(result, None)

    def test_stop_nudge_skipped_for_subagent(self):
        result = self._bun(textwrap.dedent("""
            registerSession(runDir, "oc_sub", "opencode", "exp_0001")
            const fs = await import("fs")
            const path = await import("path")
            const sfile = path.join(runDir, "inject", "sessions", "oc_sub.json")
            const rec = JSON.parse(fs.readFileSync(sfile, "utf8"))
            rec.optimize_mode = true
            fs.writeFileSync(sfile, JSON.stringify(rec))
            const text = maybeStopNudgeText(runDir, "oc_sub")
            process.stdout.write(JSON.stringify(text))
        """))
        self.assertEqual(result, None)

    def test_unmark_disarms(self):
        result = self._bun(textwrap.dedent("""
            registerSession(runDir, "oc1", "opencode")
            markOptimizeMode(runDir, "oc1")
            unmarkOptimizeMode(runDir, "oc1")
            const text = maybeStopNudgeText(runDir, "oc1")
            process.stdout.write(JSON.stringify(text))
        """))
        self.assertEqual(result, None)


@unittest.skipIf(BUN is None, "bun runtime not available")
class TestOpencodePeekDontPop(unittest.TestCase):
    """Verify the peek/commit split so a failed prompt-injection doesn't
    silently consume queued directives."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name).resolve()
        subprocess.run(["git", "init", "-q"], cwd=self.root, check=True)
        subprocess.run(["git", "config", "user.email", "t@evo"], cwd=self.root, check=True)
        subprocess.run(["git", "config", "user.name", "T"], cwd=self.root, check=True)
        subprocess.run(["git", "config", "commit.gpgsign", "false"], cwd=self.root, check=True)
        (self.root / "README.md").write_text("x\n")
        subprocess.run(["git", "add", "."], cwd=self.root, check=True)
        subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=self.root, check=True)
        sys.path.insert(0, str(REPO_ROOT / "plugins" / "evo" / "src"))
        from evo.core import init_workspace
        init_workspace(
            self.root, target="agent.py", benchmark="python bench.py",
            metric="max", gate=None,
        )
        self.run_dir = next(iter((self.root / ".evo").glob("run_*")))

    def tearDown(self):
        self._tmp.cleanup()

    def _bun(self, body: str) -> dict:
        script = (
            f'import {{ peekDrainSession, commitDrainPeek, registerSession, markOptimizeMode }} '
            f'from "{DRAIN_TS}"\n'
            f'const runDir = "{self.run_dir}"\n'
            + body
        )
        with tempfile.NamedTemporaryFile(mode="w", suffix=".ts", delete=False) as fp:
            fp.write(script)
            script_path = fp.name
        try:
            r = subprocess.run(
                [BUN, "run", script_path], capture_output=True, text=True, timeout=15,
            )
        finally:
            Path(script_path).unlink(missing_ok=True)
        self.assertEqual(r.returncode, 0,
                         f"bun failed: stderr={r.stderr!r}")
        return json.loads(r.stdout)

    def test_peek_does_not_advance_offset(self):
        # Queue an event, peek; offset should not move.
        sys.path.insert(0, str(REPO_ROOT / "plugins" / "evo" / "src"))
        from evo.inject import queue, marker
        from evo.inject.registry import register_session

        register_session(self.root, "oc1", "opencode")
        queue.append_workspace_event(self.root, "TRY_X")
        marker.touch(self.root, "oc1")

        # First peek picks up the event.
        result = self._bun(textwrap.dedent("""
            const peek1 = peekDrainSession(runDir, "oc1")
            const peek2 = peekDrainSession(runDir, "oc1")
            process.stdout.write(JSON.stringify({
                first: peek1.text,
                second: peek2.text,
            }))
        """))
        # Both peeks see the same event — peek doesn't advance.
        self.assertIn("TRY_X", result["first"])
        self.assertEqual(result["first"], result["second"],
                         "peek must be idempotent — second peek should see "
                         "the same event since the first didn't advance offset")

    def test_commit_advances_offset_and_unlinks_marker(self):
        from evo.inject import queue, marker
        from evo.inject.registry import register_session

        register_session(self.root, "oc1", "opencode")
        queue.append_workspace_event(self.root, "TRY_X")
        marker.touch(self.root, "oc1")
        self.assertTrue(marker.exists(self.root, "oc1"))

        result = self._bun(textwrap.dedent("""
            const peek = peekDrainSession(runDir, "oc1")
            commitDrainPeek(runDir, "oc1", peek)
            const peek2 = peekDrainSession(runDir, "oc1")
            process.stdout.write(JSON.stringify({
                second_peek: peek2.text,
            }))
        """))
        # After commit, second peek returns null (offset advanced).
        self.assertIsNone(result["second_peek"])
        # Marker unlinked.
        self.assertFalse(marker.exists(self.root, "oc1"))


if __name__ == "__main__":
    unittest.main()

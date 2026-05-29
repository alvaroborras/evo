"""Tests for the PreToolUse policy nudge that steers the orchestrator
back to the /optimize protocol.

Design: a deny list of file-mutation tools and bash command patterns.
When the orchestrator (in optimize_mode) hits the deny list, the drain
emits a hard-deny envelope on every odd-numbered violation (1, 3, 5, …);
even-numbered ones pass through to give the agent room to comply or
override (via `evo exit-optimize-mode`).

Tests use real drain.py functions + real session records. No mocks of
real impls.

Run: pytest tests/unit/test_policy_nudge.py -v
"""

from __future__ import annotations

import io
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "plugins" / "evo" / "src"))

from evo.inject.registry import (
    register_session, mark_engaged, mark_optimize_mode, mark_autonomous,
    mark_subagents_only,
)


def _init_git_repo(root: Path) -> None:
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.email", "t@evo"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.name", "T"], cwd=root, check=True)
    subprocess.run(["git", "config", "commit.gpgsign", "false"], cwd=root, check=True)
    (root / "README.md").write_text("x\n")
    subprocess.run(["git", "add", "."], cwd=root, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=root, check=True)


def _make_workspace(root: Path) -> Path:
    _init_git_repo(root)
    from evo.core import init_workspace
    init_workspace(
        root, target="agent.py", benchmark="python bench.py",
        metric="max", gate=None,
    )
    return next(iter((root / ".evo").glob("run_*")))


def _drive_pretooluse(
    root: Path, sid: str, tool_name: str, tool_input: dict,
    host: str = "claude-code",
) -> dict:
    """Drive drain_session as a PreToolUse hook would. Returns parsed
    JSON envelope."""
    from evo.inject.drain import drain_session
    buf = io.StringIO()
    payload = {
        "session_id": sid,
        "hook_event_name": "PreToolUse",
        "tool_name": tool_name,
        "tool_input": tool_input,
    }
    with patch("sys.stdout", buf):
        drain_session(root, sid, host=host, hook_event="PreToolUse", payload=payload)
    out = buf.getvalue().strip()
    return json.loads(out) if out else {}


def _is_denied(envelope: dict) -> bool:
    """True if the policy-block envelope denies the tool, across host
    shapes:
      - cursor: `{"permission": "deny", "agent_message": "..."}`
      - claude-code/codex: `{"hookSpecificOutput": {"permissionDecision":
        "deny", ...}}` (the documented PreToolUse shape).
    """
    if envelope.get("permission") == "deny":
        return True
    hso = envelope.get("hookSpecificOutput") or {}
    return hso.get("permissionDecision") == "deny"


def _deny_reason(envelope: dict) -> str:
    """Return the reason text from a deny envelope, in whichever field
    the host's contract uses."""
    if "reason" in envelope:
        return envelope["reason"] or ""
    if "agent_message" in envelope:
        return envelope["agent_message"] or ""
    hso = envelope.get("hookSpecificOutput") or {}
    return hso.get("permissionDecisionReason") or ""


class _Base(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name).resolve()
        self.run_dir = _make_workspace(self.root)

    def tearDown(self):
        self._tmp.cleanup()


# ---------------------------------------------------------------------------
# Deny list: tool names
# ---------------------------------------------------------------------------

class TestDenyListToolNames(unittest.TestCase):

    def test_claude_code_edit_tools_denied(self):
        from evo.inject.drain import _is_denied_in_optimize_mode
        for t in ("Edit", "Write", "NotebookEdit", "MultiEdit"):
            self.assertTrue(_is_denied_in_optimize_mode(t, {}),
                            f"{t!r} must be denied")

    def test_hermes_patch_tool_denied(self):
        """Hermes registers `patch` as its file-edit tool (see
        hermes-agent file_tools.py — `registry.register(name="patch",
        ...)`). Without this entry, hermes orchestrator file edits
        slip past the policy gate."""
        from evo.inject.drain import _is_denied_in_optimize_mode
        self.assertTrue(_is_denied_in_optimize_mode("patch", {}),
                        "hermes `patch` tool must be denied")

    def test_cursor_edit_tools_denied(self):
        from evo.inject.drain import _is_denied_in_optimize_mode
        for t in ("edit_file", "create_file", "search_replace",
                  "str_replace", "delete_file", "applypatch", "apply_patch"):
            self.assertTrue(_is_denied_in_optimize_mode(t, {}),
                            f"{t!r} must be denied")

    def test_read_tools_not_denied(self):
        from evo.inject.drain import _is_denied_in_optimize_mode
        for t in ("Read", "Glob", "Grep", "read_file", "list_dir",
                  "TodoWrite", "WebFetch", "WebSearch"):
            self.assertFalse(_is_denied_in_optimize_mode(t, {}),
                             f"{t!r} must NOT be denied (it's read-only / non-mutating)")

    def test_unknown_tool_not_denied(self):
        from evo.inject.drain import _is_denied_in_optimize_mode
        self.assertFalse(_is_denied_in_optimize_mode("RandomTool", {}))
        self.assertFalse(_is_denied_in_optimize_mode(None, {}))
        self.assertFalse(_is_denied_in_optimize_mode("", {}))


# ---------------------------------------------------------------------------
# Deny list: bash command patterns
# ---------------------------------------------------------------------------

class TestDenyListBashPatterns(unittest.TestCase):

    def _denied(self, cmd: str) -> bool:
        from evo.inject.drain import _is_denied_in_optimize_mode
        return _is_denied_in_optimize_mode("Bash", {"command": cmd})

    def test_evo_invocations_pass(self):
        for cmd in (
            "evo status",
            "evo scratchpad",
            "evo wait --timeout 60",
            "evo direct 'try cosine sim'",
            "  evo brief --to exp_0001 'do x'",
        ):
            self.assertFalse(self._denied(cmd), f"evo cmd must pass: {cmd!r}")

    def test_subagent_spawn_passes(self):
        for cmd in (
            "claude --print --model claude-sonnet-4-5 'do task'",
            "codex exec --full-auto 'do task'",
            "cursor-agent -p 'brief'",
            "opencode run --model anthropic/claude-sonnet-4-5 'brief'",
            "nohup claude --print 'brief' > /tmp/agent.log 2>&1 &",
            "hermes chat --topic foo 'brief'",
            "pi 'brief'",
        ):
            self.assertFalse(self._denied(cmd), f"host-spawn must pass: {cmd!r}")

    def test_readonly_inspection_passes(self):
        for cmd in (
            "git status",
            "git log --oneline -10",
            "git diff HEAD~1",
            "ls -la .evo",
            "cat .evo/config.json",
            "find . -name '*.py'",
            "grep -r 'TODO' src/",
            "head -20 README.md",
            "tail -50 /tmp/log",
            "pwd",
            "env | grep EVO",
            "echo hello",
            "wc -l file.txt",
            "which python",
            "python bench.py",  # running scripts is fine; not a mutation
            "pytest -xvs tests/",
            "make eval",
            "node index.js",
        ):
            self.assertFalse(self._denied(cmd), f"read-only/run cmd must pass: {cmd!r}")

    def test_redirect_to_file_denied(self):
        for cmd in (
            "echo hi > /tmp/out",
            "echo hi >> /tmp/out",
            "python bench.py > out.txt",
            "pytest > /tmp/results 2>&1",  # `2>&1` is fd duplication, not a deny
        ):
            self.assertTrue(self._denied(cmd), f"stdout redirect to file must deny: {cmd!r}")

    def test_fd_redirect_to_file_denied(self):
        for cmd in (
            "python x.py 2>err.log",
            "make 2>> errors",
            "python x.py 2> err.log",
        ):
            self.assertTrue(self._denied(cmd), f"fd redirect to file must deny: {cmd!r}")

    def test_fd_duplication_not_denied_by_itself(self):
        """`2>&1` duplicates fd 2 onto fd 1 — no file write. Driven
        through the public deny API so a regression in either layer
        (regex OR the segment dispatcher) is caught."""
        self.assertFalse(self._denied("pytest 2>&1"),
                         "`2>&1` alone (fd-to-fd) must not be flagged")
        self.assertFalse(self._denied("make build 2>&1"),
                         "`2>&1` alone (fd-to-fd) must not be flagged")

    def test_aggregate_redirect_denied(self):
        for cmd in (
            "python x.py &> /tmp/log",
            "make &>> build.log",
        ):
            self.assertTrue(self._denied(cmd), f"aggregate redirect must deny: {cmd!r}")

    def test_file_mutations_denied(self):
        for cmd in (
            "tee /tmp/x",
            "echo hi | tee -a /tmp/x",
            "sed -i 's/x/y/' f.py",
            "awk -i inplace '{print}' f.py",
            "perl -i -pe 's/x/y/' f.py",
            "mv a b",
            "cp src/foo.py wt/",
            "rm -rf experiments/",
            "mkdir custom_exp",
            "rmdir foo",
            "touch foo",
            "chmod +x foo.sh",
            "chown me:me foo",
            "ln -s a b",
            "rsync -av src/ dst/",
            "dd if=/dev/zero of=/tmp/big bs=1M count=10",
            "truncate -s 0 /tmp/x",
            "patch -p1 < diff",
            "install -m 0755 src dst",
        ):
            self.assertTrue(self._denied(cmd), f"file mutation must deny: {cmd!r}")

    def test_curl_wget_writing_denied(self):
        for cmd in (
            "curl -o out.json https://api/...",
            "curl -O https://example.com/file",
            "curl --output out.json https://api/...",
            "curl --remote-name https://example.com/file",
            "wget https://example.com/file",
        ):
            self.assertTrue(self._denied(cmd), f"download must deny: {cmd!r}")

    def test_git_worktree_mutations_denied(self):
        for cmd in (
            "git apply patch.diff",
            "git checkout -- file.py",
            "git restore file.py",
            "git reset --hard HEAD~1",
            "git clean -fd",
            "git switch other-branch",
            "git merge other",
            "git rebase main",
            "git am < patch",
            "git stash",
            "git stash push",
            "git stash save 'wip'",
            "git stash pop",
            "git stash apply",
            "git stash drop",
            "git cherry-pick abc123",
            "git pull",
            "git clone https://example.com/repo",
            "git revert HEAD",
            "git worktree add ../wt",
        ):
            self.assertTrue(self._denied(cmd), f"git mutation must deny: {cmd!r}")

    def test_git_stash_readonly_subcommands_pass(self):
        """`git stash list` and `git stash show` are read-only; must
        not be denied. The lookahead in the git pattern carves these
        out from the otherwise-deny `stash`."""
        for cmd in (
            "git stash list",
            "git stash show",
            "git stash show stash@{0}",
        ):
            self.assertFalse(self._denied(cmd),
                             f"git stash list/show must pass: {cmd!r}")

    def test_sed_perl_option_order_variants_denied(self):
        """Flag ordering: `sed -E -i` / `sed -e '...' -i` / `perl -CT -i`
        all mutate in place. The non-greedy preamble in the segment-deny
        regex must catch these."""
        for cmd in (
            "sed -E -i 's/a/b/' f.py",
            "sed -e 's/a/b/' -i f.py",
            "sed -nE -i 's/a/b/' f.py",
            "sed --regexp-extended -i 's/x/y/' f.py",
            "perl -CT -i -pe 's/a/b/' f.py",
            "perl -CSD -i.bak -pe 's/x/y/' f.py",
        ):
            self.assertTrue(self._denied(cmd),
                            f"in-place edit with options must deny: {cmd!r}")

    def test_interactive_editors_denied(self):
        for cmd in ("vim file.py", "vi file", "nano f", "emacs f"):
            self.assertTrue(self._denied(cmd), f"editor must deny: {cmd!r}")

    def test_inert_quoted_redirect_not_denied(self):
        """`echo "this > that"` is shell-safe — `>` is inside double
        quotes, not a real redirect. Must not be flagged."""
        for cmd in (
            'echo "this > that"',
            "echo 'a > b > c'",
            'evo direct "fix x > y bug"',
        ):
            self.assertFalse(self._denied(cmd), f"inert quoted must pass: {cmd!r}")

    def test_command_substitution_inside_dq_still_scanned(self):
        """`echo "$(rm -rf /)"` must NOT be skipped — command substitution
        inside double quotes still fires."""
        self.assertTrue(self._denied('echo "$(rm -rf /tmp/x)"'),
                        "command substitution inside dq must be scanned")

    def test_benign_substitution_passes(self):
        """Substitution by itself isn't denial-worthy — only mutations
        inside the substituted body are. `echo "$(pwd)"`,
        `git log "$(git rev-parse HEAD)"` are routine read-only work."""
        for cmd in (
            'echo "$(pwd)"',
            'printf "%s\\n" "$(git rev-parse --show-toplevel)"',
            "ls $(pwd)",
            'cat "$(find . -name evo.json | head -1)"',
            "echo `date`",
        ):
            self.assertFalse(self._denied(cmd),
                             f"benign substitution must pass: {cmd!r}")

    def test_nested_substitution_with_mutation_denied(self):
        """Recursion goes deep enough: `echo "$(rm $(date +%s).log)"`
        — outer body recurses to inner; inner is benign; outer body
        starts with `rm` and denies."""
        self.assertTrue(self._denied('echo "$(rm $(date +%s).log)"'),
                        "nested substitution with rm in middle layer must deny")

    def test_git_global_options_before_subcommand_denied(self):
        """`git -C path checkout`, `git --git-dir=…/.git reset --hard`,
        `git --work-tree=.` etc. mutate the worktree just like the
        unqualified form. The global-option preamble must not bypass
        the deny regex."""
        for cmd in (
            "git -C . checkout -- file.py",
            "git -C /tmp/wt stash pop",
            "git --git-dir=.git reset --hard HEAD",
            "git --work-tree=. --git-dir=.git checkout -- a",
            "git -c user.name=foo merge other",
            "git -P clean -fd",
        ):
            self.assertTrue(self._denied(cmd),
                            f"git with global options must deny: {cmd!r}")

    def test_git_global_options_with_readonly_subcommand_pass(self):
        """`git -C path log/status/diff/show` are read-only even with
        global options."""
        for cmd in (
            "git -C . status",
            "git -C /tmp/wt log --oneline -5",
            "git --git-dir=.git diff HEAD~1",
            "git -C . stash list",
            "git -C . stash show",
        ):
            self.assertFalse(self._denied(cmd),
                             f"git read-only with global options must pass: {cmd!r}")

    def test_sh_c_inside_substitution_denied(self):
        """`echo "$(sh -c 'rm -rf /tmp/x')"` — the inner `sh -c '…'`
        has its single quotes preserved during substitution extraction
        (we extract before inert-strip), so the recursion sees the
        real body and unwraps the inner `sh -c` to find `rm`."""
        for cmd in (
            'echo "$(sh -c \'rm -rf /tmp/x\')"',
            "echo \"$(bash -c 'sed -i s/a/b/ f.py')\"",
            "echo \"$(zsh -c 'mv a b')\"",
        ):
            self.assertTrue(self._denied(cmd),
                            f"sh -c inside substitution must deny: {cmd!r}")

    def test_process_substitution_with_mutation_denied(self):
        """`cat <(rm -rf /tmp/x)` and `diff <(...) <(rm ...)` execute
        their bodies as subprocesses. The body is denied recursively."""
        for cmd in (
            "cat <(rm -rf /tmp/x)",
            "diff <(sort a) <(rm /tmp/x)",
            "grep foo >(sed -i 's/x/y/' /tmp/out)",
        ):
            self.assertTrue(self._denied(cmd),
                            f"process substitution with mutation must deny: {cmd!r}")

    def test_process_substitution_benign_passes(self):
        """`diff <(sort a) <(sort b)` is routine; must not deny."""
        for cmd in (
            "diff <(sort a) <(sort b)",
            "cat <(echo 'hello world')",
        ):
            self.assertFalse(self._denied(cmd),
                             f"benign process substitution must pass: {cmd!r}")

    def test_process_substitution_in_double_quotes_is_literal(self):
        """`echo "<(rm -rf /tmp/x)"` — `<(…)` inside double quotes is
        literal text, not process substitution. Must not deny."""
        for cmd in (
            'echo "<(rm -rf /tmp/x)"',
            'echo "look at <(sed -i s/x/y/ f)"',
            "echo '<(rm -rf /tmp/x)'",
        ):
            self.assertFalse(self._denied(cmd),
                             f"<(…) inside quotes is literal: {cmd!r}")

    def test_arithmetic_expansion_not_treated_as_substitution(self):
        """`$((1 > 0))`, `$((a + b))` — arithmetic expansion. The
        contents are math, not a shell command, so the `>` redirect
        regex must not trip on them."""
        for cmd in (
            'echo "$((1 > 0))"',
            "let x=$((5 + 3))",
            "echo $((a > b ? 1 : 0))",
            'printf "%d" "$((2 ** 8))"',
        ):
            self.assertFalse(self._denied(cmd),
                             f"arithmetic expansion must pass: {cmd!r}")

    def test_chained_command_after_safe_prefix_denied(self):
        """`claude --print 'x' ; rm -rf /tmp` — the safe prefix exempts
        the claude call but the chained `rm` must still trip."""
        for cmd in (
            "claude --print 'x' ; rm -rf /tmp/junk",
            "evo status && sed -i 's/a/b/' f.py",
            "evo wait || mv a b",
            "claude --print 'x' | tee /tmp/log",
            "evo brief --to e1 'x'\nrm /tmp/x",
        ):
            self.assertTrue(self._denied(cmd), f"chained mutator must deny: {cmd!r}")

    def test_safe_prefix_with_redirect_passes(self):
        """Subagent spawn idioms include `… > /tmp/log 2>&1 &` — the
        safe-prefix exemption covers the whole command."""
        for cmd in (
            "nohup claude --print 'brief' > /tmp/agent.log 2>&1 &",
            "claude --print 'brief' >/tmp/out",
        ):
            self.assertFalse(self._denied(cmd), f"safe-prefix with redirect must pass: {cmd!r}")

    def test_bash_dash_c_wrapper_unwrapped(self):
        """`bash -c "rm -rf x"` — the deny scan must inspect inside the
        quoted argument."""
        for cmd in (
            "bash -c 'rm -rf /tmp/junk'",
            'bash -c "sed -i s/x/y/ f.py"',
            "sh -c 'mv a b'",
            "zsh -ic 'cp a b'",
            "bash -eo pipefail -c 'tee /tmp/x'",
        ):
            self.assertTrue(self._denied(cmd), f"bash -c wrapper must deny: {cmd!r}")

    def test_unbalanced_quotes_dont_crash(self):
        """Bad shell quoting (unbalanced) must not raise — just don't
        unwrap and scan as-is."""
        # Should return without exception; result may be True or False.
        from evo.inject.drain import _is_denied_in_optimize_mode
        _is_denied_in_optimize_mode("Bash", {"command": "bash -c 'oops"})
        _is_denied_in_optimize_mode("Bash", {"command": 'echo "broken'})

    def test_evo_redirect_denied(self):
        """`evo X > file` is the orchestrator capturing state to a file —
        a manual-workflow stray that the deny should catch. Host-spawn
        prefixes (claude/codex/…) keep the redirect exemption for
        subagent-spawn logging; evo does not."""
        for cmd in (
            "evo status > out.txt",
            "evo scratchpad >> notes.md",
            "evo wait > /tmp/log",
            "evo direct 'x' > /tmp/d",
        ):
            self.assertTrue(self._denied(cmd),
                            f"evo redirect must deny: {cmd!r}")

    def test_command_substitution_under_safe_prefix_denied(self):
        """`$(…)` and backticks under a host-spawn prefix must NOT be
        exempted — the substituted body is a smuggling vector."""
        for cmd in (
            'claude --print "$(rm -rf /tmp/junk)"',
            "claude --print `rm -rf /tmp/junk`",
            'evo direct "$(rm -rf /tmp/junk)"',
            "codex exec --full-auto \"$(sed -i s/a/b/ f.py)\"",
            "nohup claude --print \"$(rm x)\" > /tmp/log 2>&1 &",
        ):
            self.assertTrue(self._denied(cmd),
                            f"command substitution under safe prefix must deny: {cmd!r}")

    def test_mutating_token_inside_read_only_args_passes(self):
        """`grep -R rm src/`, `find . -name install`, `echo rm` — the
        mutating verb appears as an argument, not as the command. The
        per-segment anchored regex must not deny these."""
        for cmd in (
            "grep -R rm src/",
            "find . -name install",
            "git diff -- scripts/install.sh",
            "echo rm",
            "echo cp a b",
            "cat /tmp/mkdir.log",
            "ls -la rm",
            "git log --grep='rm files'",
        ):
            self.assertFalse(self._denied(cmd),
                             f"mutating token in args must not deny: {cmd!r}")

    def test_absolute_path_to_mutating_verb_still_denied(self):
        """`/usr/bin/rm -rf …`, `/bin/sed -i …` — the path prefix must
        not bypass the deny."""
        for cmd in (
            "/usr/bin/rm -rf /tmp/x",
            "/bin/sed -i s/a/b/ f.py",
            "/usr/local/bin/wget https://x",
        ):
            self.assertTrue(self._denied(cmd),
                            f"path-prefixed mutator must deny: {cmd!r}")


# ---------------------------------------------------------------------------
# Alternating cadence: odd violations block, even pass
# ---------------------------------------------------------------------------

class TestAlternatingCadence(_Base):

    def _setup_orchestrator(self) -> None:
        register_session(self.root, "orch", "claude-code")
        mark_engaged(self.root, "orch")
        mark_optimize_mode(self.root, "orch")
        mark_subagents_only(self.root, "orch")  # deny-gate is opt-in

    def test_first_edit_is_blocked_with_banner(self):
        self._setup_orchestrator()
        out = _drive_pretooluse(self.root, "orch", "Edit",
                                {"file_path": "/some/file.py"})
        self.assertTrue(_is_denied(out), f"first Edit must be hard-denied; got {out!r}")
        self.assertIn("optimize", _deny_reason(out).lower(),
                      "banner must mention optimize protocol")

    def test_second_edit_passes(self):
        self._setup_orchestrator()
        _drive_pretooluse(self.root, "orch", "Edit", {"file_path": "/f.py"})
        out = _drive_pretooluse(self.root, "orch", "Edit", {"file_path": "/f.py"})
        self.assertFalse(_is_denied(out), f"#2 must pass under alternating cadence; got {out!r}")

    def test_third_edit_blocks_again(self):
        self._setup_orchestrator()
        outs = [
            _drive_pretooluse(self.root, "orch", "Edit", {"file_path": "/f.py"})
            for _ in range(3)
        ]
        self.assertTrue(_is_denied(outs[0]), "#1 blocks")
        self.assertFalse(_is_denied(outs[1]), "#2 passes")
        self.assertTrue(_is_denied(outs[2]), "#3 blocks")

    def test_alternating_through_ten(self):
        self._setup_orchestrator()
        outs = [
            _drive_pretooluse(self.root, "orch", "Edit", {"file_path": "/f.py"})
            for _ in range(10)
        ]
        # Odd-indexed-from-1 → indices 0, 2, 4, 6, 8 block
        for i, out in enumerate(outs, start=1):
            if i % 2 == 1:
                self.assertTrue(_is_denied(out), f"violation #{i} (odd) must block; got {out!r}")
            else:
                self.assertFalse(_is_denied(out), f"violation #{i} (even) must pass; got {out!r}")

    def test_edits_and_bash_share_counter(self):
        """Edit + Bash violations increment the same counter; the alternating
        pattern holds across the mixed stream."""
        self._setup_orchestrator()
        outs = [
            _drive_pretooluse(self.root, "orch", "Edit", {"file_path": "/a.py"}),
            _drive_pretooluse(self.root, "orch", "Bash", {"command": "rm -rf x"}),
            _drive_pretooluse(self.root, "orch", "Edit", {"file_path": "/b.py"}),
            _drive_pretooluse(self.root, "orch", "Bash", {"command": "mv a b"}),
            _drive_pretooluse(self.root, "orch", "Edit", {"file_path": "/c.py"}),
        ]
        for i, out in enumerate(outs, start=1):
            if i % 2 == 1:
                self.assertTrue(_is_denied(out), f"mixed #{i} (odd) must block")
            else:
                self.assertFalse(_is_denied(out), f"mixed #{i} (even) must pass")


# ---------------------------------------------------------------------------
# Bash deny gating
# ---------------------------------------------------------------------------

class TestBashBlock(_Base):

    def _setup_orchestrator(self) -> None:
        register_session(self.root, "orch", "claude-code")
        mark_engaged(self.root, "orch")
        mark_optimize_mode(self.root, "orch")
        mark_subagents_only(self.root, "orch")  # deny-gate is opt-in

    def test_first_mutating_bash_blocked(self):
        self._setup_orchestrator()
        out = _drive_pretooluse(
            self.root, "orch", "Bash", {"command": "sed -i 's/a/b/' f.py"},
        )
        self.assertTrue(_is_denied(out), f"first mutating bash must block; got {out!r}")

    def test_evo_bash_never_blocks(self):
        self._setup_orchestrator()
        for cmd in ("evo status", "evo scratchpad", "evo wait", "evo direct 'foo'"):
            out = _drive_pretooluse(self.root, "orch", "Bash", {"command": cmd})
            self.assertFalse(_is_denied(out), f"evo Bash must never block: {cmd!r}; got {out!r}"
            )

    def test_subagent_spawn_bash_never_blocks(self):
        self._setup_orchestrator()
        for cmd in (
            "claude --print 'brief' &",
            "nohup codex exec --full-auto 'brief' > /tmp/a.log 2>&1 &",
        ):
            out = _drive_pretooluse(self.root, "orch", "Bash", {"command": cmd})
            self.assertFalse(_is_denied(out), f"subagent-spawn must never block: {cmd!r}; got {out!r}"
            )

    def test_running_scripts_does_not_block(self):
        self._setup_orchestrator()
        # `python bench.py` / `pytest` / `make` aren't mutating commands
        # in themselves — the deny list catches only file mutations.
        for cmd in ("python bench.py", "pytest tests/", "make eval", "node index.js"):
            out = _drive_pretooluse(self.root, "orch", "Bash", {"command": cmd})
            self.assertFalse(_is_denied(out), f"running scripts must not block: {cmd!r}; got {out!r}"
            )


# ---------------------------------------------------------------------------
# Subagent + non-optimize-mode + read tools never blocked
# ---------------------------------------------------------------------------

class TestPolicyNudgeDoesNotBlock(_Base):

    def test_subagent_edits_never_blocked(self):
        register_session(self.root, "sub", "claude-code", exp_id="exp_0042")
        mark_engaged(self.root, "sub")
        for _ in range(10):
            out = _drive_pretooluse(self.root, "sub", "Edit", {"file_path": "/f.py"})
            self.assertFalse(_is_denied(out), "subagents must never be policy-blocked")

    def test_non_optimize_mode_session_never_blocked(self):
        register_session(self.root, "casual", "claude-code")
        mark_engaged(self.root, "casual")
        for _ in range(10):
            out = _drive_pretooluse(self.root, "casual", "Edit", {"file_path": "/f.py"})
            self.assertFalse(_is_denied(out), "non-optimize-mode session must not be blocked")

    def test_optimize_mode_without_subagents_only_allows_edits(self):
        """Default flip: /optimize alone keeps optimize_mode but allows
        orchestrator edits. The deny-gate fires only after subagents_only
        is armed (via `evo subagents-only on`)."""
        register_session(self.root, "orch", "claude-code")
        mark_engaged(self.root, "orch")
        mark_optimize_mode(self.root, "orch")
        # subagents_only NOT armed
        for _ in range(10):
            out = _drive_pretooluse(self.root, "orch", "Edit", {"file_path": "/f.py"})
            self.assertFalse(
                _is_denied(out),
                "optimize_mode without subagents_only must allow orchestrator edits")

    def test_read_tools_never_blocked(self):
        register_session(self.root, "orch", "claude-code")
        mark_engaged(self.root, "orch")
        mark_optimize_mode(self.root, "orch")
        for tool in ("Read", "Glob", "Grep", "TodoWrite", "WebFetch"):
            out = _drive_pretooluse(self.root, "orch", tool, {"file_path": "/f.py"})
            self.assertFalse(_is_denied(out), f"{tool} must never be blocked")


# ---------------------------------------------------------------------------
# Cross-host: Cursor — both via drain_session (camelCase hook_event) AND
# via the full main() pipeline as the cursor hooks.json would invoke it.
# ---------------------------------------------------------------------------

class TestCursorPolicyNudge(_Base):

    def _setup_cursor_orchestrator(self) -> None:
        register_session(self.root, "cursor_sid", "cursor")
        mark_engaged(self.root, "cursor_sid")
        mark_optimize_mode(self.root, "cursor_sid")
        mark_autonomous(self.root, "cursor_sid")  # stop-nudge is opt-in
        mark_subagents_only(self.root, "cursor_sid")  # deny-gate is opt-in

    def test_cursor_edit_file_blocked_via_drain_session(self):
        """Direct drain_session entry, claude-code-style PreToolUse."""
        self._setup_cursor_orchestrator()
        out = _drive_pretooluse(
            self.root, "cursor_sid", "edit_file",
            {"file_path": "/f.py"},
            host="cursor",
        )
        self.assertTrue(_is_denied(out), f"cursor edit_file must be denied on first violation; got {out!r}"
        )

    def test_cursor_deny_envelope_uses_agent_message_not_reason(self):
        """Cursor's preToolUse contract documents `permission`,
        `user_message`, `agent_message`, `updated_input` as return
        fields. `reason` is NOT documented and cursor silently drops it.
        The deny envelope must use `agent_message` so the policy banner
        actually reaches the model.

        Verified against ~/.cursor/skills-cursor/create-hook/SKILL.md:
        'preToolUse: can return permission, user_message, agent_message,
        and updated_input'.
        """
        self._setup_cursor_orchestrator()
        out = _drive_pretooluse(
            self.root, "cursor_sid", "edit_file",
            {"file_path": "/f.py"},
            host="cursor",
        )
        self.assertEqual(out.get("permission"), "deny")
        self.assertIn("agent_message", out,
                      f"cursor deny must use agent_message field; got {out!r}")
        self.assertNotIn("reason", out,
                         "cursor doesn't honor `reason` on preToolUse — "
                         "must use agent_message instead")
        self.assertIn("EVO POLICY", out["agent_message"],
                      "policy banner must be in agent_message")

    def test_cursor_camelcase_pretooluse_blocked_via_drain_session(self):
        """Cursor sends `preToolUse` (camelCase). The policy block must
        accept both case variants so it fires through the real cursor
        event-name path."""
        self._setup_cursor_orchestrator()
        from evo.inject.drain import drain_session
        buf = io.StringIO()
        payload = {
            "session_id": "cursor_sid",
            "hook_event_name": "preToolUse",
            "tool_name": "edit_file",
            "tool_input": {"file_path": "/f.py"},
        }
        with patch("sys.stdout", buf):
            drain_session(self.root, "cursor_sid", host="cursor",
                          hook_event="preToolUse", payload=payload)
        out = json.loads(buf.getvalue() or "{}")
        self.assertTrue(_is_denied(out), f"camelCase preToolUse must trigger policy block; got {out!r}"
        )

    def test_cursor_camelcase_pretooluse_via_main_pipeline(self):
        """End-to-end: invoke evo.inject.drain.main() the same way the
        cursor hooks.json would. Stdin payload uses camelCase event name
        and `conversation_id` (cursor doesn't always set `session_id` on
        every event). Must emit the deny envelope."""
        self._setup_cursor_orchestrator()
        from evo.inject import drain as drain_mod

        payload_json = json.dumps({
            "hook_event_name": "preToolUse",
            "conversation_id": "cursor_sid",
            "workspace_roots": [str(self.root)],
            "tool_name": "edit_file",
            "tool_input": {"file_path": "/some/path.py"},
        })
        buf = io.StringIO()
        with patch("sys.stdin", io.StringIO(payload_json)), \
             patch("sys.stdout", buf):
            drain_mod.main(["--host", "cursor"])
        out = json.loads(buf.getvalue() or "{}")
        self.assertTrue(_is_denied(out), f"cursor edit_file under optimize_mode must deny on #1; got {out!r}"
        )


class TestCursorStopNudge(_Base):

    def _setup_cursor_orchestrator(self) -> None:
        register_session(self.root, "cursor_sid", "cursor")
        mark_engaged(self.root, "cursor_sid")
        mark_optimize_mode(self.root, "cursor_sid")
        mark_autonomous(self.root, "cursor_sid")  # stop-nudge is opt-in
        mark_subagents_only(self.root, "cursor_sid")  # deny-gate is opt-in

    def test_cursor_stop_emits_followup_message(self):
        """Cursor doesn't honor `decision: block` — its stop-continuation
        envelope is `{followup_message: text}`. The drain must dispatch
        the nudge through emit_for_host's cursor branch."""
        self._setup_cursor_orchestrator()
        from evo.inject.drain import drain_session
        buf = io.StringIO()
        with patch("sys.stdout", buf):
            drain_session(self.root, "cursor_sid", host="cursor",
                          hook_event="stop")
        out = json.loads(buf.getvalue() or "{}")
        msg = out.get("followup_message") or ""
        self.assertIn("optimize", msg.lower(),
                      f"cursor stop must emit followup_message with the nudge; got {out!r}")
        self.assertIn("evo wait", msg.lower())
        self.assertIn("evo exit-optimize-mode", msg.lower())

    def test_cursor_subagent_stop_emits_followup_message(self):
        self._setup_cursor_orchestrator()
        from evo.inject.drain import drain_session
        buf = io.StringIO()
        with patch("sys.stdout", buf):
            drain_session(self.root, "cursor_sid", host="cursor",
                          hook_event="subagentStop")
        out = json.loads(buf.getvalue() or "{}")
        self.assertIn("optimize", (out.get("followup_message") or "").lower())

    def test_cursor_stop_via_main_pipeline(self):
        """End-to-end through main(): cursor hooks.json invokes evo-drain
        with stdin payload using camelCase `stop` and `conversation_id`.
        Must emit followup_message even with no marker (always-fire)."""
        self._setup_cursor_orchestrator()
        from evo.inject import drain as drain_mod

        payload_json = json.dumps({
            "hook_event_name": "stop",
            "conversation_id": "cursor_sid",
            "workspace_roots": [str(self.root)],
        })
        buf = io.StringIO()
        with patch("sys.stdin", io.StringIO(payload_json)), \
             patch("sys.stdout", buf):
            drain_mod.main(["--host", "cursor"])
        out = json.loads(buf.getvalue() or "{}")
        self.assertIn(
            "optimize", (out.get("followup_message") or "").lower(),
            f"cursor stop must always-fire under optimize_mode; got {out!r}"
        )

    def test_cursor_even_denied_preToolUse_does_not_consume_directive(self):
        """Regression: even-numbered denied violations (cadence lets them
        through) on cursor non-shell tools must NOT consume directives.
        The policy block fires on odd, lets even pass — but the cursor
        IDE drops `additional_context`, so emitting an envelope would
        silently lose any queued `evo direct`."""
        from evo.inject import drain as drain_mod
        from evo.inject import queue, marker

        register_session(self.root, "cursor_sid", "cursor")
        mark_engaged(self.root, "cursor_sid")
        mark_optimize_mode(self.root, "cursor_sid")
        mark_subagents_only(self.root, "cursor_sid")  # deny-gate is opt-in

        # Violation #1 — blocked, no consume.
        out1 = _drive_pretooluse(
            self.root, "cursor_sid", "edit_file",
            {"file_path": "/f.py"}, host="cursor",
        )
        self.assertEqual(out1.get("permission"), "deny")

        # Now queue a directive and drop a marker.
        queue.append_workspace_event(self.root, "DELIVER_ME")
        marker.touch(self.root, "cursor_sid")

        # Violation #2 — under alternating cadence, this passes (no
        # deny). But it must NOT consume the directive.
        payload_json = json.dumps({
            "hook_event_name": "preToolUse",
            "conversation_id": "cursor_sid",
            "workspace_roots": [str(self.root)],
            "tool_name": "edit_file",
            "tool_input": {"file_path": "/f.py"},
        })
        buf = io.StringIO()
        with patch("sys.stdin", io.StringIO(payload_json)), \
             patch("sys.stdout", buf):
            drain_mod.main(["--host", "cursor"])
        out2 = json.loads(buf.getvalue() or "{}")
        self.assertFalse(_is_denied(out2), "#2 must pass under alternating cadence")
        self.assertNotIn("additional_context", out2,
                         "#2 must not emit additional_context (cursor drops it)")
        self.assertTrue(
            marker.exists(self.root, "cursor_sid"),
            "even-numbered denied tool must not consume the directive"
        )

    def test_cursor_non_denied_preToolUse_does_not_consume_directive(self):
        """Regression: in optimize_mode, a non-denied tool (Read/Grep)
        on cursor must NOT route through drain_session — that would
        consume queued directives by emitting additional_context which
        cursor drops, silently losing the directive."""
        from evo.inject import drain as drain_mod
        from evo.inject import queue, marker

        register_session(self.root, "cursor_sid", "cursor")
        mark_engaged(self.root, "cursor_sid")
        mark_optimize_mode(self.root, "cursor_sid")

        # Enqueue a directive and drop a marker.
        queue.append_workspace_event(self.root, "DELIVER_ME_LATER")
        marker.touch(self.root, "cursor_sid")

        # A Read tool fires (non-denied, non-shell). The gate must
        # defer — not consume.
        payload_json = json.dumps({
            "hook_event_name": "preToolUse",
            "conversation_id": "cursor_sid",
            "workspace_roots": [str(self.root)],
            "tool_name": "read_file",
            "tool_input": {"file_path": "/tmp/x"},
        })
        buf = io.StringIO()
        with patch("sys.stdin", io.StringIO(payload_json)), \
             patch("sys.stdout", buf):
            drain_mod.main(["--host", "cursor"])
        out = json.loads(buf.getvalue() or "{}")

        # Either no envelope at all, or no additional_context (the
        # critical thing: directive must NOT be consumed yet).
        self.assertNotIn("permission", out,
                         f"non-denied Read must not emit a deny: {out!r}")
        # Marker should still exist — directive not yet consumed.
        self.assertTrue(
            marker.exists(self.root, "cursor_sid"),
            "marker must still exist — Read should not consume the directive"
        )

    def test_cursor_stop_not_fired_outside_optimize_mode(self):
        """A casual cursor session (no optimize_mode) must NOT get a
        forced followup_message — that would force-continue every turn."""
        register_session(self.root, "casual_cursor", "cursor")
        mark_engaged(self.root, "casual_cursor")
        # optimize_mode NOT set

        from evo.inject.drain import drain_session
        buf = io.StringIO()
        with patch("sys.stdout", buf):
            drain_session(self.root, "casual_cursor", host="cursor",
                          hook_event="stop")
        out = json.loads(buf.getvalue() or "{}")
        # Either nothing or an empty additional_context envelope is fine;
        # what must NOT happen is a followup_message forcing continuation.
        self.assertNotIn("followup_message", out,
                         f"casual cursor session must not be force-continued; got {out!r}")


if __name__ == "__main__":
    unittest.main()

"""Tests for auto-detection of /evo:optimize invocation from
UserPromptSubmit-like hook payloads.

When the user types `/evo:optimize` (claude-code), `$evo:optimize`
(codex), `/optimize` (cursor), etc., the corresponding host fires a
prompt-submit hook with the user's text in the payload. The drain
pattern-matches the text against host-specific patterns and flips
optimize_mode on the session record. No agent action required; no
skill changes.

These tests exercise drain.main() directly (the same entrypoint the
host hook invokes), with stdin patched to a synthetic payload. No
mocks of real impls — real registry, real filesystem, real drain.

Run: pytest tests/unit/test_optimize_detection.py -v
"""

from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "plugins" / "evo" / "src"))

from evo.inject.paths import session_file
from evo.inject.registry import register_session


def _init_git_repo(root: Path) -> None:
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.email", "t@evo"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.name", "T"], cwd=root, check=True)
    subprocess.run(["git", "config", "commit.gpgsign", "false"], cwd=root, check=True)
    (root / "README.md").write_text("x\n")
    subprocess.run(["git", "add", "."], cwd=root, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=root, check=True)


def _make_workspace(root: Path) -> None:
    _init_git_repo(root)
    from evo.core import init_workspace
    init_workspace(
        root, target="agent.py", benchmark="python bench.py",
        metric="max", gate=None,
    )


def _read_record(root: Path, sid: str) -> dict | None:
    p = session_file(root, sid)
    if not p.exists():
        return None
    return json.loads(p.read_text())


class _BaseDetectionTest(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name).resolve()
        _make_workspace(self.root)

    def tearDown(self):
        self._tmp.cleanup()

    def _fire_prompt_submit(self, sid: str, host: str, prompt: str,
                            hook_event: str = "UserPromptSubmit") -> None:
        """Invoke drain.main() as the host hook would, with a synthetic
        payload. Returns nothing — assertion happens via reading the
        session record."""
        from evo.inject.drain import main
        # Self-contained Cursor invocation uses --host cursor + key on
        # conversation_id. Other hosts use the Rust hot path which passes
        # --run-dir + --session. Synthesize the right shape per host.
        if host == "cursor":
            payload = {
                "hook_event_name": hook_event,
                "conversation_id": sid,
                "workspace_roots": [str(self.root)],
                "prompt": prompt,
            }
            stdin_buf = io.StringIO(json.dumps(payload))
            stdout_buf = io.StringIO()
            with patch("sys.stdin", stdin_buf), patch("sys.stdout", stdout_buf):
                main(["--host", "cursor"])
        else:
            # Rust hot path: run_dir + session passed as args
            run_dir = next(iter((self.root / ".evo").glob("run_*")))
            payload = {
                "session_id": sid,
                "hook_event_name": hook_event,
                "prompt": prompt,
            }
            stdin_buf = io.StringIO(json.dumps(payload))
            stdout_buf = io.StringIO()
            with patch("sys.stdin", stdin_buf), patch("sys.stdout", stdout_buf):
                main(["--run-dir", str(run_dir), "--session", sid, "--host", host])


# ---------------------------------------------------------------------------
# Claude Code: /evo:optimize
# ---------------------------------------------------------------------------

class TestClaudeCodeDetection(_BaseDetectionTest):

    def test_slash_evo_optimize_flips_optimize_mode(self):
        register_session(self.root, "cc_sid", "claude-code")
        self._fire_prompt_submit("cc_sid", "claude-code", "/evo:optimize")
        rec = _read_record(self.root, "cc_sid")
        self.assertTrue(
            rec["optimize_mode"],
            "/evo:optimize at prompt start must flip optimize_mode on claude-code"
        )

    def test_slash_evo_optimize_with_args_still_matches(self):
        register_session(self.root, "cc_sid2", "claude-code")
        self._fire_prompt_submit(
            "cc_sid2", "claude-code",
            "/evo:optimize subagents=3 budget=10",
        )
        rec = _read_record(self.root, "cc_sid2")
        self.assertTrue(rec["optimize_mode"])

    def test_unrelated_prompt_does_not_flip(self):
        register_session(self.root, "cc_sid3", "claude-code")
        self._fire_prompt_submit("cc_sid3", "claude-code", "hello, please help me debug this")
        rec = _read_record(self.root, "cc_sid3")
        self.assertFalse(
            rec["optimize_mode"],
            "regular prompts must not flip the flag"
        )

    def test_slash_optimize_alias_matches(self):
        """`/optimize` is the un-namespaced alias claude-code registers
        alongside `/evo:optimize`. Most users type the shorter form."""
        register_session(self.root, "cc_alias", "claude-code")
        self._fire_prompt_submit("cc_alias", "claude-code", "/optimize fix the bug")
        rec = _read_record(self.root, "cc_alias")
        self.assertTrue(rec["optimize_mode"])

    def test_mid_prompt_invocation_matches(self):
        """Position-agnostic by design: users naturally type
        "lets try again /optimize on this" and expect the gates to arm.
        Subagent fence (mark_optimize_mode refusing to flag a session
        with exp_id) — not the regex anchor — is what protects subagent
        briefs from accidental flipping."""
        register_session(self.root, "cc_mid", "claude-code")
        self._fire_prompt_submit(
            "cc_mid", "claude-code",
            "lets try again /optimize on agent.py with budget 5"
        )
        rec = _read_record(self.root, "cc_mid")
        self.assertTrue(
            rec["optimize_mode"],
            "mid-prompt invocations must arm (position-agnostic)"
        )

    def test_file_path_does_not_match(self):
        """Boundary class excludes `/` so file paths like
        `src/optimize.py` don't accidentally arm optimize_mode."""
        register_session(self.root, "cc_path", "claude-code")
        self._fire_prompt_submit(
            "cc_path", "claude-code",
            "Please look at src/optimize.py and tell me what it does"
        )
        rec = _read_record(self.root, "cc_path")
        self.assertFalse(
            rec["optimize_mode"],
            "file path src/optimize.py must not arm — `/` before is in the boundary exclusion"
        )

    def test_identifier_substring_does_not_match(self):
        """`auto-optimize` is an identifier, not an invocation — no `/`
        prefix means no match."""
        register_session(self.root, "cc_ident", "claude-code")
        self._fire_prompt_submit(
            "cc_ident", "claude-code",
            "Run the auto-optimize routine first, then check results"
        )
        rec = _read_record(self.root, "cc_ident")
        self.assertFalse(rec["optimize_mode"])


# ---------------------------------------------------------------------------
# Codex: $evo:optimize / $evo optimize
# ---------------------------------------------------------------------------

class TestCodexDetection(_BaseDetectionTest):

    def test_dollar_optimize_bare(self):
        """Codex registers the skill as bare `optimize`, so the natural
        invocation is `$optimize`. Verified against
        codex-rs/core-skills/src/render.rs which documents
        `$SkillName` as the canonical sigil form."""
        register_session(self.root, "cdx_bare", "codex")
        self._fire_prompt_submit("cdx_bare", "codex", "$optimize")
        rec = _read_record(self.root, "cdx_bare")
        self.assertTrue(rec["optimize_mode"])

    def test_dollar_evo_colon_optimize(self):
        """Plugin-namespaced form. Codex's name-char set is
        [A-Za-z0-9_:-] (codex-rs/core-skills/src/injection.rs:506-508),
        so `$evo:optimize` parses as one token."""
        register_session(self.root, "cdx_ns", "codex")
        self._fire_prompt_submit("cdx_ns", "codex", "$evo:optimize")
        rec = _read_record(self.root, "cdx_ns")
        self.assertTrue(rec["optimize_mode"])

    def test_dollar_optimize_mid_prompt(self):
        """Codex's scanner is position-agnostic — `$optimize` mid-prompt
        is a real invocation. Mirrors codex's actual behavior; verified
        by test_extract_tool_mentions_handles_plain_and_linked_mentions
        in codex-rs/core-skills/src/injection_tests.rs:88-94."""
        register_session(self.root, "cdx_mid", "codex")
        self._fire_prompt_submit(
            "cdx_mid", "codex",
            "actually let me run $optimize on this first"
        )
        rec = _read_record(self.root, "cdx_mid")
        self.assertTrue(rec["optimize_mode"])

    def test_dollar_evo_space_optimize_does_not_match(self):
        """Codex name tokens terminate at whitespace, so `$evo optimize`
        is parsed as `$evo` followed by the literal word `optimize` —
        NOT a single skill invocation. The prior regex matched this
        incorrectly."""
        register_session(self.root, "cdx_space", "codex")
        self._fire_prompt_submit("cdx_space", "codex", "$evo optimize")
        rec = _read_record(self.root, "cdx_space")
        self.assertFalse(
            rec["optimize_mode"],
            "$evo<space>optimize is two tokens in codex, not one invocation"
        )

    def test_codex_dollar_does_not_match_claude_slash_form(self):
        """A codex session that somehow received claude-code's slash-only
        form shouldn't flip via the `$` regex. (The `/optimize` defensive
        fallback still matches in codex — see test below.)"""
        register_session(self.root, "cdx_claude", "codex")
        self._fire_prompt_submit("cdx_claude", "codex", "$evo:other_skill")
        rec = _read_record(self.root, "cdx_claude")
        self.assertFalse(rec["optimize_mode"])

    def test_codex_accepts_slash_optimize_defensively(self):
        """Defensive cross-host fallback — users mixing slash-command
        muscle memory from claude-code/cursor type `/optimize` in codex
        too. We accept it so the test-and-learn loop isn't broken by
        client-convention drift."""
        register_session(self.root, "cdx_slash", "codex")
        self._fire_prompt_submit("cdx_slash", "codex", "/optimize")
        rec = _read_record(self.root, "cdx_slash")
        self.assertTrue(rec["optimize_mode"])


# ---------------------------------------------------------------------------
# Cursor: /optimize
# ---------------------------------------------------------------------------

class TestCursorDetection(_BaseDetectionTest):

    def test_slash_optimize(self):
        # Cursor uses its self-contained drain path — session is keyed on
        # conversation_id and registers lazily. The detection happens after
        # registration on the same event.
        self._fire_prompt_submit(
            "cursor_conv", "cursor", "/optimize",
            hook_event="beforeSubmitPrompt",
        )
        rec = _read_record(self.root, "cursor_conv")
        self.assertIsNotNone(rec, "session should register on first prompt event")
        self.assertTrue(
            rec["optimize_mode"],
            "/optimize at prompt start must flip optimize_mode on cursor"
        )


# ---------------------------------------------------------------------------
# Hermes: `/optimize` + `/<plugin>:optimize` (bundled-plugin namespace)
# ---------------------------------------------------------------------------

class TestHermesDetection(_BaseDetectionTest):

    def test_bare_slash_optimize(self):
        register_session(self.root, "h1", "hermes")
        self._fire_prompt_submit("h1", "hermes", "/optimize",
                                 hook_event="UserPromptSubmit")
        rec = _read_record(self.root, "h1")
        self.assertTrue(rec["optimize_mode"])

    def test_namespaced_plugin_form(self):
        """hermes bundles can register skills as `/plugin:skill`."""
        register_session(self.root, "h2", "hermes")
        self._fire_prompt_submit("h2", "hermes", "/evo:optimize fix the bug",
                                 hook_event="UserPromptSubmit")
        rec = _read_record(self.root, "h2")
        self.assertTrue(rec["optimize_mode"])

    def test_mid_prompt(self):
        register_session(self.root, "h3", "hermes")
        self._fire_prompt_submit(
            "h3", "hermes", "ok now /optimize agent.py please",
            hook_event="UserPromptSubmit",
        )
        rec = _read_record(self.root, "h3")
        self.assertTrue(rec["optimize_mode"])


# ---------------------------------------------------------------------------
# Openclaw: `/optimize` + `/skill optimize` (generic invoker)
# ---------------------------------------------------------------------------

class TestOpenclawDetection(_BaseDetectionTest):

    def test_bare_slash_optimize(self):
        register_session(self.root, "oc1", "openclaw")
        self._fire_prompt_submit("oc1", "openclaw", "/optimize",
                                 hook_event="UserPromptSubmit")
        rec = _read_record(self.root, "oc1")
        self.assertTrue(rec["optimize_mode"])

    def test_slash_skill_invoker(self):
        """`/skill <name>` is always available in openclaw, even for skills
        not marked user-invocable (registered as textAlias "/skill" in
        commands-registry.shared.ts)."""
        register_session(self.root, "oc2", "openclaw")
        self._fire_prompt_submit("oc2", "openclaw", "/skill optimize",
                                 hook_event="UserPromptSubmit")
        rec = _read_record(self.root, "oc2")
        self.assertTrue(rec["optimize_mode"])


# ---------------------------------------------------------------------------
# Pi: `/skill:optimize` (canonical) + `/optimize` (defensive)
# ---------------------------------------------------------------------------

class TestPiDetection(_BaseDetectionTest):

    def test_canonical_skill_colon_form(self):
        """pi requires the `/skill:` prefix per agent-session.ts:1149
        — bare `/optimize` is a prompt-template lookup, not a skill."""
        register_session(self.root, "pi1", "pi")
        self._fire_prompt_submit("pi1", "pi", "/skill:optimize",
                                 hook_event="UserPromptSubmit")
        rec = _read_record(self.root, "pi1")
        self.assertTrue(rec["optimize_mode"])

    def test_bare_optimize_defensive(self):
        """Defensive cross-host fallback: even though pi itself won't
        expand bare `/optimize`, users naturally type it. Arm the gate
        so the steering still works."""
        register_session(self.root, "pi2", "pi")
        self._fire_prompt_submit("pi2", "pi", "/optimize",
                                 hook_event="UserPromptSubmit")
        rec = _read_record(self.root, "pi2")
        self.assertTrue(rec["optimize_mode"])

    def test_skill_colon_with_args(self):
        register_session(self.root, "pi3", "pi")
        self._fire_prompt_submit("pi3", "pi", "/skill:optimize budget=5",
                                 hook_event="UserPromptSubmit")
        rec = _read_record(self.root, "pi3")
        self.assertTrue(rec["optimize_mode"])


# ---------------------------------------------------------------------------
# Subagent fence — never flip optimize_mode on a subagent session
# ---------------------------------------------------------------------------

class TestSubagentFence(_BaseDetectionTest):

    def test_subagent_session_cannot_be_flipped_even_with_matching_prompt(self):
        """Position-agnostic detection means orchestrator briefs that
        mention `/evo:optimize` mid-text WILL hit the regex. The fence
        is enforced inside mark_optimize_mode (refuses sessions with
        exp_id set), not by the regex anchor. This test asserts the
        fence still holds for both anchored and mid-prompt forms."""
        register_session(
            self.root, "sub_anchored", "claude-code", exp_id="exp_0099"
        )
        self._fire_prompt_submit(
            "sub_anchored", "claude-code", "/evo:optimize do this thing"
        )
        rec = _read_record(self.root, "sub_anchored")
        self.assertFalse(
            rec["optimize_mode"],
            "subagent session (exp_id set) must never be flipped"
        )

        register_session(
            self.root, "sub_midprompt", "claude-code", exp_id="exp_0100"
        )
        self._fire_prompt_submit(
            "sub_midprompt", "claude-code",
            "Brief: you are running under /evo:optimize. Run exp_0042.",
        )
        rec = _read_record(self.root, "sub_midprompt")
        self.assertFalse(
            rec["optimize_mode"],
            "subagent fence must hold even when regex matches mid-prompt"
        )


# ---------------------------------------------------------------------------
# Already-engaged orchestrator getting /optimize a second time
# ---------------------------------------------------------------------------

class TestIdempotentDetection(_BaseDetectionTest):

    def test_already_in_optimize_mode_stays_in_optimize_mode(self):
        from evo.inject.registry import mark_optimize_mode
        register_session(self.root, "idemp_sid", "claude-code")
        mark_optimize_mode(self.root, "idemp_sid")
        original_at = _read_record(self.root, "idemp_sid")["optimize_mode_at"]
        # Re-invoke /optimize
        self._fire_prompt_submit("idemp_sid", "claude-code", "/evo:optimize")
        rec = _read_record(self.root, "idemp_sid")
        self.assertTrue(rec["optimize_mode"], "stays on")
        self.assertEqual(
            rec["optimize_mode_at"], original_at,
            "timestamp should not bump on idempotent re-invocation"
        )


if __name__ == "__main__":
    unittest.main()

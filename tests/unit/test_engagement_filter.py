"""Unit tests for the `evo direct` engagement filter.

Background: hosts auto-register sessions whenever a Claude Code / Codex /
Cursor / etc. window opens in an evo workspace, regardless of whether the
agent actually engages with evo. Today this means `evo direct` fans out
imperative directives to sessions doing unrelated work in the same repo.

The fix: `auto_register_from_env` (called on every `evo` CLI invocation)
flips `has_evo_engaged: true` on the session record. `cmd_direct` filters
fanout to engaged sessions only, for hosts in HOSTS_WITH_ENGAGEMENT. The
transition seeds the workspace offset to the current tail so directives
queued during the unengaged period don't replay.

These tests are written against the post-fix behavior. They will fail
against the pre-fix code (red baseline), then pass after the fix is
applied (green).

No mocks of real implementations — uses real register_session,
auto_register_from_env, cmd_direct, drain main(), and real on-disk
session / queue / marker files. The only `unittest.mock` use is
`patch.dict(os.environ, ...)` for env-var control and `patch("sys.stdout"|"sys.stdin", buf)`
for I/O capture / injection — both consistent with the existing
test_inject.py convention.

Run: pytest tests/unit/test_engagement_filter.py -v
"""

from __future__ import annotations

import argparse
import datetime as dt
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

from evo.inject import marker, queue
from evo.inject.paths import (
    exp_events_path,
    session_file,
    workspace_events_path,
)
from evo.inject.registry import (
    STALE_AFTER_SECONDS,
    auto_register_from_env,
    list_active_sessions,
    register_session,
)


# ---------------------------------------------------------------------------
# Helpers (mirror test_inject.py conventions)
# ---------------------------------------------------------------------------

_HOST_ENV_VARS = (
    "CLAUDE_CODE_SESSION_ID",
    "CODEX_THREAD_ID",
    "HERMES_SESSION_ID",
    "OPENCODE_SESSION_ID",
    "EVO_EXP_ID",
)


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
        root,
        target="agent.py",
        benchmark="python bench.py",
        metric="max",
        gate=None,
    )


def _clear_host_env() -> None:
    for v in _HOST_ENV_VARS:
        os.environ.pop(v, None)


def _build_args(*args: str) -> argparse.Namespace:
    return argparse.Namespace(args=list(args))


def _read_record(root: Path, sid: str) -> dict | None:
    p = session_file(root, sid)
    if not p.exists():
        return None
    return json.loads(p.read_text())


# ---------------------------------------------------------------------------
# Schema: new fields default sensibly
# ---------------------------------------------------------------------------

class TestEngagementSchemaDefaults(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name).resolve()
        _make_workspace(self.root)

    def tearDown(self):
        self._tmp.cleanup()

    def test_new_session_defaults_to_unengaged(self):
        register_session(self.root, "fresh", "claude-code")
        rec = _read_record(self.root, "fresh")
        assert rec is not None
        # Field is present and false. Without the fix, it's missing — treat
        # missing as failure (we want the field explicitly written so
        # downstream `.get("has_evo_engaged")` is unambiguous).
        assert "has_evo_engaged" in rec, "schema missing has_evo_engaged field"
        assert rec["has_evo_engaged"] is False
        assert rec.get("engaged_at") is None


# ---------------------------------------------------------------------------
# auto_register_from_env: flips engagement on first call for that session
# ---------------------------------------------------------------------------

class TestAutoRegisterMarksEngagement(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name).resolve()
        _make_workspace(self.root)

    def tearDown(self):
        self._tmp.cleanup()

    def test_auto_register_flips_has_evo_engaged_true(self):
        # Simulate the Rust binary having registered at SessionStart
        # (unengaged), without the agent having run any evo command yet.
        register_session(self.root, "claude_sid", "claude-code")
        rec = _read_record(self.root, "claude_sid")
        assert rec["has_evo_engaged"] is False

        # Agent now runs `evo status` (or any CLI command) — the CLI's
        # main() calls auto_register_from_env, which should detect the
        # session via env and flip the engagement bit.
        with patch.dict(os.environ, {"CLAUDE_CODE_SESSION_ID": "claude_sid"}, clear=False):
            _clear_host_env()
            os.environ["CLAUDE_CODE_SESSION_ID"] = "claude_sid"
            auto_register_from_env(self.root)

        rec = _read_record(self.root, "claude_sid")
        assert rec["has_evo_engaged"] is True
        assert rec.get("engaged_at") is not None

    def test_auto_register_codex_also_flips_engagement(self):
        register_session(self.root, "codex_sid", "codex")
        with patch.dict(os.environ, {"CODEX_THREAD_ID": "codex_sid"}, clear=False):
            _clear_host_env()
            os.environ["CODEX_THREAD_ID"] = "codex_sid"
            auto_register_from_env(self.root)
        rec = _read_record(self.root, "codex_sid")
        assert rec["has_evo_engaged"] is True


# ---------------------------------------------------------------------------
# Engagement transition seeds offset (prevents backfill of pre-engagement directives)
# ---------------------------------------------------------------------------

class TestEngagementTransitionSeedsOffset(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name).resolve()
        _make_workspace(self.root)

    def tearDown(self):
        self._tmp.cleanup()

    def test_directive_queued_before_engagement_does_not_replay(self):
        # Step 1: session registered, not engaged.
        register_session(self.root, "sid_x", "claude-code")

        # Step 2: a directive is queued while the session is unengaged.
        # Under the fix, cmd_direct won't touch this session's marker,
        # so we simulate this by writing the event directly to the queue.
        pre_id = queue.append_workspace_event(self.root, "DIRECTIVE FROM BEFORE ENGAGEMENT")

        # Step 3: agent runs `evo` → auto_register_from_env transitions
        # the session false → true and must seed the offset to the tail.
        with patch.dict(os.environ, {"CLAUDE_CODE_SESSION_ID": "sid_x"}, clear=False):
            _clear_host_env()
            os.environ["CLAUDE_CODE_SESSION_ID"] = "sid_x"
            auto_register_from_env(self.root)

        # The offset for this session should now be at pre_id (i.e., the
        # pre-engagement directive will not deliver on next drain).
        offset = queue.read_offset(self.root, "sid_x", "workspace")
        assert offset == pre_id, (
            f"engagement transition must seed offset to tail; "
            f"got offset={offset!r}, expected pre_id={pre_id!r}"
        )

        # Step 4: a new directive is queued post-engagement.
        post_id = queue.append_workspace_event(self.root, "DIRECTIVE POST ENGAGEMENT")

        # On drain, only the post-engagement directive should appear.
        new_events = queue.read_events_after(workspace_events_path(self.root), offset)
        assert len(new_events) == 1
        assert new_events[0]["id"] == post_id
        assert new_events[0]["text"] == "DIRECTIVE POST ENGAGEMENT"


# ---------------------------------------------------------------------------
# cmd_direct fanout filter
# ---------------------------------------------------------------------------

class TestCmdDirectEngagementFilter(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name).resolve()
        _make_workspace(self.root)
        self._old_cwd = Path.cwd()
        os.chdir(self.root)

    def tearDown(self):
        os.chdir(self._old_cwd)
        self._tmp.cleanup()

    def _run_cmd_direct(self, *parts: str) -> tuple[int, str]:
        from evo.cli import cmd_direct
        buf = io.StringIO()
        with patch("sys.stdout", buf):
            rc = cmd_direct(_build_args(*parts))
        return rc, buf.getvalue()

    def test_unengaged_claude_code_session_excluded_from_fanout(self):
        # Session registered (via Rust SessionStart) but agent never ran evo.
        register_session(self.root, "ghost", "claude-code")
        rc, out = self._run_cmd_direct("imperative directive")
        assert rc == 0
        # Marker must NOT be touched on an unengaged session.
        assert not marker.exists(self.root, "ghost"), (
            "fanout reached an unengaged claude-code session — "
            "engagement filter is not active"
        )
        # Diagnostics should report it.
        assert "skipped_unengaged=1" in out, (
            f"expected skipped_unengaged in output; got: {out!r}"
        )

    def test_unengaged_codex_session_excluded_from_fanout(self):
        register_session(self.root, "codex_ghost", "codex")
        rc, out = self._run_cmd_direct("hello")
        assert not marker.exists(self.root, "codex_ghost")
        assert "skipped_unengaged=1" in out

    def test_unengaged_cursor_session_excluded_from_fanout(self):
        register_session(self.root, "cursor_ghost", "cursor")
        rc, out = self._run_cmd_direct("hello")
        assert not marker.exists(self.root, "cursor_ghost")
        assert "skipped_unengaged=1" in out

    def test_engaged_claude_code_session_receives_marker(self):
        register_session(self.root, "active", "claude-code")
        # Engage via auto_register_from_env
        with patch.dict(os.environ, {"CLAUDE_CODE_SESSION_ID": "active"}, clear=False):
            _clear_host_env()
            os.environ["CLAUDE_CODE_SESSION_ID"] = "active"
            auto_register_from_env(self.root)
        rc, out = self._run_cmd_direct("real directive")
        assert marker.exists(self.root, "active"), \
            "engaged session must receive the marker"
        assert "fanout=1" in out

    def test_subagent_excluded_regardless_of_engagement(self):
        # Subagents are always skipped — they drain their own queue.
        register_session(self.root, "sub", "claude-code", exp_id="exp_0001")
        # Even if explicitly marked engaged, subagents must still be skipped.
        rc, out = self._run_cmd_direct("workspace directive")
        assert not marker.exists(self.root, "sub")
        assert "skipped_subagent=1" in out

    def test_host_outside_engagement_set_is_not_filtered(self):
        # The engagement filter only applies to hosts in
        # HOSTS_WITH_ENGAGEMENT. As of v0.4.4 all known hosts are in
        # the set (claude-code, codex, cursor, hermes, opencode,
        # openclaw, pi), so a session whose host string is unknown
        # (legacy registry entry, future host before it's wired in,
        # or test-only synthetic host) should pass through unfiltered.
        register_session(self.root, "future_host_sess", "future-unknown-host")
        rc, out = self._run_cmd_direct("msg")
        assert marker.exists(self.root, "future_host_sess"), (
            "host outside HOSTS_WITH_ENGAGEMENT must still receive fanout"
        )
        assert "fanout=1" in out

    def test_diagnostics_include_all_three_counts(self):
        # Mix of orchestrator/subagent/unengaged/engaged.
        register_session(self.root, "engaged", "claude-code")
        with patch.dict(os.environ, {"CLAUDE_CODE_SESSION_ID": "engaged"}, clear=False):
            _clear_host_env()
            os.environ["CLAUDE_CODE_SESSION_ID"] = "engaged"
            auto_register_from_env(self.root)
        register_session(self.root, "unengaged", "claude-code")
        register_session(self.root, "subagent", "claude-code", exp_id="exp_0099")

        rc, out = self._run_cmd_direct("multi-target directive")
        assert "fanout=1" in out
        assert "skipped_unengaged=1" in out
        assert "skipped_subagent=1" in out


# ---------------------------------------------------------------------------
# register_session: merges non-null exp_id into existing record
# ---------------------------------------------------------------------------

class TestRegisterSessionMergesExpId(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name).resolve()
        _make_workspace(self.root)

    def tearDown(self):
        self._tmp.cleanup()

    def test_existing_record_picks_up_exp_id(self):
        # Rust binary registers at SessionStart with exp_id=null.
        register_session(self.root, "dual_sid", "claude-code")
        rec = _read_record(self.root, "dual_sid")
        assert rec["exp_id"] is None

        # Python auto_register_from_env runs later with EVO_EXP_ID set
        # (subagent dispatched into an experiment).
        register_session(self.root, "dual_sid", "claude-code", exp_id="exp_0123")

        rec = _read_record(self.root, "dual_sid")
        assert rec["exp_id"] == "exp_0123", \
            "register_session must merge non-null exp_id into existing record"

    def test_existing_record_picks_up_parent_session_id(self):
        register_session(self.root, "dual2", "claude-code")
        register_session(self.root, "dual2", "claude-code", parent_session_id="parent_sid")
        rec = _read_record(self.root, "dual2")
        assert rec["parent_session_id"] == "parent_sid"

    def test_merge_does_not_clobber_existing_exp_id(self):
        # Once exp_id is set, calling register_session again without
        # passing exp_id must not blank it out.
        register_session(self.root, "stable", "claude-code", exp_id="exp_0042")
        register_session(self.root, "stable", "claude-code")  # second call, exp_id arg defaults to None
        rec = _read_record(self.root, "stable")
        assert rec["exp_id"] == "exp_0042"


# ---------------------------------------------------------------------------
# STALE_AFTER_SECONDS bumped to 30 days
# ---------------------------------------------------------------------------

class TestStaleWindow(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name).resolve()
        _make_workspace(self.root)

    def tearDown(self):
        self._tmp.cleanup()

    def test_stale_after_seconds_is_at_least_30_days(self):
        thirty_days = 30 * 24 * 60 * 60
        assert STALE_AFTER_SECONDS >= thirty_days, (
            f"STALE_AFTER_SECONDS={STALE_AFTER_SECONDS}, "
            f"want >= {thirty_days} (30 days)"
        )

    def test_session_25_days_old_is_still_active(self):
        register_session(self.root, "old_but_alive", "claude-code")
        # Backdate last_seen_at to 25 days ago.
        path = session_file(self.root, "old_but_alive")
        rec = json.loads(path.read_text())
        rec["last_seen_at"] = (
            dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=25)
        ).isoformat(timespec="seconds")
        path.write_text(json.dumps(rec))

        active = list_active_sessions(self.root)
        sids = [s.get("session_id") for s in active]
        assert "old_but_alive" in sids, (
            "session 25 days old must still be active under 30-day window"
        )


# ---------------------------------------------------------------------------
# Cursor drain marks engagement on `evo` shell commands
# ---------------------------------------------------------------------------

class TestCursorDrainDetectsEvoCommand(unittest.TestCase):
    """Cursor sessions are registered via the drain's payload (no env var
    fallback today). The drain therefore needs to detect when the agent
    runs an `evo` command via a shell tool and mark the session engaged
    itself."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name).resolve()
        _make_workspace(self.root)

    def tearDown(self):
        self._tmp.cleanup()

    def _invoke_drain_main(self, payload: dict) -> str:
        """Invoke drain.main() directly with the given hook payload on stdin."""
        from evo.inject.drain import main
        stdin_buf = io.StringIO(json.dumps(payload))
        stdout_buf = io.StringIO()
        with patch("sys.stdin", stdin_buf), patch("sys.stdout", stdout_buf):
            main(["--host", "cursor"])
        return stdout_buf.getvalue()

    def test_cursor_preToolUse_shell_evo_marks_engagement(self):
        sid = "cursor_conv_42"

        # Step 1: sessionStart registers the cursor session (unengaged).
        self._invoke_drain_main({
            "hook_event_name": "sessionStart",
            "conversation_id": sid,
            "workspace_roots": [str(self.root)],
        })
        rec = _read_record(self.root, sid)
        assert rec is not None, "sessionStart must register the cursor session"
        assert rec.get("has_evo_engaged") is False, \
            "session is freshly registered — should not yet be engaged"

        # Step 2: agent runs `evo status` via the Cursor shell tool.
        # The drain should observe the command and mark engagement.
        self._invoke_drain_main({
            "hook_event_name": "preToolUse",
            "conversation_id": sid,
            "workspace_roots": [str(self.root)],
            "tool_name": "shell",
            "tool_input": {"command": "evo status"},
        })

        rec = _read_record(self.root, sid)
        assert rec.get("has_evo_engaged") is True, (
            "Cursor drain must mark engagement when it sees an `evo` "
            "shell command"
        )

    def test_cursor_non_evo_shell_does_not_mark_engagement(self):
        sid = "cursor_conv_43"
        self._invoke_drain_main({
            "hook_event_name": "sessionStart",
            "conversation_id": sid,
            "workspace_roots": [str(self.root)],
        })

        # Some unrelated shell command — should NOT trigger engagement.
        self._invoke_drain_main({
            "hook_event_name": "preToolUse",
            "conversation_id": sid,
            "workspace_roots": [str(self.root)],
            "tool_name": "shell",
            "tool_input": {"command": "ls -la"},
        })

        rec = _read_record(self.root, sid)
        assert rec.get("has_evo_engaged") is False


# ---------------------------------------------------------------------------
# SessionStart drain must not backfill pre-existing events
# (closes the leak where Rust's unconditional SessionStart drain bypasses
# the engagement filter)
# ---------------------------------------------------------------------------

class TestSessionStartDoesNotBackfill(unittest.TestCase):
    """The Rust binary unconditionally hands stdin to the Python drain on
    SessionStart (no marker check on that event — see
    evo-hook-drain-rs/src/main.rs:319-323). If drain_session backfills
    every event in workspace.jsonl for a fresh session, the engagement
    filter is bypassed: an unrelated session opens later and drains every
    pending directive queued by a prior session.

    Fix: drain_session seeds the offset to the current queue tail when no
    offset file exists yet, so fresh sessions only see events queued
    AFTER they registered.
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name).resolve()
        _make_workspace(self.root)

    def tearDown(self):
        self._tmp.cleanup()

    def test_pre_existing_events_not_delivered_to_fresh_session(self):
        # An event is in the queue BEFORE any session registers (e.g.
        # left over from a previous run).
        pre_id = queue.append_workspace_event(
            self.root, "stale directive that should not deliver"
        )

        # A fresh session registers (simulating Rust SessionStart's
        # register_session call).
        register_session(self.root, "fresh", "claude-code")

        # Rust unconditionally invokes drain on SessionStart.
        from evo.inject.drain import drain_session
        captured = io.StringIO()
        with patch("sys.stdout", captured):
            drain_session(
                self.root, "fresh",
                host="claude-code", hook_event="SessionStart",
            )

        emitted = captured.getvalue()
        assert "stale directive" not in emitted, (
            "fresh session at SessionStart must not backfill pre-existing "
            f"events; got emitted={emitted!r}"
        )
        # Offset must be seeded past the pre-existing event.
        offset = queue.read_offset(self.root, "fresh", "workspace")
        assert offset == pre_id, (
            f"offset must be seeded to queue tail; got offset={offset!r}, "
            f"expected={pre_id!r}"
        )

    def test_post_registration_events_DO_deliver(self):
        # Sanity: directives queued AFTER a session registers still
        # deliver normally. This is the post-engagement evo direct flow.
        register_session(self.root, "active", "claude-code")
        post_id = queue.append_workspace_event(self.root, "real directive")
        marker.touch(self.root, "active")

        from evo.inject.drain import drain_session
        captured = io.StringIO()
        with patch("sys.stdout", captured):
            drain_session(
                self.root, "active",
                host="claude-code", hook_event="PreToolUse",
            )

        assert "real directive" in captured.getvalue()
        offset = queue.read_offset(self.root, "active", "workspace")
        assert offset == post_id


# ---------------------------------------------------------------------------
# SessionStart engages the orchestrator (hook hosts + hermes)
#
# Regression for the release-smoke failure: claude-code/codex/hermes
# delivered fanout=0 / skipped_unengaged=1 under /optimize because the
# orchestrator dispatches every `evo` command to subagents, so its own
# session never ran `evo` and `auto_register_from_env` never engaged it.
# SessionStart (the hook host) / on_session_start (hermes) IS the
# orchestrator's engagement signal and must flip has_evo_engaged true.
# ---------------------------------------------------------------------------

class TestSessionStartEngagesOrchestrator(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name).resolve()
        _make_workspace(self.root)

    def tearDown(self):
        self._tmp.cleanup()

    def test_register_session_engage_flag_marks_engaged(self):
        register_session(self.root, "orch", "claude-code", engage=True)
        rec = _read_record(self.root, "orch")
        assert rec["has_evo_engaged"] is True
        assert rec.get("engaged_at") is not None

    def test_register_session_engage_refused_for_subagent(self):
        # A subagent registration (exp_id set) must never engage even if
        # the engage flag is passed.
        register_session(self.root, "sub", "claude-code", exp_id="exp_0001", engage=True)
        rec = _read_record(self.root, "sub")
        assert rec["has_evo_engaged"] is False

    def test_drain_session_sessionstart_engages_orchestrator(self):
        # Rust binary registered at SessionStart (unengaged, e.g. a
        # published binary predating the engage fix), then handed off to
        # the Python drain on the SessionStart event. The Python drain
        # must engage the orchestrator.
        register_session(self.root, "orch2", "claude-code")
        assert _read_record(self.root, "orch2")["has_evo_engaged"] is False
        from evo.inject.drain import drain_session
        captured = io.StringIO()
        with patch("sys.stdout", captured):
            drain_session(
                self.root, "orch2",
                host="claude-code", hook_event="SessionStart",
                payload={"hook_event_name": "SessionStart", "session_id": "orch2"},
            )
        rec = _read_record(self.root, "orch2")
        assert rec["has_evo_engaged"] is True, (
            "SessionStart drain must engage the orchestrator so `evo direct` "
            "fanout reaches it"
        )

    def test_drain_session_sessionstart_does_not_engage_subagent(self):
        # A SessionStart-class event carrying agent_id originates from a
        # subagent — must not engage.
        register_session(self.root, "orch3", "claude-code")
        from evo.inject.drain import drain_session
        captured = io.StringIO()
        with patch("sys.stdout", captured):
            drain_session(
                self.root, "orch3",
                host="claude-code", hook_event="SessionStart",
                payload={
                    "hook_event_name": "SessionStart",
                    "session_id": "orch3",
                    "agent_id": "subagent-xyz",
                },
            )
        assert _read_record(self.root, "orch3")["has_evo_engaged"] is False

    def test_engaged_orchestrator_receives_direct_fanout(self):
        # End-to-end: SessionStart engagement makes `evo direct` deliver.
        register_session(self.root, "orch4", "claude-code")
        from evo.inject.drain import drain_session
        with patch("sys.stdout", io.StringIO()):
            drain_session(
                self.root, "orch4",
                host="claude-code", hook_event="SessionStart",
                payload={"hook_event_name": "SessionStart", "session_id": "orch4"},
            )
        old_cwd = Path.cwd()
        os.chdir(self.root)
        try:
            from evo.cli import cmd_direct
            buf = io.StringIO()
            with patch("sys.stdout", buf):
                cmd_direct(_build_args("mid-run directive"))
            out = buf.getvalue()
        finally:
            os.chdir(old_cwd)
        assert marker.exists(self.root, "orch4"), (
            "engaged orchestrator must receive the directive marker"
        )
        assert "fanout=1" in out
        assert "skipped_unengaged=0" in out


class TestHermesEngagesOnSessionStart(unittest.TestCase):
    """The hermes plugin process IS the orchestrator; on_session_start /
    pre_llm_call must engage it (hermes uses no Rust binary)."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name).resolve()
        _make_workspace(self.root)
        self._old_cwd = Path.cwd()
        os.chdir(self.root)

    def tearDown(self):
        os.chdir(self._old_cwd)
        self._tmp.cleanup()

    def test_on_session_start_marks_engaged(self):
        from evo.hermes_plugin import _on_session_start
        _on_session_start(session_id="hermes_sid")
        rec = _read_record(self.root, "hermes_sid")
        assert rec is not None, "on_session_start must register the hermes session"
        assert rec["has_evo_engaged"] is True, (
            "hermes orchestrator must be engaged at session start"
        )

    def test_engaged_hermes_session_receives_direct_fanout(self):
        from evo.hermes_plugin import _on_session_start
        _on_session_start(session_id="hermes_sid2")
        from evo.cli import cmd_direct
        buf = io.StringIO()
        with patch("sys.stdout", buf):
            cmd_direct(_build_args("hermes directive"))
        out = buf.getvalue()
        assert marker.exists(self.root, "hermes_sid2")
        assert "fanout=1" in out


class TestInheritedSessionIdNotMisclassified(unittest.TestCase):
    """claude-code/codex inherit the parent's session-id env var when
    spawning subagents. A subagent's auto_register_from_env then resolves
    to the ORCHESTRATOR's sid with EVO_EXP_ID set — register_session must
    NOT stamp that exp_id onto the engaged orchestrator record (which would
    make `evo direct` skip it as skipped_subagent)."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name).resolve()
        _make_workspace(self.root)

    def tearDown(self):
        self._tmp.cleanup()

    def test_exp_id_not_stamped_onto_engaged_orchestrator(self):
        # Orchestrator registered + engaged at SessionStart.
        register_session(self.root, "orch_sid", "claude-code", engage=True)
        rec = _read_record(self.root, "orch_sid")
        assert rec["has_evo_engaged"] is True
        # Subagent dispatch leaks EVO_EXP_ID; subagent runs `evo` and
        # auto_register_from_env resolves to the inherited orchestrator sid.
        register_session(self.root, "orch_sid", "claude-code", exp_id="exp_0007")
        rec = _read_record(self.root, "orch_sid")
        assert rec["exp_id"] is None, (
            "subagent exp_id must not be stamped onto the engaged "
            "orchestrator record"
        )
        # And the orchestrator still receives direct fanout.
        old_cwd = Path.cwd()
        os.chdir(self.root)
        try:
            from evo.cli import cmd_direct
            buf = io.StringIO()
            with patch("sys.stdout", buf):
                cmd_direct(_build_args("directive"))
            out = buf.getvalue()
        finally:
            os.chdir(old_cwd)
        assert marker.exists(self.root, "orch_sid")
        assert "fanout=1" in out
        assert "skipped_subagent=0" in out

    def test_exp_id_still_merges_onto_unengaged_subagent_first_record(self):
        # Genuine subagent-first registration: Rust registered at the
        # subagent's SessionStart with exp_id=null (pre-fix binary) and
        # unengaged; the later Python merge must still stamp exp_id.
        register_session(self.root, "sub_sid", "claude-code")  # unengaged
        register_session(self.root, "sub_sid", "claude-code", exp_id="exp_0123")
        rec = _read_record(self.root, "sub_sid")
        assert rec["exp_id"] == "exp_0123"


if __name__ == "__main__":
    unittest.main()

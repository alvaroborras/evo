"""Unit tests for evo inject subsystem.

Covers paths, registry, queue, marker, drain, and cmd_direct.
All tests use temporary directories — no shared state.

Run: pytest tests/unit/test_inject.py -v
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "plugins" / "evo" / "src"))

from evo.inject import marker, queue
from evo.inject.drain import (
    drain_session,
    emit_for_host,
    format_directive_text,
)
from evo.inject.paths import (
    ensure_dirs,
    events_dir,
    inject_root,
    marker_file,
    markers_dir,
    offset_file,
    offsets_dir,
    session_file,
    sessions_dir,
    workspace_events_path,
    exp_events_path,
)
from evo.inject.registry import (
    auto_register_from_env,
    detect_session,
    is_registered,
    list_active_sessions,
    register_session,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _init_git_repo(root: Path) -> None:
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.email", "t@evo"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.name", "T"], cwd=root, check=True)
    subprocess.run(["git", "config", "commit.gpgsign", "false"], cwd=root, check=True)
    (root / "README.md").write_text("x\n")
    subprocess.run(["git", "add", "."], cwd=root, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=root, check=True)


def _make_workspace(root: Path) -> None:
    """Set up a minimal evo workspace (git repo + .evo/run_0000/)."""
    _init_git_repo(root)
    from evo.core import init_workspace
    init_workspace(
        root,
        target="agent.py",
        benchmark="python bench.py",
        metric="max",
        gate=None,
    )


def _build_args(*args: str) -> argparse.Namespace:
    """Build a Namespace that looks like `evo direct` parsed args."""
    return argparse.Namespace(args=list(args))


# ---------------------------------------------------------------------------
# paths.py
# ---------------------------------------------------------------------------

class TestPaths(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name).resolve()
        _make_workspace(self.root)

    def tearDown(self):
        self._tmp.cleanup()

    def test_inject_root_is_under_run_dir(self):
        r = inject_root(self.root)
        assert ".evo" in str(r), r

    def test_sessions_dir_under_inject_root(self):
        assert sessions_dir(self.root) == inject_root(self.root) / "sessions"

    def test_events_dir_under_inject_root(self):
        assert events_dir(self.root) == inject_root(self.root) / "events"

    def test_offsets_dir_under_inject_root(self):
        assert offsets_dir(self.root) == inject_root(self.root) / "offsets"

    def test_markers_dir_under_inject_root(self):
        assert markers_dir(self.root) == inject_root(self.root) / "markers"

    def test_session_file_path(self):
        p = session_file(self.root, "abc123")
        assert p.name == "abc123.json"
        assert p.parent == sessions_dir(self.root)

    def test_offset_file_path(self):
        p = offset_file(self.root, "abc123")
        assert p.name == "abc123.json"
        assert p.parent == offsets_dir(self.root)

    def test_marker_file_path(self):
        p = marker_file(self.root, "abc123")
        assert p.name == "abc123.flag"
        assert p.parent == markers_dir(self.root)

    def test_workspace_events_path(self):
        p = workspace_events_path(self.root)
        assert p.name == "workspace.jsonl"

    def test_exp_events_path(self):
        p = exp_events_path(self.root, "exp_0001")
        assert p.name == "exp_0001.jsonl"

    def test_ensure_dirs_creates_all_subdirs(self):
        ensure_dirs(self.root)
        for d in (
            sessions_dir(self.root),
            events_dir(self.root),
            offsets_dir(self.root),
            markers_dir(self.root),
        ):
            assert d.is_dir(), f"missing: {d}"

    def test_ensure_dirs_is_idempotent(self):
        ensure_dirs(self.root)
        ensure_dirs(self.root)  # second call must not raise
        assert sessions_dir(self.root).is_dir()


# ---------------------------------------------------------------------------
# registry.py
# ---------------------------------------------------------------------------

class TestRegistry(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name).resolve()
        _make_workspace(self.root)

    def tearDown(self):
        self._tmp.cleanup()

    def test_register_session_creates_file(self):
        register_session(self.root, "sid1", "claude-code")
        assert session_file(self.root, "sid1").exists()

    def test_register_session_idempotent_same_data(self):
        register_session(self.root, "sid1", "claude-code")
        first = json.loads(session_file(self.root, "sid1").read_text())
        register_session(self.root, "sid1", "claude-code")
        second = json.loads(session_file(self.root, "sid1").read_text())
        # registered_at must not change on re-registration
        assert first["registered_at"] == second["registered_at"]

    def test_register_session_updates_last_seen_at(self):
        register_session(self.root, "sid1", "claude-code")
        first = json.loads(session_file(self.root, "sid1").read_text())
        time.sleep(1.1)  # ensure ISO second boundary
        register_session(self.root, "sid1", "claude-code")
        second = json.loads(session_file(self.root, "sid1").read_text())
        assert second["last_seen_at"] >= first["last_seen_at"]

    def test_register_session_stores_exp_id(self):
        register_session(self.root, "sid2", "codex", exp_id="exp_0001")
        data = json.loads(session_file(self.root, "sid2").read_text())
        assert data["exp_id"] == "exp_0001"

    def test_register_session_stores_parent_session_id(self):
        register_session(self.root, "sid3", "claude-code", parent_session_id="parent1")
        data = json.loads(session_file(self.root, "sid3").read_text())
        assert data["parent_session_id"] == "parent1"

    def test_is_registered_true_after_registration(self):
        register_session(self.root, "sid4", "claude-code")
        assert is_registered(self.root, "sid4")

    def test_is_registered_false_before_registration(self):
        assert not is_registered(self.root, "never_registered")

    def test_list_active_sessions_returns_registered(self):
        register_session(self.root, "sid5", "claude-code")
        sessions = list_active_sessions(self.root)
        sids = [s["session_id"] for s in sessions]
        assert "sid5" in sids

    def test_list_active_sessions_gcs_stale_entry(self):
        register_session(self.root, "stale", "claude-code")
        # Manually backdate last_seen_at to force staleness
        path = session_file(self.root, "stale")
        data = json.loads(path.read_text())
        data["last_seen_at"] = "2000-01-01T00:00:00+00:00"
        path.write_text(json.dumps(data))

        sessions = list_active_sessions(self.root)
        sids = [s["session_id"] for s in sessions]
        assert "stale" not in sids
        assert not path.exists(), "stale session file should have been deleted"

    def test_list_active_sessions_gcs_stale_offset_file(self):
        register_session(self.root, "stale2", "claude-code")
        queue.write_offset(self.root, "stale2", workspace_id="dummy_id")
        assert offset_file(self.root, "stale2").exists()

        path = session_file(self.root, "stale2")
        data = json.loads(path.read_text())
        data["last_seen_at"] = "2000-01-01T00:00:00+00:00"
        path.write_text(json.dumps(data))

        list_active_sessions(self.root)
        assert not offset_file(self.root, "stale2").exists(), \
            "offset file should be GC'd together with stale session"

    def test_detect_session_returns_none_when_no_env(self):
        env_vars = [v for _, v in [
            ("claude-code", "CLAUDE_CODE_SESSION_ID"),
            ("codex", "CODEX_THREAD_ID"),
            ("hermes", "HERMES_SESSION_ID"),
            ("opencode", "OPENCODE_SESSION_ID"),
        ]]
        cleaned = {v: None for v in env_vars}
        with patch.dict(os.environ, {k: "" for k in env_vars}, clear=False):
            # Remove the vars entirely
            for v in env_vars:
                os.environ.pop(v, None)
            result = detect_session()
        assert result is None

    def test_detect_session_returns_claude_code(self):
        with patch.dict(os.environ, {"CLAUDE_CODE_SESSION_ID": "sess_abc"}, clear=False):
            # remove competing vars
            for _, v in [("codex", "CODEX_THREAD_ID"), ("hermes", "HERMES_SESSION_ID"), ("opencode", "OPENCODE_SESSION_ID")]:
                os.environ.pop(v, None)
            result = detect_session()
        assert result == ("claude-code", "sess_abc")

    def test_detect_session_returns_codex(self):
        with patch.dict(os.environ, {"CODEX_THREAD_ID": "codex_thread"}, clear=False):
            os.environ.pop("CLAUDE_CODE_SESSION_ID", None)
            os.environ.pop("HERMES_SESSION_ID", None)
            os.environ.pop("OPENCODE_SESSION_ID", None)
            result = detect_session()
        assert result == ("codex", "codex_thread")

    def test_auto_register_from_env_registers_when_session_set(self):
        with patch.dict(os.environ, {"CLAUDE_CODE_SESSION_ID": "env_sess"}, clear=False):
            for _, v in [("codex", "CODEX_THREAD_ID"), ("hermes", "HERMES_SESSION_ID"), ("opencode", "OPENCODE_SESSION_ID")]:
                os.environ.pop(v, None)
            os.environ.pop("EVO_EXP_ID", None)
            auto_register_from_env(self.root)
        assert is_registered(self.root, "env_sess")

    def test_auto_register_from_env_stores_exp_id(self):
        with patch.dict(os.environ, {
            "CLAUDE_CODE_SESSION_ID": "env_sess2",
            "EVO_EXP_ID": "exp_0007",
        }, clear=False):
            for _, v in [("codex", "CODEX_THREAD_ID"), ("hermes", "HERMES_SESSION_ID"), ("opencode", "OPENCODE_SESSION_ID")]:
                os.environ.pop(v, None)
            auto_register_from_env(self.root)
        data = json.loads(session_file(self.root, "env_sess2").read_text())
        assert data["exp_id"] == "exp_0007"

    def test_auto_register_noop_when_no_session_env(self):
        for _, v in [
            ("claude-code", "CLAUDE_CODE_SESSION_ID"),
            ("codex", "CODEX_THREAD_ID"),
            ("hermes", "HERMES_SESSION_ID"),
            ("opencode", "OPENCODE_SESSION_ID"),
        ]:
            os.environ.pop(v, None)
        auto_register_from_env(self.root)
        # No session files should exist
        ensure_dirs(self.root)
        assert list(sessions_dir(self.root).iterdir()) == []


# ---------------------------------------------------------------------------
# queue.py
# ---------------------------------------------------------------------------

class TestQueue(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name).resolve()
        _make_workspace(self.root)

    def tearDown(self):
        self._tmp.cleanup()

    def test_append_workspace_event_creates_file(self):
        queue.append_workspace_event(self.root, "hello")
        assert workspace_events_path(self.root).exists()

    def test_append_workspace_event_returns_id(self):
        ev_id = queue.append_workspace_event(self.root, "hello")
        assert isinstance(ev_id, str)
        assert len(ev_id) > 0

    def test_append_exp_event_creates_exp_file(self):
        queue.append_exp_event(self.root, "exp_0001", "hi")
        assert exp_events_path(self.root, "exp_0001").exists()

    def test_append_ids_encode_monotonically_increasing_timestamps(self):
        # _ulid() encodes: ts_bytes(6) + random(10) as fixed-width hex.
        # Hex is sort-preserving in pure ASCII (unlike base32 with A-Z2-7,
        # where '2' (ASCII 50) < 'A' (ASCII 65) breaks lexicographic
        # ordering across the alphabet boundary). Fixed-width hex means
        # lexicographic string comparison matches the underlying byte
        # value, so consecutive ids written across millisecond boundaries
        # sort in creation order.
        import time as _time

        ids = []
        for i in range(3):
            ids.append(queue.append_workspace_event(self.root, f"msg{i}"))
            _time.sleep(0.01)
        # Both: timestamp prefix is non-decreasing AND lexicographic order
        # of the full id matches creation order.
        for a, b in zip(ids, ids[1:]):
            assert a <= b, f"id regressed lexicographically: {a} > {b}"
        timestamps = [int(uid[:12], 16) for uid in ids]
        for a, b in zip(timestamps, timestamps[1:]):
            assert a <= b, f"timestamp regressed: {a} > {b}"

    def test_read_events_after_none_returns_all(self):
        queue.append_workspace_event(self.root, "msg1")
        queue.append_workspace_event(self.root, "msg2")
        events = queue.read_events_after(workspace_events_path(self.root), None)
        assert len(events) == 2
        assert events[0]["text"] == "msg1"
        assert events[1]["text"] == "msg2"

    def test_read_events_after_specific_id_returns_newer_only(self):
        # Sleep across a millisecond boundary so id2 > id1 lexicographically.
        # Within the same millisecond the random suffix can invert order with
        # extremely small probability (1 in 2^80 collision on the random tail).
        import time as _time
        id1 = queue.append_workspace_event(self.root, "msg1")
        _time.sleep(0.01)
        queue.append_workspace_event(self.root, "msg2")
        events = queue.read_events_after(workspace_events_path(self.root), id1)
        assert len(events) == 1
        assert events[0]["text"] == "msg2"

    def test_read_events_after_missing_file_returns_empty(self):
        path = workspace_events_path(self.root)
        assert not path.exists()
        events = queue.read_events_after(path, None)
        assert events == []

    def test_read_events_after_tolerates_trailing_partial_line(self):
        # Write a valid line + a partial line (no trailing newline, invalid JSON)
        path = workspace_events_path(self.root)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            '{"schema_version":1,"id":"AAAAAA","ts":"2026-01-01T00:00:00+00:00","text":"ok"}\n'
            '{"partial":tru'  # truncated — no trailing newline
        )
        events = queue.read_events_after(path, None)
        assert len(events) == 1
        assert events[0]["text"] == "ok"

    def test_write_and_read_offset_workspace(self):
        queue.write_offset(self.root, "s1", workspace_id="AAABBB")
        result = queue.read_offset(self.root, "s1", "workspace")
        assert result == "AAABBB"

    def test_write_and_read_offset_exp(self):
        queue.write_offset(self.root, "s2", exp_id="CCCCC")
        result = queue.read_offset(self.root, "s2", "exp")
        assert result == "CCCCC"

    def test_read_offset_returns_none_when_missing(self):
        result = queue.read_offset(self.root, "nobody", "workspace")
        assert result is None

    def test_write_offset_preserves_other_queue(self):
        queue.write_offset(self.root, "s3", workspace_id="WS1")
        queue.write_offset(self.root, "s3", exp_id="EXP1")
        assert queue.read_offset(self.root, "s3", "workspace") == "WS1"
        assert queue.read_offset(self.root, "s3", "exp") == "EXP1"

    def test_init_offset_to_latest_sets_workspace_offset(self):
        id1 = queue.append_workspace_event(self.root, "before")
        queue.init_offset_to_latest(self.root, "s4")
        # append after init — should NOT be returned when reading from offset
        queue.append_workspace_event(self.root, "after")
        last = queue.read_offset(self.root, "s4", "workspace")
        assert last == id1

    def test_read_events_after_offset_skips_already_seen(self):
        import time as _time
        queue.append_workspace_event(self.root, "seen")
        queue.init_offset_to_latest(self.root, "s5")
        _time.sleep(0.01)  # cross millisecond boundary so new event's id > offset
        queue.append_workspace_event(self.root, "new")
        last_id = queue.read_offset(self.root, "s5", "workspace")
        events = queue.read_events_after(workspace_events_path(self.root), last_id)
        assert len(events) == 1
        assert events[0]["text"] == "new"


# ---------------------------------------------------------------------------
# marker.py
# ---------------------------------------------------------------------------

class TestMarker(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name).resolve()
        _make_workspace(self.root)

    def tearDown(self):
        self._tmp.cleanup()

    def test_touch_creates_marker(self):
        marker.touch(self.root, "sid")
        assert marker_file(self.root, "sid").exists()

    def test_exists_true_after_touch(self):
        marker.touch(self.root, "sid")
        assert marker.exists(self.root, "sid")

    def test_exists_false_before_touch(self):
        assert not marker.exists(self.root, "nosid")

    def test_unlink_removes_marker(self):
        marker.touch(self.root, "sid")
        marker.unlink(self.root, "sid")
        assert not marker.exists(self.root, "sid")

    def test_unlink_idempotent_when_absent(self):
        # Must not raise if file doesn't exist
        marker.unlink(self.root, "nosid")

    def test_touch_idempotent(self):
        marker.touch(self.root, "sid")
        marker.touch(self.root, "sid")  # must not raise
        assert marker.exists(self.root, "sid")


# ---------------------------------------------------------------------------
# drain.py
# ---------------------------------------------------------------------------

class TestFormatDirectiveText(unittest.TestCase):

    def test_single_event(self):
        events = [{"text": "do the thing"}]
        out = format_directive_text(events)
        assert out == "[EVO DIRECTIVE]\ndo the thing\n[END EVO DIRECTIVE]"

    def test_multiple_events_joined_by_newline(self):
        events = [{"text": "first"}, {"text": "second"}]
        out = format_directive_text(events)
        assert out == (
            "[EVO DIRECTIVE]\nfirst\n[END EVO DIRECTIVE]\n"
            "[EVO DIRECTIVE]\nsecond\n[END EVO DIRECTIVE]"
        )

    def test_empty_events_returns_empty_string(self):
        assert format_directive_text([]) == ""

    def test_event_with_empty_text_skipped(self):
        events = [{"text": ""}, {"text": "real"}]
        out = format_directive_text(events)
        assert out == "[EVO DIRECTIVE]\nreal\n[END EVO DIRECTIVE]"

    def test_event_missing_text_key_skipped(self):
        events = [{"id": "abc"}, {"text": "hello"}]
        out = format_directive_text(events)
        assert out == "[EVO DIRECTIVE]\nhello\n[END EVO DIRECTIVE]"


class TestEmitForHost(unittest.TestCase):

    def _capture(self, host, hook_event, text):
        import io
        buf = io.StringIO()
        with patch("evo.inject.drain.sys.stdout", buf):
            emit_for_host(host, hook_event, text)
        return buf.getvalue()

    def test_claude_code_produces_hook_specific_output(self):
        out = self._capture("claude-code", "PreToolUse", "hello")
        data = json.loads(out)
        assert "hookSpecificOutput" in data
        assert data["hookSpecificOutput"]["additionalContext"] == "hello"

    def test_claude_code_defaults_hook_event_to_pretooluse(self):
        out = self._capture("claude-code", None, "hello")
        data = json.loads(out)
        assert data["hookSpecificOutput"]["hookEventName"] == "PreToolUse"

    def test_codex_same_envelope_as_claude_code(self):
        out_cc = self._capture("claude-code", "SessionStart", "x")
        out_cx = self._capture("codex", "SessionStart", "x")
        assert json.loads(out_cc) == json.loads(out_cx)

    def test_hermes_produces_context_key(self):
        out = self._capture("hermes", None, "ctx text")
        data = json.loads(out)
        assert data == {"context": "ctx text"}

    def test_empty_text_produces_empty_object(self):
        out = self._capture("claude-code", None, "")
        assert json.loads(out) == {}


class TestDrainSession(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name).resolve()
        _make_workspace(self.root)

    def tearDown(self):
        self._tmp.cleanup()

    def _drain_capturing_stdout(self, session_id, host="claude-code", hook_event=None):
        import io
        buf = io.StringIO()
        with patch("evo.inject.drain.sys.stdout", buf):
            rc = drain_session(self.root, session_id, host=host, hook_event=hook_event)
        return rc, buf.getvalue()

    def test_drain_unregistered_session_returns_empty_obj(self):
        rc, out = self._drain_capturing_stdout("ghost")
        assert rc == 0
        assert json.loads(out) == {}

    def test_drain_orchestrator_emits_workspace_events(self):
        register_session(self.root, "orch", "claude-code")
        queue.append_workspace_event(self.root, "be bold")
        marker.touch(self.root, "orch")

        rc, out = self._drain_capturing_stdout("orch")
        assert rc == 0
        data = json.loads(out)
        assert "hookSpecificOutput" in data
        ctx = data["hookSpecificOutput"]["additionalContext"]
        assert "be bold" in ctx

    def test_drain_updates_offset_after_emit(self):
        register_session(self.root, "orch2", "claude-code")
        ev_id = queue.append_workspace_event(self.root, "msg")
        marker.touch(self.root, "orch2")

        self._drain_capturing_stdout("orch2")
        last = queue.read_offset(self.root, "orch2", "workspace")
        assert last == ev_id

    def test_drain_unlinks_marker_after_emit(self):
        register_session(self.root, "orch3", "claude-code")
        queue.append_workspace_event(self.root, "msg")
        marker.touch(self.root, "orch3")

        self._drain_capturing_stdout("orch3")
        assert not marker.exists(self.root, "orch3")

    def test_drain_subagent_uses_exp_queue_not_workspace(self):
        register_session(self.root, "sub1", "claude-code", exp_id="exp_0001")
        # workspace event should NOT appear for subagent
        queue.append_workspace_event(self.root, "orchestrator only")
        # exp event should appear
        queue.append_exp_event(self.root, "exp_0001", "subagent msg")
        marker.touch(self.root, "sub1")

        rc, out = self._drain_capturing_stdout("sub1")
        data = json.loads(out)
        ctx = data["hookSpecificOutput"]["additionalContext"]
        assert "subagent msg" in ctx
        assert "orchestrator only" not in ctx

    def test_drain_does_not_redeliver_already_seen_events(self):
        import time as _time
        register_session(self.root, "orch4", "claude-code")
        queue.append_workspace_event(self.root, "first")
        marker.touch(self.root, "orch4")
        self._drain_capturing_stdout("orch4")  # consume first

        _time.sleep(0.01)  # cross millisecond boundary so new id > consumed id
        queue.append_workspace_event(self.root, "second")
        marker.touch(self.root, "orch4")
        rc, out = self._drain_capturing_stdout("orch4")
        data = json.loads(out)
        ctx = data["hookSpecificOutput"]["additionalContext"]
        assert "second" in ctx
        assert "first" not in ctx


# ---------------------------------------------------------------------------
# cli.py cmd_direct
# ---------------------------------------------------------------------------

class TestCmdDirect(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name).resolve()
        _make_workspace(self.root)
        self._old_cwd = Path.cwd()
        os.chdir(self.root)

    def tearDown(self):
        os.chdir(self._old_cwd)
        self._tmp.cleanup()

    def _cmd_direct(self, *parts: str) -> int:
        from evo.cli import cmd_direct
        return cmd_direct(_build_args(*parts))

    def test_broadcast_creates_workspace_event(self):
        register_session(self.root, "orch", "claude-code")
        rc = self._cmd_direct("be ambitious")
        assert rc == 0
        events = queue.read_events_after(workspace_events_path(self.root), None)
        assert len(events) == 1
        assert events[0]["text"] == "be ambitious"

    def test_broadcast_touches_orchestrator_markers(self):
        register_session(self.root, "orch", "claude-code")
        # subagent must be skipped for broadcast
        register_session(self.root, "sub", "claude-code", exp_id="exp_0001")
        self._cmd_direct("broadcast message")
        assert marker.exists(self.root, "orch"), "orchestrator marker must be set"
        assert not marker.exists(self.root, "sub"), "subagent marker must not be set"

    def test_broadcast_fanout_skips_subagent_sessions(self):
        register_session(self.root, "orch", "claude-code")
        register_session(self.root, "sub", "claude-code", exp_id="exp_0002")

        import io
        buf = io.StringIO()
        with patch("sys.stdout", buf):
            self._cmd_direct("msg")
        # fanout=1 (only orchestrator)
        assert "fanout=1" in buf.getvalue()

    def test_targeted_form_writes_exp_event(self):
        rc = self._cmd_direct("exp_0001", "targeted msg")
        assert rc == 0
        events = queue.read_events_after(exp_events_path(self.root, "exp_0001"), None)
        assert len(events) == 1
        assert events[0]["text"] == "targeted msg"

    def test_targeted_form_touches_exp_marker(self):
        self._cmd_direct("exp_0001", "msg")
        assert marker.exists(self.root, "exp_0001")

    def test_targeted_form_does_not_write_workspace_event(self):
        self._cmd_direct("exp_0001", "msg")
        events = queue.read_events_after(workspace_events_path(self.root), None)
        assert events == []

    @unittest.skipIf(
        sys.platform == "win32",
        "POSIX-only: TemporaryDirectory cleanup recurses while a git repo "
        "still holds file handles on Windows (shutil.rmtree onerror loop)",
    )
    def test_error_when_no_evo_dir(self):
        # chdir to a tmpdir without .evo
        with tempfile.TemporaryDirectory() as other:
            other_root = Path(other).resolve()
            _init_git_repo(other_root)
            os.chdir(other_root)
            from evo.cli import cmd_direct
            import io
            buf = io.StringIO()
            with patch("sys.stderr", buf):
                rc = cmd_direct(_build_args("hello"))
            assert rc == 2
            assert "not in an evo workspace" in buf.getvalue()
        os.chdir(self.root)

    def test_error_when_no_args(self):
        from evo.cli import cmd_direct
        import io
        buf = io.StringIO()
        with patch("sys.stderr", buf):
            rc = cmd_direct(argparse.Namespace(args=[]))
        assert rc == 2

    def test_multi_word_text_joined(self):
        self._cmd_direct("do", "this", "thing")
        events = queue.read_events_after(workspace_events_path(self.root), None)
        assert events[0]["text"] == "do this thing"


if __name__ == "__main__":
    for name, obj in list(globals().items()):
        if isinstance(obj, type) and issubclass(obj, unittest.TestCase):
            suite = unittest.TestLoader().loadTestsFromTestCase(obj)
            runner = unittest.TextTestRunner(verbosity=2)
            runner.run(suite)

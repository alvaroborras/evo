from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

import pytest

from evo import telemetry
from evo import cli


@pytest.fixture(autouse=True)
def isolated_telemetry_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "evo-home"
    monkeypatch.setenv("EVO_HOME", str(home))
    monkeypatch.delenv("EVO_TELEMETRY", raising=False)
    monkeypatch.delenv("DO_NOT_TRACK", raising=False)
    monkeypatch.delenv("CI", raising=False)
    return home


def test_default_enabled_and_install_id_lifecycle() -> None:
    assert telemetry.telemetry_enabled() is True
    assert telemetry.status()["enabled"] is True
    assert telemetry.status()["install_id"] is None

    first = telemetry.install_id()
    assert first
    assert telemetry.status()["install_id"] == first

    second = telemetry.reset_install_id()
    assert second and second != first
    assert telemetry.install_id(create=False) == second

    telemetry.set_enabled(False)
    assert telemetry.telemetry_enabled() is False
    assert telemetry.status()["source"] == "user"

    telemetry.set_enabled(True)
    assert telemetry.telemetry_enabled() is True
    assert telemetry.status()["source"] == "user"


def test_env_and_do_not_track_disable_without_changing_settings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    telemetry.set_enabled(True)

    monkeypatch.setenv("EVO_TELEMETRY", "0")
    assert telemetry.telemetry_enabled() is False
    assert telemetry.status()["source"] == "env"

    monkeypatch.setenv("EVO_TELEMETRY", "1")
    monkeypatch.setenv("DO_NOT_TRACK", "1")
    assert telemetry.telemetry_enabled() is False
    assert telemetry.status()["source"] == "env"


def test_ci_disables_by_default_but_env_can_force_on(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CI", "true")
    assert telemetry.telemetry_enabled() is False
    assert telemetry.status()["source"] == "ci"

    monkeypatch.setenv("EVO_TELEMETRY", "on")
    assert telemetry.telemetry_enabled() is True


def test_disable_with_final_event_sends_before_persisting_off(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[tuple[str, dict, bool, float | None]] = []

    def fake_capture(
        event: str,
        properties: dict | None = None,
        *,
        flush: bool = False,
        timeout: float | None = None,
    ) -> None:
        events.append((event, properties or {}, flush, timeout))

    monkeypatch.setattr(telemetry, "capture", fake_capture)

    assert telemetry.disable_with_final_event() is True
    assert events == [
        ("telemetry_disabled", {"disabled_method": "command"}, True, 0.5)
    ]
    assert telemetry.telemetry_enabled() is False


def test_disable_with_final_event_respects_env_off(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    telemetry.set_enabled(True)
    monkeypatch.setenv("EVO_TELEMETRY", "false")
    monkeypatch.setattr(
        telemetry,
        "capture",
        lambda *args, **kwargs: pytest.fail("env-disabled telemetry must not send"),
    )

    assert telemetry.disable_with_final_event() is False
    assert telemetry.load_settings()["enabled"] is False


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Linux", "linux"),
        ("Windows", "windows"),
        ("Windows_NT", "windows"),
        ("Darwin", "darwin"),
        ("FreeBSD", "freebsd"),
        ("Plan9", "unknown"),
    ],
)
def test_normalize_os_name(raw: str, expected: str) -> None:
    assert telemetry.normalize_os_name(raw) == expected


def test_base_properties_use_minimal_identifier_set() -> None:
    props = telemetry.base_properties()

    assert props["install_id"]
    assert props["evo_version"]
    assert props["os"]
    assert "session_id" not in props
    assert "python" not in props


def test_capture_background_uses_daemon_thread_without_sync_send(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    started: list[dict] = []

    class FakeThread:
        def __init__(self, *, target, args, daemon):  # noqa: ANN001
            started.append({"target": target, "args": args, "daemon": daemon})

        def start(self) -> None:
            started.append({"started": True})

    monkeypatch.setattr(telemetry.threading, "Thread", FakeThread)

    telemetry.capture("workspace_initialized", {"outcome": "success"})

    assert started[0]["target"] is telemetry._send_event
    assert started[0]["daemon"] is True
    assert started[1] == {"started": True}


def test_capture_flush_swallows_network_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        telemetry,
        "remote_config",
        lambda timeout: {"enabled": True, "sample_rate": 1.0},
    )

    def boom(*_args, **_kwargs):  # noqa: ANN001
        raise RuntimeError("network unavailable")

    monkeypatch.setattr(telemetry.requests, "post", boom)

    telemetry.capture("workspace_initialized", {"outcome": "success"}, flush=True)


def test_usecase_sends_sanitized_description_and_tags(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: list[tuple[str, dict, bool, float | None]] = []

    def fake_capture(
        event: str,
        properties: dict | None = None,
        *,
        flush: bool = False,
        timeout: float | None = None,
    ) -> None:
        captured.append((event, properties or {}, flush, timeout))

    monkeypatch.setattr(telemetry, "capture", fake_capture)

    sent = telemetry.capture_usecase(
        "Optimizing a tool-calling agent\nfor pass rate",
        ["Coding Agent", "Retry Policy", "Coding Agent", "pass rate"],
    )

    assert sent is True
    event, props, flush, timeout = captured[0]
    assert event == "usecase"
    assert props["description"] == "Optimizing a tool-calling agent for pass rate"
    assert props["tags"] == ["coding-agent", "retry-policy", "pass-rate"]
    assert flush is True
    assert timeout == telemetry.FEEDBACK_TIMEOUT_SECONDS


def test_usecase_preserves_workspace_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: list[tuple[str, dict, bool, float | None]] = []

    def fake_capture(
        event: str,
        properties: dict | None = None,
        *,
        flush: bool = False,
        timeout: float | None = None,
    ) -> None:
        captured.append((event, properties or {}, flush, timeout))

    monkeypatch.setattr(telemetry, "capture", fake_capture)

    sent = telemetry.capture_usecase(
        "Optimizing a tool-calling agent for pass rate",
        ["coding-agent"],
        properties={
            "workspace_id": "workspace-1",
            "host": "opencode",
            "backend": "remote",
            "provider": "modal",
        },
    )

    assert sent is True
    props = captured[0][1]
    assert props["workspace_id"] == "workspace-1"
    assert props["host"] == "opencode"
    assert props["backend"] == "remote"
    assert props["provider"] == "modal"


def test_scrub_text_redacts_common_tokens() -> None:
    text = telemetry.scrub_text(
        "key=sk-abcdefghijklmnopqrstuvwxyz123456 "
        "posthog=phx_abcdefghijklmnopqrstuvwxyz1234567890 "
        "sha=0123456789abcdef0123456789abcdef01234567",
        500,
    )

    assert "sk-" not in text
    assert "phx_" not in text
    assert "0123456789abcdef" not in text
    assert "<secret>" in text
    assert "<token>" in text


def test_feedback_is_noop_when_user_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    telemetry.set_enabled(False)
    monkeypatch.setattr(
        telemetry,
        "capture",
        lambda *args, **kwargs: pytest.fail("disabled feedback must not send"),
    )

    assert (
        telemetry.capture_feedback(
            kind="bug",
            phase="run",
            summary="summary",
            expected="expected",
            actual="actual",
            repro="repro",
            tags=["agent-orchestration"],
        )
        is False
    )


def test_feedback_sends_minimal_safe_fields(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: list[tuple[str, dict, bool, float | None]] = []

    def fake_capture(
        event: str,
        properties: dict | None = None,
        *,
        flush: bool = False,
        timeout: float | None = None,
    ) -> None:
        captured.append((event, properties or {}, flush, timeout))

    monkeypatch.setattr(telemetry, "capture", fake_capture)

    sent = telemetry.capture_feedback(
        kind="bug",
        phase="run",
        summary="benchmark crashed on retry",
        expected="retry should recover",
        actual="run failed",
        repro="start remote run and interrupt once",
        tags=["remote backend", "Retry"],
        properties={"workspace_id": "workspace-1", "host": "codex"},
    )

    assert sent is True
    event, props, flush, timeout = captured[0]
    assert event == "feedback"
    assert props["kind"] == "bug"
    assert props["phase"] == "run"
    assert props["summary"] == "benchmark crashed on retry"
    assert props["tags"] == ["remote-backend", "retry"]
    assert props["workspace_id"] == "workspace-1"
    assert props["host"] == "codex"
    assert "feedback_kind" not in props
    assert "feedback_phase" not in props
    assert flush is True
    assert timeout == telemetry.FEEDBACK_TIMEOUT_SECONDS


def test_cli_command_tracking_flushes_with_short_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: list[tuple[str, dict, bool, float | None]] = []

    def fake_capture(
        event: str,
        properties: dict | None = None,
        *,
        flush: bool = False,
        timeout: float | None = None,
    ) -> None:
        captured.append((event, properties or {}, flush, timeout))

    monkeypatch.setattr(telemetry, "capture", fake_capture)
    monkeypatch.setattr(cli, "repo_root", lambda: (_ for _ in ()).throw(RuntimeError()))

    args = argparse.Namespace(
        command="init",
        host="codex",
        metric="max",
        instrumentation_mode="sdk",
    )

    cli._telemetry_track_command(args, 0, time.monotonic())

    assert len(captured) == 1
    event, props, flush, timeout = captured[0]
    assert event == "workspace_initialized"
    assert props["host"] == "codex"
    assert props["metric"] == "max"
    assert props["backend"] == "worktree"
    assert "command" not in props
    assert "instrumentation_mode" not in props
    assert flush is True
    assert timeout == 0.2


def test_autonomous_command_tracks_optimize_start(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: list[tuple[str, dict, bool, float | None]] = []

    def fake_capture(
        event: str,
        properties: dict | None = None,
        *,
        flush: bool = False,
        timeout: float | None = None,
    ) -> None:
        captured.append((event, properties or {}, flush, timeout))

    monkeypatch.setattr(telemetry, "capture", fake_capture)
    monkeypatch.setattr(
        cli,
        "_telemetry_workspace_props",
        lambda _root, exp_id=None: {"workspace_id": "workspace-1", "host": "codex"},
    )
    monkeypatch.setattr(cli, "repo_root", lambda: Path("/repo"))

    args = argparse.Namespace(command="autonomous", state="on")

    cli._telemetry_track_command(args, 0, time.monotonic())

    event, props, flush, timeout = captured[0]
    assert event == "optimize_started"
    assert props["autonomous"] is True
    assert props["workspace_id"] == "workspace-1"
    assert props["host"] == "codex"
    assert "command" not in props
    assert "subcommand" not in props
    assert flush is True
    assert timeout == 0.2


def test_failed_autonomous_command_does_not_track_optimize_start(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: list[tuple[str, dict, bool, float | None]] = []
    monkeypatch.setattr(
        telemetry,
        "capture",
        lambda *args, **kwargs: captured.append((args[0], args[1], kwargs["flush"], kwargs["timeout"])),
    )
    monkeypatch.setattr(cli, "repo_root", lambda: (_ for _ in ()).throw(RuntimeError()))

    args = argparse.Namespace(command="autonomous", state="on")

    cli._telemetry_track_command(args, 2, time.monotonic())

    assert captured == []


def test_cli_usecase_attaches_workspace_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}
    recorded: dict[str, object] = {}

    def fake_capture_usecase(description: str, tags: list[str], *, properties=None):  # noqa: ANN001
        captured["description"] = description
        captured["tags"] = tags
        captured["properties"] = properties
        return True

    monkeypatch.setattr(telemetry, "capture_usecase", fake_capture_usecase)
    monkeypatch.setattr(cli, "repo_root", lambda: Path("/repo"))
    monkeypatch.setattr(
        cli,
        "_record_telemetry_usecase_tags",
        lambda root, tags: recorded.update({"root": root, "tags": tags}),
    )
    monkeypatch.setattr(
        cli,
        "_current_telemetry_workspace_props",
        lambda: {"workspace_id": "workspace-1", "host": "codex"},
    )

    rc = cli.cmd_telemetry(
        argparse.Namespace(
            telemetry_action="usecase",
            description="Improve task pass rate",
            tag=["coding-agent"],
        )
    )

    assert rc == 0
    assert captured["description"] == "Improve task pass rate"
    assert captured["tags"] == ["coding-agent"]
    assert captured["properties"] == {"workspace_id": "workspace-1", "host": "codex"}
    assert recorded == {"root": Path("/repo"), "tags": ["coding-agent"]}


def test_cli_usecase_does_not_create_context_when_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    telemetry.set_enabled(False)
    monkeypatch.setattr(
        cli,
        "_current_telemetry_workspace_props",
        lambda: pytest.fail("disabled telemetry must not inspect workspace"),
    )

    rc = cli.cmd_telemetry(
        argparse.Namespace(
            telemetry_action="usecase",
            description="Improve task pass rate",
            tag=["coding-agent"],
        )
    )

    assert rc == 0


def test_workspace_props_prefer_node_backend(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    run_dir = root / ".evo" / "run_0000"
    run_dir.mkdir(parents=True)
    (root / ".evo" / "meta.json").write_text(
        json.dumps({"active": "run_0000", "host": "opencode"}),
        encoding="utf-8",
    )
    (run_dir / "config.json").write_text(
        json.dumps({"execution_backend": "worktree", "metric": "max"}),
        encoding="utf-8",
    )
    (run_dir / "graph.json").write_text(
        json.dumps(
            {
                "nodes": {
                    "root": {"id": "root"},
                    "exp_0000": {
                        "id": "exp_0000",
                        "backend": "remote",
                        "backend_config": {"provider": "modal"},
                        "gates": [{"name": "score-floor"}],
                    },
                }
            }
        ),
        encoding="utf-8",
    )

    props = cli._telemetry_workspace_props(root, exp_id="exp_0000")

    assert props["workspace_id"]
    assert props["host"] == "opencode"
    assert props["backend"] == "remote"
    assert props["provider"] == "modal"
    assert "metric" not in props
    assert "experiment_count_bucket" not in props
    assert "gate_count" not in props


def test_record_telemetry_usecase_tags_persists_sanitized_tags(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    evo_meta = root / ".evo" / "meta.json"
    evo_meta.parent.mkdir(parents=True)
    evo_meta.write_text(json.dumps({"active": "run_0000"}), encoding="utf-8")

    cli._record_telemetry_usecase_tags(
        root,
        ["Coding Agent", "https://private.example/repo", "Coding Agent"],
    )

    meta = json.loads(evo_meta.read_text(encoding="utf-8"))
    assert meta["telemetry_usecase_tags"] == ["coding-agent"]


@pytest.mark.parametrize(
    ("metric", "score", "parent", "best", "delta_parent", "delta_best"),
    [
        ("max", 12.0, 10.0, 11.0, 2.0, 1.0),
        ("min", 8.0, 10.0, 9.0, 2.0, 1.0),
    ],
)
def test_experiment_result_telemetry_sends_directional_deltas(
    monkeypatch: pytest.MonkeyPatch,
    metric: str,
    score: float,
    parent: float,
    best: float,
    delta_parent: float,
    delta_best: float,
) -> None:
    captured: list[tuple[str, dict, bool, float | None]] = []

    monkeypatch.setattr(
        cli,
        "_telemetry_workspace_props",
        lambda _root, exp_id=None: {"workspace_id": "workspace-1", "host": "codex"},
    )
    monkeypatch.setattr(cli, "_telemetry_usecase_tags", lambda _root: ["coding-agent"])
    monkeypatch.setattr(
        telemetry,
        "capture",
        lambda event, properties=None, *, flush=False, timeout=None: captured.append(
            (event, properties or {}, flush, timeout)
        ),
    )

    cli._emit_experiment_result_telemetry(
        Path("/repo"),
        "exp_0001",
        outcome="committed",
        metric=metric,
        score=score,
        parent_score=parent,
        best_before_score=best,
    )

    event, props, flush, timeout = captured[0]
    assert event == "experiment_result"
    assert props["experiment_id"] == "exp_0001"
    assert props["workspace_id"] == "workspace-1"
    assert props["host"] == "codex"
    assert props["outcome"] == "committed"
    assert props["metric"] == metric
    assert props["delta_vs_parent"] == delta_parent
    assert props["delta_vs_best"] == delta_best
    assert props["pct_delta_vs_parent"] == 20.0
    assert props["usecase_tags"] == ["coding-agent"]
    assert "score" not in props
    assert "parent_score" not in props
    assert flush is True
    assert timeout == 0.2


def test_branch_closed_telemetry_sends_reason_category(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: list[tuple[str, dict, bool, float | None]] = []

    monkeypatch.setattr(
        cli,
        "_telemetry_workspace_props",
        lambda _root, exp_id=None: {"workspace_id": "workspace-1", "backend": "worktree"},
    )
    monkeypatch.setattr(
        telemetry,
        "capture",
        lambda event, properties=None, *, flush=False, timeout=None: captured.append(
            (event, properties or {}, flush, timeout)
        ),
    )

    cli._emit_branch_closed_telemetry(
        Path("/repo"),
        "exp_0002",
        {"id": "exp_0002", "status": "evaluated", "score": 0.42},
        close_type="prune",
        reason="gate failed after retry",
        prune_kind="invalid",
    )

    event, props, flush, timeout = captured[0]
    assert event == "branch_closed"
    assert props["experiment_id"] == "exp_0002"
    assert props["workspace_id"] == "workspace-1"
    assert props["backend"] == "worktree"
    assert props["close_type"] == "prune"
    assert props["status_before"] == "evaluated"
    assert props["had_score"] is True
    assert props["reason_type"] == "bad_result"
    assert props["prune_kind"] == "invalid"
    assert "reason" not in props
    assert flush is True
    assert timeout == 0.2


def test_install_prints_telemetry_notice_when_enabled(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from evo import host_install

    monkeypatch.setattr(host_install, "install", lambda _host, _args: 0)

    rc = cli.cmd_install(argparse.Namespace(host="codex"))

    assert rc == 0
    assert "evo telemetry off" in capsys.readouterr().out

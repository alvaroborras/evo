from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
import requests

from evo.sandbox_client import ProcessLogEntry, SandboxAgentClient, SandboxAgentError
from evo.cli import _remote_infra_error_for_log, _write_attempt_outcome
from evo.core import attempt_dir, attempt_outcome_path
from evo.workspace_executor import RemoteExecutor


@dataclass
class _FakeRequest:
    method: str
    url: str


class _FakeResponse:
    def __init__(
        self,
        *,
        status_code: int = 200,
        json_data: Any = None,
        text: str = "",
        content: bytes = b"",
        method: str = "GET",
        url: str = "http://sandbox",
    ) -> None:
        self.status_code = status_code
        self._json_data = json_data
        self.text = text
        self.content = content
        self.request = _FakeRequest(method, url)
        self.url = url

    def json(self) -> Any:
        return self._json_data


class _FakeSession:
    def __init__(self, responses: list[Any]) -> None:
        self.headers: dict[str, str] = {}
        self.responses = list(responses)
        self.calls: list[tuple[str, str, float | None]] = []

    def get(self, url: str, *, timeout: float | None = None, **_kwargs: Any) -> Any:
        self.calls.append(("GET", url, timeout))
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        response.request = _FakeRequest("GET", url)
        response.url = url
        return response

    def close(self) -> None:
        pass


class _FakeRemoteClient:
    base_url = "http://sandbox"
    bearer_token = "token"

    def __init__(
        self,
        *,
        statuses: list[Any],
        stdout_entries: list[bytes] | None = None,
        stderr_entries: list[bytes] | None = None,
        log_errors: dict[str, Exception] | None = None,
    ) -> None:
        self.statuses = list(statuses)
        self.stdout_entries = list(stdout_entries or [])
        self.stderr_entries = list(stderr_entries or [])
        self.log_errors = dict(log_errors or {})
        self.started: list[dict[str, Any]] = []
        self.closed = False

    def clone(self) -> "_FakeRemoteClient":
        return self

    def process_start(
        self,
        command: str,
        args: list[str] | None = None,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
    ) -> str:
        self.started.append({"command": command, "args": args, "cwd": cwd, "env": env})
        return "proc-1"

    def process_status(self, process_id: str) -> dict[str, Any]:
        assert process_id == "proc-1"
        item = self.statuses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    def process_logs(
        self,
        process_id: str,
        follow: bool = False,
        stream: str = "combined",
    ):
        assert process_id == "proc-1"
        assert follow is True
        if stream in self.log_errors:
            raise self.log_errors[stream]
        entries = self.stdout_entries if stream == "stdout" else self.stderr_entries
        for sequence, data in enumerate(entries, start=1):
            yield ProcessLogEntry(
                sequence=sequence,
                stream=stream,
                timestamp_ms=sequence,
                data=data,
            )

    def fs_entries(self, path: str) -> list[Any]:
        return []

    def close(self) -> None:
        self.closed = True


def test_sandbox_client_retries_transient_health_request(monkeypatch: pytest.MonkeyPatch) -> None:
    session = _FakeSession([
        requests.ConnectionError("network blip"),
        _FakeResponse(json_data={"status": "ok"}),
    ])
    monkeypatch.setattr(requests, "Session", lambda: session)
    monkeypatch.setattr("evo.sandbox_client.time.sleep", lambda _seconds: None)

    client = SandboxAgentClient("http://sandbox", bearer_token="token")

    assert client.health() == {"status": "ok"}
    assert len(session.calls) == 2


def test_sandbox_client_does_not_retry_unauthorized_health(monkeypatch: pytest.MonkeyPatch) -> None:
    session = _FakeSession([_FakeResponse(status_code=401, text="bad token")])
    monkeypatch.setattr(requests, "Session", lambda: session)

    client = SandboxAgentClient("http://sandbox", bearer_token="token")

    with pytest.raises(SandboxAgentError) as exc_info:
        client.health()
    assert exc_info.value.status == 401
    assert len(session.calls) == 1


def test_remote_stream_tolerates_transient_status_failures(tmp_path: Path) -> None:
    client = _FakeRemoteClient(
        statuses=[
            {"status": "running"},
            requests.ConnectionError("temporary status failure"),
            {"status": "running"},
            {"status": "exited", "exitCode": 0},
        ],
        stdout_entries=[b"started\n", b"finished\n"],
    )
    executor = RemoteExecutor(client)  # type: ignore[arg-type]

    result = executor.stream(
        ["python", "bench.py"],
        cwd="/workspace/repo",
        timeout=None,
        stdout_path=tmp_path / "stdout.log",
        stderr_path=tmp_path / "stderr.log",
    )

    assert result.exit_code == 0
    assert result.timed_out is False
    assert "started\nfinished\n" == (tmp_path / "stdout.log").read_text()
    assert "[remote stream disconnected]" not in result.stderr
    journal = json.loads((tmp_path / "stdout.log.remote.json").read_text())
    assert journal["state"] == "exited"
    assert journal["process_id"] == "proc-1"
    assert journal["exit_code"] == 0
    assert journal["command"] == ["python", "bench.py"]


def test_remote_stream_reports_log_follow_failure_even_when_status_exits(tmp_path: Path) -> None:
    client = _FakeRemoteClient(
        statuses=[
            {"status": "running"},
            {"status": "exited", "exitCode": 0},
        ],
        stdout_entries=[b"ok\n"],
        log_errors={"stderr": RuntimeError("stderr stream dropped")},
    )
    executor = RemoteExecutor(client)  # type: ignore[arg-type]

    result = executor.stream(
        ["python", "bench.py"],
        cwd="/workspace/repo",
        timeout=None,
        stdout_path=tmp_path / "stdout.log",
        stderr_path=tmp_path / "stderr.log",
    )

    assert result.exit_code == 0
    assert "[remote log follow failed]" in result.stderr
    assert "stderr stream dropped" in result.stderr
    journal = json.loads((tmp_path / "stdout.log.remote.json").read_text())
    assert journal["state"] == "exited"
    assert journal["log_follow_errors"] == [
        {"stream": "stderr", "error": "stderr stream dropped"}
    ]


def test_remote_stream_marks_journal_failed_infra_after_sustained_status_errors(
    tmp_path: Path,
) -> None:
    client = _FakeRemoteClient(
        statuses=[
            requests.ConnectionError("status failed once"),
            requests.ConnectionError("status failed twice"),
        ],
    )
    executor = RemoteExecutor(client)  # type: ignore[arg-type]
    executor.status_failure_limit = 2
    executor.status_failure_base_delay = 0.0

    result = executor.stream(
        ["python", "bench.py"],
        cwd="/workspace/repo",
        timeout=None,
        stdout_path=tmp_path / "stdout.log",
        stderr_path=tmp_path / "stderr.log",
    )

    assert result.exit_code == 1
    assert "[remote stream disconnected]" in result.stderr
    journal = json.loads((tmp_path / "stdout.log.remote.json").read_text())
    assert journal["state"] == "failed_infra"
    assert journal["process_id"] == "proc-1"
    assert journal["status_failures"] == 2
    assert journal["error"] == "status failed twice"
    assert _remote_infra_error_for_log(tmp_path / "stdout.log") == "status failed twice"


def test_attempt_outcome_embeds_remote_stream_journals(tmp_path: Path) -> None:
    a_dir = attempt_dir(tmp_path, "exp_0000", 1)
    a_dir.mkdir(parents=True)
    (a_dir / "benchmark.log.remote.json").write_text(
        json.dumps({
            "state": "failed_infra",
            "process_id": "proc-1",
            "error": "container disappeared",
        }),
        encoding="utf-8",
    )
    (a_dir / "broken.remote.json").write_text("{not json", encoding="utf-8")

    _write_attempt_outcome(
        tmp_path,
        "exp_0000",
        1,
        "failed",
        node={"id": "exp_0000", "parent": "root", "hypothesis": "try remote"},
        started_at="2026-01-01T00:00:00+00:00",
        error="benchmark_exit_1",
    )

    outcome = json.loads(attempt_outcome_path(tmp_path, "exp_0000", 1).read_text())
    assert outcome["remote_streams"] == [
        {
            "state": "failed_infra",
            "process_id": "proc-1",
            "error": "container disappeared",
            "journal_path": ".evo/experiments/exp_0000/attempts/001/benchmark.log.remote.json",
        }
    ]
    assert "attempt_state" not in outcome

"""HTTP client for rivet-dev/sandbox-agent.

Thin wrapper over the daemon's REST surface: process exec (one-shot and
long-running with log streaming), filesystem ops, health. Used by both
`RemoteSandboxBackend` (for evo's lifecycle steps) and the MCP server
(for routed agent tool calls).

Endpoint reference: https://github.com/rivet-dev/sandbox-agent/blob/main/docs/openapi.json
"""
from __future__ import annotations

import base64
import json
import time
from dataclasses import dataclass
from typing import Any, Iterator
from urllib.parse import urlencode

import requests


# Default timeouts. Generous; the orchestrator-side `evo run --timeout`
# bounds long-running benchmarks separately.
DEFAULT_REQUEST_TIMEOUT = 60.0       # most ops should be <1s; 60s = patience
LONG_REQUEST_TIMEOUT = 600.0         # process/run with embedded benchmark
SAFE_REQUEST_ATTEMPTS = 3
SAFE_REQUEST_BASE_DELAY = 0.25
SAFE_REQUEST_MAX_DELAY = 2.0


@dataclass
class ProcessRunResult:
    """Response shape for `POST /v1/processes/run`."""

    stdout: str
    stderr: str
    exit_code: int | None
    timed_out: bool
    duration_ms: int
    stdout_truncated: bool = False
    stderr_truncated: bool = False


@dataclass
class FsEntry:
    """One entry from `GET /v1/fs/entries`."""

    name: str
    path: str
    is_dir: bool
    size: int | None = None


@dataclass
class ProcessLogEntry:
    sequence: int
    stream: str
    timestamp_ms: int
    data: bytes


class SandboxAgentError(Exception):
    """Raised on non-2xx responses from sandbox-agent. Carries the
    HTTP status and response body for diagnostics."""

    def __init__(self, status: int, message: str, body: str = "") -> None:
        super().__init__(f"[{status}] {message}")
        self.status = status
        self.body = body


class SandboxAgentClient:
    """Synchronous client for one sandbox-agent instance.

    Construction is cheap; one client per sandbox. Stateless except for the
    underlying `requests.Session` (kept-alive HTTP connections).
    """

    def __init__(self, base_url: str, bearer_token: str | None = None) -> None:
        # Strip trailing slash so we can compose paths cleanly.
        self.base_url = base_url.rstrip("/")
        self.bearer_token = bearer_token or ""
        self._session = requests.Session()
        if bearer_token:
            self._session.headers["Authorization"] = f"Bearer {bearer_token}"
        self._session.headers["User-Agent"] = "evo-sandbox-client/1"

    def clone(self) -> "SandboxAgentClient":
        """Return a fresh client with the same base URL and token."""
        return SandboxAgentClient(self.base_url, self.bearer_token or None)

    # ---------------------------------------------------------------- helpers

    def _url(self, path: str) -> str:
        return f"{self.base_url}{path}"

    def _check(self, resp: requests.Response) -> requests.Response:
        if resp.status_code >= 400:
            try:
                body = resp.text
            except Exception:
                body = "<unreadable>"
            raise SandboxAgentError(
                resp.status_code,
                f"{resp.request.method} {resp.url} failed",
                body=body[:1000],
            )
        return resp

    def _request(
        self,
        method: str,
        path: str,
        *,
        retry: bool = False,
        **kwargs: Any,
    ) -> requests.Response:
        call = getattr(self._session, method)
        if not retry:
            resp = call(self._url(path), **kwargs)
            return self._check(resp)

        last_err: Exception | None = None
        for attempt in range(SAFE_REQUEST_ATTEMPTS):
            try:
                resp = call(self._url(path), **kwargs)
                return self._check(resp)
            except SandboxAgentError as exc:
                last_err = exc
                if not (500 <= exc.status < 600) or attempt == SAFE_REQUEST_ATTEMPTS - 1:
                    raise
            except (requests.ConnectionError, requests.Timeout) as exc:
                last_err = exc
                if attempt == SAFE_REQUEST_ATTEMPTS - 1:
                    raise
            time.sleep(min(
                SAFE_REQUEST_BASE_DELAY * (2 ** attempt),
                SAFE_REQUEST_MAX_DELAY,
            ))
        if last_err is not None:
            raise last_err
        raise SandboxAgentError(0, f"{method.upper()} {self._url(path)} failed")

    # ---------------------------------------------------------------- health

    def health(self) -> dict[str, Any]:
        resp = self._request(
            "get",
            "/v1/health",
            retry=True,
            timeout=DEFAULT_REQUEST_TIMEOUT,
        )
        try:
            return resp.json()
        except ValueError:
            return {"raw": resp.text}

    def wait_for_health(self, timeout_seconds: float = 30.0, poll_interval: float = 0.5) -> None:
        """Poll `/v1/health` until 200 or timeout. Used post-provision."""
        deadline = time.monotonic() + timeout_seconds
        last_err: Exception | None = None
        while time.monotonic() < deadline:
            try:
                self.health()
                return
            except (requests.RequestException, SandboxAgentError) as exc:
                last_err = exc
                time.sleep(poll_interval)
        raise SandboxAgentError(
            0,
            f"sandbox-agent at {self.base_url} did not become healthy within "
            f"{timeout_seconds}s; last error: {last_err}",
        )

    # ---------------------------------------------------------------- process exec

    def process_run(
        self,
        command: str,
        args: list[str] | None = None,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
        timeout_ms: int | None = None,
        max_output_bytes: int | None = None,
    ) -> ProcessRunResult:
        """One-shot command. Blocks until the process exits or
        timeout_ms elapses (sandbox-side enforcement)."""
        body: dict[str, Any] = {"command": command}
        if args:
            body["args"] = list(args)
        if cwd:
            body["cwd"] = cwd
        if env:
            body["env"] = dict(env)
        if timeout_ms is not None:
            body["timeoutMs"] = timeout_ms
        if max_output_bytes is not None:
            body["maxOutputBytes"] = max_output_bytes

        # HTTP timeout: a bit more than sandbox-side timeout, or LONG default.
        http_timeout = (
            (timeout_ms / 1000) + 10 if timeout_ms is not None else LONG_REQUEST_TIMEOUT
        )
        resp = self._request(
            "post",
            "/v1/processes/run",
            json=body,
            timeout=http_timeout,
        )
        data = resp.json()
        return ProcessRunResult(
            stdout=data.get("stdout", ""),
            stderr=data.get("stderr", ""),
            exit_code=data.get("exitCode"),
            timed_out=bool(data.get("timedOut", False)),
            duration_ms=int(data.get("durationMs", 0)),
            stdout_truncated=bool(data.get("stdoutTruncated", False)),
            stderr_truncated=bool(data.get("stderrTruncated", False)),
        )

    def process_start(
        self,
        command: str,
        args: list[str] | None = None,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
    ) -> str:
        """Start a long-running process. Returns the process_id; logs are
        streamed via `process_logs(id, follow=True)`."""
        body: dict[str, Any] = {"command": command}
        if args:
            body["args"] = list(args)
        if cwd:
            body["cwd"] = cwd
        if env:
            body["env"] = dict(env)
        resp = self._request(
            "post",
            "/v1/processes",
            json=body,
            timeout=DEFAULT_REQUEST_TIMEOUT,
        )
        data = resp.json()
        pid = data.get("id")
        if not pid:
            raise SandboxAgentError(
                resp.status_code, "process start returned no id", body=resp.text[:200]
            )
        return str(pid)

    def process_status(self, process_id: str) -> dict[str, Any]:
        resp = self._request(
            "get",
            f"/v1/processes/{process_id}",
            retry=True,
            timeout=DEFAULT_REQUEST_TIMEOUT,
        )
        return resp.json()

    def process_logs(
        self,
        process_id: str,
        follow: bool = False,
        stream: str = "combined",
    ) -> Iterator[ProcessLogEntry]:
        """Yield parsed process log entries."""
        params = {"follow": "true" if follow else "false", "stream": stream}
        resp = self._request(
            "get",
            f"/v1/processes/{process_id}/logs?{urlencode(params)}",
            retry=not follow,
            stream=follow,
            timeout=None if follow else DEFAULT_REQUEST_TIMEOUT,
        )
        try:
            if not follow:
                payload = resp.json()
                for entry in payload.get("entries", []) or []:
                    yield _decode_log_entry(entry, default_stream=stream)
                return

            event_lines: list[str] = []
            for raw_line in resp.iter_lines(decode_unicode=True):
                if raw_line is None:
                    continue
                line = raw_line.strip()
                if not line:
                    for data_line in event_lines:
                        yield _decode_log_entry(
                            json.loads(data_line),
                            default_stream=stream,
                        )
                    event_lines = []
                    continue
                if line.startswith("data:"):
                    event_lines.append(line[len("data:"):].strip())
        finally:
            resp.close()

    def process_stop(self, process_id: str) -> None:
        self._request(
            "post",
            f"/v1/processes/{process_id}/stop",
            timeout=DEFAULT_REQUEST_TIMEOUT,
        )

    def process_kill(self, process_id: str) -> None:
        self._request(
            "post",
            f"/v1/processes/{process_id}/kill",
            timeout=DEFAULT_REQUEST_TIMEOUT,
        )

    # ---------------------------------------------------------------- filesystem

    def fs_read(self, path: str) -> bytes:
        resp = self._request(
            "get",
            f"/v1/fs/file?{urlencode({'path': path})}",
            retry=True,
            timeout=DEFAULT_REQUEST_TIMEOUT,
        )
        return resp.content

    def fs_write(self, path: str, data: bytes) -> None:
        self._request(
            "put",
            f"/v1/fs/file?{urlencode({'path': path})}",
            data=data,
            timeout=DEFAULT_REQUEST_TIMEOUT,
        )

    def fs_entries(self, path: str) -> list[FsEntry]:
        resp = self._request(
            "get",
            f"/v1/fs/entries?{urlencode({'path': path})}",
            retry=True,
            timeout=DEFAULT_REQUEST_TIMEOUT,
        )
        # Response is a top-level JSON array of FsEntry objects.
        # FsEntry shape: {name, path, entryType: "file"|"directory"|..., size, modified?}
        out = []
        for entry in resp.json() or []:
            entry_type = (entry.get("entryType") or "").lower()
            out.append(FsEntry(
                name=entry.get("name", ""),
                path=entry.get("path", ""),
                is_dir=entry_type == "directory",
                size=entry.get("size"),
            ))
        return out

    def fs_stat(self, path: str) -> dict[str, Any]:
        resp = self._request(
            "get",
            f"/v1/fs/stat?{urlencode({'path': path})}",
            retry=True,
            timeout=DEFAULT_REQUEST_TIMEOUT,
        )
        return resp.json()

    def fs_mkdir(self, path: str, recursive: bool = True) -> None:
        # `recursive` kwarg kept for API symmetry with the existing local
        # call sites, but sandbox-agent's mkdir always operates with
        # mkdir-p semantics (intermediate dirs created); the flag is a
        # no-op against the server. Server takes path as a query param.
        del recursive
        self._request(
            "post",
            f"/v1/fs/mkdir?{urlencode({'path': path})}",
            timeout=DEFAULT_REQUEST_TIMEOUT,
        )

    def fs_delete(self, path: str, recursive: bool = False) -> None:
        params = {"path": path, "recursive": "true" if recursive else "false"}
        self._request(
            "delete",
            f"/v1/fs/entry?{urlencode(params)}",
            timeout=DEFAULT_REQUEST_TIMEOUT,
        )

    def fs_move(self, src: str, dst: str) -> None:
        body = {"from": src, "to": dst}
        self._request(
            "post",
            "/v1/fs/move",
            json=body,
            timeout=DEFAULT_REQUEST_TIMEOUT,
        )

    def fs_upload_batch(self, dest_dir: str, tar_bytes: bytes) -> None:
        """Upload a tar archive that the server extracts under `dest_dir`."""
        params = {"path": dest_dir}
        self._request(
            "post",
            f"/v1/fs/upload-batch?{urlencode(params)}",
            data=tar_bytes,
            headers={"Content-Type": "application/x-tar"},
            timeout=LONG_REQUEST_TIMEOUT,
        )

    # ---------------------------------------------------------------- close

    def close(self) -> None:
        self._session.close()

    def __enter__(self) -> "SandboxAgentClient":
        return self

    def __exit__(self, *exc_info: Any) -> None:
        self.close()


def _decode_log_entry(payload: dict[str, Any], *, default_stream: str) -> ProcessLogEntry:
    data = payload.get("data", "")
    if payload.get("encoding") == "base64":
        blob = base64.b64decode(data)
    elif isinstance(data, str):
        blob = data.encode("utf-8")
    else:
        blob = bytes(data or b"")
    return ProcessLogEntry(
        sequence=int(payload.get("sequence", 0)),
        stream=str(payload.get("stream") or default_stream),
        timestamp_ms=int(payload.get("timestampMs", 0)),
        data=blob,
    )

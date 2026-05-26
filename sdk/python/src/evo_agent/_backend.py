"""Backend protocol and local file implementation."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class Backend(Protocol):
    """Transport layer between the SDK and the evo CLI (or a remote server).

    Implementations must be safe to call ``write_trace`` from multiple threads.
    """

    def setup(self, *, traces_dir: str | None, experiment_id: str | None) -> None: ...

    def write_trace(self, trace: dict[str, Any]) -> None: ...

    def emit_result(self, result: dict[str, Any]) -> None: ...

    def emit_gate_summary(self, *, passed: bool, lines: list[str]) -> None: ...


class LocalBackend:
    """Writes trace files to ``$EVO_TRACES_DIR`` and result JSON to
    ``$EVO_RESULT_PATH`` (or stdout in legacy mode).

    Thread safety: each ``write_trace`` call writes to a unique file path
    (``task_{id}.json``), so no locking is needed.
    """

    def setup(self, *, traces_dir: str | None, experiment_id: str | None) -> None:
        self._traces_dir = Path(traces_dir) if traces_dir else None
        if self._traces_dir:
            self._traces_dir.mkdir(parents=True, exist_ok=True)
        # Capture the real stdout before benchmarks redirect it to stderr.
        self._stdout = sys.stdout

    def write_trace(self, trace: dict[str, Any]) -> None:
        if self._traces_dir is None:
            return
        task_id = trace["task_id"]
        path = self._traces_dir / f"task_{task_id}.json"
        path.write_text(json.dumps(trace, indent=2), encoding="utf-8")

    def emit_result(self, result: dict[str, Any]) -> None:
        payload = json.dumps(result, indent=2)
        result_path = os.environ.get("EVO_RESULT_PATH")
        if not result_path:
            print(payload, file=self._stdout)
            return
        target = Path(result_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        # Claim destination + tmp+rename: duplicate writers fail-fast on
        # the O_EXCL claim; a crash mid-publish leaves an empty file at
        # target (caught by load_result) instead of a partial write.
        try:
            os.close(os.open(target, os.O_CREAT | os.O_EXCL | os.O_WRONLY))
        except FileExistsError:
            # Forensic sidecar: stderr may be captured/redirected by the
            # harness, so write the diagnostic to a stable filesystem
            # location too. Lets the post-mortem distinguish "benchmark
            # crashed before writing" from "concurrent writer collided".
            error_payload = {
                "error": "result_path_collision",
                "target": str(target),
                "pid": os.getpid(),
                "experiment_id": os.environ.get("EVO_EXPERIMENT_ID"),
                "message": (
                    f"{target} already exists; only one Run.finish() / "
                    f"write_result() per attempt"
                ),
            }
            try:
                target.with_suffix(target.suffix + ".error").write_text(
                    json.dumps(error_payload, indent=2), encoding="utf-8"
                )
            except OSError:
                pass
            print(
                f"evo SDK: refusing to overwrite existing {target} "
                f"(concurrent attempt or stale leftover)",
                file=sys.stderr,
                flush=True,
            )
            raise RuntimeError(error_payload["message"]) from None
        tmp = target.with_name(target.name + ".tmp")
        tmp.write_text(payload, encoding="utf-8")
        os.replace(tmp, target)

    def emit_gate_summary(self, *, passed: bool, lines: list[str]) -> None:
        for line in lines:
            print(line, file=sys.stderr)


def default_backend() -> Backend:
    """Pick a backend from environment variables."""
    server = os.environ.get("EVO_SERVER")
    if server:
        raise NotImplementedError(
            f"HTTP backend not yet available (EVO_SERVER={server}). "
            "Use local mode by unsetting EVO_SERVER."
        )
    return LocalBackend()

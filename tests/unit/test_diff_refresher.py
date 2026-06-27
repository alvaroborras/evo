"""Tests for the _DiffRefresher background thread."""

from __future__ import annotations

import threading
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from evo.core import _DiffRefresher, attempt_dir


class TestDiffRefresher:
    def test_stop_sets_event(self, tmp_path: Path) -> None:
        refresher = _DiffRefresher(
            tmp_path,
            "exp_0001",
            1,
            "abc123",
            tmp_path / "worktree",
        )
        assert not refresher._stop.is_set()
        refresher.stop(timeout=0.1)
        assert refresher._stop.is_set()

    def test_start_creates_thread(self, tmp_path: Path) -> None:
        refresher = _DiffRefresher(
            tmp_path,
            "exp_0001",
            1,
            "abc123",
            tmp_path / "worktree",
            interval=100,  # long interval so it won't actually tick
        )
        # Before start, thread should be None
        assert refresher._thread is None
        refresher.start()
        # After start, thread should be created (even if it exits quickly due to errors)
        assert refresher._thread is not None
        refresher.stop(timeout=2.0)

    def test_stop_is_idempotent(self, tmp_path: Path) -> None:
        refresher = _DiffRefresher(
            tmp_path,
            "exp_0001",
            1,
            "abc123",
            tmp_path / "worktree",
        )
        refresher.stop(timeout=0.1)
        refresher.stop(timeout=0.1)  # no-op, no error

    def test_start_is_idempotent(self, tmp_path: Path) -> None:
        refresher = _DiffRefresher(
            tmp_path,
            "exp_0001",
            1,
            "abc123",
            tmp_path / "worktree",
            interval=100,
        )
        refresher.start()
        t1 = refresher._thread
        refresher.start()
        t2 = refresher._thread
        assert t1 is t2
        refresher.stop(timeout=1.0)

    def test_daemon_thread(self, tmp_path: Path) -> None:
        refresher = _DiffRefresher(
            tmp_path,
            "exp_0001",
            1,
            "abc123",
            tmp_path / "worktree",
            interval=100,
        )
        refresher.start()
        assert refresher._thread.daemon is True
        refresher.stop(timeout=1.0)

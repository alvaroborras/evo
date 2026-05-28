"""Watchdog supervisor for the dashboard process.

Spawned by `_start_dashboard_background` as a detached subprocess.
Owns the dashboard's lifecycle: captures stdout/stderr to a rotated
log file, respawns the dashboard on unexpected exits with capped
backoff, gives up after repeated crash-on-startup so a permanently
broken dashboard doesn't loop forever.

Cross-platform: portalocker for the single-supervisor lock (same as
the rest of evo's locking), `RotatingFileHandler` for log rotation,
and `os.kill`/signal for the shutdown path. The parent
(`_start_dashboard_background`) handles the detach flags per OS.

Files this process owns under <root>/.evo/:
    supervisor.pid           own PID; cleaned up on exit
    supervisor.lock          flock target; prevents two supervisors
    supervisor.log           supervisor activity (rotated, 512 KB x 2)
    dashboard.pid            child PID; rewritten on each respawn
    dashboard.log            child stdout+stderr (rotated, 5 MB x 3)
    dashboard.dead           sentinel written when backoff gives up;
                             absence means "supervisor is still trying"

Lifecycle is intentionally simple: one supervisor per workspace, one
child at a time, no health checks (we only respawn on process death,
not on unresponsive dashboards).
"""
from __future__ import annotations

import logging
import logging.handlers
import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

# Rotation tuned for "noisy enough to capture a traceback, quiet enough
# to not fill disk." 5 MB x 3 = 20 MB max; enough to retain history
# through a few crash-loops.
DASHBOARD_LOG_MAX_BYTES = 5 * 1024 * 1024
DASHBOARD_LOG_BACKUP_COUNT = 3
SUPERVISOR_LOG_MAX_BYTES = 512 * 1024
SUPERVISOR_LOG_BACKUP_COUNT = 2

# Backoff schedule for respawn after an unexpected exit. Capped at 30s
# so a slow-crash bug doesn't push wait times to minutes.
BACKOFF_SCHEDULE_SECONDS = [1, 2, 4, 8, 16, 30]

# If the dashboard crashes this many times within this window of supervisor
# startup, the dashboard is presumed permanently broken and the supervisor
# exits after writing a sentinel.
RAPID_FAILURE_THRESHOLD = 5
RAPID_FAILURE_WINDOW_SECONDS = 60

# If the dashboard stays alive at least this long, reset the failure
# counter so a transient crash doesn't bias the next one.
HEALTHY_UPTIME_SECONDS = 120

# Wait this long for the child to exit after SIGTERM before SIGKILL.
SHUTDOWN_GRACE_SECONDS = 5.0

# Portable stop signal. A caller drops this file under <root>/.evo/ to ask
# the supervisor to shut down cleanly. Needed because on Windows an external
# signal is an uncatchable hard kill (TerminateProcess) — the SIGTERM handler
# never runs, so without the sentinel the supervisor can't tear its child
# down and remove its pid files on Windows.
SHUTDOWN_SENTINEL_NAME = "supervisor.shutdown"
SHUTDOWN_POLL_SECONDS = 0.25


_shutdown_requested = threading.Event()

# Set by the main loop to point at the current dashboard subprocess so
# the signal handler can SIGTERM it directly. proc.wait() blocks even
# when the supervisor process gets a signal — we have to ask the child
# to exit so wait() returns, then the loop sees the shutdown flag.
_active_child_proc: subprocess.Popen[bytes] | None = None


def _evo_dir(root: Path) -> Path:
    return root / ".evo"


def _make_rotating_logger(
    name: str,
    log_path: Path,
    max_bytes: int,
    backup_count: int,
    *,
    with_timestamp: bool,
) -> logging.Logger:
    logger = logging.getLogger(name)
    logger.setLevel(logging.INFO)
    logger.propagate = False
    if logger.handlers:
        return logger
    log_path.parent.mkdir(parents=True, exist_ok=True)
    handler = logging.handlers.RotatingFileHandler(
        log_path, maxBytes=max_bytes, backupCount=backup_count,
        encoding="utf-8",
    )
    fmt = "%(asctime)s %(levelname)s %(message)s" if with_timestamp else "%(message)s"
    handler.setFormatter(logging.Formatter(fmt))
    logger.addHandler(handler)
    return logger


def _pump_to_logger(stream, logger: logging.Logger) -> None:
    """Read lines from `stream` (a binary pipe) and forward each to
    `logger.info()`. Runs in a daemon thread; exits cleanly on EOF or
    any read error (broken pipe means the child is gone)."""
    try:
        for raw in iter(stream.readline, b""):
            line = raw.decode("utf-8", errors="replace").rstrip()
            if line:
                logger.info(line)
    except Exception:  # noqa: BLE001 — pipe closed; thread exits
        pass


def _handle_shutdown_signal(_signum, _frame) -> None:
    _shutdown_requested.set()
    # Propagate to the child so its parent's proc.wait() can return.
    # Without this the supervisor blocks on wait() forever even though
    # the signal flag is set — the wait won't unblock until the child
    # exits on its own.
    proc = _active_child_proc
    if proc is not None and proc.poll() is None:
        try:
            proc.terminate()
        except Exception:  # noqa: BLE001
            pass


def _watch_shutdown_sentinel(sentinel: Path) -> None:
    """Trigger a clean shutdown when the sentinel file appears.

    This is the cross-platform stop path. On POSIX the SIGTERM/SIGINT
    handlers already cover it; on Windows they can't (an external signal is
    a hard TerminateProcess that bypasses Python handlers), so this watcher
    is what lets the supervisor run its cleanup and exit 0 on Windows.
    """
    while not _shutdown_requested.is_set():
        if sentinel.exists():
            _handle_shutdown_signal(None, None)
            return
        time.sleep(SHUTDOWN_POLL_SECONDS)


def _resolve_root() -> Path:
    raw = os.environ.get("EVO_SUPERVISOR_ROOT") or os.getcwd()
    return Path(raw).resolve()


def _spawn_dashboard(root: Path) -> subprocess.Popen[bytes]:
    return subprocess.Popen(
        [sys.executable, "-m", "evo.dashboard"],
        cwd=str(root),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        stdin=subprocess.DEVNULL,
        env={**os.environ, "EVO_SUPERVISED": "1"},
    )


def main() -> int:
    root = _resolve_root()
    edir = _evo_dir(root)
    edir.mkdir(parents=True, exist_ok=True)

    supervisor_pid_file = edir / "supervisor.pid"
    supervisor_lock_file = edir / "supervisor.lock"
    dashboard_pid_file = edir / "dashboard.pid"
    dashboard_dead = edir / "dashboard.dead"
    dashboard_log = edir / "dashboard.log"
    supervisor_log = edir / "supervisor.log"
    shutdown_sentinel = edir / SHUTDOWN_SENTINEL_NAME

    # Single-supervisor guard via the workspace's existing lock pattern.
    # If acquisition times out, another supervisor is already running.
    from .locking import advisory_lock, LockTimeoutError

    sup_logger = _make_rotating_logger(
        "evo.dashboard.supervisor",
        supervisor_log,
        SUPERVISOR_LOG_MAX_BYTES,
        SUPERVISOR_LOG_BACKUP_COUNT,
        with_timestamp=True,
    )
    dash_logger = _make_rotating_logger(
        "evo.dashboard.output",
        dashboard_log,
        DASHBOARD_LOG_MAX_BYTES,
        DASHBOARD_LOG_BACKUP_COUNT,
        with_timestamp=False,
    )

    try:
        with advisory_lock(supervisor_lock_file, timeout_seconds=2.0):
            sup_logger.info(f"supervisor starting pid={os.getpid()} root={root}")
            supervisor_pid_file.write_text(str(os.getpid()), encoding="utf-8")
            dashboard_dead.unlink(missing_ok=True)
            # Clear any stale sentinel from a previous run so the watcher
            # below doesn't shut us down the instant we start.
            shutdown_sentinel.unlink(missing_ok=True)

            signal.signal(signal.SIGTERM, _handle_shutdown_signal)
            if os.name != "nt":
                signal.signal(signal.SIGINT, _handle_shutdown_signal)
            threading.Thread(
                target=_watch_shutdown_sentinel,
                args=(shutdown_sentinel,),
                daemon=True,
            ).start()

            failure_count = 0
            first_failure_at = 0.0
            backoff_index = 0
            proc: subprocess.Popen[bytes] | None = None
            global _active_child_proc

            try:
                while not _shutdown_requested.is_set():
                    start_time = time.monotonic()
                    try:
                        proc = _spawn_dashboard(root)
                        _active_child_proc = proc
                    except Exception as exc:  # noqa: BLE001
                        sup_logger.error(f"failed to spawn dashboard: {exc}")
                        try:
                            dashboard_dead.write_text(
                                f"supervisor could not spawn dashboard: {exc}\n",
                                encoding="utf-8",
                            )
                        except OSError:
                            pass
                        return 1

                    dashboard_pid_file.write_text(
                        str(proc.pid), encoding="utf-8"
                    )
                    sup_logger.info(f"dashboard spawned pid={proc.pid}")

                    pump_thread = threading.Thread(
                        target=_pump_to_logger,
                        args=(proc.stdout, dash_logger),
                        daemon=True,
                    )
                    pump_thread.start()

                    rc = proc.wait()
                    # Drain any final lines the child wrote before exit.
                    pump_thread.join(timeout=2.0)
                    uptime = time.monotonic() - start_time

                    if _shutdown_requested.is_set():
                        sup_logger.info(
                            f"dashboard exited (rc={rc}) during shutdown"
                        )
                        break

                    if rc == 0:
                        sup_logger.info(
                            "dashboard exited cleanly (rc=0); not respawning"
                        )
                        break

                    sup_logger.warning(
                        f"dashboard exited rc={rc} after {uptime:.1f}s"
                    )

                    if uptime > HEALTHY_UPTIME_SECONDS:
                        failure_count = 0
                        first_failure_at = 0.0
                        backoff_index = 0

                    now = time.monotonic()
                    if failure_count == 0:
                        first_failure_at = now
                    failure_count += 1

                    if (
                        failure_count >= RAPID_FAILURE_THRESHOLD
                        and now - first_failure_at < RAPID_FAILURE_WINDOW_SECONDS
                    ):
                        msg = (
                            f"dashboard crashed {failure_count} times in "
                            f"{int(now - first_failure_at)}s; giving up. "
                            f"Tail {dashboard_log} for the traceback."
                        )
                        sup_logger.error(msg)
                        try:
                            dashboard_dead.write_text(msg + "\n", encoding="utf-8")
                        except OSError:
                            pass
                        return 2

                    delay = BACKOFF_SCHEDULE_SECONDS[
                        min(backoff_index, len(BACKOFF_SCHEDULE_SECONDS) - 1)
                    ]
                    backoff_index += 1
                    sup_logger.info(f"backoff {delay}s before respawn")
                    if _shutdown_requested.wait(timeout=float(delay)):
                        break
            finally:
                if proc is not None and proc.poll() is None:
                    sup_logger.info(f"sending SIGTERM to dashboard pid={proc.pid}")
                    try:
                        proc.terminate()
                        try:
                            proc.wait(timeout=SHUTDOWN_GRACE_SECONDS)
                        except subprocess.TimeoutExpired:
                            sup_logger.warning(
                                f"dashboard pid={proc.pid} did not exit in "
                                f"{SHUTDOWN_GRACE_SECONDS}s; SIGKILL"
                            )
                            proc.kill()
                            proc.wait(timeout=2.0)
                    except Exception as exc:  # noqa: BLE001
                        sup_logger.error(f"error stopping dashboard: {exc}")
                dashboard_pid_file.unlink(missing_ok=True)
                supervisor_pid_file.unlink(missing_ok=True)
                shutdown_sentinel.unlink(missing_ok=True)
                sup_logger.info("supervisor exiting")
            return 0
    except LockTimeoutError:
        sup_logger.error("another supervisor is already running; exiting")
        return 3


if __name__ == "__main__":
    sys.exit(main())

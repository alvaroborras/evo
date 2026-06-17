"""Best-effort anonymous telemetry for evo.

Telemetry is intentionally CLI-side only. It never reads benchmark traces,
project notes, prompts, command strings, file paths, environment values, git
remotes, or raw exception text. Payloads are sent to evo's Cloudflare Worker,
which performs another allowlist/scrub pass before first-party storage and any
optional server-side mirrors.
"""

from __future__ import annotations

import json
import math
import os
import platform
import random
import re
import threading
import time
import uuid
from pathlib import Path
from typing import Any

import requests

from . import __version__
from .core import atomic_write_json, evo_dir, load_json, lock_file_for
from .locking import advisory_lock
from .user_defaults import global_evo_dir

TELEMETRY_CONFIG_URL = "https://telemetry.evo-hq.com/v1/config"
TELEMETRY_EVENTS_URL = "https://telemetry.evo-hq.com/v1/events"

DEFAULT_TIMEOUT_SECONDS = 0.3
FEEDBACK_TIMEOUT_SECONDS = 1.0
CONFIG_TTL_SECONDS = 24 * 60 * 60
MAX_CONTEXT_ITEMS = 24
MAX_CONTEXT_KEY_LENGTH = 48
MAX_CONTEXT_VALUE_LENGTH = 160

_SESSION_ID = str(uuid.uuid4())

_SECRET_TOKEN_RE = re.compile(
    r"\b(?:sk-[A-Za-z0-9_-]{20,}|gh[pousr]_[A-Za-z0-9_]{20,}|"
    r"ph[ckx]_[A-Za-z0-9_]{20,}|AKIA[0-9A-Z]{16})\b"
)
_LONG_HEX_RE = re.compile(r"\b[A-Fa-f0-9]{40,}\b")
_EMAIL_RE = re.compile(r"\b\S+@\S+\.\S+\b")
_ENV_ASSIGN_RE = re.compile(r"\b[A-Z_][A-Z0-9_]{2,}\s*=\s*\S+")
_PATH_RE = re.compile(r"\b(?:[A-Za-z]:\\|~/|/)[^\s\"'`]+")


def telemetry_path() -> Path:
    return global_evo_dir() / "telemetry.json"


def telemetry_config_cache_path() -> Path:
    return global_evo_dir() / "telemetry_config.json"


def load_settings() -> dict[str, Any]:
    data = load_json(telemetry_path(), {})
    return data if isinstance(data, dict) else {}


def save_settings(data: dict[str, Any]) -> None:
    path = telemetry_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with advisory_lock(lock_file_for(path)):
        atomic_write_json(path, data)


def set_enabled(enabled: bool) -> None:
    data = load_settings()
    data["enabled"] = bool(enabled)
    if enabled:
        data.setdefault("install_id", str(uuid.uuid4()))
    save_settings(data)


def install_id(create: bool = True) -> str | None:
    data = load_settings()
    current = data.get("install_id")
    if isinstance(current, str) and current:
        return current
    if not create:
        return None
    current = str(uuid.uuid4())
    data["install_id"] = current
    save_settings(data)
    return current


def reset_install_id() -> str:
    data = load_settings()
    current = str(uuid.uuid4())
    data["install_id"] = current
    save_settings(data)
    return current


def _env_telemetry_value() -> str | None:
    value = os.environ.get("EVO_TELEMETRY")
    return value.strip().lower() if value is not None else None


def env_forces_off() -> bool:
    value = _env_telemetry_value()
    if value in {"0", "false", "off", "no"}:
        return True
    dnt = os.environ.get("DO_NOT_TRACK", "").strip().lower()
    return dnt in {"1", "true", "yes"}


def env_forces_on() -> bool:
    value = _env_telemetry_value()
    return value in {"1", "true", "on", "yes"}


def telemetry_enabled() -> bool:
    if env_forces_off():
        return False
    if load_settings().get("enabled") is False:
        return False
    if os.environ.get("CI") and not env_forces_on():
        return False
    return True


def status() -> dict[str, Any]:
    settings = load_settings()
    if env_forces_off():
        source = "env"
        enabled = False
    elif settings.get("enabled") is False:
        source = "user"
        enabled = False
    elif os.environ.get("CI") and not env_forces_on():
        source = "ci"
        enabled = False
    else:
        source = "default" if "enabled" not in settings else "user"
        enabled = True
    return {
        "enabled": enabled,
        "source": source,
        "path": str(telemetry_path()),
        "install_id": install_id(create=False),
        "endpoint": events_url(),
    }


def normalize_os_name(name: str | None = None) -> str:
    raw = (name or platform.system() or "").strip().lower()
    if raw.startswith("darwin"):
        return "darwin"
    if raw.startswith("linux"):
        return "linux"
    if raw.startswith("windows") or raw.startswith("msys") or raw.startswith("mingw"):
        return "windows"
    if raw.startswith("freebsd"):
        return "freebsd"
    return "unknown"


def base_properties() -> dict[str, Any]:
    return {
        "install_id": install_id(create=True),
        "session_id": _SESSION_ID,
        "evo_version": __version__,
        "os": normalize_os_name(),
    }


def workspace_id(root: Path, create: bool = True) -> str | None:
    meta_path = evo_dir(root) / "meta.json"
    if not meta_path.exists():
        return None
    data = load_json(meta_path, {})
    if not isinstance(data, dict):
        return None
    current = data.get("telemetry_workspace_id")
    if isinstance(current, str) and current:
        return current
    if not create:
        return None
    current = str(uuid.uuid4())
    with advisory_lock(lock_file_for(meta_path)):
        data = load_json(meta_path, {})
        if not isinstance(data, dict):
            data = {}
        existing = data.get("telemetry_workspace_id")
        if isinstance(existing, str) and existing:
            return existing
        data["telemetry_workspace_id"] = current
        atomic_write_json(meta_path, data)
    return current


def events_url() -> str:
    return os.environ.get("EVO_TELEMETRY_ENDPOINT", TELEMETRY_EVENTS_URL)


def config_url() -> str:
    return os.environ.get("EVO_TELEMETRY_CONFIG_URL", TELEMETRY_CONFIG_URL)


def timeout_seconds(default: float = DEFAULT_TIMEOUT_SECONDS) -> float:
    raw = os.environ.get("EVO_TELEMETRY_TIMEOUT_MS")
    if not raw:
        return default
    try:
        parsed = max(0, int(raw)) / 1000.0
    except ValueError:
        return default
    return parsed


def _load_cached_remote_config() -> dict[str, Any] | None:
    path = telemetry_config_cache_path()
    data = load_json(path, {})
    if not isinstance(data, dict):
        return None
    fetched_at = data.get("fetched_at")
    config = data.get("config")
    if not isinstance(fetched_at, (int, float)) or not isinstance(config, dict):
        return None
    ttl = int(config.get("ttl_seconds") or CONFIG_TTL_SECONDS)
    if time.time() - float(fetched_at) > ttl:
        return None
    return config


def _fetch_remote_config(timeout: float) -> dict[str, Any] | None:
    try:
        response = requests.get(config_url(), timeout=timeout)
        if response.status_code >= 400:
            return None
        config = response.json()
        if not isinstance(config, dict):
            return None
        path = telemetry_config_cache_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_json(path, {"fetched_at": time.time(), "config": config})
        return config
    except Exception:
        return None


def remote_config(timeout: float) -> dict[str, Any]:
    cached = _load_cached_remote_config()
    if cached is not None:
        return cached
    fetched = _fetch_remote_config(timeout)
    if fetched is not None:
        return fetched
    return {"enabled": True, "sample_rate": 1.0}


def _sampled(config: dict[str, Any]) -> bool:
    try:
        rate = float(config.get("sample_rate", 1.0))
    except (TypeError, ValueError):
        rate = 1.0
    if rate <= 0:
        return False
    if rate >= 1:
        return True
    return random.random() < rate


def scrub_text(value: str, max_len: int) -> str:
    # Local belt-and-suspenders. The Worker performs the authoritative scrub.
    value = value.replace("\n", " ").replace("\r", " ")
    for marker in ("http://", "https://", "git@", "ssh://"):
        if marker in value:
            value = value.replace(marker, "<redacted>:")
    value = _EMAIL_RE.sub("<email>", value)
    value = _ENV_ASSIGN_RE.sub("<env>", value)
    value = _PATH_RE.sub("<path>", value)
    value = _SECRET_TOKEN_RE.sub("<secret>", value)
    value = _LONG_HEX_RE.sub("<token>", value)
    return " ".join(value.split())[:max_len]


def scrub_tags(tags: list[str]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for raw in tags:
        raw_text = str(raw).strip()
        raw_lower = raw_text.lower()
        if any(marker in raw_lower for marker in ("http://", "https://", "git@", "ssh://")):
            continue
        if "@" in raw_text or _SECRET_TOKEN_RE.search(raw_text) or _LONG_HEX_RE.search(raw_text):
            continue
        tag = "".join(
            ch.lower() if ch.isalnum() or ch in {"-", "_", ".", ":"} else "-"
            for ch in raw_text
        ).strip("-")[:48]
        if not tag or tag in seen:
            continue
        seen.add(tag)
        out.append(tag)
        if len(out) >= 10:
            break
    return out


def scrub_context(context: dict[str, Any]) -> dict[str, Any]:
    """Bounded public-safe context for future analytics joins.

    Context is intentionally shallow: short keys and primitive values only.
    This lets newer CLI/skill flows attach low-risk facets without expanding
    the core event contract for every dashboard question.
    """
    out: dict[str, Any] = {}
    for raw_key, raw_value in context.items():
        if len(out) >= MAX_CONTEXT_ITEMS:
            break
        key = "".join(
            ch.lower() if ch.isalnum() or ch in {"-", "_", ".", ":"} else "_"
            for ch in str(raw_key).strip()
        ).strip("_")[:MAX_CONTEXT_KEY_LENGTH]
        if not key:
            continue
        value: Any
        if isinstance(raw_value, bool):
            value = raw_value
        elif (
            isinstance(raw_value, (int, float))
            and not isinstance(raw_value, bool)
            and math.isfinite(raw_value)
        ):
            value = raw_value
        elif isinstance(raw_value, str):
            cleaned = scrub_text(raw_value, MAX_CONTEXT_VALUE_LENGTH)
            if not cleaned or "<" in cleaned or "@" in cleaned:
                continue
            value = cleaned
        else:
            continue
        out[key] = value
    return out


def capture(
    event: str,
    properties: dict[str, Any] | None = None,
    *,
    flush: bool = False,
    timeout: float | None = None,
) -> None:
    if not telemetry_enabled():
        return
    props = {**base_properties(), **(properties or {})}
    if isinstance(props.get("context"), dict):
        props["context"] = scrub_context(props["context"])
    send_timeout = timeout_seconds(timeout if timeout is not None else DEFAULT_TIMEOUT_SECONDS)
    if flush:
        _send_event(event, props, send_timeout)
        return
    try:
        thread = threading.Thread(
            target=_send_event,
            args=(event, props, send_timeout),
            daemon=True,
        )
        thread.start()
    except Exception:
        pass


def _send_event(event: str, properties: dict[str, Any], timeout: float) -> None:
    try:
        config = remote_config(timeout)
        if config.get("enabled") is False or not _sampled(config):
            return
        payload = {"event": event, "properties": properties}
        requests.post(events_url(), json=payload, timeout=timeout)
    except Exception:
        pass


def capture_usecase(
    description: str,
    tags: list[str],
    properties: dict[str, Any] | None = None,
) -> bool:
    if not telemetry_enabled():
        return False
    props = dict(properties or {})
    props.setdefault("trigger", "cli")
    props.setdefault("workflow_phase", "usecase")
    props.update(
        {
            "description": scrub_text(description, 280),
            "tags": scrub_tags(tags),
        }
    )
    capture(
        "usecase",
        props,
        flush=True,
        timeout=FEEDBACK_TIMEOUT_SECONDS,
    )
    return True


def capture_feedback(
    *,
    kind: str,
    phase: str,
    summary: str,
    expected: str,
    actual: str,
    repro: str,
    tags: list[str],
    properties: dict[str, Any] | None = None,
) -> bool:
    if not telemetry_enabled():
        return False
    props = dict(properties or {})
    props.setdefault("trigger", "cli")
    props.setdefault("workflow_phase", "feedback")
    props.update(
        {
            "kind": scrub_text(kind, 80),
            "phase": scrub_text(phase, 80),
            "summary": scrub_text(summary, 1200),
            "expected": scrub_text(expected, 1200),
            "actual": scrub_text(actual, 1200),
            "repro": scrub_text(repro, 1200),
            "tags": scrub_tags(tags),
        }
    )
    capture(
        "feedback",
        props,
        flush=True,
        timeout=FEEDBACK_TIMEOUT_SECONDS,
    )
    return True


def disable_with_final_event(properties: dict[str, Any] | None = None) -> bool:
    """Disable globally. Returns True when a final opt-out event was attempted."""
    should_send = telemetry_enabled()
    if should_send:
        props = dict(properties or {})
        props["disabled_method"] = "command"
        props.setdefault("trigger", "cli")
        props.setdefault("workflow_phase", "telemetry_disabled")
        capture("telemetry_disabled", props, flush=True, timeout=0.5)
    set_enabled(False)
    return should_send

"""Cross-project user defaults for run-behavior preferences.

Unlike the per-repo workspace config (``.evo/config.json``), these live in a
single user-level file so a preference chosen in one project can be remembered
and pre-filled in the next. Discover writes the user's answer here; optimize
reads it as the fallback below the workspace default.

Location: ``$EVO_HOME/defaults.json``, or ``~/.evo/defaults.json`` when
``EVO_HOME`` is unset. ``Path.home()`` resolves on every platform (Windows
included), and the write path reuses ``atomic_write_json`` (parent mkdir +
``os.replace``) and ``advisory_lock`` (portalocker), both already cross-platform.

Recognized keys: ``autonomous``, ``subagents_only`` (booleans).
"""
from __future__ import annotations

import os
from pathlib import Path

from .core import atomic_write_json, load_json, lock_file_for
from .locking import advisory_lock

_VALID_KEYS = frozenset({"autonomous", "subagents_only"})


def global_evo_dir() -> Path:
    """User-level evo home: ``$EVO_HOME`` or ``~/.evo``."""
    override = os.environ.get("EVO_HOME")
    return Path(override) if override else Path.home() / ".evo"


def global_defaults_path() -> Path:
    return global_evo_dir() / "defaults.json"


def load_user_defaults() -> dict:
    data = load_json(global_defaults_path(), {})
    return data if isinstance(data, dict) else {}


def get_user_default(key: str) -> bool | None:
    """Return the stored bool for ``key``, or None if unset."""
    if key not in _VALID_KEYS:
        raise ValueError(f"unknown user default key: {key!r}")
    value = load_user_defaults().get(key)
    return bool(value) if value is not None else None


def set_user_default(key: str, value: bool) -> None:
    if key not in _VALID_KEYS:
        raise ValueError(f"unknown user default key: {key!r}")
    path = global_defaults_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with advisory_lock(lock_file_for(path)):
        data = load_user_defaults()
        data[key] = bool(value)
        atomic_write_json(path, data)

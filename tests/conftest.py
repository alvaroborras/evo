"""Shared pytest config for the entire tests/ tree.

Adds tests/ to sys.path so subdirectory tests (tests/e2e/, tests/live/)
can import the leading-underscore helpers (`_sandbox_agent_fixture`,
`_sshd_fixture`) that live at tests/ root.
"""
from __future__ import annotations

import sys
from pathlib import Path

TESTS_ROOT = Path(__file__).resolve().parent
if str(TESTS_ROOT) not in sys.path:
    sys.path.insert(0, str(TESTS_ROOT))

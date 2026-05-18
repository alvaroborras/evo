"""Per-unit-test pytest config.

Builds the Rust evo-hook-drain binary once per pytest session if it's
not already staged. Both test_hook_drain.py and test_subagent_identity.py
exercise the binary as a subprocess — without it, they'd error opaquely.
The fixture fails the suite with a clear instruction if cargo isn't
available locally.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parent.parent.parent
HOOK_NAME = "evo-hook-drain.exe" if sys.platform == "win32" else "evo-hook-drain"
HOOK_PATH = REPO_ROOT / "plugins" / "evo" / "bin" / HOOK_NAME
RUST_CRATE = REPO_ROOT / "plugins" / "evo" / "bin" / "evo-hook-drain-rs"


@pytest.fixture(scope="session", autouse=True)
def _ensure_hook_drain_binary():
    """Build the Rust hook-drain binary if it's not already staged at
    plugins/evo/bin/. Fails the entire suite (not individual tests)
    if the binary can't be produced — CI ensures it's built before
    pytest runs; locally a missing cargo means the dev needs to
    install Rust.
    """
    if HOOK_PATH.exists():
        return
    cargo = subprocess.run(["cargo", "--version"], capture_output=True)
    if cargo.returncode != 0:
        pytest.fail(
            f"hook binary not staged at {HOOK_PATH} and `cargo` is not "
            f"available to build it. Install Rust (rustup.rs) then run:\n"
            f"  cd {RUST_CRATE} && cargo build --release\n"
            f"  cp target/release/{HOOK_NAME} {HOOK_PATH}"
        )
    build = subprocess.run(
        ["cargo", "build", "--release"],
        cwd=str(RUST_CRATE),
        capture_output=True,
    )
    if build.returncode != 0:
        pytest.fail(
            f"`cargo build --release` failed in {RUST_CRATE}:\n"
            f"{build.stderr.decode(errors='replace')}"
        )
    built = RUST_CRATE / "target" / "release" / HOOK_NAME
    if not built.exists():
        pytest.fail(f"cargo built but binary missing at {built}")
    shutil.copy2(built, HOOK_PATH)
    if sys.platform != "win32":
        os.chmod(HOOK_PATH, 0o755)

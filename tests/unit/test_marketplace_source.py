"""Tests for claude_code._marketplace_source — how `--version` and
`--from-path` get translated into a `claude plugin marketplace add` source.

Covers the user request: --version should accept arbitrary refs (alpha
tags, branch names, commit SHAs), not just release versions.
"""

from __future__ import annotations

from evo.host_install import claude_code as cc


def test_no_pin_returns_bare_repo():
    assert cc._marketplace_source(None, None) == "evo-hq/evo"


def test_release_version_auto_prefixed_with_v():
    assert cc._marketplace_source("0.4.0", None) == "evo-hq/evo@v0.4.0"
    assert cc._marketplace_source("1.2.3", None) == "evo-hq/evo@v1.2.3"


def test_prerelease_versions_auto_prefixed():
    assert cc._marketplace_source("0.4.0-alpha.5", None) == "evo-hq/evo@v0.4.0-alpha.5"
    assert cc._marketplace_source("0.4.0a5", None) == "evo-hq/evo@v0.4.0a5"
    assert cc._marketplace_source("0.4.0rc1", None) == "evo-hq/evo@v0.4.0rc1"


def test_branch_names_pass_through():
    assert cc._marketplace_source("main", None) == "evo-hq/evo@main"
    assert cc._marketplace_source("develop", None) == "evo-hq/evo@develop"
    assert cc._marketplace_source("feature/x", None) == "evo-hq/evo@feature/x"


def test_already_v_prefixed_tags_pass_through():
    assert cc._marketplace_source("v0.4.0", None) == "evo-hq/evo@v0.4.0"


def test_commit_shas_pass_through():
    sha = "f6bebc3e056b85d74d8c0bec2dd9f00569075008"
    assert cc._marketplace_source(sha, None) == f"evo-hq/evo@{sha}"


def test_from_path_takes_precedence_over_version():
    assert cc._marketplace_source("0.4.0", "/tmp/local") == "/tmp/local"


def test_looks_like_pypi_release():
    assert cc._looks_like_pypi_release("0.4.0") is True
    assert cc._looks_like_pypi_release("0.4.0-alpha.5") is True
    assert cc._looks_like_pypi_release("1.0.0") is True
    assert cc._looks_like_pypi_release("main") is False
    assert cc._looks_like_pypi_release("develop") is False
    assert cc._looks_like_pypi_release("v0.4.0") is False  # 'v' prefix isn't a pypi version
    assert cc._looks_like_pypi_release("f6bebc3e056b85d74d8c0bec2dd9f00569075008") is False

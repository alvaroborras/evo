"""Release-smoke: post-release end-to-end check against PUBLISHED artifacts.

This test verifies the artifacts a user actually downloads — the PyPI
wheel, the npm packages, the GitHub marketplace plugin, the npx skills
distribution — by installing them on a fresh sandbox and running the
full evo flow end-to-end.

It deliberately does NOT install from the local branch. That's
``tests/live/test_install_sandbox.py``'s job (developer workflow:
verify my branch installs cleanly). The release smoke catches a
different class of bug: a wheel built and uploaded but missing files,
a marketplace pointed at a stale ref, an entry-point that didn't make
it into the sdist, etc.

Per host, on a fresh e2b sandbox:
  1. install host CLI from its public source (npm, curl installer, etc.)
  2. install evo from PyPI: ``uv tool install evo-hq-cli==<VERSION>``
  3. install skills via ``npx skills add evo-hq/evo``  (GitHub default branch)
  4. install host plugin via the host's marketplace command
  5. copy fixture repo, ``evo init``, drive the agent
  6. mid-run ``evo direct`` injects a directive
  7. assert: ≥2 experiments committed, directive consumed, score beats baseline

Slow + LLM-spending. Gated:

    EVO_LIVE_TEST_RELEASE_SMOKE=1 \\
    E2B_API_KEY=... \\
    OPENAI_API_KEY=... \\
    pytest tests/live/test_release_smoke.py::test_opencode -v -s

Pin a specific evo version (e.g. right after a release):

    EVO_RELEASE_SMOKE_VERSION=0.4.0 pytest tests/live/test_release_smoke.py

Dry-run against THIS branch's evo before tagging (skips PyPI, installs
from a tarball of the local source — host CLIs and skills still pull
from npm/GitHub since those are versioned independently):

    EVO_RELEASE_SMOKE_SOURCE=local pytest tests/live/test_release_smoke.py

Today only opencode is implemented. The other four hosts are stubbed
xfail with a reason — each needs its own non-interactive multi-turn
driver pattern (ACP, headless TUI, or `--print`-style flags).
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
PLUGIN_ROOT = REPO_ROOT / "plugins" / "evo"
FIXTURE_ROOT = REPO_ROOT / "tests" / "fixtures" / "release_smoke"
FIXTURE_REPO = FIXTURE_ROOT / "repo"
FIXTURE_PROMPT = FIXTURE_ROOT / "prompt.md"


# ---------------------------------------------------------------------------
# Gating + fixtures
# ---------------------------------------------------------------------------


def _gate_release_smoke() -> None:
    if os.environ.get("EVO_LIVE_TEST_RELEASE_SMOKE") != "1":
        pytest.skip("set EVO_LIVE_TEST_RELEASE_SMOKE=1 to enable")
    if not os.environ.get("E2B_API_KEY"):
        pytest.skip("E2B_API_KEY not set")
    try:
        import e2b  # noqa: F401
    except ImportError:
        pytest.skip("e2b SDK not installed (`pip install 'e2b>=2.20'`)")


@pytest.fixture(scope="session")
def fixture_repo_tarball():
    """Tar the release-smoke fixture repo for upload. The fixture repo
    itself isn't 'published' — it's just the workload-under-test, and
    bundling it via tarball is the only thing we ship from the local
    tree. (The evo CLI itself comes from PyPI.)"""
    _gate_release_smoke()
    out = Path(tempfile.mkstemp(suffix=".tar.gz")[1])
    try:
        with tarfile.open(out, "w:gz") as tar:
            tar.add(str(FIXTURE_REPO), arcname="ws")
        yield out
    finally:
        out.unlink(missing_ok=True)


def _evo_pypi_spec() -> str:
    """Return the evo-hq-cli pip spec to install from PyPI.

    Pin via EVO_RELEASE_SMOKE_VERSION (e.g. ``0.4.0``); default is the
    latest stable on PyPI."""
    version = os.environ.get("EVO_RELEASE_SMOKE_VERSION", "").strip()
    return f"evo-hq-cli=={version}" if version else "evo-hq-cli"


def _use_local_source() -> bool:
    """If ``EVO_RELEASE_SMOKE_SOURCE=local``, install evo from the local
    branch instead of PyPI. Use for dry-running a pre-release flow
    against THIS branch's code, before the wheel is uploaded."""
    return os.environ.get("EVO_RELEASE_SMOKE_SOURCE", "").strip().lower() == "local"


def _make_evo_tarball(out: Path) -> None:
    """Bundle BOTH the plugins/evo package source AND the repo root's
    .claude-plugin/marketplace.json so the sandbox can:
      (a) `uv tool install /tmp/evo-local-repo/plugins/evo` for the CLI
      (b) `claude plugin marketplace add /tmp/evo-local-repo` for the
          plugin (skills + hooks + plugin.json) — pointing at the
          marketplace.json that declares ./plugins/evo as the source.
    Without (b), the test would install the GitHub-published plugin
    even in local mode, defeating the point."""
    def filt(tar):
        skip = (".git", ".venv", "node_modules", "__pycache__", "build",
                "dist", ".pytest_cache", ".egg-info")
        if any(s in tar.name for s in skip):
            return None
        return tar

    with tarfile.open(out, "w:gz") as tar:
        tar.add(str(REPO_ROOT / ".claude-plugin"),
                arcname="evo-local-repo/.claude-plugin", filter=filt)
        tar.add(str(PLUGIN_ROOT),
                arcname="evo-local-repo/plugins/evo", filter=filt)


@pytest.fixture(scope="session")
def evo_local_tarball():
    """Local-source tarball, only built when EVO_RELEASE_SMOKE_SOURCE=local.
    Lets the harness install from this branch instead of PyPI for dry runs."""
    if not _use_local_source():
        yield None
        return
    _gate_release_smoke()
    with tempfile.NamedTemporaryFile(suffix=".tar.gz", delete=False) as f:
        path = Path(f.name)
    try:
        _make_evo_tarball(path)
        yield path
    finally:
        path.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# Sandbox harness
# ---------------------------------------------------------------------------


class _Harness:
    """Sandbox helper. Mirrors the one in test_install_sandbox.py — kept
    deliberately copy-paste rather than refactored into a shared module so
    each test file is self-contained for live debugging."""

    def __init__(self, sbx, fixture_tarball: Path, evo_local_tarball: Path | None = None):
        self.sbx = sbx
        self._fixture_tarball = fixture_tarball
        self._evo_local_tarball = evo_local_tarball
        self._sudo = "sudo " if self.run("whoami").strip() != "root" else ""

    @property
    def marketplace_source(self) -> str:
        """Source for `<host> plugin marketplace add` — local path when in
        dry-run mode, GitHub repo otherwise. Resolves to a string that
        is plug-compatible with `claude plugin marketplace add` and
        `codex plugin marketplace add`."""
        if self._evo_local_tarball is not None:
            return "/tmp/evo-local-repo"
        return "evo-hq/evo"

    def run(self, cmd: str, *, timeout: int = 180, must_succeed: bool = True,
            background: bool = False) -> str:
        short = cmd[:90] + ("…" if len(cmd) > 90 else "")
        prefix = "&" if background else "$"
        print(f"{prefix} {short}", flush=True)
        r = self.sbx.commands.run(
            cmd,
            timeout=timeout,
            background=background,
            on_stdout=lambda x: print(f"  | {x.rstrip()}", flush=True),
            on_stderr=lambda x: print(f"  ! {x.rstrip()}", flush=True),
        )
        if not background and must_succeed and r.exit_code != 0:
            raise AssertionError(
                f"command failed (exit {r.exit_code}): {short}\nstderr: {r.stderr[-500:]}"
            )
        return r.stdout if not background else ""

    def upload_fixture_repo(self) -> None:
        self.sbx.files.write("/tmp/ws.tar.gz", self._fixture_tarball.read_bytes())
        self.run("mkdir -p /tmp && tar -xzf /tmp/ws.tar.gz -C /tmp/")
        self.run(
            "cd /tmp/ws && git init -q && "
            "git config user.email 'smoke@evo' && git config user.name 'smoke' && "
            "git config commit.gpgsign false && "
            "git add . && git commit -q -m 'fixture'"
        )

    def install_base_deps(self) -> None:
        """Install evo. Default: from PyPI (release-smoke proper, verifies
        the wheel users will download). With EVO_RELEASE_SMOKE_SOURCE=local:
        from a tarball of the local branch (dry-run before tagging)."""
        self.run(
            f"{self._sudo}apt-get update -qq && {self._sudo}apt-get install -y "
            f"--no-install-recommends git curl ca-certificates python3 python3-venv "
            f">/dev/null",
            timeout=300,
        )
        self.run("curl -LsSf https://astral.sh/uv/install.sh | sh > /tmp/uv.log 2>&1",
                 timeout=120)

        if self._evo_local_tarball is not None:
            print("== installing evo from LOCAL branch (dry-run mode) ==", flush=True)
            self.sbx.files.write("/tmp/evo-local-repo.tar.gz",
                                 self._evo_local_tarball.read_bytes())
            self.run("tar -xzf /tmp/evo-local-repo.tar.gz -C /tmp/")
            # /tmp/evo-local-repo/ now has:
            #   .claude-plugin/marketplace.json
            #   plugins/evo/   (CLI source + skills + hooks + plugin.json)
            self.run(
                "export PATH=$HOME/.local/bin:$PATH; "
                "uv tool install /tmp/evo-local-repo/plugins/evo "
                "> /tmp/evo-tool-install.log 2>&1 || "
                "(cat /tmp/evo-tool-install.log; exit 1)",
                timeout=300,
            )
        else:
            spec = _evo_pypi_spec()
            self.run(
                f"export PATH=$HOME/.local/bin:$PATH; "
                f"uv tool install '{spec}' > /tmp/evo-tool-install.log 2>&1 || "
                f"(cat /tmp/evo-tool-install.log; exit 1)",
                timeout=300,
            )

        self.run("export PATH=$HOME/.local/bin:$PATH; evo --version")


@pytest.fixture
def sandbox(fixture_repo_tarball, evo_local_tarball):
    _gate_release_smoke()
    from e2b import Sandbox
    sbx = Sandbox.create(timeout=1800)
    h = _Harness(sbx, fixture_repo_tarball, evo_local_tarball)
    try:
        h.install_base_deps()
        yield h
    finally:
        try:
            sbx.kill()
        except Exception:  # noqa: BLE001
            pass


# ---------------------------------------------------------------------------
# Assertion helpers
# ---------------------------------------------------------------------------


def _wait_for_n_experiments(h: _Harness, ws_root: str, n: int,
                            timeout: int = 600) -> tuple[int, str]:
    """Poll across <ws_root>/run_*/experiments/ until at least n experiment
    dirs exist anywhere, or timeout. Returns (count, active_run_dir) where
    active_run_dir is the run with the most experiments (the one the agent
    actually used).

    Counting across all runs (not just the one harness `evo init` created)
    is necessary because some host agents — observed on openclaw, also on
    codex — call `evo init` themselves and write to run_0001. Polling only
    run_0000 stays at 0 forever in that case.

    Fails fast if the agent process dies before reaching the target — no
    point polling for 15 min when the writer is gone. 30s startup grace
    period so we don't trip on the agent's own init.
    """
    deadline = time.time() + timeout
    start = time.time()
    last = -1
    active_run = f"{ws_root}/run_0000"  # fallback
    while time.time() < deadline:
        # Single shell call: experiment count + which run has the most + agent liveness.
        out = h.run(
            f"find {ws_root}/run_*/experiments -mindepth 1 -maxdepth 1 -type d "
            "2>/dev/null | wc -l; "
            # The run with the most experiment dirs is the agent's working run.
            f"for d in {ws_root}/run_*; do "
            "  c=$(ls \"$d/experiments\" 2>/dev/null | wc -l); echo \"$c $d\"; "
            "done | sort -rn | head -1 | awk '{print $2}'; "
            "if [ -f /tmp/agent.pid ]; then "
            "  kill -0 $(cat /tmp/agent.pid) 2>/dev/null && echo ALIVE || echo DEAD; "
            "else echo NOPID; fi",
            must_succeed=False,
        ).strip().splitlines()
        count = int(out[0]) if out and out[0].isdigit() else 0
        if len(out) > 1 and out[1].startswith(ws_root):
            active_run = out[1]
        agent_state = out[2] if len(out) > 2 else "NOPID"
        if count != last:
            print(f"  [poll] {count} experiments in {active_run.split('/')[-1]} "
                  f"(agent={agent_state})", flush=True)
            last = count
        if count >= n:
            return count, active_run
        if agent_state == "DEAD" and time.time() - start > 30:
            print(f"  [FAIL-FAST] agent died with only {count} experiments; "
                  "no point polling further", flush=True)
            break
        time.sleep(15)
    if time.time() >= deadline:
        print(f"  [TIMEOUT] {last} experiments after {timeout}s; dumping context...",
              flush=True)
    h.run("echo '--- /tmp/agent.log (last 200 lines) ---'; "
          "tail -200 /tmp/agent.log 2>&1 || echo '(no agent log)'",
          must_succeed=False, timeout=10)
    h.run(f"echo '--- {active_run} tree (active run with most experiments) ---'; "
          f"ls -laR {active_run} 2>&1 | head -80; "
          f"echo '--- all runs ---'; "
          f"ls -d {ws_root}/run_* 2>&1",
          must_succeed=False, timeout=10)
    h.run("echo '--- agent process state ---'; "
          "if [ -f /tmp/agent.pid ]; then "
          "  pid=$(cat /tmp/agent.pid); "
          "  ps -p $pid -o pid,stat,etime,cmd 2>&1 || echo '(agent exited)'; "
          "fi",
          must_succeed=False, timeout=10)
    elapsed = int(time.time() - start)
    raise AssertionError(
        f"only {last} experiments after {elapsed}s; expected {n} "
        f"(agent={agent_state})"
    )


def _parse_directive_id(direct_output: str) -> str:
    """`evo direct` prints: directive queued (id=<HEX>, fanout=N sessions)"""
    m = re.search(r"id=([A-F0-9]+)", direct_output)
    assert m, f"could not parse directive id from: {direct_output}"
    return m.group(1)


def _verify_directive_consumed(h: _Harness, run_dir: str, directive_id: str) -> int:
    """Count sessions whose offset matches the directive id."""
    out = h.run(
        f"grep -l '{directive_id}' {run_dir}/inject/offsets/*.json 2>/dev/null "
        f"| wc -l",
        must_succeed=False,
    ).strip()
    return int(out) if out.isdigit() else 0


def _read_graph(h: _Harness, run_dir: str) -> dict | None:
    out = h.run(f"cat {run_dir}/graph.json 2>/dev/null || echo '{{}}'",
                must_succeed=False)
    try:
        return json.loads(out)
    except json.JSONDecodeError:
        return None


def _read_best_score(h: _Harness, run_dir: str, metric: str = "max") -> float | None:
    """Best committed score across all nodes. Mirrors
    `evo.core.best_committed_score`."""
    graph = _read_graph(h, run_dir) or {}
    scores: list[float] = []
    for node in (graph.get("nodes") or {}).values():
        if node.get("status") != "committed" or node.get("score") is None:
            continue
        scores.append(float(node["score"]))
    if not scores:
        return None
    return max(scores) if metric == "max" else min(scores)


# ---------------------------------------------------------------------------
# Shared per-host driver: install → init → drive → inject → assert
# ---------------------------------------------------------------------------


def _drive_smoke(
    h: _Harness,
    *,
    host: str,
    install_steps: list[str],
    drive_cmd: str,
    env_keys: dict[str, str | None],
    inject_threshold: int = 2,
) -> None:
    """Common post-install flow shared by every host's test.

    ``install_steps`` and ``drive_cmd`` are host-specific (the bash that
    installs the host CLI + skills + plugin, and the bash that launches
    the agent). Everything else is identical: upload fixture, evo init,
    background-launch agent, wait for round-1 commits, evo direct,
    wait for completion, assert.
    """
    # Some install steps need API keys (e.g. `codex login --with-api-key`
    # reads OPENAI_API_KEY from stdin). Export env_keys before each step so
    # `printenv` / `$VAR` references inside install bash see them. Build
    # env_export once; reuse for drive_cmd below.
    env_export = "".join(f'export {k}="{v}"; ' for k, v in env_keys.items() if v)
    for step in install_steps:
        h.run(f"{env_export}{step}", timeout=600)

    h.upload_fixture_repo()
    ws_root = "/tmp/ws/.evo"
    # Harness's `evo init` lands in run_0000; some agents re-init and write
    # to run_0001 (observed on openclaw). The poll below counts across all
    # runs and returns the active one, so we don't hard-code run_0000.
    run_dir = f"{ws_root}/run_0000"
    h.run(
        f"export PATH=$HOME/.local/bin:$PATH; cd /tmp/ws && "
        # `evo run` executes from the main repo root, not the experiment
        # worktree. To benchmark the worktree's edited code, the command
        # must reference {worktree}/bench.py — otherwise Python imports
        # the main repo's target.py and reports the unchanged baseline.
        # See discover skill, "Critical rule" near line 171.
        # `-B` skips bytecode cache (avoids stale .pyc from earlier runs).
        f"evo init --name smoke --target target.py "
        f"--benchmark 'python3 -B {{worktree}}/bench.py' --metric max --host {host} "
        f"2>&1 | tail -2"
    )

    # Measure baseline BEFORE launching the agent — run the benchmark
    # against the unmodified fixture target.py. evo init doesn't store a
    # baseline score on the root node (root.score stays None), and using
    # "the agent's first committed experiment" as baseline breaks if the
    # agent goes straight to the optimal algorithm. Capturing the
    # untouched-code score here gives us a stable reference point.
    baseline_raw = h.run(
        "cd /tmp/ws && python3 -B bench.py", timeout=30,
    ).strip()
    try:
        baseline = float(json.loads(baseline_raw.splitlines()[-1])["score"])
    except (json.JSONDecodeError, KeyError, IndexError, ValueError) as e:
        raise AssertionError(
            f"[{host}] could not parse baseline from bench.py output:\n"
            f"  raw: {baseline_raw!r}\n  err: {e}"
        )
    print(f"  [baseline] {baseline} (from unmodified target.py)", flush=True)

    # Background: launch the agent. drive_cmd should write the bg pid to
    # /tmp/agent.pid and the agent log to /tmp/agent.log.
    # NOTE: must use `export VAR=value;` form, not the inline `VAR=value cmd`
    # prefix. drive_cmd typically starts with `export PATH=...; nohup ...`
    # — bash parses `ANTHROPIC_API_KEY=x export PATH=y` as scoping the key
    # to the `export` command only, and the subsequent nohup never sees it.
    # Using `export X=v;` outside the prefix puts the var into the shell's
    # environment and it propagates to nohup'd children.
    h.run(f"cd /tmp/ws && {env_export}{drive_cmd}", timeout=30)

    # Wait until N experiments commit (= round 1 done per the prompt).
    # Re-binds run_dir to the run the agent actually used (may differ from
    # the harness's run_0000 if the agent re-init'd evo).
    _, run_dir = _wait_for_n_experiments(h, ws_root, inject_threshold, timeout=900)

    # Inject an ADDITIVE directive: the prompt planned round 2 = just one
    # experiment C (sort + bisect, O(n log n)). The directive tells the
    # agent to ALSO run Experiment D (O(n) dict pass) on top of C, with a
    # unique tag embedded in D's committed target.py. The tag is how we
    # verify the directive's content reached and shaped round-2 work.
    import uuid as _uuid
    directive_tag = f"EVO_DIRECTIVE_{_uuid.uuid4().hex[:12]}"
    directive_text = (
        f"In addition to your planned round-2 Experiment C, also launch "
        f"Experiment D in parallel with C:\n"
        f"  - Experiment D: implement count_pairs as a single-pass "
        f"dictionary. For each x in xs: count += seen.get(target - x, 0); "
        f"then seen[x] = seen.get(x, 0) + 1. This is O(n).\n"
        f"D's target.py should include this line as the first non-import "
        f"statement (it's a build-tracking marker — the optimize skill "
        f"uses it for run accounting):\n"
        f"    _DIRECTIVE_TAG = \"{directive_tag}\"\n"
        f"Propagate this marker line into D's subagent brief so the "
        f"committed D variant has it."
    )
    direct_out = h.run(
        f"export PATH=$HOME/.local/bin:$PATH; cd /tmp/ws && "
        f"evo direct {_shell_quote(directive_text)}"
    )
    directive_id = _parse_directive_id(direct_out)

    # Wait for the agent to exit naturally (or timeout). 30 minutes upper
    # bound — round 2 should finish well within that.
    h.run(
        "for i in $(seq 1 60); do "
        "  if ! kill -0 $(cat /tmp/agent.pid) 2>/dev/null; then break; fi; "
        "  sleep 30; "
        "done",
        timeout=2000,
    )

    # ---------- assertions ----------
    n_experiments = int(h.run(f"ls {run_dir}/experiments/ | wc -l").strip())
    # The fixture prompt specifies 2 subagents × 2 rounds = 4 experiments.
    # >= (not ==) tolerates the rare case where a subagent's first evo run
    # fails inside budget and the same brief produces a sibling experiment.
    # Anything < 4 means round 2 didn't fire — the actual regression we care
    # about — and proves the mid-run inject + multi-round contract is broken.
    if n_experiments < 4:
        # On failure, dump the agent log and experiment dir for diagnosis.
        h.run("echo '--- /tmp/agent.log (last 200 lines) ---'; "
              "tail -200 /tmp/agent.log 2>&1 || echo '(no agent log)'",
              must_succeed=False, timeout=10)
        h.run(f"echo '--- experiment outcomes ---'; "
              f"for f in {run_dir}/experiments/exp_*/attempts/*/outcome.json; do "
              f"  echo \"=== $f ===\"; cat \"$f\" 2>/dev/null | head -20; "
              f"done",
              must_succeed=False, timeout=10)
    assert n_experiments >= 4, (
        f"[{host}] expected ≥4 experiments (2 subagents × 2 rounds), got "
        f"{n_experiments} — round 2 likely didn't fire"
    )

    consumed_by = _verify_directive_consumed(h, run_dir, directive_id)
    assert consumed_by >= 1, (
        f"[{host}] directive {directive_id} not consumed by any session; "
        f"mid-run inject did not reach the agent"
    )

    # The directive embedded a unique tag (_DIRECTIVE_TAG = "<uuid>") and
    # instructed Experiment D to include it in its committed target.py. Look
    # for at least one committed worktree containing the tag — that's the
    # deepest signal that the directive's CONTENT (not just the event id)
    # reached the subagent's reasoning and shaped its work.
    tag_count_str = h.run(
        f"grep -rl '{directive_tag}' {run_dir}/worktrees/*/target.py "
        f"2>/dev/null | wc -l",
        must_succeed=False,
    ).strip()
    tag_count = int(tag_count_str) if tag_count_str.isdigit() else 0
    if tag_count < 1:
        h.run("echo '--- /tmp/agent.log (last 300 lines) ---'; "
              "tail -300 /tmp/agent.log 2>&1 || echo '(no agent log)'",
              must_succeed=False, timeout=10)
        h.run(f"echo '--- experiment briefs/outcomes ---'; "
              f"for d in {run_dir}/experiments/exp_*; do "
              f"  echo \"=== $d ===\"; "
              f"  cat \"$d/brief.txt\" 2>/dev/null | head -30; echo; "
              f"  for a in \"$d/attempts/\"*/outcome.json; do "
              f"    echo \"-- $a --\"; cat \"$a\" 2>/dev/null | head -10; "
              f"  done; "
              f"done",
              must_succeed=False, timeout=20)
        h.run(f"echo '--- worktree target.py heads ---'; "
              f"for f in {run_dir}/worktrees/*/target.py; do "
              f"  echo \"=== $f ===\"; head -8 \"$f\" 2>/dev/null; "
              f"done",
              must_succeed=False, timeout=10)
    assert tag_count >= 1, (
        f"[{host}] directive's marker tag '{directive_tag}' found in "
        f"{tag_count} committed target.py files (expected ≥1 — Experiment D "
        f"per the directive). directive event arrived but the subagent did "
        f"not act on its content."
    )

    best = _read_best_score(h, run_dir)
    assert best is not None, f"[{host}] no committed score in graph.json"
    # baseline was measured above (before agent launch) by running bench.py
    # against the unmodified target.py — that's the true reference.
    ratio = best / baseline
    # Round-1 strategies (cache xs[i], itertools.combinations) are both
    # O(n²) — capped at constant-factor speedups, ~3-10x baseline in
    # practice. The directive's hash-map approach is O(n) — typically
    # 100x+ baseline. A ratio > 50 therefore means the agent applied
    # the directive (no other path reaches that range from the briefed
    # round-1 strategies).
    assert ratio > 50, (
        f"[{host}] best/baseline ratio {ratio:.1f}x (best={best:.1f}, "
        f"baseline={baseline:.1f}) suggests round-2 directive was not "
        f"applied — round-1 O(n²) strategies cap around ~10x baseline"
    )


def _shell_quote(s: str) -> str:
    """Single-quote a string for bash, escaping internal single quotes."""
    return "'" + s.replace("'", "'\\''") + "'"


def _read_prompt() -> str:
    return FIXTURE_PROMPT.read_text()


# ---------------------------------------------------------------------------
# Per-host tests
# ---------------------------------------------------------------------------


def test_opencode(sandbox_4g):
    """Opencode: official installer + npx skills + evo install opencode.
    Driver: ``opencode run --model openai/gpt-4.1-mini`` (single-shot;
    /optimize runs as nested tool calls inside one turn).

    Uses sandbox_4g — default 1GB sandbox OOM-kills opencode mid-run
    once the agent plus our chat.message plugin plus skill discovery
    are all loaded (observed: opencode dies with exit 137 after the
    DB migration, no further output).
    """
    openai_key = os.environ.get("OPENAI_API_KEY")
    if not openai_key:
        pytest.skip("OPENAI_API_KEY required for opencode")

    # evo-test-4g template doesn't ship nodejs; opencode itself is a static
    # binary but `npx skills add` (used to install skills) needs Node.
    sudo = sandbox_4g._sudo
    sandbox_4g.run(
        f"curl -fsSL https://deb.nodesource.com/setup_22.x | {sudo}bash - "
        "> /tmp/node-setup.log 2>&1",
        timeout=120,
    )
    sandbox_4g.run(f"{sudo}apt-get install -y nodejs >/dev/null", timeout=180)

    prompt = _shell_quote(_read_prompt())

    _drive_smoke(
        sandbox_4g,
        host="opencode",
        install_steps=[
            "curl -fsSL https://opencode.ai/install | bash > /tmp/opencode.log 2>&1",
            # Skills via the canonical cross-host CLI; opencode scans
            # ~/.agents/skills/ where this writes by default with -g.
            # npx skills accepts both `owner/repo` and local paths.
            f"export PATH=$HOME/.local/bin:$HOME/.opencode/bin:$PATH; "
            f"npx -y skills add {sandbox_4g.marketplace_source} --agent opencode -g -y",
            "export PATH=$HOME/.local/bin:$HOME/.opencode/bin:$PATH; "
            "evo install opencode",
        ],
        drive_cmd=(
            "export PATH=$HOME/.local/bin:$HOME/.opencode/bin:$PATH; "
            # gpt-4.1-mini on opencode misinterprets the "spawn N subagents"
            # line in optimize SKILL.md as `task(type="optimize", ...)` —
            # opencode rejects that ("Unknown agent type: optimize") and
            # the agent falls back to running experiments inline (no
            # `evo run`). Switching to gpt-5 since it follows skills more
            # strictly on codex; verifying same on opencode.
            f"nohup opencode run --model openai/gpt-5 {prompt} "
            "> /tmp/agent.log 2>&1 & echo $! > /tmp/agent.pid"
        ),
        env_keys={"OPENAI_API_KEY": openai_key},
    )


def test_claude_code(sandbox):
    """Claude Code: npm + claude plugin marketplace + claude plugin install.
    Driver: ``claude --print --dangerously-skip-permissions
    --model claude-haiku-4-5``. Runs as one print-mode turn with all tool
    calls (Bash, Edit, Task subagent spawns, etc.) inside it."""
    anthropic_key = os.environ.get("ANTHROPIC_API_KEY")
    if not anthropic_key:
        pytest.skip("ANTHROPIC_API_KEY required for claude-code")

    sudo = sandbox._sudo
    sandbox.run(
        f"curl -fsSL https://deb.nodesource.com/setup_22.x | {sudo}bash - "
        "> /tmp/node-setup.log 2>&1",
        timeout=120,
    )
    sandbox.run(f"{sudo}apt-get install -y nodejs >/dev/null", timeout=180)
    sandbox.run(
        f"{sudo}npm install -g @anthropic-ai/claude-code > /tmp/cc.log 2>&1",
        timeout=300,
    )

    prompt = _shell_quote(_read_prompt())
    _drive_smoke(
        sandbox,
        host="claude-code",
        install_steps=[
            f"claude plugin marketplace add {sandbox.marketplace_source} 2>&1 | tail -3",
            "claude plugin install evo@evo-hq-evo 2>&1 | tail -3",
        ],
        drive_cmd=(
            "export PATH=$HOME/.local/bin:$PATH; "
            f"nohup claude --print --dangerously-skip-permissions "
            f"--model claude-haiku-4-5 --max-budget-usd 5.0 {prompt} "
            "> /tmp/agent.log 2>&1 & echo $! > /tmp/agent.pid"
        ),
        env_keys={"ANTHROPIC_API_KEY": anthropic_key},
    )


def test_codex(sandbox):
    """Codex: npm + codex plugin marketplace + evo install codex.
    Driver: ``codex exec --dangerously-bypass-approvals-and-sandbox``.
    The bypass flag is appropriate for the e2b sandbox (already isolated)
    and required for non-interactive runs that touch files / shell."""
    openai_key = os.environ.get("OPENAI_API_KEY")
    if not openai_key:
        pytest.skip("OPENAI_API_KEY required for codex")

    sudo = sandbox._sudo
    sandbox.run(
        f"curl -fsSL https://deb.nodesource.com/setup_22.x | {sudo}bash - "
        "> /tmp/node-setup.log 2>&1",
        timeout=120,
    )
    sandbox.run(f"{sudo}apt-get install -y nodejs >/dev/null", timeout=180)
    sandbox.run(
        f"{sudo}npm install -g @openai/codex > /tmp/codex.log 2>&1",
        timeout=300,
    )

    # Each host has its own skill-invocation dialect. claude-code reads
    # `/evo:optimize`, codex reads `$evo:optimize`, and the others
    # natural-language-match. Prepending the host's native mention to a
    # host-agnostic prompt body is just translation, not protocol leakage.
    # Without it, codex's gpt-5-mini reads the round-1/round-2 specs as
    # actionable shell tasks and skips the skill entirely.
    prompt = _shell_quote("$evo:optimize\n\n" + _read_prompt())
    # In local-source mode, point `evo install codex` at the marketplace
    # root (contains .claude-plugin/marketplace.json) — it drives codex's
    # `plugin/install` RPC against that marketplace.json. PyPI/GitHub mode
    # uses the cache populated by `codex plugin marketplace add evo-hq/evo`
    # automatically (no --from-path needed).
    # --trust-hooks is the opt-in flag that auto-writes the
    # [hooks.state] entries codex's TUI would write on `/hooks` Trust.
    # Without it, codex installs evo's hooks in `untrusted` state and
    # they never fire — `evo direct` mid-run directives silently drop.
    # The release-smoke test asserts directive consumption, so we need
    # the trust step; a real user can skip the flag and trust via TUI.
    evo_install_codex_args = (
        "--from-path /tmp/evo-local-repo --trust-hooks"
        if sandbox.marketplace_source.startswith("/")
        else "--trust-hooks"
    )
    _drive_smoke(
        sandbox,
        host="codex",
        install_steps=[
            # codex does not honor $OPENAI_API_KEY for the Responses
            # websocket — it requires `~/.codex/auth.json`. `codex login
            # --with-api-key` reads the key from stdin and writes that file.
            "printenv OPENAI_API_KEY | codex login --with-api-key 2>&1 | tail -3",
            f"codex plugin marketplace add {sandbox.marketplace_source} 2>&1 | tail -3",
            f"export PATH=$HOME/.local/bin:$PATH; evo install codex "
            f"{evo_install_codex_args}",
        ],
        drive_cmd=(
            "export PATH=$HOME/.local/bin:$PATH; "
            # gpt-5-mini reads detailed prompts as actionable and skips
            # the optimize skill's protocol (writes loose `target_a.py`
            # files instead of `evo new` + `evo run`). gpt-5 (full)
            # respects skill mentions more reliably.
            f"nohup codex exec --dangerously-bypass-approvals-and-sandbox "
            f"--model gpt-5 {prompt} "
            "> /tmp/agent.log 2>&1 & echo $! > /tmp/agent.pid"
        ),
        env_keys={"OPENAI_API_KEY": openai_key},
    )


def test_hermes(sandbox):
    """Hermes: official installer + ``hermes skills install ... -y`` per skill
    + ``evo install hermes``. Driver: ``hermes chat -q ... -Q``.
    Single-turn; agent does multiple tool calls including subagent
    delegation inside that turn.

    Uses anthropic provider: hermes's `openai-codex` provider requires a
    ChatGPT-account access token, not a plain OPENAI_API_KEY (returns
    401 Unauthorized when handed a `sk-proj-...` key). Falling back to
    anthropic since hermes accepts ANTHROPIC_API_KEY directly there.
    """
    anthropic_key = os.environ.get("ANTHROPIC_API_KEY")
    if not anthropic_key:
        pytest.skip("ANTHROPIC_API_KEY required for hermes")

    sandbox.run(
        "curl -fsSL "
        "https://raw.githubusercontent.com/NousResearch/hermes-agent/main/scripts/install.sh "
        "| bash > /tmp/hermes.log 2>&1",
        timeout=900,
    )

    skills = ["discover", "optimize", "subagent", "infra-setup"]
    # NOTE: hermes skills install takes a github org/repo/path identifier
    # only — no local-path support documented. So in local-source mode,
    # hermes skills come from github.com/evo-hq/evo's default branch
    # regardless of EVO_RELEASE_SMOKE_SOURCE=local. Only the evo CLI
    # itself is local in that mode. Worth being aware of when a hermes
    # release-smoke pass hides a local skill change.
    install_skill_steps = [
        f"export PATH=$HOME/.local/bin:$PATH; "
        f"hermes skills install evo-hq/evo/plugins/evo/skills/{s} -y "
        f"{'--force' if s == 'discover' else ''} 2>&1 | tail -3"
        for s in skills
    ]

    # In local-source mode, `evo install hermes` must pip-install evo into
    # hermes's venv from the local plugin tarball. Without --from-path it
    # falls back to PyPI's evo-hq-cli, which (until 0.4.0 publishes) is
    # 0.3.3 — missing the `hermes_agent.plugins` entry-point — and the
    # install adapter's post-check fails with "entry-point not registered".
    evo_install_hermes_args = (
        "--from-path /tmp/evo-local-repo/plugins/evo"
        if sandbox.marketplace_source.startswith("/")
        else ""
    )

    prompt = _shell_quote(_read_prompt())
    _drive_smoke(
        sandbox,
        host="hermes",
        install_steps=[
            *install_skill_steps,
            f"export PATH=$HOME/.local/bin:$PATH; evo install hermes "
            f"{evo_install_hermes_args}",
            # Register anthropic credentials in hermes's auth store
            # (~/.hermes/auth.json). Required before `hermes chat
            # --provider anthropic` will route the request.
            'hermes auth add anthropic --type api-key '
            '--api-key "$ANTHROPIC_API_KEY" 2>&1 | tail -3',
        ],
        drive_cmd=(
            "export PATH=$HOME/.local/bin:$PATH; "
            # claude-haiku-4-5 follows the directive (creates exp_D with
            # the marker tag) but implements the O(n) hash-map
            # incorrectly — actual score caps at ~16x baseline instead
            # of the expected 100x+, tripping the score-ratio assertion.
            # claude-sonnet-4-5 produces a correct O(n) implementation.
            f"nohup hermes chat -q {prompt} -Q --provider anthropic "
            f"-m anthropic/claude-sonnet-4-5 "
            "> /tmp/agent.log 2>&1 & echo $! > /tmp/agent.pid"
        ),
        env_keys={"ANTHROPIC_API_KEY": anthropic_key},
    )


@pytest.fixture
def sandbox_4g(fixture_repo_tarball, evo_local_tarball):
    """Larger sandbox for openclaw (default 1GB OOMs `npm install -g openclaw`).
    Requires the ``evo-test-4g`` template — build once via:

        Template.build(Template().from_ubuntu_image('22.04'),
                       name='evo-test-4g', memory_mb=4096, cpu_count=2)
    """
    _gate_release_smoke()
    from e2b import Sandbox
    sbx = Sandbox.create(template="evo-test-4g", timeout=1800)
    h = _Harness(sbx, fixture_repo_tarball, evo_local_tarball)
    try:
        h.install_base_deps()
        yield h
    finally:
        try:
            sbx.kill()
        except Exception:  # noqa: BLE001
            pass


def test_openclaw(sandbox_4g):
    """OpenClaw: npm + openclaw plugins install + evo install openclaw.
    Driver: ``openclaw agent --local --message ... --model anthropic/...``.
    pi-coding-agent under the hood; defaults to anthropic models."""
    anthropic_key = os.environ.get("ANTHROPIC_API_KEY")
    if not anthropic_key:
        pytest.skip("ANTHROPIC_API_KEY required for openclaw")

    sudo = sandbox_4g._sudo
    sandbox_4g.run(
        f"curl -fsSL https://deb.nodesource.com/setup_22.x | {sudo}bash - "
        "> /tmp/node-setup.log 2>&1",
        timeout=120,
    )
    sandbox_4g.run(f"{sudo}apt-get install -y nodejs >/dev/null", timeout=180)
    sandbox_4g.run(
        f"{sudo}npm install -g openclaw > /tmp/openclaw.log 2>&1",
        timeout=600,
    )

    prompt = _shell_quote(_read_prompt())
    _drive_smoke(
        sandbox_4g,
        host="openclaw",
        install_steps=[
            # Bind the main agent's workspace to /tmp/ws. Two reasons:
            # (1) the default ~/.openclaw/workspace is empty, so the agent
            # would find no target.py and improvise; (2) `evo install
            # openclaw` hard-codes the pi-extension registration to main's
            # settings.json — using a different agent loses the inject-drain
            # hook, breaking mid-run directive delivery.
            # `mkdir -p /tmp/ws` is needed because install_steps run BEFORE
            # upload_fixture_repo creates it. `rm -rf` is defensive in case
            # the workspace dir already exists.
            "mkdir -p /tmp/ws ~/.openclaw && "
            "rm -rf ~/.openclaw/workspace && "
            "ln -s /tmp/ws ~/.openclaw/workspace",
            # `openclaw plugins install --marketplace` accepts local paths
            # ("local repo/path") in addition to git URLs — see openclaw help.
            f"openclaw plugins install evo --marketplace "
            f"{sandbox_4g.marketplace_source if sandbox_4g.marketplace_source.startswith('/') else 'https://github.com/evo-hq/evo'} "
            "2>&1 | tail -3",
            "export PATH=$HOME/.local/bin:$PATH; evo install openclaw",
        ],
        drive_cmd=(
            "export PATH=$HOME/.local/bin:$PATH; "
            # `--agent main` is the default agent installed by `evo install
            # openclaw`, which also registers the pi-extension that powers
            # mid-run inject. main's workspace is symlinked to /tmp/ws above.
            # claude-sonnet-4-5: opus-4-7 flags the synthetic user-message
            # directive as a prompt-injection attempt (verbatim in its
            # output: "fake '[evo direct] NEW REQUIREMENT FROM USER'
            # messages — I ignored both") and refuses to act on it.
            # Sonnet treats the appended message as a normal user turn
            # and follows it, including propagating the marker tag to D.
            f"nohup openclaw agent --local --agent main "
            f"--model anthropic/claude-sonnet-4-5 --message {prompt} "
            "> /tmp/agent.log 2>&1 & echo $! > /tmp/agent.pid"
        ),
        env_keys={"ANTHROPIC_API_KEY": anthropic_key},
    )

"""Live test: `evo run` an experiment inside a real E2B sandbox.

Skipped unless BOTH `EVO_LIVE_TEST_E2B=1` AND `E2B_API_KEY` are set.
Requires the optional `e2b` SDK.
"""
from __future__ import annotations

import json
import os
import signal
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
PLUGIN_ROOT = REPO_ROOT / "plugins" / "evo"
PLUGIN_SRC = PLUGIN_ROOT / "src"
sys.path.insert(0, str(PLUGIN_SRC))


def _gate() -> None:
    if os.environ.get("EVO_LIVE_TEST_E2B") != "1":
        print("SKIPPED (set EVO_LIVE_TEST_E2B=1 to enable)")
        sys.exit(0)
    if not os.environ.get("E2B_API_KEY"):
        print("SKIPPED (set E2B_API_KEY to enable)")
        sys.exit(0)
    try:
        import e2b  # noqa: F401
    except ImportError:
        print("SKIPPED (e2b SDK not installed)")
        sys.exit(0)


def _evo(args: list[str], cwd: Path, check: bool = True, timeout: int = 600):
    result = subprocess.run(
        ["uv", "run", "--project", str(PLUGIN_ROOT), "evo", *args],
        cwd=cwd, check=False, capture_output=True, text=True, timeout=timeout,
    )
    if check and result.returncode != 0:
        raise RuntimeError(
            f"evo {' '.join(args)} failed (rc={result.returncode}):\n"
            f"STDOUT:\n{result.stdout}\nSTDERR:\n{result.stderr}"
        )
    return result


def _build_repo(workdir: Path) -> Path:
    repo = workdir / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=repo, check=True)
    (repo / "agent.py").write_text("STATE = 'baseline'\n", encoding="utf-8")
    (repo / "eval.py").write_text(
        "import os, json\n"
        "from pathlib import Path\n"
        "result_path = os.environ['EVO_RESULT_PATH']\n"
        "Path(result_path).parent.mkdir(parents=True, exist_ok=True)\n"
        "Path(result_path).write_text(json.dumps({'score': 1.0, 'tasks': {}}))\n"
        "print(json.dumps({'score': 1.0}))\n",
        encoding="utf-8",
    )
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "baseline"], cwd=repo, check=True)
    return repo


def test_evo_run_against_e2b() -> None:
    workdir = Path(tempfile.mkdtemp(prefix="evo-e2b-e2e-"))
    repo = _build_repo(workdir)

    try:
        provider_config = (
            "template=base,"
            "timeout_seconds=300,"
            "health_timeout_seconds=90.0"
        )
        _evo(
            ["init", "--target", "agent.py",
             "--benchmark", "python eval.py",
             "--metric", "max", "--host", "generic", "--per-exp-timeout", "1800"],
            cwd=repo,
        )
        print("--- evo init OK ---")

        t0 = time.monotonic()
        out = _evo(
            ["new", "--parent", "root", "-m", "e2b e2e baseline",
             "--remote", "e2b",
             "--provider-config", provider_config],
            cwd=repo,
            timeout=300,
        )
        print(f"--- evo new exp_0000 (provisions E2B sandbox): {time.monotonic() - t0:.1f}s ---")
        print(out.stdout.strip())

        t0 = time.monotonic()
        run_out = _evo(["run", "exp_0000"], cwd=repo, timeout=300)
        print(f"--- evo run exp_0000: {time.monotonic() - t0:.1f}s ---")
        print(run_out.stdout.strip())
        assert "COMMITTED exp_0000 1.0" in run_out.stdout, run_out.stdout

        graph = json.loads(
            (repo / ".evo" / "run_0000" / "graph.json").read_text(encoding="utf-8")
        )
        commit_sha = graph["nodes"]["exp_0000"]["commit"]
        assert commit_sha, graph["nodes"]["exp_0000"]
        local_check = subprocess.run(
            ["git", "cat-file", "-e", commit_sha],
            cwd=repo,
            capture_output=True,
        )
        assert local_check.returncode == 0, commit_sha

        attempt_001 = (
            repo / ".evo" / "run_0000" / "experiments" / "exp_0000" / "attempts" / "001"
        )
        result = json.loads((attempt_001 / "result.json").read_text(encoding="utf-8"))
        assert result.get("score") == 1.0, result
        print("--- result.json fetched OK ---")
    finally:
        print("--- backstop cleanup ---")
        _evo(["reset", "--yes"], cwd=repo, check=False)
        shutil.rmtree(workdir, ignore_errors=True)


def test_e2b_recovers_after_orchestrator_death() -> None:
    """Kill local evo mid-benchmark; rerun should reattach to the E2B process."""
    workdir = Path(tempfile.mkdtemp(prefix="evo-e2b-recover-"))
    repo = workdir / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=repo, check=True)
    (repo / "agent.py").write_text("STATE = 'baseline'\n", encoding="utf-8")
    (repo / "eval.py").write_text(
        "import json, os, time\n"
        "from pathlib import Path\n"
        "traces = Path(os.environ['EVO_TRACES_DIR'])\n"
        "traces.mkdir(parents=True, exist_ok=True)\n"
        "ckpt = Path(os.environ['EVO_CHECKPOINT_DIR'])\n"
        "ckpt.mkdir(parents=True, exist_ok=True)\n"
        "for i in range(5):\n"
        "    (traces / f'task_{i}.json').write_text(json.dumps({'task_id': str(i), 'score': 1.0}))\n"
        "    (ckpt / 'progress.json').write_text(json.dumps({'step': i}))\n"
        "    print(f'tick-{i}', flush=True)\n"
        "    time.sleep(1)\n"
        "Path(os.environ['EVO_RESULT_PATH']).write_text(json.dumps({'score': 1.0, 'tasks': {str(i): 1.0 for i in range(5)}}))\n",
        encoding="utf-8",
    )
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "recover fixture"], cwd=repo, check=True)

    try:
        provider_config = (
            "template=base,"
            "timeout_seconds=300,"
            "health_timeout_seconds=90.0"
        )
        _evo(
            ["init", "--target", "agent.py",
             "--benchmark", "python eval.py",
             "--metric", "max", "--host", "generic", "--per-exp-timeout", "1800"],
            cwd=repo,
        )
        _evo(
            ["new", "--parent", "root", "-m", "e2b recovery",
             "--remote", "e2b",
             "--provider-config", provider_config],
            cwd=repo,
            timeout=300,
        )

        proc = subprocess.Popen(
            ["uv", "run", "--project", str(PLUGIN_ROOT), "evo", "run", "exp_0000"],
            cwd=repo,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
        )
        journal_path = (
            repo / ".evo" / "run_0000" / "experiments" / "exp_0000"
            / "attempts" / "001" / "benchmark.log.remote.json"
        )
        deadline = time.monotonic() + 60.0
        while time.monotonic() < deadline and not journal_path.exists():
            if proc.poll() is not None:
                stdout, stderr = proc.communicate(timeout=10)
                raise AssertionError(
                    f"evo run exited before recovery test could interrupt it:\n"
                    f"stdout:\n{stdout}\n\nstderr:\n{stderr}"
                )
            time.sleep(0.25)
        assert journal_path.exists(), "remote benchmark journal was not created"
        os.killpg(proc.pid, signal.SIGTERM)
        try:
            proc.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            os.killpg(proc.pid, signal.SIGKILL)
            proc.communicate(timeout=10)

        graph = json.loads((repo / ".evo" / "run_0000" / "graph.json").read_text(encoding="utf-8"))
        assert graph["nodes"]["exp_0000"]["status"] == "active", graph
        assert graph["nodes"]["exp_0000"]["current_attempt"] == 1, graph

        time.sleep(5.0)
        rerun = _evo(["run", "exp_0000"], cwd=repo, check=False, timeout=300)
        assert rerun.returncode == 0, (rerun.stdout, rerun.stderr)
        assert "RECOVERING exp_0000 attempt=1" in rerun.stdout, rerun.stdout
        assert "COMMITTED exp_0000 1.0" in rerun.stdout, rerun.stdout

        attempts_root = repo / ".evo" / "run_0000" / "experiments" / "exp_0000" / "attempts"
        assert sorted(p.name for p in attempts_root.iterdir()) == ["001"]
        outcome = json.loads((attempts_root / "001" / "outcome.json").read_text(encoding="utf-8"))
        assert outcome["outcome"] == "committed", outcome
        assert outcome["attempt_state"]["status"] == "committed", outcome
        assert (attempts_root / "001" / "checkpoints" / "progress.json").exists()
    finally:
        print("--- backstop cleanup ---")
        _evo(["reset", "--yes"], cwd=repo, check=False)
        shutil.rmtree(workdir, ignore_errors=True)


def main() -> None:
    _gate()
    print("=== E2B single-experiment end-to-end ===")
    test_evo_run_against_e2b()
    print("=== E2B orchestrator-crash recovery ===")
    test_e2b_recovers_after_orchestrator_death()
    print("LIVE E2B E2E OK")


if __name__ == "__main__":
    main()

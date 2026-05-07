"""Live test: `evo run` an experiment inside a real Azure VM.

Skipped unless `EVO_LIVE_TEST_AZURE=1` and Azure auth is available.
Requires the optional Azure SDK extra.
"""
from __future__ import annotations

import json
import os
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
    if os.environ.get("EVO_LIVE_TEST_AZURE") != "1":
        print("SKIPPED (set EVO_LIVE_TEST_AZURE=1 to enable)")
        sys.exit(0)
    if not _azure_auth_present():
        print("SKIPPED (Azure auth not available; run `az login` or set Azure env creds)")
        sys.exit(0)
    try:
        import azure.identity  # noqa: F401
        import azure.mgmt.compute  # noqa: F401
        import azure.mgmt.network  # noqa: F401
        import azure.mgmt.resource  # noqa: F401
    except ImportError:
        print("SKIPPED (Azure SDK not installed)")
        sys.exit(0)


def _azure_auth_present() -> bool:
    if any(
        os.environ.get(name)
        for name in (
            "AZURE_CLIENT_ID",
            "AZURE_CLIENT_SECRET",
            "AZURE_TENANT_ID",
            "ARM_CLIENT_ID",
            "ARM_CLIENT_SECRET",
            "ARM_TENANT_ID",
        )
    ):
        return True
    proc = subprocess.run(
        ["az", "account", "show"],
        capture_output=True,
        text=True,
        check=False,
    )
    return proc.returncode == 0


def _subscription_id() -> str:
    if os.environ.get("AZURE_SUBSCRIPTION_ID"):
        return os.environ["AZURE_SUBSCRIPTION_ID"]
    proc = subprocess.run(
        ["az", "account", "show", "--query", "id", "-o", "tsv"],
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0 or not proc.stdout.strip():
        raise RuntimeError("could not resolve Azure subscription id from current az login")
    return proc.stdout.strip()


def _evo(args: list[str], cwd: Path, check: bool = True, timeout: int = 1200):
    result = subprocess.run(
        ["uv", "run", "--project", str(PLUGIN_ROOT), "evo", *args],
        cwd=cwd,
        check=False,
        capture_output=True,
        text=True,
        timeout=timeout,
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


def _generate_keypair(path: Path) -> None:
    subprocess.run(
        ["ssh-keygen", "-t", "rsa", "-b", "2048", "-N", "", "-f", str(path)],
        check=True,
        capture_output=True,
        text=True,
    )


def test_evo_run_against_azure() -> None:
    workdir = Path(tempfile.mkdtemp(prefix="evo-azure-e2e-"))
    repo = _build_repo(workdir)
    key_path = workdir / "azure-evo-key"
    _generate_keypair(key_path)

    resource_group = f"evo-test-{int(time.time())}"
    provider_config = ",".join(
        [
            f"subscription_id={_subscription_id()}",
            f"resource_group={resource_group}",
            "location=westus2",
            "vm_size=Standard_D2s_v3",
            "image=Canonical:0001-com-ubuntu-server-jammy:22_04-lts:latest",
            f"key={key_path}",
            "ssh_user=azureuser",
            "health_timeout_seconds=180.0",
        ]
    )

    try:
        _evo(
            [
                "init",
                "--target",
                "agent.py",
                "--benchmark",
                "python3 eval.py",
                "--metric",
                "max",
                "--host",
                "generic",
            ],
            cwd=repo,
        )
        print("--- evo init OK ---")

        t0 = time.monotonic()
        out = _evo(
            [
                "new",
                "--parent",
                "root",
                "-m",
                "azure e2e baseline",
                "--remote",
                "azure",
                "--provider-config",
                provider_config,
            ],
            cwd=repo,
            timeout=1200,
        )
        print(f"--- evo new exp_0000 (provisions Azure VM): {time.monotonic() - t0:.1f}s ---")
        print(out.stdout.strip())

        t0 = time.monotonic()
        run_out = _evo(["run", "exp_0000"], cwd=repo, timeout=1200)
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
        print("--- commit round-trip OK ---")
    finally:
        print("--- backstop cleanup ---")
        _evo(["reset", "--yes"], cwd=repo, check=False, timeout=1200)
        subprocess.run(
            ["az", "group", "delete", "--name", resource_group, "--yes", "--no-wait"],
            capture_output=True,
            text=True,
            check=False,
        )
        shutil.rmtree(workdir, ignore_errors=True)


def main() -> None:
    _gate()
    print("=== Azure single-experiment end-to-end ===")
    test_evo_run_against_azure()
    print("LIVE AZURE E2E OK")


if __name__ == "__main__":
    main()

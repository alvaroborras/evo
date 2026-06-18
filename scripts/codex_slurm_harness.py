#!/usr/bin/env python3
"""Run a Codex/evo behavior repro against a cheap fake-Slurm cluster.

This is a live agent harness, not a unit test. It builds a disposable repo,
installs the local evo plugin into an isolated CODEX_HOME, exposes fake Slurm
commands on PATH, then asks `codex exec` to drive `$evo optimize`.

The fixture is intentionally shaped around Jacob's failure modes:

- direct `python bench.py` fails outside a Slurm job;
- the configured evo benchmark submits via `sbatch` and polls with
  `squeue`/`sacct`;
- fake Slurm records every scheduler command to JSONL for post-run audit;
- the repo can be inspected for ad-hoc launcher scripts or root edits.

Use:

    python scripts/codex_slurm_harness.py run --scenario natural
    python scripts/codex_slurm_harness.py analyze /tmp/evo-codex-slurm-.../run
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import textwrap
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[1]
PLUGIN_ROOT = REPO_ROOT / "plugins" / "evo"


TARGET_PY = '''\
"""Tiny optimization target for the Codex Slurm behavior harness."""

QUALITY = 1.0


def candidate_score() -> float:
    """Return the score multiplier the benchmark uses.

    Agents can improve this fixture by increasing QUALITY above 1.0. The
    benchmark is deliberately simple because this harness tests orchestration
    behavior, not algorithmic skill.
    """
    return float(QUALITY)
'''


BENCH_PY = '''\
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from target import candidate_score


def _append_event(kind: str, payload: dict) -> None:
    root = os.environ.get("FAKE_SLURM_ROOT")
    if not root:
        return
    path = Path(root) / "events.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    record = {"ts": time_time(), "kind": kind, **payload}
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, sort_keys=True) + "\\n")


def time_time() -> float:
    import time
    return round(time.time(), 3)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--result", required=True)
    args = parser.parse_args()

    job_id = os.environ.get("FAKE_SLURM_JOB_ID") or os.environ.get("SLURM_JOB_ID")
    if not job_id:
        _append_event("direct_benchmark_run", {"cwd": os.getcwd(), "argv": sys.argv})
        print("bench.py must run inside a Slurm job", file=sys.stderr)
        return 7

    sleep_seconds = float(os.environ.get("FAKE_BENCH_SLEEP_SECONDS", "0"))
    if sleep_seconds > 0:
        import time
        time.sleep(sleep_seconds)

    score = candidate_score()
    result = {
        "score": score,
        "job_id": job_id,
        "cuda_visible_devices": os.environ.get("CUDA_VISIBLE_DEVICES", ""),
    }
    Path(args.result).write_text(json.dumps(result, sort_keys=True) + "\\n")
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
'''


SLURM_EVAL_PY = '''\
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path


def run(cmd: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, cwd=cwd, text=True, capture_output=True, check=False)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--worktree", required=True)
    parser.add_argument("--timeout", type=float, default=45.0)
    args = parser.parse_args()

    worktree = Path(args.worktree).resolve()
    out_dir = worktree / ".slurm"
    out_dir.mkdir(exist_ok=True)
    result_path = out_dir / "result.json"
    job_script = out_dir / "job.sh"
    stdout_path = out_dir / "job-%j.out"
    stderr_path = out_dir / "job-%j.err"

    job_script.write_text(
        "#!/usr/bin/env bash\\n"
        "set -euo pipefail\\n"
        f"cd {str(worktree)!r}\\n"
        f"{sys.executable} bench.py --result {str(result_path)!r}\\n"
    )
    job_script.chmod(0o755)

    submit = run(
        [
            "sbatch",
            "--parsable",
            "--job-name=evo-fixture",
            "--gres=gpu:1",
            f"--output={stdout_path}",
            f"--error={stderr_path}",
            str(job_script),
        ],
        cwd=worktree,
    )
    if submit.returncode != 0:
        print(submit.stderr or submit.stdout, file=sys.stderr)
        return submit.returncode or 1
    job_id = submit.stdout.strip().splitlines()[-1].strip()
    if not job_id:
        print("sbatch returned no job id", file=sys.stderr)
        return 2

    deadline = time.time() + args.timeout
    state = "UNKNOWN"
    while time.time() < deadline:
        sq = run(["squeue", "-h", "-j", job_id, "-o", "%T"], cwd=worktree)
        running_state = sq.stdout.strip()
        if running_state:
            state = running_state.splitlines()[-1].strip()
            time.sleep(0.2)
            continue
        acct = run(["sacct", "-n", "-P", "-j", job_id, "-o", "State,ExitCode"], cwd=worktree)
        fields = [p for p in acct.stdout.strip().split("|") if p]
        if fields:
            state = fields[0]
            break
        time.sleep(0.2)

    if state not in {"COMPLETED", "COMPLETING"}:
        print(f"Slurm job {job_id} did not complete: state={state}", file=sys.stderr)
        return 3
    if not result_path.exists():
        print(f"Slurm job {job_id} completed without result.json", file=sys.stderr)
        return 4

    print(result_path.read_text().strip())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
'''


PROJECT_MD = '''\
# Project Understanding

This is the Codex Slurm behavior harness. The optimization target is
`target.py`; higher `candidate_score()` is better.
Scores above 1.0 are valid; increasing `QUALITY` in `target.py` improves the
benchmark score.

Resource profile:

- Binding resource: Slurm GPU jobs.
- The cluster exposes two GPU slots.
- The only valid benchmark path is the configured evo benchmark,
  `python slurm_eval.py --worktree {worktree}`.
- `slurm_eval.py` submits one `sbatch --gres=gpu:1` job and polls with
  `squeue`/`sacct`.
- Running `python bench.py` directly is invalid and intentionally fails unless
  it is inside a Slurm job.

Orchestration expectations:

- Use `$evo optimize` / evo CLI lifecycle commands.
- The orchestrator should write briefs and delegate code edits/runs to
  subagents.
- Do not create ad-hoc launcher or monitor scripts in the repo root.
- Do not manually set `CUDA_VISIBLE_DEVICES=0`; Slurm assigns the device.
'''


NOISY_TARGET_PY = '''\
"""Noisy optimization target for grouped-replicate behavior tests."""

QUALITY = 1.0
STABLE_GAIN = False
NOISY_SPIKE = False


def candidate_score(replicate: int = 1) -> float:
    """Return a deterministic noisy score for the selected candidate.

    `STABLE_GAIN` is the candidate that should win under n=3 median.
    `NOISY_SPIKE` looks best on a single lucky replicate but loses by median.
    """
    if STABLE_GAIN and NOISY_SPIKE:
        return 0.6
    if STABLE_GAIN:
        return 1.15
    if NOISY_SPIKE:
        return 1.8 if int(replicate) == 1 else 0.7
    return float(QUALITY)
'''


NOISY_BENCH_PY = '''\
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from target import candidate_score


def _append_event(kind: str, payload: dict) -> None:
    root = os.environ.get("FAKE_SLURM_ROOT")
    if not root:
        return
    path = Path(root) / "events.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    record = {"ts": time_time(), "kind": kind, **payload}
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, sort_keys=True) + "\\n")


def time_time() -> float:
    import time
    return round(time.time(), 3)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--result", required=True)
    args = parser.parse_args()

    job_id = os.environ.get("FAKE_SLURM_JOB_ID") or os.environ.get("SLURM_JOB_ID")
    if not job_id:
        _append_event("direct_benchmark_run", {"cwd": os.getcwd(), "argv": sys.argv})
        print("bench.py must run inside a Slurm job", file=sys.stderr)
        return 7

    sleep_seconds = float(os.environ.get("FAKE_BENCH_SLEEP_SECONDS", "0"))
    if sleep_seconds > 0:
        import time
        time.sleep(sleep_seconds)

    replicate = int(os.environ.get("NOISY_REPLICATE_INDEX", "1"))
    score = candidate_score(replicate)
    result = {
        "score": score,
        "replicate": replicate,
        "job_id": job_id,
        "cuda_visible_devices": os.environ.get("CUDA_VISIBLE_DEVICES", ""),
    }
    Path(args.result).write_text(json.dumps(result, sort_keys=True) + "\\n")
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
'''


NOISY_SLURM_EVAL_PY = '''\
from __future__ import annotations

import argparse
import json
import os
import statistics
import subprocess
import sys
import time
from pathlib import Path


def run(cmd: list[str], cwd: Path, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, cwd=cwd, env=env, text=True, capture_output=True, check=False)


def run_one(worktree: Path, out_dir: Path, replicate: int, timeout: float) -> dict:
    result_path = out_dir / f"replicate-{replicate}.json"
    job_script = out_dir / f"job-{replicate}.sh"
    stdout_path = out_dir / f"job-{replicate}-%j.out"
    stderr_path = out_dir / f"job-{replicate}-%j.err"

    job_script.write_text(
        "#!/usr/bin/env bash\\n"
        "set -euo pipefail\\n"
        f"cd {str(worktree)!r}\\n"
        f"NOISY_REPLICATE_INDEX={replicate} {sys.executable} bench.py --result {str(result_path)!r}\\n"
    )
    job_script.chmod(0o755)

    submit = run(
        [
            "sbatch",
            "--parsable",
            "--job-name=evo-noisy-fixture",
            "--gres=gpu:1",
            f"--output={stdout_path}",
            f"--error={stderr_path}",
            str(job_script),
        ],
        cwd=worktree,
    )
    if submit.returncode != 0:
        raise RuntimeError(submit.stderr or submit.stdout or f"sbatch failed: {submit.returncode}")
    job_id = submit.stdout.strip().splitlines()[-1].strip()
    if not job_id:
        raise RuntimeError("sbatch returned no job id")

    deadline = time.time() + timeout
    state = "UNKNOWN"
    while time.time() < deadline:
        sq = run(["squeue", "-h", "-j", job_id, "-o", "%T"], cwd=worktree)
        running_state = sq.stdout.strip()
        if running_state:
            state = running_state.splitlines()[-1].strip()
            time.sleep(0.2)
            continue
        acct = run(["sacct", "-n", "-P", "-j", job_id, "-o", "State,ExitCode"], cwd=worktree)
        fields = [p for p in acct.stdout.strip().split("|") if p]
        if fields:
            state = fields[0]
            break
        time.sleep(0.2)

    if state not in {"COMPLETED", "COMPLETING"}:
        raise RuntimeError(f"Slurm job {job_id} did not complete: state={state}")
    if not result_path.exists():
        raise RuntimeError(f"Slurm job {job_id} completed without result.json")
    return json.loads(result_path.read_text())


def aggregate(scores: list[float], mode: str) -> float:
    if mode == "median":
        return float(statistics.median(scores))
    if mode == "mean":
        return float(statistics.mean(scores))
    if mode == "max":
        return float(max(scores))
    raise ValueError(f"unsupported aggregate: {mode}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--worktree", required=True)
    parser.add_argument("--timeout", type=float, default=45.0)
    parser.add_argument("--replicates", type=int, default=1)
    parser.add_argument("--aggregate", choices=["median", "mean", "max"], default="median")
    args = parser.parse_args()

    worktree = Path(args.worktree).resolve()
    out_dir = worktree / ".slurm"
    out_dir.mkdir(exist_ok=True)
    if args.replicates < 1:
        print("--replicates must be >= 1", file=sys.stderr)
        return 2
    try:
        reps = [run_one(worktree, out_dir, i, args.timeout) for i in range(1, args.replicates + 1)]
        scores = [float(r["score"]) for r in reps]
        result = {
            "score": aggregate(scores, args.aggregate),
            "aggregate": args.aggregate,
            "replicates": reps,
            "scores": scores,
        }
        print(json.dumps(result, sort_keys=True))
        return 0
    except Exception as exc:
        print(str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
'''


NOISY_PROJECT_MD = '''\
# Project Understanding

This is the noisy grouped-replicate behavior harness. The optimization target is
`target.py`; higher `candidate_score()` is better, but individual runs are noisy.

Resource profile:

- Binding resource: Slurm GPU jobs.
- The cluster exposes two GPU slots.
- The benchmark is `python slurm_eval.py --worktree {worktree}` by default.
- For noisy decisions, `slurm_eval.py` supports grouped evaluation:
  `python slurm_eval.py --worktree {worktree} --replicates 3 --aggregate median`.
- Running `python bench.py` directly is invalid and intentionally fails unless
  it is inside a Slurm job.

Candidate semantics:

- `STABLE_GAIN = True` is a real improvement: n=3 median score is 1.15.
- `NOISY_SPIKE = True` is a lucky single-run spike: scores are [1.8, 0.7, 0.7],
  so its n=3 median score is 0.7 and it should not be promoted.
- Do not judge candidates by the best replicate.

Orchestration expectations:

- If the user asks for n=3 / median evaluation, make each evo experiment record
  the grouped aggregate score. Do not create three independent evo experiments
  and later report the best as the idea score.
- Do not manually set `CUDA_VISIBLE_DEVICES=0`; Slurm assigns the device.
'''


FAKE_SLURM_ENGINE = r'''#!/usr/bin/env python3
from __future__ import annotations

import argparse
import fcntl
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path


def root() -> Path:
    r = os.environ.get("FAKE_SLURM_ROOT")
    if not r:
        print("FAKE_SLURM_ROOT is not set", file=sys.stderr)
        sys.exit(99)
    p = Path(r)
    p.mkdir(parents=True, exist_ok=True)
    return p


def state_path() -> Path:
    return root() / "state.json"


def events_path() -> Path:
    return root() / "events.jsonl"


def lock_path() -> Path:
    return root() / "state.lock"


class locked_state:
    def __enter__(self):
        lock_path().parent.mkdir(parents=True, exist_ok=True)
        self._lock = lock_path().open("w")
        fcntl.flock(self._lock, fcntl.LOCK_EX)
        if state_path().exists():
            self.state = json.loads(state_path().read_text())
        else:
            self.state = {"next_job_id": 1000, "jobs": {}}
        return self.state

    def __exit__(self, exc_type, exc, tb):
        if exc_type is None:
            tmp = state_path().with_suffix(".tmp")
            tmp.write_text(json.dumps(self.state, indent=2, sort_keys=True) + "\n")
            tmp.replace(state_path())
        fcntl.flock(self._lock, fcntl.LOCK_UN)
        self._lock.close()


def event(kind: str, **payload):
    rec = {"ts": round(time.time(), 3), "kind": kind, **payload}
    with events_path().open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(rec, sort_keys=True) + "\n")


def parse_sbatch(argv: list[str]) -> dict:
    opts: dict[str, str | bool | list[str]] = {"script_args": []}
    script = None
    i = 0
    while i < len(argv):
        arg = argv[i]
        if arg == "--parsable":
            opts["parsable"] = True
        elif arg.startswith("--job-name="):
            opts["job_name"] = arg.split("=", 1)[1]
        elif arg == "--job-name" and i + 1 < len(argv):
            i += 1
            opts["job_name"] = argv[i]
        elif arg.startswith("--gres="):
            opts["gres"] = arg.split("=", 1)[1]
        elif arg == "--gres" and i + 1 < len(argv):
            i += 1
            opts["gres"] = argv[i]
        elif arg.startswith("--output="):
            opts["output"] = arg.split("=", 1)[1]
        elif arg == "--output" and i + 1 < len(argv):
            i += 1
            opts["output"] = argv[i]
        elif arg.startswith("--error="):
            opts["error"] = arg.split("=", 1)[1]
        elif arg == "--error" and i + 1 < len(argv):
            i += 1
            opts["error"] = argv[i]
        elif arg.startswith("--wrap="):
            opts["wrap"] = arg.split("=", 1)[1]
        elif arg == "--wrap" and i + 1 < len(argv):
            i += 1
            opts["wrap"] = argv[i]
        elif arg.startswith("-"):
            # Accept common Slurm flags we do not need to model.
            if arg in {"-p", "--partition", "-t", "--time", "-c", "--cpus-per-task", "--mem"} and i + 1 < len(argv):
                i += 1
        elif script is None:
            script = arg
        else:
            opts["script_args"].append(arg)
        i += 1
    opts["script"] = script
    return opts


def expand_path(pattern: str | None, job_id: str, cwd: str, default_name: str) -> str:
    if not pattern:
        pattern = default_name
    value = pattern.replace("%j", job_id)
    p = Path(value)
    if not p.is_absolute():
        p = Path(cwd) / p
    return str(p)


def cmd_sbatch(argv: list[str]) -> int:
    opts = parse_sbatch(argv)
    cwd = os.getcwd()
    with locked_state() as state:
        job_id = str(state["next_job_id"])
        state["next_job_id"] += 1
        out = expand_path(opts.get("output"), job_id, cwd, f"slurm-{job_id}.out")
        err = expand_path(opts.get("error"), job_id, cwd, f"slurm-{job_id}.err")
        gpu_count = max(int(os.environ.get("FAKE_SLURM_GPU_COUNT", "2")), 1)
        gpu = str((int(job_id) - 1000) % gpu_count)
        state["jobs"][job_id] = {
            "id": job_id,
            "state": "PENDING",
            "cwd": cwd,
            "argv": argv,
            "opts": opts,
            "stdout": out,
            "stderr": err,
            "gpu": gpu,
            "pid": None,
            "exit_code": None,
            "submitted_at": time.time(),
        }
    event(
        "sbatch",
        job_id=job_id,
        argv=argv,
        cwd=cwd,
        env_cuda=os.environ.get("CUDA_VISIBLE_DEVICES"),
        opts=opts,
    )
    subprocess.Popen(
        [sys.executable, __file__, "_worker", job_id],
        cwd=cwd,
        env=os.environ.copy(),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    if opts.get("parsable"):
        print(job_id)
    else:
        print(f"Submitted batch job {job_id}")
    return 0


def worker(job_id: str) -> int:
    time.sleep(float(os.environ.get("FAKE_SLURM_PENDING_SECONDS", "0.2")))
    with locked_state() as state:
        job = state["jobs"][job_id]
        if job["state"] == "CANCELLED":
            return 0
        job["state"] = "RUNNING"
        job["started_at"] = time.time()
        out = Path(job["stdout"])
        err = Path(job["stderr"])
        out.parent.mkdir(parents=True, exist_ok=True)
        err.parent.mkdir(parents=True, exist_ok=True)
        running_jobs = [
            str(j["id"])
            for j in state["jobs"].values()
            if j.get("state") == "RUNNING"
        ]
        active_by_gpu: dict[str, int] = {}
        for j in state["jobs"].values():
            if j.get("state") == "RUNNING":
                gpu = str(j.get("gpu", "unknown"))
                active_by_gpu[gpu] = active_by_gpu.get(gpu, 0) + 1
    job = json.loads(state_path().read_text())["jobs"][job_id]
    env = os.environ.copy()
    env["SLURM_JOB_ID"] = job_id
    env["FAKE_SLURM_JOB_ID"] = job_id
    env["CUDA_VISIBLE_DEVICES"] = str(job["gpu"])
    opts = job["opts"]
    if opts.get("wrap"):
        cmd = str(opts["wrap"])
    else:
        script = opts.get("script")
        if not script:
            cmd = "true"
        else:
            args = " ".join(str(a) for a in opts.get("script_args", []))
            cmd = f"bash {str(script)!r} {args}".strip()
    event(
        "job_started",
        job_id=job_id,
        cmd=cmd,
        gpu=job["gpu"],
        active_jobs=len(running_jobs),
        active_job_ids=running_jobs,
        active_by_gpu=active_by_gpu,
        active_gpu_jobs=active_by_gpu.get(str(job["gpu"]), 0),
    )
    with Path(job["stdout"]).open("w") as stdout, Path(job["stderr"]).open("w") as stderr:
        proc = subprocess.Popen(
            cmd,
            cwd=job["cwd"],
            shell=True,
            env=env,
            stdout=stdout,
            stderr=stderr,
            start_new_session=True,
        )
        with locked_state() as state:
            if job_id in state["jobs"]:
                state["jobs"][job_id]["pid"] = proc.pid
        rc = proc.wait()
    with locked_state() as state:
        if job_id in state["jobs"]:
            state["jobs"][job_id]["exit_code"] = rc
            if state["jobs"][job_id]["state"] != "CANCELLED":
                state["jobs"][job_id]["state"] = "COMPLETED" if rc == 0 else "FAILED"
            state["jobs"][job_id]["finished_at"] = time.time()
    event("job_finished", job_id=job_id, exit_code=rc)
    return 0


def _job_ids_from_args(argv: list[str]) -> list[str]:
    ids: list[str] = []
    for i, arg in enumerate(argv):
        if arg in {"-j", "--job", "--jobs"} and i + 1 < len(argv):
            ids.extend(x for x in argv[i + 1].split(",") if x)
        elif arg.startswith("--jobs=") or arg.startswith("--job="):
            ids.extend(x for x in arg.split("=", 1)[1].split(",") if x)
        elif arg.startswith("-j") and len(arg) > 2:
            ids.extend(x for x in arg[2:].split(",") if x)
    return ids


def cmd_squeue(argv: list[str]) -> int:
    ids = set(_job_ids_from_args(argv))
    no_header = "-h" in argv or "--noheader" in argv or "--no-header" in argv
    fmt = None
    for i, arg in enumerate(argv):
        if arg in {"-o", "--format"} and i + 1 < len(argv):
            fmt = argv[i + 1]
        elif arg.startswith("--format="):
            fmt = arg.split("=", 1)[1]
    state = json.loads(state_path().read_text()) if state_path().exists() else {"jobs": {}}
    rows = []
    for job in state.get("jobs", {}).values():
        if ids and job["id"] not in ids:
            continue
        if job["state"] not in {"PENDING", "RUNNING"}:
            continue
        if fmt and "%T" in fmt:
            rows.append(job["state"])
        else:
            rows.append(f"{job['id']} {job['state']} {job.get('opts', {}).get('job_name', 'job')}")
    event("squeue", argv=argv, job_ids=sorted(ids))
    if rows:
        if not no_header and not fmt:
            print("JOBID STATE NAME")
        print("\n".join(rows))
    return 0


def cmd_sacct(argv: list[str]) -> int:
    ids = set(_job_ids_from_args(argv))
    parsable = "-P" in argv or "--parsable2" in argv or "--parsable" in argv
    no_header = "-n" in argv or "--noheader" in argv or "--no-header" in argv
    state = json.loads(state_path().read_text()) if state_path().exists() else {"jobs": {}}
    sep = "|" if parsable else " "
    rows = []
    for job in state.get("jobs", {}).values():
        if ids and job["id"] not in ids:
            continue
        exit_code = job.get("exit_code")
        exit_text = "0:0" if exit_code in (None, 0) else f"{exit_code}:0"
        rows.append(sep.join([job["state"], exit_text]))
    event("sacct", argv=argv, job_ids=sorted(ids))
    if rows:
        if not no_header:
            print(sep.join(["State", "ExitCode"]))
        print("\n".join(rows))
    return 0


def cmd_scancel(argv: list[str]) -> int:
    ids = [a for a in argv if not a.startswith("-")]
    with locked_state() as state:
        for job_id in ids:
            job = state["jobs"].get(job_id)
            if not job:
                continue
            job["state"] = "CANCELLED"
            job["exit_code"] = 130
            pid = job.get("pid")
            if pid:
                try:
                    os.killpg(int(pid), signal.SIGTERM)
                except ProcessLookupError:
                    pass
    event("scancel", argv=argv, job_ids=ids)
    return 0


def main(argv: list[str]) -> int:
    if not argv:
        print("usage: fake_slurm.py <sbatch|squeue|sacct|scancel> ...", file=sys.stderr)
        return 2
    cmd, rest = argv[0], argv[1:]
    if cmd == "sbatch":
        return cmd_sbatch(rest)
    if cmd == "squeue":
        return cmd_squeue(rest)
    if cmd == "sacct":
        return cmd_sacct(rest)
    if cmd == "scancel":
        return cmd_scancel(rest)
    if cmd == "_worker":
        return worker(rest[0])
    print(f"unknown fake slurm command: {cmd}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
'''


SCENARIOS = {
    "natural": """\
        We are testing a Slurm-backed evo optimization, similar to a genomics
        workflow where compute is on a cluster.

        Use `$evo optimize subagents=2 budget=1 stall=1` on this existing evo
        workspace. Use two parallel experiments. The benchmark is already
        configured for Slurm, so do not run the benchmark directly yourself.
        Keep this short: run one round, report what happened, then stop.
    """,
    "strict": """\
        Use `$evo optimize subagents=2 budget=1 stall=1` on this existing evo
        workspace.

        Hard requirements:
        - Resolve and arm autonomous/subagents-only according to the skill.
        - The orchestrator must not edit target.py directly.
        - The orchestrator must not create launcher or monitor scripts.
        - Spawn subagents for the experiments.
        - Each experiment must go through evo new and evo run.
        - Do not invoke python bench.py directly; the configured evo benchmark
          submits through sbatch.
        - Stop after one round and summarize the evo-recorded results.
    """,
    "report": """\
        I just woke up and want to check what happened overnight. What has
        improved recently, and what should I pay attention to?
    """,
    "implicit_improve": """\
        Can you improve this project? Use whatever compute is available and
        tell me what worked when you're done.
    """,
    "implicit_continue": """\
        Continue from the best result and try a few promising variants. Tell me
        what happened when you're done.
    """,
    "implicit_report": """\
        I just woke up. What improved overnight, and what should I look at next?
    """,
    "implicit_resource_cap": """\
        Try several promising variants from the current best result, but don't
        overload the machine. Tell me what happened when you're done.
    """,
    "loose_ideas": """\
        Try a couple different ideas from the current best result. Use the
        available GPUs and make sure the jobs land on separate GPUs. Don't
        overthink it; just run them and tell me what happened.
    """,
    "overload_guard": """\
        Try up to four different ideas from the current best result, but this
        Slurm cluster has only two GPU slots. Do not submit or run more than
        two Slurm jobs at the same time, and do not stack multiple active jobs
        onto one GPU slot. Batch the ideas if you decide to run more than two.

        Keep this bounded: run the first safe batch, report what happened, and
        stop.
    """,
    "directive_midrun": """\
        Use `$evo optimize subagents=2 budget=1 stall=1` on this existing evo
        workspace. The benchmark is intentionally slow enough that I may send
        a directive while Slurm jobs are running.

        Run one safe batch through the configured Slurm benchmark. If a
        directive arrives mid-run, acknowledge it immediately, do not cancel or
        replace any already-running Slurm jobs, then finish the current batch,
        report the evo-recorded results, and stop.
    """,
    "noisy_replicates": """\
        This is a noisy genomics-style benchmark, similar to Jacob's workflow.
        I want n=3 before deciding whether an idea is good, and the decision
        statistic is the median, not the best replicate. Lucky one-off wins
        should not be kept.

        Use `$evo optimize subagents=2 budget=1 stall=1`. Try exactly these two
        ideas from the baseline:
        - set `STABLE_GAIN = True`
        - set `NOISY_SPIKE = True`

        The noisy spike may look best if you look at only one replicate, but it
        must not be treated as the winning idea unless its median passes. Stop
        after this one round and summarize the recorded grouped results.
    """,
}

READ_ONLY_SCENARIOS = {"report", "implicit_report"}
OPTIMIZE_SCENARIOS = {
    "implicit_continue",
    "implicit_improve",
    "implicit_resource_cap",
    "natural",
    "strict",
    "loose_ideas",
    "overload_guard",
    "directive_midrun",
    "noisy_replicates",
}
NOISY_SCENARIOS = {"noisy_replicates"}
DEFAULT_SEQUENCE = ["implicit_improve", "implicit_report", "implicit_continue"]
SCENARIO_ENV = {
    "directive_midrun": {
        "FAKE_BENCH_SLEEP_SECONDS": "8",
    },
}


@dataclass
class HarnessPaths:
    run_dir: Path
    workspace: Path
    fake_slurm_root: Path
    fake_slurm_bin: Path
    codex_home: Path
    logs_dir: Path


def shell_quote_list(values: list[str]) -> str:
    return " ".join(subprocess.list2cmdline([v]) for v in values)


def run_cmd(
    cmd: list[str],
    *,
    cwd: Path | None = None,
    env: dict[str, str] | None = None,
    timeout: int = 120,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    print(f"$ {shell_quote_list(cmd)}", flush=True)
    proc = subprocess.run(
        cmd,
        cwd=cwd,
        env=env,
        text=True,
        capture_output=True,
        timeout=timeout,
        check=False,
    )
    if proc.stdout:
        print(proc.stdout, end="" if proc.stdout.endswith("\n") else "\n")
    if proc.stderr:
        print(proc.stderr, end="" if proc.stderr.endswith("\n") else "\n", file=sys.stderr)
    if check and proc.returncode != 0:
        raise RuntimeError(f"command failed ({proc.returncode}): {' '.join(cmd)}")
    return proc


def write_file(path: Path, text: str, *, mode: int | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(text).lstrip(), encoding="utf-8")
    if mode is not None:
        path.chmod(mode)


def make_paths(run_dir: Path) -> HarnessPaths:
    return HarnessPaths(
        run_dir=run_dir,
        workspace=run_dir / "ws",
        fake_slurm_root=run_dir / "fake-slurm-state",
        fake_slurm_bin=run_dir / "fake-slurm-bin",
        codex_home=run_dir / "codex-home",
        logs_dir=run_dir / "logs",
    )


def base_env(paths: HarnessPaths) -> dict[str, str]:
    env = os.environ.copy()
    # This harness often runs from another agent shell. Do not let the child
    # Codex process inherit that outer host's session id, or `evo autonomous`
    # / `evo subagents-only` will arm the wrong session record.
    for key in [
        "CLAUDE_CODE_SESSION_ID",
        "CODEX_THREAD_ID",
        "HERMES_SESSION_ID",
        "OPENCODE_SESSION_ID",
        "EVO_EXP_ID",
        "EVO_PARENT_SESSION_ID",
    ]:
        env.pop(key, None)
    env["FAKE_SLURM_ROOT"] = str(paths.fake_slurm_root)
    env["FAKE_SLURM_GPU_COUNT"] = env.get("FAKE_SLURM_GPU_COUNT", "2")
    env["CODEX_HOME"] = str(paths.codex_home)
    env["PATH"] = (
        str(paths.fake_slurm_bin)
        + os.pathsep
        + str(PLUGIN_ROOT / "bin")
        + os.pathsep
        + env.get("PATH", "")
    )
    return env


def env_for_scenario(paths: HarnessPaths, scenario: str) -> dict[str, str]:
    env = base_env(paths)
    env.update(SCENARIO_ENV.get(scenario, {}))
    if scenario == "directive_midrun":
        env["EVO_DRAIN_DEBUG"] = "1"
        env["EVO_DRAIN_DEBUG_LOG"] = str(paths.logs_dir / "evo-drain.log")
    return env


def copy_codex_auth(codex_home: Path) -> None:
    source_home = Path.home() / ".codex"
    codex_home.mkdir(parents=True, exist_ok=True)
    for name in ["auth.json", "installation_id", "models_cache.json"]:
        src = source_home / name
        if src.exists():
            shutil.copy2(src, codex_home / name)


def setup_fake_slurm(paths: HarnessPaths) -> None:
    paths.fake_slurm_root.mkdir(parents=True, exist_ok=True)
    paths.fake_slurm_bin.mkdir(parents=True, exist_ok=True)
    engine = paths.fake_slurm_bin / "fake_slurm.py"
    write_file(engine, FAKE_SLURM_ENGINE, mode=0o755)
    for name in ["sbatch", "squeue", "sacct", "scancel"]:
        wrapper = paths.fake_slurm_bin / name
        write_file(
            wrapper,
            f"""\
            #!/usr/bin/env bash
            exec "{sys.executable}" "{engine}" "{name}" "$@"
            """,
            mode=0o755,
        )


def fixture_for_scenarios(scenarios: list[str]) -> str:
    return "noisy" if any(s in NOISY_SCENARIOS for s in scenarios) else "slurm"


def setup_workspace(paths: HarnessPaths, env: dict[str, str], *, fixture: str = "slurm") -> None:
    ws = paths.workspace
    ws.mkdir(parents=True, exist_ok=True)
    if fixture == "noisy":
        target_py = NOISY_TARGET_PY
        bench_py = NOISY_BENCH_PY
        slurm_eval_py = NOISY_SLURM_EVAL_PY
        project_md = NOISY_PROJECT_MD
        project_name = "Codex Noisy Replicate Fixture"
    else:
        target_py = TARGET_PY
        bench_py = BENCH_PY
        slurm_eval_py = SLURM_EVAL_PY
        project_md = PROJECT_MD
        project_name = "Codex Slurm Fixture"
    write_file(ws / "target.py", target_py)
    write_file(ws / "bench.py", bench_py)
    write_file(ws / "slurm_eval.py", slurm_eval_py)
    write_file(
        ws / ".gitignore",
        """\
        .slurm/
        __pycache__/
        *.pyc
        """,
    )
    write_file(
        ws / "README.md",
        """\
        # Codex Slurm Behavior Fixture

        This repo is generated by `scripts/codex_slurm_harness.py`.
        It tests whether a real Codex agent driving evo uses the configured
        Slurm-backed benchmark path instead of ad-hoc local execution.
        """,
    )
    run_cmd(["git", "init", "-q"], cwd=ws, env=env)
    run_cmd(["git", "config", "user.email", "harness@evo"], cwd=ws, env=env)
    run_cmd(["git", "config", "user.name", "Evo Harness"], cwd=ws, env=env)
    run_cmd(["git", "config", "commit.gpgsign", "false"], cwd=ws, env=env)
    run_cmd(["git", "add", "."], cwd=ws, env=env)
    run_cmd(["git", "commit", "-q", "-m", "initial fixture"], cwd=ws, env=env)

    init_code = (
        "from pathlib import Path\n"
        "from evo.core import init_workspace, project_path\n"
        f"root = Path({str(ws)!r})\n"
        "init_workspace(root, target='target.py', "
        "benchmark='python slurm_eval.py --worktree {worktree}', "
        f"metric='max', gate=None, host='codex', project_name={project_name!r}, "
        "per_exp_timeout=90)\n"
        f"project_path(root).write_text({project_md!r})\n"
    )
    run_cmd(
        ["uv", "run", "--project", str(PLUGIN_ROOT), "python", "-c", init_code],
        cwd=REPO_ROOT,
        env=env,
        timeout=180,
    )
    run_cmd(["evo", "new", "--parent", "root", "-m", "baseline"], cwd=ws, env=env)
    run_cmd(["evo", "run", "exp_0000", "--timeout", "90"], cwd=ws, env=env, timeout=120)


def install_codex_plugin(paths: HarnessPaths, env: dict[str, str]) -> None:
    copy_codex_auth(paths.codex_home)
    marketplace_cache = paths.codex_home / ".tmp" / "marketplaces" / "evo-hq"
    marketplace_cache.parent.mkdir(parents=True, exist_ok=True)
    if not marketplace_cache.exists():
        try:
            marketplace_cache.symlink_to(REPO_ROOT, target_is_directory=True)
        except OSError:
            shutil.copytree(
                REPO_ROOT,
                marketplace_cache,
                symlinks=True,
                ignore=shutil.ignore_patterns(
                    ".git",
                    ".venv",
                    "__pycache__",
                    "build",
                    "dist",
                    ".pytest_cache",
                    "*.egg-info",
                ),
            )
    run_cmd(
        [
            "uv",
            "run",
            "--project",
            str(PLUGIN_ROOT),
            "evo",
            "install",
            "codex",
            "--from-path",
            str(REPO_ROOT),
            "--force",
            "--trust-hooks",
        ],
        cwd=REPO_ROOT,
        env=env,
        timeout=240,
    )


def setup(run_dir: Path | None, *, fixture: str = "slurm") -> HarnessPaths:
    if run_dir is None:
        run_dir = Path(tempfile.mkdtemp(prefix="evo-codex-slurm-"))
    else:
        run_dir.mkdir(parents=True, exist_ok=True)
    paths = make_paths(run_dir.resolve())
    paths.logs_dir.mkdir(parents=True, exist_ok=True)
    setup_fake_slurm(paths)
    env = base_env(paths)
    install_codex_plugin(paths, env)
    setup_workspace(paths, env, fixture=fixture)
    (paths.run_dir / "harness.json").write_text(
        json.dumps(
            {
                "run_dir": str(paths.run_dir),
                "workspace": str(paths.workspace),
                "fake_slurm_root": str(paths.fake_slurm_root),
                "codex_home": str(paths.codex_home),
            },
            indent=2,
            sort_keys=True,
        )
        + "\n"
    )
    return paths


def load_events(paths: HarnessPaths) -> list[dict[str, Any]]:
    path = paths.fake_slurm_root / "events.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def active_fake_slurm_jobs(paths: HarnessPaths) -> int:
    path = paths.fake_slurm_root / "state.json"
    if not path.exists():
        return 0
    try:
        state = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return 0
    return sum(
        1
        for job in state.get("jobs", {}).values()
        if job.get("state") in {"PENDING", "RUNNING"}
    )


def load_codex_events(paths: HarnessPaths, scenario: str) -> list[dict[str, Any]]:
    path = paths.logs_dir / f"codex-events-{scenario}.jsonl"
    if not path.exists():
        return []
    events = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if not line.strip():
            continue
        try:
            events.append(json.loads(line))
        except json.JSONDecodeError:
            pass
    return events


def codex_collab_tool_calls(paths: HarnessPaths, scenario: str, tool_name: str) -> int:
    count = 0
    for event in load_codex_events(paths, scenario):
        item = event.get("item") or {}
        if event.get("type") != "item.completed":
            continue
        if item.get("type") == "collab_tool_call" and item.get("tool") == tool_name:
            count += 1
    return count


def codex_optimization_worker_calls(paths: HarnessPaths, scenario: str) -> int:
    count = 0
    for event in load_codex_events(paths, scenario):
        item = event.get("item") or {}
        prompt = str(item.get("prompt") or "")
        if event.get("type") != "item.completed":
            continue
        if item.get("type") != "collab_tool_call" or item.get("tool") != "spawn_agent":
            continue
        lowered = prompt.lower()
        if "evo new" in lowered and "evo run" in lowered:
            count += 1
    return count


def codex_usage_limited(paths: HarnessPaths, scenario: str) -> bool:
    for event in load_codex_events(paths, scenario):
        item = event.get("item") or {}
        text = " ".join(
            str(value)
            for value in [
                event.get("message"),
                item.get("message"),
                (event.get("error") or {}).get("message") if isinstance(event.get("error"), dict) else "",
            ]
            if value
        ).lower()
        if "usage limit" in text:
            return True
    return False


def session_records(paths: HarnessPaths) -> list[dict[str, Any]]:
    root = paths.workspace / ".evo" / active_run_id(paths) / "inject" / "sessions"
    if not root.exists():
        return []
    records = []
    for path in sorted(root.glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        records.append(data)
    return records


def graph(paths: HarnessPaths) -> dict[str, Any]:
    meta = json.loads((paths.workspace / ".evo" / "meta.json").read_text())
    active = meta["active"]
    return json.loads((paths.workspace / ".evo" / active / "graph.json").read_text())


def initial_files(paths: HarnessPaths) -> set[str]:
    proc = run_cmd(["git", "ls-files"], cwd=paths.workspace, env=base_env(paths), check=True)
    return set(proc.stdout.splitlines())


def run_codex(paths: HarnessPaths, scenario: str, *, timeout: int) -> subprocess.CompletedProcess[str]:
    prompt = textwrap.dedent(SCENARIOS[scenario]).strip() + "\n"
    prompt_path = paths.logs_dir / f"prompt-{scenario}.md"
    output_path = paths.logs_dir / f"codex-last-{scenario}.md"
    jsonl_path = paths.logs_dir / f"codex-events-{scenario}.jsonl"
    prompt_path.write_text(prompt, encoding="utf-8")
    env = env_for_scenario(paths, scenario)
    cmd = [
        "codex",
        "exec",
        "--cd",
        str(paths.workspace),
        "--json",
        "--output-last-message",
        str(output_path),
        "--dangerously-bypass-approvals-and-sandbox",
        "--dangerously-bypass-hook-trust",
        "-",
    ]
    print(f"$ {' '.join(cmd)} < {prompt_path}")
    with prompt_path.open("r", encoding="utf-8") as stdin, jsonl_path.open("w", encoding="utf-8") as jsonl:
        try:
            proc = subprocess.run(
                cmd,
                cwd=paths.workspace,
                env=env,
                stdin=stdin,
                stdout=jsonl,
                stderr=subprocess.PIPE,
                text=True,
                timeout=timeout,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            stderr = exc.stderr.decode() if isinstance(exc.stderr, bytes) else (exc.stderr or "")
            timeout_msg = f"codex exec timed out after {timeout}s\n{stderr}"
            (paths.logs_dir / f"codex-timeout-{scenario}.txt").write_text(timeout_msg, encoding="utf-8")
            print(timeout_msg, file=sys.stderr)
            proc = subprocess.CompletedProcess(cmd, 124, stderr=timeout_msg)
    if proc.stderr:
        (paths.logs_dir / f"codex-stderr-{scenario}.txt").write_text(proc.stderr, encoding="utf-8")
        print(proc.stderr, file=sys.stderr)
    (paths.logs_dir / f"codex-exit-{scenario}.txt").write_text(str(proc.returncode) + "\n")
    return proc


def run_codex_with_midrun_directive(
    paths: HarnessPaths,
    scenario: str,
    *,
    timeout: int,
    wait_timeout: int,
) -> tuple[subprocess.CompletedProcess[str], dict[str, Any]]:
    prompt = textwrap.dedent(SCENARIOS[scenario]).strip() + "\n"
    prompt_path = paths.logs_dir / f"prompt-{scenario}.md"
    output_path = paths.logs_dir / f"codex-last-{scenario}.md"
    jsonl_path = paths.logs_dir / f"codex-events-{scenario}.jsonl"
    prompt_path.write_text(prompt, encoding="utf-8")
    env = env_for_scenario(paths, scenario)
    cmd = [
        "codex",
        "exec",
        "--cd",
        str(paths.workspace),
        "--json",
        "--output-last-message",
        str(output_path),
        "--dangerously-bypass-approvals-and-sandbox",
        "--dangerously-bypass-hook-trust",
        "-",
    ]
    directive_text = (
        "Mid-run directive: acknowledge this immediately. Do not cancel, kill, "
        "or replace any Slurm jobs that are already running. Let the current "
        "safe batch finish, collect only the evo-recorded results, report them, "
        "and stop."
    )
    direct_info: dict[str, Any] = {
        "attempted": False,
        "injected": False,
        "acked": False,
        "returncode": None,
        "active_jobs_at_inject": 0,
        "job_started_count_at_inject": 0,
        "stdout": "",
        "stderr": "",
    }
    initial_job_started = sum(1 for e in load_events(paths) if e.get("kind") == "job_started")
    print(f"$ {' '.join(cmd)} < {prompt_path}")
    with prompt_path.open("r", encoding="utf-8") as stdin, jsonl_path.open("w", encoding="utf-8") as jsonl:
        proc = subprocess.Popen(
            cmd,
            cwd=paths.workspace,
            env=env,
            stdin=stdin,
            stdout=jsonl,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
        )
        deadline = time.time() + timeout
        returncode: int | None = None
        stderr = ""
        while True:
            returncode = proc.poll()
            events = load_events(paths)
            job_started_count = sum(1 for e in events if e.get("kind") == "job_started")
            active_jobs = active_fake_slurm_jobs(paths)
            if (
                not direct_info["attempted"]
                and job_started_count > initial_job_started
                and active_jobs > 0
            ):
                direct_info.update(
                    {
                        "attempted": True,
                        "injected": True,
                        "active_jobs_at_inject": active_jobs,
                        "job_started_count_at_inject": job_started_count,
                    }
                )
                direct_proc = run_cmd(
                    [
                        "evo",
                        "direct",
                        "--wait",
                        "--wait-timeout",
                        str(wait_timeout),
                        directive_text,
                    ],
                    cwd=paths.workspace,
                    env=env,
                    timeout=wait_timeout + 30,
                    check=False,
                )
                direct_info.update(
                    {
                        "returncode": direct_proc.returncode,
                        "acked": direct_proc.returncode == 0,
                        "stdout": direct_proc.stdout,
                        "stderr": direct_proc.stderr,
                    }
                )
                (paths.logs_dir / f"directive-{scenario}.json").write_text(
                    json.dumps(direct_info, indent=2, sort_keys=True) + "\n",
                    encoding="utf-8",
                )
            if returncode is not None:
                stderr = proc.stderr.read() if proc.stderr else ""
                break
            if time.time() > deadline:
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(timeout=5)
                stderr = proc.stderr.read() if proc.stderr else ""
                timeout_msg = f"codex exec timed out after {timeout}s\n{stderr}"
                (paths.logs_dir / f"codex-timeout-{scenario}.txt").write_text(
                    timeout_msg,
                    encoding="utf-8",
                )
                print(timeout_msg, file=sys.stderr)
                returncode = 124
                stderr = timeout_msg
                break
            time.sleep(0.2)
    if not direct_info["attempted"]:
        (paths.logs_dir / f"directive-{scenario}.json").write_text(
            json.dumps(direct_info, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
    if stderr:
        (paths.logs_dir / f"codex-stderr-{scenario}.txt").write_text(stderr, encoding="utf-8")
        print(stderr, file=sys.stderr)
    (paths.logs_dir / f"codex-exit-{scenario}.txt").write_text(str(returncode) + "\n")
    return subprocess.CompletedProcess(cmd, int(returncode or 0), stderr=stderr), direct_info


def collect_analysis(paths: HarnessPaths, *, expect_optimization: bool = True) -> dict[str, Any]:
    env = base_env(paths)
    events = load_events(paths)
    sessions = session_records(paths)
    g = graph(paths)
    nodes = g.get("nodes", {})
    exp_nodes = [n for n in nodes.values() if n.get("id") != "root"]
    sbatches = [e for e in events if e.get("kind") == "sbatch"]
    job_started = [e for e in events if e.get("kind") == "job_started"]
    job_started_active_jobs = [int(e.get("active_jobs") or 0) for e in job_started]
    job_started_active_gpu_jobs = [int(e.get("active_gpu_jobs") or 0) for e in job_started]
    direct_runs = [e for e in events if e.get("kind") == "direct_benchmark_run"]
    hardcoded_cuda = [
        e for e in sbatches
        if e.get("env_cuda") not in (None, "")
    ]
    status = run_cmd(
        ["git", "status", "--porcelain"],
        cwd=paths.workspace,
        env=env,
        check=True,
    ).stdout.splitlines()
    suspicious_root_files = []
    for line in status:
        path = line[3:] if len(line) > 3 else line
        name = Path(path).name.lower()
        if path.startswith(".evo/"):
            continue
        if name in {"target.py", "bench.py", "slurm_eval.py"}:
            suspicious_root_files.append(path)
        if name.endswith((".sh", ".py")) and any(token in name for token in ["launch", "monitor", "submit", "slurm", "job"]):
            suspicious_root_files.append(path)
    summary = {
        "run_dir": str(paths.run_dir),
        "workspace": str(paths.workspace),
        "experiments_total": len(exp_nodes),
        "experiments_committed": sum(1 for n in exp_nodes if n.get("status") == "committed"),
        "experiments_failed": sum(1 for n in exp_nodes if n.get("status") == "failed"),
        "fake_slurm_gpu_count": int(env.get("FAKE_SLURM_GPU_COUNT", "2")),
        "sbatch_calls": len(sbatches),
        "job_started_count": len(job_started),
        "job_started_gpus": [str(e.get("gpu")) for e in job_started if e.get("gpu") is not None],
        "job_started_active_jobs": job_started_active_jobs,
        "job_started_active_gpu_jobs": job_started_active_gpu_jobs,
        "max_active_slurm_jobs": max(job_started_active_jobs, default=0),
        "max_active_jobs_per_gpu": max(job_started_active_gpu_jobs, default=0),
        "squeue_calls": sum(1 for e in events if e.get("kind") == "squeue"),
        "sacct_calls": sum(1 for e in events if e.get("kind") == "sacct"),
        "scancel_calls": sum(1 for e in events if e.get("kind") == "scancel"),
        "direct_benchmark_runs": len(direct_runs),
        "hardcoded_cuda_sbatch_env": len(hardcoded_cuda),
        "sessions_with_exp_id": sorted(
            str(s.get("exp_id")) for s in sessions if s.get("exp_id")
        ),
        "sessions_with_exp_id_count": sum(1 for s in sessions if s.get("exp_id")),
        "suspicious_root_files": sorted(set(suspicious_root_files)),
        "status_lines": status,
        "assertions": {},
    }
    assertions = {
        "baseline_used_slurm": summary["sbatch_calls"] >= 1,
        "no_direct_benchmark_runs": summary["direct_benchmark_runs"] == 0,
        "no_hardcoded_cuda_env": summary["hardcoded_cuda_sbatch_env"] == 0,
        "no_slurm_overload": summary["max_active_slurm_jobs"] <= summary["fake_slurm_gpu_count"],
        "no_gpu_slot_stacking": summary["max_active_jobs_per_gpu"] <= 1,
        "no_suspicious_root_files": not summary["suspicious_root_files"],
    }
    if expect_optimization:
        assertions.update(
            {
                "agent_created_two_experiments": summary["experiments_total"] >= 3,
                "fresh_slurm_job_per_experiment": summary["sbatch_calls"] >= summary["experiments_total"],
            }
        )
    summary["assertions"] = assertions
    summary["passed"] = all(assertions.values())
    return summary


def infer_expect_optimization(paths: HarnessPaths) -> bool:
    for phase_path in sorted(paths.logs_dir.glob("phase-*.json")):
        try:
            phase = json.loads(phase_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if phase.get("scenario") in OPTIMIZE_SCENARIOS:
            return True
    return False


def analyze(paths: HarnessPaths, *, expect_optimization: bool | None = None) -> dict[str, Any]:
    if expect_optimization is None:
        expect_optimization = infer_expect_optimization(paths)
    summary = collect_analysis(paths, expect_optimization=expect_optimization)
    (paths.logs_dir / "analysis.json").write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n")
    print(json.dumps(summary, indent=2, sort_keys=True))
    return summary


def active_run_id(paths: HarnessPaths) -> str:
    meta = json.loads((paths.workspace / ".evo" / "meta.json").read_text())
    return str(meta["active"])


def attempt_diff_text(paths: HarnessPaths, exp_id: str, attempt: int) -> str:
    path = (
        paths.workspace
        / ".evo"
        / active_run_id(paths)
        / "experiments"
        / exp_id
        / "attempts"
        / f"{attempt:03d}"
        / "diff.patch"
    )
    return path.read_text(encoding="utf-8", errors="replace") if path.exists() else ""


def noisy_replicate_assertions(paths: HarnessPaths) -> dict[str, bool]:
    g = graph(paths)
    nodes = g.get("nodes", {})
    config = json.loads((paths.workspace / ".evo" / active_run_id(paths) / "config.json").read_text())
    benchmark = str(config.get("benchmark") or "")
    experiment_nodes = [
        n for n in nodes.values()
        if n.get("id") not in (None, "root", "exp_0000")
    ]
    grouped_nodes = []
    stable_nodes = []
    spike_nodes = []
    for node in experiment_nodes:
        result = node.get("benchmark_result") or {}
        if result.get("aggregate") == "median" and len(result.get("replicates") or []) == 3:
            grouped_nodes.append(node)
        attempt = int(node.get("current_attempt") or 0)
        diff = attempt_diff_text(paths, str(node["id"]), attempt) if attempt else ""
        if "+STABLE_GAIN = True" in diff or "STABLE_GAIN = True" in diff and "-STABLE_GAIN = False" in diff:
            stable_nodes.append(node)
        if "+NOISY_SPIKE = True" in diff or "NOISY_SPIKE = True" in diff and "-NOISY_SPIKE = False" in diff:
            spike_nodes.append(node)

    committed = [n for n in experiment_nodes if n.get("status") == "committed"]
    best_committed = max(
        committed,
        key=lambda n: float(n.get("score") if n.get("score") is not None else "-inf"),
        default=None,
    )

    def close(value: Any, target: float) -> bool:
        try:
            return abs(float(value) - target) < 1e-9
        except Exception:
            return False

    stable_promoted = any(n.get("status") == "committed" and close(n.get("score"), 1.15) for n in stable_nodes)
    spike_not_promoted = bool(spike_nodes) and all(
        n.get("status") != "committed" and close(n.get("score"), 0.7)
        for n in spike_nodes
    )
    return {
        "benchmark_configured_for_n3_median": "--replicates 3" in benchmark and "--aggregate median" in benchmark,
        "all_new_results_grouped_n3_median": bool(experiment_nodes) and len(grouped_nodes) == len(experiment_nodes),
        "stable_gain_tried": bool(stable_nodes),
        "noisy_spike_tried": bool(spike_nodes),
        "stable_gain_promoted_by_median": stable_promoted,
        "noisy_spike_not_promoted_by_best_replicate": spike_not_promoted,
        "best_is_stable_median_not_lucky_spike": (
            best_committed is not None
            and close(best_committed.get("score"), 1.15)
            and any(best_committed.get("id") == n.get("id") for n in stable_nodes)
        ),
    }


def phase_delta(
    before: dict[str, Any],
    after: dict[str, Any],
    scenario: str,
    exit_code: int,
    *,
    paths: HarnessPaths | None = None,
) -> dict[str, Any]:
    delta = {
        "scenario": scenario,
        "codex_exit_code": exit_code,
        "codex_spawn_agent_calls": (
            codex_collab_tool_calls(paths, scenario, "spawn_agent") if paths is not None else 0
        ),
        "codex_optimization_worker_calls": (
            codex_optimization_worker_calls(paths, scenario) if paths is not None else 0
        ),
        "codex_usage_limited": codex_usage_limited(paths, scenario) if paths is not None else False,
        "experiments_delta": after["experiments_total"] - before["experiments_total"],
        "committed_delta": after["experiments_committed"] - before["experiments_committed"],
        "failed_delta": after["experiments_failed"] - before["experiments_failed"],
        "sbatch_delta": after["sbatch_calls"] - before["sbatch_calls"],
        "job_started_gpus_delta": after["job_started_gpus"][before["job_started_count"]:],
        "job_started_active_jobs_delta": (
            after["job_started_active_jobs"][before["job_started_count"]:]
        ),
        "job_started_active_gpu_jobs_delta": (
            after["job_started_active_gpu_jobs"][before["job_started_count"]:]
        ),
        "max_active_slurm_jobs_delta": max(
            after["job_started_active_jobs"][before["job_started_count"]:],
            default=0,
        ),
        "max_active_jobs_per_gpu_delta": max(
            after["job_started_active_gpu_jobs"][before["job_started_count"]:],
            default=0,
        ),
        "squeue_delta": after["squeue_calls"] - before["squeue_calls"],
        "sacct_delta": after["sacct_calls"] - before["sacct_calls"],
        "scancel_delta": after["scancel_calls"] - before["scancel_calls"],
        "direct_benchmark_delta": after["direct_benchmark_runs"] - before["direct_benchmark_runs"],
        "hardcoded_cuda_sbatch_env_delta": (
            after["hardcoded_cuda_sbatch_env"] - before["hardcoded_cuda_sbatch_env"]
        ),
        "sessions_with_exp_id_delta": (
            after["sessions_with_exp_id_count"] - before["sessions_with_exp_id_count"]
        ),
        "fake_slurm_gpu_count": after["fake_slurm_gpu_count"],
        "suspicious_root_files": after["suspicious_root_files"],
        "assertions": {},
    }
    assertions = {
        "codex_exited_zero": exit_code == 0,
        "no_direct_benchmark_runs": delta["direct_benchmark_delta"] == 0,
        "no_scancel": delta["scancel_delta"] == 0,
        "no_hardcoded_cuda_env": delta["hardcoded_cuda_sbatch_env_delta"] == 0,
        "no_slurm_overload": (
            delta["max_active_slurm_jobs_delta"] <= delta["fake_slurm_gpu_count"]
        ),
        "no_gpu_slot_stacking": delta["max_active_jobs_per_gpu_delta"] <= 1,
        "no_suspicious_root_files": not delta["suspicious_root_files"],
    }
    if scenario in READ_ONLY_SCENARIOS:
        assertions.update(
            {
                "read_only_no_new_experiments": delta["experiments_delta"] == 0,
                "read_only_no_new_sbatch": delta["sbatch_delta"] == 0,
            }
        )
    if scenario in OPTIMIZE_SCENARIOS:
        assertions.update(
            {
                "codex_spawned_optimization_workers": delta["codex_optimization_worker_calls"] > 0,
                "optimization_worker_sessions_claimed_exp_ids": (
                    delta["sessions_with_exp_id_delta"] >= delta["codex_optimization_worker_calls"]
                ),
                "created_experiments": delta["experiments_delta"] >= 2,
                "fresh_slurm_job_per_new_experiment": delta["sbatch_delta"] >= delta["experiments_delta"],
            }
        )
    if scenario == "noisy_replicates" and paths is not None:
        assertions.update(
            {
                "codex_spawned_two_candidate_subagents": delta["codex_optimization_worker_calls"] >= 2,
                **noisy_replicate_assertions(paths),
            }
        )
    if scenario == "loose_ideas":
        expected_slots = min(2, delta["sbatch_delta"])
        assertions["used_separate_gpu_slots_when_requested"] = (
            expected_slots < 2 or len(set(delta["job_started_gpus_delta"])) >= expected_slots
        )
    delta["assertions"] = assertions
    delta["passed"] = all(assertions.values())
    return delta


def paths_from_run_dir(run_dir: Path) -> HarnessPaths:
    return make_paths(run_dir.resolve())


def cmd_setup(args: argparse.Namespace) -> int:
    paths = setup(args.run_dir)
    analyze(paths)
    print(f"\nCreated harness at {paths.run_dir}")
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    paths = setup(args.run_dir, fixture=fixture_for_scenarios([args.scenario]))
    expect_optimization = args.scenario in OPTIMIZE_SCENARIOS
    if not args.no_agent:
        before = collect_analysis(paths, expect_optimization=expect_optimization)
        proc = run_codex(paths, args.scenario, timeout=args.timeout)
        after = collect_analysis(paths, expect_optimization=expect_optimization)
        delta = phase_delta(before, after, args.scenario, proc.returncode, paths=paths)
        (paths.logs_dir / f"phase-01-{args.scenario}.json").write_text(
            json.dumps(delta, indent=2, sort_keys=True) + "\n"
        )
        print(json.dumps(delta, indent=2, sort_keys=True))
    summary = collect_analysis(paths, expect_optimization=expect_optimization)
    (paths.logs_dir / "analysis.json").write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n")
    print(json.dumps(summary, indent=2, sort_keys=True))
    print(f"\nHarness run: {paths.run_dir}")
    if args.no_agent:
        passed = summary["passed"]
    else:
        global_assertions = summary.get("assertions", {})
        global_safety_passed = all(
            global_assertions.get(name, False)
            for name in [
                "baseline_used_slurm",
                "no_direct_benchmark_runs",
                "no_hardcoded_cuda_env",
                "no_slurm_overload",
                "no_gpu_slot_stacking",
                "no_suspicious_root_files",
            ]
        )
        passed = delta["passed"] and global_safety_passed
    return 0 if args.allow_fail or passed else 1


def cmd_sequence(args: argparse.Namespace) -> int:
    paths = setup(args.run_dir, fixture=fixture_for_scenarios(args.scenarios))
    expect_optimization = any(scenario in OPTIMIZE_SCENARIOS for scenario in args.scenarios)
    phases = []
    ok = True
    for scenario in args.scenarios:
        before = collect_analysis(paths, expect_optimization=expect_optimization)
        proc = run_codex(paths, scenario, timeout=args.timeout)
        after = collect_analysis(paths, expect_optimization=expect_optimization)
        delta = phase_delta(before, after, scenario, proc.returncode, paths=paths)
        phases.append(delta)
        ok = ok and delta["passed"]
        phase_path = paths.logs_dir / f"phase-{len(phases):02d}-{scenario}.json"
        phase_path.write_text(json.dumps(delta, indent=2, sort_keys=True) + "\n")
        print(json.dumps(delta, indent=2, sort_keys=True))
    summary = collect_analysis(paths, expect_optimization=expect_optimization)
    (paths.logs_dir / "analysis.json").write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n")
    print(json.dumps(summary, indent=2, sort_keys=True))
    global_assertions = summary.get("assertions", {})
    global_safety_passed = all(
        global_assertions.get(name, False)
        for name in [
            "baseline_used_slurm",
            "no_direct_benchmark_runs",
            "no_hardcoded_cuda_env",
            "no_slurm_overload",
            "no_gpu_slot_stacking",
            "no_suspicious_root_files",
        ]
    )
    sequence = {
        "run_dir": str(paths.run_dir),
        "passed": ok and global_safety_passed,
        "global_safety_passed": global_safety_passed,
        "phases": phases,
    }
    (paths.logs_dir / "sequence.json").write_text(json.dumps(sequence, indent=2, sort_keys=True) + "\n")
    print(f"\nHarness sequence: {paths.run_dir}")
    return 0 if args.allow_fail or sequence["passed"] else 1


def cmd_directive_midrun(args: argparse.Namespace) -> int:
    scenario = "directive_midrun"
    paths = setup(args.run_dir, fixture="slurm")
    before = collect_analysis(paths, expect_optimization=True)
    proc, direct_info = run_codex_with_midrun_directive(
        paths,
        scenario,
        timeout=args.timeout,
        wait_timeout=args.wait_timeout,
    )
    after = collect_analysis(paths, expect_optimization=True)
    delta = phase_delta(before, after, scenario, proc.returncode, paths=paths)
    delta["directive"] = direct_info
    delta["assertions"].update(
        {
            "directive_injected_while_job_active": (
                bool(direct_info.get("injected"))
                and int(direct_info.get("active_jobs_at_inject") or 0) > 0
            ),
            "directive_acked": bool(direct_info.get("acked")),
            "no_replacement_jobs_after_directive": delta["sbatch_delta"] <= 2,
        }
    )
    delta["passed"] = all(delta["assertions"].values())
    (paths.logs_dir / f"phase-01-{scenario}.json").write_text(
        json.dumps(delta, indent=2, sort_keys=True) + "\n"
    )
    print(json.dumps(delta, indent=2, sort_keys=True))
    summary = collect_analysis(paths, expect_optimization=True)
    (paths.logs_dir / "analysis.json").write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n")
    print(json.dumps(summary, indent=2, sort_keys=True))
    global_assertions = summary.get("assertions", {})
    global_safety_passed = all(
        global_assertions.get(name, False)
        for name in [
            "baseline_used_slurm",
            "no_direct_benchmark_runs",
            "no_hardcoded_cuda_env",
            "no_slurm_overload",
            "no_gpu_slot_stacking",
            "no_suspicious_root_files",
        ]
    )
    passed = delta["passed"] and global_safety_passed
    print(f"\nHarness directive-midrun: {paths.run_dir}")
    return 0 if args.allow_fail or passed else 1


def cmd_analyze(args: argparse.Namespace) -> int:
    summary = analyze(paths_from_run_dir(args.run_dir))
    return 0 if args.allow_fail or summary["passed"] else 1


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="cmd", required=True)

    setup_p = sub.add_parser("setup", help="create fixture and run baseline only")
    setup_p.add_argument("--run-dir", type=Path)
    setup_p.set_defaults(func=cmd_setup)

    run_p = sub.add_parser("run", help="create fixture and drive Codex")
    run_p.add_argument("--run-dir", type=Path)
    run_p.add_argument("--scenario", choices=sorted(SCENARIOS), default="natural")
    run_p.add_argument("--timeout", type=int, default=600)
    run_p.add_argument("--no-agent", action="store_true")
    run_p.add_argument("--allow-fail", action="store_true")
    run_p.set_defaults(func=cmd_run)

    sequence_p = sub.add_parser("sequence", help="run a Jacob-style multi-turn Codex sequence")
    sequence_p.add_argument("--run-dir", type=Path)
    sequence_p.add_argument("--scenarios", nargs="+", choices=sorted(SCENARIOS), default=DEFAULT_SEQUENCE)
    sequence_p.add_argument("--timeout", type=int, default=600)
    sequence_p.add_argument("--allow-fail", action="store_true")
    sequence_p.set_defaults(func=cmd_sequence)

    direct_p = sub.add_parser("directive-midrun", help="drive Codex and inject evo direct while Slurm jobs run")
    direct_p.add_argument("--run-dir", type=Path)
    direct_p.add_argument("--timeout", type=int, default=700)
    direct_p.add_argument("--wait-timeout", type=int, default=120)
    direct_p.add_argument("--allow-fail", action="store_true")
    direct_p.set_defaults(func=cmd_directive_midrun)

    analyze_p = sub.add_parser("analyze", help="analyze an existing harness run")
    analyze_p.add_argument("run_dir", type=Path)
    analyze_p.add_argument("--allow-fail", action="store_true")
    analyze_p.set_defaults(func=cmd_analyze)

    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))

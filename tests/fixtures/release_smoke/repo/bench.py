"""Deterministic benchmark for target.count_pairs.

Generates a fixed input, runs count_pairs, checks the result against an
independent reference, and prints a single JSON line with score and
elapsed time. Score is items per millisecond.
"""

from __future__ import annotations

import random
import sys
import time

from target import count_pairs


N = 4000
TARGET = 100
SEED = 42


def _reference(xs: list[int], target: int) -> int:
    """Independent reference implementation for the correctness check."""
    seen: dict[int, int] = {}
    count = 0
    for x in xs:
        count += seen.get(target - x, 0)
        seen[x] = seen.get(x, 0) + 1
    return count


def main() -> int:
    rng = random.Random(SEED)
    xs = [rng.randint(0, 200) for _ in range(N)]

    expected = _reference(xs, TARGET)

    t0 = time.perf_counter()
    got = count_pairs(xs, TARGET)
    elapsed_ms = (time.perf_counter() - t0) * 1000

    if got != expected:
        print(f"FAIL: count_pairs returned {got}, reference says {expected}",
              file=sys.stderr)
        return 1

    # evo's `evo run` parses stdout as a single JSON object with at least
    # a `score` field. Emit JSON, not key=value lines.
    score = N / max(elapsed_ms, 0.001)
    import json as _json
    print(_json.dumps({"score": round(score, 2), "elapsed_ms": round(elapsed_ms, 2)}))
    return 0


if __name__ == "__main__":
    sys.exit(main())

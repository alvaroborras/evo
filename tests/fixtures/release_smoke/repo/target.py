"""Count unordered index pairs (i, j), i < j, with xs[i] + xs[j] == target."""

from __future__ import annotations


def count_pairs(xs: list[int], target: int) -> int:
    """Return the number of unordered index pairs (i, j) with i < j
    such that xs[i] + xs[j] == target."""
    n = len(xs)
    count = 0
    for i in range(n):
        for j in range(i + 1, n):
            if xs[i] + xs[j] == target:
                count += 1
    return count

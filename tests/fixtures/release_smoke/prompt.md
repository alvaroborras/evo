Use the `optimize` skill to explore optimizations for `target.py` in this
repository.

**Round 1**: launch exactly 2 experiments in parallel, each trying ONE of
these approaches (do not deviate, do not pick a different algorithm):

- Experiment A: keep the double loop but cache `xs[i]` in a local variable
  in the outer loop. Same O(n²); small constant-factor win.
- Experiment B: rewrite using `itertools.combinations`. Same O(n²); pushes
  the inner loop into C.

**Round 2**: launch exactly 1 experiment:

- Experiment C: sort `xs` first, then for each `i` use `bisect` to find
  `target - xs[i]` in the tail. O(n log n).

Report the best score at the end.

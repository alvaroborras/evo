# Cursor hook probe

Verifies the one part of the Cursor inject design that can't be checked from
evo's side: **does Cursor splice the `additional_context` returned by
`evo-drain --host cursor` into the agent's turn, and do the hooks fire under
headless `cursor-agent -p`?**

```bash
bash scripts/cursor_hook_probe/probe.sh
```

Prerequisites: `cursor-agent` on PATH and authenticated (`curl
https://cursor.com/install -fsS | bash`, then `cursor-agent login`), plus
`evo-drain` (or `uv` to run it from this repo).

Exit codes: `0` confirmed (token reached the agent), `1` token not delivered
(hook didn't fire headless, or `additional_context` ignored — inspect the
dumped output), `2` inconclusive (missing prerequisite).

The evo-side round-trip (queue → marker → drain → `additional_context` JSON)
is covered without the binary by the dry run in
`scripts/cursor_hook_probe/dryrun.sh`.

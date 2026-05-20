#!/usr/bin/env bash
# Probe: does Cursor honor the `additional_context` that `evo-drain --host
# cursor` returns from a hook, and splice it into the agent's turn?
#
# This is the one part of the Cursor inject design that can't be verified
# from evo's side alone — it needs the real `cursor-agent` binary running a
# session with our hooks.json wired in. (The evo-side queue->marker->drain
# round-trip is covered by the dry run / unit tests.)
#
# What it does:
#   1. Builds a throwaway git repo + `evo init --host cursor`.
#   2. Queues a directive carrying a unique token into the workspace queue.
#   3. Writes a project-level `.cursor/hooks.json` wiring sessionStart +
#      postToolUse -> `evo-drain --host cursor`.
#   4. Runs `cursor-agent -p` with a prompt that forces a tool call (so
#      postToolUse fires) and asks the agent to echo any directive it sees.
#   5. Greps the streamed output for the token. Token present => Cursor
#      delivered our additional_context. Absent => it didn't (or the hook
#      didn't fire headless).
#
# Usage:  bash scripts/cursor_hook_probe/probe.sh
# Requires: cursor-agent on PATH (authenticated), evo-drain on PATH.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
TOKEN="EVO_PROBE_$(date +%s)_$RANDOM"

note() { printf '\n=== %s ===\n' "$*"; }
fail() { printf 'PROBE INCONCLUSIVE: %s\n' "$*" >&2; exit 2; }

# --- prerequisites -----------------------------------------------------------
command -v cursor-agent >/dev/null 2>&1 || fail \
  "cursor-agent not on PATH. Install it:  curl https://cursor.com/install -fsS | bash
   (then authenticate: cursor-agent login). Re-run this probe afterward."

# Prefer the repo's editable evo-drain so the probe tests local changes.
DRAIN_CMD="evo-drain"
if ! command -v evo-drain >/dev/null 2>&1; then
  if command -v uv >/dev/null 2>&1; then
    DRAIN_CMD="uv run --project $REPO_ROOT/plugins/evo evo-drain"
  else
    fail "evo-drain not on PATH and uv unavailable. Run: uv tool install --editable $REPO_ROOT/plugins/evo"
  fi
fi

EVO_CMD="evo"
command -v evo >/dev/null 2>&1 || EVO_CMD="uv run --project $REPO_ROOT/plugins/evo evo"

# --- throwaway workspace -----------------------------------------------------
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
cd "$WORK"
git init -q
git config user.email probe@example.com
git config user.name probe
printf 'print("hello")\n' > main.py
git add -A && git commit -q -m init

note "evo init --host cursor"
# Benchmark is irrelevant to the probe; use a trivial always-passing command.
$EVO_CMD init --target main.py --benchmark "true" --metric max --host cursor --port 0 \
  || fail "evo init failed"

note "queue a directive carrying token $TOKEN"
$EVO_CMD direct "PROBE: if you can read this, include the exact string $TOKEN in your reply." \
  2>/dev/null || \
  uv run --project "$REPO_ROOT/plugins/evo" python -c \
    "import sys; from pathlib import Path; from evo.inject import queue; \
     queue.append_workspace_event(Path('$WORK'), 'PROBE: if you can read this, include the exact string $TOKEN in your reply.')"

note "write .cursor/hooks.json"
mkdir -p .cursor
cat > .cursor/hooks.json <<JSON
{
  "version": 1,
  "hooks": {
    "sessionStart": [{ "command": "$DRAIN_CMD --host cursor" }],
    "postToolUse":  [{ "command": "$DRAIN_CMD --host cursor" }]
  }
}
JSON
cat .cursor/hooks.json

note "run cursor-agent -p (forcing a tool call) — streaming output"
OUT="$WORK/agent_out.txt"
# Prompt forces a file read (=> a tool call => postToolUse fires) and asks
# the agent to surface any directive. --force allows tool use non-interactively.
cursor-agent -p "Read main.py, then tell me what it prints. If you received any [EVO DIRECTIVE], follow it." \
  --force --output-format stream-json 2>&1 | tee "$OUT" || true

note "result"
if grep -q "$TOKEN" "$OUT"; then
  echo "PASS: Cursor delivered the injected additional_context (token $TOKEN found)."
  echo "      => evo-drain --host cursor round-trips through Cursor hooks. Design confirmed."
  exit 0
else
  echo "FAIL/INCONCLUSIVE: token $TOKEN not found in agent output."
  echo "  Either the hook didn't fire headless, or additional_context wasn't spliced in."
  echo "  Inspect $OUT (copied below is the tail):"
  tail -40 "$OUT" || true
  exit 1
fi

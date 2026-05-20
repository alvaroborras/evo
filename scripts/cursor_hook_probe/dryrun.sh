#!/usr/bin/env bash
# Hermetic dry run for the Cursor host integration. No network, no
# cursor-agent binary. Validates everything on evo's side of the contract:
#
#   1. `evo install cursor` writes ~/.cursor/hooks.json (sessionStart +
#      postToolUse -> evo-drain --host cursor) without clobbering existing
#      hooks, and `evo doctor cursor` agrees.
#   2. The drain round-trip: a queued directive, fed a simulated Cursor
#      postToolUse stdin payload, comes back out of `evo-drain --host cursor`
#      as {"additional_context": "...[EVO DIRECTIVE]..."}.
#   3. sessionStart self-registers + drains; empty queue yields {}.
#
# The only thing this CANNOT check is whether Cursor itself honors the
# returned additional_context — that's what probe.sh (needs cursor-agent) is
# for.
#
# Usage:  bash scripts/cursor_hook_probe/dryrun.sh

set -euo pipefail
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
EVO="uv run --project $REPO_ROOT/plugins/evo evo"
DRAIN="uv run --project $REPO_ROOT/plugins/evo evo-drain"
PY="uv run --project $REPO_ROOT/plugins/evo python"

pass() { printf '  PASS: %s\n' "$*"; }
die()  { printf '  FAIL: %s\n' "$*" >&2; exit 1; }

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

# ---------------------------------------------------------------------------
echo "[1] install + doctor against a throwaway CURSOR_HOME"
export CURSOR_HOME="$TMP/dotcursor"
mkdir -p "$CURSOR_HOME"
# Pre-seed an unrelated user hook to prove we merge, not clobber.
cat > "$CURSOR_HOME/hooks.json" <<'JSON'
{ "version": 1, "hooks": { "afterFileEdit": [{ "command": "echo mine" }] } }
JSON

# Only exercise the hook-writing half here (skip npx skills network) by
# calling the adapter's writer + doctor directly with CURSOR_HOME set.
$PY - <<PY
from evo.host_install import cursor
cursor._write_inject_hooks()
PY

HOOKS="$CURSOR_HOME/hooks.json"
grep -q '"afterFileEdit"' "$HOOKS" || die "clobbered the user's existing hook"
pass "preserved user's afterFileEdit hook"
grep -q '"sessionStart"' "$HOOKS" && grep -q '"postToolUse"' "$HOOKS" || die "did not wire inject events"
grep -q 'evo-drain --host cursor' "$HOOKS" || die "drain command not written"
pass "wired sessionStart + postToolUse -> evo-drain --host cursor"

# Idempotence: writing twice must not duplicate evo entries.
$PY - <<PY
from evo.host_install import cursor
cursor._write_inject_hooks()
PY
COUNT=$($PY - <<PY
import json,os
d=json.load(open(os.path.join(os.environ["CURSOR_HOME"],"hooks.json")))
n=sum("evo-drain" in str(e.get("command","")) for ev in ("sessionStart","postToolUse") for e in d["hooks"].get(ev,[]))
print(n)
PY
)
[ "$COUNT" = "2" ] || die "expected 2 evo entries after re-run, got $COUNT (not idempotent)"
pass "idempotent re-run (2 evo entries, no dupes)"

# doctor: evo-drain isn't on PATH in this shell, so expect rc=1 but the
# hooks-wired line should be a ✓. Capture and assert on the hooks line.
DOC_OUT="$($EVO doctor cursor 2>&1 || true)"
echo "$DOC_OUT" | grep -q "inject hooks wired" || die "doctor did not confirm wired hooks"
pass "doctor confirms inject hooks wired"

echo "  uninstall removes evo entries, keeps the user's"
$EVO uninstall cursor >/dev/null 2>&1 || true
grep -q '"afterFileEdit"' "$HOOKS" || die "uninstall removed the user's hook"
if grep -q 'evo-drain' "$HOOKS"; then die "uninstall left evo entries behind"; fi
pass "uninstall removed evo entries, preserved user's afterFileEdit"

# ---------------------------------------------------------------------------
echo "[2] drain round-trip with a simulated Cursor postToolUse payload"
WORK="$TMP/repo"
mkdir -p "$WORK"; cd "$WORK"
git init -q; git config user.email d@e.x; git config user.name d
printf 'x=1\n' > main.py; git add -A; git commit -q -m init
$EVO init --target main.py --benchmark "true" --metric max --host cursor --port 0 >/dev/null

TOKEN="DRYRUN_TOK_$RANDOM"
$EVO direct "PROBE follow this: emit $TOKEN" >/dev/null 2>&1 || \
  $PY - <<PY
from pathlib import Path
from evo.inject import queue
queue.append_workspace_event(Path("$WORK"), "PROBE follow this: emit $TOKEN")
PY

SID="conv-$RANDOM"
# sessionStart: self-registers the session and drains the workspace queue.
SS_OUT=$(printf '{"hook_event_name":"sessionStart","conversation_id":"%s","workspace_roots":["%s"]}' "$SID" "$WORK" | $DRAIN --host cursor)
echo "    sessionStart -> $SS_OUT"
echo "$SS_OUT" | grep -q '"additional_context"' || die "sessionStart did not emit additional_context"
echo "$SS_OUT" | grep -q "$TOKEN" || die "sessionStart additional_context missing the token"
echo "$SS_OUT" | grep -q 'EVO DIRECTIVE' || die "sessionStart missing the authenticity banner"
pass "sessionStart emitted additional_context with banner + token"

# A second directive + marker -> postToolUse delivers it.
TOKEN2="DRYRUN_TOK2_$RANDOM"
$PY - <<PY
from pathlib import Path
from evo.inject import queue, marker
root=Path("$WORK")
queue.append_workspace_event(root, "second: emit $TOKEN2")
marker.touch(root, "$SID")
PY
PT_OUT=$(printf '{"hook_event_name":"postToolUse","conversation_id":"%s","workspace_roots":["%s"]}' "$SID" "$WORK" | $DRAIN --host cursor)
echo "    postToolUse -> $PT_OUT"
echo "$PT_OUT" | grep -q '"additional_context"' || die "postToolUse did not emit additional_context"
echo "$PT_OUT" | grep -q "$TOKEN2" || die "postToolUse missing second token"
pass "postToolUse delivered queued directive as additional_context"

# Empty queue (offset now caught up, no marker) -> {}.
EMPTY_OUT=$(printf '{"hook_event_name":"postToolUse","conversation_id":"%s","workspace_roots":["%s"]}' "$SID" "$WORK" | $DRAIN --host cursor)
echo "    postToolUse (drained) -> $EMPTY_OUT"
[ "$EMPTY_OUT" = "{}" ] || die "expected {} when nothing queued, got $EMPTY_OUT"
pass "empty queue yields {} (no spurious injection)"

echo
echo "DRY RUN PASSED — evo-side Cursor inject contract verified."
echo "Remaining unknown (needs cursor-agent): does Cursor splice the returned"
echo "additional_context into the agent turn / fire hooks headless. Run probe.sh."

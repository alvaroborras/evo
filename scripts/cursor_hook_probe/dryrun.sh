#!/usr/bin/env bash
# Hermetic dry run for the Cursor host integration. No network, no
# cursor-agent binary. Validates everything on evo's side of the contract:
#
#   1. `evo install cursor` writes ~/.cursor/hooks.json (sessionStart + stop
#      -> evo-drain --host cursor) without clobbering existing hooks, and
#      `evo doctor cursor` agrees.
#   2. The drain round-trip:
#        - sessionStart registers the session and emits {} (no delivery — the
#          IDE drops additional_context, so we don't inject there).
#        - a queued + marked directive, fed a simulated `stop` payload, comes
#          back as {"followup_message": "...[EVO DIRECTIVE]..."} (the channel
#          the IDE auto-submits as a visible message).
#        - empty queue yields {}.
#
# The only thing this CANNOT check is whether Cursor itself auto-submits the
# returned followup_message — that's what probe.sh (needs cursor-agent) is for.
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
for ev in sessionStart beforeSubmitPrompt preToolUse stop; do
  grep -q "\"$ev\"" "$HOOKS" || die "did not wire inject event: $ev"
done
grep -q 'evo-drain --host cursor' "$HOOKS" || die "drain command not written"
pass "wired sessionStart + beforeSubmitPrompt + preToolUse + stop -> evo-drain --host cursor"

# Idempotence: writing twice must not duplicate evo entries.
$PY - <<PY
from evo.host_install import cursor
cursor._write_inject_hooks()
PY
COUNT=$($PY - <<PY
import json,os
d=json.load(open(os.path.join(os.environ["CURSOR_HOME"],"hooks.json")))
n=sum("evo-drain" in str(e.get("command","")) for ev in ("sessionStart","beforeSubmitPrompt","preToolUse","stop") for e in d["hooks"].get(ev,[]))
print(n)
PY
)
[ "$COUNT" = "4" ] || die "expected 4 evo entries after re-run, got $COUNT (not idempotent)"
pass "idempotent re-run (4 evo entries, no dupes)"

DOC_OUT="$($EVO doctor cursor 2>&1 || true)"
echo "$DOC_OUT" | grep -q "inject hooks wired" || die "doctor did not confirm wired hooks"
pass "doctor confirms inject hooks wired"

echo "  uninstall removes evo entries, keeps the user's"
$EVO uninstall cursor >/dev/null 2>&1 || true
grep -q '"afterFileEdit"' "$HOOKS" || die "uninstall removed the user's hook"
if grep -q 'evo-drain' "$HOOKS"; then die "uninstall left evo entries behind"; fi
pass "uninstall removed evo entries, preserved user's afterFileEdit"

# ---------------------------------------------------------------------------
echo "[2] drain round-trip: sessionStart registers, stop delivers"
WORK="$TMP/repo"
mkdir -p "$WORK"; cd "$WORK"
git init -q; git config user.email d@e.x; git config user.name d
printf 'x=1\n' > main.py; git add -A; git commit -q -m init
$EVO init --target main.py --benchmark "true" --metric max --host cursor --port 0 >/dev/null

SID="conv-$RANDOM"
# sessionStart: registers the session, seeds the offset, emits {} (no consume).
SS_OUT=$(printf '{"hook_event_name":"sessionStart","conversation_id":"%s","workspace_roots":["%s"]}' "$SID" "$WORK" | $DRAIN --host cursor)
echo "    sessionStart -> $SS_OUT"
[ "$SS_OUT" = "{}" ] || die "sessionStart should emit {} (register-only), got $SS_OUT"
ls "$WORK"/.evo/run_*/inject/sessions/"$SID".json >/dev/null 2>&1 || die "sessionStart did not register the session"
pass "sessionStart registered the session and emitted {} (no consume)"

# A directive queued AFTER session start, with the session marked.
TOKEN="DRYRUN_TOK_$RANDOM"
$PY - <<PY
from pathlib import Path
from evo.inject import queue, marker
root = Path("$WORK")
queue.append_workspace_event(root, "follow this: emit $TOKEN")
marker.touch(root, "$SID")
PY
# stop: delivers the directive as followup_message (the IDE auto-submits it).
ST_OUT=$(printf '{"hook_event_name":"stop","conversation_id":"%s","workspace_roots":["%s"]}' "$SID" "$WORK" | $DRAIN --host cursor)
echo "    stop -> $ST_OUT"
echo "$ST_OUT" | grep -q '"followup_message"' || die "stop did not emit followup_message"
echo "$ST_OUT" | grep -q "$TOKEN" || die "stop followup_message missing the token"
echo "$ST_OUT" | grep -q 'EVO DIRECTIVE' || die "stop missing the authenticity banner"
pass "stop delivered the directive as followup_message with banner + token"

# Empty queue (offset caught up, marker cleared) -> {}.
EMPTY_OUT=$(printf '{"hook_event_name":"stop","conversation_id":"%s","workspace_roots":["%s"]}' "$SID" "$WORK" | $DRAIN --host cursor)
echo "    stop (drained) -> $EMPTY_OUT"
[ "$EMPTY_OUT" = "{}" ] || die "expected {} when nothing queued, got $EMPTY_OUT"
pass "empty queue yields {} (no spurious followup / no loop)"

# ---------------------------------------------------------------------------
echo "[3] preToolUse tool-aware delivery (mid-turn)"
ptu() { printf '{"hook_event_name":"preToolUse","conversation_id":"%s","workspace_roots":["%s"],"tool_name":"%s","tool_input":%s}' "$SID" "$WORK" "$1" "$2" | $DRAIN --host cursor; }
qmark() { $PY -c "from pathlib import Path; from evo.inject import queue, marker; r=Path('$WORK'); queue.append_workspace_event(r,'$1'); marker.touch(r,'$SID')"; }

qmark "T_SHELL"
SH=$(ptu Shell '{"command":"ls -la"}')
echo "    Shell -> $SH"
echo "$SH" | grep -q '"updated_input"' && echo "$SH" | grep -q 'T_SHELL' && echo "$SH" | grep -q 'ls -la' || die "Shell preToolUse should rewrite command with directive echo"
pass "Shell preToolUse -> updated_input echo (non-disruptive, command preserved)"

# Non-shell tools (Read, Edit, ...) DEFER — no consume — so stop can deliver.
# (deny+agent_message consumed the directive without delivering it; deferring
# is the fix.)
qmark "T_READ"
RD=$(ptu Read '{"path":"main.py"}')
echo "    Read -> $RD"
[ "$RD" = "{}" ] || die "Read preToolUse should DEFER (emit {}), got $RD"
RDSTOP=$(printf '{"hook_event_name":"stop","conversation_id":"%s","workspace_roots":["%s"]}' "$SID" "$WORK" | $DRAIN --host cursor)
echo "    stop after read -> $RDSTOP"
echo "$RDSTOP" | grep -q 'T_READ' || die "deferred read directive lost — should deliver on next stop"
pass "Read preToolUse defers (no consume); directive delivered on next stop"

qmark "T_EDIT"
ED=$(ptu Edit '{"path":"main.py"}')
echo "    Edit -> $ED"
[ "$ED" = "{}" ] || die "Edit preToolUse should DEFER (emit {}), got $ED"
EDSTOP=$(printf '{"hook_event_name":"stop","conversation_id":"%s","workspace_roots":["%s"]}' "$SID" "$WORK" | $DRAIN --host cursor)
echo "    stop after edit -> $EDSTOP"
echo "$EDSTOP" | grep -q 'T_EDIT' || die "deferred edit directive lost — should deliver on next stop"
pass "Edit preToolUse defers (no consume); directive delivered on next stop"

echo
echo "DRY RUN PASSED — evo-side Cursor inject contract verified."
echo "Remaining unknown (needs cursor-agent / IDE): does Cursor auto-submit the"
echo "returned followup_message. Reproduce in the IDE with the debug log on."

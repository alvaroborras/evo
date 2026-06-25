#!/usr/bin/env bash
# wait_hint.sh — PostToolUse hook that emits a one-shot reminder to use
# `evo wait` instead of a tail-loop when the agent starts a long-running
# command (training, vLLM serve, eval, accelerate launch, explicit
# backgrounding, or the polling anti-pattern itself).
#
# Contract:
#   - Reads the host's PostToolUse JSON payload from stdin.
#   - Matches against Bash tool_input.command only; ignores all other tools.
#   - Prints the hint to stdout (Claude Code routes PostToolUse stdout into
#     the next-turn context as a non-blocking system reminder).
#   - Deduplicated per session via a marker file under $TMPDIR.
#   - Exits 0 always. Never blocks the agent. Failures are swallowed.
#
# Pairs with the polling-discipline section in plugins/evo/skills/{discover,
# optimize}/SKILL.md and the `evo wait` CLI surface.

set -u

emit_empty() {
  printf '{}\n'
  exit 0
}

emit_hint() {
  # Codex and Claude Code hooks expect JSON stdout. Raw reminder text or blank
  # stdout is treated as invalid hook output by Codex.
  printf '%s\n' '{"hookSpecificOutput":{"hookEventName":"PostToolUse","additionalContext":"[evo-hint] Long-running command detected. Use `evo wait --for process=<pid> --for log-growth=<path> --for gpu-active --timeout 60m --json` with process liveness, log delta, GPU activity, bounded timeout, and structured exit instead of tail-loop polling. See `evo wait --help`."}}'
  exit 0
}

# Read all of stdin (small JSON payload). Tolerate non-pipe invocation.
payload=""
if [ ! -t 0 ]; then
  payload="$(cat 2>/dev/null || true)"
fi

# Extract a JSON string value for KEY from PAYLOAD via a tolerant grep.
# Handles either top-level keys (`"tool_name":"Bash"`) or nested under
# `tool_use` (the synthetic test format in the issue). No jq dep — host
# environments don't guarantee it.
extract_str() {
  key="$1"
  printf '%s' "$payload" \
    | grep -o "\"${key}\"[[:space:]]*:[[:space:]]*\"[^\"]*\"" \
    | head -1 \
    | sed -E "s/^\"${key}\"[[:space:]]*:[[:space:]]*\"(.*)\"$/\1/"
}

tool_name="$(extract_str tool_name)"
if [ -z "$tool_name" ]; then
  # Fall back to the nested `tool_use.name` shape used in the task's
  # synthetic test payload.
  tool_name="$(extract_str name)"
fi

# Only care about shell tool calls.
case "$tool_name" in
  Bash|bash|shell|Shell) ;;
  *) emit_empty ;;
esac

# Extract the command string. Same dual-shape tolerance: top-level
# `tool_input.command` or nested `tool_use.input.command`. Both reduce to a
# `"command":"..."` substring so one grep covers both.
command_str="$(printf '%s' "$payload" \
  | grep -o '"command"[[:space:]]*:[[:space:]]*"[^"]*"' \
  | head -1 \
  | sed -E 's/^"command"[[:space:]]*:[[:space:]]*"(.*)"$/\1/')"

if [ -z "$command_str" ]; then
  emit_empty
fi

# Pattern table — match against the raw command string. Each pattern is a
# basic-regex tested via grep -E for portability across bash/dash.
matched=0
while IFS= read -r pattern; do
  [ -z "$pattern" ] && continue
  if printf '%s' "$command_str" | grep -Eq "$pattern"; then
    matched=1
    break
  fi
done <<'PATTERNS'
python[0-9.]*[[:space:]]+([^|;&]*[[:space:]])?[^|;&]*train
python[0-9.]*[[:space:]]+([^|;&]*[[:space:]])?[^|;&]*evaluate\.py
python[0-9.]*[[:space:]]+([^|;&]*[[:space:]])?[^|;&]*run_eval\.py
python[0-9.]*[[:space:]]+([^|;&]*[[:space:]])?[^|;&]*eval
vllm[[:space:]]+serve
trl[[:space:]]+vllm-serve
accelerate[[:space:]]+launch
nohup[[:space:]]+.*&[[:space:]]*$
sleep[[:space:]]+[0-9]+.*(;|&&|\|\|).*tail
tail[[:space:]]+.*(;|&&|\|\|).*sleep[[:space:]]+[0-9]+
while[[:space:]]+true.*sleep
while[[:space:]]+:.*sleep
PATTERNS

if [ "$matched" -eq 0 ]; then
  emit_empty
fi

# Dedup per session. session_id comes from the same payload; on miss we
# fall back to a stable "no-session" marker so multiple identical hits in
# one shell still dedup.
session_id="$(extract_str session_id)"
[ -z "$session_id" ] && session_id="no-session"

marker_dir="${TMPDIR:-/tmp}"
marker_file="${marker_dir%/}/evo-wait-hint-${session_id}.shown"
if [ -e "$marker_file" ]; then
  emit_empty
fi

# Touch BEFORE printing so concurrent hook fires don't double-emit.
: > "$marker_file" 2>/dev/null || true

emit_hint

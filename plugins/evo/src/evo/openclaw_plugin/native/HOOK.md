# Evo Mid-Run Inject

OpenClaw plugin that delivers `evo direct` directives mid-conversation by appending them to the most recent tool-result message via the `tool_result_persist` hook.

## How it delivers

1. Subscribe to `tool_result_persist`.
2. On every tool result, drain the evo workspace inject queue.
3. If a directive is queued, return the tool-result message with the directive appended.
4. The LLM sees the directive as part of the tool-result message in its next reasoning step.

## Install

Installed automatically by `evo install openclaw`. Set `EVO_DEBUG_INJECT=1` to write diagnostic logs to `/tmp/evo-inject.log`.

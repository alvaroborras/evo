// Opencode plugin entry — registered via opencode auto-discovery in
// `~/.config/opencode/plugins/evo.js` (or `.opencode/plugins/` per-workspace).
//
// Hook contract (verified against sst/opencode `packages/plugin/src/index.ts`):
//   - `chat.message`: fires per user message; mutating `parts` injects text
//     into LLM input. Used to drain directives + auto-arm optimize_mode.
//   - `tool.execute.before`: fires pre-tool. THROW to deny — opencode shows
//     the error message to the model as "Blocked by policy" feedback.
//     Used as the policy gate: deny when orchestrator + optimize_mode +
//     denied tool (alternating cadence).
//   - `event`: fires for system events. `event.type === "session.idle"`
//     is the turn-end signal (analogue of claude-code Stop). When the
//     orchestrator session is in optimize_mode, send a follow-up message
//     via `client.session.prompt(...)` so the loop continues autonomously.
//
// There is NO `stop` hook on opencode — verified absent from the Hooks
// type in upstream. The always-fire stop nudge uses session.idle + the
// SDK's session.prompt recipe.

import {
  POLICY_NUDGE_TEMPLATE,
  commitDrainPeek,
  drainSession,
  findEvoRunDir,
  formatDirectiveText,
  getSession,
  initOffsetToLatest,
  isEvoCommand,
  isRegistered,
  markAutonomous,
  markEngaged,
  markOptimizeMode,
  markSubagentsOnly,
  maybeMarkOptimizeFromPrompt,
  maybeStopNudgeText,
  peekDrainSession,
  registerSession,
  shouldPolicyBlock,
  unmarkAutonomous,
  unmarkSubagentsOnly,
} from "./drain.js"
import * as fs from "fs"
import * as path from "path"

function markerExists(runDir: string, sid: string): boolean {
  return fs.existsSync(path.join(runDir, "inject", "markers", `${sid}.flag`))
}

function extractPromptTextFromParts(parts: any): string {
  // chat.message's upstream contract puts the user prompt content in
  // `output.parts: Part[]` — typically a single `{type: "text", text}`
  // for plain prompts. `UserMessage` has metadata only, NOT the prompt
  // text. Read from parts (verified against
  // sst/opencode/packages/plugin/src/index.ts).
  if (!Array.isArray(parts)) return ""
  for (const p of parts) {
    if (p && p.type === "text" && typeof p.text === "string" && p.text) {
      return p.text
    }
  }
  return ""
}

/**
 * Opencode plugin factory — returns hook handlers per opencode's plugin SDK.
 */
export const EvoPlugin = async ({ project, client }: any) => {
  // Idempotent register — only writes the registry file if absent. Safe
  // to call every tool call; cost is a single fs.existsSync check after
  // first registration.
  const ensureRegistered = (sessionID: string | undefined): string | null => {
    if (!sessionID) return null
    const runDir = findEvoRunDir(project?.directory)
    if (!runDir) return null
    if (!isRegistered(runDir, sessionID)) {
      registerSession(runDir, sessionID, "opencode")
    }
    return runDir
  }

  return {
    "chat.message": async (input: any, output: any) => {
      const sessionID: string | undefined = input?.sessionID
      if (!sessionID) return

      const runDir = ensureRegistered(sessionID)
      if (!runDir) return

      // Auto-arm optimize_mode if the user's prompt matches `/optimize`.
      // Prompt text lives in output.parts (Part[]), NOT in message — the
      // upstream `chat.message` contract has UserMessage carry metadata,
      // and parts as a separate array.
      const promptText = extractPromptTextFromParts(output?.parts)
      maybeMarkOptimizeFromPrompt(runDir, sessionID, "opencode", promptText)

      // Drain queued directives. On first fire the marker may not exist
      // yet (just registered); drainSession is cheap on empty queues.
      const result = drainSession(runDir, sessionID)
      if (!result.text) return

      if (!Array.isArray(output.parts)) {
        output.parts = []
      }
      const messageID: string =
        input?.messageID ?? output?.message?.id ?? output.parts[0]?.messageID ?? ""
      const partID = `prt_evo_${Date.now().toString(36)}_${Math.random().toString(36).slice(2, 10)}`
      output.parts.unshift({
        type: "text",
        id: partID,
        sessionID,
        messageID,
        text: result.text,
      })
    },

    "tool.execute.before": async (input: any, output: any) => {
      // Fires before every tool call. Three jobs:
      //  1. Keep the session registered in the CURRENT active run.
      //  2. Engagement detection — flip has_evo_engaged on `evo …` shells
      //     so `evo direct` fanout can reach this session.
      //  3. POLICY GATE — when the orchestrator (no exp_id) is in
      //     optimize_mode and the tool is on the deny list, throw to
      //     block. Alternating cadence (odd violations throw; even pass).
      const runDir = ensureRegistered(input?.sessionID)
      if (!runDir) return
      const sid: string | undefined = input?.sessionID
      if (!sid) return

      const toolName = (input?.tool || input?.toolName || "").toString()
      const toolArgs = output?.args ?? input?.args ?? {}

      // Engagement detection for evo shell commands.
      if (toolName.toLowerCase() === "bash" || toolName.toLowerCase() === "shell") {
        const cmd = (toolArgs as any)?.command ?? ""
        if (typeof cmd === "string" && isEvoCommand(cmd)) {
          if (markEngaged(runDir, sid)) {
            initOffsetToLatest(runDir, sid)
          }
          // Autonomous arming: opencode has no session env var, so the
          // `evo autonomous on` CLI can't self-detect the session — we
          // observe the command here and arm/disarm in-process. Not prose:
          // this fires only on the actual command execution.
          if (/^\s*evo\s+exit-optimize-mode\b/.test(cmd)) {
            unmarkAutonomous(runDir, sid)
            unmarkSubagentsOnly(runDir, sid)
          } else if (/^\s*evo\s+autonomous\s+off\s*$/.test(cmd)) {
            unmarkAutonomous(runDir, sid)
          } else if (/^\s*evo\s+autonomous(\s+on)?\s*$/.test(cmd)) {
            markAutonomous(runDir, sid)
          } else if (/^\s*evo\s+subagents-only\s+off\s*$/.test(cmd)) {
            unmarkSubagentsOnly(runDir, sid)
          } else if (/^\s*evo\s+subagents-only(\s+on)?\s*$/.test(cmd)) {
            markSubagentsOnly(runDir, sid)
          }
        }
      }

      // Policy gate. shouldPolicyBlock returns true on odd-numbered
      // violations under optimize_mode; throws here become "Blocked by
      // policy" feedback to the model.
      if (shouldPolicyBlock(runDir, sid, toolName, toolArgs)) {
        throw new Error(POLICY_NUDGE_TEMPLATE)
      }
    },

    event: async ({ event }: any) => {
      // Always-fire stop nudge on session.idle (the opencode analogue of
      // claude-code Stop). When the orchestrator session is in optimize_mode,
      // inject a follow-up message via client.session.prompt so the agent
      // keeps driving the /optimize loop. Escape hatch: `evo exit-optimize-mode`.
      if (!event || event.type !== "session.idle") return
      const sessionID: string | undefined =
        event.properties?.sessionID ?? event.sessionID ?? event.session_id
      if (!sessionID) return
      const runDir = findEvoRunDir(project?.directory)
      if (!runDir) return
      const sess = getSession(runDir, sessionID) as any
      if (!sess || sess.exp_id || !sess.optimize_mode || !sess.autonomous) return

      // Bail BEFORE any state mutation if we can't actually deliver.
      // session.prompt is the only delivery channel here; if it's
      // missing, consuming queued directives would silently lose them.
      if (!client || typeof client.session?.prompt !== "function") return

      // Peek (don't pop) so a failed injection doesn't consume the
      // directive. Commit offsets + unlink marker only after the
      // session.prompt call succeeds.
      const peek = peekDrainSession(runDir, sessionID)
      const nudge = maybeStopNudgeText(runDir, sessionID)
      if (!nudge) return
      const text = peek.text ? peek.text + "\n\n" + nudge : nudge

      try {
        await client.session.prompt({
          path: { id: sessionID },
          body: {
            parts: [{ type: "text", text }],
          },
        })
        // Success — now commit the drain.
        commitDrainPeek(runDir, sessionID, peek)
      } catch (_e) {
        // Don't crash the agent on a stop-nudge failure. Directives
        // stay queued; next session.idle (or chat.message) will retry.
      }
    },
  }
}

export default EvoPlugin

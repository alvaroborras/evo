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

      // Directives are NOT drained here. `chat.message` fires only at
      // user-message creation, never for a mid-run `evo direct`, so the
      // directive's text would never ride this hook. Worse, a consume-once
      // drain here advances the offset + unlinks the marker on unrelated
      // message creations (including subagent prompts), silently discarding
      // the directive before the model ever sees it. Delivery happens in the
      // `event` (session.idle) handler via client.session.prompt — the one
      // channel that reaches the model mid-run — with peek + commit-after-send.
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

    "tool.execute.after": async (input: any, output: any) => {
      // PRIMARY mid-run directive delivery. In single-shot `opencode run`
      // the orchestrator runs an entire /optimize round-loop in ONE turn
      // (plan -> spawn subagents -> `evo wait` -> plan next round), so
      // session.idle never fires mid-run and can't surface a mid-run
      // `evo direct`. tool.execute hooks DO fire on every tool call during
      // the turn — including when `evo wait` returns, exactly when the
      // orchestrator is about to plan the next round — so we surface queued
      // directives by appending them to the tool result the model reads next.
      const sessionID: string | undefined = input?.sessionID
      if (!sessionID) return
      const runDir = ensureRegistered(sessionID)
      if (!runDir) return
      const sess = getSession(runDir, sessionID) as any
      if (!sess) return
      // Deliver to the engaged orchestrator or a subagent with a targeted
      // directive — the same set `evo direct` fans out to.
      if (!sess.exp_id && !sess.has_evo_engaged) return

      // Peek (don't pop) so a delivery that never lands isn't lost. Append
      // the directive (it carries the [EVO DIRECTIVE id=...] banner + the
      // `evo ack` instruction) to this tool's result, then commit the drain
      // so it isn't re-appended to every subsequent tool call.
      const peek = peekDrainSession(runDir, sessionID)
      if (!peek.text) return
      if (typeof output?.output === "string") {
        output.output = output.output
          ? output.output + "\n\n" + peek.text
          : peek.text
      } else if (output) {
        output.output = peek.text
      } else {
        return // nowhere to write — leave queued for the next tool call
      }
      commitDrainPeek(runDir, sessionID, peek)
    },

    event: async ({ event }: any) => {
      // session.idle is the opencode turn boundary (the analogue of
      // claude-code Stop). Two INDEPENDENT jobs, both delivered via
      // client.session.prompt — the one channel that reaches the model
      // mid-run (chat.message can't; it fires only at user-message creation):
      //   1. Deliver queued mid-run directives (steering). Fires whenever the
      //      engaged orchestrator is in optimize_mode — NOT gated on autonomous,
      //      and NOT dependent on a nudge being present. A subagent session
      //      delivers directives targeted at it (its own exp queue), no nudge.
      //   2. Stop-nudge to keep the /optimize loop going. Orchestrator only,
      //      and only when autonomous (maybeStopNudgeText self-gates on it).
      // Escape hatch: `evo exit-optimize-mode`.
      if (!event || event.type !== "session.idle") return
      const sessionID: string | undefined =
        event.properties?.sessionID ?? event.sessionID ?? event.session_id
      if (!sessionID) return
      const runDir = findEvoRunDir(project?.directory)
      if (!runDir) return
      const sess = getSession(runDir, sessionID) as any
      if (!sess) return
      const isSubagent = !!sess.exp_id
      // A non-optimize-mode orchestrator has nothing to deliver. Subagents
      // still receive directives targeted at them regardless of the flag.
      if (!isSubagent && !sess.optimize_mode) return

      // session.prompt is the only delivery channel here; if it's missing,
      // peeking-then-committing would silently lose the directive.
      if (!client || typeof client.session?.prompt !== "function") return

      // Peek (don't pop) so a failed send doesn't consume the directive.
      // Commit offsets + unlink the marker only after the send succeeds.
      const peek = peekDrainSession(runDir, sessionID)
      const nudge = isSubagent ? null : maybeStopNudgeText(runDir, sessionID)

      // Deliver if EITHER a directive or a nudge is pending — directive
      // delivery must not depend on the nudge being present.
      const blocks = [peek.text, nudge].filter(Boolean)
      if (blocks.length === 0) return
      const text = blocks.join("\n\n")

      try {
        await client.session.prompt({
          path: { id: sessionID },
          body: {
            parts: [{ type: "text", text }],
          },
        })
        // Success — now commit the drain (advance offset, unlink marker).
        commitDrainPeek(runDir, sessionID, peek)
      } catch (_e) {
        // Don't crash the agent on a delivery failure. Directives stay
        // queued (not committed); the next session.idle retries.
      }
    },
  }
}

export default EvoPlugin

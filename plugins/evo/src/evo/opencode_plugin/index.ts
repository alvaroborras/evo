// Opencode plugin entry — registered via opencode auto-discovery in
// `~/.config/opencode/plugins/evo.js` (or `.opencode/plugins/` per-workspace).
//
// Two hooks:
//   `chat.message` fires per user message — prepends drained directive
//     text to the user message before it goes to the LLM. Useful when the
//     user submits multiple turns interactively.
//   `tool.execute.before` fires before each tool call — re-detects the
//     active evo run and re-registers the session there. Needed because
//     some agents (notably gpt-5 with the optimize skill) call
//     `evo init` themselves mid-session, switching the active run from
//     run_0000 to run_0001. Without re-registration, the session lives
//     in run_0000 while `evo direct` looks in run_0001 (fanout=0).
//
// See notes/cross-host-inject-design.md.

import {
  drainSession,
  findEvoRunDir,
  isRegistered,
  registerSession,
} from "./drain.js"
import * as fs from "fs"
import * as path from "path"

function markerExists(runDir: string, sid: string): boolean {
  return fs.existsSync(path.join(runDir, "inject", "markers", `${sid}.flag`))
}

/**
 * Opencode plugin factory — returns hook handlers per opencode's plugin SDK.
 */
export const EvoPlugin = async ({ project }: any) => {
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

      // On first fire, drain unconditionally to catch any directive
      // queued before the session existed. Later fires use the marker
      // file as a fast path — skip drain when nothing is queued.
      const hasMarker = markerExists(runDir, sessionID)
      if (!hasMarker) {
        // Also drain on first fire (no marker yet because session was
        // just registered) — drainSession returns null cheaply if the
        // queue is empty.
      }

      const result = drainSession(runDir, sessionID)
      if (result.text) {
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
      }
    },

    "tool.execute.before": async (input: any, _output: any) => {
      // Fires before every tool call. Used purely to keep the session
      // registered in the CURRENT active run — re-detection happens
      // inside ensureRegistered (findEvoRunDir picks the latest run_*).
      // Cheap: a single isRegistered check after the first call.
      ensureRegistered(input?.sessionID)
    },
  }
}

export default EvoPlugin

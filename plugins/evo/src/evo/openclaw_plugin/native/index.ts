// Openclaw-native plugin for evo mid-run inject.
//
// Delivery mechanism: `tool_result_persist` hook — mutate the assistant
// tool-result message before persistence. The LLM sees the directive
// as part of the most recent tool-result message on its next reasoning
// step.
//
// Why `tool_result_persist`:
//   It is the only documented hook that (a) fires per tool result in
//   embedded `openclaw agent --message` mode and (b) lets the handler
//   return a modified message that lands in the conversation stream
//   the LLM reads on its next turn. `agent_turn_prepare` /
//   `before_prompt_build` fire only at session start in embedded mode;
//   `enqueueNextTurnInjection` queues to a boundary that never fires
//   again; `before_tool_call` mutates tool params, which is the wrong
//   semantic (the directive is not part of the tool call).
//
// Session register: subscribe to `session_start` for early register
// so `evo direct` fanout includes this run before the first tool
// result lands. A 1500ms poll covers runtimes where session_start
// does not fire.

import {
  drainSession,
  findEvoRunDir,
  isRegistered,
  registerSession,
} from "../../opencode_plugin/drain.js"
import * as crypto from "crypto"
import * as fs from "fs"
import * as os from "os"
import * as path from "path"

const DEBUG = process.env.EVO_DEBUG_INJECT === "1"
// Detect re-entry on the same message by checking for the banner's
// open tag — it's user-visible but unique enough that no honest tool
// output would contain it.
const BANNER_OPEN = "[EVO DIRECTIVE]"
const BANNER_CLOSE = "[END EVO DIRECTIVE]"

function log(line: string) {
  if (!DEBUG) return
  try {
    fs.appendFileSync(
      "/tmp/evo-inject.log",
      `[${new Date().toISOString()}] ${line}\n`,
    )
  } catch {}
}

function findOpenclawRunDir(): string | null {
  const cwdRun = findEvoRunDir(process.cwd())
  if (cwdRun) return cwdRun
  const fallback = path.join(os.homedir(), ".openclaw", "workspace")
  if (fs.existsSync(fallback)) {
    return findEvoRunDir(fallback)
  }
  return null
}

function deriveSessionId(): string {
  const runDir = findOpenclawRunDir() || process.cwd()
  const marker = "/.evo/"
  const idx = runDir.indexOf(marker)
  const workspace = idx >= 0 ? runDir.slice(0, idx) : process.cwd()
  const hash = crypto.createHash("sha256").update(workspace).digest("hex").slice(0, 12)
  return "openclaw-" + hash
}

// Subagents share workspace cwd, so they hash to the same sid; once
// the parent's drain advances the on-disk offset the subagent's drain
// returns null. Cache the drained text so the directive re-appends
// to every subsequent tool-result message until session end.
const drainedTexts: string[] = []

function directiveBanner(): string {
  if (drainedTexts.length === 0) return ""
  // Same banner shape every transport emits — `optimize` and `subagent`
  // skills document it as the authenticity signal for user directives.
  return [
    ``,
    BANNER_OPEN,
    drainedTexts.join("\n\n"),
    BANNER_CLOSE,
  ].join("\n")
}

export default {
  id: "evo-inject",
  name: "Evo Mid-Run Inject",
  description:
    "Delivers `evo direct` directives mid-conversation by appending them to the most recent tool-result message via tool_result_persist.",
  register(api: any) {
    log(`register() called, cwd=${process.cwd()}`)

    const ensureRegistered = () => {
      const runDir = findOpenclawRunDir()
      if (!runDir) return null
      const sid = deriveSessionId()
      if (!isRegistered(runDir, sid)) {
        registerSession(runDir, sid, "openclaw")
        log(`registered session ${sid} in ${runDir}`)
      }
      return { runDir, sid }
    }

    const pumpDirectives = (runDir: string, sid: string) => {
      const result = drainSession(runDir, sid)
      if (result.text) {
        drainedTexts.push(result.text)
        log(`drained ${result.text.length} bytes`)
      }
    }

    // Defensive coverage: different openclaw runtimes (versions, agent
    // modes) emit different startup events. Subscribing to all keeps
    // session registration race-free across runtimes. `ensureRegistered`
    // is idempotent so duplicate calls cost nothing.
    for (const ev of ["agent_turn_prepare", "before_prompt_build", "before_agent_run", "session_start"]) {
      try {
        api.on(ev, async () => {
          ensureRegistered()
        })
      } catch {}
    }

    // Last-resort poll for runtimes where none of the above fire.
    // Cheap (.unref()) — does not hold the process open after exit.
    const interval = setInterval(() => {
      try {
        const ctx = ensureRegistered()
        if (ctx) pumpDirectives(ctx.runDir, ctx.sid)
      } catch {}
    }, 1500)
    if (typeof (interval as any).unref === "function") {
      ;(interval as any).unref()
    }

    api.on("tool_result_persist", async (event: any) => {
      const ctx = ensureRegistered()
      if (ctx) pumpDirectives(ctx.runDir, ctx.sid)

      if (drainedTexts.length === 0) return undefined

      // Per docs: handler returns the modified message (rewrites
      // `details` or `content`). Payload shape is not documented
      // verbatim — sniff the message and append to whichever text
      // field exists.
      const msg = event?.message ?? event?.assistantMessage ?? event
      if (!msg || typeof msg !== "object") return undefined

      const banner = directiveBanner()
      let mutated = false
      const tryAppendString = (obj: any, key: string) => {
        // Re-entry guard: skip if this message already contains the
        // banner (avoids double-wrapping on tool-result replay).
        if (typeof obj?.[key] === "string" && !obj[key].includes(BANNER_OPEN)) {
          obj[key] = obj[key] + banner
          mutated = true
          return true
        }
        return false
      }

      if (Array.isArray(msg.content)) {
        for (const part of msg.content) {
          if (part && typeof part === "object") {
            if (tryAppendString(part, "text")) break
            if (tryAppendString(part, "output")) break
          }
        }
      }
      if (!mutated) tryAppendString(msg, "content")
      if (!mutated && msg.details && typeof msg.details === "object") {
        tryAppendString(msg.details, "text")
        tryAppendString(msg.details, "output")
        tryAppendString(msg.details, "stdout")
        tryAppendString(msg.details, "content")
      }
      if (!mutated) tryAppendString(msg, "text")

      return mutated ? msg : undefined
    })
  },
}

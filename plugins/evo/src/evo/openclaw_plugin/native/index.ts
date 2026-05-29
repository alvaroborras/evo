// Openclaw-native plugin for evo /optimize steering + mid-run inject.
//
// Four hook subscriptions, each verified against openclaw's
// `src/plugins/hook-types.ts` and `src/plugins/hooks.ts`:
//
//   - `tool_result_persist` — mid-run directive delivery. Appends the
//     latest `evo direct` payload to the most recent tool-result
//     message so the LLM sees it on its next reasoning step.
//
//   - `before_prompt_build` — `/optimize` auto-arm. The hook receives
//     the user prompt before the LLM call (docs/concepts/agent-loop.md:
//     "runs after session load to inject prependContext... before
//     prompt submission"). Pattern match flips optimize_mode on the
//     session record.
//
//   - `before_tool_call` — synchronous deny gate. Returning
//     `{ block: true, blockReason: ... }` short-circuits the tool and
//     feeds blockReason back to the model as the error. Dispatcher at
//     hooks.ts:569 (`shouldStop: result.block === true`, terminal).
//
//   - `before_agent_finalize` — stop nudge. Returning
//     `{ action: "revise", retry: { instruction, idempotencyKey } }`
//     forces another model pass with the EVO LOOP banner so the agent
//     keeps driving /optimize autonomously. Merger at hooks.ts:166
//     preserves `revise` over `continue`.
//
// Session register: subscribe to `session_start` + the prompt-build
// events for early register so `evo direct` fanout includes this run
// before the first tool result lands. A 1500ms poll covers runtimes
// where none of the startup events fire.

import {
  POLICY_NUDGE_TEMPLATE,
  STOP_NUDGE_TEMPLATE,
  drainSession,
  findEvoRunDir,
  initOffsetToLatest,
  isRegistered,
  markAutonomous,
  markEngaged,
  maybeMarkOptimizeFromPrompt,
  maybeStopNudgeText,
  registerSession,
  shouldPolicyBlock,
  unmarkAutonomous,
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
  // Include EVO_EXP_ID in the seed so subagents get a distinct sid
  // from the parent and from each other. See factory.ts deriveSessionId
  // for the design rationale.
  const expId = process.env.EVO_EXP_ID || ""
  const seed = expId ? `${workspace}|${expId}` : workspace
  const hash = crypto.createHash("sha256").update(seed).digest("hex").slice(0, 12)
  return "openclaw-" + hash
}

// Subagents share workspace cwd, so they hash to the same sid; once
// the parent's drain advances the on-disk offset the subagent's drain
// returns null. Cache the drained text so the directive re-appends
// to every subsequent tool-result message until session end.
const drainedTexts: string[] = []

function directiveBanner(): string {
  if (drainedTexts.length === 0) return ""
  // drainedTexts entries are already wrapped with
  // [EVO DIRECTIVE]...[END EVO DIRECTIVE] by formatDirectiveText()
  // inside drainSession() (shared with opencode plugin). Don't
  // double-wrap — produces nested banners that violate the documented
  // contract in optimize/subagent skills and that stricter models
  // (opus-4-7) refuse outright.
  return "\n" + drainedTexts.join("\n\n")
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
      // Subagent gets a distinct sid (via EVO_EXP_ID in the hash);
      // pass exp_id at first registration so the record is tagged
      // correctly from birth.
      if (!isRegistered(runDir, sid)) {
        const expId = process.env.EVO_EXP_ID || null
        registerSession(runDir, sid, "openclaw", expId)
        log(`registered session ${sid} in ${runDir}${expId ? " (exp_id=" + expId + ")" : ""}`)
        // Engage the session immediately. The original
        // scan-the-LLM-payload engagement signal doesn't work for
        // openclaw orchestrators that dispatch all evo work to
        // subagents (claude-sonnet-4-5 commonly does this) — the
        // parent's own payload never contains `evo …` so it stays
        // unengaged forever and `evo direct` falls through with
        // fanout=0 / skipped_unengaged=1. The engagement gate exists
        // to filter stale registered-but-inactive sessions; for
        // openclaw the host process IS the orchestrator, so its
        // existence is the engagement signal. Same fix shape as the
        // pi-extension bundle (see factory.ts).
        if (markEngaged(runDir, sid)) {
          initOffsetToLatest(runDir, sid)
        }
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
    // `before_prompt_build` is handled below (it does both register and
    // auto-arm); listing it here would register twice on the same hook.
    for (const ev of ["agent_turn_prepare", "before_agent_run", "session_start"]) {
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

    // Extract the most recent user-text from a before_prompt_build
    // event payload. openclaw's prompt-build payload shape is not
    // fully documented; sniff both `messages[]` (anthropic style) and
    // `input[]` (openai style) and pick the trailing user message.
    const extractLatestUserText = (event: any): string | null => {
      try {
        const sources = [event?.messages, event?.payload?.messages,
                         event?.input, event?.payload?.input,
                         event?.userMessage ? [event.userMessage] : null,
                         event?.prompt ? [{ role: "user", content: event.prompt }] : null]
        for (const arr of sources) {
          if (!Array.isArray(arr)) continue
          for (let i = arr.length - 1; i >= 0; i--) {
            const m = arr[i]
            if (!m || (m.role && m.role !== "user")) continue
            if (typeof m.content === "string" && m.content) return m.content
            if (Array.isArray(m.content)) {
              for (const c of m.content) {
                if (typeof c?.text === "string" && c.text) return c.text
              }
            }
            if (typeof m.text === "string" && m.text) return m.text
          }
        }
      } catch {}
      return null
    }

    api.on("before_prompt_build", async (event: any) => {
      const ctx = ensureRegistered()
      if (!ctx) return undefined
      try {
        const promptText = extractLatestUserText(event)
        if (promptText) {
          maybeMarkOptimizeFromPrompt(ctx.runDir, ctx.sid, "openclaw", promptText)
        }
      } catch {}
      return undefined
    })

    // Deny gate. Failure policy is "fail-closed" (hooks.ts:842), so a
    // thrown handler also denies the tool. Wrap everything defensively
    // so transient errors (e.g. inject dir missing) fail-open instead.
    api.on("before_tool_call", async (event: any) => {
      try {
        const ctx = ensureRegistered()
        if (!ctx) return undefined
        const toolName = event?.toolName ?? event?.tool_name ?? event?.tool?.name
        const toolInput = event?.params ?? event?.input ?? event?.tool?.params ?? {}
        // Autonomous arming via command observation (no session env var on
        // openclaw/pi, so the CLI can't self-detect). Fires on the actual
        // `evo autonomous on|off` / `evo exit-optimize-mode` command.
        const cmd = (toolInput as any)?.command
        if (typeof cmd === "string") {
          if (/^\s*evo\s+autonomous\s+off\s*$/.test(cmd) ||
              /^\s*evo\s+exit-optimize-mode\b/.test(cmd)) {
            unmarkAutonomous(ctx.runDir, ctx.sid)
          } else if (/^\s*evo\s+autonomous(\s+on)?\s*$/.test(cmd)) {
            markAutonomous(ctx.runDir, ctx.sid)
          }
        }
        if (shouldPolicyBlock(ctx.runDir, ctx.sid, toolName, toolInput)) {
          log(`deny ${toolName} in optimize_mode`)
          return { block: true, blockReason: POLICY_NUDGE_TEMPLATE }
        }
      } catch (err: any) {
        log(`before_tool_call error (fail-open): ${err?.message ?? err}`)
      }
      return undefined
    })

    // Stop nudge — keep /optimize running autonomously across turns.
    // idempotencyKey is per-firing so each finalize gets its own retry.
    api.on("before_agent_finalize", async (event: any) => {
      try {
        const ctx = ensureRegistered()
        if (!ctx) return undefined
        const instruction = maybeStopNudgeText(ctx.runDir, ctx.sid)
        if (instruction) {
          const turnId = event?.agentRunId ?? event?.runId ?? Date.now()
          log(`stop nudge revise sid=${ctx.sid} turn=${turnId}`)
          return {
            action: "revise",
            retry: {
              instruction,
              idempotencyKey: `evo-optimize-${ctx.sid}-${turnId}`,
            },
          }
        }
      } catch (err: any) {
        log(`before_agent_finalize error: ${err?.message ?? err}`)
      }
      return undefined
    })

    // Synchronous in openclaw (hooks.ts:42) — async handler returns a
    // Promise that the runtime discards, swallowing the mutation. Body
    // is pure sync filesystem so the `async` is unneeded.
    api.on("tool_result_persist", (event: any) => {
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
        // Short-circuit: stop at first successful append so we don't
        // shove the banner into every text-shaped field on the message.
        tryAppendString(msg.details, "text") ||
          tryAppendString(msg.details, "output") ||
          tryAppendString(msg.details, "stdout") ||
          tryAppendString(msg.details, "content")
      }
      if (!mutated) tryAppendString(msg, "text")

      return mutated ? msg : undefined
    })
  },
}

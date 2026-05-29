// Shared register-factory for hosts that use pi's ExtensionAPI shape
// (openclaw, pi). Each host's entry file calls makeRegister(host) to bind
// its own host string into the session record. Two reasons this matters:
//
//   1. The host string drives observability — `evo direct` enumeration,
//      dashboard listing, and `inject/sessions/<sid>.json` all reflect
//      what's recorded here. Tagging pi sessions as "openclaw" (the
//      pre-0.4.4 bug) made pi traffic invisible to per-host metrics.
//   2. Session ids are prefixed `<host>-<cwd_hash>`, so running pi and
//      openclaw concurrently against the same workspace produces two
//      distinct session records instead of colliding on one.
//
// Pi ExtensionAPI contract (verified against
// earendil-works/pi `packages/coding-agent/docs/extensions.md`):
//   - `session_start` — observer.
//   - `before_provider_request` — can replace payload; used to inject
//     directives + the policy banner as user messages.
//   - `tool_call` — fires pre-tool; return `{block: true, reason}` to
//     block. CAN mutate `event.input`. Used as the policy gate.
//   - `turn_end` — observer-only return-wise, but we can call
//     `pi.sendUserMessage(text, { deliverAs: "followUp" })` from inside
//     it to queue an autonomous-continuation message. That message
//     "Always triggers a turn" per the upstream docs, so the orchestrator
//     keeps driving the /optimize loop autonomously.
//
// (`ctx.sendUserMessage` is the deadlock-risk variant — only safe in
// command handlers. The top-level `pi.sendUserMessage` is safe to call
// from event handlers.)

import {
  POLICY_NUDGE_TEMPLATE,
  STOP_NUDGE_TEMPLATE,
  commitDrainPeek,
  drainSession,
  findEvoRunDir,
  formatDirectiveText,
  getSession,
  incrementAndShouldBlock,
  initOffsetToLatest,
  isDeniedInOptimizeMode,
  isAcked,
  isEvoCommand,
  isRegistered,
  markAutonomous,
  markEngaged,
  parseDirectiveIds,
  markOptimizeMode,
  maybeMarkOptimizeFromPrompt,
  maybeStopNudgeText,
  peekDrainSession,
  registerSession,
  unmarkAutonomous,
} from "../opencode_plugin/drain.js"
import * as crypto from "crypto"

interface PiExtensionAPI {
  on(event: string, handler: (event: any, ctx: any) => any): void
  sendUserMessage?: (
    content: string | Array<{ type: string; text?: string }>,
    options?: { deliverAs?: "steer" | "followUp" | "nextTurn"; triggerTurn?: boolean },
  ) => void
}

export function makeRegister(host: string): (api: PiExtensionAPI) => void {
  function deriveSessionId(): string {
    // Include EVO_EXP_ID in the seed so subagents get a DIFFERENT sid
    // from the parent (and from each other). Without this, a parent and
    // a subagent in the same cwd collapse onto one session record;
    // tagging exp_id on the shared record would then disable the
    // orchestrator's steering. Distinct sids let each agent carry its
    // own exp_id state and own queue offsets.
    const expId = process.env.EVO_EXP_ID || ""
    const seed = expId ? `${process.cwd()}|${expId}` : process.cwd()
    const hash = crypto.createHash("sha256").update(seed).digest("hex").slice(0, 12)
    return `${host}-${hash}`
  }

  return function register(api: PiExtensionAPI): void {
    // In-memory cache of directive text already drained from disk. We keep
    // appending these to every outbound LLM payload so that subagents
    // (which share sid via cwd hash) also see the directive on their own
    // first call.
    // Each drained directive tracked by its event ids so the replay below
    // can stop once the agent acks it (otherwise the same directive — and
    // its `evo ack` footer — is re-injected every turn for the whole
    // session, causing repeated re-acks + context bloat).
    const drainedItems: { ids: string[]; text: string }[] = []

    const ensureRegistered = (): { sid: string; runDir: string } | null => {
      const runDir = findEvoRunDir()
      if (!runDir) return null
      const sid = deriveSessionId()
      // Subagents get a different sid (EVO_EXP_ID in the hash seed),
      // so each agent has its own session record — no shared-record
      // pollution between parent and child. Pass exp_id at first
      // registration so the record carries the right tag from birth.
      if (!isRegistered(runDir, sid)) {
        const expId = process.env.EVO_EXP_ID || null
        registerSession(runDir, sid, host, expId)
      }
      return { sid, runDir }
    }

    const appendToPayload = (event: any, text: string): void => {
      if (Array.isArray(event.payload?.input)) {
        event.payload.input.push({
          role: "user",
          content: [{ type: "input_text", text }],
        })
      } else if (Array.isArray(event.payload?.messages)) {
        event.payload.messages.push({
          role: "user",
          content: [{ type: "text", text }],
        })
      }
    }

    api.on("session_start", () => {
      const ctx = ensureRegistered()
      if (!ctx) return
      // Engage the session immediately at startup. The original
      // scan-based engagement (in before_provider_request below) only
      // flipped the flag when the orchestrator's outbound LLM payload
      // contained an `evo …` tool call. For pi orchestrators that
      // dispatch all evo work to subagents (claude-sonnet-4-5 commonly
      // does this) the parent's own payload never contains `evo`, so it
      // stayed unengaged forever and `evo direct` fell through with
      // fanout=0 / skipped_unengaged=1. The engagement gate exists to
      // filter stale registered-but-inactive sessions, but for pi/
      // openclaw the host process IS the orchestrator — its existence
      // is the engagement signal.
      if (markEngaged(ctx.runDir, ctx.sid)) {
        initOffsetToLatest(ctx.runDir, ctx.sid)
      }
    })

    // Best-effort engagement detection: scan the outbound LLM payload's
    // recent messages for tool calls that ran `evo …`. Both OpenAI
    // (payload.input items with type:"function_call") and Anthropic
    // (payload.messages tool_use blocks) expose recent tool calls in
    // their payloads. We sniff the command string and flip engagement
    // if we see an evo invocation. Heuristic — agents could wrap the
    // call in a script — but matches the common case of `bash -c "evo …"`
    // or direct tool calls.
    const scanForEvoCommands = (payload: any): boolean => {
      try {
        // OpenAI Responses-style: payload.input entries with arguments
        const items = Array.isArray(payload?.input) ? payload.input : []
        for (const it of items) {
          const args = it?.arguments
          if (typeof args === "string" && isEvoCommand(args)) return true
          if (typeof args === "object" && args) {
            const cmd = args.command ?? args.cmd ?? args.shell
            if (typeof cmd === "string" && isEvoCommand(cmd)) return true
          }
        }
        // Anthropic Messages-style: payload.messages with tool_use blocks
        const msgs = Array.isArray(payload?.messages) ? payload.messages : []
        for (const m of msgs) {
          const content = Array.isArray(m?.content) ? m.content : []
          for (const c of content) {
            if (c?.type === "tool_use") {
              const cmd = c?.input?.command ?? c?.input?.cmd
              if (typeof cmd === "string" && isEvoCommand(cmd)) return true
            }
          }
        }
      } catch {
        // Defensive: never break the LLM call on a scan error
      }
      return false
    }

    // Extract the most recent user-message text from the outbound LLM
    // payload. Handles both OpenAI Responses-style (payload.input) and
    // Anthropic Messages-style (payload.messages), with `content` as
    // either a plain string or a parts array.
    const extractLatestUserText = (payload: any): string => {
      try {
        const items = Array.isArray(payload?.input) ? payload.input : []
        for (let i = items.length - 1; i >= 0; i--) {
          const it = items[i]
          if (it?.role !== "user") continue
          if (typeof it.content === "string" && it.content) return it.content
          if (Array.isArray(it.content)) {
            for (const c of it.content) {
              if (typeof c?.text === "string" && c.text) return c.text
            }
          }
        }
        const msgs = Array.isArray(payload?.messages) ? payload.messages : []
        for (let i = msgs.length - 1; i >= 0; i--) {
          const m = msgs[i]
          if (m?.role !== "user") continue
          if (typeof m.content === "string") return m.content
          if (Array.isArray(m.content)) {
            for (const c of m.content) {
              if (typeof c?.text === "string" && c.text) return c.text
            }
          }
        }
      } catch {
        // Defensive
      }
      return ""
    }

    api.on("before_provider_request", (event: any, _ctx: any) => {
      const ctx = ensureRegistered()
      if (!ctx) return

      // Auto-arm optimize_mode if the most recent user message looks like
      // `/optimize`. Mirrors the per-host pattern matchers.
      const promptText = extractLatestUserText(event.payload)
      maybeMarkOptimizeFromPrompt(ctx.runDir, ctx.sid, host, promptText)

      // Engagement is now flipped at session_start (see above) so we
      // don't need to scan-then-engage here. Scanning still useful for
      // future heuristics; for now, it's a no-op since the session is
      // already engaged.
      scanForEvoCommands(event.payload)

      // Drain any new on-disk events (advances offset → consumed_by++).
      const result = drainSession(ctx.runDir, ctx.sid)
      if (result.text) {
        drainedItems.push({ ids: parseDirectiveIds(result.text), text: result.text })
      }

      // Replay previously drained directives so subagents that share sid
      // also receive the content directly — BUT drop any directive the
      // agent has already acked, so a delivered+acknowledged directive
      // stops being re-injected (no more re-acks / context bloat). A
      // directive with no parseable id (legacy) is kept (can't track ack).
      for (let i = drainedItems.length - 1; i >= 0; i--) {
        const it = drainedItems[i]
        if (it.ids.length > 0 && it.ids.every((id) => isAcked(ctx.runDir, id))) {
          drainedItems.splice(i, 1)
        }
      }
      if (drainedItems.length === 0) return
      const combined = drainedItems.map((it) => it.text).join("\n")
      appendToPayload(event, combined)
      return event.payload
    })

    // Policy gate via the tool_call hook. Verified against
    // earendil-works/pi: returning `{block: true, reason}` blocks the
    // tool and feeds the reason back to the agent as the tool result.
    api.on("tool_call", (event: any, _ctx: any) => {
      const ctx = ensureRegistered()
      if (!ctx) return
      const sess = getSession(ctx.runDir, ctx.sid) as any
      if (!sess) return
      if (sess.exp_id) return // subagent — exempt
      const toolName = event?.toolName ?? event?.tool_name
      const toolInput = event?.input ?? {}
      // Autonomous arming: openclaw/pi have no session env var, so the
      // `evo autonomous on` CLI can't self-detect the session — observe
      // the command here and arm/disarm in-process (not prose; fires only
      // on the actual command). Runs regardless of optimize_mode state.
      const cmd = (toolInput as any)?.command
      if (typeof cmd === "string") {
        if (/^\s*evo\s+autonomous\s+off\s*$/.test(cmd) ||
            /^\s*evo\s+exit-optimize-mode\b/.test(cmd)) {
          unmarkAutonomous(ctx.runDir, ctx.sid)
        } else if (/^\s*evo\s+autonomous(\s+on)?\s*$/.test(cmd)) {
          markAutonomous(ctx.runDir, ctx.sid)
        }
      }
      if (!sess.optimize_mode) return
      if (!isDeniedInOptimizeMode(toolName, toolInput)) return
      if (incrementAndShouldBlock(ctx.runDir, ctx.sid, toolName)) {
        return { block: true, reason: POLICY_NUDGE_TEMPLATE }
      }
      // Even-numbered violation under alternating cadence — pass.
    })

    // Always-fire stop nudge via turn_end. Uses pi's top-level
    // `pi.sendUserMessage(text, { deliverAs: "followUp" })`. Per upstream
    // docs the followUp message waits for the agent to finish current
    // tool execution, then delivers AND triggers another turn — the
    // exact behavior we want for /optimize loop continuation.
    api.on("turn_end", async (_event: any, _ctx: any) => {
      if (typeof api.sendUserMessage !== "function") return // older pi
      const ctx = ensureRegistered()
      if (!ctx) return
      const sess = getSession(ctx.runDir, ctx.sid) as any
      if (!sess) return
      if (sess.exp_id) return
      if (!sess.optimize_mode) return
      if (!sess.autonomous) return  // opt-in only; default stops naturally

      // Peek queued directives (don't pop) and combine with the nudge.
      // If sendUserMessage throws, directives stay queued.
      const peek = peekDrainSession(ctx.runDir, ctx.sid)

      const text = peek.text
        ? peek.text + "\n\n" + STOP_NUDGE_TEMPLATE
        : STOP_NUDGE_TEMPLATE
      try {
        api.sendUserMessage(text, { deliverAs: "followUp" })
        commitDrainPeek(ctx.runDir, ctx.sid, peek)
      } catch (_e) {
        // Best-effort — pi may reject if the session is shutting down.
      }
    })
  }
}

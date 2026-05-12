// Openclaw pi-coding-agent extension — drains evo directives and
// appends them to the outbound LLM payload via `before_provider_request`.
//
// Why `before_provider_request` and not `context`:
//   `context` lets handlers inspect/filter AgentState.messages, but the
//   return value does NOT propagate into the actual LLM payload (the
//   payload is built independently from AgentState; verified by
//   monkey-patching global.fetch and inspecting the outbound bytes).
//   `before_provider_request` fires right before the HTTP request is
//   sent; returning a modified payload here replaces what gets sent.
//
// Why APPEND, not PREPEND:
//   The LLM honors the LATEST user message as the active instruction;
//   earlier messages in `input` are reference context. Prepending the
//   directive at the start of the conversation history makes the LLM
//   treat it as superseded by later user prompts. Appending puts it
//   at the end so it's read as the most-recent user turn, which the
//   model acts on (verified by directive-marker test).
//
// Provider-format detection: OpenAI Responses API uses `payload.input`
// (array of typed items, user messages have content: [{type:"input_text"}]).
// Anthropic uses `payload.messages` (array of {role, content}). Sniff
// and append a properly-formed user message.
//
// Session id derivation: pi's ExtensionAPI doesn't expose the session
// id from event handlers; use a stable hash of cwd.
//
// Re-detect runDir per call: some host agents (e.g. openclaw's
// pi-coding-agent + the optimize skill) re-run `evo init` mid-session,
// creating run_0001 and switching the active marker. Caching runDir
// would keep registering in run_0000 while `evo direct` looks in
// run_0001 (fanout=0).
//
// Why we replay drained directives on every LLM call:
//   pi-coding-agent runs subagents (Task tool) in the same process as
//   the parent. They share `process.cwd()`, so they hash to the same
//   session id. Once the parent's first LLM call drains the queue, the
//   on-disk offset advances and `drainSession` returns null for
//   subsequent calls — including the subagent's. The parent then has
//   to perfectly relay the directive's content (including any marker
//   tags) into the subagent's brief, and frontier models don't always
//   do that verbatim. Caching the drained text in memory and
//   re-appending it to every LLM payload gives the subagent direct
//   access to the directive on its own first call, so it can act on
//   the content directly rather than relying on parent relay.
//
// See notes/cross-host-inject-design.md.

import {
  drainSession,
  findEvoRunDir,
  isRegistered,
  registerSession,
} from "../opencode_plugin/drain.js"
import * as crypto from "crypto"

interface PiExtensionAPI {
  on(event: string, handler: (event: any, ctx: any) => any): void
}

function deriveSessionId(): string {
  const hash = crypto.createHash("sha256").update(process.cwd()).digest("hex").slice(0, 12)
  return `openclaw-${hash}`
}

export default function register(api: PiExtensionAPI): void {
  // In-memory cache of directive text already drained from disk. We keep
  // appending these to every outbound LLM payload so that subagents
  // (which share sid via cwd hash) also see the directive on their own
  // first call.
  const drainedTexts: string[] = []

  const ensureRegistered = (): { sid: string; runDir: string } | null => {
    const runDir = findEvoRunDir()
    if (!runDir) return null
    const sid = deriveSessionId()
    if (!isRegistered(runDir, sid)) {
      registerSession(runDir, sid, "openclaw")
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
    ensureRegistered()
  })

  api.on("before_provider_request", (event: any, _ctx: any) => {
    const ctx = ensureRegistered()
    if (!ctx) return

    // Drain any new on-disk events (advances offset → consumed_by++).
    const result = drainSession(ctx.runDir, ctx.sid)
    if (result.text) drainedTexts.push(result.text)

    // Replay every previously drained directive on every call so
    // subagents that share sid also receive the content directly.
    if (drainedTexts.length === 0) return
    const combined = drainedTexts.join("\n")
    appendToPayload(event, combined)
    return event.payload
  })
}

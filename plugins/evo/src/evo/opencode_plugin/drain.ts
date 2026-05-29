// TS port of evo.inject.drain — used by in-process plugins on opencode and
// openclaw/pi (which wrap the same bundle). Mirrors the Python `drain.py` +
// `queue.py` + `marker.py` logic. Schema parity is enforced by
// `tests/inject_fixtures/*.json` consumed by both implementations.
//
// Opencode hook contract (verified against sst/opencode
// `packages/plugin/src/index.ts` Hooks type):
//   - `chat.message(input, {message, parts})` — fires on new user message;
//     mutating `parts` injects text into LLM input.
//   - `tool.execute.before(input, {args})` — fires pre-tool; THROW to deny
//     (the error message becomes "Blocked by policy" feedback).
//   - `event({event})` — fires for system events; `event.type === "session.idle"`
//     signals the agent finished its turn (the analogue of claude-code Stop).
//   - There is NO `stop` hook on opencode (verified absent from Hooks type).
//     The always-fire stop nudge uses `event(session.idle)` + the
//     `client.session.prompt({noReply: false, parts: [...]})` recipe to
//     inject a follow-up user message that re-engages the loop.
//
// See notes/cross-host-inject-design.md.

import * as fs from "fs"
import * as path from "path"

const QUEUE_SCHEMA_VERSION = 1

export interface QueueEvent {
  schema_version: number
  id: string
  ts: string
  text: string
}

export interface SessionRecord {
  schema_version: number
  session_id: string
  host: string
  pid: number
  registered_at: string
  last_seen_at: string
  exp_id: string | null
  parent_session_id: string | null
  // v0.4.4: engagement flag set when the agent first runs an `evo`
  // command. The Python `auto_register_from_env` handles this for hosts
  // that export a session-id env var; in-process JS plugins (opencode,
  // openclaw, pi) detect `evo` shell commands via tool hooks and call
  // markEngaged() themselves.
  has_evo_engaged?: boolean
  engaged_at?: string | null
  // v0.4.5: optimize_mode tags the orchestrator driving /optimize (policy
  // gate). autonomous (opt-in) gates the always-fire stop nudge — default
  // off, so a plain /optimize stops naturally; armed via the agent running
  // `evo autonomous on`, which these in-process plugins observe as a tool
  // call (the CLI can't self-detect a session here).
  optimize_mode?: boolean
  optimize_mode_at?: string | null
  autonomous?: boolean
  autonomous_at?: string | null
  subagents_only?: boolean
  subagents_only_at?: string | null
}

export interface DrainResult {
  /** Text to inject (`[evo direct] ...` lines joined by newline), or null if nothing to deliver. */
  text: string | null
  /** New workspace offset to record (or null if no workspace events drained). */
  newWorkspaceOffset: string | null
  /** New exp offset to record (or null if not a subagent or no exp events drained). */
  newExpOffset: string | null
}

// ──────────────────────────────────────────────────────────────────────────
// Path helpers — mirror evo/inject/paths.py
// ──────────────────────────────────────────────────────────────────────────

function injectRoot(runDir: string): string {
  return path.join(runDir, "inject")
}
function sessionFile(runDir: string, sid: string): string {
  return path.join(injectRoot(runDir), "sessions", `${sid}.json`)
}
function workspaceEventsPath(runDir: string): string {
  return path.join(injectRoot(runDir), "events", "workspace.jsonl")
}
function expEventsPath(runDir: string, expId: string): string {
  return path.join(injectRoot(runDir), "events", `${expId}.jsonl`)
}
function offsetFile(runDir: string, sid: string): string {
  return path.join(injectRoot(runDir), "offsets", `${sid}.json`)
}
function markerFile(runDir: string, sid: string): string {
  return path.join(injectRoot(runDir), "markers", `${sid}.flag`)
}
function ackFile(runDir: string, eventId: string): string {
  return path.join(injectRoot(runDir), "acks", `${eventId}.json`)
}

/** True if the agent has acked this directive (inject/acks/<id>.json exists,
 * written by `evo ack`). Used to STOP re-appending a directive into the
 * model's context once acknowledged — otherwise the openclaw/pi/native
 * "replay drained directives every turn" cache re-injects (and the agent
 * re-acks) the same directive for the whole session. */
export function isAcked(runDir: string, eventId: string): boolean {
  try {
    return fs.existsSync(ackFile(runDir, eventId))
  } catch {
    return false
  }
}

/** Extract the directive event ids from a drained banner block
 * (`[EVO DIRECTIVE id=<id>]`). A single drain may concatenate several. */
export function parseDirectiveIds(text: string): string[] {
  const ids: string[] = []
  const re = /\[EVO DIRECTIVE id=([^\]]+)\]/g
  let m: RegExpExecArray | null
  while ((m = re.exec(text)) !== null) ids.push(m[1])
  return ids
}

// ──────────────────────────────────────────────────────────────────────────
// File primitives
// ──────────────────────────────────────────────────────────────────────────

function readJsonOrNull<T>(p: string): T | null {
  try {
    return JSON.parse(fs.readFileSync(p, "utf8"))
  } catch {
    return null
  }
}

function atomicWriteJson(p: string, data: unknown): void {
  fs.mkdirSync(path.dirname(p), { recursive: true })
  const tmp = `${p}.tmp.${process.pid}`
  fs.writeFileSync(tmp, JSON.stringify(data))
  fs.renameSync(tmp, p)
}

function unlinkIfExists(p: string): void {
  try {
    fs.unlinkSync(p)
  } catch {
    // ignore — file may not exist
  }
}

// ──────────────────────────────────────────────────────────────────────────
// Queue read — mirror evo/inject/queue.py read_events_after
// ──────────────────────────────────────────────────────────────────────────

export function readEventsAfter(queuePath: string, afterId: string | null): QueueEvent[] {
  if (!fs.existsSync(queuePath)) return []
  let text: string
  try {
    text = fs.readFileSync(queuePath, "utf8")
  } catch {
    return []
  }
  const out: QueueEvent[] = []
  for (const line of text.split("\n")) {
    const trimmed = line.trim()
    if (!trimmed) continue
    let rec: any
    try {
      rec = JSON.parse(trimmed)
    } catch {
      // Tolerate trailing partial line (writer was mid-append)
      continue
    }
    const recId = rec?.id
    if (typeof recId !== "string") continue
    if (afterId === null || recId > afterId) {
      out.push(rec as QueueEvent)
    }
  }
  return out
}

export function readOffset(runDir: string, sid: string, queue: "workspace" | "exp"): string | null {
  const data = readJsonOrNull<Record<string, any>>(offsetFile(runDir, sid))
  if (!data) return null
  if (queue === "workspace") return data.last_workspace_event_id ?? null
  if (queue === "exp") return data.last_exp_event_id ?? null
  return null
}

function nowIso(): string {
  // Match Python isoformat(timespec="seconds") with UTC suffix.
  // Python emits "+00:00"; we normalize to that for parity.
  return new Date().toISOString().replace(/\.\d{3}Z$/, "+00:00")
}

export function writeOffset(
  runDir: string,
  sid: string,
  opts: { workspaceId?: string | null; expId?: string | null },
): void {
  const p = offsetFile(runDir, sid)
  let data: Record<string, any> = readJsonOrNull(p) ?? {}
  data.schema_version = QUEUE_SCHEMA_VERSION
  data.session_id = sid
  if (opts.workspaceId !== undefined && opts.workspaceId !== null) {
    data.last_workspace_event_id = opts.workspaceId
  }
  if (opts.expId !== undefined && opts.expId !== null) {
    data.last_exp_event_id = opts.expId
  }
  data.updated_at = nowIso()
  atomicWriteJson(p, data)
}

// ──────────────────────────────────────────────────────────────────────────
// Format directive text — mirror evo/inject/drain.py format_directive_text
// ──────────────────────────────────────────────────────────────────────────

export function formatDirectiveText(events: QueueEvent[]): string {
  // Mirrors evo.inject.drain.format_directive_text — embeds the event id
  // and an `evo ack <id>` instruction so the agent can acknowledge the
  // directive via the L2 ACK channel.
  const lines: string[] = []
  for (const ev of events) {
    if (!ev.text) continue
    const id = (ev as any).id || ""
    if (id) {
      lines.push(`[EVO DIRECTIVE id=${id}]`)
      lines.push(ev.text)
      lines.push(`[END EVO DIRECTIVE — run \`evo ack ${id}\` to confirm you have received this message, then proceed]`)
    } else {
      lines.push("[EVO DIRECTIVE]")
      lines.push(ev.text)
      lines.push("[END EVO DIRECTIVE]")
    }
  }
  return lines.join("\n")
}

// ──────────────────────────────────────────────────────────────────────────
// Session registry helpers
// ──────────────────────────────────────────────────────────────────────────

export function getSession(runDir: string, sid: string): SessionRecord | null {
  return readJsonOrNull<SessionRecord>(sessionFile(runDir, sid))
}

export function isRegistered(runDir: string, sid: string): boolean {
  return fs.existsSync(sessionFile(runDir, sid))
}

const REGISTRY_SCHEMA_VERSION = 1

export function registerSession(
  runDir: string,
  sid: string,
  host: string,
  expId: string | null = null,
): void {
  const p = sessionFile(runDir, sid)
  const now = nowIso()
  const existing = readJsonOrNull<SessionRecord>(p)
  if (existing) {
    existing.last_seen_at = now
    // Merge: pick up exp_id if existing record lacked one. Mirrors the
    // Python register_session behavior for the same field.
    if (expId && !existing.exp_id) existing.exp_id = expId
    if (existing.has_evo_engaged === undefined) existing.has_evo_engaged = false
    if (existing.engaged_at === undefined) existing.engaged_at = null
    atomicWriteJson(p, existing)
    return
  }
  const rec: SessionRecord = {
    schema_version: REGISTRY_SCHEMA_VERSION,
    session_id: sid,
    host,
    pid: process.pid,
    registered_at: now,
    last_seen_at: now,
    exp_id: expId,
    parent_session_id: null,
    has_evo_engaged: false,
    engaged_at: null,
  }
  atomicWriteJson(p, rec)
  // Seed offset to the queue tail at registration time so this session
  // only sees events queued AFTER it registered — matches the v0.4.4
  // safety contract in Python register_session.
  initOffsetToLatest(runDir, sid)
}


/** Flip has_evo_engaged on the session record if currently false.
 *  Returns true on the transition; caller should also seed offset.
 *  Idempotent — no-op on subsequent calls. Mirrors Python mark_engaged. */
export function markEngaged(runDir: string, sid: string): boolean {
  const p = sessionFile(runDir, sid)
  const rec = readJsonOrNull<SessionRecord>(p)
  if (!rec) return false
  if (rec.has_evo_engaged) return false
  rec.has_evo_engaged = true
  rec.engaged_at = nowIso()
  atomicWriteJson(p, rec)
  return true
}


/** Seed the workspace offset to the current queue tail. Called at
 *  registration time + on engagement transition to prevent backfill
 *  of pre-existing events. Mirrors Python init_offset_to_latest. */
export function initOffsetToLatest(runDir: string, sid: string): void {
  const wsPath = workspaceEventsPath(runDir)
  let latest: string | null = null
  if (fs.existsSync(wsPath)) {
    const events = readEventsAfter(wsPath, null)
    if (events.length > 0) latest = events[events.length - 1].id
  }
  writeOffset(runDir, sid, { workspaceId: latest })
}


/** Detect whether a shell command starts with `evo ` (or is just `evo`).
 *  Used by in-process plugins to flip engagement when the agent shells
 *  to the evo CLI. */
const EVO_CMD_RE = /^\s*evo(\s|$)/

export function isEvoCommand(command: string | undefined | null): boolean {
  if (!command || typeof command !== "string") return false
  return EVO_CMD_RE.test(command)
}

// ──────────────────────────────────────────────────────────────────────────
// Workspace root resolution — mirror evo/core.py repo_root() walking up to .evo/
// ──────────────────────────────────────────────────────────────────────────

export function findEvoRunDir(cwd?: string): string | null {
  // Prefer EVO_RUN_DIR env var.
  const envRunDir = process.env.EVO_RUN_DIR
  if (envRunDir) return envRunDir

  let dir = cwd || process.cwd()
  while (dir !== "/" && dir !== "") {
    const evoDir = path.join(dir, ".evo")
    if (fs.existsSync(evoDir)) {
      // Pick newest run_* lexicographically (run_NNNN sorts correctly)
      try {
        const runs = fs
          .readdirSync(evoDir)
          .filter((n) => n.startsWith("run_"))
          .sort()
        if (runs.length === 0) return null
        return path.join(evoDir, runs[runs.length - 1])
      } catch {
        return null
      }
    }
    const parent = path.dirname(dir)
    if (parent === dir) break
    dir = parent
  }
  return null
}

// ──────────────────────────────────────────────────────────────────────────
// Drain entry point — mirror evo.inject.drain.drain_session
// ──────────────────────────────────────────────────────────────────────────

/**
 * Peek-only variant: read pending events without advancing the offset or
 * unlinking the marker. Caller commits via `commitDrainPeek(...)` after
 * the injection succeeds. Used by hooks where the injection itself can
 * fail (e.g. `client.session.prompt` on a terminating session) and we
 * don't want to silently consume queued directives.
 */
export function peekDrainSession(runDir: string, sessionId: string): DrainResult {
  const sess = getSession(runDir, sessionId)
  if (!sess) {
    return { text: null, newWorkspaceOffset: null, newExpOffset: null }
  }
  const expId = sess.exp_id
  let events: QueueEvent[] = []
  let newWorkspaceOffset: string | null = null
  let newExpOffset: string | null = null

  if (expId) {
    const lastId = readOffset(runDir, sessionId, "exp")
    const newEvents = readEventsAfter(expEventsPath(runDir, expId), lastId)
    events = newEvents
    if (newEvents.length > 0) newExpOffset = newEvents[newEvents.length - 1].id
  } else {
    const lastId = readOffset(runDir, sessionId, "workspace")
    const newEvents = readEventsAfter(workspaceEventsPath(runDir), lastId)
    events = newEvents
    if (newEvents.length > 0) newWorkspaceOffset = newEvents[newEvents.length - 1].id
  }

  const text = events.length > 0 ? formatDirectiveText(events) : null
  return { text, newWorkspaceOffset, newExpOffset }
}

/**
 * Commit phase for a successful peek-then-inject. Advances offsets and
 * unlinks the marker. Safe to call with all-null DrainResult (no-op).
 */
export function commitDrainPeek(
  runDir: string,
  sessionId: string,
  peek: DrainResult,
): void {
  if (peek.newWorkspaceOffset || peek.newExpOffset) {
    writeOffset(runDir, sessionId, {
      workspaceId: peek.newWorkspaceOffset,
      expId: peek.newExpOffset,
    })
  }
  unlinkIfExists(markerFile(runDir, sessionId))
}

/**
 * Read pending events for `sessionId`, format text, update offset, unlink marker.
 * Returns the formatted text + offset deltas. Caller decides how to inject the
 * text into the host's hook contract (e.g. opencode `chat.params.system`).
 *
 * Side effects: writes new offset, unlinks marker. Caller does NOT need to
 * touch those files.
 */
export function drainSession(runDir: string, sessionId: string): DrainResult {
  const sess = getSession(runDir, sessionId)
  if (!sess) {
    unlinkIfExists(markerFile(runDir, sessionId))
    return { text: null, newWorkspaceOffset: null, newExpOffset: null }
  }

  const expId = sess.exp_id
  let events: QueueEvent[] = []
  let newWorkspaceOffset: string | null = null
  let newExpOffset: string | null = null

  if (expId) {
    const lastId = readOffset(runDir, sessionId, "exp")
    const newEvents = readEventsAfter(expEventsPath(runDir, expId), lastId)
    events = newEvents
    if (newEvents.length > 0) newExpOffset = newEvents[newEvents.length - 1].id
  } else {
    const lastId = readOffset(runDir, sessionId, "workspace")
    const newEvents = readEventsAfter(workspaceEventsPath(runDir), lastId)
    events = newEvents
    if (newEvents.length > 0) newWorkspaceOffset = newEvents[newEvents.length - 1].id
  }

  const text = events.length > 0 ? formatDirectiveText(events) : null
  if (newWorkspaceOffset || newExpOffset) {
    writeOffset(runDir, sessionId, {
      workspaceId: newWorkspaceOffset,
      expId: newExpOffset,
    })
  }
  unlinkIfExists(markerFile(runDir, sessionId))
  return { text, newWorkspaceOffset, newExpOffset }
}


// ──────────────────────────────────────────────────────────────────────────
// Optimize-mode policy gate + stop nudge — mirrors evo.inject.drain
// ──────────────────────────────────────────────────────────────────────────

/** Banner text emitted when the orchestrator strays from /optimize protocol.
 *  Kept in sync with `_POLICY_NUDGE_TEMPLATE` in evo/inject/drain.py. */
export const POLICY_NUDGE_TEMPLATE =
  "[EVO POLICY]\n" +
  "Preventative block. You may have strayed from /evo:optimize protocol.\n" +
  "\n" +
  "When /optimize is active, the orchestrator should NOT edit files directly\n" +
  "or run experiment commands by hand. The protocol is: write briefs, spawn\n" +
  "subagents, let them do the edits and runs, then read the results.\n" +
  "\n" +
  "If you need to wait for subagents to finish, use `evo wait` — it blocks\n" +
  "until any experiment concludes, max 1h.\n" +
  "\n" +
  "Manual edits and shell commands that mutate files are discouraged unless\n" +
  "evo doesn't already expose the operation. If this block was actually\n" +
  "warranted (rare), run `evo exit-optimize-mode` to disable the safety\n" +
  "nudges, then retry the tool.\n" +
  "[END EVO POLICY]\n"

/** Continuation prompt emitted on session.idle while in optimize_mode.
 *  Kept in sync with `_STOP_NUDGE_TEMPLATE` in evo/inject/drain.py. */
export const STOP_NUDGE_TEMPLATE =
  "[EVO LOOP]\n" +
  "You are driving /evo:optimize autonomously. Don't stop, and don't ask\n" +
  "the user for direction — they explicitly invoked /optimize for hands-off\n" +
  "operation. They'll intervene via `evo direct` if they need to.\n" +
  "\n" +
  "If subagents are still running: run `evo wait` to block until the next\n" +
  "experiment concludes (or up to 1h). Then resume planning.\n" +
  "\n" +
  "If subagents are done and you have unread results: read `evo scratchpad`,\n" +
  "update annotations as needed, and plan + spawn the next round.\n" +
  "\n" +
  "Stop only if `evo status` shows the budget exhausted or you've hit the\n" +
  "stall limit. If so, print a final summary first. To suppress this\n" +
  "continuation loop for a legitimate one-off task, run\n" +
  "`evo exit-optimize-mode`.\n" +
  "[END EVO LOOP]\n"


/** File-mutation tool names across hosts. Exact-name match (lowercased).
 *  Kept in sync with `_DENY_TOOL_NAMES` in evo/inject/drain.py. */
export const DENY_TOOL_NAMES: Set<string> = new Set([
  // claude-code / codex
  "edit",
  "write",
  "notebookedit",
  "notebook_edit",
  "multiedit",
  "multi_edit",
  // cursor
  "edit_file",
  "create_file",
  "search_replace",
  "str_replace",
  "applypatch",
  "apply_patch",
  "delete_file",
  // opencode / openclaw / pi / hermes variants
  "file_write",
  "file_edit",
  // hermes: registers `patch` as its primary file-edit tool
  "patch",
])

/** Shell-execution tool names. Only when the tool is in this set does the
 *  bash-pattern scan run. Mirrors `_BASH_TOOL_NAMES`. */
export const BASH_TOOL_NAMES: Set<string> = new Set([
  "bash",
  "shell",
  "exec",
  "run_terminal_cmd",
  "runterminalcmd",
  "run_command",
  "terminal",
  "execute_code",
  "execute",
])


// Per-segment hard-deny patterns. Anchored to segment start (after optional
// `nohup` / `/path/to/`). Sed/perl/awk in-place use non-greedy preamble so
// `sed -E -i` and `sed -e '...' -i` both match. Git pattern allows global
// options before the mutating subcommand. Mirrors `_SEGMENT_DENY_RE`.
const SEGMENT_DENY_RE =
  /^\s*(?:nohup\s+)?(?:\S*\/)?(?:tee\b(?:\s+-[aiu]+)*\s+[^\s|&<>]+|sed\b[^|&;]*?\s-[a-zA-Z]*i[a-zA-Z]*\b|sed\b[^|&;]*?\s--in-place\b|perl\b[^|&;]*?\s-[a-zA-Z]*i[a-zA-Z]*\b|awk\b[^|&;]*?\s-i\s+inplace\b|(?:mv|cp|rm|mkdir|rmdir|touch|chmod|chown|chgrp|ln|rsync)(?:\s|$)|dd\b[^|&;]*?\bof=|curl\b[^|&;]*?\s-[a-zA-Z]*[oO][a-zA-Z=]*(?:\s|$)|curl\b[^|&;]*?\s--output(?:=|\s)|curl\b[^|&;]*?\s--remote-name\b|wget(?:\s|$)|patch(?:\s|$)|install(?:\s|$)|truncate(?:\s|$)|git\b(?:\s+(?:-[a-zA-Z]\S*|--[a-z][a-z-]*(?:=\S+)?)(?:\s+\S+)?)*?\s+(?:apply|checkout|restore|reset|clean|switch|merge|rebase|am|stash(?!\s+(?:list|show)\b)|cherry-pick|pull|clone|revert|worktree)\b|(?:vim|vi|nano|emacs)(?:\s|$))/

// File-redirect patterns. Mirrors `_REDIRECT_DENY_RE`. Subagent-spawn idioms
// (`> /tmp/log 2>&1`) are exempted via the host-spawn prefix check below.
const REDIRECT_DENY_RE =
  /(?:(?<![<\d&])>>?\s*[^\s|&<>;]+|\b\d+>>?\s*(?!&)[^\s|&<>;]+|&>>?\s*(?!&)[^\s|&<>;]+|>\|\s*[^\s|&<>;]+)/

// Host-spawn prefix — exempts redirects (subagent logging). evo prefix is
// intentionally NOT here (orchestrator dumping state to a file is a stray).
const HOST_SPAWN_PREFIX_RE =
  /^\s*(?:nohup\s+)?(?:claude(?:\s|$)|codex(?:\s|$)|cursor-agent(?:\s|$)|opencode(?:\s|$)|hermes(?:\s|$)|openclaw(?:\s|$)|pi(?:\s|$)|pi-coding-agent(?:\s|$))/

// Unquoted shell separators — split segments. Bare `&` excluded if it's
// part of `&&`, `>&`, `&&` rhs, or trailing background marker.
const UNQUOTED_SEPARATOR_RE = /[;\n]|&&|\|\||\|(?!\|)|(?<![>&])&(?![&>])(?!\s*$)/


/** Split a sanitized shell command on unquoted separators into segments. */
function splitSegments(cmd: string): string[] {
  return cmd.split(UNQUOTED_SEPARATOR_RE)
}


/** Walk `seg` honoring shell quote state and yield substitution bodies:
 *  `$(...)`, backticks, `<(...)`, `>(...)`. Skips `$((...))` arithmetic
 *  (math expressions, not shell commands). Single-quoted regions are
 *  inert (skipped). Mirrors `_extract_substitution_bodies`. */
function extractSubstitutionBodies(seg: string): string[] {
  const bodies: string[] = []
  let i = 0
  const n = seg.length
  let state: "default" | "sq" | "dq" = "default"

  const findBalancedParenClose = (start: number): number => {
    let depth = 1
    let k = start
    let inner: "default" | "sq" | "dq" = "default"
    while (k < n && depth > 0) {
      const cc = seg[k]
      if (inner === "sq") {
        if (cc === "'") inner = "default"
        k++
        continue
      }
      if (inner === "dq") {
        if (cc === "\\" && k + 1 < n) {
          k += 2
          continue
        }
        if (cc === '"') {
          inner = "default"
          k++
          continue
        }
      }
      if (cc === "\\" && k + 1 < n) {
        k += 2
        continue
      }
      if (cc === "'" && inner === "default") {
        inner = "sq"
      } else if (cc === '"' && inner === "default") {
        inner = "dq"
      } else if (cc === "(") {
        depth++
      } else if (cc === ")") {
        depth--
      }
      k++
    }
    return depth === 0 ? k : -1
  }

  while (i < n) {
    const c = seg[i]
    if (state === "sq") {
      if (c === "'") state = "default"
      i++
      continue
    }
    if (state === "dq") {
      if (c === "\\" && i + 1 < n) {
        i += 2
        continue
      }
      if (c === '"') {
        state = "default"
        i++
        continue
      }
    }
    if (c === "\\" && i + 1 < n) {
      i += 2
      continue
    }
    if (c === "'" && state === "default") {
      state = "sq"
      i++
      continue
    }
    if (c === '"' && state === "default") {
      state = "dq"
      i++
      continue
    }
    // $(...) command substitution; skip $((...)) arithmetic.
    if (c === "$" && i + 1 < n && seg[i + 1] === "(") {
      if (i + 2 < n && seg[i + 2] === "(") {
        i += 3
        continue
      }
      const end = findBalancedParenClose(i + 2)
      if (end !== -1) {
        bodies.push(seg.slice(i + 2, end - 1))
        i = end
        continue
      }
    }
    // <(...) / >(...) process substitution; only at default state.
    if ((c === "<" || c === ">") && i + 1 < n && seg[i + 1] === "(" && state === "default") {
      const end = findBalancedParenClose(i + 2)
      if (end !== -1) {
        bodies.push(seg.slice(i + 2, end - 1))
        i = end
        continue
      }
    }
    // Backtick (no nesting).
    if (c === "`" && state !== "sq") {
      let j = i + 1
      while (j < n && seg[j] !== "`") {
        if (seg[j] === "\\" && j + 1 < n) {
          j += 2
          continue
        }
        j++
      }
      if (j < n) {
        bodies.push(seg.slice(i + 1, j))
        i = j + 1
        continue
      }
    }
    i++
  }
  return bodies
}


/** Strip inert single/double quoted regions and erase `$((…))` arithmetic.
 *  Mirrors `_strip_inert_quoted`. */
function stripInertQuoted(cmd: string): string {
  // Strip single-quoted regions (always literal).
  let out = cmd.replace(/'[^']*'/g, "''")
  // Strip double-quoted regions IF they contain no $(…) or backticks.
  out = out.replace(/"(?:[^"\\]|\\.)*"/g, (match) => {
    if (match.indexOf("$(") >= 0 || match.indexOf("`") >= 0) return match
    return '""'
  })
  // Erase $((…)) arithmetic with balanced-paren walking.
  const buf: string[] = []
  let i = 0
  const n = out.length
  while (i < n) {
    if (out[i] === "$" && i + 2 < n && out[i + 1] === "(" && out[i + 2] === "(") {
      let depth = 2
      let j = i + 3
      while (j < n && depth > 0) {
        if (out[j] === "(") depth++
        else if (out[j] === ")") depth--
        j++
      }
      if (depth === 0) {
        i = j
        continue
      }
    }
    buf.push(out[i])
    i++
  }
  return buf.join("")
}


/** Unwrap `bash -c '…'` / `sh -c "…"` etc. so the inner script body is
 *  scanned. Approximates Python `shlex.split` for the common forms used in
 *  practice. Mirrors `_unwrap_shell_c_arguments`. */
const SHELL_INTERPRETERS = new Set(["bash", "sh", "zsh", "dash", "ash"])

function tokenize(cmd: string): string[] | null {
  // Minimal shell tokenizer: respect single/double quotes, escape sequences.
  // Returns null on unbalanced quotes (caller falls back to scanning cmd
  // as-is).
  const out: string[] = []
  let buf = ""
  let state: "default" | "sq" | "dq" = "default"
  let inToken = false
  for (let i = 0; i < cmd.length; i++) {
    const c = cmd[i]
    if (state === "sq") {
      if (c === "'") {
        state = "default"
        continue
      }
      buf += c
      inToken = true
      continue
    }
    if (state === "dq") {
      if (c === "\\" && i + 1 < cmd.length) {
        buf += cmd[++i]
        continue
      }
      if (c === '"') {
        state = "default"
        continue
      }
      buf += c
      inToken = true
      continue
    }
    if (c === "'") {
      state = "sq"
      inToken = true
      continue
    }
    if (c === '"') {
      state = "dq"
      inToken = true
      continue
    }
    if (c === "\\" && i + 1 < cmd.length) {
      buf += cmd[++i]
      inToken = true
      continue
    }
    if (/\s/.test(c)) {
      if (inToken) {
        out.push(buf)
        buf = ""
        inToken = false
      }
      continue
    }
    buf += c
    inToken = true
  }
  if (state !== "default") return null
  if (inToken) out.push(buf)
  return out
}

function unwrapShellCArguments(cmd: string): string {
  const tokens = tokenize(cmd)
  if (!tokens || tokens.length === 0) return cmd
  const appended: string[] = []
  for (let i = 0; i < tokens.length; i++) {
    const tok = tokens[i]
    const name = tok.replace(/\/+$/, "").split("/").pop() || ""
    if (!SHELL_INTERPRETERS.has(name)) continue
    let j = i + 1
    while (j < tokens.length) {
      const t = tokens[j]
      if (t === "-c") {
        if (j + 1 < tokens.length) appended.push(tokens[j + 1])
        break
      }
      // Combined short-opt block containing `c` (e.g. `-ic`, `-lc`, `-ce`).
      if (t.startsWith("-") && !t.startsWith("--") && t.length > 1 && t.slice(1).indexOf("c") >= 0) {
        if (j + 1 < tokens.length) appended.push(tokens[j + 1])
        break
      }
      j++
    }
  }
  if (appended.length === 0) return cmd
  return cmd + " ; " + appended.join(" ; ")
}


/** Return true if this tool call is on the optimize-mode deny list. Mirrors
 *  `_is_denied_in_optimize_mode`. */
export function isDeniedInOptimizeMode(
  toolName: string | null | undefined,
  toolInput: unknown,
): boolean {
  if (!toolName) return false
  const t = toolName.toLowerCase()
  if (DENY_TOOL_NAMES.has(t)) return true
  if (!BASH_TOOL_NAMES.has(t)) return false
  const input = (toolInput || {}) as Record<string, unknown>
  const cmd = typeof input.command === "string" ? (input.command as string) : ""
  if (!cmd) return false

  const prepared = unwrapShellCArguments(cmd)

  // Recurse into substitution bodies on raw prepared command (before
  // inert-strip would nuke `sh -c '...'` content inside substitution).
  for (const body of extractSubstitutionBodies(prepared)) {
    if (isDeniedInOptimizeMode("Bash", { command: body })) return true
  }

  const sanitized = stripInertQuoted(prepared)
  for (const rawSeg of splitSegments(sanitized)) {
    const seg = rawSeg.trim()
    if (!seg) continue
    if (SEGMENT_DENY_RE.test(seg)) return true
    if (HOST_SPAWN_PREFIX_RE.test(seg)) continue
    if (REDIRECT_DENY_RE.test(seg)) return true
  }
  return false
}


// ──────────────────────────────────────────────────────────────────────────
// optimize_mode session field + policy state — mirror Python registry
// ──────────────────────────────────────────────────────────────────────────

/** Flip `optimize_mode` true on the session record if currently false.
 *  Refuses subagents (exp_id set). Returns true on transition.
 *  Mirrors `mark_optimize_mode` in evo/inject/registry.py. */
export function markOptimizeMode(runDir: string, sid: string): boolean {
  const p = sessionFile(runDir, sid)
  const rec = readJsonOrNull<any>(p)
  if (!rec) return false
  if (rec.exp_id) return false
  if (rec.optimize_mode) return false
  rec.optimize_mode = true
  rec.optimize_mode_at = nowIso()
  atomicWriteJson(p, rec)
  return true
}

/** Clear `optimize_mode` flag. Mirrors `unmark_optimize_mode`. */
export function unmarkOptimizeMode(runDir: string, sid: string): boolean {
  const p = sessionFile(runDir, sid)
  const rec = readJsonOrNull<any>(p)
  if (!rec) return false
  if (!rec.optimize_mode) return false
  rec.optimize_mode = false
  rec.optimize_mode_at = null
  atomicWriteJson(p, rec)
  return true
}

/** Arm `autonomous` (opt-in stop-nudge loop). Mirrors Python `mark_autonomous`.
 * Refuses subagents (exp_id). Returns true on the false->true transition. */
export function markAutonomous(runDir: string, sid: string): boolean {
  const p = sessionFile(runDir, sid)
  const rec = readJsonOrNull<any>(p)
  if (!rec) return false
  if (rec.exp_id) return false
  if (rec.autonomous) return false
  rec.autonomous = true
  rec.autonomous_at = nowIso()
  atomicWriteJson(p, rec)
  return true
}

/** Disarm `autonomous`. Mirrors Python `unmark_autonomous`. */
export function unmarkAutonomous(runDir: string, sid: string): boolean {
  const p = sessionFile(runDir, sid)
  const rec = readJsonOrNull<any>(p)
  if (!rec) return false
  if (!rec.autonomous) return false
  rec.autonomous = false
  rec.autonomous_at = null
  atomicWriteJson(p, rec)
  return true
}

/** Arm `subagents_only` (enforce the policy deny — orchestrator may not
 * edit). Mirrors Python `mark_subagents_only`. Refuses subagents. */
export function markSubagentsOnly(runDir: string, sid: string): boolean {
  const p = sessionFile(runDir, sid)
  const rec = readJsonOrNull<any>(p)
  if (!rec) return false
  if (rec.exp_id) return false
  if (rec.subagents_only) return false
  rec.subagents_only = true
  rec.subagents_only_at = nowIso()
  atomicWriteJson(p, rec)
  return true
}

/** Disarm `subagents_only` (allow orchestrator edits). Mirrors Python. */
export function unmarkSubagentsOnly(runDir: string, sid: string): boolean {
  const p = sessionFile(runDir, sid)
  const rec = readJsonOrNull<any>(p)
  if (!rec) return false
  if (!rec.subagents_only) return false
  rec.subagents_only = false
  rec.subagents_only_at = null
  atomicWriteJson(p, rec)
  return true
}


// `/optimize` prompt patterns per host. Mirrors `_OPTIMIZE_INVOCATION_PATTERNS`.
// The leading `[\s"']*` tolerates wrapping quotes — `opencode run "..."` and
// shell-quoted invocations land in chat.message with the literal `"` as the
// first character. Without this, the model never auto-arms optimize_mode
// because the regex sees `"/optimize` and the `/` isn't at the start.
// Position-agnostic: matches the invocation anywhere in the prompt.
// Boundary class `[^A-Za-z0-9_/:-]` before the slash prevents file-path
// matches like `src/optimize.py`. Mirrors Python
// `_OPTIMIZE_INVOCATION_PATTERNS` — keep in sync.
//
// Per-host forms (verified against each host's source):
//   - opencode: bare `/optimize` only (no namespacing in core).
//   - openclaw: bare `/optimize` + `/skill optimize` (generic invoker
//     registered as textAlias "/skill" in
//     src/auto-reply/commands-registry.shared.ts).
//   - pi: `/skill:optimize` is the only form pi actually expands
//     (@earendil-works/pi-coding-agent agent-session.ts:1149 —
//     `if (!text.startsWith("/skill:")) return text;`). Bare
//     `/optimize` accepted defensively for users mixing conventions.
//
// Each host has an array of patterns; auto-arm fires on any match.
const OPTIMIZE_PROMPT_RES: Record<string, RegExp[]> = {
  opencode: [/(?:^|[^A-Za-z0-9_/:-])\/optimize\b/i],
  openclaw: [
    /(?:^|[^A-Za-z0-9_/:-])\/optimize\b/i,
    /(?:^|[^A-Za-z0-9_/:-])\/skill\s+optimize\b/i,
  ],
  pi: [
    /(?:^|[^A-Za-z0-9_/:-])\/skill:optimize\b/i,
    /(?:^|[^A-Za-z0-9_/:-])\/optimize\b/i,
  ],
}

/** Auto-arm optimize_mode if the user's prompt matches the host's
 *  `/optimize` invocation pattern. Mirrors `_maybe_mark_optimize_from_prompt`. */
export function maybeMarkOptimizeFromPrompt(
  runDir: string,
  sid: string,
  host: string,
  promptText: string | null | undefined,
): void {
  if (!promptText) return
  const patterns = OPTIMIZE_PROMPT_RES[host]
  if (!patterns) return
  if (!patterns.some((re) => re.test(promptText))) return
  markOptimizeMode(runDir, sid)
}


// Policy state — per-session violation counter. Same file as Python
// `_policy_state_file` (inject/policy_state/<sid>.json).
function policyStateFile(runDir: string, sid: string): string {
  return path.join(injectRoot(runDir), "policy_state", `${sid}.json`)
}

function readPolicyState(runDir: string, sid: string): Record<string, any> {
  return readJsonOrNull<Record<string, any>>(policyStateFile(runDir, sid)) || {}
}

function writePolicyState(runDir: string, sid: string, data: Record<string, any>): void {
  atomicWriteJson(policyStateFile(runDir, sid), data)
}


/** Increment the policy violation counter and return whether this odd-
 *  numbered violation should fire a deny. Alternating cadence matches the
 *  Python `_should_policy_block` `count % 2 === 1` rule. */
export function incrementAndShouldBlock(runDir: string, sid: string, toolName: string | null | undefined): boolean {
  const state = readPolicyState(runDir, sid)
  const count = (state.violation_count || 0) + 1
  state.violation_count = count
  state.last_violation_tool = toolName || ""
  state.nudge_pending = true
  writePolicyState(runDir, sid, state)
  return count % 2 === 1
}


/** Decide whether to deny the current tool call on the orchestrator session
 *  under optimize_mode. Mirrors `_should_policy_block` minus the hook-event
 *  case check (opencode tool.execute.before is unambiguously pre-tool). */
export function shouldPolicyBlock(
  runDir: string,
  sid: string,
  toolName: string | null | undefined,
  toolInput: unknown,
): boolean {
  const sess = getSession(runDir, sid) as any
  if (!sess) return false
  if (sess.exp_id) return false
  if (!sess.optimize_mode) return false
  if (!sess.subagents_only) return false  // opt-in: default allows orchestrator edits
  if (!isDeniedInOptimizeMode(toolName, toolInput)) return false
  return incrementAndShouldBlock(runDir, sid, toolName)
}


/** Return the stop-nudge text if the session is an orchestrator in
 *  optimize_mode. Always fires (no progress gate). Mirrors
 *  `_maybe_stop_nudge_text`. */
export function maybeStopNudgeText(runDir: string, sid: string): string | null {
  const sess = getSession(runDir, sid) as any
  if (!sess) return null
  if (sess.exp_id) return null
  if (!sess.optimize_mode) return null
  if (!sess.autonomous) return null  // opt-in only; default stops naturally
  return STOP_NUDGE_TEMPLATE
}

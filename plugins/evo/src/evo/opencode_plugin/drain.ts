// TS port of evo.inject.drain — used by in-process plugins on opencode and
// openclaw. Mirrors the Python `drain.py` + `queue.py` + `marker.py` logic.
// Schema parity is enforced by `tests/inject_fixtures/*.json` consumed by
// both implementations.
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
  const lines: string[] = []
  for (const ev of events) {
    if (ev.text) {
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
  }
  atomicWriteJson(p, rec)
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

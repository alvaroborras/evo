// ../../opencode_plugin/drain.ts
import * as fs from "fs";
import * as path from "path";
var QUEUE_SCHEMA_VERSION = 1;
function injectRoot(runDir) {
  return path.join(runDir, "inject");
}
function sessionFile(runDir, sid) {
  return path.join(injectRoot(runDir), "sessions", `${sid}.json`);
}
function workspaceEventsPath(runDir) {
  return path.join(injectRoot(runDir), "events", "workspace.jsonl");
}
function expEventsPath(runDir, expId) {
  return path.join(injectRoot(runDir), "events", `${expId}.jsonl`);
}
function offsetFile(runDir, sid) {
  return path.join(injectRoot(runDir), "offsets", `${sid}.json`);
}
function markerFile(runDir, sid) {
  return path.join(injectRoot(runDir), "markers", `${sid}.flag`);
}
function readJsonOrNull(p) {
  try {
    return JSON.parse(fs.readFileSync(p, "utf8"));
  } catch {
    return null;
  }
}
function atomicWriteJson(p, data) {
  fs.mkdirSync(path.dirname(p), { recursive: true });
  const tmp = `${p}.tmp.${process.pid}`;
  fs.writeFileSync(tmp, JSON.stringify(data));
  fs.renameSync(tmp, p);
}
function unlinkIfExists(p) {
  try {
    fs.unlinkSync(p);
  } catch {}
}
function readEventsAfter(queuePath, afterId) {
  if (!fs.existsSync(queuePath))
    return [];
  let text;
  try {
    text = fs.readFileSync(queuePath, "utf8");
  } catch {
    return [];
  }
  const out = [];
  for (const line of text.split(`
`)) {
    const trimmed = line.trim();
    if (!trimmed)
      continue;
    let rec;
    try {
      rec = JSON.parse(trimmed);
    } catch {
      continue;
    }
    const recId = rec?.id;
    if (typeof recId !== "string")
      continue;
    if (afterId === null || recId > afterId) {
      out.push(rec);
    }
  }
  return out;
}
function readOffset(runDir, sid, queue) {
  const data = readJsonOrNull(offsetFile(runDir, sid));
  if (!data)
    return null;
  if (queue === "workspace")
    return data.last_workspace_event_id ?? null;
  if (queue === "exp")
    return data.last_exp_event_id ?? null;
  return null;
}
function nowIso() {
  return new Date().toISOString().replace(/\.\d{3}Z$/, "+00:00");
}
function writeOffset(runDir, sid, opts) {
  const p = offsetFile(runDir, sid);
  let data = readJsonOrNull(p) ?? {};
  data.schema_version = QUEUE_SCHEMA_VERSION;
  data.session_id = sid;
  if (opts.workspaceId !== undefined && opts.workspaceId !== null) {
    data.last_workspace_event_id = opts.workspaceId;
  }
  if (opts.expId !== undefined && opts.expId !== null) {
    data.last_exp_event_id = opts.expId;
  }
  data.updated_at = nowIso();
  atomicWriteJson(p, data);
}
function formatDirectiveText(events) {
  const lines = [];
  for (const ev of events) {
    if (ev.text) {
      lines.push("[EVO DIRECTIVE]");
      lines.push(ev.text);
      lines.push("[END EVO DIRECTIVE]");
    }
  }
  return lines.join(`
`);
}
function getSession(runDir, sid) {
  return readJsonOrNull(sessionFile(runDir, sid));
}
function isRegistered(runDir, sid) {
  return fs.existsSync(sessionFile(runDir, sid));
}
var REGISTRY_SCHEMA_VERSION = 1;
function registerSession(runDir, sid, host, expId = null) {
  const p = sessionFile(runDir, sid);
  const now = nowIso();
  const existing = readJsonOrNull(p);
  if (existing) {
    existing.last_seen_at = now;
    atomicWriteJson(p, existing);
    return;
  }
  const rec = {
    schema_version: REGISTRY_SCHEMA_VERSION,
    session_id: sid,
    host,
    pid: process.pid,
    registered_at: now,
    last_seen_at: now,
    exp_id: expId,
    parent_session_id: null
  };
  atomicWriteJson(p, rec);
}
function findEvoRunDir(cwd) {
  const envRunDir = process.env.EVO_RUN_DIR;
  if (envRunDir)
    return envRunDir;
  let dir = cwd || process.cwd();
  while (dir !== "/" && dir !== "") {
    const evoDir = path.join(dir, ".evo");
    if (fs.existsSync(evoDir)) {
      try {
        const runs = fs.readdirSync(evoDir).filter((n) => n.startsWith("run_")).sort();
        if (runs.length === 0)
          return null;
        return path.join(evoDir, runs[runs.length - 1]);
      } catch {
        return null;
      }
    }
    const parent = path.dirname(dir);
    if (parent === dir)
      break;
    dir = parent;
  }
  return null;
}
function drainSession(runDir, sessionId) {
  const sess = getSession(runDir, sessionId);
  if (!sess) {
    unlinkIfExists(markerFile(runDir, sessionId));
    return { text: null, newWorkspaceOffset: null, newExpOffset: null };
  }
  const expId = sess.exp_id;
  let events = [];
  let newWorkspaceOffset = null;
  let newExpOffset = null;
  if (expId) {
    const lastId = readOffset(runDir, sessionId, "exp");
    const newEvents = readEventsAfter(expEventsPath(runDir, expId), lastId);
    events = newEvents;
    if (newEvents.length > 0)
      newExpOffset = newEvents[newEvents.length - 1].id;
  } else {
    const lastId = readOffset(runDir, sessionId, "workspace");
    const newEvents = readEventsAfter(workspaceEventsPath(runDir), lastId);
    events = newEvents;
    if (newEvents.length > 0)
      newWorkspaceOffset = newEvents[newEvents.length - 1].id;
  }
  const text = events.length > 0 ? formatDirectiveText(events) : null;
  if (newWorkspaceOffset || newExpOffset) {
    writeOffset(runDir, sessionId, {
      workspaceId: newWorkspaceOffset,
      expId: newExpOffset
    });
  }
  unlinkIfExists(markerFile(runDir, sessionId));
  return { text, newWorkspaceOffset, newExpOffset };
}

// index.ts
import * as crypto from "crypto";
import * as fs2 from "fs";
import * as os from "os";
import * as path2 from "path";
var DEBUG = process.env.EVO_DEBUG_INJECT === "1";
var BANNER_OPEN = "[EVO DIRECTIVE]";
function log(line) {
  if (!DEBUG)
    return;
  try {
    fs2.appendFileSync("/tmp/evo-inject.log", `[${new Date().toISOString()}] ${line}
`);
  } catch {}
}
function findOpenclawRunDir() {
  const cwdRun = findEvoRunDir(process.cwd());
  if (cwdRun)
    return cwdRun;
  const fallback = path2.join(os.homedir(), ".openclaw", "workspace");
  if (fs2.existsSync(fallback)) {
    return findEvoRunDir(fallback);
  }
  return null;
}
function deriveSessionId() {
  const runDir = findOpenclawRunDir() || process.cwd();
  const marker = "/.evo/";
  const idx = runDir.indexOf(marker);
  const workspace = idx >= 0 ? runDir.slice(0, idx) : process.cwd();
  const hash = crypto.createHash("sha256").update(workspace).digest("hex").slice(0, 12);
  return "openclaw-" + hash;
}
var drainedTexts = [];
function directiveBanner() {
  if (drainedTexts.length === 0)
    return "";
  return `
` + drainedTexts.join(`

`);
}
var native_default = {
  id: "evo-inject",
  name: "Evo Mid-Run Inject",
  description: "Delivers `evo direct` directives mid-conversation by appending them to the most recent tool-result message via tool_result_persist.",
  register(api) {
    log(`register() called, cwd=${process.cwd()}`);
    const ensureRegistered = () => {
      const runDir = findOpenclawRunDir();
      if (!runDir)
        return null;
      const sid = deriveSessionId();
      if (!isRegistered(runDir, sid)) {
        registerSession(runDir, sid, "openclaw");
        log(`registered session ${sid} in ${runDir}`);
      }
      return { runDir, sid };
    };
    const pumpDirectives = (runDir, sid) => {
      const result = drainSession(runDir, sid);
      if (result.text) {
        drainedTexts.push(result.text);
        log(`drained ${result.text.length} bytes`);
      }
    };
    for (const ev of ["agent_turn_prepare", "before_prompt_build", "before_agent_run", "session_start"]) {
      try {
        api.on(ev, async () => {
          ensureRegistered();
        });
      } catch {}
    }
    const interval = setInterval(() => {
      try {
        const ctx = ensureRegistered();
        if (ctx)
          pumpDirectives(ctx.runDir, ctx.sid);
      } catch {}
    }, 1500);
    if (typeof interval.unref === "function") {
      interval.unref();
    }
    api.on("tool_result_persist", async (event) => {
      const ctx = ensureRegistered();
      if (ctx)
        pumpDirectives(ctx.runDir, ctx.sid);
      if (drainedTexts.length === 0)
        return;
      const msg = event?.message ?? event?.assistantMessage ?? event;
      if (!msg || typeof msg !== "object")
        return;
      const banner = directiveBanner();
      let mutated = false;
      const tryAppendString = (obj, key) => {
        if (typeof obj?.[key] === "string" && !obj[key].includes(BANNER_OPEN)) {
          obj[key] = obj[key] + banner;
          mutated = true;
          return true;
        }
        return false;
      };
      if (Array.isArray(msg.content)) {
        for (const part of msg.content) {
          if (part && typeof part === "object") {
            if (tryAppendString(part, "text"))
              break;
            if (tryAppendString(part, "output"))
              break;
          }
        }
      }
      if (!mutated)
        tryAppendString(msg, "content");
      if (!mutated && msg.details && typeof msg.details === "object") {
        tryAppendString(msg.details, "text") || tryAppendString(msg.details, "output") || tryAppendString(msg.details, "stdout") || tryAppendString(msg.details, "content");
      }
      if (!mutated)
        tryAppendString(msg, "text");
      return mutated ? msg : undefined;
    });
  }
};
export {
  native_default as default
};

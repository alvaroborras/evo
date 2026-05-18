// drain.ts
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
import * as fs2 from "fs";
import * as path2 from "path";
function markerExists(runDir, sid) {
  return fs2.existsSync(path2.join(runDir, "inject", "markers", `${sid}.flag`));
}
var EvoPlugin = async ({ project }) => {
  const ensureRegistered = (sessionID) => {
    if (!sessionID)
      return null;
    const runDir = findEvoRunDir(project?.directory);
    if (!runDir)
      return null;
    if (!isRegistered(runDir, sessionID)) {
      registerSession(runDir, sessionID, "opencode");
    }
    return runDir;
  };
  return {
    "chat.message": async (input, output) => {
      const sessionID = input?.sessionID;
      if (!sessionID)
        return;
      const runDir = ensureRegistered(sessionID);
      if (!runDir)
        return;
      const hasMarker = markerExists(runDir, sessionID);
      if (!hasMarker) {}
      const result = drainSession(runDir, sessionID);
      if (result.text) {
        if (!Array.isArray(output.parts)) {
          output.parts = [];
        }
        const messageID = input?.messageID ?? output?.message?.id ?? output.parts[0]?.messageID ?? "";
        const partID = `prt_evo_${Date.now().toString(36)}_${Math.random().toString(36).slice(2, 10)}`;
        output.parts.unshift({
          type: "text",
          id: partID,
          sessionID,
          messageID,
          text: result.text
        });
      }
    },
    "tool.execute.before": async (input, _output) => {
      ensureRegistered(input?.sessionID);
    }
  };
};
var opencode_plugin_default = EvoPlugin;
export {
  opencode_plugin_default as default,
  EvoPlugin
};

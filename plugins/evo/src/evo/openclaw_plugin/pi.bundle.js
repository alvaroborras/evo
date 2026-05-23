// ../opencode_plugin/drain.ts
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
    if (!ev.text)
      continue;
    const id = ev.id || "";
    if (id) {
      lines.push(`[EVO DIRECTIVE id=${id}]`);
      lines.push(ev.text);
      lines.push(`[END EVO DIRECTIVE — when done, run: evo ack ${id}]`);
    } else {
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
    if (expId && !existing.exp_id)
      existing.exp_id = expId;
    if (existing.has_evo_engaged === undefined)
      existing.has_evo_engaged = false;
    if (existing.engaged_at === undefined)
      existing.engaged_at = null;
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
    parent_session_id: null,
    has_evo_engaged: false,
    engaged_at: null
  };
  atomicWriteJson(p, rec);
  initOffsetToLatest(runDir, sid);
}
function markEngaged(runDir, sid) {
  const p = sessionFile(runDir, sid);
  const rec = readJsonOrNull(p);
  if (!rec)
    return false;
  if (rec.has_evo_engaged)
    return false;
  rec.has_evo_engaged = true;
  rec.engaged_at = nowIso();
  atomicWriteJson(p, rec);
  return true;
}
function initOffsetToLatest(runDir, sid) {
  const wsPath = workspaceEventsPath(runDir);
  let latest = null;
  if (fs.existsSync(wsPath)) {
    const events = readEventsAfter(wsPath, null);
    if (events.length > 0)
      latest = events[events.length - 1].id;
  }
  writeOffset(runDir, sid, { workspaceId: latest });
}
var EVO_CMD_RE = /^\s*evo(\s|$)/;
function isEvoCommand(command) {
  if (!command || typeof command !== "string")
    return false;
  return EVO_CMD_RE.test(command);
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
function peekDrainSession(runDir, sessionId) {
  const sess = getSession(runDir, sessionId);
  if (!sess) {
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
  return { text, newWorkspaceOffset, newExpOffset };
}
function commitDrainPeek(runDir, sessionId, peek) {
  if (peek.newWorkspaceOffset || peek.newExpOffset) {
    writeOffset(runDir, sessionId, {
      workspaceId: peek.newWorkspaceOffset,
      expId: peek.newExpOffset
    });
  }
  unlinkIfExists(markerFile(runDir, sessionId));
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
var POLICY_NUDGE_TEMPLATE = `[EVO POLICY]
` + `Preventative block. You may have strayed from /evo:optimize protocol.
` + `
` + `When /optimize is active, the orchestrator should NOT edit files directly
` + `or run experiment commands by hand. The protocol is: write briefs, spawn
` + `subagents, let them do the edits and runs, then read the results.
` + `
` + "If you need to wait for subagents to finish, use `evo wait` — it blocks\n" + `until any experiment concludes, max 1h.
` + `
` + `Manual edits and shell commands that mutate files are discouraged unless
` + `evo doesn't already expose the operation. If this block was actually
` + "warranted (rare), run `evo exit-optimize-mode` to disable the safety\n" + `nudges, then retry the tool.
` + `[END EVO POLICY]
`;
var STOP_NUDGE_TEMPLATE = `[EVO LOOP]
` + `You are driving /evo:optimize autonomously. Don't stop, and don't ask
` + `the user for direction — they explicitly invoked /optimize for hands-off
` + "operation. They'll intervene via `evo direct` if they need to.\n" + `
` + "If subagents are still running: run `evo wait` to block until the next\n" + `experiment concludes (or up to 1h). Then resume planning.
` + `
` + "If subagents are done and you have unread results: read `evo scratchpad`,\n" + `update annotations as needed, and plan + spawn the next round.
` + `
` + "Stop only if `evo status` shows the budget exhausted or you've hit the\n" + `stall limit. If so, print a final summary first. To suppress this
` + `continuation loop for a legitimate one-off task, run
` + "`evo exit-optimize-mode`.\n" + `[END EVO LOOP]
`;
var DENY_TOOL_NAMES = new Set([
  "edit",
  "write",
  "notebookedit",
  "notebook_edit",
  "multiedit",
  "multi_edit",
  "edit_file",
  "create_file",
  "search_replace",
  "str_replace",
  "applypatch",
  "apply_patch",
  "delete_file",
  "file_write",
  "file_edit"
]);
var BASH_TOOL_NAMES = new Set([
  "bash",
  "shell",
  "exec",
  "run_terminal_cmd",
  "runterminalcmd",
  "run_command",
  "terminal",
  "execute_code",
  "execute"
]);
var SEGMENT_DENY_RE = /^\s*(?:nohup\s+)?(?:\S*\/)?(?:tee\b(?:\s+-[aiu]+)*\s+[^\s|&<>]+|sed\b[^|&;]*?\s-[a-zA-Z]*i[a-zA-Z]*\b|sed\b[^|&;]*?\s--in-place\b|perl\b[^|&;]*?\s-[a-zA-Z]*i[a-zA-Z]*\b|awk\b[^|&;]*?\s-i\s+inplace\b|(?:mv|cp|rm|mkdir|rmdir|touch|chmod|chown|chgrp|ln|rsync)(?:\s|$)|dd\b[^|&;]*?\bof=|curl\b[^|&;]*?\s-[a-zA-Z]*[oO][a-zA-Z=]*(?:\s|$)|curl\b[^|&;]*?\s--output(?:=|\s)|curl\b[^|&;]*?\s--remote-name\b|wget(?:\s|$)|patch(?:\s|$)|install(?:\s|$)|truncate(?:\s|$)|git\b(?:\s+(?:-[a-zA-Z]\S*|--[a-z][a-z-]*(?:=\S+)?)(?:\s+\S+)?)*?\s+(?:apply|checkout|restore|reset|clean|switch|merge|rebase|am|stash(?!\s+(?:list|show)\b)|cherry-pick|pull|clone|revert|worktree)\b|(?:vim|vi|nano|emacs)(?:\s|$))/;
var REDIRECT_DENY_RE = /(?:(?<![<\d&])>>?\s*[^\s|&<>;]+|\b\d+>>?\s*(?!&)[^\s|&<>;]+|&>>?\s*(?!&)[^\s|&<>;]+|>\|\s*[^\s|&<>;]+)/;
var HOST_SPAWN_PREFIX_RE = /^\s*(?:nohup\s+)?(?:claude(?:\s|$)|codex(?:\s|$)|cursor-agent(?:\s|$)|opencode(?:\s|$)|hermes(?:\s|$)|openclaw(?:\s|$)|pi(?:\s|$)|pi-coding-agent(?:\s|$))/;
var UNQUOTED_SEPARATOR_RE = /[;\n]|&&|\|\||\|(?!\|)|(?<![>&])&(?![&>])(?!\s*$)/;
function splitSegments(cmd) {
  return cmd.split(UNQUOTED_SEPARATOR_RE);
}
function extractSubstitutionBodies(seg) {
  const bodies = [];
  let i = 0;
  const n = seg.length;
  let state = "default";
  const findBalancedParenClose = (start) => {
    let depth = 1;
    let k = start;
    let inner = "default";
    while (k < n && depth > 0) {
      const cc = seg[k];
      if (inner === "sq") {
        if (cc === "'")
          inner = "default";
        k++;
        continue;
      }
      if (inner === "dq") {
        if (cc === "\\" && k + 1 < n) {
          k += 2;
          continue;
        }
        if (cc === '"') {
          inner = "default";
          k++;
          continue;
        }
      }
      if (cc === "\\" && k + 1 < n) {
        k += 2;
        continue;
      }
      if (cc === "'" && inner === "default") {
        inner = "sq";
      } else if (cc === '"' && inner === "default") {
        inner = "dq";
      } else if (cc === "(") {
        depth++;
      } else if (cc === ")") {
        depth--;
      }
      k++;
    }
    return depth === 0 ? k : -1;
  };
  while (i < n) {
    const c = seg[i];
    if (state === "sq") {
      if (c === "'")
        state = "default";
      i++;
      continue;
    }
    if (state === "dq") {
      if (c === "\\" && i + 1 < n) {
        i += 2;
        continue;
      }
      if (c === '"') {
        state = "default";
        i++;
        continue;
      }
    }
    if (c === "\\" && i + 1 < n) {
      i += 2;
      continue;
    }
    if (c === "'" && state === "default") {
      state = "sq";
      i++;
      continue;
    }
    if (c === '"' && state === "default") {
      state = "dq";
      i++;
      continue;
    }
    if (c === "$" && i + 1 < n && seg[i + 1] === "(") {
      if (i + 2 < n && seg[i + 2] === "(") {
        i += 3;
        continue;
      }
      const end = findBalancedParenClose(i + 2);
      if (end !== -1) {
        bodies.push(seg.slice(i + 2, end - 1));
        i = end;
        continue;
      }
    }
    if ((c === "<" || c === ">") && i + 1 < n && seg[i + 1] === "(" && state === "default") {
      const end = findBalancedParenClose(i + 2);
      if (end !== -1) {
        bodies.push(seg.slice(i + 2, end - 1));
        i = end;
        continue;
      }
    }
    if (c === "`" && state !== "sq") {
      let j = i + 1;
      while (j < n && seg[j] !== "`") {
        if (seg[j] === "\\" && j + 1 < n) {
          j += 2;
          continue;
        }
        j++;
      }
      if (j < n) {
        bodies.push(seg.slice(i + 1, j));
        i = j + 1;
        continue;
      }
    }
    i++;
  }
  return bodies;
}
function stripInertQuoted(cmd) {
  let out = cmd.replace(/'[^']*'/g, "''");
  out = out.replace(/"(?:[^"\\]|\\.)*"/g, (match) => {
    if (match.indexOf("$(") >= 0 || match.indexOf("`") >= 0)
      return match;
    return '""';
  });
  const buf = [];
  let i = 0;
  const n = out.length;
  while (i < n) {
    if (out[i] === "$" && i + 2 < n && out[i + 1] === "(" && out[i + 2] === "(") {
      let depth = 2;
      let j = i + 3;
      while (j < n && depth > 0) {
        if (out[j] === "(")
          depth++;
        else if (out[j] === ")")
          depth--;
        j++;
      }
      if (depth === 0) {
        i = j;
        continue;
      }
    }
    buf.push(out[i]);
    i++;
  }
  return buf.join("");
}
var SHELL_INTERPRETERS = new Set(["bash", "sh", "zsh", "dash", "ash"]);
function tokenize(cmd) {
  const out = [];
  let buf = "";
  let state = "default";
  let inToken = false;
  for (let i = 0;i < cmd.length; i++) {
    const c = cmd[i];
    if (state === "sq") {
      if (c === "'") {
        state = "default";
        continue;
      }
      buf += c;
      inToken = true;
      continue;
    }
    if (state === "dq") {
      if (c === "\\" && i + 1 < cmd.length) {
        buf += cmd[++i];
        continue;
      }
      if (c === '"') {
        state = "default";
        continue;
      }
      buf += c;
      inToken = true;
      continue;
    }
    if (c === "'") {
      state = "sq";
      inToken = true;
      continue;
    }
    if (c === '"') {
      state = "dq";
      inToken = true;
      continue;
    }
    if (c === "\\" && i + 1 < cmd.length) {
      buf += cmd[++i];
      inToken = true;
      continue;
    }
    if (/\s/.test(c)) {
      if (inToken) {
        out.push(buf);
        buf = "";
        inToken = false;
      }
      continue;
    }
    buf += c;
    inToken = true;
  }
  if (state !== "default")
    return null;
  if (inToken)
    out.push(buf);
  return out;
}
function unwrapShellCArguments(cmd) {
  const tokens = tokenize(cmd);
  if (!tokens || tokens.length === 0)
    return cmd;
  const appended = [];
  for (let i = 0;i < tokens.length; i++) {
    const tok = tokens[i];
    const name = tok.replace(/\/+$/, "").split("/").pop() || "";
    if (!SHELL_INTERPRETERS.has(name))
      continue;
    let j = i + 1;
    while (j < tokens.length) {
      const t = tokens[j];
      if (t === "-c") {
        if (j + 1 < tokens.length)
          appended.push(tokens[j + 1]);
        break;
      }
      if (t.startsWith("-") && !t.startsWith("--") && t.length > 1 && t.slice(1).indexOf("c") >= 0) {
        if (j + 1 < tokens.length)
          appended.push(tokens[j + 1]);
        break;
      }
      j++;
    }
  }
  if (appended.length === 0)
    return cmd;
  return cmd + " ; " + appended.join(" ; ");
}
function isDeniedInOptimizeMode(toolName, toolInput) {
  if (!toolName)
    return false;
  const t = toolName.toLowerCase();
  if (DENY_TOOL_NAMES.has(t))
    return true;
  if (!BASH_TOOL_NAMES.has(t))
    return false;
  const input = toolInput || {};
  const cmd = typeof input.command === "string" ? input.command : "";
  if (!cmd)
    return false;
  const prepared = unwrapShellCArguments(cmd);
  for (const body of extractSubstitutionBodies(prepared)) {
    if (isDeniedInOptimizeMode("Bash", { command: body }))
      return true;
  }
  const sanitized = stripInertQuoted(prepared);
  for (const rawSeg of splitSegments(sanitized)) {
    const seg = rawSeg.trim();
    if (!seg)
      continue;
    if (SEGMENT_DENY_RE.test(seg))
      return true;
    if (HOST_SPAWN_PREFIX_RE.test(seg))
      continue;
    if (REDIRECT_DENY_RE.test(seg))
      return true;
  }
  return false;
}
function markOptimizeMode(runDir, sid) {
  const p = sessionFile(runDir, sid);
  const rec = readJsonOrNull(p);
  if (!rec)
    return false;
  if (rec.exp_id)
    return false;
  if (rec.optimize_mode)
    return false;
  rec.optimize_mode = true;
  rec.optimize_mode_at = nowIso();
  atomicWriteJson(p, rec);
  return true;
}
var OPTIMIZE_PROMPT_RES = {
  opencode: /^\s*\/optimize\b/i,
  openclaw: /^\s*\/optimize\b/i,
  pi: /^\s*\/optimize\b/i
};
function maybeMarkOptimizeFromPrompt(runDir, sid, host, promptText) {
  if (!promptText)
    return;
  const re = OPTIMIZE_PROMPT_RES[host];
  if (!re)
    return;
  if (!re.test(promptText))
    return;
  markOptimizeMode(runDir, sid);
}
function policyStateFile(runDir, sid) {
  return path.join(injectRoot(runDir), "policy_state", `${sid}.json`);
}
function readPolicyState(runDir, sid) {
  return readJsonOrNull(policyStateFile(runDir, sid)) || {};
}
function writePolicyState(runDir, sid, data) {
  atomicWriteJson(policyStateFile(runDir, sid), data);
}
function incrementAndShouldBlock(runDir, sid, toolName) {
  const state = readPolicyState(runDir, sid);
  const count = (state.violation_count || 0) + 1;
  state.violation_count = count;
  state.last_violation_tool = toolName || "";
  state.nudge_pending = true;
  writePolicyState(runDir, sid, state);
  return count % 2 === 1;
}

// factory.ts
import * as crypto from "crypto";
function makeRegister(host) {
  function deriveSessionId() {
    const expId = process.env.EVO_EXP_ID || "";
    const seed = expId ? `${process.cwd()}|${expId}` : process.cwd();
    const hash = crypto.createHash("sha256").update(seed).digest("hex").slice(0, 12);
    return `${host}-${hash}`;
  }
  return function register(api) {
    const drainedTexts = [];
    const ensureRegistered = () => {
      const runDir = findEvoRunDir();
      if (!runDir)
        return null;
      const sid = deriveSessionId();
      if (!isRegistered(runDir, sid)) {
        const expId = process.env.EVO_EXP_ID || null;
        registerSession(runDir, sid, host, expId);
      }
      return { sid, runDir };
    };
    const appendToPayload = (event, text) => {
      if (Array.isArray(event.payload?.input)) {
        event.payload.input.push({
          role: "user",
          content: [{ type: "input_text", text }]
        });
      } else if (Array.isArray(event.payload?.messages)) {
        event.payload.messages.push({
          role: "user",
          content: [{ type: "text", text }]
        });
      }
    };
    api.on("session_start", () => {
      ensureRegistered();
    });
    const scanForEvoCommands = (payload) => {
      try {
        const items = Array.isArray(payload?.input) ? payload.input : [];
        for (const it of items) {
          const args = it?.arguments;
          if (typeof args === "string" && isEvoCommand(args))
            return true;
          if (typeof args === "object" && args) {
            const cmd = args.command ?? args.cmd ?? args.shell;
            if (typeof cmd === "string" && isEvoCommand(cmd))
              return true;
          }
        }
        const msgs = Array.isArray(payload?.messages) ? payload.messages : [];
        for (const m of msgs) {
          const content = Array.isArray(m?.content) ? m.content : [];
          for (const c of content) {
            if (c?.type === "tool_use") {
              const cmd = c?.input?.command ?? c?.input?.cmd;
              if (typeof cmd === "string" && isEvoCommand(cmd))
                return true;
            }
          }
        }
      } catch {}
      return false;
    };
    const extractLatestUserText = (payload) => {
      try {
        const items = Array.isArray(payload?.input) ? payload.input : [];
        for (let i = items.length - 1;i >= 0; i--) {
          const it = items[i];
          if (it?.role !== "user")
            continue;
          if (typeof it.content === "string" && it.content)
            return it.content;
          if (Array.isArray(it.content)) {
            for (const c of it.content) {
              if (typeof c?.text === "string" && c.text)
                return c.text;
            }
          }
        }
        const msgs = Array.isArray(payload?.messages) ? payload.messages : [];
        for (let i = msgs.length - 1;i >= 0; i--) {
          const m = msgs[i];
          if (m?.role !== "user")
            continue;
          if (typeof m.content === "string")
            return m.content;
          if (Array.isArray(m.content)) {
            for (const c of m.content) {
              if (typeof c?.text === "string" && c.text)
                return c.text;
            }
          }
        }
      } catch {}
      return "";
    };
    api.on("before_provider_request", (event, _ctx) => {
      const ctx = ensureRegistered();
      if (!ctx)
        return;
      const promptText = extractLatestUserText(event.payload);
      maybeMarkOptimizeFromPrompt(ctx.runDir, ctx.sid, host, promptText);
      if (scanForEvoCommands(event.payload)) {
        if (markEngaged(ctx.runDir, ctx.sid)) {
          initOffsetToLatest(ctx.runDir, ctx.sid);
        }
      }
      const result = drainSession(ctx.runDir, ctx.sid);
      if (result.text)
        drainedTexts.push(result.text);
      if (drainedTexts.length === 0)
        return;
      const combined = drainedTexts.join(`
`);
      appendToPayload(event, combined);
      return event.payload;
    });
    api.on("tool_call", (event, _ctx) => {
      const ctx = ensureRegistered();
      if (!ctx)
        return;
      const sess = getSession(ctx.runDir, ctx.sid);
      if (!sess)
        return;
      if (sess.exp_id)
        return;
      if (!sess.optimize_mode)
        return;
      const toolName = event?.toolName ?? event?.tool_name;
      const toolInput = event?.input ?? {};
      if (!isDeniedInOptimizeMode(toolName, toolInput))
        return;
      if (incrementAndShouldBlock(ctx.runDir, ctx.sid, toolName)) {
        return { block: true, reason: POLICY_NUDGE_TEMPLATE };
      }
    });
    api.on("turn_end", async (_event, _ctx) => {
      if (typeof api.sendUserMessage !== "function")
        return;
      const ctx = ensureRegistered();
      if (!ctx)
        return;
      const sess = getSession(ctx.runDir, ctx.sid);
      if (!sess)
        return;
      if (sess.exp_id)
        return;
      if (!sess.optimize_mode)
        return;
      const peek = peekDrainSession(ctx.runDir, ctx.sid);
      const text = peek.text ? peek.text + `

` + STOP_NUDGE_TEMPLATE : STOP_NUDGE_TEMPLATE;
      try {
        api.sendUserMessage(text, { deliverAs: "followUp" });
        commitDrainPeek(ctx.runDir, ctx.sid, peek);
      } catch (_e) {}
    });
  };
}

// pi-entry.ts
var pi_entry_default = makeRegister("pi");
export {
  pi_entry_default as default
};

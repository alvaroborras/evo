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
    if (!ev.text)
      continue;
    const id = ev.id || "";
    if (id) {
      lines.push(`[EVO DIRECTIVE id=${id}]`);
      lines.push(ev.text);
      lines.push(`[END EVO DIRECTIVE — run \`evo ack ${id}\` to confirm you have received this message, then proceed]`);
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
  "file_edit",
  "patch"
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
  opencode: [/(?:^|[^A-Za-z0-9_/:-])\/optimize\b/i],
  openclaw: [
    /(?:^|[^A-Za-z0-9_/:-])\/optimize\b/i,
    /(?:^|[^A-Za-z0-9_/:-])\/skill\s+optimize\b/i
  ],
  pi: [
    /(?:^|[^A-Za-z0-9_/:-])\/skill:optimize\b/i,
    /(?:^|[^A-Za-z0-9_/:-])\/optimize\b/i
  ]
};
function maybeMarkOptimizeFromPrompt(runDir, sid, host, promptText) {
  if (!promptText)
    return;
  const patterns = OPTIMIZE_PROMPT_RES[host];
  if (!patterns)
    return;
  if (!patterns.some((re) => re.test(promptText)))
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
function shouldPolicyBlock(runDir, sid, toolName, toolInput) {
  const sess = getSession(runDir, sid);
  if (!sess)
    return false;
  if (sess.exp_id)
    return false;
  if (!sess.optimize_mode)
    return false;
  if (!isDeniedInOptimizeMode(toolName, toolInput))
    return false;
  return incrementAndShouldBlock(runDir, sid, toolName);
}
function maybeStopNudgeText(runDir, sid) {
  const sess = getSession(runDir, sid);
  if (!sess)
    return null;
  if (sess.exp_id)
    return null;
  if (!sess.optimize_mode)
    return null;
  return STOP_NUDGE_TEMPLATE;
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
  const expId = process.env.EVO_EXP_ID || "";
  const seed = expId ? `${workspace}|${expId}` : workspace;
  const hash = crypto.createHash("sha256").update(seed).digest("hex").slice(0, 12);
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
        const expId = process.env.EVO_EXP_ID || null;
        registerSession(runDir, sid, "openclaw", expId);
        log(`registered session ${sid} in ${runDir}${expId ? " (exp_id=" + expId + ")" : ""}`);
        if (markEngaged(runDir, sid)) {
          initOffsetToLatest(runDir, sid);
        }
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
    for (const ev of ["agent_turn_prepare", "before_agent_run", "session_start"]) {
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
    const extractLatestUserText = (event) => {
      try {
        const sources = [
          event?.messages,
          event?.payload?.messages,
          event?.input,
          event?.payload?.input,
          event?.userMessage ? [event.userMessage] : null,
          event?.prompt ? [{ role: "user", content: event.prompt }] : null
        ];
        for (const arr of sources) {
          if (!Array.isArray(arr))
            continue;
          for (let i = arr.length - 1;i >= 0; i--) {
            const m = arr[i];
            if (!m || m.role && m.role !== "user")
              continue;
            if (typeof m.content === "string" && m.content)
              return m.content;
            if (Array.isArray(m.content)) {
              for (const c of m.content) {
                if (typeof c?.text === "string" && c.text)
                  return c.text;
              }
            }
            if (typeof m.text === "string" && m.text)
              return m.text;
          }
        }
      } catch {}
      return null;
    };
    api.on("before_prompt_build", async (event) => {
      const ctx = ensureRegistered();
      if (!ctx)
        return;
      try {
        const promptText = extractLatestUserText(event);
        if (promptText) {
          maybeMarkOptimizeFromPrompt(ctx.runDir, ctx.sid, "openclaw", promptText);
        }
      } catch {}
      return;
    });
    api.on("before_tool_call", async (event) => {
      try {
        const ctx = ensureRegistered();
        if (!ctx)
          return;
        const toolName = event?.toolName ?? event?.tool_name ?? event?.tool?.name;
        const toolInput = event?.params ?? event?.input ?? event?.tool?.params ?? {};
        if (shouldPolicyBlock(ctx.runDir, ctx.sid, toolName, toolInput)) {
          log(`deny ${toolName} in optimize_mode`);
          return { block: true, blockReason: POLICY_NUDGE_TEMPLATE };
        }
      } catch (err) {
        log(`before_tool_call error (fail-open): ${err?.message ?? err}`);
      }
      return;
    });
    api.on("before_agent_finalize", async (event) => {
      try {
        const ctx = ensureRegistered();
        if (!ctx)
          return;
        const instruction = maybeStopNudgeText(ctx.runDir, ctx.sid);
        if (instruction) {
          const turnId = event?.agentRunId ?? event?.runId ?? Date.now();
          log(`stop nudge revise sid=${ctx.sid} turn=${turnId}`);
          return {
            action: "revise",
            retry: {
              instruction,
              idempotencyKey: `evo-optimize-${ctx.sid}-${turnId}`
            }
          };
        }
      } catch (err) {
        log(`before_agent_finalize error: ${err?.message ?? err}`);
      }
      return;
    });
    api.on("tool_result_persist", (event) => {
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

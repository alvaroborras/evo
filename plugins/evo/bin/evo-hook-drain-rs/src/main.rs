// evo-hook-drain — hot-path hook invoked by host plugins (Claude Code, Codex).
//
// Reads session_id from stdin's JSON payload (host hook contract), then does
// two stat checks; exits in ~1-3ms when there's nothing to deliver. Hands off
// to `evo-drain` (Python console_script) only when the marker says there's
// something to drain.
//
// Cross-platform (Linux / macOS / Windows). Built natively per target via CI
// (no cross-toolchain needed). Pure stdlib — no crate deps — for smallest
// binary and fastest startup.
//
// See notes/cross-host-inject-design.md.

use std::env;
use std::fs;
use std::io::{self, Read, Write};
use std::path::{Path, PathBuf};
use std::process::{self, Command, Stdio};
use std::time::{SystemTime, UNIX_EPOCH};

const OK_EMPTY: &str = "{}";

fn emit_ok() -> ! {
    print!("{}", OK_EMPTY);
    process::exit(0);
}

fn read_stdin() -> String {
    use std::io::IsTerminal;
    let mut buf = String::new();
    if io::stdin().is_terminal() {
        return buf;
    }
    let _ = io::stdin().read_to_string(&mut buf);
    buf
}

/// Find the captured group of `"key"\s*:\s*"VALUE"` in a JSON-ish buffer.
/// Hand-rolled scan — avoids pulling in regex crate which would bloat the
/// binary by ~500 KB.
fn find_json_string(buf: &str, key: &str) -> Option<String> {
    let needle = format!("\"{}\"", key);
    let start = buf.find(&needle)?;
    let rest = &buf[start + needle.len()..];
    let colon = rest.find(':')?;
    let after_colon = &rest[colon + 1..];
    let quote = after_colon.find('"')?;
    let value_start = quote + 1;
    let after_quote = &after_colon[value_start..];
    let end = after_quote.find('"')?;
    Some(after_quote[..end].to_string())
}

/// Per-host env var → host string. Ordered for stable fallback when
/// no payload session_id is present and multiple env vars are set
/// (nested env corner case). The match-by-value path in
/// `resolve_session` makes the order irrelevant when a payload sid is
/// available — we pick the env var whose value equals the payload sid.
const HOST_ENV_VARS: &[(&str, &str)] = &[
    ("CLAUDE_CODE_SESSION_ID", "claude-code"),
    ("CODEX_THREAD_ID", "codex"),
    ("HERMES_SESSION_ID", "hermes"),
    ("OPENCODE_SESSION_ID", "opencode"),
];

fn detect_host_from_path(buf: &str) -> &'static str {
    if buf.contains(".codex/") || buf.contains("\\.codex\\") {
        "codex"
    } else if buf.contains(".hermes/") || buf.contains("\\.hermes\\") {
        "hermes"
    } else if buf.contains(".opencode/") || buf.contains("\\.opencode\\") {
        "opencode"
    } else {
        "claude-code"
    }
}

/// Resolve (session_id, host) together. The payload session_id wins;
/// host is the env var whose value matches that sid. If no env var
/// matches, fall back to path-fragment detection. With no payload sid,
/// the first non-empty env var in `HOST_ENV_VARS` order supplies both
/// sid and host — that handles the standalone case while keeping
/// matched-sid the priority signal in nested envs.
fn resolve_session(stdin_buf: &str) -> (String, &'static str) {
    if let Some(sid) = find_json_string(stdin_buf, "session_id") {
        // Pick the env var whose value matches; that's authoritative
        // for which host owns this session.
        for (var, host) in HOST_ENV_VARS {
            if let Ok(v) = env::var(var) {
                if v == sid {
                    return (sid, host);
                }
            }
        }
        // No env var matches the payload sid — fall back to path
        // detection. Common in test harnesses + nested cases where the
        // outer host's env vars don't reflect this inner session.
        return (sid, detect_host_from_path(stdin_buf));
    }
    // No payload sid — pick the first env var set.
    for (var, host) in HOST_ENV_VARS {
        if let Ok(v) = env::var(var) {
            if !v.is_empty() {
                return (v, host);
            }
        }
    }
    (String::new(), "claude-code")
}

fn find_evo_run_dir() -> Option<PathBuf> {
    if let Ok(v) = env::var("EVO_RUN_DIR") {
        if !v.is_empty() {
            return Some(PathBuf::from(v));
        }
    }
    let mut cwd = env::current_dir().ok()?;
    loop {
        let evo_dir = cwd.join(".evo");
        if evo_dir.is_dir() {
            let mut runs: Vec<PathBuf> = fs::read_dir(&evo_dir)
                .ok()?
                .filter_map(|e| e.ok())
                .map(|e| e.path())
                .filter(|p| {
                    p.is_dir()
                        && p.file_name()
                            .and_then(|n| n.to_str())
                            .map_or(false, |n| n.starts_with("run_"))
                })
                .collect();
            runs.sort();
            return runs.into_iter().last();
        }
        if !cwd.pop() {
            return None;
        }
    }
}


fn iso8601_utc_now() -> String {
    let secs = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap_or_default()
        .as_secs() as i64;
    // Days since 1970-01-01; from there compute year/month/day.
    let days = secs / 86400;
    let mut remaining = secs % 86400;
    if remaining < 0 {
        remaining += 86400;
    }
    let hh = remaining / 3600;
    let mm = (remaining % 3600) / 60;
    let ss = remaining % 60;
    let (y, mo, d) = civil_from_days(days);
    // `+00:00` (not `Z`) so Python `datetime.fromisoformat()` parses it
    // on 3.10 — that version doesn't accept the `Z` suffix. The Python
    // side uses `.isoformat()` which already emits `+00:00`, so this
    // also makes the two writers byte-compatible.
    format!("{:04}-{:02}-{:02}T{:02}:{:02}:{:02}+00:00", y, mo, d, hh, mm, ss)
}

/// Howard Hinnant's date algorithm — convert days-since-1970 to (Y, M, D).
fn civil_from_days(z: i64) -> (i64, u32, u32) {
    let z = z + 719468;
    let era = if z >= 0 { z } else { z - 146096 } / 146097;
    let doe = (z - era * 146097) as u64;
    let yoe = (doe - doe / 1460 + doe / 36524 - doe / 146096) / 365;
    let y = yoe as i64 + era * 400;
    let doy = doe - (365 * yoe + yoe / 4 - yoe / 100);
    let mp = (5 * doy + 2) / 153;
    let d = (doy - (153 * mp + 2) / 5 + 1) as u32;
    let m = (if mp < 10 { mp + 3 } else { mp - 9 }) as u32;
    let y = if m <= 2 { y + 1 } else { y };
    (y, m, d)
}

fn register_session(run_dir: &Path, sid: &str, host: &str, engage: bool) -> io::Result<()> {
    let sessions_dir = run_dir.join("inject").join("sessions");
    fs::create_dir_all(&sessions_dir)?;
    let now = iso8601_utc_now();
    // Honor EVO_EXP_ID at first registration so a subagent's first
    // SessionStart drain correctly routes to the exp queue (drain.py
    // chooses workspace-vs-exp based on `sess.exp_id`). Without this,
    // a directive queued by `evo direct --to exp_id` BEFORE the Python
    // `auto_register_from_env` merge can be missed.
    let exp_id = match env::var("EVO_EXP_ID") {
        Ok(v) if !v.is_empty() => Some(v),
        _ => None,
    };
    let exp_id_json = match &exp_id {
        Some(v) => format!(r#""{}""#, escape_json_str(v)),
        None => "null".to_string(),
    };
    // Engage the orchestrator at SessionStart. The hook-host process that
    // fired SessionStart IS the orchestrator, so SessionStart is its
    // engagement signal — same as the in-process JS plugins (pi/openclaw)
    // marking engaged at session_start. Under /optimize the orchestrator
    // dispatches every `evo` command to subagents, so it never runs `evo`
    // itself; without engaging here, the Python `evo direct` broadcast
    // filters it out (skipped_unengaged) and mid-run directives never
    // land. NEVER engage a subagent registration (EVO_EXP_ID set) — a
    // subagent must not engage the workspace loop on its own.
    let engaged = engage && exp_id.is_none();
    let (engaged_json, engaged_at_json) = if engaged {
        ("true".to_string(), format!(r#""{}""#, now))
    } else {
        ("false".to_string(), "null".to_string())
    };
    let payload = format!(
        r#"{{"schema_version":1,"session_id":"{}","host":"{}","pid":{},"registered_at":"{}","last_seen_at":"{}","exp_id":{},"parent_session_id":null,"has_evo_engaged":{},"engaged_at":{},"optimize_mode":false,"optimize_mode_at":null}}"#,
        sid,
        host,
        process::id(),
        now,
        now,
        exp_id_json,
        engaged_json,
        engaged_at_json,
    );
    fs::write(sessions_dir.join(format!("{}.json", sid)), payload)
}

/// Minimal JSON-string escape for the small set of chars likely to
/// appear in session/exp ids. We never expect quotes or backslashes,
/// but be defensive.
fn escape_json_str(s: &str) -> String {
    let mut out = String::with_capacity(s.len());
    for c in s.chars() {
        match c {
            '"' => out.push_str("\\\""),
            '\\' => out.push_str("\\\\"),
            '\n' => out.push_str("\\n"),
            '\r' => out.push_str("\\r"),
            '\t' => out.push_str("\\t"),
            _ => out.push(c),
        }
    }
    out
}

fn read_version(manifest: &Path) -> Option<String> {
    let text = fs::read_to_string(manifest).ok()?;
    find_json_string(&text, "version")
}

/// Cross-platform `which`: probe PATH (and PATHEXT on Windows).
fn which(cmd: &str) -> Option<PathBuf> {
    let path_sep = if cfg!(windows) { ';' } else { ':' };
    let exts: Vec<String> = if cfg!(windows) {
        env::var("PATHEXT")
            .unwrap_or_else(|_| ".COM;.EXE;.BAT;.CMD".into())
            .split(';')
            .map(|s| s.to_string())
            .collect()
    } else {
        vec![String::new()]
    };
    let path_var = env::var("PATH").unwrap_or_default();
    for dir in path_var.split(path_sep).filter(|s| !s.is_empty()) {
        for ext in &exts {
            let candidate = Path::new(dir).join(format!("{}{}", cmd, ext));
            if candidate.is_file() {
                return Some(candidate);
            }
        }
    }
    None
}

fn session_start_drift_checks(plugin_root: &Path) {
    let cache_manifest = plugin_root.join(".claude-plugin").join("plugin.json");
    let plugin_root_str = plugin_root.to_string_lossy().replace('\\', "/");

    let home = env::var("HOME")
        .or_else(|_| env::var("USERPROFILE"))
        .map(PathBuf::from)
        .ok();

    let mkt_manifest: Option<PathBuf> = home.as_ref().and_then(|h| {
        if plugin_root_str.contains("/.claude/plugins/cache/") {
            Some(
                h.join(".claude")
                    .join("plugins")
                    .join("marketplaces")
                    .join("evo-hq-evo")
                    .join("plugins")
                    .join("evo")
                    .join(".claude-plugin")
                    .join("plugin.json"),
            )
        } else if plugin_root_str.contains("/.codex/plugins/cache/") {
            Some(
                h.join(".codex")
                    .join(".tmp")
                    .join("marketplaces")
                    .join("evo-hq")
                    .join("plugins")
                    .join("evo")
                    .join(".claude-plugin")
                    .join("plugin.json"),
            )
        } else {
            None
        }
    });

    if let Some(mkt) = mkt_manifest {
        if mkt.is_file() && cache_manifest.is_file() {
            if let (Some(cv), Some(mv)) = (read_version(&cache_manifest), read_version(&mkt)) {
                if cv != mv {
                    let _ = writeln!(
                        io::stderr(),
                        "evo: plugin cache is stale (running {}, marketplace has {}). Run: evo update --force",
                        cv, mv
                    );
                }
            }
        }
    }

    if which("evo-drain").is_none() {
        let _ = writeln!(
            io::stderr(),
            "evo: install evo-hq-cli to enable mid-run inject (uv tool install evo-hq-cli)"
        );
    }
}

fn handoff_to_drain(run_dir: &Path, sid: &str, host: &str, stdin_buf: &str) -> ! {
    let drain = match which("evo-drain") {
        Some(p) => p,
        None => {
            let _ = writeln!(
                io::stderr(),
                "evo-hook-drain: install evo-hq-cli to enable drain — 'uv tool install evo-hq-cli' or 'pipx install evo-hq-cli'"
            );
            print!("{}", OK_EMPTY);
            process::exit(1);
        }
    };

    let mut child = match Command::new(&drain)
        .arg("--run-dir")
        .arg(run_dir)
        .arg("--session")
        .arg(sid)
        .arg("--host")
        .arg(host)
        .stdin(Stdio::piped())
        .stdout(Stdio::inherit())
        .stderr(Stdio::inherit())
        .spawn()
    {
        Ok(c) => c,
        Err(_) => {
            print!("{}", OK_EMPTY);
            process::exit(1);
        }
    };

    if let Some(stdin) = child.stdin.as_mut() {
        let _ = stdin.write_all(stdin_buf.as_bytes());
    }
    let status = child.wait().map(|s| s.code().unwrap_or(1)).unwrap_or(1);
    process::exit(status);
}


/// Returns true if `<run_dir>/inject/markers/<sid>.flag` exists.
fn marker_exists(run_dir: &Path, sid: &str) -> bool {
    run_dir
        .join("inject")
        .join("markers")
        .join(format!("{}.flag", sid))
        .is_file()
}

/// Returns true if `<run_dir>/inject/optimize_mode/<sid>.flag` exists.
/// This is the side-channel signal that says "this session is the
/// orchestrator driving /evo:optimize" — the deny gate + stop nudge
/// need drain to run on tool/stop events.
fn optimize_flag_exists(run_dir: &Path, sid: &str) -> bool {
    run_dir
        .join("inject")
        .join("optimize_mode")
        .join(format!("{}.flag", sid))
        .is_file()
}

/// Decide whether to hand off to the Python drain. Inputs are the three
/// fast-path stat results. SessionStart is handled before this; here we
/// gate everything else.
///
/// Rules:
///   - marker exists                       → hand off (deliver directive)
///   - optimize flag on + tool/stop event  → hand off (policy / stop nudge)
///   - optimize flag off + UserPromptSubmit → hand off (might detect /optimize)
///   - otherwise                            → fast exit ({})
fn should_handoff(hook_event: &str, marker: bool, opt_flag: bool) -> bool {
    if marker {
        return true;
    }
    if opt_flag {
        return matches!(hook_event, "PreToolUse" | "Stop" | "SubagentStop");
    }
    matches!(hook_event, "UserPromptSubmit")
}

fn main() {
    let stdin_buf = read_stdin();

    // Resolve session id + host together. Matching env var value to
    // payload sid is what makes nested envs (codex spawned from claude
    // and vice versa) classify correctly.
    let (sid, host) = resolve_session(&stdin_buf);
    if sid.is_empty() {
        emit_ok();
    }

    let run_dir = match find_evo_run_dir() {
        Some(d) => d,
        None => emit_ok(),
    };

    let hook_event = find_json_string(&stdin_buf, "hook_event_name").unwrap_or_default();

    let sessions_file = run_dir.join("inject").join("sessions").join(format!("{}.json", sid));

    if hook_event == "SessionStart" {
        if !sessions_file.is_file() {
            // engage=true: SessionStart on the hook host IS the orchestrator
            // engagement signal. register_session refuses to engage if
            // EVO_EXP_ID is set (subagent context).
            let _ = register_session(&run_dir, &sid, host, true);
        }
        // Plugin root = parent of the directory containing this executable.
        let exe = env::current_exe().ok();
        let plugin_root = exe
            .as_ref()
            .and_then(|e| e.parent())
            .and_then(|p| p.parent())
            .map(PathBuf::from);
        if let Some(root) = plugin_root {
            session_start_drift_checks(&root);
        }
        // Fall through to handoff: drain seeds offsets + emits empty
        // additionalContext envelope. Cheap and matches prior behavior.
        handoff_to_drain(&run_dir, &sid, host, &stdin_buf);
    }

    // Resume support: claude-code resumes (and `evo init` mid-session)
    // don't fire SessionStart, so the session may not be registered yet
    // when UserPromptSubmit arrives. Lazy-register on first prompt so the
    // /optimize matcher in Python actually has a session record to flip.
    if !sessions_file.is_file() {
        if hook_event == "UserPromptSubmit" {
            // A resumed orchestrator's first prompt. Engage unless this is
            // a subagent-originated prompt (agent_id present) — a subagent
            // must not engage the workspace loop on its own. register_session
            // additionally refuses to engage when EVO_EXP_ID is set.
            let engage = !is_subagent_context(&stdin_buf);
            let _ = register_session(&run_dir, &sid, host, engage);
        } else {
            emit_ok();
        }
    }

    let marker = marker_exists(&run_dir, &sid);
    let opt_flag = optimize_flag_exists(&run_dir, &sid);
    if !should_handoff(&hook_event, marker, opt_flag) {
        emit_ok();
    }

    // Subagent fence: claude-code (and codex) inherit the parent's
    // CLAUDE_CODE_SESSION_ID env var when spawning subagents via the
    // Task tool. So when a subagent makes a tool call and PreToolUse
    // fires, this hook resolves to the parent's session_id — same as
    // the orchestrator. If we drain here, the directive's
    // additionalContext flows into the SUBAGENT's API call, not the
    // orchestrator's. The subagent finishes its narrow task, context
    // is discarded, the orchestrator never sees the directive.
    //
    // Discriminator: the hook payload's `agent_id` field. Claude-code
    // includes it (along with `agent_type`) in PreToolUse payloads
    // for subagent (Task tool) tool calls; the orchestrator's own
    // tool calls have it absent. Verified empirically.
    //
    // On subagent context: fast-exit. The queue stays pending until a
    // main-session hook (typically the orchestrator's next Stop or a
    // top-level Bash/Read call) consumes it and delivers as a new
    // user turn / additionalContext.
    if is_subagent_context(&stdin_buf) {
        emit_ok();
    }

    handoff_to_drain(&run_dir, &sid, host, &stdin_buf);
}


fn is_subagent_context(stdin_buf: &str) -> bool {
    // Claude-code includes `agent_id` (and `agent_type`) fields in hook
    // payloads triggered by subagent (Task tool) tool calls. The
    // orchestrator's own tool calls have these fields absent. This is
    // the canonical discriminator — verified empirically by dumping
    // every hook payload across a release-smoke run:
    //   - 20 PreToolUse with agent_id present: every one originated
    //     from a subagent (file edits in worktrees/exp_*/, evo run
    //     commands inside subagents)
    //   - 12 PreToolUse with agent_id absent: every one originated
    //     from the orchestrator (Skill, top-level Bash, Agent/Task
    //     spawn, evo ack)
    //
    // Earlier `transcript_path` check was wrong — claude-code passes
    // the MAIN session's transcript_path even for subagent tool calls.
    match find_json_string(stdin_buf, "agent_id") {
        Some(aid) => !aid.is_empty(),
        None => false,
    }
}

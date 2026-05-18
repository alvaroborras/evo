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

fn find_session_id(stdin_buf: &str) -> String {
    if let Some(sid) = find_json_string(stdin_buf, "session_id") {
        return sid;
    }
    for env_var in [
        "CLAUDE_CODE_SESSION_ID",
        "CODEX_THREAD_ID",
        "HERMES_SESSION_ID",
        "OPENCODE_SESSION_ID",
    ] {
        if let Ok(v) = env::var(env_var) {
            if !v.is_empty() {
                return v;
            }
        }
    }
    String::new()
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

fn detect_host_from_stdin(buf: &str) -> &'static str {
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
    format!("{:04}-{:02}-{:02}T{:02}:{:02}:{:02}Z", y, mo, d, hh, mm, ss)
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

fn register_session(run_dir: &Path, sid: &str, host: &str) -> io::Result<()> {
    let sessions_dir = run_dir.join("inject").join("sessions");
    fs::create_dir_all(&sessions_dir)?;
    let now = iso8601_utc_now();
    let payload = format!(
        r#"{{"schema_version":1,"session_id":"{}","host":"{}","pid":{},"registered_at":"{}","last_seen_at":"{}","exp_id":null,"parent_session_id":null}}"#,
        sid,
        host,
        process::id(),
        now,
        now
    );
    fs::write(sessions_dir.join(format!("{}.json", sid)), payload)
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

fn handoff_to_drain(run_dir: &Path, sid: &str, stdin_buf: &str) -> ! {
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

fn main() {
    let stdin_buf = read_stdin();

    let sid = find_session_id(&stdin_buf);
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
            let host = detect_host_from_stdin(&stdin_buf);
            let _ = register_session(&run_dir, &sid, host);
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
    }

    if !sessions_file.is_file() {
        emit_ok();
    }

    if hook_event != "SessionStart" {
        let marker = run_dir.join("inject").join("markers").join(format!("{}.flag", sid));
        if !marker.is_file() {
            emit_ok();
        }
    }

    handoff_to_drain(&run_dir, &sid, &stdin_buf);
}

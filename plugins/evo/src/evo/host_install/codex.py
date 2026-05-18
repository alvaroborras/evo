"""Codex install — enables `plugin_hooks` and the evo plugin in
config.toml. Codex has no `plugin install` CLI command; activation is
done by adding a `[plugins."<name>@<marketplace>"]` section."""

from __future__ import annotations

import argparse
import os
from pathlib import Path

from ._hook_drain import ensure_hook_drain_binary


_PLUGIN_KEY = '[plugins."evo@evo-hq"]'

_INSTALL_HINT = """\
Codex install (run `codex plugin marketplace add evo-hq/evo` first if
not already done). This command:
  - sets [features] plugin_hooks = true  (required for `evo direct`)
  - adds [plugins."evo@evo-hq"]          (enables the plugin)
in ~/.codex/config.toml.
"""


def _codex_base() -> Path:
    home_override = os.environ.get("CODEX_HOME")
    return Path(home_override) if home_override else Path.home() / ".codex"


def _marketplace_cache() -> Path:
    return _codex_base() / ".tmp" / "marketplaces" / "evo-hq"


def _toggle_plugin_hooks(enable: bool) -> tuple[bool, Path]:
    cfg = _codex_base() / "config.toml"
    if not cfg.exists():
        return False, cfg
    text = cfg.read_text()
    has_features = "[features]" in text
    has_pluginhooks_true = "plugin_hooks = true" in text
    has_pluginhooks_false = "plugin_hooks = false" in text

    if enable:
        if has_pluginhooks_true:
            return False, cfg
        if has_pluginhooks_false:
            text = text.replace("plugin_hooks = false", "plugin_hooks = true")
        elif has_features:
            text = text.replace("[features]", "[features]\nplugin_hooks = true", 1)
        else:
            text = text.rstrip() + "\n\n[features]\nplugin_hooks = true\n"
    else:
        if not has_pluginhooks_true:
            return False, cfg
        text = text.replace("plugin_hooks = true", "plugin_hooks = false")

    cfg.write_text(text)
    return True, cfg


def _enable_plugin(enable: bool) -> tuple[bool, Path]:
    """Add or remove the `[plugins."evo@evo-hq"]` section in
    config.toml. Codex 0.130+ uses owner-only marketplace names, so the
    plugin key is `evo@evo-hq` (not `evo@evo-hq-evo`)."""
    cfg = _codex_base() / "config.toml"
    if not cfg.exists():
        return False, cfg
    text = cfg.read_text()
    present = _PLUGIN_KEY in text

    if enable:
        if present:
            return False, cfg
        text = text.rstrip() + f"\n\n{_PLUGIN_KEY}\n"
    else:
        if not present:
            return False, cfg
        text = "\n".join(
            line for line in text.splitlines() if line.strip() != _PLUGIN_KEY.strip()
        ) + "\n"

    cfg.write_text(text)
    return True, cfg


def install(args: argparse.Namespace) -> int:
    print(_INSTALL_HINT)

    import shutil as _shutil
    if _shutil.which("codex") is None:
        print(
            "ERROR: `codex` binary not on PATH. Install Codex first:\n"
            "  npm install -g @openai/codex",
            file=__import__("sys").stderr,
        )
        return 2

    from_path = getattr(args, "from_path", None)
    trust_hooks = bool(getattr(args, "trust_hooks", False))
    force = bool(getattr(args, "force", False))

    # Drive `codex plugin marketplace add` automatically. Skip if:
    #   - --from-path is set (user is testing a local marketplace)
    #   - marketplace clone already exists and --force not set (avoids
    #     overwriting a tag-pinned install with unpinned default-branch
    #     content; e.g. release-smoke tests do their own tag-pinned
    #     marketplace add and would lose the pin if we re-added unpinned)
    mkt_cache = _marketplace_cache()
    if from_path:
        pass
    elif mkt_cache.exists() and not force:
        print(
            f"codex marketplace cache already at {mkt_cache}; "
            "skipping `codex plugin marketplace add` prereq (use --force to refresh)"
        )
    else:
        import subprocess as _sp
        version = getattr(args, "version", None)
        source = "evo-hq/evo"
        if version:
            import re as _re
            ref = f"v{version}" if _re.match(r"^\d+\.\d+\.\d+", version) else version
            source = f"{source}@{ref}"
        mkt_cmd = ["codex", "plugin", "marketplace", "add", source]
        print(f"$ {' '.join(mkt_cmd)}")
        _sp.call(mkt_cmd)

    # Ensure config.toml exists. On freshly-npm-installed codex (never run
    # interactively), the marketplace add above creates ~/.codex/ but may
    # not create config.toml until the first `codex` invocation. Write a
    # stub so the toggle/enable helpers below have something to edit.
    cfg = _codex_base() / "config.toml"
    if not cfg.exists():
        cfg.parent.mkdir(parents=True, exist_ok=True)
        cfg.write_text("# created by `evo install codex`\n")
        print(f"created stub {cfg}")

    # Replicate what codex's TUI `plugin/install` RPC does: copy the plugin
    # contents into `~/.codex/plugins/cache/<marketplace>/<plugin>/<ver>/`
    # and add `[plugins."<plugin>@<marketplace>"] enabled = true` to
    # config.toml. Without this, `codex plugin marketplace add` alone
    # leaves the plugin discoverable but inert — skills aren't in codex's
    # tool list, the hooks/hooks.json drain script never fires, and
    # `evo direct` directives go undelivered.
    #
    # The TUI uses an RPC method `plugin/install` (verified by inspecting
    # `codex app-server generate-json-schema`). We do not drive that RPC
    # because `codex app-server` exits early on missing bubblewrap inside
    # minimal containers (e2b sandboxes etc.). File-copy replicates the
    # same disk state and works anywhere.
    return _install_via_filecopy(from_path, trust_hooks=trust_hooks)


def _resolve_marketplace_json(from_path: str | None) -> Path | None:
    """Return the path to the marketplace.json file `plugin/install`
    needs. `from_path` (when set) can be either the marketplace root
    (contains `.claude-plugin/marketplace.json`) or a plugin dir
    (`<root>/plugins/evo/`). For the latter we walk up to the root.
    Falls back to the PyPI/GitHub cache otherwise.
    """
    if from_path:
        p = Path(from_path).resolve()
        # plugin-dir form: <root>/plugins/<name>
        if p.name and p.parent.name == "plugins":
            p = p.parent.parent
        mj = p / ".claude-plugin" / "marketplace.json"
        return mj if mj.exists() else None
    mj = _marketplace_cache() / ".claude-plugin" / "marketplace.json"
    return mj if mj.exists() else None


def _install_via_filecopy(from_path: str | None, *, trust_hooks: bool = False) -> int:
    """Mirror what codex's `plugin/install` RPC writes to disk:
      1. read marketplace name from `<root>/.claude-plugin/marketplace.json`
      2. read plugin version from `<plugin-root>/.codex-plugin/plugin.json`
      3. copy plugin contents to
         `~/.codex/plugins/cache/<marketplace>/<plugin>/<version>/`
      4. ensure `[plugins."<plugin>@<marketplace>"] enabled = true`
         is in config.toml

    If `trust_hooks` is True, additionally write
    `[hooks.state."<key>"] trusted_hash = "..."` entries that match what
    codex's TUI writes when the user trusts each hook via `/hooks`.
    Without trust, hooks remain in `untrusted` state and never fire —
    `evo direct` directives won't reach the agent.
    """
    import json as _json
    import shutil as _shutil
    import sys as _sys

    # Locate marketplace root + plugin root. `from_path` can be the
    # marketplace root (preferred) or a plugin dir (`<root>/plugins/<n>`).
    if from_path:
        p = Path(from_path).resolve()
        if p.name and p.parent.name == "plugins":
            mkt_root = p.parent.parent
        else:
            mkt_root = p
    else:
        mkt_root = _marketplace_cache()
    plugin_root = mkt_root / "plugins" / "evo"
    marketplace_json = mkt_root / ".claude-plugin" / "marketplace.json"
    codex_manifest = plugin_root / ".codex-plugin" / "plugin.json"

    for required, label in [
        (marketplace_json, "marketplace.json"),
        (codex_manifest, ".codex-plugin/plugin.json"),
    ]:
        if not required.exists():
            print(
                f"✗ missing {label}: {required}\n"
                "  PyPI/GitHub: run `codex plugin marketplace add evo-hq/evo`\n"
                "  Local mode:  pass `--from-path <marketplace-root>`",
                file=_sys.stderr,
            )
            return 2

    # Marketplace name: codex names a marketplace differently per source.
    #   - `codex plugin marketplace add evo-hq/evo` (PyPI/GitHub shorthand)
    #     → name = `evo-hq` (the owner)
    #   - `codex plugin marketplace add <local-dir>` (local)
    #     → name = marketplace.json's top-level `"name"` field (`evo-hq-evo`)
    # When --from-path is given we're in local mode; otherwise PyPI mode
    # and we use the cache dir's basename (which codex assigned).
    try:
        mkt = _json.loads(marketplace_json.read_text())
    except (OSError, _json.JSONDecodeError) as exc:
        print(f"✗ could not parse {marketplace_json}: {exc}", file=_sys.stderr)
        return 2
    if from_path:
        mkt_name = mkt.get("name") or "evo-hq-evo"
    else:
        # PyPI mode: cache dir is `~/.codex/.tmp/marketplaces/<owner>/`
        mkt_name = mkt_root.name  # e.g. "evo-hq"
    try:
        manifest = _json.loads(codex_manifest.read_text())
        version = manifest.get("version") or "0.0.0"
    except (OSError, _json.JSONDecodeError) as exc:
        print(f"✗ could not parse {codex_manifest}: {exc}", file=_sys.stderr)
        return 2

    cache_dst = (_codex_base() / "plugins" / "cache" / mkt_name / "evo" / version)
    if cache_dst.exists():
        print(f"removing previous install at {cache_dst}")
        _shutil.rmtree(cache_dst)
    cache_dst.parent.mkdir(parents=True, exist_ok=True)
    print(f"copying {plugin_root} → {cache_dst}")
    _shutil.copytree(plugin_root, cache_dst, symlinks=False,
                     ignore=_shutil.ignore_patterns(
                         ".git", ".venv", "__pycache__", "build", "dist",
                         ".pytest_cache", "*.egg-info"))

    # Stage the platform-native evo-hook-drain binary. hooks.json points
    # at <PLUGIN_ROOT>/bin/evo-hook-drain; without the binary every hook
    # fire would be a no-op. The cache_dst was just (re)created above
    # (rmtree if existed) so a fresh fetch is always correct here —
    # `force=False` (default) avoids re-fetch only when the file is
    # already at dest, which never happens since we just wiped.
    ensure_hook_drain_binary(cache_dst)

    # Update config.toml: ensure `[features] plugin_hooks = true`
    # (gates whether codex fires plugin hooks at all) AND
    # `[plugins."evo@<mkt>"] enabled = true` (activates the plugin).
    # Without plugin_hooks=true the hooks.json declaration is ignored
    # and `evo direct` directives never get drained.
    changed_h, cfg = _toggle_plugin_hooks(enable=True)
    if changed_h:
        print(f"updated {cfg}: enabled plugin_hooks")
    else:
        print(f"plugin_hooks already enabled in {cfg}")

    plugin_key = f'[plugins."evo@{mkt_name}"]'
    text = cfg.read_text() if cfg.exists() else ""
    if plugin_key not in text:
        text = text.rstrip() + f"\n\n{plugin_key}\nenabled = true\n"
        cfg.write_text(text)
        print(f"updated {cfg}: added {plugin_key} (enabled = true)")
    else:
        print(f"{plugin_key} already present in {cfg}")

    # Hooks are codex's most security-sensitive surface (run on every
    # tool call). Codex installs them in `untrusted` state — they're
    # registered but won't fire. Two paths to enable:
    #   - Interactive: user opens `codex` → `/hooks` → trusts each
    #   - Headless:    pass `--trust-hooks` to this command
    nested_hooks = cache_dst / "hooks" / "hooks.json"
    if not nested_hooks.exists():
        print(
            "\n(no hooks/hooks.json in plugin — skill-only install complete)"
        )
        return 0

    if trust_hooks:
        _trust_plugin_hooks(nested_hooks, plugin_id=f"evo@{mkt_name}", cfg=cfg)
    else:
        print(
            "\nPlugin hooks installed but UNTRUSTED. To enable mid-run "
            "directives (`evo direct`) on this codex install:\n"
            "  - Start `codex`, then `/hooks`, trust each evo hook, OR\n"
            "  - Re-run: evo install codex --trust-hooks  "
            "(skips the codex security review)"
        )
    return 0


def _hook_event_label(event_name: str) -> str | None:
    """Map hooks.json camel-case event name → codex's snake-case label.
    Returns None for unrecognized events (skipped in trust step)."""
    return {
        "PreToolUse": "pre_tool_use",
        "PostToolUse": "post_tool_use",
        "PermissionRequest": "permission_request",
        "PreCompact": "pre_compact",
        "PostCompact": "post_compact",
        "SessionStart": "session_start",
        "UserPromptSubmit": "user_prompt_submit",
        "Stop": "stop",
    }.get(event_name)


def _canonical_json(value):
    """Recursively sort dict keys (mirrors codex's `canonical_json`)."""
    if isinstance(value, dict):
        return {k: _canonical_json(value[k]) for k in sorted(value.keys())}
    if isinstance(value, list):
        return [_canonical_json(v) for v in value]
    return value


def _command_hook_hash(event_label: str, matcher: str | None,
                       command: str, timeout_sec: int = 600,
                       async_: bool = False, status_msg: str | None = None) -> str:
    """Reimplement codex's `command_hook_hash` so we can compute the
    `trusted_hash` value codex's TUI writes. Algorithm (from
    codex-rs/config/src/fingerprint.rs::version_for_toml):
      1. build NormalizedHookIdentity (event_name + flattened MatcherGroup)
      2. drop Option::None fields (TOML has no null)
      3. canonical JSON (sort keys recursively)
      4. compact serde_json::to_vec
      5. SHA256, prefixed with "sha256:"
    Verified empirically against codex's `hooks/list` output across all
    3 hook events (pre_tool_use, session_start, user_prompt_submit).
    """
    import hashlib as _hashlib
    import json as _json
    identity: dict = {"event_name": event_label}
    if matcher is not None:
        identity["matcher"] = matcher
    handler: dict = {"type": "command", "command": command, "async": async_}
    if timeout_sec is not None:
        handler["timeout"] = timeout_sec
    if status_msg is not None:
        handler["statusMessage"] = status_msg
    identity["hooks"] = [handler]
    serialized = _json.dumps(_canonical_json(identity),
                             separators=(",", ":"), ensure_ascii=False).encode()
    return "sha256:" + _hashlib.sha256(serialized).hexdigest()


def _trust_plugin_hooks(hooks_json_path: Path, plugin_id: str, cfg: Path) -> None:
    """Write `[hooks.state."<key>"] trusted_hash = "..."` entries to
    config.toml — same effect as `codex` → `/hooks` → Trust each.

    The key shape codex uses (verified via `hooks/list` RPC):
        <plugin_id>:hooks/hooks.json:<event_label>:<group_idx>:<handler_idx>
    """
    import json as _json
    import sys as _sys
    try:
        hooks_file = _json.loads(hooks_json_path.read_text())
    except (OSError, _json.JSONDecodeError) as exc:
        print(f"✗ could not parse {hooks_json_path}: {exc}", file=_sys.stderr)
        return

    events = hooks_file.get("hooks", {})
    lines: list[str] = []
    for event_name, groups in events.items():
        event_label = _hook_event_label(event_name)
        if event_label is None:
            continue
        for group_idx, group in enumerate(groups or []):
            matcher = group.get("matcher")
            for handler_idx, handler in enumerate(group.get("hooks", []) or []):
                if handler.get("type") != "command":
                    continue
                cmd = handler.get("command")
                if not cmd:
                    continue
                timeout = handler.get("timeout")
                if timeout is None:
                    timeout = 600  # codex's default after normalization
                trusted_hash = _command_hook_hash(
                    event_label, matcher, cmd,
                    timeout_sec=timeout,
                    async_=bool(handler.get("async", False)),
                    status_msg=handler.get("statusMessage"),
                )
                key = (f'{plugin_id}:hooks/hooks.json:'
                       f'{event_label}:{group_idx}:{handler_idx}')
                lines.append(
                    f'[hooks.state."{key}"]\ntrusted_hash = "{trusted_hash}"'
                )

    if not lines:
        print("(no command hooks to trust)")
        return

    block = "\n\n".join(lines) + "\n"
    text = cfg.read_text() if cfg.exists() else ""
    # Replace existing [hooks.state."..."] blocks for this plugin so
    # repeated --trust-hooks calls don't accumulate stale entries.
    import re as _re
    pat = _re.compile(
        rf'\[hooks\.state\."{_re.escape(plugin_id)}:[^"]+"\]\n'
        rf'trusted_hash = "sha256:[a-f0-9]+"\s*',
        _re.MULTILINE,
    )
    text = pat.sub("", text).rstrip() + "\n\n" + block
    cfg.write_text(text)
    print(f"updated {cfg}: trusted {len(lines)} hooks for {plugin_id}")


def _install_via_rpc(from_path: str | None) -> int:
    """Send `initialize` + `plugin/install` to `codex app-server` via
    stdio JSON-RPC. Returns 0 on success, 2 on failure.
    """
    import json as _json
    import subprocess as _sp
    import sys as _sys

    mj = _resolve_marketplace_json(from_path)
    if mj is None:
        if from_path:
            print(
                f"✗ marketplace.json not found at {from_path} (looked for "
                f"<root>/.claude-plugin/marketplace.json)",
                file=_sys.stderr,
            )
        else:
            print(
                "✗ no marketplace cache; run "
                "`codex plugin marketplace add evo-hq/evo` first, or pass "
                "`--from-path <marketplace-root>` for local source",
                file=_sys.stderr,
            )
        return 2

    init = _json.dumps({
        "jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {"clientInfo": {"name": "evo-install", "version": "0.1"}},
    })
    install = _json.dumps({
        "jsonrpc": "2.0", "id": 2, "method": "plugin/install",
        "params": {"pluginName": "evo", "marketplacePath": str(mj)},
    })
    input_str = init + "\n" + install + "\n"

    print(f"calling codex plugin/install with marketplacePath={mj}")
    try:
        proc = _sp.run(
            ["codex", "app-server"],
            input=input_str,
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (FileNotFoundError, _sp.TimeoutExpired) as e:
        print(f"✗ codex app-server invocation failed: {e}", file=_sys.stderr)
        return 2

    # Diagnostic: surface the raw RPC exchange so failures are debuggable.
    print(f"codex app-server exit={proc.returncode}")
    if proc.stdout:
        print(f"--- stdout ({len(proc.stdout)} bytes) ---")
        print(proc.stdout[:2000])
    if proc.stderr:
        print(f"--- stderr ({len(proc.stderr)} bytes) ---")
        print(proc.stderr[:2000])

    # Parse newline-delimited JSON-RPC responses; surface any error.
    saw_id2 = False
    failed = False
    for line in proc.stdout.splitlines():
        line = line.strip()
        if not line or not line.startswith("{"):
            continue
        try:
            msg = _json.loads(line)
        except _json.JSONDecodeError:
            continue
        if msg.get("id") == 2:
            saw_id2 = True
            if "error" in msg:
                print(f"✗ plugin/install failed: {msg['error']}", file=_sys.stderr)
                failed = True
            else:
                print(f"✓ plugin/install succeeded: {msg.get('result', {})}")
    if not saw_id2:
        print(
            "✗ plugin/install RPC: no response with id=2 — codex app-server "
            "may have exited before processing, or output got truncated",
            file=_sys.stderr,
        )
        return 2
    if failed:
        return 2
    return 0


def uninstall(args: argparse.Namespace) -> int:
    changed_h, cfg = _toggle_plugin_hooks(enable=False)
    if changed_h:
        print(f"updated {cfg}: disabled plugin_hooks")
    changed_p, _ = _enable_plugin(enable=False)
    if changed_p:
        print(f"updated {cfg}: removed {_PLUGIN_KEY}")
    print("To remove the marketplace itself: `codex plugin marketplace remove evo-hq`")
    return 0


def doctor(args: argparse.Namespace) -> int:
    cfg = _codex_base() / "config.toml"
    cache = _marketplace_cache()

    rc = 0
    cfg_text = cfg.read_text() if cfg.exists() else ""

    if "plugin_hooks = true" in cfg_text:
        print(f"✓ plugin_hooks = true in {cfg}")
    else:
        print(f"✗ plugin_hooks not enabled in {cfg}")
        print("  Run: evo install codex")
        rc = 1

    if _PLUGIN_KEY in cfg_text:
        print(f"✓ {_PLUGIN_KEY} in {cfg}")
    else:
        print(f"✗ {_PLUGIN_KEY} not in {cfg}")
        print("  Run: evo install codex")
        rc = 1

    if cache.exists():
        print(f"✓ evo marketplace cached at {cache}")
    else:
        print(f"✗ no marketplace cache at {cache}")
        print("  Run: codex plugin marketplace add evo-hq/evo")
        rc = 1
    return rc

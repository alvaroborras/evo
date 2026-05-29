"""Tests for openclaw + pi steering (shared factory.ts).

Both wrap the same `factory.ts` over pi's ExtensionAPI. Verified hooks
(against earendil-works/pi extensions.md):
  - session_start — observer.
  - before_provider_request — can replace payload; used for directive
    injection AND auto-arming optimize_mode by scanning user messages.
  - tool_call — fires pre-tool; returns `{block: true, reason}` to block.
  - turn_end — observer return is ignored, but the handler calls the
    top-level `pi.sendUserMessage(text, {deliverAs: "followUp"})` to
    queue an autonomous-continuation message. Per pi docs that message
    always triggers a turn, so the orchestrator keeps driving /optimize
    autonomously. (Distinct from `ctx.sendUserMessage`, which is
    deadlock-prone in event handlers.)

Run: pytest tests/unit/test_openclaw_pi_steering.py -v
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
FACTORY_TS = REPO_ROOT / "plugins" / "evo" / "src" / "evo" / "openclaw_plugin" / "factory.ts"
BUN = shutil.which("bun")


def _make_workspace(root: Path) -> Path:
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.email", "t@evo"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.name", "T"], cwd=root, check=True)
    subprocess.run(["git", "config", "commit.gpgsign", "false"], cwd=root, check=True)
    (root / "README.md").write_text("x\n")
    subprocess.run(["git", "add", "."], cwd=root, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=root, check=True)
    sys.path.insert(0, str(REPO_ROOT / "plugins" / "evo" / "src"))
    from evo.core import init_workspace
    init_workspace(
        root, target="agent.py", benchmark="python bench.py",
        metric="max", gate=None,
    )
    return next(iter((root / ".evo").glob("run_*")))


@unittest.skipIf(BUN is None, "bun runtime not available")
class TestOpenclawPiDenyGate(unittest.TestCase):
    """Drive the shared makeRegister factory through a mock ExtensionAPI
    and verify the tool_call hook blocks denied tools under optimize_mode."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name).resolve()
        self.run_dir = _make_workspace(self.root)

    def tearDown(self):
        self._tmp.cleanup()

    def _run_factory(self, host: str, body: str) -> dict:
        # Drive the factory in `cwd=self.root` so deriveSessionId picks
        # up the workspace path.
        script = textwrap.dedent(f"""
            import {{ makeRegister }} from "{FACTORY_TS}"
            process.chdir("{self.root}")
            const handlers: Record<string, any> = {{}}
            const api = {{
                on(name: string, h: any) {{ handlers[name] = h }},
            }}
            const register = makeRegister("{host}")
            register(api)
        """).strip() + "\n" + body
        with tempfile.NamedTemporaryFile(mode="w", suffix=".ts", delete=False) as fp:
            fp.write(script)
            script_path = fp.name
        try:
            r = subprocess.run(
                [BUN, "run", script_path], capture_output=True, text=True, timeout=20,
            )
        finally:
            Path(script_path).unlink(missing_ok=True)
        self.assertEqual(r.returncode, 0,
                         f"bun failed: stderr={r.stderr!r}\nstdout={r.stdout!r}")
        return json.loads(r.stdout)

    def test_tool_call_blocks_edit_in_optimize_mode(self):
        result = self._run_factory("pi", textwrap.dedent("""
            // Fire session_start to register, then arm optimize_mode,
            // then fire tool_call and capture the return.
            await handlers.session_start({}, {})
            const fs = await import("fs")
            const path = await import("path")
            const cryp = await import("crypto")
            const hash = cryp.createHash("sha256").update(process.cwd()).digest("hex").slice(0, 12)
            const sid = `pi-${hash}`
            const sfile = path.join(process.cwd(), ".evo", "run_0000", "inject", "sessions", `${sid}.json`)
            const rec = JSON.parse(fs.readFileSync(sfile, "utf8"))
            rec.optimize_mode = true
            rec.subagents_only = true  // deny-gate is opt-in
            fs.writeFileSync(sfile, JSON.stringify(rec))

            const ret = await handlers.tool_call(
                { toolName: "edit", input: { file_path: "/tmp/x" } },
                {}
            )
            process.stdout.write(JSON.stringify({
                blocked: ret?.block === true,
                reason_has_policy: typeof ret?.reason === "string" && ret.reason.indexOf("EVO POLICY") >= 0,
            }))
        """))
        self.assertTrue(result["blocked"], "tool_call must return {block: true}")
        self.assertTrue(result["reason_has_policy"],
                        "block reason must contain EVO POLICY banner")

    def test_tool_call_allows_read(self):
        result = self._run_factory("pi", textwrap.dedent("""
            await handlers.session_start({}, {})
            const fs = await import("fs")
            const path = await import("path")
            const cryp = await import("crypto")
            const hash = cryp.createHash("sha256").update(process.cwd()).digest("hex").slice(0, 12)
            const sid = `pi-${hash}`
            const sfile = path.join(process.cwd(), ".evo", "run_0000", "inject", "sessions", `${sid}.json`)
            const rec = JSON.parse(fs.readFileSync(sfile, "utf8"))
            rec.optimize_mode = true
            fs.writeFileSync(sfile, JSON.stringify(rec))

            const ret = await handlers.tool_call(
                { toolName: "read_file", input: { file_path: "/tmp/x" } },
                {}
            )
            process.stdout.write(JSON.stringify({
                blocked: ret?.block === true,
                ret_is_undefined: ret === undefined,
            }))
        """))
        self.assertFalse(result["blocked"], "read_file must not block")

    def test_tool_call_alternating_cadence(self):
        result = self._run_factory("pi", textwrap.dedent("""
            await handlers.session_start({}, {})
            const fs = await import("fs")
            const path = await import("path")
            const cryp = await import("crypto")
            const hash = cryp.createHash("sha256").update(process.cwd()).digest("hex").slice(0, 12)
            const sid = `pi-${hash}`
            const sfile = path.join(process.cwd(), ".evo", "run_0000", "inject", "sessions", `${sid}.json`)
            const rec = JSON.parse(fs.readFileSync(sfile, "utf8"))
            rec.optimize_mode = true
            rec.subagents_only = true  // deny-gate is opt-in
            fs.writeFileSync(sfile, JSON.stringify(rec))

            const outcomes: boolean[] = []
            for (let i = 0; i < 5; i++) {
                const ret = await handlers.tool_call(
                    { toolName: "edit", input: { file_path: "/tmp/x" } },
                    {}
                )
                outcomes.push(ret?.block === true)
            }
            process.stdout.write(JSON.stringify(outcomes))
        """))
        # Alternating cadence: #1, #3, #5 block; #2, #4 pass.
        self.assertEqual(result, [True, False, True, False, True])

    def test_tool_call_not_blocked_outside_optimize_mode(self):
        result = self._run_factory("openclaw", textwrap.dedent("""
            await handlers.session_start({}, {})
            // optimize_mode is NOT armed for this session.
            const ret = await handlers.tool_call(
                { toolName: "edit", input: { file_path: "/tmp/x" } },
                {}
            )
            process.stdout.write(JSON.stringify({
                blocked: ret?.block === true,
            }))
        """))
        self.assertFalse(result["blocked"])

    def test_optimize_prompt_arms_mode_via_before_provider_request(self):
        result = self._run_factory("pi", textwrap.dedent("""
            await handlers.session_start({}, {})
            // Simulate openai-style payload with /optimize as the user msg.
            await handlers.before_provider_request({
                payload: {
                    input: [
                        { role: "user", content: [{ type: "input_text", text: "/optimize fix the thing" }] }
                    ],
                },
            }, {})
            const fs = await import("fs")
            const path = await import("path")
            const cryp = await import("crypto")
            const hash = cryp.createHash("sha256").update(process.cwd()).digest("hex").slice(0, 12)
            const sid = `pi-${hash}`
            const sfile = path.join(process.cwd(), ".evo", "run_0000", "inject", "sessions", `${sid}.json`)
            const rec = JSON.parse(fs.readFileSync(sfile, "utf8"))
            process.stdout.write(JSON.stringify({
                optimize_mode: rec.optimize_mode === true,
            }))
        """))
        self.assertTrue(result["optimize_mode"],
                        "/optimize in user message must auto-arm optimize_mode")

    def test_turn_end_fires_stop_nudge_via_sendUserMessage(self):
        """When optimize_mode is on, turn_end should call
        pi.sendUserMessage(text, {deliverAs: "followUp"}) with the EVO
        LOOP banner. This is pi's autonomous-continuation primitive."""
        result = self._run_factory("pi", textwrap.dedent("""
            const calls: any[] = []
            api.sendUserMessage = (content: any, options: any) => {
                calls.push({ content, options })
            }
            await handlers.session_start({}, {})
            const fs = await import("fs")
            const path = await import("path")
            const cryp = await import("crypto")
            const hash = cryp.createHash("sha256").update(process.cwd()).digest("hex").slice(0, 12)
            const sid = `pi-${hash}`
            const sfile = path.join(process.cwd(), ".evo", "run_0000", "inject", "sessions", `${sid}.json`)
            const rec = JSON.parse(fs.readFileSync(sfile, "utf8"))
            rec.optimize_mode = true
            rec.autonomous = true
            fs.writeFileSync(sfile, JSON.stringify(rec))

            await handlers.turn_end({ turnIndex: 0 }, {})

            process.stdout.write(JSON.stringify({
                num_calls: calls.length,
                content_has_loop: typeof calls[0]?.content === "string" &&
                                  calls[0].content.indexOf("EVO LOOP") >= 0,
                deliver_as: calls[0]?.options?.deliverAs,
            }))
        """))
        self.assertEqual(result["num_calls"], 1, "turn_end must call sendUserMessage once")
        self.assertTrue(result["content_has_loop"], "content must include EVO LOOP")
        self.assertEqual(result["deliver_as"], "followUp",
                         "must use deliverAs:followUp for safe continuation")

    def test_turn_end_no_nudge_outside_optimize_mode(self):
        result = self._run_factory("pi", textwrap.dedent("""
            const calls: any[] = []
            api.sendUserMessage = (content: any, options: any) => {
                calls.push({ content, options })
            }
            await handlers.session_start({}, {})
            // optimize_mode NOT set
            await handlers.turn_end({ turnIndex: 0 }, {})
            process.stdout.write(JSON.stringify({ num_calls: calls.length }))
        """))
        self.assertEqual(result["num_calls"], 0,
                         "casual pi session must not be force-continued")

    def test_tool_call_observes_evo_autonomous_on_then_nudges(self):
        """End-to-end opt-in: optimize_mode on but autonomous OFF → turn_end
        does NOT nudge; after the tool_call hook OBSERVES `evo autonomous on`
        (the in-process arm path, since pi has no session env var), turn_end
        nudges. Proves command-observation arming, not prompt prose."""
        result = self._run_factory("pi", textwrap.dedent("""
            const calls: any[] = []
            api.sendUserMessage = (content: any, options: any) => {
                calls.push({ content, options })
            }
            await handlers.session_start({}, {})
            const fs = await import("fs")
            const path = await import("path")
            const cryp = await import("crypto")
            const hash = cryp.createHash("sha256").update(process.cwd()).digest("hex").slice(0, 12)
            const sid = `pi-${hash}`
            const sfile = path.join(process.cwd(), ".evo", "run_0000", "inject", "sessions", `${sid}.json`)
            let rec = JSON.parse(fs.readFileSync(sfile, "utf8"))
            rec.optimize_mode = true
            fs.writeFileSync(sfile, JSON.stringify(rec))

            // turn_end WITHOUT autonomous → no nudge (opt-in default).
            await handlers.turn_end({ turnIndex: 0 }, {})
            const calls_before = calls.length

            // tool_call OBSERVES `evo autonomous on` → arms autonomous in-process.
            await handlers.tool_call(
                { toolName: "bash", input: { command: "evo autonomous on" } }, {}
            )
            rec = JSON.parse(fs.readFileSync(sfile, "utf8"))
            const armed = rec.autonomous === true

            // turn_end WITH autonomous → nudge fires.
            await handlers.turn_end({ turnIndex: 1 }, {})
            process.stdout.write(JSON.stringify({
                calls_before, armed, calls_after: calls.length,
            }))
        """))
        self.assertEqual(result["calls_before"], 0,
                         "no nudge before autonomous is armed (opt-in default)")
        self.assertTrue(result["armed"],
                        "tool_call observing `evo autonomous on` must arm autonomous")
        self.assertEqual(result["calls_after"], 1,
                         "nudge fires once autonomous is armed")

    def test_tool_call_observes_evo_subagents_only_on_then_denies(self):
        """End-to-end opt-in: optimize_mode on but subagents_only OFF →
        an orchestrator edit is ALLOWED; after the tool_call hook OBSERVES
        `evo subagents-only on` (the in-process arm path, since pi has no
        session env var), the orchestrator edit is DENIED. Proves
        command-observation arming, not prompt prose."""
        result = self._run_factory("pi", textwrap.dedent("""
            await handlers.session_start({}, {})
            const fs = await import("fs")
            const path = await import("path")
            const cryp = await import("crypto")
            const hash = cryp.createHash("sha256").update(process.cwd()).digest("hex").slice(0, 12)
            const sid = `pi-${hash}`
            const sfile = path.join(process.cwd(), ".evo", "run_0000", "inject", "sessions", `${sid}.json`)
            let rec = JSON.parse(fs.readFileSync(sfile, "utf8"))
            rec.optimize_mode = true
            fs.writeFileSync(sfile, JSON.stringify(rec))

            // Edit BEFORE subagents-only armed → allowed (opt-in default).
            const before = await handlers.tool_call(
                { toolName: "edit", input: { file_path: "/tmp/x" } }, {}
            )

            // tool_call OBSERVES `evo subagents-only on` → arms in-process.
            await handlers.tool_call(
                { toolName: "bash", input: { command: "evo subagents-only on" } }, {}
            )
            rec = JSON.parse(fs.readFileSync(sfile, "utf8"))
            const armed = rec.subagents_only === true

            // Edit WITH subagents-only → denied (#1 violation, odd → block).
            const after = await handlers.tool_call(
                { toolName: "edit", input: { file_path: "/tmp/x" } }, {}
            )
            process.stdout.write(JSON.stringify({
                blocked_before: before?.block === true,
                armed,
                blocked_after: after?.block === true,
            }))
        """))
        self.assertFalse(result["blocked_before"],
                         "orchestrator edit allowed before subagents-only armed")
        self.assertTrue(result["armed"],
                        "tool_call observing `evo subagents-only on` must arm the flag")
        self.assertTrue(result["blocked_after"],
                        "orchestrator edit denied once subagents-only is armed")

    def test_directive_replay_stops_after_ack(self):
        """The replay cache re-appends a drained directive to every
        before_provider_request — but must STOP once the agent acks it
        (else it re-injects + the agent re-acks all session, as seen live:
        pi acked count=79). Fire the hook: directive appears, re-appears on
        the next call (pre-ack replay), then DISAPPEARS once acked."""
        result = self._run_factory("pi", textwrap.dedent("""
            const fs = await import("fs")
            const path = await import("path")
            const cryp = await import("crypto")
            await handlers.session_start({}, {})
            const hash = cryp.createHash("sha256").update(process.cwd()).digest("hex").slice(0, 12)
            const sid = `pi-${hash}`
            const inj = path.join(process.cwd(), ".evo", "run_0000", "inject")
            // arm optimize_mode + autonomous
            const sfile = path.join(inj, "sessions", `${sid}.json`)
            const rec = JSON.parse(fs.readFileSync(sfile, "utf8"))
            rec.optimize_mode = true; rec.autonomous = true
            fs.writeFileSync(sfile, JSON.stringify(rec))
            // queue a workspace directive (raw event; drainSession wraps it)
            const eid = "01TESTDIRECTIVE00000000001"
            fs.mkdirSync(path.join(inj, "events"), { recursive: true })
            fs.appendFileSync(path.join(inj, "events", "workspace.jsonl"),
                JSON.stringify({schema_version:1, id:eid, ts:"2026-01-01T00:00:00+00:00", text:"USE HASHMAP"}) + "\\n")
            const mkEvent = () => ({ payload: { input: [{ role:"user", content:"go" }] } })
            const sawDirective = (ev) => JSON.stringify(ev.payload).indexOf("USE HASHMAP") >= 0

            const e1 = mkEvent(); await handlers.before_provider_request(e1, {})
            const first = sawDirective(e1)            // drained + appended
            const e2 = mkEvent(); await handlers.before_provider_request(e2, {})
            const second = sawDirective(e2)           // re-replayed (pre-ack)

            // agent acks the directive
            fs.mkdirSync(path.join(inj, "acks"), { recursive: true })
            fs.writeFileSync(path.join(inj, "acks", `${eid}.json`), JSON.stringify({event_id:eid}))

            const e3 = mkEvent(); await handlers.before_provider_request(e3, {})
            const third = sawDirective(e3)            // dropped after ack
            process.stdout.write(JSON.stringify({ first, second, third }))
        """))
        self.assertTrue(result["first"], "directive must be delivered on first call")
        self.assertTrue(result["second"], "directive re-replays until acked (pre-ack)")
        self.assertFalse(result["third"], "directive must STOP re-injecting once acked")

    def test_tool_call_observes_evo_autonomous_off_disarms(self):
        """`evo autonomous off` observed via tool_call disarms the loop."""
        result = self._run_factory("pi", textwrap.dedent("""
            await handlers.session_start({}, {})
            const fs = await import("fs")
            const path = await import("path")
            const cryp = await import("crypto")
            const hash = cryp.createHash("sha256").update(process.cwd()).digest("hex").slice(0, 12)
            const sid = `pi-${hash}`
            const sfile = path.join(process.cwd(), ".evo", "run_0000", "inject", "sessions", `${sid}.json`)
            let rec = JSON.parse(fs.readFileSync(sfile, "utf8"))
            rec.optimize_mode = true; rec.autonomous = true
            fs.writeFileSync(sfile, JSON.stringify(rec))
            await handlers.tool_call(
                { toolName: "bash", input: { command: "evo autonomous off" } }, {}
            )
            rec = JSON.parse(fs.readFileSync(sfile, "utf8"))
            process.stdout.write(JSON.stringify({ autonomous: rec.autonomous }))
        """))
        self.assertFalse(result["autonomous"],
                         "observing `evo autonomous off` must disarm autonomous")

    def test_anthropic_payload_with_string_content_arms_mode(self):
        """Regression for the round-1 P2: openai-style `input` items
        with a plain-string `content` field (not parts array) must also
        be matched by extractLatestUserText."""
        result = self._run_factory("pi", textwrap.dedent("""
            await handlers.session_start({}, {})
            // Plain string content (not a parts array)
            await handlers.before_provider_request({
                payload: {
                    input: [
                        { role: "user", content: "/optimize plain string" }
                    ],
                },
            }, {})
            const fs = await import("fs")
            const path = await import("path")
            const cryp = await import("crypto")
            const hash = cryp.createHash("sha256").update(process.cwd()).digest("hex").slice(0, 12)
            const sid = `pi-${hash}`
            const sfile = path.join(process.cwd(), ".evo", "run_0000", "inject", "sessions", `${sid}.json`)
            const rec = JSON.parse(fs.readFileSync(sfile, "utf8"))
            process.stdout.write(JSON.stringify({ optimize_mode: rec.optimize_mode === true }))
        """))
        self.assertTrue(result["optimize_mode"],
                        "input item with string content must reach the matcher")

    def test_subagent_env_var_records_exp_id(self):
        """When EVO_EXP_ID is set in the env (subagent dispatch sets it),
        the subagent gets a DISTINCT sid (cwd + exp_id in the hash seed)
        and registerSession records exp_id on that subagent's own record.
        The subagent fence (`sess.exp_id` checks) then fires correctly."""
        result = self._run_factory("pi", textwrap.dedent("""
            process.env.EVO_EXP_ID = "exp_0042"
            await handlers.session_start({}, {})
            const fs = await import("fs")
            const path = await import("path")
            const cryp = await import("crypto")
            // Subagent sid uses cwd|exp_id as the hash seed.
            const seed = process.cwd() + "|" + "exp_0042"
            const hash = cryp.createHash("sha256").update(seed).digest("hex").slice(0, 12)
            const sid = `pi-${hash}`
            const sfile = path.join(process.cwd(), ".evo", "run_0000", "inject", "sessions", `${sid}.json`)
            const rec = JSON.parse(fs.readFileSync(sfile, "utf8"))
            process.stdout.write(JSON.stringify({ exp_id: rec.exp_id, host: rec.host }))
        """))
        self.assertEqual(result["exp_id"], "exp_0042",
                         "subagent record must carry exp_id")
        self.assertEqual(result["host"], "pi")

    def test_parent_and_subagent_get_distinct_session_records(self):
        """The architecturally correct fix for cwd-collision: parent and
        subagent derive DIFFERENT sids (parent uses cwd alone, subagent
        uses cwd|exp_id). Each gets its own session record. Parent's
        record stays exp_id=null so its steering still fires. Subagent's
        record has exp_id set so the fence exempts it."""
        result = self._run_factory("pi", textwrap.dedent("""
            const fs = await import("fs")
            const path = await import("path")
            const cryp = await import("crypto")

            // Phase 1: parent registers without EVO_EXP_ID.
            delete process.env.EVO_EXP_ID
            await handlers.session_start({}, {})
            const parent_hash = cryp.createHash("sha256").update(process.cwd()).digest("hex").slice(0, 12)
            const parent_sid = `pi-${parent_hash}`
            const parent_sfile = path.join(process.cwd(), ".evo", "run_0000", "inject", "sessions", `${parent_sid}.json`)
            const parent_rec = JSON.parse(fs.readFileSync(parent_sfile, "utf8"))

            // Phase 2: subagent with EVO_EXP_ID. Same cwd, DIFFERENT sid.
            process.env.EVO_EXP_ID = "exp_sub_001"
            await handlers.session_start({}, {})
            const sub_seed = process.cwd() + "|exp_sub_001"
            const sub_hash = cryp.createHash("sha256").update(sub_seed).digest("hex").slice(0, 12)
            const sub_sid = `pi-${sub_hash}`
            const sub_sfile = path.join(process.cwd(), ".evo", "run_0000", "inject", "sessions", `${sub_sid}.json`)
            const sub_rec = JSON.parse(fs.readFileSync(sub_sfile, "utf8"))

            // Re-read parent to confirm it wasn't mutated.
            const parent_after = JSON.parse(fs.readFileSync(parent_sfile, "utf8"))

            process.stdout.write(JSON.stringify({
                parent_sid: parent_sid,
                sub_sid: sub_sid,
                sids_differ: parent_sid !== sub_sid,
                parent_exp_id_before: parent_rec.exp_id,
                parent_exp_id_after: parent_after.exp_id,
                sub_exp_id: sub_rec.exp_id,
            }))
        """))
        self.assertTrue(result["sids_differ"],
                        "parent and subagent must derive different sids")
        self.assertIsNone(result["parent_exp_id_before"],
                          "parent record starts exp_id=null")
        self.assertIsNone(result["parent_exp_id_after"],
                          "parent record must STAY exp_id=null after "
                          "subagent registers — otherwise parent's "
                          "steering would be disabled")
        self.assertEqual(result["sub_exp_id"], "exp_sub_001",
                         "subagent's own record carries exp_id")

    def test_parent_steering_still_fires_after_subagent_registered(self):
        """End-to-end check: orchestrator-in-optimize_mode + subagent
        registers in the same cwd → parent's tool_call must STILL deny
        edit (the previous merge-fix would have broken this)."""
        result = self._run_factory("pi", textwrap.dedent("""
            const fs = await import("fs")
            const path = await import("path")
            const cryp = await import("crypto")

            // Parent registers + arms optimize_mode.
            delete process.env.EVO_EXP_ID
            await handlers.session_start({}, {})
            const phash = cryp.createHash("sha256").update(process.cwd()).digest("hex").slice(0, 12)
            const psid = `pi-${phash}`
            const pfile = path.join(process.cwd(), ".evo", "run_0000", "inject", "sessions", `${psid}.json`)
            let prec = JSON.parse(fs.readFileSync(pfile, "utf8"))
            prec.optimize_mode = true
            prec.subagents_only = true  // deny-gate is opt-in
            fs.writeFileSync(pfile, JSON.stringify(prec))

            // Subagent registers with EVO_EXP_ID.
            process.env.EVO_EXP_ID = "exp_sub_002"
            await handlers.session_start({}, {})

            // Reset env back so the parent's tool_call hits the parent.
            delete process.env.EVO_EXP_ID

            const ret = await handlers.tool_call(
                { toolName: "edit", input: {} }, {}
            )
            process.stdout.write(JSON.stringify({ blocked: ret?.block === true }))
        """))
        self.assertTrue(result["blocked"],
                        "parent's tool_call must STILL deny after subagent "
                        "registers — distinct-sid design keeps parent's "
                        "exp_id=null so the policy gate fires")

    def test_subagent_tool_call_not_blocked_even_in_optimize_mode(self):
        """End-to-end: subagent process (EVO_EXP_ID set) registers its
        OWN session record (distinct sid). The exp_id on that record
        exempts the subagent's tool_call even if optimize_mode is
        on for that record."""
        result = self._run_factory("pi", textwrap.dedent("""
            process.env.EVO_EXP_ID = "exp_sub"
            await handlers.session_start({}, {})
            const fs = await import("fs")
            const path = await import("path")
            const cryp = await import("crypto")
            // Subagent sid uses cwd|exp_id as the seed.
            const seed = process.cwd() + "|exp_sub"
            const hash = cryp.createHash("sha256").update(seed).digest("hex").slice(0, 12)
            const sid = `pi-${hash}`
            const sfile = path.join(process.cwd(), ".evo", "run_0000", "inject", "sessions", `${sid}.json`)
            const rec = JSON.parse(fs.readFileSync(sfile, "utf8"))
            // Even with optimize_mode flipped on, the subagent must pass.
            rec.optimize_mode = true
            fs.writeFileSync(sfile, JSON.stringify(rec))

            const ret = await handlers.tool_call(
                { toolName: "edit", input: {} }, {}
            )
            process.stdout.write(JSON.stringify({ blocked: ret?.block === true }))
        """))
        self.assertFalse(result["blocked"],
                         "subagent (exp_id set) must not be policy-blocked")

    def test_host_string_recorded_correctly(self):
        """Sanity: openclaw and pi each record their own host string."""
        for host in ("pi", "openclaw"):
            with self.subTest(host=host):
                # Reset workspace for each subtest.
                self.tearDown()
                self.setUp()
                result = self._run_factory(host, textwrap.dedent("""
                    await handlers.session_start({}, {})
                    const fs = await import("fs")
                    const path = await import("path")
                    const cryp = await import("crypto")
                    const hash = cryp.createHash("sha256").update(process.cwd()).digest("hex").slice(0, 12)
                """ + f'                    const sid = `{host}-${{hash}}`\n' + """
                    const sfile = path.join(process.cwd(), ".evo", "run_0000", "inject", "sessions", `${sid}.json`)
                    const rec = JSON.parse(fs.readFileSync(sfile, "utf8"))
                    process.stdout.write(JSON.stringify({ host: rec.host }))
                """))
                self.assertEqual(result["host"], host,
                                 f"session record must tag host={host!r}, got {result!r}")


if __name__ == "__main__":
    unittest.main()

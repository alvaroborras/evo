/*
 * evo-optimize.js — Claude Code dynamic-workflow driver for the /evo:optimize round loop.
 *
 * This is the CODE form of plugins/evo/skills/optimize/SKILL.md "The Loop". It is an
 * opt-in, Claude-Code-only driver; the prose skill remains the canonical, host-agnostic
 * default. The workflow encodes the loop CONTROL: while/stall, mandatory scan + cross-history
 * axis check, research escalation (ideators on stall / every ~5 commits), brief + diversity,
 * fan-out + verify, collect + frontier-select. All domain work goes through the `evo` CLI inside
 * agents — the script itself never touches the filesystem/shell.
 *
 * Treat this as a TEMPLATE: launch it as-is for the standard loop, or adapt the prompts /
 * batch sizing / model routing per repo. The firm parts (mandatory scan, verify-before-
 * count, stall, no-budget-in-condition) are the structure; brief content is adjustable.
 *
 * args (passed by optimize/SKILL.md Step 0.2):
 *   { pluginRoot, subagents, budget, stall }
 *   - pluginRoot : absolute path of the evo plugin (${CLAUDE_PLUGIN_ROOT}); used so nodes
 *                  can Read the subagent skill by path (deterministic protocol loading).
 *   - subagents  : round width N
 *   - budget     : per-subagent iteration budget
 *   - stall      : consecutive no-improve rounds before stopping
 *
 * Schemas are inlined (the workflow runtime is not relied on to resolve relative imports).
 */

export const meta = {
  name: 'evo-optimize',
  description: 'Deterministic evo tree-search loop over the evo CLI (orient, scan, ideate-on-stall, brief, fan-out, verify, collect).',
  phases: [
    { title: 'Orient',   detail: 'read state + select frontier parents to extend' },
    { title: 'Scan',     detail: 'mandatory parallel cross-cutting scan + structural aggregation (incl. cross-history axis check)' },
    { title: 'Ideate',   detail: 'research escalation: parallel ideators on stall / every ~5 commits' },
    { title: 'Brief',    detail: 'write one non-overlapping brief per subagent (reconciling ideator proposals)' },
    { title: 'Optimize', detail: 'parallel optimization subagents (evo new/run)' },
    { title: 'Verify',   detail: 'validity audit + benchmark-noise confirm' },
    { title: 'Collect',  detail: 'prune dead lineages, record cross-cutting notes' },
  ],
}

// ---------------------------------------------------------------------------
// Schemas (inlined)
// ---------------------------------------------------------------------------
const STATE = {
  type: 'object',
  // direction + evaluatedIds are required: the stall comparator needs direction (min vs max),
  // and the mandatory scan keys off evaluatedIds (the state node must return [] when there are none).
  required: ['bestScore', 'ceiling', 'frontier', 'direction', 'evaluatedIds'],
  properties: {
    bestScore: { type: 'number' },
    bestExpId: { type: 'string' },
    ceiling: { type: 'number' },
    direction: { enum: ['max', 'min'] },
    frontier: {
      type: 'array',
      items: {
        type: 'object',
        properties: { id: { type: 'string' }, score: { type: 'number' }, rank: { type: 'integer' } },
        required: ['id'],
      },
    },
    evaluatedIds: { type: 'array', items: { type: 'string' } },
    committedCount: { type: 'integer' },    // total committed experiments (drives the periodic ideator trigger)
    verifyRepeats: { type: 'integer' },     // benchmark noise profile (1 = deterministic, no confirm-loop)
    summary: { type: 'string' },            // short scratchpad summary for subagent context
  },
}

const FINDINGS = {
  type: 'object',
  required: ['findings'],
  properties: {
    findings: {
      type: 'array',
      items: {
        type: 'object',
        required: ['description', 'experiment_ids'],
        properties: {
          description: { type: 'string' },
          experiment_ids: { type: 'array', items: { type: 'string' } },
          evidence: { type: 'array', items: { type: 'string' } },
        },
      },
    },
  },
}

const PATTERNS = {
  type: 'object',
  required: ['patterns'],
  properties: {
    patterns: {
      type: 'array',
      items: {
        type: 'object',
        required: ['kind', 'label'],
        properties: {
          kind: { type: 'string', enum: ['single', 'intersection', 'improver', 'axis-warning'] },
          label: { type: 'string' },
          experiment_ids: { type: 'array', items: { type: 'string' } },
        },
      },
    },
  },
}

const BRIEFS = {
  type: 'object',
  required: ['briefs'],
  properties: {
    briefs: {
      type: 'array',
      items: {
        type: 'object',
        required: ['objective', 'parent', 'boundaries', 'pointerTraces'],
        properties: {
          objective: { type: 'string' },
          parent: { type: 'string' },
          boundaries: { type: 'string' },
          pointerTraces: { type: 'array', items: { type: 'string' } },
          hard: { type: 'boolean' },
        },
      },
    },
  },
}

const SUBAGENT_RESULT = {
  type: 'object',
  required: ['expIds', 'status', 'committedImprover'],
  properties: {
    expIds: { type: 'array', items: { type: 'string', pattern: '^exp_[0-9]+$' } }, // proof the protocol ran
    bestExpId: { type: 'string' },
    bestScore: { type: 'number' },
    status: { type: 'string', enum: ['committed', 'evaluated', 'failed', 'none'] },
    committedImprover: { type: 'boolean' },
    gatesAdded: { type: 'array', items: { type: 'string' } },
    learnings: { type: 'array', items: { type: 'string' } },
    suggestions: { type: 'array', items: { type: 'string' } },
  },
  // NOTE: "a committed improver must carry bestExpId + a numeric bestScore" is intentionally NOT
  // expressed as a JSON-Schema allOf/if/then here. The workflow StructuredOutput validator runs in
  // strict mode and REJECTS allOf/if/then sub-schemas (verified empirically: the schema fails to
  // compile and every agent call errors with "subagent completed without calling StructuredOutput").
  // The improver requirement is enforced in the verify stage instead (the bestExpId / numeric
  // bestScore guard), which is functionally equivalent for stall accounting and auditing.
}

const VERDICT = {
  type: 'object',
  required: ['valid'],
  properties: {
    valid: { type: 'boolean' },
    reasons: { type: 'array', items: { type: 'string' } },
  },
}

// Implement stage output: experiment allocated + edited in its worktree, NOT yet run.
const IMPL_RESULT = {
  type: 'object',
  required: ['expId', 'worktree'],
  properties: {
    expId: { type: 'string', pattern: '^exp_[0-9]+$' },
    worktree: { type: 'string' },
    summary: { type: 'string' },
  },
}

// Pre-run verifier verdict (design-time cheating gate).
const PREVERDICT = {
  type: 'object',
  required: ['pass'],
  properties: {
    pass: { type: 'boolean' },
    findings: { type: 'array', items: { type: 'string' } },
  },
}

// ---------------------------------------------------------------------------
// Helpers (pure JS — control-plane only)
// ---------------------------------------------------------------------------
// `args` may arrive as an object OR as a JSON STRING — the Workflow `args` param is frequently
// threaded to the script verbatim as a string (confirmed empirically). Coerce so the four knobs
// resolve either way; then Number() so even stringified numbers ("1") coerce, with NaN || 5 -> 5.
const A = typeof args === 'string'
  ? (() => { try { return JSON.parse(args) } catch (_) { return {} } })()
  : (args || {})
const pr = A.pluginRoot || ''
const WIDTH = Number(A.subagents) || 5
const ITER  = Number(A.budget) || 5
const LIMIT = Number(A.stall) || 5
// Fire ideators (research escalation) once the stall counter reaches this — strictly BELOW the hard
// stall limit, so the loop researches its way toward a new direction before it gives up.
const IDEATE_STALL = Math.max(1, Math.min(3, LIMIT - 1))
const IDEATE_EVERY_COMMITS = 5   // periodic research cadence (matches prose step 6b)
const PREVERIFY_MAX = 3          // pre-run verify <-> revise attempts before discarding a rigged edit
// Experiments per scan agent. Heuristic for the prose "small enough to read in one pass" rule —
// the workflow can't recursively self-partition like the prose loop, so this is fixed up front.
// Lower it for heavy traces (many tasks / long messages); raise it for tiny traces.
const SCAN_BATCH = 6

function betterResult(a, b, direction) {
  if (!a) return b
  if (!b) return a
  const sa = typeof a.bestScore === 'number' ? a.bestScore : null
  const sb = typeof b.bestScore === 'number' ? b.bestScore : null
  if (sa === null) return b
  if (sb === null) return a
  return (direction === 'min' ? sb < sa : sb > sa) ? b : a
}

function chunk(arr, n) {
  const out = []
  for (let i = 0; i < arr.length; i += n) out.push(arr.slice(i, i + n))
  return out
}

// Compact label for a batch of ids: factor out the shared leading chars.
// e.g. ["exp_0003","exp_0004","exp_0005"] -> "exp_000[3,4,5]"
function commonPrefix(strs) {
  if (!strs.length) return ''
  let p = strs[0]
  for (const s of strs) {
    while (p && !s.startsWith(p)) p = p.slice(0, -1)
    if (!p) break
  }
  return p
}
function batchLabel(b) {
  if (!b.length) return 'frontier'
  if (b.length === 1) return b[0]
  const p = commonPrefix(b)
  return p.length > 1 ? `${p}[${b.map((x) => x.slice(p.length)).join(',')}]` : b.join(',')
}

// Diversity check: drop briefs whose pointer-trace sets overlap heavily with an earlier one.
function dedupeBriefs(briefs) {
  const kept = []
  for (const b of briefs) {
    const ptr = new Set(b.pointerTraces || [])
    const clash = kept.some((k) => {
      const o = new Set(k.pointerTraces || [])
      const inter = [...ptr].filter((x) => o.has(x)).length
      const overlap = inter / Math.max(1, Math.min(ptr.size, o.size))
      return k.parent === b.parent && overlap >= 0.6
    })
    if (!clash) kept.push(b)
  }
  return kept
}

// ---------------------------------------------------------------------------
// Node prompt builders
// ---------------------------------------------------------------------------
function statePrompt() {
  return [
    'Read-only. Do NOT edit files or run experiments. Run these and parse their output:',
    '`evo scratchpad`, `evo frontier` (already prints a JSON envelope), `evo status`, `evo awaiting`.',
    'Also read `.evo/project.md` for the metric goal, direction, and the benchmark-determinism line.',
    'Return: bestScore + bestExpId; the theoretical ceiling (1.0 for max metric, 0.0 for min)',
    'and direction; the frontier nodes ALREADY ranked by the configured strategy',
    '(id, score, rank) — preserve evo\'s ordering, do not re-rank; the list of evaluated-but-',
    'undecided experiment ids; committedCount (number of committed experiments, from `evo status`);',
    'verifyRepeats (from project.md: 1 if deterministic, 3 if sampling-based / variance-expected);',
    'and a 2-3 sentence scratchpad summary for subagent context.',
  ].join(' ')
}

// Verbatim scan brief from optimize/SKILL.md step 3.
function scanBrief(batch) {
  return [
    'You are a read-only evo scan sub-agent. Do not run experiments or edit code.',
    '',
    'Start by reading `.evo/project.md` to understand the optimization goal and metric. All your findings should be relevant to this goal.',
    '',
    `Your batch: ${JSON.stringify(batch)}.`,
    '',
    'For each experiment, read its `outcome.json` and `traces/task_*.json` under `.evo/run_*/experiments/<id>/attempts/NNN/`. Also consider `hypothesis` and prose `error` text.',
    '',
    'Find patterns that will populate the next round\'s subagent briefs:',
    '- Shared failure causes -- root-cause reasons recurring across 2+ experiments (the *why*, not the surface gate name).',
    '- Wall patterns -- approaches or gates multiple experiments consistently fail on.',
    '- Compound-failure standouts -- single experiments hitting multiple failure modes.',
    '',
    'Evidence must be VERBATIM quotes from outcome.json fields, trace messages, or error text -- not paraphrases. If you cannot cite verbatim evidence for a finding, drop it. Evidence: short quotes (<200 chars), max 3 per finding.',
    'Return JSON only: {"findings":[{"description","experiment_ids":[],"evidence":[]}]}.',
  ].join('\n')
}

function aggregatePrompt(ids) {
  return [
    'Read-only. These experiments', JSON.stringify(ids), 'are a MIX of evaluated-but-undecided nodes',
    'and committed frontier nodes. Load each outcome.json under the active run dir',
    '(`.evo/run_*/experiments/<id>/attempts/NNN/outcome.json`) in Python; the `outcome` field tells you which is which.',
    'From the EVALUATED ones aggregate: co-occurring gate_failures; shared zero-score task ids in',
    'benchmark.result.tasks; recurring substrings in error — emit each single-pattern set AND every',
    'pairwise intersection where >=2 experiments exhibit both (kind:"intersection").',
    'From the COMMITTED ones enumerate improvers (outcome=committed — evo already applied the metric',
    'direction when it committed; do NOT re-derive improvement from a raw score>parent comparison) as kind:"improver".',
    'CROSS-HISTORY AXIS CHECK (look beyond this batch): run `evo tree` and read the `hypothesis` of ALL committed',
    'experiments in the run. If 3+ STRUCTURALLY DISTINCT hypotheses (not parameter sweeps of one idea) committed at',
    '~the same score (a plateau), the bottleneck is not where those hypotheses aimed — emit kind:"axis-warning" whose',
    'label names the saturated axis AND suggests the orthogonal axis (harness, score definition, input data, or a',
    'different mechanism) the next briefs should pivot to. At most one axis-warning.',
    'Return JSON only.',
  ].join(' ')
}

function briefPrompt(state, findings, patterns, parents, ideated) {
  return [
    'You are the evo orchestrator\'s brief writer.',
    'State summary:', state.summary || '',
    '\nVerified scan findings:', JSON.stringify(findings),
    '\nStructural patterns (incl. intersections, improvers, and any axis-warning):', JSON.stringify(patterns),
    '\nSelected parent nodes:', JSON.stringify(parents.map((p) => p.id)),
    ideated
      ? '\nFRESH IDEATOR PROPOSALS may be available — read `.evo/run_*/ideator/proposals.jsonl` and reconcile BEFORE writing: skip any whose technique was already tried (`evo discards --like "<keyword>"`); score the rest by expected_score_uplift x confidence (frontier_extrapolation > failure_analysis > literature, all else equal); let the top 1-2 become brief objectives, citing the proposal\'s hypothesis/technique. Proposals are advisory — if none beat the in-graph scan findings, ignore them.'
      : '',
    '\nIf the patterns include an "axis-warning", the current axis is saturated — target the ORTHOGONAL axis it names rather than iterating the plateaued one.',
    `\nWrite up to ${WIDTH} briefs (use the full round width of ${WIDTH} whenever you can find that many genuinely DISTINCT objectives — multiple briefs MAY branch from the SAME parent when fewer than ${WIDTH} frontier parents exist, as long as each attacks a different surface; do not pad with redundant briefs). One per subagent, each with four fields:`,
    '1. objective -- one sentence naming WHERE in system behavior the gain hides, with evidence; NO file/function/edit names.',
    '2. parent -- which experiment id to branch from (choose from the selected parents).',
    '3. boundaries -- what NOT to try and why (discarded approaches, gates not to regress, what adjacent briefs this round do).',
    '4. pointerTraces -- task ids to study first, one-line reason each.',
    'Mark hard:true on any brief needing deep trace analysis.',
    'The briefs MUST NOT collapse onto each other -- distinct objectives, non-overlapping pointer traces, different surfaces.',
    'Return JSON only.',
  ].join(' ')
}

// IMPLEMENT — allocate + edit, but do NOT run (a pre-run verifier audits the edit first).
function implementPrompt(brief, parent, state) {
  return [
    `First, load and follow the evo subagent skill: Read ${pr}/skills/subagent/SKILL.md IN FULL and follow it as your operating protocol — do not skip it even if the brief looks simple.`,
    `Allocate your experiment via \`evo new --parent ${parent}\`, then edit inside the returned worktree to implement the brief.`,
    'IMPORTANT: do NOT run `evo run` yet — a pre-run verifier audits your change first. Stop once the edit is complete.',
    'Do NOT edit benchmark, gate, or framework code; do NOT weaken/bypass any gate.',
    '',
    'Your brief:',
    `- Objective: ${brief.objective}`,
    `- Parent node: ${parent}`,
    `- Boundaries / anti-patterns: ${brief.boundaries}`,
    `- Pointer traces: ${(brief.pointerTraces || []).join('; ')}`,
    '',
    'Context (current state):', state.summary || '',
    '',
    'Return the experiment id you created (exp_NNNN), its worktree path, and a 1-2 sentence change summary.',
  ].join('\n')
}

// PRE-VERIFY — independent design-time cheating audit of the uncommitted edit (evo:verifier, pre-phase).
function preVerifyPrompt(expId, worktree) {
  return [
    `Read-only PRE-RUN verifier audit (design-time cheating) for experiment ${expId}.`,
    `Inspect the UNCOMMITTED change in its worktree (\`git -C ${worktree} diff\` plus the edited files).`,
    'Set pass:false if the change does ANY of: leaks held-out / validation / gold answers into what the target sees;',
    'subsets or narrows the eval/benchmark command to only easy cases; adds a new artifact that needs a gate but has none;',
    'rests on a generic/empty hypothesis; short-circuits via a cache; fabricates artifacts; or edits benchmark/gate/framework code.',
    'Otherwise pass:true. Return {pass, findings:[]}; findings are concrete, fixable statements. Default pass:false if you cannot confirm it is clean.',
  ].join(' ')
}

// REVISE — feed the verifier findings back to fix the edit (still no run).
function revisePrompt(expId, worktree, findings) {
  return [
    `The pre-run verifier FAILED experiment ${expId} for these design-time issues:`, JSON.stringify(findings || []),
    `Revise the edit in its worktree (${worktree}) to address EVERY finding WITHOUT weakening the brief's objective or gaming the metric.`,
    'Do NOT run `evo run`. Do NOT edit benchmark/gate/framework code. Return a 1-2 sentence summary of the fix.',
  ].join(' ')
}

// RUN — evaluate + commit the (pre-verified) experiment.
function runPrompt(expId) {
  return [
    `Run \`evo run ${expId}\` to evaluate and (if it improves and passes gates) commit it.`,
    'If it exits GATE_FAILED, do not fight the gate — report status=evaluated.',
    `Return: expIds:["${expId}"]; status (committed|evaluated|failed|none); committedImprover = true ONLY if evo printed COMMITTED;`,
    'bestExpId + bestScore (required when committedImprover is true); any gates added; learnings.',
  ].join(' ')
}

// DISCARD — pre-verify never satisfied; do not run a rigged experiment.
function discardPrompt(expId, findings) {
  return [
    `Pre-run verification could not be satisfied for ${expId} after ${PREVERIFY_MAX} revision attempts:`, JSON.stringify(findings || []),
    `Annotate the experiment (\`evo annotate ${expId} "pre-verify failed: ..."\`), then run`,
    `\`evo discard ${expId} --reason "pre-verify: design-time cheating not resolved"\` so a rigged experiment is never run or committed.`,
    'Return a one-line confirmation.',
  ].join(' ')
}

// One ideator brief (failure_analysis | literature | frontier_extrapolation). Dispatched via
// agentType 'evo:ideator' so the agent gets the ideator system prompt + its tool set (incl.
// WebSearch/WebFetch for literature). It appends proposals to .evo/run_*/ideator/proposals.jsonl.
function ideatorPrompt(brief) {
  return [
    `brief=${brief}`,
    '(workspace: infer from the current directory by walking up until you find `.evo/`.)',
    'Follow your ideator protocol: produce proposals for this brief and append them as JSONL to',
    '`.evo/run_*/ideator/proposals.jsonl` in a single final write, then return a short JSON summary.',
  ].join('\n')
}

function auditPrompt(expId) {
  return [
    `Read-only validity audit of experiment ${expId}.`,
    'Read its artifacts under `.evo/run_*/experiments/<id>/attempts/NNN/` (diff.patch, outcome.json, benchmark.log).',
    'Check: did it edit benchmark / gate / framework code? does the held-out slice still pass?',
    'is the score reproducible / not short-circuited by a cache? any constant-return or metric-gaming pattern?',
    'Return {valid:bool, reasons:[]}. Default valid:false if you cannot confirm.',
  ].join(' ')
}

function collectPrompt(results, round) {
  return [
    `Round ${round} results:`, JSON.stringify(results.map((r) => ({ expIds: r.expIds, status: r.status, improver: r.committedImprover }))),
    '\nRead each evaluated node\'s outcome.json (`.evo/run_*/experiments/<id>/attempts/NNN/outcome.json`) and spot shared failure modes the per-subagent summaries glossed over.',
    'Where a committed node has 3+ children that all regressed, run `evo prune <id> --reason "exhausted: ..."` (never `evo discard` a committed node).',
    'Record cross-cutting learnings with `evo set <id> --note "..."` and any workspace insight with `evo note "..."`.',
    'Return a one-line summary of what you pruned and noted.',
  ].join(' ')
}

// Per-brief lane: implement -> pre-verify <-> revise loop -> run -> post-audit, repeated up to the
// iteration budget (deepening the branch each time a committed improver lands). The independent
// evo:verifier gates EACH run for design-time cheating BEFORE the experiment is evaluated; its
// findings are fed back to a revise agent on the same experiment until it passes or is discarded.
async function runBrief(brief, state) {
  let parent = brief.parent
  let best = null
  for (let depth = 0; depth < ITER; depth++) {
    const impl = await agent(implementPrompt(brief, parent, state), {
      schema: IMPL_RESULT, model: brief.hard ? 'opus' : 'sonnet', phase: 'Optimize', label: `impl:${parent}#${depth}`,
    })
    if (!impl || !impl.expId) break

    // pre-verify <-> revise feedback loop (design-time cheating gate)
    let pv = null
    for (let v = 0; v < PREVERIFY_MAX; v++) {
      pv = await agent(preVerifyPrompt(impl.expId, impl.worktree), {
        schema: PREVERDICT, agentType: 'evo:verifier', phase: 'Verify', label: `preverify:${impl.expId}#${v}`,
      })
      if (pv && pv.pass) break
      if (v < PREVERIFY_MAX - 1) {
        await agent(revisePrompt(impl.expId, impl.worktree, pv && pv.findings), {
          model: brief.hard ? 'opus' : 'sonnet', phase: 'Optimize', label: `revise:${impl.expId}#${v}`,
        })
      }
    }
    if (!pv || !pv.pass) {
      await agent(discardPrompt(impl.expId, pv && pv.findings), { phase: 'Verify', label: `discard:${impl.expId}` })
      break // couldn't produce a clean edit on this branch — stop spending budget here
    }

    // run -> evaluate + commit
    const r = await agent(runPrompt(impl.expId), { schema: SUBAGENT_RESULT, phase: 'Optimize', label: `run:${impl.expId}` })
    if (!r) break

    // post-run validity audit (evo:verifier, post-phase) on committed improvers
    let valid = true
    if (r.committedImprover) {
      if (!r.bestExpId || typeof r.bestScore !== 'number') {
        valid = false
      } else {
        const audit = await agent(auditPrompt(r.bestExpId), { schema: VERDICT, agentType: 'evo:verifier', phase: 'Verify', label: `audit:${r.bestExpId}` })
        valid = !!(audit && audit.valid)
        if (valid && (Number(state.verifyRepeats) || 1) > 1) {
          log(`note: ${r.bestExpId} on a noisy benchmark (repeats=${state.verifyRepeats}); confirm-loop pending the evo rescore affordance — relying on the held-out gate`)
        }
      }
    }
    const scored = { ...r, valid }
    best = betterResult(best, scored, state.direction)

    if (valid && r.committedImprover && r.bestExpId) {
      parent = r.bestExpId // deepen: extend the new commit on the next budget iteration
    } else {
      break // evaluated / failed / invalid — stop deepening this branch
    }
  }
  return best
}

// ---------------------------------------------------------------------------
// The loop
// ---------------------------------------------------------------------------
let stall = 0
let round = 0
let lastIdeatedCommit = 0      // committedCount at the last ideator dispatch (periodic cadence)
let ideatedThisStall = false   // fire ideators once per stall episode, not every stalled round

log(`evo-optimize start: subagents=${WIDTH} budget=${ITER} stall=${LIMIT} | argsType=${typeof args} A.subagents=${A.subagents} A.budget=${A.budget} A.stall=${A.stall}`)

while (stall < LIMIT) {
  round += 1

  phase('Orient')
  const state = await agent(statePrompt(), { schema: STATE, agentType: 'Explore', model: 'haiku', phase: 'Orient', label: `state:r${round}` })
  if (state.bestScore === state.ceiling) { log(`ceiling reached (best=${state.bestScore}) — stopping`); break }
  const parents = (state.frontier || []).slice(0, WIDTH)
  if (parents.length === 0) { log('no explorable frontier nodes — stopping'); break }

  // N1 + N1.5 — mandatory parallel scan + structural aggregation (barrier).
  // The scan runs EVERY round (hard rule). When there are no evaluated-undecided nodes yet
  // (e.g. round 1, right after the baseline), fall back to scanning the committed frontier nodes
  // so at least one scan agent still runs before briefs.
  phase('Scan')
  const evaluatedIds = state.evaluatedIds || []
  const frontierIds = (state.frontier || []).map((f) => f.id).filter(Boolean)
  const scanTargets = evaluatedIds.length ? evaluatedIds : frontierIds
  const batches = chunk(scanTargets, SCAN_BATCH)
  const scanThunks = batches.map((b) => () => agent(scanBrief(b), { schema: FINDINGS, agentType: 'Explore', phase: 'Scan', label: `scan ${b.length}: ${batchLabel(b)}` }))
  // Aggregate sees BOTH evaluated-undecided nodes (for failure intersections) AND committed
  // frontier nodes (so the improver enumeration has committed experiments to draw from).
  const aggregateIds = [...new Set([...evaluatedIds, ...frontierIds])]
  const aggThunk = aggregateIds.length
    ? [() => agent(aggregatePrompt(aggregateIds), { schema: PATTERNS, agentType: 'Explore', phase: 'Scan', label: 'aggregate' })]
    : []
  const scanResults = (await parallel([...scanThunks, ...aggThunk])).filter(Boolean)
  const findings = scanResults.flatMap((r) => (r && r.findings) ? r.findings : [])
  const patterns = scanResults.flatMap((r) => (r && r.patterns) ? r.patterns : [])

  // N1.7 — research escalation (6b): on stall (before the hard limit) or every ~5 commits, fire
  // the three ideators in parallel. They append proposals to .evo/run_*/ideator/proposals.jsonl;
  // parallel() blocks until all return (the "block until proposals land" the prose does via evo wait).
  const commits = Number(state.committedCount) || 0
  const stalledTrigger = stall >= IDEATE_STALL && !ideatedThisStall
  const periodicTrigger = commits - lastIdeatedCommit >= IDEATE_EVERY_COMMITS
  let ideated = false
  if (stalledTrigger || periodicTrigger) {
    phase('Ideate')
    await parallel(['frontier_extrapolation', 'failure_analysis', 'literature'].map((b) => () =>
      agent(ideatorPrompt(b), { agentType: 'evo:ideator', phase: 'Ideate', label: `ideate:${b}` })))
    lastIdeatedCommit = commits
    if (stalledTrigger) ideatedThisStall = true
    ideated = true
    log(`ideators fired (trigger: ${stalledTrigger ? 'stall' : 'periodic'}, stall=${stall}, commits=${commits})`)
  }

  // N2 — brief writer (judgment): reconciles ideator proposals (6c) + acts on axis-warning; JS diversity dedupe.
  phase('Brief')
  const briefOut = await agent(briefPrompt(state, findings, patterns, parents, ideated), { schema: BRIEFS, phase: 'Brief', label: `briefs:r${round}` })
  const briefs = dedupeBriefs((briefOut && briefOut.briefs) || [])
  if (briefs.length === 0) { log('no briefs produced — stopping'); break }

  // N3..N4 — fan out one lane per brief; each lane: implement -> pre-verify<->revise -> run -> post-audit.
  const results = (await parallel(briefs.map((b) => () => runBrief(b, state)))).filter(Boolean)

  // N5 — collect: prune dead lineages, record notes.
  phase('Collect')
  await agent(collectPrompt(results, round), { phase: 'Collect', label: `collect:r${round}` })

  // Loop control: stall resets only when this round produced a VERIFIED committed score that
  // beats the PRIOR BEST in the metric direction. A committed improver that beat its own parent
  // but not the global best does NOT reset stall (it's progress on a branch, not a new best).
  // No budget in the condition.
  const dir = state.direction || 'max'
  const gains = results
    .filter((r) => r.committedImprover && r.valid !== false && typeof r.bestScore === 'number')
    .map((r) => r.bestScore)
  const roundBest = gains.length ? (dir === 'min' ? Math.min(...gains) : Math.max(...gains)) : null
  const improved = roundBest !== null && (dir === 'min' ? roundBest < state.bestScore : roundBest > state.bestScore)
  stall = improved ? 0 : stall + 1
  if (improved) ideatedThisStall = false   // new best → a fresh stall episode may re-trigger ideators later
  log(`round ${round}: improved=${improved} roundBest=${roundBest} prevBest=${state.bestScore} stall=${stall}/${LIMIT} spent=${budget.spent()}`)
}

log(`optimize workflow finished after ${round} round(s), final stall=${stall}/${LIMIT}`)
return { rounds: round, finalStall: stall }

/*
 * evo-optimize.js — Claude Code dynamic-workflow driver for the /evo:optimize round loop.
 *
 * This is the CODE form of plugins/evo/skills/optimize/SKILL.md "The Loop". It is an
 * opt-in, Claude-Code-only driver; the prose skill remains the canonical, host-agnostic
 * default. The workflow encodes the loop CONTROL: while/stall, mandatory scan + cross-history
 * axis check, research escalation (ideators on stall / every ~5 commits), brief + diversity,
 * fan-out + verify, collect + frontier-select. A concurrent ANALYST thread (Opus, self-paced,
 * read-only) runs alongside the round loop via Promise.all — host + cross-history checks during
 * rounds, feeding hints into the next brief. All domain work goes through the `evo` CLI inside
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
    { title: 'Analyst',  detail: 'concurrent independent observer (Opus) — host + cross-history checks during rounds' },
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
    taskSkills: { type: 'array', items: { type: 'string' } },     // category skills a builder should load (e.g. ["finetuning"]) — resolved from config/project, never hardcoded in the workflow
    knownLearnings: { type: 'array', items: { type: 'string' } }, // durable lessons to apply up front (drained from annotations) so a fresh stateless lane doesn't rediscover them
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

// Analyst tick output: work-quality hints (fed into the next brief) + runtime/host alerts (surfaced).
const ANALYST_FINDINGS = {
  type: 'object',
  required: ['briefHints', 'alerts', 'stops'],
  properties: {
    briefHints: { type: 'array', items: { type: 'string' } },
    alerts: { type: 'array', items: { type: 'string' } },
    // STOP recommendations for in-flight experiments that are clearly doomed.
    // A stop is NOT a crash: each carries the diagnosis + a fix so the gated
    // enforcer can abort, annotate (lesson outlives the worktree), and classify+
    // preserve — and the fix feeds the next round (and, if general, a skill prior).
    stops: {
      type: 'array',
      items: {
        type: 'object',
        required: ['expId', 'failureClass', 'reason', 'fixHint'],
        properties: {
          expId: { type: 'string' },
          failureClass: { enum: ['build', 'eval', 'hypothesis'] },
          reason: { type: 'string' },    // what's wrong + the concrete evidence
          fixHint: { type: 'string' },   // what the NEXT experiment must change
        },
      },
    },
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
// Concurrent analyst thread (runs alongside the round loop, NOT per-round).
const ANALYST_ENABLED = true
const ANALYST_MODEL = 'opus'    // the analyst always reasons with Opus (judgment-heavy)
const ANALYST_INTERVAL_S = 300  // self-pace: observe ~every 5 min, during rounds
const ANALYST_HOP_S = 15        // the wait is INTERRUPTIBLE in hops of this size: when the optimize loop
                                // ends mid-wait it drops a sentinel the analyst polls, so the in-flight
                                // tick exits within ~ANALYST_HOP_S instead of stalling the run for the
                                // full interval (the script can't interrupt an agent's `sleep` directly).
const DONE_SENTINEL = '.evo/.wf_optimize_done'  // optimize -> analyst "loop is over" signal (a file,
                                // since the in-memory `done` flag isn't visible to the agent's process)
const ANALYST_MAX_FAILS = 3     // consecutive failed ticks before the advisory analyst self-disables
                                // (guards against a hot-spin when ticks fail instantly, e.g. a bad schema)
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
    'Also run `evo config get task-skills` (if unset/blank, infer the task category from project.md) and read recent `evo annotations` for durable learnings already found this run.',
    'Return: bestScore + bestExpId; the theoretical ceiling (1.0 for max metric, 0.0 for min)',
    'and direction; the frontier nodes ALREADY ranked by the configured strategy',
    '(id, score, rank) — preserve evo\'s ordering, do not re-rank; the list of evaluated-but-',
    'undecided experiment ids; committedCount (number of committed experiments, from `evo status`);',
    'verifyRepeats (from project.md: 1 if deterministic, 3 if sampling-based / variance-expected);',
    'taskSkills (category skills a builder should load, e.g. ["finetuning"] — from `evo config get task-skills`, else inferred from project.md);',
    'knownLearnings (short durable lessons from annotations to apply up front: trainer-API quirks, device placement, eval-side config, etc.);',
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
    '- Axis exhaustion vs fixable plumbing -- read each node\'s `failure_class` (build|eval|hypothesis) from outcome.json. A cluster of `hypothesis` failures across STRUCTURALLY DISTINCT approaches means the axis itself is unpromising (flag it so the next briefs diverge); `build`/`eval` failures are fixable plumbing (recipe/scoring) and must NOT be read as axis exhaustion.',
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
    'Also read DISCARDED nodes (`evo discards` + their outcome.json `failure_class`): a cluster of 3+ failure_class="hypothesis"',
    'discards across STRUCTURALLY DISTINCT approaches is itself an axis-warning (the direction keeps not helping). IGNORE',
    'failure_class="build"/"eval" discards for axis purposes — those are fixable plumbing (retry/resume or eval-retest), not',
    'evidence the axis is wrong.',
    'Return JSON only.',
  ].join(' ')
}

function briefPrompt(state, findings, patterns, parents, ideated, analystHints) {
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
    (analystHints && analystHints.length)
      ? '\nLIVE ANALYST SIGNALS (from the concurrent observer — fold relevant ones into objectives/boundaries, e.g. switch off a saturated axis, avoid a flagged dead direction): ' + JSON.stringify(analystHints)
      : '',
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
// Context capsule shared by every builder/runner lane: which category skills to load on demand,
// and the durable learnings to apply up front — so a fresh stateless agent inherits the priors and
// hard-won lessons a prose single-subagent would have had, instead of rediscovering them.
function capsuleLines(state) {
  const skills = (state && state.taskSkills) || []
  const learnings = (state && state.knownLearnings) || []
  const lines = []
  lines.push(skills.length
    ? `Task-category skills — load IN FULL via your host skill loader if the work needs them (they carry this category's priors, recipes, and pre-run checks): ${skills.join(', ')}.`
    : "Identify the task category from `.evo/project.md` and load the matching evo skill (e.g. evo:finetuning for a training move) IN FULL before you build — it carries this category's priors and pre-run checks.")
  if (learnings.length) lines.push(`KNOWN LEARNINGS to apply before acting (already found this run — do not rediscover): ${JSON.stringify(learnings)}.`)
  return lines
}

function implementPrompt(brief, parent, state) {
  return [
    `First, load and follow the evo subagent skill: Read ${pr}/skills/subagent/SKILL.md IN FULL and follow it as your operating protocol — do not skip it even if the brief looks simple.`,
    ...capsuleLines(state),
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
function runPrompt(expId, state) {
  return [
    `First, load the evo subagent skill: Read ${pr}/skills/subagent/SKILL.md IN FULL and follow its run protocol (it covers \`evo run ${expId} --check\` for non-committing wiring validation that does not consume the attempt budget).`,
    ...capsuleLines(state),
    `CRITICAL ordering: if this experiment produces an output artifact through a build or training step (whatever your recipe declares — a checkpoint dir, adapter, merged model, index, etc.), run that step to COMPLETION and confirm the artifact exists BEFORE the real run. Never call \`evo run\` while that step is still in flight or before its output exists — evaluating a not-yet-produced artifact wastes the attempt. If the experiment warm-starts, the parent's reusable artifact is in EVO_PARENT_POLICY (start from it; do not redo from scratch).`,
    `Then run \`evo run ${expId}\` to evaluate and (if it improves and passes gates) commit it.`,
    'If it exits GATE_FAILED, do not fight the gate — report status=evaluated.',
    'If `evo run` is terminated externally mid-flight (the concurrent analyst can STOP a doomed experiment — it aborts the run and discards this node with a diagnosis), do NOT retry: report status:none and stop. The diagnosis is already recorded as an annotation and will steer the next brief.',
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

// One analyst tick (a FRESH Opus agent each call — no memory across ticks, so `reported` carries
// the dedup state in the loop's closure). Read-only: observes host + cross-history signals DURING
// rounds, returns work-quality briefHints (folded into the next brief) + runtime alerts (surfaced).
function analystPrompt(ctx, intervalS, reported) {
  return [
    'You are the evo ANALYST — an independent observer running CONCURRENTLY with the optimize loop.',
    'Read-only: do NOT edit code, run experiments, or mutate evo state.',
    `FIRST pace yourself with an INTERRUPTIBLE wait, so you stop promptly when the optimize loop ends. Run this single Bash command with a tool timeout of at least ${(intervalS + 30) * 1000} ms:`,
    `  \`if [ -f ${DONE_SENTINEL} ]; then echo OPTIMIZE_DONE; else for i in $(seq 1 ${Math.ceil(intervalS / ANALYST_HOP_S)}); do sleep ${ANALYST_HOP_S}; [ -f ${DONE_SENTINEL} ] && { echo OPTIMIZE_DONE; break; }; done; fi\``,
    `If that prints OPTIMIZE_DONE, the optimize loop has finished — return {"briefHints":[],"alerts":[]} immediately WITHOUT gathering any signals. Otherwise the full interval elapsed: now gather signals and report.`,
    `Current loop state: round=${ctx.round}, stall=${ctx.stall}/${LIMIT}, best=${ctx.bestScore}.`,
    `Already reported (do NOT repeat — only emit findings NEW since these): ${JSON.stringify(reported || [])}.`,
    'Walk these checks (skip any whose inputs are unavailable; cite evidence; nothing speculative):',
    '- Zombie GPU: `nvidia-smi --query-compute-apps=pid,used_memory,process_name --format=csv,noheader` + `ps` — a PID holding >=4GB not tied to an active `evo run`. ALERT with a verify clause (do NOT kill).',
    '- Buried stderr warning: tail recent experiment stderr under `.evo/run_*/experiments/*/attempts/*/` for tokenizer / EOS / chat_template / parity-mismatch lines not already annotated. ALERT.',
    '- Stuck experiment / time-budget overrun: from `evo status`/`evo show`, an experiment active far longer than its peers, or a round overrunning the others. ALERT.',
    '- Stuck axis: from `evo tree`, 3+ structurally-distinct committed hypotheses plateaued at ~the same score → name the saturated axis + one orthogonal axis. BRIEF HINT.',
    '- Dead direction / ignored mechanism: annotations repeatedly naming a mechanism the recent work ignores, or a direction that keeps regressing. BRIEF HINT.',
    '- Heading toward failure (STOP): an in-flight experiment that is CLEARLY doomed or wasting the budget — a divergent / NaN / flatlined progress metric; projected completion beyond the remaining time budget; or a known-fatal signature (e.g. output the scorer cannot parse; a silent resource mis-placement that tanks throughput with no error; a corrupt input/format that invalidates the result). HIGH PRECISION ONLY: default to NOT stopping — recommend a STOP only with concrete evidence that finishing is wasted, and only for an experiment still `active`. Emit a stop with: expId; failureClass (build = the build/produce step is broken; eval = artifact is fine but scoring/serving is wrong; hypothesis = it runs but won\'t help); reason (the diagnosis + the evidence you saw); fixHint (what the NEXT experiment must change).',
    'You stay READ-ONLY: do NOT run `evo abort` / `evo discard` yourself. A gated enforcer acts on each stop — it aborts the run + its subprocess tree, annotates your diagnosis (so it outlives the worktree and feeds the next round), and discards with the failureClass so the partial artifact is preserved. A STOP is a diagnosed, recoverable stop, never a silent kill.',
    'Return {briefHints:[...], alerts:[...], stops:[...]}. briefHints feed the NEXT round\'s briefs; alerts surface to the user; each stop triggers the gated enforcer (and its fixHint also feeds the next brief). All-empty is fine — most ticks should be quiet.',
  ].join('\n')
}

// Per-brief lane: implement -> pre-verify <-> revise loop -> run -> post-audit, repeated up to the
// iteration budget (deepening the branch each time a committed improver lands). The independent
// evo:verifier gates EACH run for design-time cheating BEFORE the experiment is evaluated; its
// findings are fed back to a revise agent on the same experiment until it passes or is discarded.
//
// Lane decomposition (decompose only at CONTEXT SEAMS): build+eval are a SINGLE agent — `run`
// produces the artifact and evaluates it end-to-end (one coherent context, no handoff mid-build).
// The only split is `implement` (write the edit) vs `run`, separated by the read-only evo:verifier
// seam — a genuinely different concern (adversarial diff audit, different agentType/model) that has
// to interpose between the edit and the expensive run. The two share state by REFERENCE (the
// worktree on disk), not by passing a context window, and BOTH receive the capsule (category skills
// + known learnings via capsuleLines), so neither reverts to base-model defaults. Merging implement
// into run would erase the verifier gate for no real gain, since the code already lives in the
// worktree the run agent reads.
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
    const r = await agent(runPrompt(impl.expId, state), { schema: SUBAGENT_RESULT, phase: 'Optimize', label: `run:${impl.expId}` })
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
let lastBestScore = null       // latest best score, surfaced to the concurrent analyst thread
let done = false               // set when the optimize loop ends -> stops the analyst thread
const analystSignals = []      // briefHints the analyst pushes; drained into the next round's brief

log(`evo-optimize start: subagents=${WIDTH} budget=${ITER} stall=${LIMIT} analyst=${ANALYST_ENABLED ? ANALYST_MODEL : 'off'} | argsType=${typeof args} A.subagents=${A.subagents} A.budget=${A.budget} A.stall=${A.stall}`)

// The optimize round loop (runs concurrently with analystLoop via Promise.all).
async function optimizeLoop() {
  while (stall < LIMIT) {
    round += 1

    phase('Orient')
    const state = await agent(statePrompt(), { schema: STATE, agentType: 'Explore', model: 'sonnet', phase: 'Orient', label: `state:r${round}` })
    lastBestScore = state.bestScore
    if (state.bestScore === state.ceiling) { log(`ceiling reached (best=${state.bestScore}) — stopping`); break }
    const parents = (state.frontier || []).slice(0, WIDTH)
    if (parents.length === 0) { log('no explorable frontier nodes — stopping'); break }

    // N1 + N1.5 — mandatory parallel scan + structural aggregation (barrier). Scan runs EVERY round
    // (hard rule); when there are no evaluated-undecided nodes yet (round 1) it falls back to the
    // committed frontier so at least one scan agent still runs before briefs.
    phase('Scan')
    const evaluatedIds = state.evaluatedIds || []
    const frontierIds = (state.frontier || []).map((f) => f.id).filter(Boolean)
    const scanTargets = evaluatedIds.length ? evaluatedIds : frontierIds
    const batches = chunk(scanTargets, SCAN_BATCH)
    const scanThunks = batches.map((b) => () => agent(scanBrief(b), { schema: FINDINGS, agentType: 'Explore', phase: 'Scan', label: `scan ${b.length}: ${batchLabel(b)}` }))
    const aggregateIds = [...new Set([...evaluatedIds, ...frontierIds])]
    const aggThunk = aggregateIds.length
      ? [() => agent(aggregatePrompt(aggregateIds), { schema: PATTERNS, agentType: 'Explore', phase: 'Scan', label: 'aggregate' })]
      : []
    const scanResults = (await parallel([...scanThunks, ...aggThunk])).filter(Boolean)
    const findings = scanResults.flatMap((r) => (r && r.findings) ? r.findings : [])
    const patterns = scanResults.flatMap((r) => (r && r.patterns) ? r.patterns : [])

    // N1.7 — research escalation (6b): on stall (before the hard limit) or every ~5 commits, fire the
    // three ideators in parallel. parallel() blocks until all return (proposals land before briefing).
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

    // N2 — brief writer: reconciles ideator proposals (6c), acts on axis-warning, and folds in any
    // live analyst hints accumulated since the last round; JS diversity dedupe afterwards.
    phase('Brief')
    const analystHints = analystSignals.splice(0)
    const briefOut = await agent(briefPrompt(state, findings, patterns, parents, ideated, analystHints), { schema: BRIEFS, phase: 'Brief', label: `briefs:r${round}` })
    const briefs = dedupeBriefs((briefOut && briefOut.briefs) || [])
    if (briefs.length === 0) { log('no briefs produced — stopping'); break }

    // N3..N4 — fan out one lane per brief; each lane: implement -> pre-verify<->revise -> run -> post-audit.
    const results = (await parallel(briefs.map((b) => () => runBrief(b, state)))).filter(Boolean)

    // N5 — collect: prune dead lineages, record notes.
    phase('Collect')
    await agent(collectPrompt(results, round), { phase: 'Collect', label: `collect:r${round}` })

    // Loop control: stall resets only when this round produced a VERIFIED committed score that beats
    // the PRIOR BEST in the metric direction (a beat-its-own-parent commit is branch progress, not a
    // new best, and does NOT reset stall). No budget in the condition.
    const dir = state.direction || 'max'
    const gains = results
      .filter((r) => r.committedImprover && r.valid !== false && typeof r.bestScore === 'number')
      .map((r) => r.bestScore)
    const roundBest = gains.length ? (dir === 'min' ? Math.min(...gains) : Math.max(...gains)) : null
    const improved = roundBest !== null && (dir === 'min' ? roundBest < state.bestScore : roundBest > state.bestScore)
    stall = improved ? 0 : stall + 1
    if (improved) ideatedThisStall = false
    log(`round ${round}: improved=${improved} roundBest=${roundBest} prevBest=${state.bestScore} stall=${stall}/${LIMIT} spent=${budget.spent()}`)
  }
  done = true
  // Wake any in-flight analyst tick now (its `sleep` can't see the in-memory `done`): the sentinel
  // makes the tick's interruptible wait exit within ~ANALYST_HOP_S instead of running the full interval.
  if (ANALYST_ENABLED) await agent(`mkdir -p .evo && : > ${DONE_SENTINEL} && echo signalled`, { phase: 'Collect', label: 'signal:optimize-done' })
  log(`optimize loop finished after ${round} round(s), final stall=${stall}/${LIMIT}`)
  return { rounds: round, finalStall: stall }
}

// Concurrent analyst thread (P1-sliver/P2-P5/P7): an independent, self-paced Opus observer that runs
// DURING rounds (not per-round). Each tick is a FRESH agent (no cross-tick memory), so `reported`
// holds the dedup state in this closure. Work-quality findings -> analystSignals (next brief);
// runtime/host alerts -> the run log. Stops when optimizeLoop sets `done`.
// Gated ENFORCER for an analyst STOP: detect (analyst) and act (this agent) stay separate. Verifies
// the experiment is still active, then aborts its run (driver + subprocess tree), annotates the
// diagnosis (survives the worktree + feeds the next round via knownLearnings), and discards with the
// failure class so the partial artifact is preserved + classified. A STOP is a diagnosed, recoverable
// stop — never a silent kill.
function enforceStopPrompt(s) {
  return [
    `A concurrent analyst flagged experiment ${s.expId} as heading toward failure and recommends STOPPING it. You are the gated ENFORCER — read-only except for the three evo commands below; do NOT edit code or run training.`,
    `First VERIFY: run \`evo show ${s.expId}\`. Only proceed if its status is still \`active\`. If it is committed / evaluated / discarded / not found, do NOTHING and report skipped (it already resolved).`,
    `If still active, run in order:`,
    `  1. \`evo abort ${s.expId}\` — stop the evo run driver and its subprocess tree.`,
    `  2. annotate the diagnosis so it outlives the worktree and feeds the next round: \`evo annotate ${s.expId} "STOPPED (${s.failureClass}): ${s.reason} | FIX: ${s.fixHint}"\` (quote carefully).`,
    `  3. classify + preserve: \`evo discard ${s.expId} --force --failure-class ${s.failureClass} --reason "analyst stop: ${s.reason}"\` (--force because abort already killed the driver; declared artifacts are preserved).`,
    `Report what you did (aborted / annotated / discarded) or that you skipped because it was no longer active. This is a diagnosed, recoverable stop, not a crash.`,
  ].join('\n')
}

async function analystLoop() {
  if (!ANALYST_ENABLED) return
  const reported = []   // closure memory across the stateless ticks (caps re-alerting)
  let t = 0
  let fails = 0   // consecutive tick failures; trips the self-disable below
  while (!done) {
    t += 1
    // The analyst is purely advisory and read-only: a failed tick must NEVER reject this loop and
    // abort the optimizer. Swallow any tick error, log it, and continue (or exit if `done` flipped).
    let tick = null
    try {
      tick = await agent(analystPrompt({ round, stall, bestScore: lastBestScore }, ANALYST_INTERVAL_S, reported.slice(-30)), {
        agentType: 'Explore', model: ANALYST_MODEL, schema: ANALYST_FINDINGS, phase: 'Analyst', label: `analyst#${t}`,
      })
    } catch (e) {
      log(`ANALYST tick #${t} errored (ignored, optimize unaffected): ${(e && e.message) || e}`)
    }
    if (tick) {
      fails = 0   // a real tick resets the failure streak
      for (const h of (tick.briefHints || [])) { analystSignals.push(h); reported.push(h) }
      for (const a of (tick.alerts || [])) { log(`ANALYST ALERT: ${a}`); reported.push(a) }
      // STOP recommendations: hand each to a gated enforcer (detect/act separation). The fix also
      // feeds the next round's brief so the loop corrects rather than just abandons.
      for (const s of (tick.stops || [])) {
        if (!s || !s.expId) continue
        const stopKey = `stop:${s.expId}`
        if (reported.includes(stopKey)) continue   // never re-enforce the same experiment
        reported.push(stopKey)
        log(`ANALYST STOP: ${s.expId} [${s.failureClass}] ${s.reason}`)
        analystSignals.push(`Experiment ${s.expId} was stopped (${s.failureClass}): ${s.reason} — next: ${s.fixHint}`)
        try {
          await agent(enforceStopPrompt(s), { phase: 'Analyst', label: `enforce-stop:${s.expId}` })
        } catch (e) {
          log(`ANALYST enforce-stop ${s.expId} errored (ignored): ${(e && e.message) || e}`)
        }
      }
    } else if (++fails >= ANALYST_MAX_FAILS) {
      // The pacing wait lives INSIDE the agent, so a tick that fails before sleeping (e.g. a schema
      // reject) leaves nothing to pace the retry — left unchecked the loop hot-spins agents. The
      // analyst is optional, so after a short streak of failures, disable it for the rest of the run.
      log(`ANALYST disabled after ${fails} consecutive failed ticks — optimize continues without it.`)
      return
    }
  }
}

// Clear any stale sentinel from a prior run BEFORE the threads start, else the analyst's first wait
// would see it and exit instantly. The script can't touch the filesystem itself, so an agent does it.
if (ANALYST_ENABLED) await agent(`rm -f ${DONE_SENTINEL}; echo cleared`, { phase: 'Orient', label: 'init:clear-sentinel' })

// optimizeLoop is the run's result; analystLoop is advisory. The `.catch` is the definitive guard that
// the observer thread can NEVER reject the combined promise and fail an otherwise-good optimize run.
const [optimizeResult] = await Promise.all([
  optimizeLoop(),
  analystLoop().catch((e) => log(`ANALYST thread exited abnormally (ignored): ${(e && e.message) || e}`)),
])
return optimizeResult

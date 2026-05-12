// Test runner — invokes the TS drainSession over a fixture-built run dir
// and prints the result as JSON for the parity test to compare against
// the Python implementation.
//
// Usage: bun run _runner.ts <runDir> <sessionId>

import { drainSession } from "../../../plugins/evo/src/evo/opencode_plugin/drain.ts"

const runDir = process.argv[2]
const sessionId = process.argv[3]

if (!runDir || !sessionId) {
  console.error("usage: bun run _runner.ts <runDir> <sessionId>")
  process.exit(2)
}

const result = drainSession(runDir, sessionId)
process.stdout.write(JSON.stringify(result))

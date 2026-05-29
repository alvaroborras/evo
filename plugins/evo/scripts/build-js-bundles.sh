#!/usr/bin/env bash
# Rebuild the committed JS host bundles from their TS sources.
# The bundles (opencode/openclaw/pi/native) are checked-in artifacts; CI's
# sync-from-source.sh copies them but does NOT build. Run this after editing
# any *_plugin/*.ts, then commit the regenerated bundles.
# Requires: bun. Builds from inside each plugin dir so the bundle's header
# comments match the committed style (relative paths) -> clean diffs.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../src/evo" && pwd)"
build() { ( cd "$ROOT/$1" && bun build "$2" --target=node --format=esm > "$3" ) && echo "built $1/$3"; }
build opencode_plugin index.ts evo.bundle.js
build openclaw_plugin index.ts evo.bundle.js
build openclaw_plugin pi-entry.ts pi.bundle.js
build openclaw_plugin/native index.ts index.js
echo "done. Now run: bash plugins/evo/npm/scripts/sync-from-source.sh"

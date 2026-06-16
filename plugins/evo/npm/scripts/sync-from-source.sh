#!/usr/bin/env bash
# Copy the canonical evo bundle + skills from plugins/evo/ into this
# package. Source of truth lives in plugins/evo/; this package is a
# distribution surface for npm. CI runs this before `npm publish` so
# the published tarball always matches the tagged release content.
#
# Safe to run locally too — the committed extensions/ and skills/
# under plugins/evo/npm/ should already match the source. Re-running
# just rewrites them.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../../.." && pwd)"
PKG="$ROOT/plugins/evo/npm"
SRC="$ROOT/plugins/evo"

# Bundle JS — copy the pi-specific bundle (built from pi-entry.ts).
# Both bundles share factory.ts; only the host string baked in differs
# ("pi" vs "openclaw"). Correctly tagging pi sessions in the registry
# is the fix for the pre-0.4.4 pi-tagged-as-openclaw bug.
mkdir -p "$PKG/extensions/evo"
cp "$SRC/src/evo/openclaw_plugin/pi.bundle.js" "$PKG/extensions/evo/index.js"
echo "synced extension: $PKG/extensions/evo/index.js (host=pi)"

# Skills — pi discovers each subdir under skills/ as a separate skill.
for name in discover optimize subagent infra-setup report ship; do
    dest="$PKG/skills/$name"
    rm -rf "$dest"
    mkdir -p "$dest"
    cp -R "$SRC/skills/$name/." "$dest/"
    # Strip Python bytecode cache dirs — they get created when reference
    # scripts are run from the source tree and pollute the npm tarball.
    find "$dest" -type d -name "__pycache__" -exec rm -rf {} + 2>/dev/null || true
    echo "synced skill: $dest"
done

echo "done"

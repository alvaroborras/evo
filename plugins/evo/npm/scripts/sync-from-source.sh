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

# Bundle JS — the openclaw_plugin/evo.bundle.js targets pi's
# ExtensionAPI directly; pi is the upstream SDK openclaw embeds.
mkdir -p "$PKG/extensions/evo"
cp "$SRC/src/evo/openclaw_plugin/evo.bundle.js" "$PKG/extensions/evo/index.js"
echo "synced extension: $PKG/extensions/evo/index.js"

# Skills — pi discovers each subdir under skills/ as a separate skill.
for name in discover optimize subagent infra-setup; do
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

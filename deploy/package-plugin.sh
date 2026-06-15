#!/usr/bin/env bash
# Rebuild dist/behdashtik-mirror-connector.zip from the current plugin source.
# Run manually or called automatically by the pre-commit hook.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PLUGIN_SRC="$REPO_ROOT/wordpress-plugin/behdashtik-mirror-connector"
DIST_DIR="$REPO_ROOT/dist"
ZIP_PATH="$DIST_DIR/behdashtik-mirror-connector.zip"

mkdir -p "$DIST_DIR"

# Build inside a temp dir so the zip entry path is behdashtik-mirror-connector/…
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

cp -r "$PLUGIN_SRC" "$TMP/behdashtik-mirror-connector"

# Remove any local dev / OS noise
find "$TMP" -name '.DS_Store' -delete
find "$TMP" -name '__pycache__' -exec rm -rf {} + 2>/dev/null || true

(cd "$TMP" && zip -rq "$ZIP_PATH" behdashtik-mirror-connector/)

echo "Packaged: $ZIP_PATH ($(du -sh "$ZIP_PATH" | cut -f1))"

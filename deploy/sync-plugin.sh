#!/usr/bin/env bash
# sync-plugin.sh — deploy the Behdashtik Mirror Connector plugin
#
# Usage:
#   ./deploy/sync-plugin.sh                          # sync to dev (safe default)
#   ./deploy/sync-plugin.sh --dry-run                # preview what would change on dev
#   ./deploy/sync-plugin.sh --deploy-to-production   # sync to production (explicit opt-in)
#   ./deploy/sync-plugin.sh --deploy-to-production --dry-run
#
# The script refuses to touch the production path without --deploy-to-production.
# Update PROD_DEST below when the production site is configured on this server.

set -euo pipefail

# ---------------------------------------------------------------------------
# Path configuration — update PROD_DEST when production site is set up
# ---------------------------------------------------------------------------
PLUGIN_SRC="$(cd "$(dirname "$0")/.." && pwd)/wordpress-plugin/behdashtik-mirror-connector"
DEV_DEST="/var/www/dev.behdashtik.ir/wp-content/plugins/behdashtik-mirror-connector"
PROD_DEST="/var/www/behdashtik.ir/wp-content/plugins/behdashtik-mirror-connector"  # placeholder

# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------
DRY_RUN=false
DEPLOY_TO_PRODUCTION=false

for arg in "$@"; do
  case "$arg" in
    --dry-run)                DRY_RUN=true ;;
    --deploy-to-production)   DEPLOY_TO_PRODUCTION=true ;;
    *)
      echo "Unknown flag: $arg"
      echo "Usage: $0 [--dry-run] [--deploy-to-production]"
      exit 1
      ;;
  esac
done

# ---------------------------------------------------------------------------
# Production guard — must be explicit; never the accidental default
# ---------------------------------------------------------------------------
if [[ "$DEPLOY_TO_PRODUCTION" == "true" ]]; then
  DEST="$PROD_DEST"
  TARGET_LABEL="PRODUCTION"

  if [[ "$DRY_RUN" != "true" ]]; then
    echo ""
    echo "  *** DEPLOYING TO PRODUCTION: $PROD_DEST ***"
    echo ""
    read -rp "  Type 'yes' to confirm: " confirm
    if [[ "$confirm" != "yes" ]]; then
      echo "Aborted."
      exit 1
    fi
  fi
else
  DEST="$DEV_DEST"
  TARGET_LABEL="dev"

  # Safety: if the destination path looks like the production path, refuse
  if [[ "$DEST" == "$PROD_DEST" ]]; then
    echo "ERROR: DEV_DEST and PROD_DEST resolve to the same path."
    echo "       Check the path configuration at the top of this script."
    exit 1
  fi
fi

# ---------------------------------------------------------------------------
# Verify source exists
# ---------------------------------------------------------------------------
if [[ ! -d "$PLUGIN_SRC" ]]; then
  echo "ERROR: Plugin source not found: $PLUGIN_SRC"
  exit 1
fi

# ---------------------------------------------------------------------------
# Sync
# ---------------------------------------------------------------------------
RSYNC_FLAGS=( -av --checksum --delete )
if [[ "$DRY_RUN" == "true" ]]; then
  RSYNC_FLAGS+=( --dry-run )
  echo "[dry-run] Would sync to $TARGET_LABEL: $DEST"
else
  echo "Syncing to $TARGET_LABEL: $DEST"
fi

rsync "${RSYNC_FLAGS[@]}" "$PLUGIN_SRC/" "$DEST/"

if [[ "$DRY_RUN" != "true" ]]; then
  chown -R www-data:www-data "$DEST/"

  # Flush WP OPcache / rewrite rules if wp-cli is available
  WP_PATH="$(dirname "$(dirname "$DEST")")"
  WP_PATH="$(dirname "$(dirname "$WP_PATH")")"  # up from plugins/plugin-name to wp root
  if command -v wp &>/dev/null && [[ -f "$WP_PATH/wp-config.php" ]]; then
    wp --path="$WP_PATH" --allow-root eval 'opcache_reset();' 2>/dev/null || true
    wp --path="$WP_PATH" --allow-root rewrite flush 2>/dev/null || true
    echo "OPcache flushed and rewrite rules refreshed."
  fi

  echo "Done. Plugin deployed to $TARGET_LABEL."
fi

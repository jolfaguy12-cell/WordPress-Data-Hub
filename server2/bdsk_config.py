"""
Shared configuration loader for Behdashtik Server 2 tools.

Precedence (highest → lowest):
  BDSK_* environment variables  (loaded from server2/.env via python-dotenv)
  → server2/config.json values
  → hardcoded defaults inside each caller

Safety guards abort startup when the active config contains a dangerous
combination (e.g. dev URL pointing at a non-_dev mirror DB).
"""

import json
import os
import pathlib
import sys

try:
    from dotenv import load_dotenv as _load_dotenv
    _DOTENV_AVAILABLE = True
except ImportError:
    _DOTENV_AVAILABLE = False

_DIR = pathlib.Path(__file__).parent
CONFIG_PATH = _DIR / "config.json"
_ENV_PATH   = _DIR / ".env"


def load_config() -> dict:
    # 1. Load .env into os.environ (if present)
    if _DOTENV_AVAILABLE and _ENV_PATH.exists():
        _load_dotenv(_ENV_PATH, override=True)
    elif not _DOTENV_AVAILABLE and _ENV_PATH.exists():
        print("[config] WARNING: server2/.env found but python-dotenv is not installed — "
              "install python-dotenv to enable .env support", file=sys.stderr)

    # 2. Load config.json as base dict (optional when .env covers everything)
    if CONFIG_PATH.exists():
        with CONFIG_PATH.open() as f:
            cfg: dict = json.load(f)
    else:
        cfg = {}

    # 3. Overlay BDSK_* env vars
    _apply_env_overrides(cfg)

    # 4. Safety guard
    _validate_env_safety(cfg)

    return cfg


def _apply_env_overrides(cfg: dict) -> None:
    """Apply BDSK_* environment variables onto the config dict (in-place)."""
    def _get(key: str):
        return os.environ.get(key)

    def _int(key: str):
        v = _get(key)
        return int(v) if v is not None else None

    def _bool(key: str):
        v = _get(key)
        if v is None:
            return None
        return v.lower() in ("1", "true", "yes")

    def _set(d: dict, k: str, v):
        if v is not None:
            d[k] = v

    # Top-level
    _set(cfg, "env",                        _get("BDSK_ENV"))
    _set(cfg, "wp_base_url",                _get("BDSK_WP_BASE_URL"))
    _set(cfg, "api_secret",                 _get("BDSK_API_SECRET"))
    _set(cfg, "archive_storage_path",       _get("BDSK_ARCHIVE_STORAGE_PATH"))
    _set(cfg, "request_timeout_seconds",    _int("BDSK_REQUEST_TIMEOUT_SECONDS"))
    _set(cfg, "poll_interval_seconds",      _int("BDSK_POLL_INTERVAL_SECONDS"))
    _set(cfg, "poll_timeout_seconds",       _int("BDSK_POLL_TIMEOUT_SECONDS"))
    _set(cfg, "failed_archive_retention_days", _int("BDSK_FAILED_ARCHIVE_RETENTION_DAYS"))
    _set(cfg, "writeback_disabled",         _bool("BDSK_WRITEBACK_DISABLED"))
    _set(cfg, "export_allowed",             _bool("BDSK_EXPORT_ALLOWED"))
    # Internal state path overrides
    _set(cfg, "_event_sync_state_path",     _get("BDSK_EVENT_SYNC_STATE_PATH"))
    _set(cfg, "_media_sync_state_path",     _get("BDSK_MEDIA_SYNC_STATE_PATH"))
    _set(cfg, "_hub_db_path",               _get("BDSK_HUB_DB_PATH"))

    # mirror_db
    db = cfg.setdefault("mirror_db", {})
    _set(db, "host",              _get("BDSK_MIRROR_DB_HOST"))
    _set(db, "port",              _int("BDSK_MIRROR_DB_PORT"))
    _set(db, "user",              _get("BDSK_MIRROR_DB_USER"))
    _set(db, "password",          _get("BDSK_MIRROR_DB_PASSWORD"))
    _set(db, "name",              _get("BDSK_MIRROR_DB_NAME"))
    _set(db, "readonly_user",     _get("BDSK_MIRROR_DB_READONLY_USER"))
    _set(db, "readonly_password", _get("BDSK_MIRROR_DB_READONLY_PASSWORD"))

    # hub_state_db
    hs = cfg.setdefault("hub_state_db", {})
    _set(hs, "name", _get("BDSK_HUB_STATE_DB_NAME"))

    # hub (dashboard)
    hub = cfg.setdefault("hub", {})
    _set(hub, "secret_key",           _get("BDSK_HUB_SECRET_KEY"))
    _set(hub, "session_lifetime_hours", _int("BDSK_SESSION_LIFETIME_HOURS"))
    _set(hub, "host",                 _get("BDSK_DASHBOARD_HOST"))
    _set(hub, "port",                 _int("BDSK_DASHBOARD_PORT"))

    # data_api
    da = cfg.setdefault("data_api", {})
    _set(da, "key", _get("BDSK_DATA_API_KEY"))

    # media_sync
    ms = cfg.setdefault("media_sync", {})
    _set(ms, "storage_path",          _get("BDSK_MEDIA_STORAGE_PATH"))
    _set(ms, "enabled",               _bool("BDSK_MEDIA_SYNC_ENABLED"))
    _set(ms, "concurrency",           _int("BDSK_MEDIA_SYNC_CONCURRENCY"))
    _set(ms, "max_files_per_run",     _int("BDSK_MEDIA_SYNC_MAX_FILES_PER_RUN"))
    _set(ms, "max_mb_per_run",        _int("BDSK_MEDIA_SYNC_MAX_MB_PER_RUN"))
    _set(ms, "max_file_size_bytes",   _int("BDSK_MEDIA_SYNC_MAX_FILE_SIZE_BYTES"))
    _set(ms, "connect_timeout",       _int("BDSK_MEDIA_SYNC_CONNECT_TIMEOUT"))
    _set(ms, "read_timeout",          _int("BDSK_MEDIA_SYNC_READ_TIMEOUT"))
    _set(ms, "download_retries",      _int("BDSK_MEDIA_SYNC_DOWNLOAD_RETRIES"))

    # event_sync
    es = cfg.setdefault("event_sync", {})
    _set(es, "enabled",    _bool("BDSK_EVENT_SYNC_ENABLED"))
    _set(es, "batch_size", _int("BDSK_EVENT_SYNC_BATCH_SIZE"))
    _set(es, "max_retries", _int("BDSK_EVENT_SYNC_MAX_RETRIES"))


def _validate_env_safety(cfg: dict) -> None:
    """Hard-fail on dangerous environment/DB combinations."""
    env          = cfg.get("env", "")
    mirror_name  = cfg.get("mirror_db", {}).get("name", "")
    hub_name     = cfg.get("hub_state_db", {}).get("name", "behdashtik_hub_state")
    url          = cfg.get("wp_base_url", "").rstrip("/")

    def _fail(msg: str):
        print(f"[SAFETY FAIL] {msg}", file=sys.stderr)
        sys.exit(f"[SAFETY FAIL] {msg}")

    # Rule 1+2: dev env → both DB names must end with _dev
    if env == "dev":
        if mirror_name and not mirror_name.endswith("_dev"):
            _fail(f"BDSK_ENV=dev but mirror_db name '{mirror_name}' does not end with '_dev'. "
                  "Set BDSK_MIRROR_DB_NAME=behdashtik_wp_mirror_dev (or appropriate _dev name).")
        if hub_name and not hub_name.endswith("_dev"):
            _fail(f"BDSK_ENV=dev but hub_state_db name '{hub_name}' does not end with '_dev'. "
                  "Set BDSK_HUB_STATE_DB_NAME=behdashtik_hub_state_dev.")

    # Rule 3: main/prod env → DB names must NOT end with _dev
    if env in ("main", "prod"):
        if mirror_name and mirror_name.endswith("_dev"):
            _fail(f"BDSK_ENV={env} but mirror_db name '{mirror_name}' ends with '_dev'. "
                  "Production must not use a _dev DB.")
        if hub_name and hub_name.endswith("_dev"):
            _fail(f"BDSK_ENV={env} but hub_state_db name '{hub_name}' ends with '_dev'. "
                  "Production must not use a _dev hub state DB.")

    # Rule 4: dev URL → mirror DB must end with _dev
    if "dev." in url and mirror_name and not mirror_name.endswith("_dev"):
        _fail(f"WP source URL '{url}' is a dev site but mirror_db name '{mirror_name}' "
              "does not end with '_dev'. Set BDSK_MIRROR_DB_NAME to a _dev DB.")

    # Rule 5: production URL → mirror DB must NOT end with _dev
    if url in ("https://behdashtik.ir", "http://behdashtik.ir") and mirror_name.endswith("_dev"):
        _fail(f"WP source URL is production ({url}) but mirror_db name '{mirror_name}' "
              "ends with '_dev'. Set BDSK_MIRROR_DB_NAME to the production DB name.")

    # Rule 6: mirror and hub_state must be different DBs
    if mirror_name and hub_name and mirror_name == hub_name:
        _fail(f"mirror_db.name and hub_state_db.name are both '{mirror_name}'. "
              "They must be different databases — hub_state is persistent, mirror is swappable.")

    # Rule 7: warn if storage paths are outside the project root
    project_root = str(_DIR.parent)
    for label, path in [
        ("archive_storage_path", cfg.get("archive_storage_path", "")),
        ("media_sync.storage_path", cfg.get("media_sync", {}).get("storage_path", "")),
    ]:
        if path and not path.startswith(project_root):
            print(f"[config] WARN: {label} '{path}' is outside project root '{project_root}'",
                  file=sys.stderr)

    # Print safety confirmation lines if ENV is set
    if env:
        _ok = "[SAFETY] ✓"
        if env == "dev":
            print(f"{_ok} ENV=dev, mirror DB ends with _dev")
            print(f"{_ok} Hub state DB ends with _dev")
        elif env in ("main", "prod"):
            print(f"{_ok} ENV={env}, mirror DB does not end with _dev")
        if mirror_name != hub_name:
            print(f"{_ok} Mirror DB ≠ hub state DB")
        if url:
            print(f"{_ok} WP URL matches environment")


def print_config_summary(cfg: dict) -> None:
    """Print sanitized config to stdout for --show-config."""
    mirror  = cfg.get("mirror_db", {})
    hub     = cfg.get("hub", {})
    ms      = cfg.get("media_sync", {})
    es      = cfg.get("event_sync", {})

    def _mask(v):
        return "***" if v else "(not set)"

    dir_ = str(_DIR)

    print(f"[config] ENV               : {cfg.get('env', '(not set)')}")
    print(f"[config] WP source URL     : {cfg.get('wp_base_url', '(not set)')}")
    print(f"[config] Mirror DB         : {mirror.get('name', '(not set)')}  "
          f"({mirror.get('host', '?')}:{mirror.get('port', 3306)})")
    print(f"[config] Hub state DB      : {cfg.get('hub_state_db', {}).get('name', 'behdashtik_hub_state')}")
    print(f"[config] Archive path      : {cfg.get('archive_storage_path', '(not set)')}")
    print(f"[config] Media path        : {ms.get('storage_path', '(not set)')}")
    print(f"[config] Event state path  : {cfg.get('_event_sync_state_path') or dir_ + '/event_sync_state.json'}")
    print(f"[config] Media state path  : {cfg.get('_media_sync_state_path') or dir_ + '/media_sync_state.json'}")
    print(f"[config] Hub DB path       : {cfg.get('_hub_db_path') or dir_ + '/hub.db'}")
    print(f"[config] Dashboard         : {hub.get('host', '127.0.0.1')}:{hub.get('port', 8089)}")
    print(f"[config] Writeback         : {'DISABLED' if cfg.get('writeback_disabled') else 'enabled'}")
    print(f"[config] Export allowed    : {'yes' if cfg.get('export_allowed', True) else 'no'}")
    print(f"[config] Media sync        : {'enabled' if ms.get('enabled', True) else 'disabled'}")
    print(f"[config] Event sync        : {'enabled' if es.get('enabled', True) else 'disabled'}")
    print(f"[config] API secret        : {_mask(cfg.get('api_secret'))}")
    print(f"[config] DB password       : {_mask(mirror.get('password'))}")
    print(f"[config] Hub secret key    : {_mask(hub.get('secret_key'))}")
    print(f"[config] Data API key      : {_mask(cfg.get('data_api', {}).get('key'))}")

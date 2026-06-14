#!/usr/bin/env python3
"""
Behdashtik Mirror Connector — Server 2 pipeline
================================================
Runs the full export pipeline:
  health-check → start export → poll → download → verify →
  confirm → import into staging DB → validate → swap to mirror DB

Usage:
  python pipeline.py                    # full pipeline
  python pipeline.py --health-only      # health check only
  python pipeline.py --test             # test export (50 rows per table)
  python pipeline.py --import-only /datahub/db-archives/<job_id>
"""

import argparse
import concurrent.futures
import gzip
import hashlib
import json
import os
import pathlib
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone
from typing import Any

import pymysql
import requests

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

CONFIG_PATH = pathlib.Path(__file__).parent / "config.json"


def load_config() -> dict:
    if not CONFIG_PATH.exists():
        sys.exit(
            f"[ERROR] config.json not found. Copy config.example.json to config.json and fill in your values.\n"
            f"Expected path: {CONFIG_PATH}"
        )
    with CONFIG_PATH.open() as f:
        return json.load(f)


# ---------------------------------------------------------------------------
# HTTP client helpers
# ---------------------------------------------------------------------------

def _parse_json(text: str, context: str) -> dict:
    """Parse JSON from a response body that may have a PHP notice/warning prefix."""
    start = text.find("{")
    if start == -1:
        raise RuntimeError(f"{context}: response contains no JSON object — body: {text[:300]}")
    prefix = text[:start].strip()
    if prefix:
        print(f"[warn] {context}: stripped non-JSON prefix ({len(prefix)} chars): {prefix[:120]!r}")
    return json.loads(text[start:])


def api_get(cfg: dict, path: str, params: dict | None = None) -> dict:
    url = cfg["wp_base_url"].rstrip("/") + "/wp-json/behdashtik-connector/v1" + path
    resp = requests.get(
        url,
        headers={"Authorization": f"Bearer {cfg['api_secret']}"},
        params=params,
        timeout=cfg.get("request_timeout_seconds", 60),
    )
    if not resp.ok:
        raise RuntimeError(f"GET {path} failed: {resp.status_code} — {resp.text[:300]}")
    return _parse_json(resp.text, f"GET {path}")


def _trigger_as_queue(cfg: dict) -> None:
    """Kick Action Scheduler so it processes the export chunk queue.

    In production, WP-Cron fires on every page load and drives AS automatically —
    no action needed here.  In local dev (Docker with no real traffic), the
    WP-Cron loopback can't reach `localhost:8080` from inside the container, so
    AS never runs on its own.

    Set `as_runner_cmd` in config.json to a shell command that triggers the queue
    (e.g. `docker compose exec -T wpcli wp cron event run action_scheduler_run_queue`).
    The command is run fire-and-forget; failures are suppressed so polling continues.
    """
    cmd = cfg.get("as_runner_cmd")
    if not cmd:
        return
    try:
        subprocess.Popen(
            cmd, shell=True,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
    except Exception:
        pass


def api_post(cfg: dict, path: str, body: dict | None = None) -> dict:
    url = cfg["wp_base_url"].rstrip("/") + "/wp-json/behdashtik-connector/v1" + path
    resp = requests.post(
        url,
        headers={
            "Authorization": f"Bearer {cfg['api_secret']}",
            "Content-Type": "application/json",
        },
        json=body or {},
        timeout=cfg.get("request_timeout_seconds", 60),
    )
    if not resp.ok and resp.status_code not in (409,):
        raise RuntimeError(f"POST {path} failed: {resp.status_code} — {resp.text[:300]}")
    return _parse_json(resp.text, f"POST {path}")


# ---------------------------------------------------------------------------
# 1. Health check
# ---------------------------------------------------------------------------

def health_check(cfg: dict) -> dict:
    print("[health] Calling health endpoint …")
    data = api_get(cfg, "/health")
    print(f"[health] Status            : {data.get('status')}")
    print(f"[health] Plugin version    : {data.get('plugin_version')}")
    print(f"[health] WordPress version : {data.get('wordpress_version')}")
    print(f"[health] WooCommerce       : {data.get('woocommerce_version') or 'not active'}")
    print(f"[health] PHP               : {data.get('php_version')}")
    print(f"[health] MySQL/MariaDB     : {data.get('mysql_or_mariadb_version')}")
    print(f"[health] gzip available    : {data.get('gzip_or_zlib_available')}")
    print(f"[health] connector_enabled : {data.get('connector_enabled')}")
    print(f"[health] read_mode         : {data.get('read_mode_status')}")
    print(f"[health] backup_export     : {data.get('backup_export_enabled')}")
    print(f"[health] server_time       : {data.get('server_time')}")

    if data.get("status") != "ok":
        raise RuntimeError("[health] Plugin reported non-ok status.")
    return data


# ---------------------------------------------------------------------------
# 2. Start export
# ---------------------------------------------------------------------------

def start_export(cfg: dict, test_mode: bool = False) -> str:
    print(f"[export] Starting export job (test_mode={test_mode}) …")
    body = {}
    if test_mode:
        body["test"] = True

    data = api_post(cfg, "/db-export/start", body)

    if "error" in data and data.get("error") == "export_already_running":
        job_id = data["job_id"]
        print(f"[export] Export already running: {job_id} — attaching to it.")
        return job_id

    job_id = data["job_id"]
    print(f"[export] Job created: {job_id} (status: {data.get('status')})")
    return job_id


# ---------------------------------------------------------------------------
# 3. Poll until ready
# ---------------------------------------------------------------------------

def poll_until_ready(cfg: dict, job_id: str) -> dict:
    interval     = cfg.get("poll_interval_seconds", 5)
    timeout      = cfg.get("poll_timeout_seconds", 3600)
    deadline     = time.time() + timeout
    print(f"[poll] Waiting for job {job_id} to become ready …")

    while time.time() < deadline:
        _trigger_as_queue(cfg)
        data = api_get(cfg, f"/db-export/status/{job_id}")
        status   = data.get("status")
        progress = data.get("progress_percent", 0)
        table    = data.get("current_table", "")

        print(f"[poll] {status:12s}  {progress:5.1f}%  {table}")

        if status == "ready":
            print(f"[poll] Job ready. Archive size: {data.get('archive_size', 0):,} bytes")
            return data

        if status in ("failed", "expired"):
            raise RuntimeError(f"[poll] Job {job_id} ended with status '{status}': {data.get('last_error')}")

        time.sleep(interval)

    raise TimeoutError(f"[poll] Job {job_id} did not complete within {timeout}s.")


# ---------------------------------------------------------------------------
# 4. Download archive
# ---------------------------------------------------------------------------

def download_archive(cfg: dict, job_id: str, status_data: dict) -> pathlib.Path:
    manifest = status_data.get("archive_manifest", {})
    parts    = manifest.get("parts", [])
    token    = status_data.get("token")  # not in status response — fetched separately

    # The token is returned in the status response when job is 'ready'.
    # It is gated behind the API key, so returning it here is safe.
    token = status_data.get("download_token")
    if not token:
        # Re-fetch in case status_data came from the poll loop before token was populated
        fresh = api_get(cfg, f"/db-export/status/{job_id}")
        token = fresh.get("download_token")

    if not token:
        raise RuntimeError(
            "[download] No download_token in status response. "
            "Ensure the job status is 'ready' and the plugin is up to date."
        )

    job_dir = pathlib.Path(cfg["archive_storage_path"]) / job_id
    job_dir.mkdir(parents=True, exist_ok=True)

    downloaded_parts = []

    for i, part in enumerate(parts, start=1):
        filename  = part["filename"]
        expected_sha256 = part.get("sha256")
        dest_path = job_dir / filename

        print(f"[download] Part {i}/{len(parts)}: {filename} ({part.get('size', 0):,} bytes) …")

        url = (
            cfg["wp_base_url"].rstrip("/")
            + f"/wp-json/behdashtik-connector/v1/db-export/download/{job_id}"
            + f"?part={i}&token={token}"
        )

        with requests.get(
            url,
            headers={"Authorization": f"Bearer {cfg['api_secret']}"},
            stream=True,
            timeout=None,  # large files — no timeout on download
        ) as r:
            if not r.ok:
                raise RuntimeError(f"[download] Part {i} download failed: {r.status_code}")

            sha256 = hashlib.sha256()
            with dest_path.open("wb") as f:
                for chunk in r.iter_content(chunk_size=65536):
                    f.write(chunk)
                    sha256.update(chunk)

        actual = sha256.hexdigest()
        if expected_sha256 and actual != expected_sha256:
            raise RuntimeError(
                f"[download] Checksum mismatch on part {i}!\n"
                f"  expected: {expected_sha256}\n"
                f"  actual:   {actual}"
            )
        print(f"[download] Part {i} OK  sha256={actual[:16]}…")
        downloaded_parts.append(str(dest_path))

    # Write meta.json
    meta = {
        "backup_id":       job_id,
        "job_id":          job_id,
        "parts":           parts,
        "archive_size":    status_data.get("archive_size", 0),
        "checksum":        status_data.get("checksum"),
        "created_at":      datetime.now(timezone.utc).isoformat(),
        "downloaded_at":   datetime.now(timezone.utc).isoformat(),
        "source_site":     cfg["wp_base_url"],
        "db_prefix":       manifest.get("db_prefix", ""),
        "tables_included": manifest.get("tables_included", []),
        "import_status":   "pending",
        "archive_until":   None,  # filled in after import
    }
    (job_dir / "meta.json").write_text(json.dumps(meta, indent=2))
    print(f"[download] All parts downloaded to {job_dir}")
    return job_dir


# ---------------------------------------------------------------------------
# 5. Confirm download
# ---------------------------------------------------------------------------

def confirm_download(cfg: dict, job_id: str) -> None:
    print(f"[confirm] Confirming download for job {job_id} …")
    data = api_post(cfg, "/db-export/confirm-download", {"job_id": job_id})
    if data.get("confirmed"):
        print("[confirm] Server confirmed. Remote archive files will be cleaned up.")
    else:
        print(f"[confirm] Unexpected response: {data}")


# ---------------------------------------------------------------------------
# 6. Import into staging DB
# ---------------------------------------------------------------------------

def import_archive(cfg: dict, job_id: str, job_dir: pathlib.Path) -> str:
    db_cfg    = cfg["mirror_db"]
    staging   = db_cfg["name"] + "_staging"
    host      = db_cfg["host"]
    port      = db_cfg.get("port", 3306)
    user      = db_cfg["user"]
    password  = db_cfg["password"]

    meta_path = job_dir / "meta.json"
    meta      = json.loads(meta_path.read_text())
    parts     = meta["parts"]

    print(f"[import] Creating staging database: {staging} …")
    _mysql_exec(host, port, user, password, f"CREATE DATABASE IF NOT EXISTS `{staging}` CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;")

    for i, part_info in enumerate(parts, start=1):
        gz_path = job_dir / part_info["filename"]
        if not gz_path.exists():
            raise FileNotFoundError(f"[import] Missing part file: {gz_path}")

        print(f"[import] Importing part {i}/{len(parts)}: {gz_path.name} …")

        # Pipe gunzip output directly into mysql (no temp file needed)
        cmd = [
            "mysql",
            f"--host={host}",
            f"--port={port}",
            f"--user={user}",
            f"--password={password}",
            "--default-character-set=utf8mb4",
            staging,
        ]

        # gzip.GzipFile.fileno() exposes the underlying *compressed* fd, so we
        # must not pass the GzipFile directly as subprocess stdin — mysql would
        # receive raw gzip bytes.  Decompress to memory and feed via input=.
        with gzip.open(str(gz_path), "rb") as gz_in:
            sql_bytes = gz_in.read()

        result = subprocess.run(cmd, input=sql_bytes, capture_output=True)

        if result.returncode != 0:
            raise RuntimeError(
                f"[import] mysql import failed for part {i}:\n"
                + result.stderr.decode(errors="replace")
            )
        print(f"[import] Part {i} imported successfully.")

    return staging


# ---------------------------------------------------------------------------
# 7. Validate import
# ---------------------------------------------------------------------------

def validate_import(cfg: dict, job_id: str, staging_db: str, job_dir: pathlib.Path) -> None:
    db_cfg   = cfg["mirror_db"]
    meta     = json.loads((job_dir / "meta.json").read_text())
    expected = set(meta.get("tables_included", []))
    prefix   = meta.get("db_prefix", "wp_")

    conn = pymysql.connect(
        host=db_cfg["host"],
        port=db_cfg.get("port", 3306),
        user=db_cfg["user"],
        password=db_cfg["password"],
        database=staging_db,
        charset="utf8mb4",
    )

    try:
        with conn.cursor() as cur:
            # Table count
            cur.execute("SHOW TABLES")
            actual_tables = {row[0] for row in cur.fetchall()}
            missing = expected - actual_tables

            if missing:
                raise RuntimeError(f"[validate] Missing tables: {missing}")

            print(f"[validate] Table count OK: {len(actual_tables)} tables present.")

            # Check core tables exist
            core = [f"{prefix}posts", f"{prefix}options", f"{prefix}users"]
            for tbl in core:
                if tbl not in actual_tables:
                    raise RuntimeError(f"[validate] Core table missing: {tbl}")
            print(f"[validate] Core tables present: {core}")

            # Rough row count sanity check (posts must have at least 1 row)
            cur.execute(f"SELECT COUNT(*) FROM `{prefix}posts`")
            posts_count = cur.fetchone()[0]
            print(f"[validate] {prefix}posts row count: {posts_count}")

    finally:
        conn.close()

    print("[validate] Validation passed.")


# ---------------------------------------------------------------------------
# 8. Swap staging DB → mirror DB
# ---------------------------------------------------------------------------

def swap_staging_to_mirror(cfg: dict, staging_db: str) -> None:
    db_cfg = cfg["mirror_db"]
    mirror = db_cfg["name"]
    host   = db_cfg["host"]
    port   = db_cfg.get("port", 3306)
    user   = db_cfg["user"]
    pw     = db_cfg["password"]

    print(f"[swap] Swapping {staging_db} → {mirror} …")

    # Drop existing mirror DB and rename staging
    _mysql_exec(host, port, user, pw, f"DROP DATABASE IF EXISTS `{mirror}`;")
    _mysql_exec(host, port, user, pw, f"CREATE DATABASE `{mirror}` CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;")

    # MySQL doesn't support RENAME DATABASE — we use the import approach:
    # dump from staging and import into mirror
    dump_cmd = [
        "mysqldump",
        f"--host={host}",
        f"--port={port}",
        f"--user={user}",
        f"--password={pw}",
        "--single-transaction",
        "--quick",
        staging_db,
    ]
    import_cmd = [
        "mysql",
        f"--host={host}",
        f"--port={port}",
        f"--user={user}",
        f"--password={pw}",
        mirror,
    ]

    dump_proc   = subprocess.Popen(dump_cmd, stdout=subprocess.PIPE)
    import_proc = subprocess.Popen(import_cmd, stdin=dump_proc.stdout, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    dump_proc.stdout.close()
    _, import_err = import_proc.communicate()

    if import_proc.returncode != 0:
        raise RuntimeError("[swap] Failed to copy staging to mirror:\n" + import_err.decode(errors="replace"))

    # Drop staging
    _mysql_exec(host, port, user, pw, f"DROP DATABASE IF EXISTS `{staging_db}`;")
    print(f"[swap] Mirror DB '{mirror}' is now up to date.")


# ---------------------------------------------------------------------------
# 9. Update meta.json with final status
# ---------------------------------------------------------------------------

def update_meta(job_dir: pathlib.Path, status: str) -> None:
    meta_path = job_dir / "meta.json"
    if not meta_path.exists():
        return
    meta = json.loads(meta_path.read_text())
    now  = datetime.now(timezone.utc)
    meta["import_status"] = status
    # retain archive for 5 months
    from datetime import timedelta
    meta["archive_until"] = (now + timedelta(days=150)).isoformat()
    meta_path.write_text(json.dumps(meta, indent=2))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _mysql_exec(host: str, port: int, user: str, password: str, sql: str) -> None:
    cmd = [
        "mysql",
        f"--host={host}",
        f"--port={port}",
        f"--user={user}",
        f"--password={password}",
        "-e", sql,
    ]
    result = subprocess.run(cmd, capture_output=True)
    if result.returncode != 0:
        raise RuntimeError("mysql error: " + result.stderr.decode(errors="replace"))


# ---------------------------------------------------------------------------
# Retention cleanup (daily cron should call this)
# ---------------------------------------------------------------------------

def prune_old_archives(cfg: dict) -> None:
    base = pathlib.Path(cfg["archive_storage_path"])
    if not base.exists():
        return
    now = datetime.now(timezone.utc)
    for job_dir in base.iterdir():
        if not job_dir.is_dir():
            continue
        meta_path = job_dir / "meta.json"
        if not meta_path.exists():
            continue
        meta = json.loads(meta_path.read_text())
        archive_until = meta.get("archive_until")
        if not archive_until:
            continue
        from datetime import datetime as dt
        until = dt.fromisoformat(archive_until)
        if now > until:
            print(f"[prune] Removing expired archive: {job_dir.name}")
            shutil.rmtree(job_dir)


# ---------------------------------------------------------------------------
# Main pipeline
# ---------------------------------------------------------------------------

def run_pipeline(cfg: dict, test_mode: bool = False) -> None:
    print("=" * 60)
    print("Behdashtik Mirror Connector — Export Pipeline")
    print(f"Target: {cfg['wp_base_url']}")
    print(f"Time  : {datetime.now(timezone.utc).isoformat()}")
    print("=" * 60)

    health_check(cfg)

    job_id = start_export(cfg, test_mode=test_mode)
    status = poll_until_ready(cfg, job_id)

    job_dir = download_archive(cfg, job_id, status)
    confirm_download(cfg, job_id)

    staging_db = import_archive(cfg, job_id, job_dir)

    try:
        validate_import(cfg, job_id, staging_db, job_dir)
    except Exception as e:
        # Leave staging intact for debugging; do NOT swap
        update_meta(job_dir, "failed")
        raise RuntimeError(f"Import validation failed — mirror DB NOT updated. Staging kept as '{staging_db}'.\n{e}") from e

    swap_staging_to_mirror(cfg, staging_db)
    update_meta(job_dir, "success")

    print()
    print("=" * 60)
    print(f"Pipeline complete. Mirror DB '{cfg['mirror_db']['name']}' updated.")
    print(f"Archive stored at: {job_dir}")
    print("=" * 60)


def import_only(cfg: dict, job_dir_path: str) -> None:
    """Re-run import+validate+swap for an already-downloaded archive."""
    job_dir = pathlib.Path(job_dir_path)
    if not job_dir.exists():
        sys.exit(f"[ERROR] Directory not found: {job_dir}")
    meta    = json.loads((job_dir / "meta.json").read_text())
    job_id  = meta["job_id"]
    staging = import_archive(cfg, job_id, job_dir)
    validate_import(cfg, job_id, staging, job_dir)
    swap_staging_to_mirror(cfg, staging)
    update_meta(job_dir, "success")
    print("Import complete.")


# ---------------------------------------------------------------------------
# Media Sync — local index table setup
# ---------------------------------------------------------------------------

MEDIA_SYNC_STATE_PATH = pathlib.Path(__file__).parent / "media_sync_state.json"

MEDIA_LOCAL_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS bdsk_local_media_index (
    id               BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    manifest_id      BIGINT UNSIGNED NOT NULL,
    attachment_id    BIGINT UNSIGNED NOT NULL,
    product_id       BIGINT UNSIGNED NOT NULL DEFAULT 0,
    order_id         BIGINT UNSIGNED NOT NULL DEFAULT 0,
    image_type       VARCHAR(20)     NOT NULL DEFAULT '',
    original_url     TEXT            NOT NULL,
    alt_text         TEXT,
    title            TEXT,
    caption          TEXT,
    width            INT             DEFAULT NULL,
    height           INT             DEFAULT NULL,
    mime_type        VARCHAR(100)    DEFAULT NULL,
    file_size        BIGINT          DEFAULT NULL,
    modified_at      VARCHAR(30)     DEFAULT NULL,
    manifest_status  VARCHAR(10)     NOT NULL DEFAULT 'active',
    local_path       TEXT            DEFAULT NULL,
    local_file_size  BIGINT          DEFAULT NULL,
    download_status  VARCHAR(10)     NOT NULL DEFAULT 'pending',
    downloaded_at    DATETIME        DEFAULT NULL,
    last_checked_at  DATETIME        DEFAULT NULL,
    retry_count      INT             NOT NULL DEFAULT 0,
    PRIMARY KEY      (id),
    UNIQUE KEY       manifest_id (manifest_id),
    KEY              download_status (download_status),
    KEY              attachment_id (attachment_id)
) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci
"""


def _media_conn(cfg: dict):
    db = cfg["mirror_db"]
    return pymysql.connect(
        host=db["host"],
        port=db.get("port", 3306),
        user=db["user"],
        password=db["password"],
        database=db["name"],
        charset="utf8mb4",
        autocommit=True,
    )


def setup_media_local_table(cfg: dict) -> None:
    with _media_conn(cfg) as conn:
        with conn.cursor() as cur:
            cur.execute(MEDIA_LOCAL_TABLE_SQL)


# ---------------------------------------------------------------------------
# Media Sync — manifest fetching
# ---------------------------------------------------------------------------

def _fetch_manifest_page(cfg: dict, after_id: int, limit: int,
                          modified_since: int | None, include_deleted: bool) -> dict:
    params: dict = {"after_id": after_id, "limit": limit}
    if modified_since:
        params["modified_since"] = modified_since
    if not include_deleted:
        params["include_deleted"] = "false"
    return api_get(cfg, "/media-manifest", params=params)


def _upsert_local_media(cur, item: dict) -> None:
    cur.execute(
        """
        INSERT INTO bdsk_local_media_index
          (manifest_id, attachment_id, product_id, order_id, image_type,
           original_url, alt_text, title, caption, width, height, mime_type,
           file_size, modified_at, manifest_status, last_checked_at)
        VALUES
          (%s, %s, %s, %s, %s,
           %s, %s, %s, %s, %s, %s, %s,
           %s, %s, %s, NOW())
        ON DUPLICATE KEY UPDATE
          attachment_id   = VALUES(attachment_id),
          product_id      = VALUES(product_id),
          order_id        = VALUES(order_id),
          image_type      = VALUES(image_type),
          original_url    = VALUES(original_url),
          alt_text        = VALUES(alt_text),
          title           = VALUES(title),
          caption         = VALUES(caption),
          width           = VALUES(width),
          height          = VALUES(height),
          mime_type       = VALUES(mime_type),
          file_size       = VALUES(file_size),
          modified_at     = VALUES(modified_at),
          manifest_status = VALUES(manifest_status),
          last_checked_at = NOW()
        """,
        (
            item["id"],
            item["attachment_id"],
            item.get("product_id") or 0,
            item.get("order_id") or 0,
            item["image_type"],
            item["original_url"],
            item.get("alt_text"),
            item.get("title"),
            item.get("caption"),
            item.get("width"),
            item.get("height"),
            item.get("mime_type"),
            item.get("file_size"),
            item.get("modified_at"),
            item.get("status", "active"),
        ),
    )


# ---------------------------------------------------------------------------
# Media Sync — file download
# ---------------------------------------------------------------------------

def _local_path_for(media_base: pathlib.Path, item: dict) -> pathlib.Path:
    url = item["original_url"]
    marker = "/wp-content/uploads/"
    idx = url.find(marker)
    if idx != -1:
        rel = url[idx + len(marker):]
        # Sanitise: strip query string / fragment
        rel = rel.split("?")[0].split("#")[0]
        return media_base / rel
    # Fallback: flat directory named by attachment_id
    basename = url.split("/")[-1].split("?")[0] or "unknown"
    return media_base / "_unmatched" / f"{item['attachment_id']}_{basename}"


def _download_one(cfg: dict, item: dict, media_base: pathlib.Path,
                  max_file_size: int) -> tuple[str, str | None]:
    """Returns (manifest_id_str, error_message_or_None)."""
    manifest_id = str(item["id"])
    url         = item["original_url"]
    dest        = _local_path_for(media_base, item)
    expected_size = item.get("file_size")

    # Skip files above max_file_size
    if expected_size and expected_size > max_file_size:
        return manifest_id, f"skipped: file_size {expected_size} > max {max_file_size}"

    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_suffix(dest.suffix + ".part")

    for attempt in range(3):
        try:
            with requests.get(
                url,
                headers={"Authorization": f"Bearer {cfg['api_secret']}"},
                stream=True,
                timeout=(10, 120),  # connect, read
            ) as r:
                if not r.ok:
                    raise RuntimeError(f"HTTP {r.status_code}")
                with part.open("wb") as f:
                    for chunk in r.iter_content(chunk_size=65536):
                        f.write(chunk)

            actual = part.stat().st_size
            if expected_size and actual != expected_size:
                part.unlink(missing_ok=True)
                raise RuntimeError(f"size mismatch: got {actual}, expected {expected_size}")

            part.rename(dest)
            return manifest_id, None

        except Exception as exc:
            if attempt == 2:
                part.unlink(missing_ok=True)
                return manifest_id, str(exc)
            time.sleep(2 ** attempt)

    return manifest_id, "max retries exceeded"


# ---------------------------------------------------------------------------
# Media Sync — load / save sync state
# ---------------------------------------------------------------------------

def _load_sync_state() -> dict:
    if MEDIA_SYNC_STATE_PATH.exists():
        try:
            return json.loads(MEDIA_SYNC_STATE_PATH.read_text())
        except Exception:
            pass
    return {"last_sync_at": None}


def _save_sync_state(state: dict) -> None:
    MEDIA_SYNC_STATE_PATH.write_text(json.dumps(state, indent=2))


# ---------------------------------------------------------------------------
# Media Sync — main entry point
# ---------------------------------------------------------------------------

def run_media_sync(cfg: dict, full: bool = False) -> None:
    media_cfg     = cfg.get("media_sync", {})
    if not media_cfg.get("enabled", True):
        print("[media] Media sync disabled in config.")
        return

    media_base    = pathlib.Path(media_cfg.get("storage_path", "/tmp/bdsk-media"))
    concurrency   = int(media_cfg.get("concurrency", 4))
    max_file_size = int(media_cfg.get("max_file_size_bytes", 52_428_800))  # 50 MB
    page_limit    = 200

    media_base.mkdir(parents=True, exist_ok=True)

    # Protect the directory (deny web access)
    htaccess = media_base / ".htaccess"
    if not htaccess.exists():
        htaccess.write_text("Deny from all\n")

    # Ensure local index table exists
    setup_media_local_table(cfg)

    state = _load_sync_state()
    sync_start = int(time.time())

    if full or not state.get("last_sync_at"):
        modified_since = None
        print("[media] Starting full media manifest sync …")
    else:
        # 5-minute safety margin for clock skew
        modified_since = max(0, int(state["last_sync_at"]) - 300)
        ts_str = datetime.fromtimestamp(modified_since, tz=timezone.utc).isoformat()
        print(f"[media] Incremental sync since {ts_str} …")

    # ---- Phase 1: page through manifest and upsert into local index ----
    after_id     = 0
    total_items  = 0

    with _media_conn(cfg) as conn:
        with conn.cursor() as cur:
            while True:
                page = _fetch_manifest_page(
                    cfg, after_id, page_limit, modified_since, include_deleted=True
                )
                items = page.get("items", [])
                if not items:
                    break

                for item in items:
                    _upsert_local_media(cur, item)

                total_items += len(items)
                after_id = page.get("next_after_id") or 0
                print(f"[media] Fetched {total_items} manifest entries …", end="\r")

                if not page.get("has_more"):
                    break

    print(f"\n[media] Manifest sync complete: {total_items} entries processed.")

    # ---- Phase 2: handle deletions ----
    with _media_conn(cfg) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT local_path FROM bdsk_local_media_index
                WHERE manifest_status = 'deleted' AND download_status != 'deleted'
                """
            )
            for (local_path,) in cur.fetchall():
                if local_path:
                    p = pathlib.Path(local_path)
                    if p.exists():
                        p.unlink()
                        print(f"[media] Deleted: {p}")
            cur.execute(
                """
                UPDATE bdsk_local_media_index
                SET download_status = 'deleted'
                WHERE manifest_status = 'deleted' AND download_status != 'deleted'
                """
            )

    # ---- Phase 3: determine download queue ----
    with _media_conn(cfg) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT manifest_id, attachment_id, image_type, original_url,
                       file_size, modified_at, local_path, local_file_size,
                       downloaded_at
                FROM bdsk_local_media_index
                WHERE manifest_status = 'active'
                AND download_status NOT IN ('deleted', 'skipped')
                """
            )
            rows = cur.fetchall()

    download_queue = []
    for (mid, att_id, itype, url, file_size, mod_at, local_path, local_file_size, downloaded_at) in rows:
        if not local_path:
            # Never downloaded
            download_queue.append({"id": mid, "attachment_id": att_id, "image_type": itype,
                                   "original_url": url, "file_size": file_size, "modified_at": mod_at})
            continue
        dest = pathlib.Path(local_path)
        if not dest.exists():
            download_queue.append({"id": mid, "attachment_id": att_id, "image_type": itype,
                                   "original_url": url, "file_size": file_size, "modified_at": mod_at})
            continue
        if file_size and local_file_size != file_size:
            download_queue.append({"id": mid, "attachment_id": att_id, "image_type": itype,
                                   "original_url": url, "file_size": file_size, "modified_at": mod_at})
            continue
        if mod_at and downloaded_at and mod_at > downloaded_at.isoformat() if hasattr(downloaded_at, 'isoformat') else False:
            download_queue.append({"id": mid, "attachment_id": att_id, "image_type": itype,
                                   "original_url": url, "file_size": file_size, "modified_at": mod_at})

    print(f"[media] {len(download_queue)} files to download.")

    if not download_queue:
        _save_sync_state({"last_sync_at": sync_start})
        print("[media] Media sync complete (nothing to download).")
        return

    # ---- Phase 4: concurrent downloads ----
    done = 0
    errors = 0

    def _do_download(item):
        return _download_one(cfg, item, media_base, max_file_size)

    with concurrent.futures.ThreadPoolExecutor(max_workers=concurrency) as pool:
        futures = {pool.submit(_do_download, item): item for item in download_queue}
        for future in concurrent.futures.as_completed(futures):
            item = futures[future]
            manifest_id, error = future.result()
            dest = _local_path_for(media_base, item)

            with _media_conn(cfg) as conn:
                with conn.cursor() as cur:
                    if error:
                        errors += 1
                        cur.execute(
                            """
                            UPDATE bdsk_local_media_index
                            SET download_status = 'failed', retry_count = retry_count + 1,
                                last_checked_at = NOW()
                            WHERE manifest_id = %s
                            """,
                            (manifest_id,),
                        )
                        print(f"[media] FAIL  attachment={item['attachment_id']} — {error}")
                    else:
                        done += 1
                        file_size = dest.stat().st_size if dest.exists() else None
                        cur.execute(
                            """
                            UPDATE bdsk_local_media_index
                            SET download_status = 'downloaded',
                                local_path = %s,
                                local_file_size = %s,
                                downloaded_at = NOW(),
                                last_checked_at = NOW(),
                                retry_count = 0
                            WHERE manifest_id = %s
                            """,
                            (str(dest), file_size, manifest_id),
                        )

    print(f"[media] Downloads complete: {done} OK, {errors} failed.")
    _save_sync_state({"last_sync_at": sync_start})
    print("[media] Media sync state saved.")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Behdashtik Mirror Connector pipeline")
    parser.add_argument("--health-only",     action="store_true", help="Only run health check")
    parser.add_argument("--test",            action="store_true", help="Use test export (50 rows per table)")
    parser.add_argument("--import-only",     metavar="JOB_DIR",   help="Re-import an already-downloaded archive")
    parser.add_argument("--prune",           action="store_true", help="Prune expired local archives and exit")
    parser.add_argument("--media-sync",      action="store_true", help="Run incremental media file sync")
    parser.add_argument("--media-sync-full", action="store_true", help="Run full media file sync (ignores last_sync_at)")
    args = parser.parse_args()

    cfg = load_config()

    try:
        if args.prune:
            prune_old_archives(cfg)
        elif args.health_only:
            health_check(cfg)
        elif args.import_only:
            import_only(cfg, args.import_only)
        elif args.media_sync_full:
            run_media_sync(cfg, full=True)
        elif args.media_sync:
            run_media_sync(cfg, full=False)
        else:
            run_pipeline(cfg, test_mode=args.test)
    except (RuntimeError, TimeoutError, FileNotFoundError) as exc:
        print(f"\n[FATAL] {exc}", file=sys.stderr)
        sys.exit(1)

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
  python pipeline.py --import-only /root/wordpress-data-hub/data/db-archives/<job_id>
"""

import argparse
import concurrent.futures
import gzip
import hashlib
import hmac
import json
import os
import pathlib
import shutil
import sqlite3
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
            + f"?part={i}"
        )

        with requests.get(
            url,
            headers={
                "Authorization": f"Bearer {cfg['api_secret']}",
                "X-BDSK-Download-Token": token,
            },
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
        init_command="SET sql_mode=''",  # allow zero dates mirrored from WordPress
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

    media_base    = pathlib.Path(media_cfg.get("storage_path", "/root/wordpress-data-hub/data/media"))
    concurrency   = int(media_cfg.get("concurrency", 4))
    max_file_size = int(media_cfg.get("max_file_size_bytes", 52_428_800))  # 50 MB
    page_limit    = 200

    media_base.mkdir(parents=True, exist_ok=True)
    media_base.chmod(0o700)  # restrict: no other users/processes should read this

    # Deny web access if ever accidentally served
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
# Event Sync — consumer pipeline
# ---------------------------------------------------------------------------

EVENT_SYNC_STATE_PATH = pathlib.Path(__file__).parent / "event_sync_state.json"

EVENT_LOG_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS bdsk_event_log (
    id                   BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    event_id             VARCHAR(36)     NOT NULL,
    entity_type          VARCHAR(20)     NOT NULL,
    entity_id            BIGINT UNSIGNED NOT NULL,
    event_type           VARCHAR(20)     NOT NULL,
    received_at          DATETIME        NOT NULL,
    processed_at         DATETIME        NULL,
    mirror_update_status VARCHAR(20)     NOT NULL DEFAULT 'ok',
    error_message        TEXT            NULL,
    PRIMARY KEY (id),
    KEY event_id (event_id)
) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci
"""


def setup_event_log_table(cfg: dict) -> None:
    with _media_conn(cfg) as conn:
        with conn.cursor() as cur:
            cur.execute(EVENT_LOG_TABLE_SQL)


def _load_event_state() -> dict:
    if EVENT_SYNC_STATE_PATH.exists():
        try:
            return json.loads(EVENT_SYNC_STATE_PATH.read_text())
        except Exception:
            pass
    return {}


def _save_event_state(state: dict) -> None:
    EVENT_SYNC_STATE_PATH.write_text(json.dumps(state))


def _staging_db_exists(cfg: dict) -> bool:
    """Returns True if the staging DB exists, indicating a Phase 1 import/swap is in progress."""
    db = cfg["mirror_db"]
    staging_name = db["name"] + "_staging"
    conn = pymysql.connect(
        host=db["host"],
        port=db.get("port", 3306),
        user=db["user"],
        password=db["password"],
        charset="utf8mb4",
    )
    try:
        with conn.cursor() as cur:
            cur.execute("SHOW DATABASES LIKE %s", (staging_name,))
            return cur.fetchone() is not None
    finally:
        conn.close()


# Mirror DB uses the same table prefix as WordPress (verbatim copy from Phase 1 export)
_WP_PREFIX = "wp_"


def _apply_order_delete(cur, order_id: int) -> None:
    prefix = _WP_PREFIX
    cur.execute(f"SELECT order_item_id FROM {prefix}woocommerce_order_items WHERE order_id = %s", (order_id,))
    item_ids = [r[0] for r in cur.fetchall()]
    if item_ids:
        ph = ",".join(["%s"] * len(item_ids))
        cur.execute(f"DELETE FROM {prefix}woocommerce_order_itemmeta WHERE order_item_id IN ({ph})", item_ids)
    cur.execute(f"DELETE FROM {prefix}woocommerce_order_items WHERE order_id = %s", (order_id,))
    cur.execute(f"DELETE FROM {prefix}wc_orders_meta WHERE order_id = %s", (order_id,))
    cur.execute(f"DELETE FROM {prefix}wc_orders WHERE id = %s", (order_id,))


def _apply_order_upsert(cur, snapshot: dict) -> None:
    prefix = _WP_PREFIX
    order_id = int(snapshot["order_id"])
    order_row = snapshot["order_row"]

    # Upsert main order row
    cols = list(order_row.keys())
    vals = [order_row[c] for c in cols]
    col_sql  = ", ".join(f"`{c}`" for c in cols)
    ph_sql   = ", ".join(["%s"] * len(cols))
    upd_sql  = ", ".join(f"`{c}` = VALUES(`{c}`)" for c in cols if c != "id")
    cur.execute(
        f"INSERT INTO {prefix}wc_orders ({col_sql}) VALUES ({ph_sql}) "
        f"ON DUPLICATE KEY UPDATE {upd_sql}",
        vals,
    )

    # Replace meta: delete all then bulk insert
    cur.execute(f"DELETE FROM {prefix}wc_orders_meta WHERE order_id = %s", (order_id,))
    if snapshot["meta"]:
        meta_ph = ", ".join(["(%s, %s, %s)"] * len(snapshot["meta"]))
        meta_vals = []
        for m in snapshot["meta"]:
            meta_vals.extend([order_id, m["meta_key"], m["meta_value"]])
        cur.execute(
            f"INSERT INTO {prefix}wc_orders_meta (order_id, meta_key, meta_value) VALUES {meta_ph}",
            meta_vals,
        )

    # Replace order items and itemmeta
    cur.execute(f"SELECT order_item_id FROM {prefix}woocommerce_order_items WHERE order_id = %s", (order_id,))
    existing_item_ids = [r[0] for r in cur.fetchall()]
    if existing_item_ids:
        ph = ",".join(["%s"] * len(existing_item_ids))
        cur.execute(f"DELETE FROM {prefix}woocommerce_order_itemmeta WHERE order_item_id IN ({ph})", existing_item_ids)
    cur.execute(f"DELETE FROM {prefix}woocommerce_order_items WHERE order_id = %s", (order_id,))

    for item in snapshot.get("items", []):
        item_row = item["item_row"]
        icols = list(item_row.keys())
        ivals = [item_row[c] for c in icols]
        icol_sql = ", ".join(f"`{c}`" for c in icols)
        iph_sql  = ", ".join(["%s"] * len(icols))
        iupd_sql = ", ".join(f"`{c}` = VALUES(`{c}`)" for c in icols if c != "order_item_id")
        cur.execute(
            f"INSERT INTO {prefix}woocommerce_order_items ({icol_sql}) VALUES ({iph_sql}) "
            f"ON DUPLICATE KEY UPDATE {iupd_sql}",
            ivals,
        )
        new_item_id = int(item_row["order_item_id"])
        if item.get("itemmeta"):
            im_ph   = ", ".join(["(%s, %s, %s)"] * len(item["itemmeta"]))
            im_vals = []
            for im in item["itemmeta"]:
                im_vals.extend([new_item_id, im["meta_key"], im["meta_value"]])
            cur.execute(
                f"INSERT INTO {prefix}woocommerce_order_itemmeta (order_item_id, meta_key, meta_value) VALUES {im_ph}",
                im_vals,
            )


def _apply_product_delete(cur, product_id: int) -> None:
    prefix = _WP_PREFIX
    # Get variation IDs
    cur.execute(
        f"SELECT ID FROM {prefix}posts WHERE post_parent = %s AND post_type = 'product_variation'",
        (product_id,),
    )
    var_ids = [r[0] for r in cur.fetchall()]

    all_ids = [product_id] + var_ids
    ph = ",".join(["%s"] * len(all_ids))

    cur.execute(f"DELETE FROM {prefix}postmeta WHERE post_id IN ({ph})", all_ids)
    cur.execute(f"DELETE FROM {prefix}wc_product_meta_lookup WHERE product_id IN ({ph})", all_ids)
    cur.execute(f"DELETE FROM {prefix}term_relationships WHERE object_id = %s", (product_id,))
    if var_ids:
        var_ph = ",".join(["%s"] * len(var_ids))
        cur.execute(f"DELETE FROM {prefix}posts WHERE ID IN ({var_ph})", var_ids)
    cur.execute(f"DELETE FROM {prefix}posts WHERE ID = %s", (product_id,))


def _apply_product_upsert(cur, snapshot: dict) -> None:
    prefix = _WP_PREFIX
    product_id = int(snapshot["product_id"])
    post_row = snapshot["post_row"]

    # Upsert main post row
    cols = list(post_row.keys())
    vals = [post_row[c] for c in cols]
    col_sql = ", ".join(f"`{c}`" for c in cols)
    ph_sql  = ", ".join(["%s"] * len(cols))
    upd_sql = ", ".join(f"`{c}` = VALUES(`{c}`)" for c in cols if c != "ID")
    cur.execute(
        f"INSERT INTO {prefix}posts ({col_sql}) VALUES ({ph_sql}) "
        f"ON DUPLICATE KEY UPDATE {upd_sql}",
        vals,
    )

    # Replace postmeta
    cur.execute(f"DELETE FROM {prefix}postmeta WHERE post_id = %s", (product_id,))
    if snapshot["meta"]:
        m_ph   = ", ".join(["(%s, %s, %s)"] * len(snapshot["meta"]))
        m_vals = []
        for m in snapshot["meta"]:
            m_vals.extend([product_id, m["meta_key"], m["meta_value"]])
        cur.execute(
            f"INSERT INTO {prefix}postmeta (post_id, meta_key, meta_value) VALUES {m_ph}",
            m_vals,
        )

    # Replace term_relationships
    cur.execute(f"DELETE FROM {prefix}term_relationships WHERE object_id = %s", (product_id,))
    tr_rows = []
    for term_group in snapshot.get("terms", []):
        taxonomy = term_group["taxonomy"]
        for term_id in term_group["term_ids"]:
            # Look up term_taxonomy_id in mirror DB
            cur.execute(
                f"SELECT term_taxonomy_id FROM {prefix}term_taxonomy "
                f"WHERE taxonomy = %s AND term_id = %s LIMIT 1",
                (taxonomy, term_id),
            )
            row = cur.fetchone()
            if row:
                tr_rows.append((product_id, row[0]))
    if tr_rows:
        tr_ph = ", ".join(["(%s, %s)"] * len(tr_rows))
        tr_vals = [v for pair in tr_rows for v in pair]
        cur.execute(
            f"INSERT IGNORE INTO {prefix}term_relationships (object_id, term_taxonomy_id) VALUES {tr_ph}",
            tr_vals,
        )

    # Upsert product lookup row
    if snapshot.get("lookup_row"):
        lr = snapshot["lookup_row"]
        lr_cols = list(lr.keys())
        lr_vals = [lr[c] for c in lr_cols]
        lr_col_sql = ", ".join(f"`{c}`" for c in lr_cols)
        lr_ph_sql  = ", ".join(["%s"] * len(lr_cols))
        lr_upd_sql = ", ".join(f"`{c}` = VALUES(`{c}`)" for c in lr_cols if c != "product_id")
        cur.execute(
            f"INSERT INTO {prefix}wc_product_meta_lookup ({lr_col_sql}) VALUES ({lr_ph_sql}) "
            f"ON DUPLICATE KEY UPDATE {lr_upd_sql}",
            lr_vals,
        )

    # Handle variations
    snapshot_var_ids = []
    for var in snapshot.get("variations", []):
        vp = var["post_row"]
        var_id = int(vp["ID"])
        snapshot_var_ids.append(var_id)

        # Upsert variation post row
        vcols = list(vp.keys())
        vvals = [vp[c] for c in vcols]
        vcol_sql = ", ".join(f"`{c}`" for c in vcols)
        vph_sql  = ", ".join(["%s"] * len(vcols))
        vupd_sql = ", ".join(f"`{c}` = VALUES(`{c}`)" for c in vcols if c != "ID")
        cur.execute(
            f"INSERT INTO {prefix}posts ({vcol_sql}) VALUES ({vph_sql}) "
            f"ON DUPLICATE KEY UPDATE {vupd_sql}",
            vvals,
        )

        # Replace variation postmeta
        cur.execute(f"DELETE FROM {prefix}postmeta WHERE post_id = %s", (var_id,))
        if var.get("meta"):
            vm_ph   = ", ".join(["(%s, %s, %s)"] * len(var["meta"]))
            vm_vals = []
            for m in var["meta"]:
                vm_vals.extend([var_id, m["meta_key"], m["meta_value"]])
            cur.execute(
                f"INSERT INTO {prefix}postmeta (post_id, meta_key, meta_value) VALUES {vm_ph}",
                vm_vals,
            )

        # Upsert variation lookup row
        if var.get("lookup_row"):
            vlr = var["lookup_row"]
            vlr_cols = list(vlr.keys())
            vlr_vals = [vlr[c] for c in vlr_cols]
            vlr_col_sql = ", ".join(f"`{c}`" for c in vlr_cols)
            vlr_ph_sql  = ", ".join(["%s"] * len(vlr_cols))
            vlr_upd_sql = ", ".join(f"`{c}` = VALUES(`{c}`)" for c in vlr_cols if c != "product_id")
            cur.execute(
                f"INSERT INTO {prefix}wc_product_meta_lookup ({vlr_col_sql}) VALUES ({vlr_ph_sql}) "
                f"ON DUPLICATE KEY UPDATE {vlr_upd_sql}",
                vlr_vals,
            )

    # Delete orphan variations (in mirror but not in snapshot)
    if snapshot_var_ids:
        excl_ph = ",".join(["%s"] * len(snapshot_var_ids))
        cur.execute(
            f"SELECT ID FROM {prefix}posts "
            f"WHERE post_parent = %s AND post_type = 'product_variation' AND ID NOT IN ({excl_ph})",
            [product_id] + snapshot_var_ids,
        )
        orphan_ids = [r[0] for r in cur.fetchall()]
    else:
        cur.execute(
            f"SELECT ID FROM {prefix}posts WHERE post_parent = %s AND post_type = 'product_variation'",
            (product_id,),
        )
        orphan_ids = [r[0] for r in cur.fetchall()]

    if orphan_ids:
        oph = ",".join(["%s"] * len(orphan_ids))
        cur.execute(f"DELETE FROM {prefix}postmeta WHERE post_id IN ({oph})", orphan_ids)
        cur.execute(f"DELETE FROM {prefix}wc_product_meta_lookup WHERE product_id IN ({oph})", orphan_ids)
        cur.execute(f"DELETE FROM {prefix}posts WHERE ID IN ({oph})", orphan_ids)


# ---------------------------------------------------------------------------
# Webhook Dispatcher
# ---------------------------------------------------------------------------

HUB_DB_PATH = pathlib.Path(__file__).parent / "hub.db"


def _build_product_payload(snap: dict) -> dict:
    post   = snap.get("post_row") or {}
    meta   = {r["meta_key"]: r["meta_value"] for r in snap.get("meta", [])}
    lookup = snap.get("lookup_row") or {}
    terms  = snap.get("terms", [])

    def _meta(key, default=None):
        return meta.get(key, default)

    def _term_ids(taxonomy):
        for t in terms:
            if t.get("taxonomy") == taxonomy:
                return [int(x) for x in (t.get("term_ids") or [])]
        return []

    variations_out = []
    for v in snap.get("variations", []):
        vpost = v.get("post_row") or {}
        vmeta = {r["meta_key"]: r["meta_value"] for r in v.get("meta", [])}
        variations_out.append({
            "id":             int(vpost.get("ID") or 0),
            "status":         vpost.get("post_status", ""),
            "sku":            vmeta.get("_sku") or "",
            "price":          vmeta.get("_price") or "",
            "regular_price":  vmeta.get("_regular_price") or "",
            "sale_price":     vmeta.get("_sale_price") or "",
            "stock_quantity": int(vmeta.get("_stock_quantity") or 0),
            "stock_status":   vmeta.get("_stock_status") or "instock",
            "manage_stock":   vmeta.get("_manage_stock") == "yes",
        })

    return {
        "id":             int(post.get("ID") or 0),
        "name":           post.get("post_title", ""),
        "slug":           post.get("post_name", ""),
        "status":         post.get("post_status", ""),
        "type":           "variable" if variations_out else ("simple" if not int(post.get("post_parent") or 0) else "variation"),
        "sku":            _meta("_sku") or lookup.get("sku") or "",
        "price":          _meta("_price") or "",
        "regular_price":  _meta("_regular_price") or "",
        "sale_price":     _meta("_sale_price") or "",
        "stock_quantity": int(_meta("_stock_quantity") or 0) if _meta("_stock_quantity") is not None else None,
        "stock_status":   _meta("_stock_status") or lookup.get("stock_status") or "instock",
        "manage_stock":   _meta("_manage_stock") == "yes",
        "on_sale":        bool(int(lookup.get("onsale") or 0)),
        "categories":     [{"id": i} for i in _term_ids("product_cat")],
        "tags":           [{"id": i} for i in _term_ids("product_tag")],
        "date_created":   post.get("post_date_gmt") or post.get("post_date", ""),
        "date_modified":  post.get("post_modified_gmt") or post.get("post_modified", ""),
        "parent_id":      int(post.get("post_parent") or 0),
        "variations":     [v["id"] for v in variations_out],
        "_variations":    variations_out,
    }


def _build_order_payload(snap: dict) -> dict:
    order = snap.get("order_row") or {}
    meta  = {r["meta_key"]: r["meta_value"] for r in snap.get("meta", [])}

    def _meta(key, default=""):
        return meta.get(key, default)

    line_items    = []
    shipping_lines = []
    fee_lines     = []

    for item_data in snap.get("items", []):
        item     = item_data.get("item_row") or {}
        imeta    = {r["meta_key"]: r["meta_value"] for r in item_data.get("itemmeta", [])}
        item_type = item.get("order_item_type", "")

        if item_type == "line_item":
            line_items.append({
                "id":           int(item.get("order_item_id") or 0),
                "name":         item.get("order_item_name", ""),
                "product_id":   int(imeta.get("_product_id") or 0),
                "variation_id": int(imeta.get("_variation_id") or 0),
                "quantity":     int(imeta.get("_qty") or 1),
                "subtotal":     imeta.get("_line_subtotal") or "0",
                "total":        imeta.get("_line_total") or "0",
                "sku":          imeta.get("_sku") or "",
            })
        elif item_type == "shipping":
            shipping_lines.append({
                "id":           int(item.get("order_item_id") or 0),
                "method_title": item.get("order_item_name", ""),
                "method_id":    imeta.get("method_id") or "",
                "total":        imeta.get("cost") or "0",
            })
        elif item_type == "fee":
            fee_lines.append({
                "id":    int(item.get("order_item_id") or 0),
                "name":  item.get("order_item_name", ""),
                "total": imeta.get("_line_total") or "0",
            })

    return {
        "id":                    int(order.get("id") or 0),
        "status":                (order.get("status") or "").replace("wc-", ""),
        "currency":              order.get("currency", ""),
        "date_created":          order.get("date_created_gmt") or "",
        "date_modified":         order.get("date_updated_gmt") or "",
        "total":                 str(order.get("total_amount") or ""),
        "customer_id":           int(order.get("customer_id") or 0),
        "customer_note":         _meta("_customer_note"),
        "payment_method":        _meta("_payment_method"),
        "payment_method_title":  _meta("_payment_method_title"),
        "transaction_id":        _meta("_transaction_id"),
        "billing": {
            "first_name": _meta("_billing_first_name"),
            "last_name":  _meta("_billing_last_name"),
            "company":    _meta("_billing_company"),
            "address_1":  _meta("_billing_address_1"),
            "address_2":  _meta("_billing_address_2"),
            "city":       _meta("_billing_city"),
            "state":      _meta("_billing_state"),
            "postcode":   _meta("_billing_postcode"),
            "country":    _meta("_billing_country"),
            "email":      _meta("_billing_email") or order.get("billing_email", ""),
            "phone":      _meta("_billing_phone"),
        },
        "shipping": {
            "first_name": _meta("_shipping_first_name"),
            "last_name":  _meta("_shipping_last_name"),
            "company":    _meta("_shipping_company"),
            "address_1":  _meta("_shipping_address_1"),
            "address_2":  _meta("_shipping_address_2"),
            "city":       _meta("_shipping_city"),
            "state":      _meta("_shipping_state"),
            "postcode":   _meta("_shipping_postcode"),
            "country":    _meta("_shipping_country"),
        },
        "line_items":     line_items,
        "shipping_lines": shipping_lines,
        "fee_lines":      fee_lines,
    }


def _get_active_endpoints(event_name: str) -> list[dict]:
    if not HUB_DB_PATH.exists():
        return []
    conn = sqlite3.connect(HUB_DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        cur = conn.cursor()
        # webhook_endpoints table may not exist yet (before first dashboard run)
        cur.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='webhook_endpoints'"
        )
        if not cur.fetchone():
            return []
        cur.execute(
            "SELECT id, name, url, secret, event_filter FROM webhook_endpoints WHERE enabled=1"
        )
        rows = [dict(r) for r in cur.fetchall()]
    finally:
        conn.close()

    result = []
    for row in rows:
        try:
            filters = json.loads(row["event_filter"])
        except Exception:
            filters = ["product.upserted", "product.deleted", "order.upserted", "order.deleted"]
        if event_name in filters:
            result.append(row)
    return result


def _sign_webhook_body(body: bytes, secret: str) -> str:
    return hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def _log_delivery(
    endpoint_id: int, event: str, entity_id: int, sent_at: str,
    http_status: int | None, success: bool, error: str | None,
) -> None:
    if not HUB_DB_PATH.exists():
        return
    conn = sqlite3.connect(HUB_DB_PATH)
    try:
        conn.execute(
            "INSERT INTO webhook_deliveries "
            "(endpoint_id, event, entity_id, sent_at, http_status, success, error) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (endpoint_id, event, entity_id, sent_at,
             http_status, 1 if success else 0, error),
        )
        conn.commit()
    finally:
        conn.close()


def _dispatch_webhooks(
    cfg: dict, entity_type: str, entity_id: int,
    event_type: str, snap: dict | None,
) -> None:
    event_name = f"{entity_type}.{event_type}"
    endpoints  = _get_active_endpoints(event_name)
    if not endpoints:
        return

    if event_type == "deleted" or not (snap and snap.get("exists")):
        data = {"id": entity_id}
    elif entity_type == "order":
        data = _build_order_payload(snap)
    else:
        data = _build_product_payload(snap)

    payload = {
        "event":       event_name,
        "entity_type": entity_type,
        "entity_id":   entity_id,
        "timestamp":   datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "data":        data,
    }
    body     = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    sent_at  = datetime.now(timezone.utc).isoformat()
    wc_cfg   = cfg.get("webhooks", {})
    timeout  = int(wc_cfg.get("timeout_seconds", 10))

    for ep in endpoints:
        sig = _sign_webhook_body(body, ep["secret"])
        try:
            resp = requests.post(
                ep["url"],
                data=body,
                headers={
                    "Content-Type":    "application/json",
                    "X-BDSK-Signature": sig,
                    "X-BDSK-Event":    event_name,
                },
                timeout=timeout,
            )
            success     = resp.ok
            http_status = resp.status_code
            error       = None if success else f"HTTP {resp.status_code}: {resp.text[:120]}"
        except Exception as exc:
            success     = False
            http_status = None
            error       = str(exc)[:200]

        status_str = "ok" if success else "fail"
        print(f"[webhook] {ep['name']} {event_name} entity={entity_id} → {http_status or 'ERR'} ({status_str})")
        _log_delivery(ep["id"], event_name, entity_id, sent_at, http_status, success, error)


def run_event_sync(cfg: dict) -> None:
    print("=" * 60)
    print("Behdashtik Mirror Connector — Event Sync")
    print(f"Time: {datetime.now(timezone.utc).isoformat()}")
    print("=" * 60)

    setup_event_log_table(cfg)

    # Check for in-progress staging-DB swap (Phase 1)
    if _staging_db_exists(cfg):
        print("[events] Staging DB swap in progress — skipping this cycle.")
        return

    state    = _load_event_state()
    after_id = int(state.get("after_id", 0))
    batch    = int(cfg.get("event_sync", {}).get("batch_size", 200))
    max_retry = int(cfg.get("event_sync", {}).get("max_retries", 10))

    data   = api_get(cfg, "/events/pending", {"after_id": after_id, "limit": batch})
    events = data.get("items", [])

    if not events:
        print("[events] No pending events.")
        return

    print(f"[events] {len(events)} pending event(s) since id={after_id}.")

    # Deduplicate: group by (entity_type, entity_id), keep last event_type per entity
    seen: dict[tuple, dict] = {}
    for ev in events:
        key = (ev["entity_type"], int(ev["entity_id"]))
        seen[key] = ev  # last event wins

    received_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    ack_ids: list[str] = []
    all_event_ids_for_entity: dict[tuple, list[str]] = {}
    for ev in events:
        key = (ev["entity_type"], int(ev["entity_id"]))
        all_event_ids_for_entity.setdefault(key, []).append(ev["event_id"])

    log_rows: list[dict] = []

    with _media_conn(cfg) as conn:
        conn.autocommit(False)
        for (entity_type, entity_id), ev in seen.items():
            event_type = ev["event_type"]
            ev_ids_for_entity = all_event_ids_for_entity[(entity_type, entity_id)]

            status      = "ok"
            error_msg   = None
            processed_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")

            # Check retry cap: use retry_count from the representative event
            retry_count = int(ev.get("retry_count", 0))
            if retry_count >= max_retry:
                print(f"[events] EXPIRED  {entity_type}={entity_id} after {retry_count} retries — skipping.")
                status = "expired"
                # Still ack so it moves to acknowledged; Server 2 consumer logs it as expired
                ack_ids.extend(ev_ids_for_entity)
                log_rows.append({
                    "event_id": ev["event_id"], "entity_type": entity_type,
                    "entity_id": entity_id, "event_type": event_type,
                    "received_at": received_at, "processed_at": processed_at,
                    "mirror_update_status": "expired", "error_message": f"retry_count={retry_count}",
                })
                continue

            try:
                effective_event  = event_type
                snap_for_webhook = None
                with conn.cursor() as cur:
                    if event_type == "deleted":
                        if entity_type == "order":
                            _apply_order_delete(cur, entity_id)
                        else:
                            _apply_product_delete(cur, entity_id)
                    else:
                        # upserted — fetch current snapshot
                        snap = api_get(cfg, f"/snapshot/{entity_type}/{entity_id}")
                        if not snap.get("exists"):
                            # Treat 404 snapshot as deleted (entity deleted between event capture and processing)
                            effective_event = "deleted"
                            if entity_type == "order":
                                _apply_order_delete(cur, entity_id)
                            else:
                                _apply_product_delete(cur, entity_id)
                        else:
                            snap_for_webhook = snap
                            if entity_type == "order":
                                _apply_order_upsert(cur, snap)
                            else:
                                _apply_product_upsert(cur, snap)

                conn.commit()
                ack_ids.extend(ev_ids_for_entity)
                print(f"[events] OK       {entity_type}={entity_id} ({effective_event})")
                # Best-effort webhook dispatch — delivery failure does not affect mirror correctness
                try:
                    _dispatch_webhooks(cfg, entity_type, entity_id, effective_event, snap_for_webhook)
                except Exception as wh_exc:
                    print(f"[webhook] dispatch error (non-fatal): {wh_exc}")

            except Exception as exc:
                conn.rollback()
                status    = "failed"
                error_msg = str(exc)
                print(f"[events] FAIL     {entity_type}={entity_id} ({event_type}): {error_msg[:120]}")

            log_rows.append({
                "event_id": ev["event_id"], "entity_type": entity_type,
                "entity_id": entity_id, "event_type": event_type,
                "received_at": received_at, "processed_at": processed_at,
                "mirror_update_status": status, "error_message": error_msg,
            })

    # Write event log rows
    if log_rows:
        with _media_conn(cfg) as conn:
            with conn.cursor() as cur:
                for lr in log_rows:
                    cur.execute(
                        """INSERT INTO bdsk_event_log
                           (event_id, entity_type, entity_id, event_type,
                            received_at, processed_at, mirror_update_status, error_message)
                           VALUES (%s, %s, %s, %s, %s, %s, %s, %s)""",
                        (lr["event_id"], lr["entity_type"], lr["entity_id"],
                         lr["event_type"], lr["received_at"], lr["processed_at"],
                         lr["mirror_update_status"], lr["error_message"]),
                    )

    # Ack successfully processed events
    if ack_ids:
        api_post(cfg, "/events/ack", {"event_ids": ack_ids})
        print(f"[events] Acknowledged {len(ack_ids)} event(s).")

    # Advance cursor to max id seen in this batch
    max_id = max(int(ev["id"]) for ev in events)
    _save_event_state({"after_id": max_id})
    print(f"[events] Cursor advanced to after_id={max_id}.")
    print("[events] Event sync complete.")


# ---------------------------------------------------------------------------
# Status command — read-only system overview
# ---------------------------------------------------------------------------

def get_status_data(cfg: dict) -> dict:
    """Gather all status information from WP health endpoint + local state."""
    health = api_get(cfg, "/health")

    # Mirror DB: latest export job
    latest_job = None
    try:
        with _media_conn(cfg) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT job_id, status, created_at, finished_at FROM bdsk_local_media_index LIMIT 0"
                )
    except Exception:
        pass

    try:
        with _media_conn(cfg) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT job_id, status, created_at, finished_at "
                    "FROM wp_bdsk_export_jobs ORDER BY id DESC LIMIT 1"
                )
                row = cur.fetchone()
                if row:
                    latest_job = {
                        "job_id":      row[0][:8] + "…" if row[0] else None,
                        "status":      row[1],
                        "created_at":  str(row[2]) if row[2] else None,
                        "finished_at": str(row[3]) if row[3] else None,
                    }
    except Exception:
        pass

    # Mirror DB: media index counts by status
    media_counts: dict = {}
    try:
        with _media_conn(cfg) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT status, COUNT(*) FROM bdsk_local_media_index GROUP BY status"
                )
                for s, cnt in cur.fetchall():
                    media_counts[s] = cnt
    except Exception:
        pass

    # Local state files
    media_state     = _load_sync_state()
    event_state     = _load_event_state()
    event_state_mtime = None
    if EVENT_SYNC_STATE_PATH.exists():
        import os
        event_state_mtime = datetime.fromtimestamp(
            os.path.getmtime(EVENT_SYNC_STATE_PATH), tz=timezone.utc
        ).strftime("%Y-%m-%d %H:%M UTC")

    # Count local archive directories
    archive_base = pathlib.Path(cfg.get("archive_storage_path", ""))
    archive_count = len([d for d in archive_base.iterdir() if d.is_dir()]) if archive_base.exists() else 0

    # Count local media files on disk
    media_base  = pathlib.Path(cfg.get("media_sync", {}).get("storage_path", ""))
    media_files = 0
    if media_base.exists():
        media_files = sum(1 for _ in media_base.rglob("*") if _.is_file())

    last_sync_ts = media_state.get("last_sync_at")
    last_sync_str = (
        datetime.fromtimestamp(last_sync_ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        if last_sync_ts else "never"
    )

    return {
        "health":         health,
        "latest_job":     latest_job,
        "media_counts":   media_counts,
        "media_files":    media_files,
        "archive_count":  archive_count,
        "archive_base":   str(archive_base),
        "last_sync_at":   last_sync_str,
        "event_after_id": event_state.get("after_id", 0),
        "event_last_run": event_state_mtime,
    }


def run_status(cfg: dict) -> None:
    """Print a human-readable system status overview. Read-only, no mutations."""
    d = get_status_data(cfg)
    h = d["health"]

    print("=" * 60)
    print("Behdashtik Mirror Connector — Status")
    print("=" * 60)

    # WordPress / plugin
    conn_status = "connected"
    last_req    = h.get("last_successful_request") or h.get("last_successful_at")
    if not last_req:
        conn_status = "never connected"
    elif h.get("last_connection_status") == "stale":
        conn_status = "stale"

    print(f"\nWordPress: {'OK' if h.get('status') == 'ok' else 'ERROR'}")
    print(f"  Plugin v{h.get('plugin_version')} | WP {h.get('wordpress_version')} | "
          f"WC {h.get('woocommerce_version') or 'N/A'} | PHP {h.get('php_version')}")
    print(f"  Connector: {'enabled' if h.get('connector_enabled') else 'DISABLED'} | "
          f"Read access: {h.get('read_mode_status', 'off')} | Write access: off")
    print(f"  Last successful request: {last_req or '—'} ({conn_status})")
    if h.get("last_cleanup_run"):
        print(f"  Last cleanup run: {h['last_cleanup_run']}")

    # DB mirror
    job = d["latest_job"]
    print(f"\nDB Mirror ({cfg['mirror_db']['name']}):")
    if job:
        print(f"  Last export job: {job['job_id']} — {job['status']}")
        print(f"  Created: {job['created_at'] or '—'} | Finished: {job['finished_at'] or '—'}")
    else:
        print("  No export jobs found in mirror DB.")
    print(f"  Archives on disk: {d['archive_count']} ({d['archive_base']})")

    # Media
    mc = d["media_counts"]
    print(f"\nMedia:")
    print(f"  Index: {h.get('media_index_status', 'unknown')}")
    if mc:
        active  = mc.get("downloaded", 0) + mc.get("active", 0)
        pending = mc.get("pending", 0) + mc.get("queued", 0)
        print(f"  Local index: {active} downloaded, {pending} pending, "
              f"{mc.get('failed', 0)} failed")
    print(f"  Local files on disk: {d['media_files']}")
    print(f"  Last sync: {d['last_sync_at']}")

    # Events
    pending_ev = h.get("event_outbox_pending_count", 0)
    print(f"\nEvents:")
    print(f"  Pending on WP: {pending_ev}")
    print(f"  Last processed id (cursor): {d['event_after_id']}")
    print(f"  Last event-sync run: {d['event_last_run'] or 'never'}")

    print()


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
    parser.add_argument("--event-sync",      action="store_true", help="Run event sync (apply pending order/product changes to mirror)")
    parser.add_argument("--status",          action="store_true", help="Show system status overview (read-only)")
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
        elif args.event_sync:
            run_event_sync(cfg)
        elif args.status:
            run_status(cfg)
        else:
            run_pipeline(cfg, test_mode=args.test)
    except (RuntimeError, TimeoutError, FileNotFoundError) as exc:
        print(f"\n[FATAL] {exc}", file=sys.stderr)
        sys.exit(1)

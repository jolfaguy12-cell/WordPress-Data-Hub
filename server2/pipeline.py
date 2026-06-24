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
import base64
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
from bdsk_config import load_config, print_config_summary


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
    try:
        resp = requests.get(
            url,
            headers={"Authorization": f"Bearer {cfg['api_secret']}"},
            params=params,
            timeout=cfg.get("request_timeout_seconds", 60),
        )
    except requests.exceptions.RequestException as exc:
        raise RuntimeError(f"GET {path} connection failed: {exc}") from None
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
    try:
        resp = requests.post(
            url,
            headers={
                "Authorization": f"Bearer {cfg['api_secret']}",
                "Content-Type": "application/json",
            },
            json=body or {},
            timeout=cfg.get("request_timeout_seconds", 60),
        )
    except requests.exceptions.RequestException as exc:
        raise RuntimeError(f"POST {path} connection failed: {exc}") from None
    if not resp.ok and resp.status_code not in (409,):
        raise RuntimeError(f"POST {path} failed: {resp.status_code} — {resp.text[:300]}")
    return _parse_json(resp.text, f"POST {path}")


# ---------------------------------------------------------------------------
# 1. Health check
# ---------------------------------------------------------------------------

def health_check(cfg: dict) -> dict:
    print(f"[health] Source URL        : {cfg.get('wp_base_url', '?')}")
    print(f"[health] Mirror DB         : {cfg.get('mirror_db', {}).get('name', '?')}")
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

def start_export(cfg: dict, test_mode: bool = False) -> tuple[str, str]:
    """Start a new export job. Returns (job_id, export_mode)."""
    print(f"[export] Starting export job (test_mode={test_mode}) …")
    body = {}
    if test_mode:
        body["test"] = True

    data = api_post(cfg, "/db-export/start", body)

    if "error" in data and data.get("error") == "export_already_running":
        job_id = data["job_id"]
        if test_mode:
            raise RuntimeError(
                f"[export] A job ({job_id}) is already running — cannot start a test export.\n"
                f"[export] Cancel it first:\n"
                f"[export]   wp --path=/var/www/dev.behdashtik.ir --allow-root eval '\n"
                f"[export]     global $wpdb;\n"
                f"[export]     $wpdb->update(BDSK_DB::jobs_table(),\n"
                f'[export]       ["status"=>"failed","last_error"=>"cancelled"],\n'
                f'[export]       ["job_id"=>"{job_id}"]);'
                f"'"
            )
        print(f"[export] Export already running: {job_id} — attaching to it.")
        # Determine mode from current health since start response is a 409 body.
        health = api_get(cfg, "/health")
        export_mode = health.get("export_mode", "local_private_archive_mode")
        return job_id, export_mode

    job_id      = data["job_id"]
    export_mode = data.get("export_mode", "local_private_archive_mode")
    print(f"[export] Job created: {job_id} (status: {data.get('status')}, mode: {export_mode})")
    return job_id, export_mode


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
# 4b. Streaming export (shared_host_no_file_mode)
# ---------------------------------------------------------------------------

def _run_streaming_export(cfg: dict, job_id: str) -> pathlib.Path:
    """Drive chunk loop for shared_host_no_file_mode. Assembles gzip archive locally."""
    archive_dir  = pathlib.Path(cfg["archive_storage_path"]) / job_id
    archive_dir.mkdir(parents=True, exist_ok=True)
    archive_path = archive_dir / f"{job_id}.sql.gz"
    meta_path    = archive_dir / "meta.json"

    # If a prior session already completed streaming for this job, reuse the archive
    # rather than overwriting it.  Opening in "wb" mode on re-attach would truncate
    # the archive to only the chunks sent after re-attachment, producing a partial SQL
    # dump that fails at import.
    if archive_path.exists() and meta_path.exists():
        try:
            prev_meta = json.loads(meta_path.read_text())
            if prev_meta.get("checksum"):
                print(f"[stream] Archive already streamed for job {job_id} — reusing.")
                print(f"[stream] Archive: {archive_path}")
                return archive_dir
        except Exception:
            pass

    # Guard: if the archive exists but streaming wasn't completed (no checksum in
    # meta.json), the prior session was interrupted mid-stream.  Re-attaching and
    # overwriting would produce a partial dump (chunks from the re-attach point only;
    # all earlier chunks are permanently lost from the server queue).  Fail loudly.
    if archive_path.exists() and archive_path.stat().st_size > 0:
        raise RuntimeError(
            f"[stream] Archive {archive_path} exists but is incomplete "
            f"(prior session was interrupted mid-stream). "
            f"The WP server has already consumed those chunks — they cannot be "
            f"retrieved.  To restart: delete '{archive_dir}' and cancel/expire "
            f"job {job_id} on the WP side, then start a fresh export."
        )

    print(f"[stream] Streaming export for job {job_id} …")
    print(f"[stream] Archive: {archive_path}")

    chunk_num   = 0
    total_bytes = 0

    with gzip.open(str(archive_path), "wb") as gz_out:
        while True:
            resp = api_post(cfg, f"/db-export/chunk/{job_id}")

            if "error" in resp:
                raise RuntimeError(f"[stream] chunk endpoint returned error: {resp}")

            sql_bytes    = base64.b64decode(resp["sql_chunk"])
            bytes_this   = len(sql_bytes)
            gz_out.write(sql_bytes)
            total_bytes += bytes_this
            chunk_num   += 1

            progress = resp.get("progress_percent", 0)
            table    = resp.get("current_table") or "(done)"
            print(
                f"[stream] chunk {chunk_num:4d}  {progress:5.1f}%  "
                f"{bytes_this:7,} SQL bytes  {table}"
            )

            if resp.get("complete"):
                break

    archive_size   = archive_path.stat().st_size
    archive_sha256 = hashlib.sha256(archive_path.read_bytes()).hexdigest()

    print(
        f"[stream] Complete: {chunk_num} chunks, {total_bytes:,} SQL bytes, "
        f"{archive_size:,} compressed bytes"
    )
    print(f"[stream] sha256={archive_sha256[:32]}…")

    meta = {
        "backup_id":       job_id,
        "job_id":          job_id,
        "export_mode":     "shared_host_no_file_mode",
        "parts": [{
            "filename": archive_path.name,
            "size":     archive_size,
            "sha256":   archive_sha256,
        }],
        "archive_size":    archive_size,
        "checksum":        archive_sha256,
        "created_at":      datetime.now(timezone.utc).isoformat(),
        "downloaded_at":   datetime.now(timezone.utc).isoformat(),
        "source_site":     cfg["wp_base_url"],
        "db_prefix":       "wp_",
        "tables_included": [],
        "import_status":   "pending",
        "archive_until":   None,
    }
    (archive_dir / "meta.json").write_text(json.dumps(meta, indent=2))
    print(f"[stream] Archive stored at {archive_dir}")

    confirm_download(cfg, job_id)
    return archive_dir


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
    """Remove expired and abandoned archive directories.

    Two retention rules apply:
      1. Successfully imported archives carry an `archive_until` timestamp
         (set after import — default 150 days). They are removed once past it.
      2. Failed / interrupted archives never get an `archive_until` (or lack a
         meta.json entirely). They are useless once stale, so they are removed
         after `failed_archive_retention_days` (default 14) based on the
         directory's modification time.
    """
    from datetime import datetime as dt, timedelta

    base = pathlib.Path(cfg["archive_storage_path"])
    if not base.exists():
        return

    now = datetime.now(timezone.utc)
    failed_retention = timedelta(
        days=int(cfg.get("failed_archive_retention_days", 14))
    )

    for job_dir in base.iterdir():
        if not job_dir.is_dir():
            continue

        meta_path = job_dir / "meta.json"
        archive_until = None
        if meta_path.exists():
            try:
                archive_until = json.loads(meta_path.read_text()).get("archive_until")
            except (json.JSONDecodeError, OSError):
                archive_until = None

        if archive_until:
            # Rule 1: successfully imported archive with explicit expiry.
            if now > dt.fromisoformat(archive_until):
                print(f"[prune] Removing expired archive: {job_dir.name}")
                shutil.rmtree(job_dir)
            continue

        # Rule 2: failed / interrupted / orphaned archive (no archive_until).
        mtime = datetime.fromtimestamp(job_dir.stat().st_mtime, tz=timezone.utc)
        age = now - mtime
        if age > failed_retention:
            print(
                f"[prune] Removing abandoned archive (no expiry, "
                f"{age.days}d old): {job_dir.name}"
            )
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

    job_id, export_mode = start_export(cfg, test_mode=test_mode)

    if export_mode == "shared_host_no_file_mode":
        job_dir = _run_streaming_export(cfg, job_id)
    else:
        status  = poll_until_ready(cfg, job_id)
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


def run_chunk_test(cfg: dict) -> None:
    """Start a streaming job, fetch exactly one chunk, print result. Diagnostic only."""
    print("[chunk-test] Starting streaming export job (test_mode=True) …")
    data = api_post(cfg, "/db-export/start", {"test": True})
    job_id      = data.get("job_id")
    export_mode = data.get("export_mode")

    if not job_id:
        raise RuntimeError(f"[chunk-test] No job_id in response: {data}")
    if export_mode != "shared_host_no_file_mode":
        raise RuntimeError(
            f"[chunk-test] Expected shared_host_no_file_mode, got '{export_mode}'. "
            "Remove BDSK_EXPORT_STORAGE_PATH from wp-config.php to use streaming mode."
        )

    print(f"[chunk-test] Job {job_id} created ({export_mode})")
    print("[chunk-test] Fetching first chunk …")

    resp      = api_post(cfg, f"/db-export/chunk/{job_id}")
    sql_bytes = base64.b64decode(resp["sql_chunk"])

    print(f"[chunk-test] chunk_num     : {resp.get('chunk_num')}")
    print(f"[chunk-test] SQL bytes      : {len(sql_bytes):,}")
    print(f"[chunk-test] progress       : {resp.get('progress_percent', 0):.1f}%")
    print(f"[chunk-test] current_table  : {resp.get('current_table') or '(done)'}")
    print(f"[chunk-test] complete       : {resp.get('complete')}")
    print(f"[chunk-test] SQL preview (first 300 chars):")
    preview = sql_bytes[:300].decode("utf-8", errors="replace")
    for line in preview.splitlines():
        print(f"  {line}")

    print(
        f"\n[chunk-test] PASS — chunk endpoint works.\n"
        f"[chunk-test] Note: job {job_id} left in 'running' state (test_mode=True, 50 rows/table cap).\n"
        f"[chunk-test] It will be auto-marked stalled after 15 min by the heartbeat checker.\n"
        f"[chunk-test] To cancel immediately: mark status='failed' via WP-CLI."
    )


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

def _media_sync_state_path(cfg: dict) -> pathlib.Path:
    p = cfg.get("_media_sync_state_path")
    return pathlib.Path(p) if p else pathlib.Path(__file__).parent / "media_sync_state.json"


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
    variation_id     BIGINT UNSIGNED NOT NULL DEFAULT 0,
    role             VARCHAR(20)     NOT NULL DEFAULT '',
    manifest_status  VARCHAR(10)     NOT NULL DEFAULT 'active',
    local_path       TEXT            DEFAULT NULL,
    local_file_size  BIGINT          DEFAULT NULL,
    etag             VARCHAR(255)    DEFAULT NULL,
    checksum         CHAR(64)        DEFAULT NULL,
    download_status  VARCHAR(10)     NOT NULL DEFAULT 'pending',
    downloaded_at    DATETIME        DEFAULT NULL,
    last_checked_at  DATETIME        DEFAULT NULL,
    retry_count      INT             NOT NULL DEFAULT 0,
    last_error       TEXT            DEFAULT NULL,
    PRIMARY KEY      (id),
    UNIQUE KEY       manifest_id (manifest_id),
    KEY              download_status (download_status),
    KEY              attachment_id (attachment_id)
) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci
"""

# Additive migrations for pre-existing bdsk_local_media_index tables.
MEDIA_LOCAL_TABLE_MIGRATIONS = [
    "ADD COLUMN variation_id BIGINT UNSIGNED NOT NULL DEFAULT 0",
    "ADD COLUMN role         VARCHAR(20)     NOT NULL DEFAULT ''",
    "ADD COLUMN etag         VARCHAR(255)    DEFAULT NULL",
    "ADD COLUMN checksum     CHAR(64)        DEFAULT NULL",
    "ADD COLUMN last_error   TEXT            DEFAULT NULL",
]


def _mirror_conn(cfg: dict):
    """Open a connection to the WordPress mirror DB (replaces _media_conn)."""
    db = cfg["mirror_db"]
    return pymysql.connect(
        host=db["host"],
        port=db.get("port", 3306),
        user=db["user"],
        password=db["password"],
        database=db["name"],
        charset="utf8mb4",
        autocommit=True,
        init_command="SET sql_mode=''",
    )


def _hub_db_name(cfg: dict) -> str:
    """Name of the persistent hub-state DB (never swapped or dropped by export)."""
    return cfg.get("hub_state_db", {}).get("name", "behdashtik_hub_state")


def _hub_conn(cfg: dict):
    """Open a connection to the persistent hub-state DB, creating it if needed."""
    db   = cfg["mirror_db"]  # same host/creds; different DB name
    name = _hub_db_name(cfg)
    # Create the DB if it doesn't exist yet (idempotent), and ensure the
    # readonly user (used by the Data API) can also read from it.
    admin = pymysql.connect(
        host=db["host"], port=db.get("port", 3306),
        user=db["user"], password=db["password"],
        charset="utf8mb4",
    )
    try:
        with admin.cursor() as cur:
            cur.execute(
                f"CREATE DATABASE IF NOT EXISTS `{name}` "
                "CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci"
            )
            ro_user = db.get("readonly_user")
            if ro_user:
                try:
                    cur.execute(
                        f"GRANT SELECT ON `{name}`.* TO %s",
                        (ro_user,),
                    )
                except Exception:
                    pass  # user may not exist; non-fatal
    finally:
        admin.close()
    return pymysql.connect(
        host=db["host"],
        port=db.get("port", 3306),
        user=db["user"],
        password=db["password"],
        database=name,
        charset="utf8mb4",
        autocommit=True,
        init_command="SET sql_mode=''",
    )


def _migrate_hub_tables_if_needed(cfg: dict) -> None:
    """One-time: copy bdsk_local_media_index + bdsk_event_log from mirror to hub
    if the hub tables are empty and the mirror tables have rows.  Safe to re-run."""
    mirror_name = cfg["mirror_db"]["name"]
    hub_name    = _hub_db_name(cfg)
    if mirror_name == hub_name:
        return  # already the same DB; nothing to migrate
    try:
        with _hub_conn(cfg) as conn:
            with conn.cursor() as cur:
                for table in ("bdsk_local_media_index", "bdsk_event_log"):
                    cur.execute(f"SELECT COUNT(*) FROM `{table}`")
                    hub_count = cur.fetchone()[0]
                    if hub_count > 0:
                        continue  # already has data
                    # Check source
                    try:
                        cur.execute(
                            f"SELECT COUNT(*) FROM `{mirror_name}`.`{table}`"
                        )
                        src_count = cur.fetchone()[0]
                    except Exception:
                        continue  # mirror table doesn't exist; nothing to migrate
                    if src_count == 0:
                        continue
                    cur.execute(
                        f"INSERT INTO `{table}` SELECT * FROM `{mirror_name}`.`{table}`"
                    )
                    print(f"[hub] Migrated {src_count} rows from mirror.{table} → hub.{table}")
    except Exception as exc:
        print(f"[hub] WARNING: migration check failed (non-fatal): {exc}")


def setup_media_local_table(cfg: dict) -> None:
    with _hub_conn(cfg) as conn:
        with conn.cursor() as cur:
            cur.execute(MEDIA_LOCAL_TABLE_SQL)
            # Find existing columns and apply only the missing additive migrations
            # (ADD COLUMN IF NOT EXISTS is unsupported on MySQL 8 — check first).
            cur.execute("SHOW COLUMNS FROM bdsk_local_media_index")
            have = {r[0] for r in cur.fetchall()}
            for clause in MEDIA_LOCAL_TABLE_MIGRATIONS:
                col = clause.split()[2]  # "ADD COLUMN <name> ..."
                if col not in have:
                    cur.execute(f"ALTER TABLE bdsk_local_media_index {clause}")
    _migrate_hub_tables_if_needed(cfg)


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


# Maps the WP media-index image_type to the Server-2 mapping "role".
_ROLE_BY_IMAGE_TYPE = {
    "main":      "main",
    "gallery":   "gallery",
    "variation": "variation",
    "evidence":  "content",
    "unknown":   "other",
}


def _resolve_variation_parent(cur, variation_id: int) -> int:
    """Return the parent product id for a variation post (0 if not resolvable)."""
    cur.execute(
        f"SELECT post_parent FROM {_WP_PREFIX}posts WHERE ID = %s AND post_type = 'product_variation'",
        (variation_id,),
    )
    row = cur.fetchone()
    return int(row[0]) if row and row[0] else 0


def _upsert_local_media(hub_cur, item: dict, mirror_cur=None) -> None:
    image_type = item["image_type"]
    role       = _ROLE_BY_IMAGE_TYPE.get(image_type, "other")

    # The WP index stores product_id = variation post id for variation images.
    # Record the variation id separately and resolve the owning product so the
    # Hub can present products/{product_id}/variations/{variation_id}/.
    # mirror_cur (connection to WordPress mirror DB) is needed for this lookup;
    # without it the parent defaults to 0.
    raw_pid      = item.get("product_id") or 0
    product_id   = raw_pid
    variation_id = 0
    if image_type == "variation" and raw_pid and mirror_cur is not None:
        variation_id = raw_pid
        product_id   = _resolve_variation_parent(mirror_cur, raw_pid)
    elif image_type == "variation" and raw_pid:
        variation_id = raw_pid

    hub_cur.execute(
        """
        INSERT INTO bdsk_local_media_index
          (manifest_id, attachment_id, product_id, order_id, variation_id, role, image_type,
           original_url, alt_text, title, caption, width, height, mime_type,
           file_size, modified_at, manifest_status, last_checked_at)
        VALUES
          (%s, %s, %s, %s, %s, %s, %s,
           %s, %s, %s, %s, %s, %s, %s,
           %s, %s, %s, NOW())
        ON DUPLICATE KEY UPDATE
          attachment_id   = VALUES(attachment_id),
          product_id      = VALUES(product_id),
          order_id        = VALUES(order_id),
          variation_id    = VALUES(variation_id),
          role            = VALUES(role),
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
            product_id,
            item.get("order_id") or 0,
            variation_id,
            role,
            image_type,
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
                  max_file_size: int, timeout: tuple[int, int],
                  retries: int) -> dict:
    """Download one media file with conditional GET + retry/backoff.

    Returns a result dict:
      { manifest_id, error, not_modified, etag, checksum, skipped }
    On a 304 (unchanged vs. stored etag/modified_at) the existing file is kept.
    """
    manifest_id   = str(item["id"])
    url           = item["original_url"]
    dest          = _local_path_for(media_base, item)
    expected_size = item.get("file_size")

    def _result(**kw):
        base = {"manifest_id": manifest_id, "error": None, "not_modified": False,
                "etag": None, "checksum": None, "skipped": False}
        base.update(kw)
        return base

    # Skip files above max_file_size
    if expected_size and expected_size > max_file_size:
        return _result(skipped=True,
                       error=f"skipped: file_size {expected_size} > max {max_file_size}")

    # Conditional GET via the stored ETag (the reliable validator). We do NOT send
    # If-Modified-Since: the stored modified_at is WordPress's DB modified time, not
    # the file's HTTP Last-Modified, so it can't be compared safely. Whether a file
    # is stale is already decided in the queue-building step (size + modified_at).
    cond_headers = {"Authorization": f"Bearer {cfg['api_secret']}"}
    if item.get("etag") and dest.exists():
        cond_headers["If-None-Match"] = item["etag"]

    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_suffix(dest.suffix + ".part")

    for attempt in range(retries):
        try:
            with requests.get(url, headers=cond_headers, stream=True, timeout=timeout) as r:
                if r.status_code == 304:
                    return _result(not_modified=True, etag=item.get("etag"))
                if not r.ok:
                    raise RuntimeError(f"HTTP {r.status_code}")
                sha = hashlib.sha256()
                with part.open("wb") as f:
                    for chunk in r.iter_content(chunk_size=65536):
                        f.write(chunk)
                        sha.update(chunk)
                etag = r.headers.get("ETag")

            actual = part.stat().st_size
            if expected_size and actual != expected_size:
                part.unlink(missing_ok=True)
                raise RuntimeError(f"size mismatch: got {actual}, expected {expected_size}")

            part.rename(dest)
            return _result(etag=etag, checksum=sha.hexdigest())

        except Exception as exc:
            if attempt == retries - 1:
                part.unlink(missing_ok=True)
                return _result(error=str(exc))
            time.sleep(2 ** attempt)

    return _result(error="max retries exceeded")


# ---------------------------------------------------------------------------
# Media Sync — load / save sync state
# ---------------------------------------------------------------------------

def _load_sync_state(cfg: dict) -> dict:
    p = _media_sync_state_path(cfg)
    if p.exists():
        try:
            return json.loads(p.read_text())
        except Exception:
            pass
    return {"last_sync_at": None}


def _save_sync_state(cfg: dict, state: dict) -> None:
    _media_sync_state_path(cfg).write_text(json.dumps(state, indent=2))


# ---------------------------------------------------------------------------
# Media Sync — main entry point
# ---------------------------------------------------------------------------

def run_media_sync(cfg: dict, full: bool = False) -> None:
    media_cfg     = cfg.get("media_sync", {})
    if not media_cfg.get("enabled", True):
        print("[media] Media sync disabled in config.")
        return

    # Guard: don't run while a staging DB swap is in progress
    if _staging_db_exists(cfg):
        print("[media] Staging DB swap in progress — skipping media sync this cycle.")
        return

    media_base    = pathlib.Path(media_cfg.get("storage_path", "/root/wordpress-data-hub/data/media"))
    # Throttling: concurrency default 1, hard-capped at 2.
    concurrency   = max(1, min(2, int(media_cfg.get("concurrency", 1))))
    max_file_size = int(media_cfg.get("max_file_size_bytes", 52_428_800))  # 50 MB
    # Per-run caps so a run is bounded and near-live (not a full uploads sync).
    max_files_per_run = int(media_cfg.get("max_files_per_run", 500))
    max_mb_per_run    = int(media_cfg.get("max_mb_per_run", 300))
    # Retry / timeout
    dl_retries    = int(media_cfg.get("download_retries", 3))
    dl_giveup     = int(media_cfg.get("max_download_retries", 5))  # stop re-queuing after this
    timeout       = (int(media_cfg.get("connect_timeout", 10)),
                     int(media_cfg.get("read_timeout", 120)))
    page_limit    = 200

    media_base.mkdir(parents=True, exist_ok=True)
    media_base.chmod(0o700)  # restrict: no other users/processes should read this

    # Deny web access if ever accidentally served
    htaccess = media_base / ".htaccess"
    if not htaccess.exists():
        htaccess.write_text("Deny from all\n")

    # Ensure local index table exists
    setup_media_local_table(cfg)

    state = _load_sync_state(cfg)
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

    # hub_conn writes to persistent hub-state DB; mirror_conn reads wp_posts for
    # variation parent resolution (wp_posts lives in the mirror DB, not hub state).
    with _hub_conn(cfg) as hub_conn, _mirror_conn(cfg) as mirror_conn:
        with hub_conn.cursor() as hub_cur, mirror_conn.cursor() as mirror_cur:
            while True:
                page = _fetch_manifest_page(
                    cfg, after_id, page_limit, modified_since, include_deleted=True
                )
                items = page.get("items", [])
                if not items:
                    break

                for item in items:
                    _upsert_local_media(hub_cur, item, mirror_cur)

                total_items += len(items)
                after_id = page.get("next_after_id") or 0
                print(f"[media] Fetched {total_items} manifest entries …", end="\r")

                if not page.get("has_more"):
                    break

    print(f"\n[media] Manifest sync complete: {total_items} entries processed.")

    # ---- Phase 2: handle deletions ----
    with _hub_conn(cfg) as conn:
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
    # Bound retries: rows that have failed >= dl_giveup times are not re-queued
    # (they stay 'failed' with last_error) so failed work cannot grow unbounded.
    with _hub_conn(cfg) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT manifest_id, attachment_id, image_type, original_url,
                       file_size, modified_at, local_path, local_file_size,
                       downloaded_at, etag
                FROM bdsk_local_media_index
                WHERE manifest_status = 'active'
                AND download_status NOT IN ('deleted', 'skipped')
                AND retry_count < %s
                """,
                (dl_giveup,),
            )
            rows = cur.fetchall()

    download_queue = []
    for (mid, att_id, itype, url, file_size, mod_at, local_path,
         local_file_size, downloaded_at, etag) in rows:
        def _q():
            return {"id": mid, "attachment_id": att_id, "image_type": itype,
                    "original_url": url, "file_size": file_size,
                    "modified_at": mod_at, "etag": etag}
        if not local_path:
            download_queue.append(_q()); continue
        dest = pathlib.Path(local_path)
        if not dest.exists():
            download_queue.append(_q()); continue
        if file_size and local_file_size != file_size:
            download_queue.append(_q()); continue
        if mod_at and downloaded_at and (mod_at > downloaded_at.isoformat()
                                         if hasattr(downloaded_at, 'isoformat') else False):
            download_queue.append(_q()); continue

    total_candidates = len(download_queue)

    # Apply per-run caps (files + MB). Remaining items resume on the next run.
    capped = []
    acc_mb = 0.0
    for item in download_queue:
        if len(capped) >= max_files_per_run:
            break
        fs_mb = (item.get("file_size") or 0) / 1_048_576
        if capped and (acc_mb + fs_mb) > max_mb_per_run:
            break
        capped.append(item)
        acc_mb += fs_mb
    if len(capped) < total_candidates:
        print(f"[media] cap reached: queuing {len(capped)}/{total_candidates} "
              f"(max_files={max_files_per_run}, max_mb={max_mb_per_run}); "
              f"remainder resumes next run.")
    download_queue = capped

    print(f"[media] {len(download_queue)} files to download (concurrency={concurrency}).")

    if not download_queue:
        _save_sync_state(cfg, {"last_sync_at": sync_start})
        print("[media] Media sync complete (nothing to download).")
        return

    # ---- Phase 4: concurrent downloads ----
    done = unchanged = errors = 0

    def _do_download(item):
        return _download_one(cfg, item, media_base, max_file_size, timeout, dl_retries)

    with concurrent.futures.ThreadPoolExecutor(max_workers=concurrency) as pool:
        futures = {pool.submit(_do_download, item): item for item in download_queue}
        for future in concurrent.futures.as_completed(futures):
            item = futures[future]
            res  = future.result()
            dest = _local_path_for(media_base, item)

            with _hub_conn(cfg) as conn:
                with conn.cursor() as cur:
                    if res["skipped"]:
                        cur.execute(
                            "UPDATE bdsk_local_media_index SET download_status='skipped', "
                            "last_error=%s, last_checked_at=NOW() WHERE manifest_id=%s",
                            (res["error"], res["manifest_id"]),
                        )
                        print(f"[media] SKIP  attachment={item['attachment_id']} — {res['error']}")
                    elif res["not_modified"]:
                        unchanged += 1
                        cur.execute(
                            "UPDATE bdsk_local_media_index SET download_status='downloaded', "
                            "last_checked_at=NOW(), retry_count=0, last_error=NULL "
                            "WHERE manifest_id=%s",
                            (res["manifest_id"],),
                        )
                    elif res["error"]:
                        errors += 1
                        cur.execute(
                            "UPDATE bdsk_local_media_index SET download_status='failed', "
                            "retry_count=retry_count+1, last_error=%s, last_checked_at=NOW() "
                            "WHERE manifest_id=%s",
                            (res["error"][:500], res["manifest_id"]),
                        )
                        print(f"[media] FAIL  attachment={item['attachment_id']} — {res['error']}")
                    else:
                        done += 1
                        file_size = dest.stat().st_size if dest.exists() else None
                        cur.execute(
                            "UPDATE bdsk_local_media_index SET download_status='downloaded', "
                            "local_path=%s, local_file_size=%s, etag=%s, checksum=%s, "
                            "downloaded_at=NOW(), last_checked_at=NOW(), retry_count=0, "
                            "last_error=NULL WHERE manifest_id=%s",
                            (str(dest), file_size, res["etag"], res["checksum"], res["manifest_id"]),
                        )

    print(f"[media] Downloads complete: {done} OK, {unchanged} unchanged(304), {errors} failed.")
    _save_sync_state(cfg, {"last_sync_at": sync_start})
    print("[media] Media sync state saved.")


# ---------------------------------------------------------------------------
# Event Sync — consumer pipeline
# ---------------------------------------------------------------------------

def _event_sync_state_path(cfg: dict) -> pathlib.Path:
    p = cfg.get("_event_sync_state_path")
    return pathlib.Path(p) if p else pathlib.Path(__file__).parent / "event_sync_state.json"


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
    with _hub_conn(cfg) as conn:
        with conn.cursor() as cur:
            cur.execute(EVENT_LOG_TABLE_SQL)


def _load_event_state(cfg: dict) -> dict:
    p = _event_sync_state_path(cfg)
    if p.exists():
        try:
            return json.loads(p.read_text())
        except Exception:
            pass
    return {}


def _save_event_state(cfg: dict, state: dict) -> None:
    _event_sync_state_path(cfg).write_text(json.dumps(state))


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


def _apply_term_upsert(cur, snap: dict) -> None:
    """Idempotent upsert of a term into wp_terms / wp_term_taxonomy / wp_termmeta.

    Snapshot shape (from GET /snapshot/term/{id}):
      { term_id, term_row, taxonomies: [...], termmeta: [...] }
    Covers create, rename, slug change, parent change, description/meta changes.
    """
    prefix  = _WP_PREFIX
    term_id = int(snap["term_id"])

    # wp_terms
    tr = snap["term_row"]
    tcols = list(tr.keys())
    tvals = [tr[c] for c in tcols]
    tcol_sql = ", ".join(f"`{c}`" for c in tcols)
    tph_sql  = ", ".join(["%s"] * len(tcols))
    tupd_sql = ", ".join(f"`{c}` = VALUES(`{c}`)" for c in tcols if c != "term_id")
    cur.execute(
        f"INSERT INTO {prefix}terms ({tcol_sql}) VALUES ({tph_sql}) "
        f"ON DUPLICATE KEY UPDATE {tupd_sql}",
        tvals,
    )

    # wp_term_taxonomy — a term_id may back multiple taxonomy rows
    for tt in snap.get("taxonomies", []):
        ttcols = list(tt.keys())
        ttvals = [tt[c] for c in ttcols]
        ttcol_sql = ", ".join(f"`{c}`" for c in ttcols)
        ttph_sql  = ", ".join(["%s"] * len(ttcols))
        ttupd_sql = ", ".join(f"`{c}` = VALUES(`{c}`)" for c in ttcols if c != "term_taxonomy_id")
        cur.execute(
            f"INSERT INTO {prefix}term_taxonomy ({ttcol_sql}) VALUES ({ttph_sql}) "
            f"ON DUPLICATE KEY UPDATE {ttupd_sql}",
            ttvals,
        )

    # wp_termmeta — replace all rows for this term
    cur.execute(f"DELETE FROM {prefix}termmeta WHERE term_id = %s", (term_id,))
    tm = snap.get("termmeta", [])
    if tm:
        tm_ph   = ", ".join(["(%s, %s, %s)"] * len(tm))
        tm_vals = []
        for m in tm:
            tm_vals.extend([term_id, m["meta_key"], m["meta_value"]])
        cur.execute(
            f"INSERT INTO {prefix}termmeta (term_id, meta_key, meta_value) VALUES {tm_ph}",
            tm_vals,
        )


def _apply_term_delete(cur, term_id: int) -> None:
    """Delete a term and all rows that reference it. Idempotent."""
    prefix = _WP_PREFIX
    # Resolve this term's taxonomy ids, then drop any product relationships using them.
    cur.execute(
        f"SELECT term_taxonomy_id FROM {prefix}term_taxonomy WHERE term_id = %s",
        (term_id,),
    )
    tt_ids = [r[0] for r in cur.fetchall()]
    if tt_ids:
        ph = ",".join(["%s"] * len(tt_ids))
        cur.execute(
            f"DELETE FROM {prefix}term_relationships WHERE term_taxonomy_id IN ({ph})",
            tt_ids,
        )
    cur.execute(f"DELETE FROM {prefix}termmeta WHERE term_id = %s", (term_id,))
    cur.execute(f"DELETE FROM {prefix}term_taxonomy WHERE term_id = %s", (term_id,))
    cur.execute(f"DELETE FROM {prefix}terms WHERE term_id = %s", (term_id,))


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


def _apply_product_upsert(cfg, cur, snapshot: dict) -> None:
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
    def _lookup_tt_id(taxonomy: str, term_id: int):
        cur.execute(
            f"SELECT term_taxonomy_id FROM {prefix}term_taxonomy "
            f"WHERE taxonomy = %s AND term_id = %s LIMIT 1",
            (taxonomy, term_id),
        )
        r = cur.fetchone()
        return r[0] if r else None

    for term_group in snapshot.get("terms", []):
        taxonomy = term_group["taxonomy"]
        for term_id in term_group["term_ids"]:
            tt_id = _lookup_tt_id(taxonomy, term_id)
            if tt_id is None:
                # Term missing in mirror (e.g. created after last full export).
                # Auto-repair: fetch a small term snapshot and upsert it, then re-lookup.
                try:
                    term_snap = api_get(cfg, f"/snapshot/term/{term_id}")
                    if term_snap.get("exists"):
                        _apply_term_upsert(cur, term_snap)
                        tt_id = _lookup_tt_id(taxonomy, term_id)
                except Exception as exc:  # noqa: BLE001 — repair is best-effort
                    print(
                        f"[events] WARN    product={product_id}: term_id={term_id} "
                        f"({taxonomy}) auto-repair failed: {str(exc)[:120]}"
                    )
            if tt_id is not None:
                tr_rows.append((product_id, tt_id))
            else:
                print(
                    f"[events] WARN    product={product_id}: term_id={term_id} ({taxonomy}) "
                    f"not found in mirror and auto-repair did not resolve it — "
                    f"term assignment skipped."
                )
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

def _hub_db_path(cfg: dict) -> pathlib.Path:
    p = cfg.get("_hub_db_path")
    return pathlib.Path(p) if p else pathlib.Path(__file__).parent / "hub.db"


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


def _get_active_endpoints(cfg: dict, event_name: str) -> list[dict]:
    db_path = _hub_db_path(cfg)
    if not db_path.exists():
        return []
    conn = sqlite3.connect(db_path)
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
    cfg: dict, endpoint_id: int, event: str, entity_id: int, sent_at: str,
    http_status: int | None, success: bool, error: str | None,
) -> None:
    db_path = _hub_db_path(cfg)
    if not db_path.exists():
        return
    conn = sqlite3.connect(db_path)
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
    endpoints  = _get_active_endpoints(cfg, event_name)
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
        _log_delivery(cfg, ep["id"], event_name, entity_id, sent_at, http_status, success, error)


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

    state    = _load_event_state(cfg)
    after_id = int(state.get("after_id", 0))
    batch    = int(cfg.get("event_sync", {}).get("batch_size", 200))
    max_retry = int(cfg.get("event_sync", {}).get("max_retries", 10))

    data   = api_get(cfg, "/events/pending", {"after_id": after_id, "limit": batch})
    events = data.get("items", [])

    # Cursor desync guard: if we got no events but after_id > 0, check whether
    # the source outbox has ANY pending events at all. If it does and their ids
    # are all below our cursor, the source DB was restored or reimported to an
    # earlier state and the cursor is now ahead of the source — all new events
    # would be silently skipped forever without this check.
    if not events and after_id > 0:
        probe        = api_get(cfg, "/events/pending", {"after_id": 0, "limit": 1})
        probe_events = probe.get("items", [])
        if probe_events:
            oldest_pending_id = int(probe_events[0]["id"])
            if after_id >= oldest_pending_id:
                # Cursor is at or ahead of the oldest pending event — desync confirmed.
                new_cursor = max(0, oldest_pending_id - 1)
                print(
                    f"[events] WARNING: cursor desync — stored cursor ({after_id}) is ahead of "
                    f"source outbox (oldest pending id={oldest_pending_id}). "
                    f"Source DB may have been restored. Resetting cursor to {new_cursor} "
                    f"to recover pending events. Reprocessing is idempotent (UPSERT)."
                )
                after_id = new_cursor
                _save_event_state(cfg, {"after_id": after_id})
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

    with _mirror_conn(cfg) as conn:
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
                        elif entity_type == "term":
                            _apply_term_delete(cur, entity_id)
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
                            elif entity_type == "term":
                                _apply_term_delete(cur, entity_id)
                            else:
                                _apply_product_delete(cur, entity_id)
                        else:
                            snap_for_webhook = snap
                            if entity_type == "order":
                                _apply_order_upsert(cur, snap)
                            elif entity_type == "term":
                                _apply_term_upsert(cur, snap)
                            else:
                                _apply_product_upsert(cfg, cur, snap)

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

    # Write event log rows (to persistent hub-state DB, not the mirror)
    if log_rows:
        with _hub_conn(cfg) as conn:
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
    _save_event_state(cfg, {"after_id": max_id})
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
        with _mirror_conn(cfg) as conn:
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

    # Hub state DB: media index counts by status (hub DB is persistent; survives swaps)
    media_counts: dict = {}
    try:
        with _hub_conn(cfg) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT download_status, COUNT(*) FROM bdsk_local_media_index GROUP BY download_status"
                )
                for s, cnt in cur.fetchall():
                    media_counts[s] = cnt
    except Exception:
        pass

    # Local state files
    media_state     = _load_sync_state(cfg)
    event_state     = _load_event_state(cfg)
    event_state_mtime = None
    _esp = _event_sync_state_path(cfg)
    if _esp.exists():
        event_state_mtime = datetime.fromtimestamp(
            _esp.stat().st_mtime, tz=timezone.utc
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
    parser.add_argument("--chunk-test",      action="store_true", help="Start a streaming job, fetch one chunk, print result (diagnostic)")
    parser.add_argument("--test",            action="store_true", help="Use test export (50 rows per table)")
    parser.add_argument("--import-only",     metavar="JOB_DIR",   help="Re-import an already-downloaded archive")
    parser.add_argument("--prune",           action="store_true", help="Prune expired local archives and exit")
    parser.add_argument("--media-sync",      action="store_true", help="Run incremental media file sync")
    parser.add_argument("--media-sync-full", action="store_true", help="Run full media file sync (ignores last_sync_at)")
    parser.add_argument("--event-sync",      action="store_true", help="Run event sync (apply pending order/product changes to mirror)")
    parser.add_argument("--status",          action="store_true", help="Show system status overview (read-only)")
    parser.add_argument("--show-config",     action="store_true", help="Print sanitized config and exit (no secrets)")
    args = parser.parse_args()

    cfg = load_config()

    if args.show_config:
        print_config_summary(cfg)
        sys.exit(0)

    try:
        if args.prune:
            prune_old_archives(cfg)
        elif args.health_only:
            health_check(cfg)
        elif args.chunk_test:
            run_chunk_test(cfg)
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

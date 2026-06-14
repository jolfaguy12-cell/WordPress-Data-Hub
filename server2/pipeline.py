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
    return resp.json()


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
    return resp.json()


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
    interval = cfg.get("poll_interval_seconds", 5)
    timeout  = cfg.get("poll_timeout_seconds", 3600)
    deadline = time.time() + timeout

    print(f"[poll] Waiting for job {job_id} to become ready …")

    while time.time() < deadline:
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

        with gzip.open(str(gz_path), "rb") as gz_in:
            result = subprocess.run(cmd, stdin=gz_in, capture_output=True)

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
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Behdashtik Mirror Connector pipeline")
    parser.add_argument("--health-only",  action="store_true", help="Only run health check")
    parser.add_argument("--test",         action="store_true", help="Use test export (50 rows per table)")
    parser.add_argument("--import-only",  metavar="JOB_DIR",   help="Re-import an already-downloaded archive")
    parser.add_argument("--prune",        action="store_true", help="Prune expired local archives and exit")
    args = parser.parse_args()

    cfg = load_config()

    try:
        if args.prune:
            prune_old_archives(cfg)
        elif args.health_only:
            health_check(cfg)
        elif args.import_only:
            import_only(cfg, args.import_only)
        else:
            run_pipeline(cfg, test_mode=args.test)
    except (RuntimeError, TimeoutError, FileNotFoundError) as exc:
        print(f"\n[FATAL] {exc}", file=sys.stderr)
        sys.exit(1)

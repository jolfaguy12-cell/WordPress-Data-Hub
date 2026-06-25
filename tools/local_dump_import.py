#!/usr/bin/env python3
"""
Local mysqldump → mirror import for dev environment.

Since dev.behdashtik.ir runs on the same machine as Server 2, we can dump
the source WordPress DB directly without any WordPress/PHP export path.

Usage:
    cd /root/wordpress-data-hub
    python3 tools/local_dump_import.py

What it does:
    1. Dumps topkal_dbalirus (dev WP) via local mysqldump
    2. Compresses to data/db-archives/<job_id>/<job_id>.sql.gz
    3. Imports into behdashtik_wp_mirror_dev_staging
    4. Validates (table count, core tables, wp_posts row count)
    5. Swaps staging → behdashtik_wp_mirror_dev
    6. hub_state DB is never touched (survives swap by design)
"""

import gzip
import hashlib
import json
import os
import pathlib
import re
import subprocess
import sys
import tempfile
import time
import uuid

sys.path.insert(0, str(pathlib.Path(__file__).parent.parent / "server2"))
from bdsk_config import load_config
from pipeline import import_archive, validate_import, swap_staging_to_mirror, update_meta

# ---------------------------------------------------------------------------
# Source DB — read credentials from dev wp-config.php
# ---------------------------------------------------------------------------

WP_CONFIG = pathlib.Path("/var/www/dev.behdashtik.ir/wp-config.php")

SOURCE_SOCKET = "/run/mysqld/mysqld.sock"   # native mysqld unix socket

def _read_wp_config() -> dict:
    content = WP_CONFIG.read_text(errors="replace")
    def get(key):
        # Handles both: define('KEY', 'val') and define( 'KEY', "val" )
        m = re.search(
            r"""define\s*\(\s*['"]""" + re.escape(key) + r"""['"]\s*,\s*['"]([^'"]*)['"]\s*\)""",
            content,
        )
        return m.group(1) if m else ""
    return {
        "db":   get("DB_NAME"),
        "user": get("DB_USER"),
        "pass": get("DB_PASSWORD"),
    }


def _table_list(src: dict, cnf_file: str) -> list[str]:
    result = subprocess.run(
        ["mysql",
         f"--defaults-extra-file={cnf_file}",
         "--batch", "--skip-column-names",
         src["db"],
         "-e", "SHOW TABLES;"],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(f"Could not list source tables:\n{result.stderr}")
    return [t.strip() for t in result.stdout.splitlines() if t.strip()]


def _run_dump(src: dict, cnf_file: str, out_path: pathlib.Path) -> None:
    cmd = [
        "mysqldump",
        f"--defaults-extra-file={cnf_file}",
        "--single-transaction",
        "--quick",
        "--set-gtid-purged=OFF",
        "--no-tablespaces",
        src["db"],
    ]
    print(f"[dump] Running mysqldump on {src['db']} …")
    t0 = time.time()
    with gzip.open(str(out_path), "wb", compresslevel=6) as gz:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        total = 0
        while True:
            chunk = proc.stdout.read(65536)
            if not chunk:
                break
            gz.write(chunk)
            total += len(chunk)
            if total % (5 * 1024 * 1024) < 65536:
                mb = total / 1024 / 1024
                print(f"[dump]   {mb:.1f} MB SQL written …", flush=True)
        _, stderr = proc.communicate()
    if proc.returncode != 0:
        raise RuntimeError(f"[dump] mysqldump failed:\n{stderr.decode(errors='replace')}")
    elapsed = time.time() - t0
    size_mb = out_path.stat().st_size / 1024 / 1024
    print(f"[dump] Done in {elapsed:.1f}s — compressed size: {size_mb:.2f} MB")


def main() -> None:
    print("=" * 60)
    print("Behdashtik Dev — Local mysqldump → Mirror Import")
    print("=" * 60)

    cfg = load_config()

    # Safety: dev only
    env = cfg.get("env", "")
    mirror = cfg["mirror_db"]["name"]
    if env != "dev" or not mirror.endswith("_dev"):
        print(f"[ABORT] ENV={env}, mirror={mirror} — this script is dev-only.", file=sys.stderr)
        sys.exit(1)

    src = _read_wp_config()
    if not src["db"] or not src["user"]:
        print("[ABORT] Could not read source DB credentials from wp-config.php", file=sys.stderr)
        sys.exit(1)

    print(f"[info] Source DB : {src['db']}  (user: {src['user']}, socket: {SOURCE_SOCKET})")
    print(f"[info] Mirror DB : {mirror}")

    # Write a temp MySQL credentials file so password never appears in ps output
    with tempfile.NamedTemporaryFile(mode="w", suffix=".cnf", delete=False) as f:
        cnf_file = f.name
        f.write("[client]\n")
        f.write(f"socket={SOURCE_SOCKET}\n")
        f.write(f"user={src['user']}\n")
        f.write(f"password={src['pass']}\n")
    os.chmod(cnf_file, 0o600)

    try:
        # 1. List tables for meta.json
        print("[info] Listing source tables …")
        tables = _table_list(src, cnf_file)
        print(f"[info] {len(tables)} tables found in source DB")

        # 2. Create archive directory
        job_id  = str(uuid.uuid4())
        job_dir = pathlib.Path(cfg["archive_storage_path"]) / job_id
        job_dir.mkdir(parents=True, exist_ok=True)
        archive_path = job_dir / f"{job_id}.sql.gz"

        # 3. Dump
        _run_dump(src, cnf_file, archive_path)

        sha256 = hashlib.sha256(archive_path.read_bytes()).hexdigest()
        size   = archive_path.stat().st_size

        # 4. Write meta.json in pipeline-compatible format
        meta = {
            "job_id":          job_id,
            "export_mode":     "local_direct_dump",
            "source_db":       src["db"],
            "source_socket":   SOURCE_SOCKET,
            "tables_included": tables,
            "db_prefix":       "wp_",
            "parts": [
                {
                    "filename": archive_path.name,
                    "size":     size,
                    "sha256":   sha256,
                }
            ],
        }
        (job_dir / "meta.json").write_text(json.dumps(meta, indent=2))
        print(f"[info] meta.json written to {job_dir}")

        # 5. Import into staging via pipeline
        staging_db = import_archive(cfg, job_id, job_dir)

        # 6. Validate
        validate_import(cfg, job_id, staging_db, job_dir)

        # 7. Swap staging → mirror (hub_state is untouched)
        swap_staging_to_mirror(cfg, staging_db)
        update_meta(job_dir, "success")

    finally:
        os.unlink(cnf_file)

    # 8. Post-swap: recalculate variable product min/max prices in lookup table.
    # WooCommerce stores prices on variations, but the parent lookup row is only updated
    # by WP hooks at runtime. After a raw dump/restore, parent rows may have min_price=0.
    # Recalculate from variation lookup rows so the Hub shows correct prices.
    import pymysql
    db_cfg = cfg["mirror_db"]
    rconn = pymysql.connect(
        host=db_cfg["host"], port=db_cfg.get("port", 3306),
        user=db_cfg["user"], password=db_cfg["password"],
        database=mirror, charset="utf8mb4",
    )
    with rconn.cursor() as cur:
        # MySQL/MariaDB won't allow correlated subqueries on the target table; use JOIN form.
        cur.execute("""
            UPDATE wp_wc_product_meta_lookup pl
            JOIN wp_posts p ON p.ID = pl.product_id AND p.post_type = 'product'
            JOIN (
                SELECT v.post_parent AS parent_id,
                       MIN(vl.min_price) AS new_min,
                       MAX(vl.max_price) AS new_max
                FROM wp_posts v
                JOIN wp_wc_product_meta_lookup vl ON vl.product_id = v.ID
                WHERE v.post_type = 'product_variation'
                  AND v.post_status = 'publish'
                  AND vl.min_price > 0
                GROUP BY v.post_parent
            ) AS var_prices ON var_prices.parent_id = p.ID
            SET pl.min_price = var_prices.new_min,
                pl.max_price = var_prices.new_max
            WHERE pl.min_price = 0
        """)
        updated = cur.rowcount
        rconn.commit()
    rconn.close()
    print(f"[fix] Variable product lookup recalculated: {updated} parent rows updated")

    # 9. Post-swap sanity
    print()
    print("=" * 60)
    print("Post-import check")
    print("=" * 60)
    conn = pymysql.connect(
        host=db_cfg["host"], port=db_cfg.get("port", 3306),
        user=db_cfg["user"], password=db_cfg["password"],
        database=mirror, charset="utf8mb4",
        cursorclass=pymysql.cursors.DictCursor,
    )
    with conn.cursor() as cur:
        cur.execute("SELECT post_status, COUNT(*) cnt FROM wp_posts WHERE post_type='product' GROUP BY post_status ORDER BY cnt DESC")
        rows = cur.fetchall()
        print("Products by status:")
        for r in rows:
            print(f"  {r['post_status']}: {r['cnt']}")

        cur.execute("SELECT COUNT(*) cnt FROM wp_wc_orders")
        orders = cur.fetchone()
        print(f"Orders total: {orders['cnt']}")

        cur.execute("SELECT COUNT(*) cnt FROM wp_posts WHERE post_type='product' AND post_status='publish'")
        pub = cur.fetchone()
        print(f"Published products: {pub['cnt']}")

        cur.execute("SELECT COUNT(*) cnt FROM wp_wc_product_meta_lookup WHERE min_price = 0 AND product_id IN (SELECT ID FROM wp_posts WHERE post_type='product' AND post_status='publish')")
        no_price = cur.fetchone()
        print(f"Published products with no price in lookup: {no_price['cnt']}")
    conn.close()

    print()
    print("=" * 60)
    print(f"Done. Mirror '{mirror}' fully populated from local dump.")
    print(f"Archive: {job_dir}")
    print("=" * 60)


if __name__ == "__main__":
    main()

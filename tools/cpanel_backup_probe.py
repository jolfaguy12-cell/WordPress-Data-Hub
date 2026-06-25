#!/usr/bin/env python3
"""
cPanel database backup probe — read-only, no import, no full backup.

Usage:
    export CPANEL_TOKEN=<token>
    python3 tools/cpanel_backup_probe.py

Probes only:
  - Mysql/list_databases
  - Mysql/dump_database (UAPI)
  - Mysql/dumpdb (API2 legacy)
  - getsqlbackup CGI
  - Mysql/list_remote_accesshosts
  - Fileman listing for pre-existing DB files

Never runs full backup. Never imports. Never prints token.
"""

import os
import pathlib
import sys
import json
import urllib.request
import ssl
import time

CPANEL_HOST = os.environ.get("CPANEL_HOST", "pdc-251.pentaserverns.com")
CPANEL_PORT = os.environ.get("CPANEL_PORT", "2083")
CPANEL_USER = os.environ.get("CPANEL_USER", "topkal")
CPANEL_TOKEN = os.environ.get("CPANEL_TOKEN", "")
TARGET_DB    = os.environ.get("CPANEL_DB", "topkal_dbalirus")
PROBE_DIR    = pathlib.Path("/root/wordpress-data-hub/data/cpanel-probe")

if not CPANEL_TOKEN:
    print("[ABORT] Set CPANEL_TOKEN env var", file=sys.stderr)
    sys.exit(1)

BASE = f"https://{CPANEL_HOST}:{CPANEL_PORT}"
AUTH_HEADER = f"cpanel {CPANEL_USER}:{CPANEL_TOKEN}"
SSL_CTX = ssl.create_default_context()
SSL_CTX.check_hostname = False
SSL_CTX.verify_mode = ssl.CERT_NONE

results = {}


def _get(path: str, timeout: int = 15) -> tuple[int, bytes]:
    url = BASE + path
    req = urllib.request.Request(url, headers={"Authorization": AUTH_HEADER})
    try:
        with urllib.request.urlopen(req, context=SSL_CTX, timeout=timeout) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()
    except Exception as e:
        return 0, str(e).encode()


def _json(body: bytes) -> dict:
    try:
        return json.loads(body)
    except Exception:
        return {}


def check(label: str, path: str, *, download_to: pathlib.Path | None = None) -> dict:
    t0 = time.time()
    code, body = _get(path, timeout=30 if download_to else 15)
    elapsed = time.time() - t0
    result = {"http": code, "elapsed": round(elapsed, 2)}

    if download_to and code == 200 and len(body) > 100:
        download_to.parent.mkdir(parents=True, exist_ok=True)
        download_to.write_bytes(body)
        result["downloaded"] = str(download_to)
        result["size"] = len(body)
    else:
        d = _json(body)
        result["status"] = d.get("status") if d else None
        result["errors"] = (d.get("errors") or d.get("cpanelresult", {}).get("error"))
        result["data_sample"] = (d.get("data") or "")[:200] if d else body[:200].decode(errors="replace")

    results[label] = result
    return result


print("=" * 60)
print("cPanel DB Backup Probe")
print(f"Host : {CPANEL_HOST}:{CPANEL_PORT}")
print(f"User : {CPANEL_USER}")
print(f"DB   : {TARGET_DB}")
print("=" * 60)

# 1. Auth + DB list
r = check("db_list", f"/execute/Mysql/list_databases")
dbs = []
if r["http"] == 200 and r.get("status") == 1:
    _, body = _get("/execute/Mysql/list_databases")
    d = _json(body)
    dbs = [x.get("database", x) for x in (d.get("data") or [])]
print(f"\n[1] Auth + DB list: HTTP {r['http']} | auth={'OK' if r.get('status')==1 else 'FAIL'} | dbs={dbs}")

# 2. UAPI dump_database
r2 = check("uapi_dump", f"/execute/Mysql/dump_database?db={TARGET_DB}")
print(f"[2] UAPI dump_database: HTTP {r2['http']} | {r2.get('errors') or 'ok'}")

# 3. Legacy API2 dumpdb
r3 = check("api2_dumpdb",
    f"/json-api/cpanel?cpanel_jsonapi_version=2&cpanel_jsonapi_module=Mysql&cpanel_jsonapi_func=dumpdb&db={TARGET_DB}")
print(f"[3] API2 Mysql::dumpdb: HTTP {r3['http']} | {r3.get('errors') or 'ok'}")

# 4. getsqlbackup CGI
out = PROBE_DIR / f"{TARGET_DB}.sql.gz"
r4 = check("getsqlbackup", f"/getsqlbackup/{TARGET_DB}.sql.gz", download_to=out)
downloaded = r4.get("downloaded")
print(f"[4] getsqlbackup: HTTP {r4['http']} | {'DOWNLOADED → ' + downloaded if downloaded else 'FAILED'}")

# 5. Remote MySQL hosts
r5 = check("remote_hosts", "/execute/Mysql/list_remote_accesshosts")
_, body5 = _get("/execute/Mysql/list_remote_accesshosts")
remote_hosts = [h.get("host", h) for h in (_json(body5).get("data") or [])]
print(f"[5] Remote MySQL hosts: {remote_hosts or '(none whitelisted)'}")

print()
print("=" * 60)
print("SUMMARY")
print("=" * 60)
print(f"cPanel auth          : {'YES' if results['db_list'].get('status')==1 else 'NO'}")
print(f"DB {TARGET_DB} found : {'YES' if TARGET_DB in dbs else 'NO'}")
print(f"UAPI dump_database   : {'YES' if results['uapi_dump']['http']==200 and not results['uapi_dump'].get('errors') else 'NO — ' + str(results['uapi_dump'].get('errors',''))[:80]}")
print(f"API2 dumpdb          : {'YES' if results['api2_dumpdb']['http']==200 and not results['api2_dumpdb'].get('errors') else 'NO — ' + str(results['api2_dumpdb'].get('errors',''))[:80]}")
print(f"getsqlbackup CGI     : {'YES → ' + downloaded + ' (' + str(r4.get('size','?')) + ' bytes)' if downloaded else 'NO — HTTP ' + str(results['getsqlbackup']['http'])}")
print(f"Full backup avoided  : YES")
print(f"Remote MySQL access  : {'configured: ' + str(remote_hosts) if remote_hosts else 'NOT configured — no hosts whitelisted'}")

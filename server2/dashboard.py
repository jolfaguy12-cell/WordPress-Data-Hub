#!/usr/bin/env python3
"""
Behdashtik Hub Dashboard — hub.behdashtik.ir
============================================
Multi-page Flask app: login, status dashboard, user management.
Binds to 127.0.0.1:8089 (nginx reverse-proxies with SSL).

Run via systemd:   systemctl start bdsk-dashboard
Bootstrap users:   python3 dashboard.py --create-user
"""

import argparse
import getpass
import hashlib
import hmac
import json
import os
import pathlib
import secrets
import sqlite3
import sys
import time
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

sys.path.insert(0, str(pathlib.Path(__file__).parent))

import jinja2
from flask import (Flask, Response, flash, redirect, render_template,
                   request, session, url_for)
from data_api import data_api as data_api_bp
from pipeline import get_status_data, load_config
from werkzeug.security import check_password_hash, generate_password_hash

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

BASE_DIR = pathlib.Path(__file__).parent
DB_PATH  = BASE_DIR / "hub.db"

# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------

def init_db() -> None:
    with _db() as cur:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS users (
                id             INTEGER PRIMARY KEY AUTOINCREMENT,
                username       TEXT UNIQUE NOT NULL,
                password_hash  TEXT NOT NULL,
                created_at     TEXT NOT NULL,
                last_login_at  TEXT
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS webhook_endpoints (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                name         TEXT NOT NULL,
                url          TEXT NOT NULL,
                secret       TEXT NOT NULL,
                event_filter TEXT NOT NULL,
                enabled      INTEGER NOT NULL DEFAULT 1,
                created_at   TEXT NOT NULL
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS webhook_deliveries (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                endpoint_id INTEGER NOT NULL,
                event       TEXT NOT NULL,
                entity_id   INTEGER NOT NULL,
                sent_at     TEXT NOT NULL,
                http_status INTEGER,
                success     INTEGER NOT NULL,
                error       TEXT
            )
        """)


@contextmanager
def _db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        cur = conn.cursor()
        yield cur
        conn.commit()
    finally:
        conn.close()


def get_user_by_id(uid: int) -> sqlite3.Row | None:
    with _db() as cur:
        cur.execute("SELECT * FROM users WHERE id = ?", (uid,))
        return cur.fetchone()


def get_user_by_username(username: str) -> sqlite3.Row | None:
    with _db() as cur:
        cur.execute("SELECT * FROM users WHERE username = ?", (username,))
        return cur.fetchone()


def list_users() -> list:
    with _db() as cur:
        cur.execute("SELECT id, username, created_at, last_login_at FROM users ORDER BY id")
        return cur.fetchall()


def count_users() -> int:
    with _db() as cur:
        cur.execute("SELECT COUNT(*) FROM users")
        return cur.fetchone()[0]


def create_user(username: str, password: str) -> None:
    h = generate_password_hash(password)
    now = datetime.now(timezone.utc).isoformat()
    with _db() as cur:
        cur.execute(
            "INSERT INTO users (username, password_hash, created_at) VALUES (?, ?, ?)",
            (username, h, now),
        )


def update_password(uid: int, new_password: str) -> None:
    h = generate_password_hash(new_password)
    with _db() as cur:
        cur.execute("UPDATE users SET password_hash = ? WHERE id = ?", (h, uid))


def delete_user(uid: int) -> None:
    with _db() as cur:
        cur.execute("DELETE FROM users WHERE id = ?", (uid,))


def record_login(uid: int) -> None:
    now = datetime.now(timezone.utc).isoformat()
    with _db() as cur:
        cur.execute("UPDATE users SET last_login_at = ? WHERE id = ?", (now, uid))


# ---------------------------------------------------------------------------
# Webhook DB helpers
# ---------------------------------------------------------------------------

ALL_EVENTS = ["product.upserted", "product.deleted", "order.upserted", "order.deleted"]


def list_endpoints() -> list:
    with _db() as cur:
        cur.execute("SELECT * FROM webhook_endpoints ORDER BY id")
        return cur.fetchall()


def get_endpoint(eid: int) -> sqlite3.Row | None:
    with _db() as cur:
        cur.execute("SELECT * FROM webhook_endpoints WHERE id = ?", (eid,))
        return cur.fetchone()


def create_endpoint(name: str, url: str, event_filter: list) -> tuple[int, str]:
    new_secret = secrets.token_hex(32)
    now        = datetime.now(timezone.utc).isoformat()
    with _db() as cur:
        cur.execute(
            "INSERT INTO webhook_endpoints (name, url, secret, event_filter, enabled, created_at) "
            "VALUES (?, ?, ?, ?, 1, ?)",
            (name, url, new_secret, json.dumps(event_filter), now),
        )
        return cur.lastrowid, new_secret


def update_endpoint(eid: int, name: str, url: str, event_filter: list, enabled: bool) -> None:
    with _db() as cur:
        cur.execute(
            "UPDATE webhook_endpoints SET name=?, url=?, event_filter=?, enabled=? WHERE id=?",
            (name, url, json.dumps(event_filter), 1 if enabled else 0, eid),
        )


def delete_endpoint(eid: int) -> None:
    with _db() as cur:
        cur.execute("DELETE FROM webhook_endpoints WHERE id = ?", (eid,))
        cur.execute("DELETE FROM webhook_deliveries WHERE endpoint_id = ?", (eid,))


def regenerate_secret(eid: int) -> str:
    new_secret = secrets.token_hex(32)
    with _db() as cur:
        cur.execute("UPDATE webhook_endpoints SET secret=? WHERE id=?", (new_secret, eid))
    return new_secret


def list_deliveries(limit: int = 60, endpoint_id: int | None = None) -> list:
    with _db() as cur:
        if endpoint_id is not None:
            cur.execute(
                "SELECT d.*, e.name AS endpoint_name FROM webhook_deliveries d "
                "JOIN webhook_endpoints e ON d.endpoint_id = e.id "
                "WHERE d.endpoint_id = ? ORDER BY d.id DESC LIMIT ?",
                (endpoint_id, limit),
            )
        else:
            cur.execute(
                "SELECT d.*, e.name AS endpoint_name FROM webhook_deliveries d "
                "JOIN webhook_endpoints e ON d.endpoint_id = e.id "
                "ORDER BY d.id DESC LIMIT ?",
                (limit,),
            )
        return cur.fetchall()


def get_endpoint_last_delivery(eid: int) -> sqlite3.Row | None:
    with _db() as cur:
        cur.execute(
            "SELECT success, http_status, sent_at FROM webhook_deliveries "
            "WHERE endpoint_id = ? ORDER BY id DESC LIMIT 1",
            (eid,),
        )
        return cur.fetchone()


def prune_webhook_deliveries() -> int:
    cutoff = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
    with _db() as cur:
        cur.execute("DELETE FROM webhook_deliveries WHERE sent_at < ?", (cutoff,))
        return cur.rowcount


# ---------------------------------------------------------------------------
# Login rate limiting (in-memory, resets on restart — acceptable for single-process)
# ---------------------------------------------------------------------------

_LOGIN_FAILS: dict[str, tuple[int, float]] = {}  # ip_hash -> (count, expires_at)
MAX_FAILS    = 10
WINDOW_SECS  = 15 * 60


def _ip_hash(ip: str) -> str:
    return hashlib.sha256(ip.encode()).hexdigest()


def _get_fail_count(ip: str) -> int:
    key = _ip_hash(ip)
    entry = _LOGIN_FAILS.get(key)
    if not entry:
        return 0
    count, expires = entry
    if time.time() > expires:
        del _LOGIN_FAILS[key]
        return 0
    return count


def _increment_fail(ip: str) -> int:
    key   = _ip_hash(ip)
    count = _get_fail_count(ip) + 1
    _LOGIN_FAILS[key] = (count, time.time() + WINDOW_SECS)
    return count


def _reset_fail(ip: str) -> None:
    _LOGIN_FAILS.pop(_ip_hash(ip), None)


# ---------------------------------------------------------------------------
# Flask app
# ---------------------------------------------------------------------------

app = Flask(__name__)
app.register_blueprint(data_api_bp)


def _init_app() -> None:
    cfg = load_config()
    hub = cfg.get("hub", {})
    key = hub.get("secret_key", "")
    if not key:
        sys.exit("[ERROR] config.json missing hub.secret_key. Generate with: "
                 "python3 -c \"import secrets; print(secrets.token_hex(32))\"")
    app.secret_key = key
    lifetime_h = int(hub.get("session_lifetime_hours", 12))
    app.permanent_session_lifetime = timedelta(hours=lifetime_h)
    app.config["PIPELINE_CFG"] = cfg
    app.config["DATA_API_KEY"] = (
        cfg.get("data_api", {}).get("key", "")
        or os.environ.get("BDSK_DATA_API_KEY", "")
    )
    init_db()
    if count_users() == 0:
        sys.exit(
            "[ERROR] No hub users exist. Create the first account with:\n"
            "  python3 dashboard.py --create-user"
        )


# ---------------------------------------------------------------------------
# Auth helpers
# ---------------------------------------------------------------------------

def _logged_in() -> bool:
    return bool(session.get("user_id"))


def _current_username() -> str:
    uid = session.get("user_id")
    if not uid:
        return ""
    row = get_user_by_id(uid)
    return row["username"] if row else ""


def _require_login():
    if not _logged_in():
        return redirect(url_for("login_page"))
    return None


# ---------------------------------------------------------------------------
# Shared CSS + base template
# ---------------------------------------------------------------------------

CSS = """
*, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0 }
body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
       background: #f3f4f6; color: #111827; min-height: 100vh }
a { color: #2563eb; text-decoration: none }
a:hover { text-decoration: underline }

/* Nav */
.nav { background: #1e293b; color: #e2e8f0; padding: 0 24px;
       display: flex; align-items: center; height: 52px; gap: 0 }
.nav .brand { font-weight: 700; font-size: 1rem; margin-right: 28px; color: #f8fafc }
.nav a { color: #94a3b8; padding: 0 14px; height: 52px; display: flex;
          align-items: center; font-size: .88rem; border-bottom: 2px solid transparent }
.nav a:hover, .nav a.active { color: #f8fafc; border-bottom-color: #3b82f6;
                                text-decoration: none }
.nav .spacer { flex: 1 }
.nav .user-chip { font-size: .8rem; color: #64748b; margin-right: 8px }

/* Layout */
.page { padding: 28px 24px; max-width: 1100px; margin: 0 auto }
h1 { font-size: 1.4rem; font-weight: 700; margin-bottom: 20px; color: #1e293b }
h2 { font-size: 1rem; font-weight: 600; margin-bottom: 14px; color: #374151 }

/* Cards */
.grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(280px, 1fr)); gap: 16px }
.card { background: #fff; border-radius: 10px; padding: 20px;
        box-shadow: 0 1px 3px rgba(0,0,0,.08), 0 1px 2px rgba(0,0,0,.04) }
.card h2 { font-size: .78rem; text-transform: uppercase; letter-spacing: .06em;
           color: #6b7280; margin-bottom: 14px }
.row { display: flex; justify-content: space-between; align-items: baseline;
       padding: 7px 0; border-bottom: 1px solid #f3f4f6; font-size: .88rem }
.row:last-child { border-bottom: none }
.row .lbl { color: #6b7280 }
.row .val { font-weight: 500; text-align: right; word-break: break-all; max-width: 60% }
.num { font-size: 2.2rem; font-weight: 800; color: #1e293b; line-height: 1 }
.sub { font-size: .78rem; color: #9ca3af; margin-top: 4px }

/* Badges */
.badge { display: inline-block; padding: 2px 9px; border-radius: 20px;
         font-size: .75rem; font-weight: 600; white-space: nowrap }
.badge-ok   { background: #dcfce7; color: #166534 }
.badge-warn { background: #fef9c3; color: #854d0e }
.badge-err  { background: #fee2e2; color: #991b1b }

/* Alerts */
.alert { padding: 12px 16px; border-radius: 8px; margin-bottom: 18px; font-size: .9rem }
.alert-ok  { background: #dcfce7; color: #14532d; border: 1px solid #bbf7d0 }
.alert-err { background: #fee2e2; color: #7f1d1d; border: 1px solid #fecaca }

/* Login page */
.login-wrap { min-height: 100vh; display: flex; align-items: center;
              justify-content: center; background: #f3f4f6 }
.login-box { background: #fff; border-radius: 12px; padding: 40px;
             box-shadow: 0 4px 24px rgba(0,0,0,.08); width: 100%; max-width: 380px }
.login-box h1 { text-align: center; margin-bottom: 6px; font-size: 1.3rem }
.login-box .sub { text-align: center; color: #6b7280; font-size: .85rem; margin-bottom: 28px }

/* Forms */
.form-group { margin-bottom: 16px }
label { display: block; font-size: .85rem; font-weight: 500;
        color: #374151; margin-bottom: 6px }
input[type=text], input[type=password] {
  width: 100%; padding: 9px 12px; border: 1px solid #d1d5db;
  border-radius: 7px; font-size: .9rem; outline: none;
  transition: border-color .15s }
input[type=text]:focus, input[type=password]:focus { border-color: #3b82f6 }
.btn { display: inline-flex; align-items: center; justify-content: center;
       padding: 9px 18px; border: none; border-radius: 7px; font-size: .88rem;
       font-weight: 500; cursor: pointer; transition: background .15s }
.btn-primary { background: #2563eb; color: #fff }
.btn-primary:hover { background: #1d4ed8 }
.btn-danger  { background: #ef4444; color: #fff; padding: 6px 12px; font-size: .8rem }
.btn-danger:hover  { background: #dc2626 }
.btn-secondary { background: #e5e7eb; color: #374151; padding: 6px 12px; font-size: .8rem }
.btn-secondary:hover { background: #d1d5db }
.btn-full { width: 100% }

/* Table */
table { width: 100%; border-collapse: collapse; font-size: .88rem }
th { background: #f9fafb; padding: 10px 14px; text-align: left;
     font-size: .78rem; text-transform: uppercase; letter-spacing: .04em;
     color: #6b7280; border-bottom: 2px solid #e5e7eb }
td { padding: 10px 14px; border-bottom: 1px solid #f3f4f6; color: #374151 }
tr:last-child td { border-bottom: none }
.actions { display: flex; gap: 6px; align-items: center }

/* Add-user form / add-endpoint form */
.add-form { background: #fff; border-radius: 10px; padding: 20px;
            box-shadow: 0 1px 3px rgba(0,0,0,.08); margin-top: 24px }
.add-form .inline { display: flex; gap: 10px; flex-wrap: wrap; align-items: flex-end }
.add-form .inline .form-group { margin-bottom: 0; flex: 1; min-width: 140px }

/* Secret reveal box */
.secret-box { background: #f0fdf4; border: 1px solid #86efac; border-radius: 8px;
              padding: 14px 16px; margin-bottom: 18px }
.secret-box .label { font-size: .78rem; font-weight: 600; color: #166534;
                     text-transform: uppercase; letter-spacing: .05em; margin-bottom: 6px }
.secret-box code { font-family: ui-monospace, monospace; font-size: .85rem;
                   word-break: break-all; color: #14532d; display: block }

/* Event filter checkboxes */
.check-group { display: flex; gap: 14px; flex-wrap: wrap; margin-top: 6px }
.check-group label { display: flex; align-items: center; gap: 6px;
                     font-size: .85rem; font-weight: 400; color: #374151; cursor: pointer }
.check-group input[type=checkbox] { width: auto; accent-color: #2563eb }

/* Status dot */
.dot { display: inline-block; width: 8px; height: 8px; border-radius: 50%;
       margin-right: 5px; vertical-align: middle }
.dot-ok  { background: #22c55e }
.dot-err { background: #ef4444 }
.dot-off { background: #9ca3af }

/* URL cell truncation */
.url-cell { max-width: 260px; overflow: hidden; text-overflow: ellipsis;
            white-space: nowrap; font-size: .82rem; color: #6b7280 }

@media (max-width: 600px) {
  .page { padding: 16px }
  .grid { grid-template-columns: 1fr }
  .nav a { padding: 0 8px }
}
"""

BASE = """\
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
{%- if refresh %}<meta http-equiv="refresh" content="{{ refresh }}">{%- endif %}
<title>{% block title %}Behdashtik Hub{% endblock %}</title>
<style>{{ css }}</style>
</head>
<body>
{% block nav %}
<nav class="nav">
  <span class="brand">&#x1F4CA; Behdashtik Hub</span>
  <a href="{{ url_for('dashboard') }}" class="{{ 'active' if active == 'dashboard' }}">Dashboard</a>
  <a href="{{ url_for('users_page') }}" class="{{ 'active' if active == 'users' }}">Users</a>
  <a href="{{ url_for('webhooks_page') }}" class="{{ 'active' if active == 'webhooks' }}">Webhooks</a>
  <span class="spacer"></span>
  <span class="user-chip">{{ current_user }}</span>
  <form method="post" action="{{ url_for('logout') }}" style="margin:0">
    <button class="btn btn-secondary" style="padding:5px 12px;font-size:.8rem">Logout</button>
  </form>
</nav>
{% endblock %}
{% with msgs = get_flashed_messages(with_categories=True) %}
  {% if msgs %}
  <div class="page" style="padding-bottom:0">
    {% for cat, msg in msgs %}
    <div class="alert {{ 'alert-ok' if cat == 'ok' else 'alert-err' }}">{{ msg }}</div>
    {% endfor %}
  </div>
  {% endif %}
{% endwith %}
{% block body %}{% endblock %}
</body>
</html>"""

LOGIN_PAGE = """\
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Login — Behdashtik Hub</title>
<style>{{ css }}</style>
</head>
<body>
<div class="login-wrap">
  <div class="login-box">
    <h1>Behdashtik Hub</h1>
    <p class="sub">Sign in to continue</p>
    {% for cat, msg in msgs %}
    <div class="alert {{ 'alert-ok' if cat == 'ok' else 'alert-err' }}" style="margin-bottom:18px">{{ msg }}</div>
    {% endfor %}
    <form method="post">
      <div class="form-group">
        <label for="username">Username</label>
        <input type="text" id="username" name="username"
               value="{{ username or '' }}" autocomplete="username" autofocus>
      </div>
      <div class="form-group">
        <label for="password">Password</label>
        <input type="password" id="password" name="password" autocomplete="current-password">
      </div>
      <button type="submit" class="btn btn-primary btn-full">Sign in</button>
    </form>
  </div>
</div>
</body>
</html>"""

DASHBOARD_PAGE = """\
{% extends 'base.html' %}
{% block title %}Dashboard — Behdashtik Hub{% endblock %}
{% block body %}
<div class="page">
  <h1>System Status <span style="font-size:.75rem;font-weight:400;color:#9ca3af">
    auto-refreshes every 60 s &nbsp;·&nbsp; {{ now }}</span></h1>
  <div class="grid">

    <div class="card">
      <h2>WordPress</h2>
      <div class="row"><span class="lbl">Status</span><span class="val">
        <span class="badge {{ 'badge-ok' if wp_ok else 'badge-err' }}">{{ '● OK' if wp_ok else '● Error' }}</span>
      </span></div>
      <div class="row"><span class="lbl">Plugin</span><span class="val">v{{ plugin_version }}</span></div>
      <div class="row"><span class="lbl">WordPress</span><span class="val">{{ wp_version }}</span></div>
      <div class="row"><span class="lbl">WooCommerce</span><span class="val">{{ wc_version }}</span></div>
      <div class="row"><span class="lbl">PHP</span><span class="val">{{ php_version }}</span></div>
      <div class="row"><span class="lbl">Connector</span><span class="val">
        <span class="badge {{ 'badge-ok' if connector_enabled else 'badge-err' }}">
          {{ 'enabled' if connector_enabled else 'DISABLED' }}</span>
      </span></div>
      <div class="row"><span class="lbl">Connection</span><span class="val">
        <span class="badge {{ conn_cls }}">{{ conn_status }}</span>
      </span></div>
      <div class="row"><span class="lbl">Last request</span><span class="val">{{ last_req or '—' }}</span></div>
      <div class="row"><span class="lbl">Last cleanup</span><span class="val">{{ last_cleanup or '—' }}</span></div>
    </div>

    <div class="card">
      <h2>DB Mirror</h2>
      {% if job %}
      <div class="row"><span class="lbl">Job</span><span class="val">{{ job.job_id[:8] }}…</span></div>
      <div class="row"><span class="lbl">Status</span><span class="val">
        <span class="badge {{ 'badge-ok' if job.status in ('done','completed','downloaded') else ('badge-warn' if job.status in ('running','pending') else 'badge-err') }}">
          {{ job.status }}</span></span></div>
      <div class="row"><span class="lbl">Created</span><span class="val">{{ job.created_at or '—' }}</span></div>
      <div class="row"><span class="lbl">Finished</span><span class="val">{{ job.finished_at or '—' }}</span></div>
      {% else %}
      <div class="row"><span class="lbl">Status</span><span class="val">No jobs</span></div>
      {% endif %}
      <div class="row"><span class="lbl">Archives on disk</span><span class="val">{{ archive_count }}</span></div>
    </div>

    <div class="card">
      <h2>Media</h2>
      <div class="row"><span class="lbl">Index</span><span class="val">{{ media_index_status }}</span></div>
      {% if mc %}
      <div class="row"><span class="lbl">Downloaded</span><span class="val">{{ mc.get('downloaded',0) + mc.get('active',0) }}</span></div>
      <div class="row"><span class="lbl">Pending</span><span class="val">{{ mc.get('pending',0) + mc.get('queued',0) }}</span></div>
      <div class="row"><span class="lbl">Failed</span><span class="val">{{ mc.get('failed',0) }}</span></div>
      {% endif %}
      <div class="row"><span class="lbl">Files on disk</span><span class="val">{{ media_files }}</span></div>
      <div class="row"><span class="lbl">Last sync</span><span class="val">{{ last_sync_at or '—' }}</span></div>
    </div>

    <div class="card">
      <h2>Event Outbox</h2>
      <div class="num">{{ pending_events }}</div>
      <div class="sub">Pending on WordPress</div>
      <br>
      <div class="row"><span class="lbl">Cursor</span><span class="val">after_id={{ event_after_id }}</span></div>
      <div class="row"><span class="lbl">Last sync</span><span class="val">{{ event_last_run or 'never' }}</span></div>
    </div>

  </div>
</div>
{% endblock %}"""

USERS_PAGE = """\
{% extends 'base.html' %}
{% block title %}Users — Behdashtik Hub{% endblock %}
{% block body %}
<div class="page">
  <h1>User Management</h1>

  <div class="card">
    <h2>Accounts</h2>
    <table>
      <thead><tr>
        <th>Username</th><th>Created</th><th>Last login</th><th>Actions</th>
      </tr></thead>
      <tbody>
      {% for u in users %}
      <tr>
        <td><strong>{{ u['username'] }}</strong></td>
        <td>{{ u['created_at'][:19].replace('T',' ') if u['created_at'] else '—' }}</td>
        <td>{{ u['last_login_at'][:19].replace('T',' ') if u['last_login_at'] else 'never' }}</td>
        <td>
          <div class="actions">
            <form method="post" action="{{ url_for('change_password', uid=u['id']) }}">
              <input type="hidden" name="new_password" id="np_{{ u['id'] }}" value="">
              <button type="button" class="btn btn-secondary"
                onclick="var p=prompt('New password for {{ u[\'username\'] }}:');
                         if(p){document.getElementById('np_{{ u[\'id\'] }}').value=p;this.form.submit()}">
                Change password
              </button>
            </form>
            {% if total_users > 1 %}
            <form method="post" action="{{ url_for('delete_user_route', uid=u['id']) }}"
                  onsubmit="return confirm('Delete {{ u[\'username\'] }}?')">
              <button type="submit" class="btn btn-danger">Delete</button>
            </form>
            {% else %}
            <span style="font-size:.78rem;color:#9ca3af">last account</span>
            {% endif %}
          </div>
        </td>
      </tr>
      {% endfor %}
      </tbody>
    </table>
  </div>

  <div class="add-form">
    <h2>Add account</h2>
    <form method="post" action="{{ url_for('add_user') }}">
      <div class="inline">
        <div class="form-group">
          <label>Username</label>
          <input type="text" name="username" autocomplete="off" required>
        </div>
        <div class="form-group">
          <label>Password</label>
          <input type="password" name="password" required>
        </div>
        <div class="form-group" style="align-self:flex-end">
          <button type="submit" class="btn btn-primary">Add user</button>
        </div>
      </div>
    </form>
  </div>
</div>
{% endblock %}"""

WEBHOOKS_PAGE = """\
{% extends 'base.html' %}
{% block title %}Webhooks — Behdashtik Hub{% endblock %}
{% block body %}
<div class="page">
  <h1>Webhooks</h1>

  {%- if new_secret %}
  <div class="secret-box">
    <div class="label">Secret — copy it now, it won't be shown again</div>
    <code>{{ new_secret }}</code>
    <div style="margin-top:8px;font-size:.8rem;color:#166534">
      Send <strong>X-BDSK-Signature: hmac-sha256(body, secret)</strong> to verify deliveries.
    </div>
  </div>
  {%- endif %}

  <div class="card" style="margin-bottom:24px">
    <h2>Endpoints</h2>
    {%- if endpoints %}
    <table>
      <thead><tr>
        <th>Name</th><th>URL</th><th>Events</th><th>Status</th><th>Last delivery</th><th>Actions</th>
      </tr></thead>
      <tbody>
      {%- for ep in endpoints %}
      {%- set ld = last_deliveries.get(ep['id']) %}
      <tr>
        <td><strong>{{ ep['name'] }}</strong></td>
        <td><div class="url-cell" title="{{ ep['url'] }}">{{ ep['url'] }}</div></td>
        <td style="font-size:.8rem">
          {%- for evt in ep['event_filter_parsed'] %}
          <span class="badge badge-ok" style="margin:1px 2px;padding:1px 6px">{{ evt }}</span>
          {%- endfor %}
        </td>
        <td>
          {%- if ep['enabled'] %}
          <span class="badge badge-ok">enabled</span>
          {%- else %}
          <span class="badge" style="background:#f3f4f6;color:#6b7280">disabled</span>
          {%- endif %}
        </td>
        <td style="font-size:.82rem">
          {%- if ld %}
          <span class="dot {{ 'dot-ok' if ld['success'] else 'dot-err' }}"></span>
          {{ ld['sent_at'][:19].replace('T',' ') if ld['sent_at'] else '—' }}
          {%- if ld['http_status'] %} ({{ ld['http_status'] }}){%- endif %}
          {%- else %}never{%- endif %}
        </td>
        <td>
          <div class="actions">
            <form method="post" action="{{ url_for('webhook_toggle', eid=ep['id']) }}">
              <button class="btn btn-secondary">{{ 'Disable' if ep['enabled'] else 'Enable' }}</button>
            </form>
            <form method="post" action="{{ url_for('webhook_regen_secret', eid=ep['id']) }}"
                  onsubmit="return confirm('Regenerate secret for {{ ep[\'name\'] }}? Old secret stops working immediately.')">
              <button class="btn btn-secondary">New secret</button>
            </form>
            <form method="post" action="{{ url_for('webhook_delete', eid=ep['id']) }}"
                  onsubmit="return confirm('Delete {{ ep[\'name\'] }}?')">
              <button class="btn btn-danger">Delete</button>
            </form>
          </div>
        </td>
      </tr>
      {%- endfor %}
      </tbody>
    </table>
    {%- else %}
    <p style="color:#6b7280;font-size:.9rem;padding:12px 0">
      No endpoints yet. Add one below to start receiving event notifications.
    </p>
    {%- endif %}
  </div>

  <div class="add-form">
    <h2>Add endpoint</h2>
    <form method="post" action="{{ url_for('webhook_add') }}">
      <div class="inline" style="margin-bottom:14px">
        <div class="form-group">
          <label>Name</label>
          <input type="text" name="name" placeholder="My integration" required>
        </div>
        <div class="form-group" style="flex:2;min-width:220px">
          <label>URL</label>
          <input type="url" name="url" placeholder="https://..." required>
        </div>
      </div>
      <div class="form-group">
        <label>Events to send</label>
        <div class="check-group">
          {%- for evt in all_events %}
          <label>
            <input type="checkbox" name="events" value="{{ evt }}" checked>
            {{ evt }}
          </label>
          {%- endfor %}
        </div>
      </div>
      <div style="margin-top:14px">
        <button type="submit" class="btn btn-primary">Add endpoint</button>
      </div>
    </form>
  </div>

  {%- if deliveries %}
  <div class="card" style="margin-top:24px">
    <h2>Recent deliveries</h2>
    <table>
      <thead><tr>
        <th>Time</th><th>Endpoint</th><th>Event</th><th>Entity</th><th>Status</th>
      </tr></thead>
      <tbody>
      {%- for d in deliveries %}
      <tr>
        <td style="font-size:.82rem;white-space:nowrap">{{ d['sent_at'][:19].replace('T',' ') if d['sent_at'] else '—' }}</td>
        <td>{{ d['endpoint_name'] }}</td>
        <td><code style="font-size:.82rem">{{ d['event'] }}</code></td>
        <td style="font-size:.82rem">id={{ d['entity_id'] }}</td>
        <td>
          {%- if d['success'] %}
          <span class="badge badge-ok">{{ d['http_status'] or 'ok' }}</span>
          {%- else %}
          <span class="badge badge-err" title="{{ d['error'] or '' }}">
            {{ d['http_status'] or 'err' }}
          </span>
          {%- endif %}
        </td>
      </tr>
      {%- endfor %}
      </tbody>
    </table>
  </div>
  {%- endif %}

</div>
{% endblock %}"""

# Wire all templates into Flask's Jinja2 environment via DictLoader so that
# {% extends 'base.html' %} works correctly across render_template() calls.
app.jinja_loader = jinja2.DictLoader({
    "base.html":      BASE,
    "login.html":     LOGIN_PAGE,
    "dashboard.html": DASHBOARD_PAGE,
    "users.html":     USERS_PAGE,
    "webhooks.html":  WEBHOOKS_PAGE,
})


def _render(template_name: str, **ctx):
    ctx.setdefault("css", CSS)
    ctx.setdefault("active", "")
    ctx.setdefault("current_user", _current_username())
    ctx.setdefault("refresh", None)
    return render_template(template_name, **ctx)


# ---------------------------------------------------------------------------
# Routes — auth
# ---------------------------------------------------------------------------

@app.route("/login", methods=["GET", "POST"])
def login_page():
    if _logged_in():
        return redirect(url_for("dashboard"))

    msgs = []
    username_val = ""

    if request.method == "POST":
        ip        = request.remote_addr or "unknown"
        fail_cnt  = _get_fail_count(ip)
        if fail_cnt >= MAX_FAILS:
            msgs = [("err", "Too many failed attempts. Try again in 15 minutes.")]
            return _render("login.html", msgs=msgs, username=username_val), 429

        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        username_val = username

        user = get_user_by_username(username)
        if user and check_password_hash(user["password_hash"], password):
            _reset_fail(ip)
            session.permanent = True
            session["user_id"] = user["id"]
            record_login(user["id"])
            return redirect(url_for("dashboard"))
        else:
            _increment_fail(ip)
            msgs = [("err", "Invalid username or password.")]

    return _render("login.html", msgs=msgs, username=username_val)


@app.route("/logout", methods=["POST"])
def logout():
    session.clear()
    return redirect(url_for("login_page"))


# ---------------------------------------------------------------------------
# Routes — dashboard
# ---------------------------------------------------------------------------

@app.route("/")
def dashboard():
    redir = _require_login()
    if redir:
        return redir

    cfg = load_config()
    try:
        d = get_status_data(cfg)
    except Exception as exc:
        return _render("dashboard.html", active="dashboard", refresh=60,
                       now="error", wp_ok=False, plugin_version="?",
                       wp_version="?", wc_version="?", php_version="?",
                       connector_enabled=False, conn_cls="badge-err",
                       conn_status="error", last_req=str(exc), last_cleanup=None,
                       job=None, archive_count=0, media_index_status="?",
                       mc={}, media_files=0, last_sync_at=None,
                       pending_events="?", event_after_id=0, event_last_run=None)

    h   = d.get("health", {})
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    last_req  = h.get("last_successful_request")
    conn_status, conn_cls = "never", "badge-err"
    if last_req:
        try:
            dt = datetime.fromisoformat(last_req.replace(" ", "T"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            age_h = (datetime.now(timezone.utc) - dt).total_seconds() / 3600
            if age_h < 1:
                conn_status, conn_cls = "connected", "badge-ok"
            elif age_h < 24:
                conn_status, conn_cls = "stale", "badge-warn"
            else:
                conn_status, conn_cls = "old", "badge-err"
        except Exception:
            conn_status = "unknown"

    return _render("dashboard.html", active="dashboard", refresh=60, now=now,
                   wp_ok=h.get("status") == "ok",
                   plugin_version=h.get("plugin_version", "?"),
                   wp_version=h.get("wordpress_version", "?"),
                   wc_version=h.get("woocommerce_version") or "N/A",
                   php_version=h.get("php_version", "?"),
                   connector_enabled=h.get("connector_enabled", False),
                   conn_status=conn_status, conn_cls=conn_cls,
                   last_req=last_req,
                   last_cleanup=h.get("last_cleanup_run"),
                   job=d.get("latest_job"),
                   archive_count=d.get("archive_count", 0),
                   media_index_status=h.get("media_index_status", "unknown"),
                   mc=d.get("media_counts") or {},
                   media_files=d.get("media_files", 0),
                   last_sync_at=d.get("last_sync_at"),
                   pending_events=h.get("event_outbox_pending_count", 0),
                   event_after_id=d.get("event_after_id", 0),
                   event_last_run=d.get("event_last_run"))


@app.route("/health")
def health():
    return {"status": "ok"}


# ---------------------------------------------------------------------------
# Routes — user management
# ---------------------------------------------------------------------------

@app.route("/users")
def users_page():
    redir = _require_login()
    if redir:
        return redir
    users = list_users()
    return _render("users.html", active="users", users=users, total_users=len(users))


@app.route("/users/add", methods=["POST"])
def add_user():
    redir = _require_login()
    if redir:
        return redir

    username = request.form.get("username", "").strip()
    password = request.form.get("password", "")

    if not username or not password:
        flash("Username and password are required.", "err")
        return redirect(url_for("users_page"))
    if len(password) < 8:
        flash("Password must be at least 8 characters.", "err")
        return redirect(url_for("users_page"))

    try:
        create_user(username, password)
        flash(f"User '{username}' created.", "ok")
    except sqlite3.IntegrityError:
        flash(f"Username '{username}' already exists.", "err")

    return redirect(url_for("users_page"))


@app.route("/users/<int:uid>/delete", methods=["POST"])
def delete_user_route(uid: int):
    redir = _require_login()
    if redir:
        return redir

    if count_users() <= 1:
        flash("Cannot delete the last remaining user.", "err")
        return redirect(url_for("users_page"))

    row = get_user_by_id(uid)
    if not row:
        flash("User not found.", "err")
        return redirect(url_for("users_page"))

    delete_user(uid)
    flash(f"User '{row['username']}' deleted.", "ok")
    return redirect(url_for("users_page"))


@app.route("/users/<int:uid>/change-password", methods=["POST"])
def change_password(uid: int):
    redir = _require_login()
    if redir:
        return redir

    new_pw = request.form.get("new_password", "")
    if len(new_pw) < 8:
        flash("Password must be at least 8 characters.", "err")
        return redirect(url_for("users_page"))

    row = get_user_by_id(uid)
    if not row:
        flash("User not found.", "err")
        return redirect(url_for("users_page"))

    update_password(uid, new_pw)
    flash(f"Password updated for '{row['username']}'.", "ok")
    return redirect(url_for("users_page"))


# ---------------------------------------------------------------------------
# Routes — webhooks
# ---------------------------------------------------------------------------

@app.route("/webhooks")
def webhooks_page():
    redir = _require_login()
    if redir:
        return redir

    endpoints_raw = list_endpoints()
    endpoints_out = []
    last_deliveries: dict[int, sqlite3.Row] = {}
    for ep in endpoints_raw:
        ep_dict = dict(ep)
        try:
            ep_dict["event_filter_parsed"] = json.loads(ep["event_filter"])
        except Exception:
            ep_dict["event_filter_parsed"] = ALL_EVENTS
        endpoints_out.append(ep_dict)
        ld = get_endpoint_last_delivery(ep["id"])
        if ld:
            last_deliveries[ep["id"]] = dict(ld)

    deliveries = [dict(d) for d in list_deliveries(limit=60)]

    new_secret = session.pop("new_webhook_secret", None)

    return _render("webhooks.html", active="webhooks",
                   endpoints=endpoints_out,
                   last_deliveries=last_deliveries,
                   deliveries=deliveries,
                   all_events=ALL_EVENTS,
                   new_secret=new_secret)


@app.route("/webhooks/add", methods=["POST"])
def webhook_add():
    redir = _require_login()
    if redir:
        return redir

    name   = request.form.get("name", "").strip()
    url    = request.form.get("url", "").strip()
    events = request.form.getlist("events")

    if not name or not url:
        flash("Name and URL are required.", "err")
        return redirect(url_for("webhooks_page"))
    if not events:
        flash("Select at least one event.", "err")
        return redirect(url_for("webhooks_page"))
    valid = [e for e in events if e in ALL_EVENTS]
    if not valid:
        flash("Invalid event selection.", "err")
        return redirect(url_for("webhooks_page"))

    _, new_secret = create_endpoint(name, url, valid)
    session["new_webhook_secret"] = new_secret
    flash(f"Endpoint '{name}' created.", "ok")
    return redirect(url_for("webhooks_page"))


@app.route("/webhooks/<int:eid>/toggle", methods=["POST"])
def webhook_toggle(eid: int):
    redir = _require_login()
    if redir:
        return redir

    ep = get_endpoint(eid)
    if not ep:
        flash("Endpoint not found.", "err")
        return redirect(url_for("webhooks_page"))

    try:
        ef = json.loads(ep["event_filter"])
    except Exception:
        ef = ALL_EVENTS
    update_endpoint(eid, ep["name"], ep["url"], ef, not bool(ep["enabled"]))
    state = "enabled" if not ep["enabled"] else "disabled"
    flash(f"Endpoint '{ep['name']}' {state}.", "ok")
    return redirect(url_for("webhooks_page"))


@app.route("/webhooks/<int:eid>/regenerate-secret", methods=["POST"])
def webhook_regen_secret(eid: int):
    redir = _require_login()
    if redir:
        return redir

    ep = get_endpoint(eid)
    if not ep:
        flash("Endpoint not found.", "err")
        return redirect(url_for("webhooks_page"))

    new_secret = regenerate_secret(eid)
    session["new_webhook_secret"] = new_secret
    flash(f"Secret regenerated for '{ep['name']}'.", "ok")
    return redirect(url_for("webhooks_page"))


@app.route("/webhooks/<int:eid>/delete", methods=["POST"])
def webhook_delete(eid: int):
    redir = _require_login()
    if redir:
        return redir

    ep = get_endpoint(eid)
    if not ep:
        flash("Endpoint not found.", "err")
        return redirect(url_for("webhooks_page"))

    delete_endpoint(eid)
    flash(f"Endpoint '{ep['name']}' deleted.", "ok")
    return redirect(url_for("webhooks_page"))


# ---------------------------------------------------------------------------
# CLI bootstrap
# ---------------------------------------------------------------------------

def cmd_create_user() -> None:
    init_db()
    print("Create hub user")
    username = input("Username: ").strip()
    if not username:
        sys.exit("Username cannot be empty.")
    if get_user_by_username(username):
        sys.exit(f"User '{username}' already exists.")
    password = getpass.getpass("Password: ")
    if len(password) < 8:
        sys.exit("Password must be at least 8 characters.")
    confirm = getpass.getpass("Confirm password: ")
    if password != confirm:
        sys.exit("Passwords do not match.")
    create_user(username, password)
    print(f"User '{username}' created successfully.")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Behdashtik Hub Dashboard")
    parser.add_argument("--create-user", action="store_true",
                        help="Interactively create a hub user")
    args = parser.parse_args()

    if args.create_user:
        cmd_create_user()
    else:
        _init_app()
        app.run(host="127.0.0.1", port=8089, debug=False)

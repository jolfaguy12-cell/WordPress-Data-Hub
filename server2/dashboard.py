#!/usr/bin/env python3
"""
Behdashtik Hub Dashboard — hub.behdashtik.ir
============================================
Read-only status dashboard for the Behdashtik mirror system.
Binds to 127.0.0.1:8089 (nginx reverse-proxies with Basic Auth + SSL).

Run via systemd service bdsk-dashboard.service.
"""

import pathlib
import sys
from datetime import datetime, timezone

sys.path.insert(0, str(pathlib.Path(__file__).parent))

from flask import Flask, Response, render_template_string
from pipeline import get_status_data, load_config

app = Flask(__name__)

TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="refresh" content="60">
<title>Behdashtik Hub</title>
<style>
body{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;margin:0;background:#f0f2f5;color:#1d1d1f}
.hdr{background:#1d2331;color:#fff;padding:16px 24px;display:flex;align-items:center;gap:12px}
.hdr h1{margin:0;font-size:1.2rem;font-weight:600}
.hdr .ts{margin-left:auto;font-size:.8rem;opacity:.7}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(300px,1fr));gap:16px;padding:20px}
.card{background:#fff;border-radius:8px;padding:20px;box-shadow:0 1px 3px rgba(0,0,0,.1)}
.card h2{margin:0 0 12px;font-size:.95rem;color:#6b7280;text-transform:uppercase;letter-spacing:.05em}
.badge{display:inline-block;padding:3px 8px;border-radius:12px;font-size:.8rem;font-weight:600}
.ok{background:#d1fae5;color:#065f46}
.warn{background:#fef3c7;color:#92400e}
.err{background:#fee2e2;color:#991b1b}
.row{display:flex;justify-content:space-between;padding:6px 0;border-bottom:1px solid #f3f4f6;font-size:.9rem}
.row:last-child{border-bottom:none}
.row .lbl{color:#6b7280}
.row .val{font-weight:500;text-align:right;max-width:55%;word-break:break-all}
.num{font-size:2rem;font-weight:700;color:#1d2331}
.sub{font-size:.8rem;color:#9ca3af}
</style>
</head>
<body>
<div class="hdr">
  <h1>&#x1F4CA; Behdashtik Hub</h1>
  <span class="ts">Auto-refreshes every 60&thinsp;s &nbsp;|&nbsp; {{ now }}</span>
</div>
<div class="grid">

  <div class="card">
    <h2>WordPress</h2>
    <div class="row"><span class="lbl">Status</span><span class="val">
      <span class="badge {{ wp_cls }}">{{ wp_label }}</span></span></div>
    <div class="row"><span class="lbl">Plugin</span><span class="val">v{{ plugin_version }}</span></div>
    <div class="row"><span class="lbl">WP</span><span class="val">{{ wp_version }}</span></div>
    <div class="row"><span class="lbl">WC</span><span class="val">{{ wc_version }}</span></div>
    <div class="row"><span class="lbl">PHP</span><span class="val">{{ php_version }}</span></div>
    <div class="row"><span class="lbl">Connector</span><span class="val">{{ "enabled" if connector_enabled else "DISABLED" }}</span></div>
    <div class="row"><span class="lbl">Connection</span><span class="val">
      <span class="badge {{ conn_cls }}">{{ conn_status }}</span></span></div>
    <div class="row"><span class="lbl">Last request</span><span class="val">{{ last_req or "—" }}</span></div>
    <div class="row"><span class="lbl">Last cleanup</span><span class="val">{{ last_cleanup or "—" }}</span></div>
  </div>

  <div class="card">
    <h2>DB Mirror</h2>
    {% if job %}
    <div class="row"><span class="lbl">Last job</span><span class="val">{{ job.job_id[:8] }}…</span></div>
    <div class="row"><span class="lbl">Status</span><span class="val">
      <span class="badge {{ 'ok' if job.status in ('done','completed') else ('warn' if job.status in ('running','pending') else 'err') }}">
        {{ job.status }}</span></span></div>
    <div class="row"><span class="lbl">Created</span><span class="val">{{ job.created_at or "—" }}</span></div>
    <div class="row"><span class="lbl">Finished</span><span class="val">{{ job.finished_at or "—" }}</span></div>
    {% else %}
    <div class="row"><span class="lbl">Status</span><span class="val">No jobs found</span></div>
    {% endif %}
    <div class="row"><span class="lbl">Archives on disk</span><span class="val">{{ archive_count }}</span></div>
  </div>

  <div class="card">
    <h2>Media</h2>
    <div class="row"><span class="lbl">Index status</span><span class="val">{{ media_index_status }}</span></div>
    {% if mc %}
    <div class="row"><span class="lbl">Downloaded</span><span class="val">{{ mc.get('downloaded',0) + mc.get('active',0) }}</span></div>
    <div class="row"><span class="lbl">Pending</span><span class="val">{{ mc.get('pending',0) + mc.get('queued',0) }}</span></div>
    <div class="row"><span class="lbl">Failed</span><span class="val">{{ mc.get('failed',0) }}</span></div>
    {% endif %}
    <div class="row"><span class="lbl">Files on disk</span><span class="val">{{ media_files }}</span></div>
    <div class="row"><span class="lbl">Last sync</span><span class="val">{{ last_sync_at or "—" }}</span></div>
  </div>

  <div class="card">
    <h2>Event Outbox</h2>
    <div class="num">{{ pending_events }}</div>
    <div class="sub">Pending on WordPress</div>
    <br>
    <div class="row"><span class="lbl">Cursor (after_id)</span><span class="val">{{ event_after_id }}</span></div>
    <div class="row"><span class="lbl">Last event sync</span><span class="val">{{ event_last_run or "never" }}</span></div>
  </div>

</div>
</body>
</html>"""


@app.route("/")
def index():
    cfg = load_config()
    try:
        d = get_status_data(cfg)
    except Exception as exc:
        return Response(f"<pre>Error: {exc}</pre>", 500, content_type="text/html")

    h = d.get("health", {})
    now_utc = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    last_req = h.get("last_successful_request")
    conn_status, conn_cls = "never", "err"
    if last_req:
        try:
            dt = datetime.fromisoformat(last_req.replace(" ", "T"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            age_h = (datetime.now(timezone.utc) - dt).total_seconds() / 3600
            if age_h < 1:
                conn_status, conn_cls = "connected", "ok"
            elif age_h < 24:
                conn_status, conn_cls = "stale", "warn"
            else:
                conn_status, conn_cls = "old", "err"
        except Exception:
            conn_status = "unknown"

    wp_ok = h.get("status") == "ok"
    html = render_template_string(
        TEMPLATE,
        now=now_utc,
        wp_cls="ok" if wp_ok else "err",
        wp_label="● OK" if wp_ok else "● Error",
        plugin_version=h.get("plugin_version", "?"),
        wp_version=h.get("wordpress_version", "?"),
        wc_version=h.get("woocommerce_version") or "N/A",
        php_version=h.get("php_version", "?"),
        connector_enabled=h.get("connector_enabled", False),
        conn_status=conn_status,
        conn_cls=conn_cls,
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
        event_last_run=d.get("event_last_run"),
    )
    return Response(html, content_type="text/html")


@app.route("/health")
def health():
    return {"status": "ok"}


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=8089, debug=False)

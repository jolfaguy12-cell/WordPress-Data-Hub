# Operations Runbook

Day-to-day reference for the Behdashtik WordPress Data Hub running in production.

## Production topology

| Component | Host | Details |
|---|---|---|
| WordPress + plugin | `dev.behdashtik.ir` | Apache/nginx, PHP 8.1, WC 10.7.0 |
| Mirror DB | `127.0.0.1:3307` | MySQL 8.0, Docker container |
| Pipeline + dashboard | `/root/wordpress-data-hub/server2/` | Python 3.11 |
| Hub dashboard | `hub.behdashtik.ir:443` | nginx → `127.0.0.1:8089` (systemd) |
| Plugin path | `/var/www/dev.behdashtik.ir/wp-content/plugins/behdashtik-mirror-connector/` | |
| Debug log | `/var/www/dev.behdashtik.ir/wp-content/bdsk-debug.log` | weekly logrotate, 8 rotations |
| Archive storage | `/root/wordpress-data-hub/data/db-archives/` | auto-pruned after download |
| Media storage | `/root/wordpress-data-hub/data/media/` | incremental, kept indefinitely |

---

## Scheduled jobs

### System cron (`crontab -l` on the server 2 host)

```
*/5 * * * *  curl -s --max-time 30 https://dev.behdashtik.ir/wp-cron.php?doing_wp_cron > /dev/null 2>&1
```

Pokes WordPress cron every 5 minutes as a backup for Action Scheduler when traffic is low.

### Action Scheduler (WordPress-side, runs inside WP process)

| Hook | Schedule | Purpose |
|---|---|---|
| `bdsk_export_chunk` | On demand (queued by pipeline) | Exports one batch of DB rows |
| `bdsk_check_stuck_jobs` | Every 5 minutes | Fails jobs stuck >15 min (heartbeat timeout) |
| `bdsk_cleanup_expired` | Hourly | Deletes old export files, prunes DB rows |

### Pipeline (server 2 host — add to crontab or systemd timer as needed)

```bash
# Full DB export + import (suggested: nightly, off-peak)
0 3 * * *  cd /root/wordpress-data-hub/server2 && python3 pipeline.py >> /var/log/bdsk-pipeline.log 2>&1

# Event sync — keeps mirror near-real-time
* * * * *  cd /root/wordpress-data-hub/server2 && python3 pipeline.py --event-sync >> /var/log/bdsk-pipeline.log 2>&1

# Media sync (suggested: nightly after full export)
30 3 * * *  cd /root/wordpress-data-hub/server2 && python3 pipeline.py --media-sync >> /var/log/bdsk-pipeline.log 2>&1

# Prune old archives
0 4 * * 0  cd /root/wordpress-data-hub/server2 && python3 pipeline.py --prune >> /var/log/bdsk-pipeline.log 2>&1
```

---

## Dashboard service

```bash
# Status
systemctl status bdsk-dashboard

# Restart
systemctl restart bdsk-dashboard

# Logs (last 50 lines)
journalctl -u bdsk-dashboard -n 50
```

The service runs as root, `WorkingDirectory=/root/wordpress-data-hub/server2`, port 8089, reverse-proxied by nginx on `hub.behdashtik.ir:443`.

---

## Health checks

### Quick: plugin health endpoint

```bash
# Replace <SECRET> with the api_secret from server2/config.json
curl -s -H "Authorization: BDSK <SECRET>" \
  https://dev.behdashtik.ir/wp-json/behdashtik-connector/v1/health | python3 -m json.tool
```

Key fields to inspect:

| Field | Healthy value |
|---|---|
| `connector` | `ok` |
| `read` | `ok` |
| `write` | `ok` |
| `media` | `ok` |
| `event` | `ok` |
| `event_outbox_pending_count` | Low (< 50 between syncs) |
| `last_successful_request` | Recent timestamp |
| `last_cleanup_run` | Within the past 2 hours |

### Pipeline status

```bash
cd /root/wordpress-data-hub/server2
python3 pipeline.py --status
```

### Event sync backlog

```bash
# Count pending events in the outbox (run on WP host or via WP-CLI)
wp eval "echo BDSK_Event_Outbox::get_stats()['pending'];"
```

A sustained backlog > 500 with event sync running suggests a stuck or failing sync loop — check the pipeline log.

### Archive directory size

```bash
du -sh /root/wordpress-data-hub/data/db-archives/
ls -lh /root/wordpress-data-hub/data/db-archives/
```

Archives are auto-deleted after successful import. Stale archives (> 24 h old, still present) may indicate a failed pipeline run.

---

## Credential locations

| Secret | Location |
|---|---|
| Plugin API secret | WP admin → Behdashtik → Settings (hashed in DB); plaintext only shown once on first activation |
| Pipeline config (all secrets) | `/root/wordpress-data-hub/server2/config.json` (gitignored) |
| Hub dashboard login | `hub.db` `users` table (bcrypt hash); reset via dashboard UI |
| Webhook endpoint secrets | `hub.db` `webhook_endpoints` table (plaintext, generated on create/regenerate); shown once in dashboard |
| Mirror DB credentials | `server2/config.json` → `mirror_db.*` |
| SSL certificates | Managed by Certbot for `hub.behdashtik.ir` and `dev.behdashtik.ir` |

**Never commit `server2/config.json` or `server2/hub.db`** — both are gitignored.

---

## Common operations

### Force a full re-export

```bash
cd /root/wordpress-data-hub/server2
python3 pipeline.py
```

If a previous job is stuck at `running`:

```bash
# Cancel the stuck job from WP-CLI on the WordPress host
wp eval "BDSK_Export_Job::cancel_stuck_jobs();"
# Then re-run
python3 pipeline.py
```

### Run event sync manually

```bash
cd /root/wordpress-data-hub/server2
python3 pipeline.py --event-sync
```

### Import a previously downloaded archive (without re-exporting)

```bash
cd /root/wordpress-data-hub/server2
python3 pipeline.py --import-only /root/wordpress-data-hub/data/db-archives/<job-dir>/
```

### Resync all media (full scan, ignores state)

```bash
cd /root/wordpress-data-hub/server2
python3 pipeline.py --media-sync-full
```

### Deploy plugin update to production

```bash
cd /root/wordpress-data-hub/deploy
./sync-plugin.sh --deploy-to-production
```

The script rsyncs the plugin directory, then flushes the PHP opcache and WP rewrite rules via WP-CLI.

### Rotate the pipeline API secret

1. Go to WP admin → Behdashtik → Settings → Regenerate secret.
2. Copy the new plaintext secret shown (only shown once).
3. Update `server2/config.json` → `api_secret`.
4. Restart any cron jobs that use it (they read config.json at startup).

### Add a webhook endpoint

Log into `hub.behdashtik.ir`, go to **Webhooks**, fill in the add-endpoint form. Copy the secret from the green reveal box — it is shown only once. The receiving server must verify `X-BDSK-Signature: sha256=<hex>` using HMAC-SHA256 with that secret over the raw request body.

---

## Log files

| Log | Path | Rotation |
|---|---|---|
| Plugin debug log | `/var/www/dev.behdashtik.ir/wp-content/bdsk-debug.log` | Weekly, 8 rotations (logrotate) |
| Pipeline log | `/var/log/bdsk-pipeline.log` | Manual or add logrotate entry |
| Dashboard service | `journalctl -u bdsk-dashboard` | systemd journal (default retention) |

Enable plugin debug logging: WP admin → Behdashtik → Settings → Debug Log.

---

## Cleanup jobs (automatic)

The hourly `bdsk_cleanup_expired` Action Scheduler job handles:

- Deletes export archive files for completed/failed jobs
- Prunes `wp_bdsk_media_index` rows for attachments deleted > 7 days ago
- Prunes `wp_bdsk_event_outbox` rows acknowledged > 7 days ago
- Prunes `wp_bdsk_request_log` rows > 30 days old
- Prunes `webhook_deliveries` rows in `hub.db` > 30 days old

An **Emergency Cleanup** button is available in the WP admin if the disk fills up unexpectedly.

---

## Mirror DB schema notes

The mirror is a verbatim copy of the WordPress/WooCommerce schema under the `wp_` prefix. Key tables:

| Table | Content |
|---|---|
| `wp_posts` / `wp_postmeta` | Products (HPOS-era; post_type = 'product') |
| `wp_wc_orders` / `wp_wc_orders_meta` | Orders (HPOS) |
| `wp_woocommerce_order_items` / `wp_woocommerce_order_itemmeta` | Line items |
| `wp_wc_product_meta_lookup` | Denormalized product pricing/stock |
| `wp_term_relationships` / `wp_term_taxonomy` / `wp_terms` | Product categories and tags |
| `wp_bdsk_event_log` | Record of every event sync apply (ok / failed) |

Read-only access: connect as `mirror_readonly` (credentials in `config.json` → `mirror_db.readonly_*`).

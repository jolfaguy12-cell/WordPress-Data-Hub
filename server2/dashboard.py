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
import pymysql
import pymysql.cursors
from flask import (Flask, Response, flash, redirect, render_template,
                   request, session, url_for)
from data_api import data_api as data_api_bp
from bdsk_config import load_config
from pipeline import get_status_data
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


@contextmanager
def _mirror_db():
    cfg = load_config()
    db  = cfg["mirror_db"]
    conn = pymysql.connect(
        host=db["host"], port=db.get("port", 3306),
        user=db.get("readonly_user", db["user"]),
        password=db.get("readonly_password", db["password"]),
        database=db["name"],
        charset="utf8mb4",
        cursorclass=pymysql.cursors.DictCursor,
        init_command="SET sql_mode=''",
        connect_timeout=5,
    )
    try:
        yield conn
    finally:
        conn.close()


def get_orders_page_data(filters: dict) -> dict:
    """Fetch all data needed for the Orders page from the mirror DB."""
    status_f    = filters.get("status", "")
    source_f    = filters.get("order_source", "")
    has_phone_f = filters.get("has_phone", "")
    has_email_f = filters.get("has_email", "")
    date_from   = filters.get("date_from", "")
    date_to     = filters.get("date_to", "")

    _BS = "SELECT order_id FROM wp_wc_orders_meta WHERE meta_key='_sync_basalam_hash_id'"
    _OP = "SELECT order_id FROM wp_wc_order_operational_data WHERE created_via=%s"

    try:
        with _mirror_db() as conn:
            with conn.cursor() as cur:
                # ── Summary stats ────────────────────────────────────────────
                cur.execute(
                    "SELECT COUNT(*) as cnt, SUM(total_amount) as total_sales"
                    " FROM wp_wc_orders WHERE type='shop_order'"
                )
                t = cur.fetchone()
                total_orders = int(t["cnt"] or 0)
                total_sales  = float(t["total_sales"] or 0)

                cur.execute(
                    "SELECT COUNT(*) as cnt FROM wp_wc_orders"
                    " WHERE type='shop_order' AND status IN ('wc-pending','wc-failed','wc-on-hold')"
                )
                unpaid_count = int(cur.fetchone()["cnt"] or 0)

                # By-status
                cur.execute(
                    "SELECT status, COUNT(*) as cnt FROM wp_wc_orders"
                    " WHERE type='shop_order' GROUP BY status ORDER BY cnt DESC"
                )
                by_status = [
                    {"status": (r["status"][3:] if r["status"].startswith("wc-") else r["status"]),
                     "count": r["cnt"]}
                    for r in cur.fetchall()
                ]

                # By-source (Basalam / website / manual / unknown)
                cur.execute(
                    f"SELECT COUNT(*) as cnt, COALESCE(SUM(o.total_amount),0) as sales"
                    f" FROM wp_wc_orders o WHERE o.type='shop_order' AND o.id IN ({_BS})"
                )
                bs = cur.fetchone(); basalam_cnt = int(bs["cnt"]); basalam_sales = float(bs["sales"])
                cur.execute(
                    f"SELECT COUNT(*) as cnt, COALESCE(SUM(o.total_amount),0) as sales"
                    f" FROM wp_wc_orders o JOIN wp_wc_order_operational_data op ON op.order_id=o.id"
                    f" WHERE o.type='shop_order' AND op.created_via='checkout' AND o.id NOT IN ({_BS})"
                )
                ws = cur.fetchone(); website_cnt = int(ws["cnt"]); website_sales = float(ws["sales"])
                cur.execute(
                    f"SELECT COUNT(*) as cnt, COALESCE(SUM(o.total_amount),0) as sales"
                    f" FROM wp_wc_orders o JOIN wp_wc_order_operational_data op ON op.order_id=o.id"
                    f" WHERE o.type='shop_order' AND op.created_via='admin' AND o.id NOT IN ({_BS})"
                )
                ma = cur.fetchone(); manual_cnt = int(ma["cnt"]); manual_sales = float(ma["sales"])
                unknown_cnt = max(0, total_orders - basalam_cnt - website_cnt - manual_cnt)

                # Contact availability
                cur.execute(
                    "SELECT"
                    " SUM(CASE WHEN billing_email IS NOT NULL AND billing_email != '' THEN 1 ELSE 0 END) as he,"
                    " COUNT(*) as tot FROM wp_wc_orders WHERE type='shop_order'"
                )
                cr = cur.fetchone(); has_email_cnt = int(cr["he"] or 0)
                cur.execute(
                    f"SELECT COUNT(DISTINCT a.order_id) as hp"
                    f" FROM wp_wc_order_addresses a JOIN wp_wc_orders o ON o.id=a.order_id"
                    f" WHERE o.type='shop_order' AND a.address_type='billing'"
                    f" AND a.phone IS NOT NULL AND a.phone != ''"
                )
                has_phone_cnt = int(cur.fetchone()["hp"] or 0)

                # ── Filtered order list ──────────────────────────────────────
                where:  list[str] = ["o.type = 'shop_order'"]
                params: list      = []
                if status_f:
                    s = status_f if status_f.startswith("wc-") else f"wc-{status_f}"
                    where.append("o.status = %s"); params.append(s)
                if source_f == "basalam":
                    where.append(f"o.id IN ({_BS})")
                elif source_f == "website":
                    where.append(f"o.id NOT IN ({_BS})")
                    where.append(f"o.id IN ({_OP})"); params.append("checkout")
                elif source_f == "manual":
                    where.append(f"o.id NOT IN ({_BS})")
                    where.append(f"o.id IN ({_OP})"); params.append("admin")
                elif source_f == "unknown":
                    where.append(f"o.id NOT IN ({_BS})")
                    where.append("o.id NOT IN (SELECT order_id FROM wp_wc_order_operational_data WHERE created_via IN ('checkout','admin'))")
                if date_from:
                    where.append("o.date_created_gmt >= %s"); params.append(date_from)
                if date_to:
                    where.append("o.date_created_gmt <= %s"); params.append(date_to)
                if has_email_f == "1":
                    where.append("o.billing_email IS NOT NULL AND o.billing_email != ''")
                elif has_email_f == "0":
                    where.append("(o.billing_email IS NULL OR o.billing_email = '')")
                if has_phone_f == "1":
                    where.append("EXISTS (SELECT 1 FROM wp_wc_order_addresses pa WHERE pa.order_id=o.id AND pa.address_type='billing' AND pa.phone IS NOT NULL AND pa.phone != '')")
                elif has_phone_f == "0":
                    where.append("NOT EXISTS (SELECT 1 FROM wp_wc_order_addresses pa WHERE pa.order_id=o.id AND pa.address_type='billing' AND pa.phone IS NOT NULL AND pa.phone != '')")

                where_sql = " AND ".join(where)
                cur.execute(f"SELECT COUNT(*) as cnt FROM wp_wc_orders o WHERE {where_sql}", params)
                filtered_total = int(cur.fetchone()["cnt"] or 0)

                cur.execute(
                    f"SELECT o.id, o.status, o.currency, o.total_amount, o.customer_id,"
                    f" o.billing_email, o.date_created_gmt, o.payment_method"
                    f" FROM wp_wc_orders o WHERE {where_sql}"
                    f" ORDER BY o.id DESC LIMIT 50",
                    params,
                )
                order_rows = cur.fetchall()
                order_ids = [r["id"] for r in order_rows]

                src_map: dict = {}; op_map: dict = {}; phone_map: dict = {}
                if order_ids:
                    ph = ",".join(["%s"] * len(order_ids))
                    cur.execute(
                        f"SELECT order_id, meta_value FROM wp_wc_orders_meta"
                        f" WHERE order_id IN ({ph}) AND meta_key='_sync_basalam_hash_id'",
                        order_ids,
                    )
                    src_map = {r["order_id"]: r["meta_value"] for r in cur.fetchall()}
                    cur.execute(
                        f"SELECT order_id, created_via FROM wp_wc_order_operational_data WHERE order_id IN ({ph})",
                        order_ids,
                    )
                    op_map = {r["order_id"]: (r["created_via"] or "") for r in cur.fetchall()}
                    cur.execute(
                        f"SELECT order_id, phone FROM wp_wc_order_addresses"
                        f" WHERE order_id IN ({ph}) AND address_type='billing'",
                        order_ids,
                    )
                    phone_map = {r["order_id"]: r["phone"] for r in cur.fetchall()}

                def _src(oid):
                    bs = src_map.get(oid); cv = op_map.get(oid, "")
                    if bs:    return "basalam", bs
                    if cv == "checkout": return "website", None
                    if cv == "admin":    return "manual",  None
                    return "unknown", None

                def _mphone(p): return f"{p[:3]}***{p[-2:]}" if p and len(p) > 5 else ("***" if p else None)
                def _memail(e):
                    if not e: return None
                    try: l,d=e.split("@",1); return f"{l[:1]}***@{d}"
                    except: return "***"

                orders = []
                for r in order_rows:
                    oid  = r["id"]
                    st   = r["status"]
                    src, ext_id = _src(oid)
                    phone = phone_map.get(oid)
                    orders.append({
                        "id": oid,
                        "status": st[3:] if st.startswith("wc-") else st,
                        "currency": r["currency"] or "IRT",
                        "total": float(r["total_amount"] or 0),
                        "date_created": str(r["date_created_gmt"] or "")[:16],
                        "order_source": src,
                        "external_order_id": ext_id,
                        "phone_masked": _mphone(phone),
                        "email_masked": _memail(r["billing_email"]),
                        "has_phone": bool(phone),
                        "has_email": bool(r["billing_email"]),
                    })

                # ── Unpaid / recovery candidates ─────────────────────────────
                cur.execute(
                    "SELECT o.id, o.status, o.currency, o.total_amount, o.billing_email,"
                    " o.date_created_gmt, o.payment_method"
                    " FROM wp_wc_orders o"
                    " WHERE o.type='shop_order' AND o.status IN ('wc-pending','wc-failed','wc-on-hold')"
                    " ORDER BY o.date_created_gmt ASC LIMIT 100"
                )
                unpaid_rows = cur.fetchall()
                unpaid_ids  = [r["id"] for r in unpaid_rows]
                up_src: dict = {}; up_op: dict = {}; up_phone: dict = {}
                if unpaid_ids:
                    ph2 = ",".join(["%s"] * len(unpaid_ids))
                    cur.execute(f"SELECT order_id, meta_value FROM wp_wc_orders_meta WHERE order_id IN ({ph2}) AND meta_key='_sync_basalam_hash_id'", unpaid_ids)
                    up_src = {r["order_id"]: r["meta_value"] for r in cur.fetchall()}
                    cur.execute(f"SELECT order_id, created_via FROM wp_wc_order_operational_data WHERE order_id IN ({ph2})", unpaid_ids)
                    up_op  = {r["order_id"]: (r["created_via"] or "") for r in cur.fetchall()}
                    cur.execute(f"SELECT order_id, phone FROM wp_wc_order_addresses WHERE order_id IN ({ph2}) AND address_type='billing'", unpaid_ids)
                    up_phone = {r["order_id"]: r["phone"] for r in cur.fetchall()}

                reason_map = {"wc-pending": "pending_payment", "wc-failed": "payment_failed", "wc-on-hold": "on_hold"}
                now_utc = datetime.now(timezone.utc)

                unpaid = []
                for r in unpaid_rows:
                    oid = r["id"]; st = r["status"]
                    bs = up_src.get(oid); cv = up_op.get(oid, "")
                    if bs:  src2, ext2 = "basalam", bs
                    elif cv == "checkout": src2, ext2 = "website", None
                    elif cv == "admin":    src2, ext2 = "manual",  None
                    else:                  src2, ext2 = "unknown", None
                    phone2 = up_phone.get(oid)
                    age_h = None
                    if r["date_created_gmt"]:
                        try:
                            dt = r["date_created_gmt"]
                            if not isinstance(dt, datetime):
                                dt = datetime.fromisoformat(str(dt))
                            age_h = round((now_utc - dt.replace(tzinfo=timezone.utc)).total_seconds() / 3600, 1)
                        except Exception:
                            pass
                    unpaid.append({
                        "id": oid,
                        "status": st[3:] if st.startswith("wc-") else st,
                        "currency": r["currency"] or "IRT",
                        "total": float(r["total_amount"] or 0),
                        "date_created": str(r["date_created_gmt"] or "")[:16],
                        "order_source": src2,
                        "external_order_id": ext2,
                        "phone_masked": _mphone(phone2),
                        "email_masked": _memail(r["billing_email"]),
                        "has_phone": bool(phone2),
                        "has_email": bool(r["billing_email"]),
                        "age_hours": age_h,
                        "recovery_reason": reason_map.get(st, "unpaid"),
                    })

        return {
            "ok": True,
            "total_orders": total_orders,
            "total_sales": total_sales,
            "unpaid_count": unpaid_count,
            "basalam_cnt": basalam_cnt, "basalam_sales": basalam_sales,
            "website_cnt": website_cnt, "website_sales": website_sales,
            "manual_cnt":  manual_cnt,  "manual_sales":  manual_sales,
            "unknown_cnt": unknown_cnt,
            "has_email_cnt": has_email_cnt, "has_phone_cnt": has_phone_cnt,
            "by_status": by_status,
            "orders": orders, "filtered_total": filtered_total,
            "unpaid": unpaid,
        }
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


_STOCK_STATUS_FA = {
    "instock": "موجود", "outofstock": "ناموجود", "onbackorder": "پیش‌سفارش",
}
_PRODUCT_STATUS_FA = {
    "publish": "منتشرشده", "draft": "پیش‌نویس", "private": "خصوصی", "trash": "حذف‌شده",
}

def get_products_page_data(filters: dict) -> dict:
    """Fetch products page data from the mirror DB."""
    search_f      = filters.get("search", "").strip()
    status_f      = filters.get("status", "")
    stock_f       = filters.get("stock_status", "")
    has_price_f   = filters.get("has_price", "")
    has_image_f   = filters.get("has_image", "")
    cat_f         = filters.get("category_id", "")
    brand_f       = filters.get("brand_id", "")
    page          = max(1, int(filters.get("page", 1) or 1))
    per_page      = 30

    try:
        with _mirror_db() as conn:
            with conn.cursor() as cur:
                # ── Summary stats ─────────────────────────────────────────
                cur.execute(
                    "SELECT COUNT(*) as tot,"
                    " SUM(p.post_status='publish') as published,"
                    " SUM(p.post_status='draft') as draft,"
                    " SUM(p.post_status='private') as priv,"
                    " SUM(l.stock_status='outofstock' OR l.stock_status='') as outofstock_cnt,"
                    " SUM(l.stock_status='onbackorder') as backorder_cnt,"
                    " SUM(l.min_price IS NULL OR l.min_price=0) as no_price_cnt,"
                    " SUM(pm.meta_value IS NULL OR pm.meta_value='') as no_image_cnt"
                    " FROM wp_posts p"
                    " LEFT JOIN wp_wc_product_meta_lookup l ON l.product_id=p.ID"
                    " LEFT JOIN wp_postmeta pm ON pm.post_id=p.ID AND pm.meta_key='_thumbnail_id'"
                    " WHERE p.post_type='product'",
                )
                s = cur.fetchone()
                total         = int(s["tot"] or 0)
                published_cnt = int(s["published"] or 0)
                draft_cnt     = int(s["draft"] or 0)
                private_cnt   = int(s["priv"] or 0)
                outofstock_cnt= int(s["outofstock_cnt"] or 0)
                backorder_cnt = int(s["backorder_cnt"] or 0)
                no_price_cnt  = int(s["no_price_cnt"] or 0)
                no_image_cnt  = int(s["no_image_cnt"] or 0)

                # ── Categories & brands for dropdowns ────────────────────
                cur.execute(
                    "SELECT t.term_id, t.name FROM wp_terms t"
                    " JOIN wp_term_taxonomy tt ON tt.term_id=t.term_id"
                    " WHERE tt.taxonomy='product_cat' AND tt.count>0"
                    " ORDER BY tt.count DESC LIMIT 40"
                )
                categories = [{"id": r["term_id"], "name": r["name"]} for r in cur.fetchall()]
                cur.execute(
                    "SELECT t.term_id, t.name FROM wp_terms t"
                    " JOIN wp_term_taxonomy tt ON tt.term_id=t.term_id"
                    " WHERE tt.taxonomy='product_brand' AND tt.count>0"
                    " ORDER BY tt.count DESC LIMIT 30"
                )
                brands = [{"id": r["term_id"], "name": r["name"]} for r in cur.fetchall()]

                # ── Filtered product list ─────────────────────────────────
                where: list[str] = ["p.post_type = 'product'"]
                params: list     = []

                if status_f:
                    where.append("p.post_status = %s"); params.append(status_f)
                if stock_f:
                    where.append("l.stock_status = %s"); params.append(stock_f)
                if has_price_f == "1":
                    where.append("l.min_price IS NOT NULL AND l.min_price > 0")
                elif has_price_f == "0":
                    where.append("(l.min_price IS NULL OR l.min_price = 0)")
                if has_image_f == "1":
                    where.append("pm_th.meta_value IS NOT NULL AND pm_th.meta_value != ''")
                elif has_image_f == "0":
                    where.append("(pm_th.meta_value IS NULL OR pm_th.meta_value = '')")
                if cat_f:
                    where.append(
                        "EXISTS (SELECT 1 FROM wp_term_relationships tr2"
                        " JOIN wp_term_taxonomy tt2 ON tt2.term_taxonomy_id=tr2.term_taxonomy_id"
                        " WHERE tr2.object_id=p.ID AND tt2.taxonomy='product_cat' AND tt2.term_id=%s)"
                    ); params.append(int(cat_f))
                if brand_f:
                    where.append(
                        "EXISTS (SELECT 1 FROM wp_term_relationships tr3"
                        " JOIN wp_term_taxonomy tt3 ON tt3.term_taxonomy_id=tr3.term_taxonomy_id"
                        " WHERE tr3.object_id=p.ID AND tt3.taxonomy='product_brand' AND tt3.term_id=%s)"
                    ); params.append(int(brand_f))
                if search_f:
                    where.append("(p.post_title LIKE %s OR l.sku LIKE %s)")
                    params += [f"%{search_f}%", f"%{search_f}%"]

                where_sql = " AND ".join(where)
                cur.execute(f"SELECT COUNT(*) as cnt FROM wp_posts p"
                            f" LEFT JOIN wp_wc_product_meta_lookup l ON l.product_id=p.ID"
                            f" LEFT JOIN wp_postmeta pm_th ON pm_th.post_id=p.ID AND pm_th.meta_key='_thumbnail_id'"
                            f" WHERE {where_sql}", params)
                filtered_total = int(cur.fetchone()["cnt"] or 0)

                offset = (page - 1) * per_page
                cur.execute(
                    f"SELECT p.ID, p.post_title, p.post_status, p.post_modified,"
                    f" p.guid, l.sku, l.min_price, l.max_price,"
                    f" l.stock_status, l.stock_quantity, l.total_sales,"
                    f" pm_th.meta_value as thumbnail_id"
                    f" FROM wp_posts p"
                    f" LEFT JOIN wp_wc_product_meta_lookup l ON l.product_id=p.ID"
                    f" LEFT JOIN wp_postmeta pm_th ON pm_th.post_id=p.ID AND pm_th.meta_key='_thumbnail_id'"
                    f" WHERE {where_sql}"
                    f" ORDER BY p.ID DESC LIMIT %s OFFSET %s",
                    params + [per_page, offset],
                )
                product_rows = cur.fetchall()
                pids = [r["ID"] for r in product_rows]

                # Batch fetch categories + brands for listed products
                cat_map: dict   = {}
                brand_map: dict = {}
                media_map: dict = {}
                if pids:
                    ph = ",".join(["%s"] * len(pids))
                    cur.execute(
                        f"SELECT tr.object_id, t.name, tt.taxonomy"
                        f" FROM wp_term_relationships tr"
                        f" JOIN wp_term_taxonomy tt ON tt.term_taxonomy_id=tr.term_taxonomy_id"
                        f" JOIN wp_terms t ON t.term_id=tt.term_id"
                        f" WHERE tr.object_id IN ({ph})"
                        f" AND tt.taxonomy IN ('product_cat','product_brand')"
                        f" ORDER BY tr.object_id, tt.taxonomy",
                        pids,
                    )
                    for r in cur.fetchall():
                        oid = r["object_id"]
                        if r["taxonomy"] == "product_cat":
                            cat_map.setdefault(oid, []).append(r["name"])
                        else:
                            brand_map.setdefault(oid, []).append(r["name"])

                    # Media status from hub_state
                    cur.execute(
                        f"SELECT product_id, image_type, download_status"
                        f" FROM behdashtik_hub_state_dev.bdsk_local_media_index"
                        f" WHERE product_id IN ({ph}) AND image_type='main'",
                        pids,
                    )
                    for r in cur.fetchall():
                        media_map[r["product_id"]] = r["download_status"]

                products = []
                for r in product_rows:
                    pid  = r["ID"]
                    th   = r["thumbnail_id"]
                    products.append({
                        "id":            pid,
                        "title":         r["post_title"] or "—",
                        "status":        r["post_status"] or "",
                        "modified":      str(r["post_modified"] or "")[:16],
                        "sku":           r["sku"] or "",
                        "min_price":     float(r["min_price"] or 0),
                        "max_price":     float(r["max_price"] or 0),
                        "stock_status":  r["stock_status"] or "instock",
                        "stock_qty":     r["stock_quantity"],
                        "total_sales":   int(r["total_sales"] or 0),
                        "has_image":     bool(th),
                        "image_local":   media_map.get(pid),
                        "categories":    ", ".join(cat_map.get(pid, [])) or "—",
                        "brands":        ", ".join(brand_map.get(pid, [])) or "—",
                        "url":           r["guid"] or "",
                    })

        return {
            "ok": True,
            "total": total, "published_cnt": published_cnt,
            "draft_cnt": draft_cnt, "private_cnt": private_cnt,
            "outofstock_cnt": outofstock_cnt, "backorder_cnt": backorder_cnt,
            "no_price_cnt": no_price_cnt, "no_image_cnt": no_image_cnt,
            "categories": categories, "brands": brands,
            "products": products,
            "filtered_total": filtered_total,
            "page": page, "per_page": per_page,
            "total_pages": max(1, (filtered_total + per_page - 1) // per_page),
        }
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


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

_STATIC_DIR = pathlib.Path(__file__).parent / "static"
app = Flask(__name__, static_folder=str(_STATIC_DIR), static_url_path="/static")
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
    app.config["DATA_API_KEY"] = cfg.get("data_api", {}).get("key", "")
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
@font-face {
  font-family: "Vazirmatn";
  src: url("/static/fonts/vazirmatn/Vazirmatn-Regular.woff2") format("woff2");
  font-weight: 400;
  font-style: normal;
  font-display: swap;
}
@font-face {
  font-family: "Vazirmatn";
  src: url("/static/fonts/vazirmatn/Vazirmatn-Medium.woff2") format("woff2");
  font-weight: 500 600;
  font-style: normal;
  font-display: swap;
}
@font-face {
  font-family: "Vazirmatn";
  src: url("/static/fonts/vazirmatn/Vazirmatn-Bold.woff2") format("woff2");
  font-weight: 700 900;
  font-style: normal;
  font-display: swap;
}

*, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0 }
body { font-family: "Vazirmatn", -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
       background: #f3f4f6; color: #111827; min-height: 100vh }
a { color: #2563eb; text-decoration: none }
a:hover { text-decoration: underline }

/* RTL pages */
.rtl { direction: rtl; text-align: right }
.rtl th { text-align: right }
.rtl .nav { flex-direction: row-reverse }
.rtl .nav .brand { margin-right: 0; margin-left: 28px }
.rtl .nav .user-chip { margin-right: 0; margin-left: 8px }
.rtl .filter-bar form { flex-direction: row-reverse }
.rtl .src-row { flex-direction: row-reverse }
.rtl .stat-strip { flex-direction: row-reverse }
.rtl table { direction: rtl }
.rtl td, .rtl th { text-align: right }
.rtl .tbl-wrap { direction: rtl }
.rtl .actions { flex-direction: row-reverse }
.rtl .dot { margin-right: 0; margin-left: 5px }

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

/* Source badges */
.badge-basalam { background: #fef3c7; color: #92400e }
.badge-website { background: #dbeafe; color: #1e40af }
.badge-manual  { background: #f3e8ff; color: #6b21a8 }
.badge-unknown { background: #f1f5f9; color: #475569 }
.badge-unpaid  { background: #fee2e2; color: #991b1b }
.badge-onhold  { background: #fef9c3; color: #854d0e }

/* Stat row for source breakdown */
.src-row { display: flex; gap: 12px; flex-wrap: wrap; margin-bottom: 16px }
.src-card { flex: 1; min-width: 120px; background: #f8fafc; border-radius: 8px;
            padding: 12px 16px; text-align: center }
.src-card .src-num { font-size: 1.6rem; font-weight: 800; color: #1e293b }
.src-card .src-lbl { font-size: .75rem; color: #6b7280; text-transform: uppercase;
                     letter-spacing: .05em; margin-top: 2px }
.src-card .src-sub { font-size: .72rem; color: #9ca3af; margin-top: 2px }

/* Compact filter form */
.filter-bar { background: #fff; border-radius: 10px; padding: 16px 20px;
              box-shadow: 0 1px 3px rgba(0,0,0,.06); margin-bottom: 20px }
.filter-bar form { display: flex; gap: 10px; flex-wrap: wrap; align-items: flex-end }
.filter-bar .fg  { display: flex; flex-direction: column; gap: 4px; min-width: 130px }
.filter-bar label { font-size: .78rem; font-weight: 500; color: #6b7280 }
.filter-bar select, .filter-bar input[type=text], .filter-bar input[type=date] {
  padding: 6px 10px; border: 1px solid #d1d5db; border-radius: 6px;
  font-size: .85rem; outline: none }
.filter-bar select:focus, .filter-bar input:focus { border-color: #3b82f6 }

/* Stat summary strip */
.stat-strip { display: flex; gap: 14px; flex-wrap: wrap; margin-bottom: 20px }
.stat-box { background: #fff; border-radius: 10px; padding: 16px 20px; flex: 1;
            min-width: 140px; box-shadow: 0 1px 3px rgba(0,0,0,.06) }
.stat-box .big  { font-size: 2rem; font-weight: 800; color: #1e293b; line-height: 1 }
.stat-box .lbl2 { font-size: .78rem; color: #9ca3af; margin-top: 4px; text-transform: uppercase;
                  letter-spacing: .05em }

/* Scrollable table wrapper */
.tbl-wrap { overflow-x: auto }

/* Environment info bar */
.env-bar { display: flex; align-items: center; gap: 12px; flex-wrap: wrap;
           padding: 10px 16px; border-radius: 8px; margin-bottom: 20px;
           font-size: .85rem; font-weight: 500 }
.env-bar-dev  { background: #fef9c3; border: 1px solid #fde68a; color: #78350f }
.env-bar-prod { background: #fee2e2; border: 1px solid #fca5a5; color: #7f1d1d }
.env-badge { display: inline-block; padding: 3px 10px; border-radius: 20px;
             font-size: .78rem; font-weight: 700; letter-spacing: .06em;
             background: #92400e; color: #fff }
.env-bar-prod .env-badge { background: #991b1b }

@media (max-width: 600px) {
  .page { padding: 16px }
  .grid { grid-template-columns: 1fr }
  .nav a { padding: 0 8px }
}
"""

BASE = """\
<!DOCTYPE html>
<html lang="fa" dir="rtl">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
{%- if refresh %}<meta http-equiv="refresh" content="{{ refresh }}">{%- endif %}
<title>{% block title %}بهداشتیک هاب{% endblock %}</title>
<style>{{ css | safe }}</style>
</head>
<body class="rtl">
{% block nav %}
<nav class="nav">
  <span class="brand">&#x1F4CA; بهداشتیک هاب</span>
  <a href="{{ url_for('dashboard') }}" class="{{ 'active' if active == 'dashboard' }}">داشبورد</a>
  <a href="{{ url_for('orders_page') }}" class="{{ 'active' if active == 'orders' }}">سفارش‌ها</a>
  <a href="{{ url_for('products_page') }}" class="{{ 'active' if active == 'products' }}">محصولات</a>
  <a href="{{ url_for('users_page') }}" class="{{ 'active' if active == 'users' }}">کاربران</a>
  <a href="{{ url_for('webhooks_page') }}" class="{{ 'active' if active == 'webhooks' }}">وب‌هوک‌ها</a>
  <span class="spacer"></span>
  <span class="user-chip">{{ current_user }}</span>
  <form method="post" action="{{ url_for('logout') }}" style="margin:0">
    <button class="btn btn-secondary" style="padding:5px 12px;font-size:.8rem">خروج</button>
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
<style>{{ css | safe }}</style>
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
{% block title %}داشبورد — بهداشتیک هاب{% endblock %}
{% block body %}
<div class="page">
  <h1>وضعیت سیستم <span style="font-size:.75rem;font-weight:400;color:#9ca3af">
    بروزرسانی خودکار هر ۶۰ ثانیه &nbsp;·&nbsp; {{ now }}</span></h1>

  {# Environment info bar #}
  <div class="env-bar env-bar-{{ 'dev' if env_name == 'DEV' else 'prod' }}">
    <span class="env-badge">{{ env_name }}</span>
    <span>منبع: <strong>{{ wp_source }}</strong></span>
    <span style="color:#6b7280">|</span>
    <span>میرور: <code style="font-size:.82rem">{{ mirror_db }}</code></span>
  </div>

  {# Key counts strip #}
  <div class="stat-strip">
    <div class="stat-box">
      <div class="big">{{ product_count }}</div>
      <div class="lbl2">محصول منتشرشده</div>
    </div>
    <div class="stat-box">
      <div class="big">{{ order_count }}</div>
      <div class="lbl2">سفارش (HPOS)</div>
    </div>
    <div class="stat-box">
      <div class="big">{{ media_files }}</div>
      <div class="lbl2">فایل رسانه</div>
    </div>
    <div class="stat-box">
      <div class="big">{{ pending_events }}</div>
      <div class="lbl2">رویداد در صف</div>
    </div>
  </div>

  <div class="grid">

    <div class="card">
      <h2>وردپرس</h2>
      <div class="row"><span class="lbl">وضعیت</span><span class="val">
        <span class="badge {{ 'badge-ok' if wp_ok else 'badge-err' }}">{{ '● خوب' if wp_ok else '● خطا' }}</span>
      </span></div>
      <div class="row"><span class="lbl">افزونه</span><span class="val">v{{ plugin_version }}</span></div>
      <div class="row"><span class="lbl">وردپرس</span><span class="val">{{ wp_version }}</span></div>
      <div class="row"><span class="lbl">ووکامرس</span><span class="val">{{ wc_version }}</span></div>
      <div class="row"><span class="lbl">PHP</span><span class="val">{{ php_version }}</span></div>
      <div class="row"><span class="lbl">کانکتور</span><span class="val">
        <span class="badge {{ 'badge-ok' if connector_enabled else 'badge-err' }}">
          {{ 'فعال' if connector_enabled else 'غیرفعال' }}</span>
      </span></div>
      <div class="row"><span class="lbl">اتصال</span><span class="val">
        <span class="badge {{ conn_cls }}">{{ conn_status }}</span>
      </span></div>
      <div class="row"><span class="lbl">آخرین درخواست</span><span class="val">{{ last_req or '—' }}</span></div>
    </div>

    <div class="card">
      <h2>میرور دیتابیس</h2>
      <div class="row"><span class="lbl">آخرین ایمپورت</span><span class="val">{{ last_import_at or '—' }}</span></div>
      <div class="row"><span class="lbl">آرشیو روی دیسک</span><span class="val">{{ archive_count }}</span></div>
      {% if job %}
      <div class="row"><span class="lbl">آخرین جاب</span><span class="val">{{ job.job_id }}</span></div>
      <div class="row"><span class="lbl">وضعیت جاب</span><span class="val">
        <span class="badge {{ 'badge-ok' if job.status in ('done','completed','downloaded','success') else ('badge-warn' if job.status in ('running','pending') else 'badge-err') }}">
          {{ job.status }}</span></span></div>
      <div class="row"><span class="lbl">ساخته‌شده</span><span class="val">{{ job.created_at or '—' }}</span></div>
      <div class="row"><span class="lbl">تمام‌شده</span><span class="val">{{ job.finished_at or '—' }}</span></div>
      {% else %}
      <div class="row"><span class="lbl">وضعیت</span><span class="val"><span class="badge badge-warn">بدون جاب</span></span></div>
      {% endif %}
    </div>

    <div class="card">
      <h2>رسانه</h2>
      <div class="row"><span class="lbl">وضعیت ایندکس</span><span class="val">{{ media_index_status }}</span></div>
      {% if mc %}
      <div class="row"><span class="lbl">دانلودشده</span><span class="val">{{ mc.get('downloaded',0) + mc.get('active',0) }}</span></div>
      <div class="row"><span class="lbl">در صف</span><span class="val">{{ mc.get('pending',0) + mc.get('queued',0) }}</span></div>
      <div class="row"><span class="lbl">خطا</span><span class="val">{{ mc.get('failed',0) }}</span></div>
      {% endif %}
      <div class="row"><span class="lbl">فایل روی دیسک</span><span class="val">{{ media_files }}</span></div>
      <div class="row"><span class="lbl">آخرین همگام‌سازی</span><span class="val">{{ last_sync_at or '—' }}</span></div>
    </div>

    <div class="card">
      <h2>صف رویدادها</h2>
      <div class="num">{{ pending_events }}</div>
      <div class="sub">رویداد در انتظار روی وردپرس</div>
      <br>
      <div class="row"><span class="lbl">کرسر</span><span class="val">after_id={{ event_after_id }}</span></div>
      <div class="row"><span class="lbl">آخرین همگام‌سازی</span><span class="val">{{ event_last_run or 'هرگز' }}</span></div>
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

_ORDER_SRC_FA = {"basalam": "باسلام", "website": "سایت", "manual": "دستی", "unknown": "نامشخص"}
_STATUS_FA = {
    "completed": "تکمیل‌شده", "processing": "در حال پردازش", "pending": "در انتظار پرداخت",
    "failed": "ناموفق", "on-hold": "در انتظار", "cancelled": "لغوشده",
    "refunded": "مسترد‌شده", "bslm-wait-vendor": "انتظار فروشنده (باسلام)",
    "bslm-shipping": "در حال ارسال (باسلام)", "bslm-preparation": "آماده‌سازی (باسلام)",
}

ORDERS_PAGE = """\
{% extends 'base.html' %}
{% block title %}سفارش‌ها — بهداشتیک هاب{% endblock %}
{% block body %}
<div class="page">
  <h1>گزارش سفارش‌ها</h1>

  {%- if not ok %}
  <div class="alert alert-err">خطا در اتصال به پایگاه داده: {{ error }}</div>
  {%- else %}

  {# ── خلاصه آماری ────────────────────────────────────────────────── #}
  <div class="stat-strip">
    <div class="stat-box">
      <div class="big">{{ total_orders }}</div>
      <div class="lbl2">کل سفارش‌ها</div>
    </div>
    <div class="stat-box">
      <div class="big">{{ "{:,.0f}".format(total_sales) }}</div>
      <div class="lbl2">مجموع فروش (تومان)</div>
    </div>
    <div class="stat-box">
      <div class="big" style="color:{% if unpaid_count > 0 %}#dc2626{% else %}#22c55e{% endif %}">{{ unpaid_count }}</div>
      <div class="lbl2">پرداخت‌نشده / قابل پیگیری</div>
    </div>
    <div class="stat-box">
      <div class="big" style="color:#92400e">{{ basalam_cnt }}</div>
      <div class="lbl2">سفارش‌های باسلام</div>
      <div style="font-size:.72rem;color:#9ca3af">{{ "{:,.0f}".format(basalam_sales) }} تومان</div>
    </div>
    <div class="stat-box">
      <div class="big">{{ has_phone_cnt }}/{{ has_email_cnt }}</div>
      <div class="lbl2">شماره تماس / ایمیل</div>
    </div>
  </div>

  {# ── تفکیک بر اساس منبع ───────────────────────────────────────── #}
  <div class="card" style="margin-bottom:20px">
    <h2>منبع سفارش</h2>
    <div class="src-row">
      <div class="src-card">
        <div class="src-num" style="color:#1e40af">{{ website_cnt }}</div>
        <div class="src-lbl">سایت</div>
        <div class="src-sub">{{ "{:,.0f}".format(website_sales) }}</div>
      </div>
      <div class="src-card">
        <div class="src-num" style="color:#92400e">{{ basalam_cnt }}</div>
        <div class="src-lbl">باسلام</div>
        <div class="src-sub">{{ "{:,.0f}".format(basalam_sales) }}</div>
      </div>
      <div class="src-card">
        <div class="src-num" style="color:#6b21a8">{{ manual_cnt }}</div>
        <div class="src-lbl">دستی</div>
        <div class="src-sub">{{ "{:,.0f}".format(manual_sales) }}</div>
      </div>
      <div class="src-card">
        <div class="src-num" style="color:#475569">{{ unknown_cnt }}</div>
        <div class="src-lbl">نامشخص</div>
        <div class="src-sub">—</div>
      </div>
    </div>

    <h2>وضعیت سفارش</h2>
    <div style="display:flex;gap:8px;flex-wrap:wrap;direction:rtl">
      {%- for s in by_status %}
      <span class="badge {% if s.status in ('pending','failed') %}badge-unpaid{% elif s.status == 'on-hold' %}badge-onhold{% elif s.status == 'completed' %}badge-ok{% else %}badge-warn{% endif %}">
        {{ status_fa.get(s.status, s.status) }}: {{ s.count }}
      </span>
      {%- endfor %}
    </div>
  </div>

  {# ── فرم فیلتر ─────────────────────────────────────────────────── #}
  <div class="filter-bar">
    <form method="get" action="{{ url_for('orders_page') }}">
      <div class="fg">
        <label>منبع سفارش</label>
        <select name="order_source">
          <option value="">همه منابع</option>
          <option value="website"  {% if filters.order_source == 'website'  %}selected{% endif %}>سایت</option>
          <option value="basalam"  {% if filters.order_source == 'basalam'  %}selected{% endif %}>باسلام</option>
          <option value="manual"   {% if filters.order_source == 'manual'   %}selected{% endif %}>دستی</option>
          <option value="unknown"  {% if filters.order_source == 'unknown'  %}selected{% endif %}>نامشخص</option>
        </select>
      </div>
      <div class="fg">
        <label>وضعیت</label>
        <select name="status">
          <option value="">همه وضعیت‌ها</option>
          {%- for s in by_status %}
          <option value="{{ s.status }}" {% if filters.status == s.status %}selected{% endif %}>{{ status_fa.get(s.status, s.status) }}</option>
          {%- endfor %}
        </select>
      </div>
      <div class="fg">
        <label>شماره تماس</label>
        <select name="has_phone">
          <option value="">همه</option>
          <option value="1" {% if filters.has_phone == '1' %}selected{% endif %}>دارد</option>
          <option value="0" {% if filters.has_phone == '0' %}selected{% endif %}>ندارد</option>
        </select>
      </div>
      <div class="fg">
        <label>ایمیل</label>
        <select name="has_email">
          <option value="">همه</option>
          <option value="1" {% if filters.has_email == '1' %}selected{% endif %}>دارد</option>
          <option value="0" {% if filters.has_email == '0' %}selected{% endif %}>ندارد</option>
        </select>
      </div>
      <div class="fg">
        <label>از تاریخ</label>
        <input type="date" name="date_from" value="{{ filters.date_from or '' }}">
      </div>
      <div class="fg">
        <label>تا تاریخ</label>
        <input type="date" name="date_to" value="{{ filters.date_to or '' }}">
      </div>
      <div class="fg" style="flex-direction:row;gap:6px">
        <button type="submit" class="btn btn-primary">اعمال فیلتر</button>
        <a href="{{ url_for('orders_page') }}" class="btn btn-secondary">پاک‌کردن</a>
      </div>
    </form>
  </div>

  {# ── لیست سفارش‌ها ─────────────────────────────────────────────── #}
  <div class="card" style="margin-bottom:20px">
    <h2>سفارش‌ها
      <span style="font-weight:400;color:#9ca3af;font-size:.85rem">
        — نمایش تا ۵۰ سفارش از {{ filtered_total }}
        {%- if filters.order_source or filters.status %} (فیلترشده){%- endif %}
      </span>
    </h2>
    <div class="tbl-wrap">
      <table>
        <thead><tr>
          <th>شناسه</th><th>وضعیت</th><th>منبع</th><th>شناسه خارجی</th>
          <th>مبلغ کل</th><th>تاریخ سفارش</th><th>شماره تماس</th><th>ایمیل</th>
        </tr></thead>
        <tbody>
        {%- if orders %}
          {%- for o in orders %}
          <tr>
            <td><strong>#{{ o.id }}</strong></td>
            <td><span class="badge {% if o.status in ('pending','failed') %}badge-unpaid{% elif o.status == 'on-hold' %}badge-onhold{% elif o.status == 'completed' %}badge-ok{% else %}badge-warn{% endif %}">{{ status_fa.get(o.status, o.status) }}</span></td>
            <td><span class="badge badge-{{ o.order_source }}">{{ src_fa.get(o.order_source, o.order_source) }}</span></td>
            <td style="font-family:monospace;font-size:.8rem;direction:ltr;text-align:left">{{ o.external_order_id or '—' }}</td>
            <td style="direction:ltr;text-align:left">{{ "{:,.0f}".format(o.total) }} تومان</td>
            <td style="color:#6b7280;font-size:.82rem;direction:ltr;text-align:left">{{ o.date_created }}</td>
            <td style="direction:ltr;text-align:left">{% if o.has_phone %}<span class="badge badge-ok" title="{{ o.phone_masked }}">{{ o.phone_masked }}</span>{% else %}<span style="color:#9ca3af">—</span>{% endif %}</td>
            <td style="direction:ltr;text-align:left;font-size:.78rem">{% if o.has_email %}{{ o.email_masked }}{% else %}<span style="color:#9ca3af">—</span>{% endif %}</td>
          </tr>
          {%- endfor %}
        {%- else %}
          <tr><td colspan="8" style="text-align:center;color:#9ca3af;padding:24px">سفارشی با این فیلتر یافت نشد.</td></tr>
        {%- endif %}
        </tbody>
      </table>
    </div>
  </div>

  {# ── سفارش‌های پرداخت‌نشده / قابل پیگیری ─────────────────────── #}
  <div class="card">
    <h2>سفارش‌های پرداخت‌نشده / قابل پیگیری
      <span style="font-weight:400;color:#9ca3af;font-size:.85rem">
        — وضعیت: در انتظار، ناموفق، در حال انتظار
      </span>
    </h2>
    <div class="tbl-wrap">
      <table>
        <thead><tr>
          <th>شناسه</th><th>وضعیت</th><th>دلیل</th><th>منبع</th><th>شناسه خارجی</th>
          <th>مبلغ</th><th>تاریخ ثبت</th><th>ساعت‌های گذشته</th><th>شماره تماس</th><th>ایمیل</th>
        </tr></thead>
        <tbody>
        {%- if unpaid %}
          {%- for o in unpaid %}
          <tr>
            <td><strong>#{{ o.id }}</strong></td>
            <td><span class="badge {% if o.status == 'failed' %}badge-err{% elif o.status == 'on-hold' %}badge-warn{% else %}badge-unpaid{% endif %}">{{ status_fa.get(o.status, o.status) }}</span></td>
            <td style="font-size:.8rem;color:#6b7280">{{ o.recovery_reason }}</td>
            <td><span class="badge badge-{{ o.order_source }}">{{ src_fa.get(o.order_source, o.order_source) }}</span></td>
            <td style="font-family:monospace;font-size:.8rem;direction:ltr;text-align:left">{{ o.external_order_id or '—' }}</td>
            <td style="direction:ltr;text-align:left">{{ "{:,.0f}".format(o.total) }} تومان</td>
            <td style="color:#6b7280;font-size:.82rem;direction:ltr;text-align:left">{{ o.date_created }}</td>
            <td style="color:#6b7280;direction:ltr;text-align:left">{{ o.age_hours or '—' }}</td>
            <td style="direction:ltr;text-align:left">{% if o.has_phone %}<span class="badge badge-ok">{{ o.phone_masked }}</span>{% else %}<span style="color:#9ca3af">—</span>{% endif %}</td>
            <td style="direction:ltr;text-align:left;font-size:.78rem">{% if o.has_email %}{{ o.email_masked }}{% else %}<span style="color:#9ca3af">—</span>{% endif %}</td>
          </tr>
          {%- endfor %}
        {%- else %}
          <tr><td colspan="10" style="text-align:center;color:#22c55e;padding:24px">هیچ سفارش پرداخت‌نشده‌ای وجود ندارد.</td></tr>
        {%- endif %}
        </tbody>
      </table>
    </div>
  </div>

  {%- endif %}
</div>
{% endblock %}"""

PRODUCTS_PAGE = """\
{% extends 'base.html' %}
{% block title %}محصولات — بهداشتیک هاب{% endblock %}
{% block body %}
<div class="page">
  <h1>مدیریت محصولات</h1>

  {%- if not ok %}
  <div class="alert alert-err">خطا در اتصال به پایگاه داده: {{ error }}</div>
  {%- else %}

  {# ── خلاصه آماری ────────────────────────────────────────────────── #}
  <div class="stat-strip">
    <div class="stat-box">
      <div class="big">{{ total }}</div>
      <div class="lbl2">کل محصولات</div>
    </div>
    <div class="stat-box">
      <div class="big" style="color:#16a34a">{{ published_cnt }}</div>
      <div class="lbl2">محصولات منتشرشده</div>
    </div>
    <div class="stat-box">
      <div class="big" style="color:#6b7280">{{ draft_cnt }}</div>
      <div class="lbl2">پیش‌نویس</div>
    </div>
    <div class="stat-box">
      <div class="big" style="color:#dc2626">{{ outofstock_cnt }}</div>
      <div class="lbl2">محصولات ناموجود</div>
    </div>
    <div class="stat-box">
      <div class="big" style="color:#d97706">{{ no_price_cnt }}</div>
      <div class="lbl2">بدون قیمت</div>
    </div>
    <div class="stat-box">
      <div class="big" style="color:#7c3aed">{{ no_image_cnt }}</div>
      <div class="lbl2">بدون تصویر</div>
    </div>
  </div>

  {# ── فرم جستجو و فیلتر ─────────────────────────────────────────── #}
  <div class="filter-bar">
    <form method="get" action="{{ url_for('products_page') }}">
      <div class="fg" style="min-width:180px">
        <label>جستجوی محصول</label>
        <input type="text" name="search" placeholder="نام یا کد SKU..." value="{{ filters.search or '' }}">
      </div>
      <div class="fg">
        <label>وضعیت انتشار</label>
        <select name="status">
          <option value="">همه</option>
          <option value="publish" {% if filters.status == 'publish' %}selected{% endif %}>منتشرشده</option>
          <option value="draft"   {% if filters.status == 'draft'   %}selected{% endif %}>پیش‌نویس</option>
          <option value="private" {% if filters.status == 'private' %}selected{% endif %}>خصوصی</option>
        </select>
      </div>
      <div class="fg">
        <label>وضعیت موجودی</label>
        <select name="stock_status">
          <option value="">همه</option>
          <option value="instock"     {% if filters.stock_status == 'instock'     %}selected{% endif %}>موجود</option>
          <option value="outofstock"  {% if filters.stock_status == 'outofstock'  %}selected{% endif %}>ناموجود</option>
          <option value="onbackorder" {% if filters.stock_status == 'onbackorder' %}selected{% endif %}>پیش‌سفارش</option>
        </select>
      </div>
      <div class="fg">
        <label>قیمت</label>
        <select name="has_price">
          <option value="">همه</option>
          <option value="1" {% if filters.has_price == '1' %}selected{% endif %}>دارد</option>
          <option value="0" {% if filters.has_price == '0' %}selected{% endif %}>ندارد</option>
        </select>
      </div>
      <div class="fg">
        <label>تصویر</label>
        <select name="has_image">
          <option value="">همه</option>
          <option value="1" {% if filters.has_image == '1' %}selected{% endif %}>دارد</option>
          <option value="0" {% if filters.has_image == '0' %}selected{% endif %}>ندارد</option>
        </select>
      </div>
      {%- if categories %}
      <div class="fg">
        <label>دسته‌بندی</label>
        <select name="category_id">
          <option value="">همه دسته‌ها</option>
          {%- for c in categories %}
          <option value="{{ c.id }}" {% if filters.category_id == c.id|string %}selected{% endif %}>{{ c.name }}</option>
          {%- endfor %}
        </select>
      </div>
      {%- endif %}
      {%- if brands %}
      <div class="fg">
        <label>برند</label>
        <select name="brand_id">
          <option value="">همه برندها</option>
          {%- for b in brands %}
          <option value="{{ b.id }}" {% if filters.brand_id == b.id|string %}selected{% endif %}>{{ b.name }}</option>
          {%- endfor %}
        </select>
      </div>
      {%- endif %}
      <div class="fg" style="flex-direction:row;gap:6px">
        <button type="submit" class="btn btn-primary">اعمال فیلتر</button>
        <a href="{{ url_for('products_page') }}" class="btn btn-secondary">پاک‌کردن</a>
      </div>
    </form>
  </div>

  {# ── لیست محصولات ──────────────────────────────────────────────── #}
  <div class="card">
    <h2>محصولات
      <span style="font-weight:400;color:#9ca3af;font-size:.85rem">
        — نمایش {{ products|length }} از {{ filtered_total }}
        {%- if filters.search or filters.status or filters.stock_status %} (فیلترشده){%- endif %}
      </span>
    </h2>
    <div class="tbl-wrap">
      <table>
        <thead><tr>
          <th>شناسه</th>
          <th>نام محصول</th>
          <th>وضعیت</th>
          <th>کد SKU</th>
          <th>قیمت (تومان)</th>
          <th>موجودی</th>
          <th>تعداد</th>
          <th>دسته‌بندی</th>
          <th>برند</th>
          <th>تصویر</th>
          <th>آخرین بروزرسانی</th>
          <th>مشاهده</th>
        </tr></thead>
        <tbody>
        {%- if products %}
          {%- for p in products %}
          <tr>
            <td><strong>#{{ p.id }}</strong></td>
            <td style="max-width:200px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap" title="{{ p.title }}">{{ p.title }}</td>
            <td><span class="badge {% if p.status == 'publish' %}badge-ok{% elif p.status == 'draft' %}badge-warn{% else %}badge-err{% endif %}">{{ prod_status_fa.get(p.status, p.status) }}</span></td>
            <td style="font-family:monospace;font-size:.8rem;direction:ltr;text-align:left">{{ p.sku or '—' }}</td>
            <td style="direction:ltr;text-align:left">
              {%- if p.min_price > 0 %}
                {{ "{:,.0f}".format(p.min_price) }}
                {%- if p.max_price > p.min_price %} – {{ "{:,.0f}".format(p.max_price) }}{%- endif %}
              {%- else %}—{%- endif %}
            </td>
            <td><span class="badge {% if p.stock_status == 'instock' %}badge-ok{% elif p.stock_status == 'onbackorder' %}badge-warn{% else %}badge-err{% endif %}">{{ stock_fa.get(p.stock_status, p.stock_status) }}</span></td>
            <td style="direction:ltr;text-align:left;color:#6b7280">{{ p.stock_qty if p.stock_qty is not none else '—' }}</td>
            <td style="font-size:.8rem;color:#374151">{{ p.categories }}</td>
            <td style="font-size:.8rem;color:#374151">{{ p.brands }}</td>
            <td style="text-align:center">
              {%- if p.image_local == 'downloaded' %}
                <span class="badge badge-ok" title="دانلودشده">✓</span>
              {%- elif p.has_image %}
                <span class="badge badge-warn" title="در سایت موجود است">▲</span>
              {%- else %}
                <span style="color:#9ca3af">—</span>
              {%- endif %}
            </td>
            <td style="color:#6b7280;font-size:.82rem;direction:ltr;text-align:left">{{ p.modified }}</td>
            <td>
              {%- if p.url %}
              <a href="{{ p.url }}" target="_blank" class="btn btn-secondary" style="padding:3px 10px;font-size:.78rem">مشاهده</a>
              {%- else %}—{%- endif %}
            </td>
          </tr>
          {%- endfor %}
        {%- else %}
          <tr><td colspan="12" style="text-align:center;color:#9ca3af;padding:24px">محصولی با این فیلتر یافت نشد.</td></tr>
        {%- endif %}
        </tbody>
      </table>
    </div>

    {# ── صفحه‌بندی ──────────────────────────────────────────────── #}
    {%- if total_pages > 1 %}
    <div style="display:flex;gap:8px;justify-content:center;padding:16px 0;flex-wrap:wrap">
      {%- if page > 1 %}
      <a href="?{{ filters_qs }}&page={{ page - 1 }}" class="btn btn-secondary">‹ قبلی</a>
      {%- endif %}
      <span style="padding:6px 12px;color:#6b7280;font-size:.88rem">صفحه {{ page }} از {{ total_pages }}</span>
      {%- if page < total_pages %}
      <a href="?{{ filters_qs }}&page={{ page + 1 }}" class="btn btn-secondary">بعدی ›</a>
      {%- endif %}
    </div>
    {%- endif %}
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
    "orders.html":    ORDERS_PAGE,
    "products.html":  PRODUCTS_PAGE,
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
    env_name   = cfg.get("env", "?").upper()
    wp_source  = cfg.get("wp_base_url", "?")
    mirror_db  = cfg["mirror_db"]["name"]
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
                       pending_events="?", event_after_id=0, event_last_run=None,
                       env_name=env_name, wp_source=wp_source, mirror_db=mirror_db,
                       product_count=0, order_count=0, last_import_at=None)

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
                   event_last_run=d.get("event_last_run"),
                   env_name=env_name, wp_source=wp_source, mirror_db=mirror_db,
                   product_count=d.get("product_count", 0),
                   order_count=d.get("order_count", 0),
                   last_import_at=d.get("last_import_at"))


@app.route("/health")
def health():
    return {"status": "ok"}


# ---------------------------------------------------------------------------
# Routes — orders
# ---------------------------------------------------------------------------

@app.route("/orders")
def orders_page():
    redir = _require_login()
    if redir:
        return redir

    filters = {
        "order_source": request.args.get("order_source", ""),
        "status":       request.args.get("status", ""),
        "has_phone":    request.args.get("has_phone", ""),
        "has_email":    request.args.get("has_email", ""),
        "date_from":    request.args.get("date_from", ""),
        "date_to":      request.args.get("date_to", ""),
    }
    d = get_orders_page_data(filters)
    return _render("orders.html", active="orders", filters=filters,
                   src_fa=_ORDER_SRC_FA, status_fa=_STATUS_FA, **d)


@app.route("/products")
def products_page():
    redir = _require_login()
    if redir:
        return redir

    filters = {
        "search":       request.args.get("search", ""),
        "status":       request.args.get("status", ""),
        "stock_status": request.args.get("stock_status", ""),
        "has_price":    request.args.get("has_price", ""),
        "has_image":    request.args.get("has_image", ""),
        "category_id":  request.args.get("category_id", ""),
        "brand_id":     request.args.get("brand_id", ""),
        "page":         request.args.get("page", "1"),
    }
    # Build query-string for pagination links (without page param)
    qs_parts = [f"{k}={v}" for k, v in filters.items() if v and k != "page"]
    filters_qs = "&".join(qs_parts)

    d = get_products_page_data(filters)
    return _render("products.html", active="products", filters=filters,
                   filters_qs=filters_qs,
                   stock_fa=_STOCK_STATUS_FA,
                   prod_status_fa=_PRODUCT_STATUS_FA, **d)


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
        _hub = load_config().get("hub", {})
        app.run(
            host=_hub.get("host", "127.0.0.1"),
            port=int(_hub.get("port", 8090)),
            debug=False,
        )

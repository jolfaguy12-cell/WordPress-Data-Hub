"""
Behdashtik Data Hub — Read-Only REST API  (Blueprint: /api/v1)

All endpoints require the header:  X-Hub-API-Key: <key>
Key is read from config.json (or .env BDSK_DATA_API_KEY) via the shared config loader.
"""

from __future__ import annotations

import json
import pathlib
from contextlib import contextmanager
from datetime import datetime, timezone
from functools import wraps

import pymysql
import pymysql.cursors
from flask import Blueprint, current_app, jsonify, request

API_VERSION = "1.0.0"
data_api = Blueprint("data_api", __name__, url_prefix="/api/v1")

# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------

def _configured_key() -> str:
    return current_app.config.get("DATA_API_KEY", "")


def require_api_key(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        key = request.headers.get("X-Hub-API-Key", "")
        cfg_key = _configured_key()
        if not cfg_key:
            return _err("not_configured", "Data API key is not configured on the server.", 503)
        if not key or key != cfg_key:
            return _err("unauthorized", "Invalid or missing X-Hub-API-Key header.", 401)
        return f(*args, **kwargs)
    return wrapper


# ---------------------------------------------------------------------------
# DB / config helpers
# ---------------------------------------------------------------------------

@contextmanager
def _db():
    cfg = current_app.config["PIPELINE_CFG"]
    db = cfg["mirror_db"]
    conn = pymysql.connect(
        host=db["host"],
        port=db.get("port", 3306),
        user=db.get("readonly_user", db["user"]),
        password=db.get("readonly_password", db["password"]),
        database=db["name"],
        charset="utf8mb4",
        cursorclass=pymysql.cursors.DictCursor,
        init_command="SET sql_mode=''",
    )
    try:
        yield conn
    finally:
        conn.close()


@contextmanager
def _hub_db():
    """Read-only connection to the persistent hub-state DB (bdsk_local_media_index etc.)."""
    cfg  = current_app.config["PIPELINE_CFG"]
    db   = cfg["mirror_db"]  # same host/creds
    name = cfg.get("hub_state_db", {}).get("name", "behdashtik_hub_state")
    try:
        conn = pymysql.connect(
            host=db["host"],
            port=db.get("port", 3306),
            user=db.get("readonly_user", db["user"]),
            password=db.get("readonly_password", db["password"]),
            database=name,
            charset="utf8mb4",
            cursorclass=pymysql.cursors.DictCursor,
            init_command="SET sql_mode=''",
        )
        try:
            yield conn
        finally:
            conn.close()
    except Exception:
        # Hub DB may not exist yet (before first media-sync run).  Yield a sentinel
        # that callers can detect via a flag rather than crashing the request.
        yield None


def _media_base() -> pathlib.Path:
    cfg = current_app.config["PIPELINE_CFG"]
    p = cfg.get("media_sync", {}).get("storage_path", "")
    return pathlib.Path(p) if p else pathlib.Path(__file__).parent.parent / "data" / "media"


def _wp_uploads_url() -> str:
    cfg = current_app.config["PIPELINE_CFG"]
    return cfg.get("wp_base_url", "").rstrip("/") + "/wp-content/uploads"


# ---------------------------------------------------------------------------
# Response helpers
# ---------------------------------------------------------------------------

def _ok(data, **extra):
    resp = {"data": data}
    resp.update(extra)
    return jsonify(resp)


def _err(code: str, message: str, status: int = 400):
    return jsonify({"error": code, "message": message}), status


def _paginate(total: int, page: int, per_page: int) -> dict:
    return {
        "page": page,
        "per_page": per_page,
        "total": total,
        "pages": max(1, (total + per_page - 1) // per_page),
    }


def _page_params() -> tuple[int, int]:
    try:
        page = max(1, int(request.args.get("page", 1)))
    except (TypeError, ValueError):
        page = 1
    try:
        per_page = min(100, max(1, int(request.args.get("per_page", 20))))
    except (TypeError, ValueError):
        per_page = 20
    return page, per_page


def _dt(val) -> str | None:
    if val is None:
        return None
    if isinstance(val, datetime):
        return val.isoformat()
    s = str(val)
    return None if s.startswith("0000") else s


def _int(val) -> int | None:
    try:
        return int(val) if val is not None and val != "" else None
    except (TypeError, ValueError):
        return None


def _float(val) -> float | None:
    try:
        return float(val) if val is not None and val != "" else None
    except (TypeError, ValueError):
        return None


def _mask_email(email: str | None) -> str | None:
    if not email:
        return None
    try:
        local, domain = email.split("@", 1)
        return f"{local[:1]}***@{domain}"
    except ValueError:
        return "***"


def _mask_phone(phone: str | None) -> str | None:
    if not phone:
        return None
    p = str(phone).strip()
    if len(p) <= 5:
        return "***"
    return f"{p[:3]}***{p[-2:]}"


# ---------------------------------------------------------------------------
# Internal: product helpers
# ---------------------------------------------------------------------------

def _meta_dict(cur, post_id: int) -> dict:
    cur.execute("SELECT meta_key, meta_value FROM wp_postmeta WHERE post_id = %s", (post_id,))
    return {r["meta_key"]: r["meta_value"] for r in cur.fetchall()}


def _terms_by_taxonomy(cur, post_id: int) -> dict:
    cur.execute(
        """SELECT tt.taxonomy, t.term_id, t.name, t.slug
           FROM wp_term_relationships tr
           JOIN wp_term_taxonomy tt ON tr.term_taxonomy_id = tt.term_taxonomy_id
           JOIN wp_terms t ON tt.term_id = t.term_id
           WHERE tr.object_id = %s""",
        (post_id,),
    )
    result: dict[str, list] = {}
    for r in cur.fetchall():
        result.setdefault(r["taxonomy"], []).append(
            {"id": r["term_id"], "name": r["name"], "slug": r["slug"]}
        )
    return result


def _lookup_row(cur, product_id: int) -> dict:
    cur.execute(
        "SELECT * FROM wp_wc_product_meta_lookup WHERE product_id = %s", (product_id,)
    )
    return cur.fetchone() or {}


def _attachment_image(cur, att_id: int) -> dict | None:
    if not att_id:
        return None
    cur.execute(
        "SELECT ID, post_title, guid FROM wp_posts WHERE ID = %s AND post_type = 'attachment'",
        (att_id,),
    )
    post = cur.fetchone()
    if not post:
        return None
    cur.execute(
        "SELECT meta_value FROM wp_postmeta WHERE post_id = %s AND meta_key = '_wp_attached_file'",
        (att_id,),
    )
    row = cur.fetchone()
    rel_path = row["meta_value"] if row else None
    media_base = _media_base()
    uploads_url = _wp_uploads_url()
    local_file = (media_base / rel_path) if rel_path else None
    return {
        "id": post["ID"],
        "title": post["post_title"],
        "url": f"{uploads_url}/{rel_path}" if rel_path else post["guid"],
        "local_path": str(local_file) if local_file and local_file.exists() else None,
        "file": rel_path,
    }


def _product_images(cur, post_id: int, meta: dict) -> list:
    ids: list[int] = []
    thumb_id = _int(meta.get("_thumbnail_id"))
    if thumb_id:
        ids.append(thumb_id)
    for gid in (meta.get("_product_image_gallery") or "").split(","):
        gid = gid.strip()
        n = _int(gid)
        if n and n not in ids:
            ids.append(n)

    images = []
    for i, aid in enumerate(ids):
        img = _attachment_image(cur, aid)
        if img:
            img["position"] = i
            img["is_thumbnail"] = aid == thumb_id
            images.append(img)
    return images


def _build_product(post: dict, meta: dict, terms: dict, lookup: dict, *,
                   full: bool = False, cur=None) -> dict:
    obj: dict = {
        "id": post["ID"],
        "name": post.get("post_title"),
        "slug": post.get("post_name"),
        "status": post.get("post_status"),
        "type": "variable" if post.get("post_type") == "product" and meta.get("_product_type") == "variable" else post.get("post_type"),
        "parent_id": _int(post.get("post_parent")) or None,
        "date_created": _dt(post.get("post_date")),
        "date_modified": _dt(post.get("post_modified")),
        "sku": lookup.get("sku") or meta.get("_sku"),
        "global_unique_id": lookup.get("global_unique_id") or meta.get("_global_unique_id"),
        "price": _float(lookup.get("min_price") or meta.get("_price")),
        "regular_price": _float(meta.get("_regular_price")),
        "sale_price": _float(meta.get("_sale_price")) if meta.get("_sale_price") else None,
        "on_sale": bool(lookup.get("onsale")),
        "stock_quantity": _float(lookup.get("stock_quantity") or meta.get("_stock")),
        "stock_status": lookup.get("stock_status") or meta.get("_stock_status"),
        "manage_stock": meta.get("_manage_stock") == "yes",
        "backorders": meta.get("_backorders"),
        "virtual": bool(lookup.get("virtual")) or meta.get("_virtual") == "yes",
        "downloadable": bool(lookup.get("downloadable")) or meta.get("_downloadable") == "yes",
        "tax_status": lookup.get("tax_status") or meta.get("_tax_status"),
        "tax_class": lookup.get("tax_class") or meta.get("_tax_class"),
        "average_rating": _float(lookup.get("average_rating") or meta.get("_wc_average_rating")),
        "review_count": _int(lookup.get("rating_count") or meta.get("_wc_review_count")),
        "total_sales": _int(lookup.get("total_sales") or meta.get("total_sales")),
        "categories": terms.get("product_cat", []),
        "brands": terms.get("product_brand", []),
        "tags": terms.get("product_tag", []),
    }

    if full and cur is not None:
        obj["description"] = post.get("post_content") or None
        obj["short_description"] = post.get("post_excerpt") or None
        obj["images"] = _product_images(cur, post["ID"], meta)
        obj["seo"] = {
            "title": meta.get("rank_math_title"),
            "description": meta.get("rank_math_description"),
            "focus_keyword": meta.get("rank_math_focus_keyword"),
            "score": _int(meta.get("rank_math_seo_score")),
        }
        obj["raw_attributes"] = meta.get("_product_attributes")

        cur.execute(
            "SELECT * FROM wp_posts WHERE post_parent = %s AND post_type = 'product_variation' ORDER BY menu_order",
            (post["ID"],),
        )
        variations = []
        for vp in cur.fetchall():
            vmeta = _meta_dict(cur, vp["ID"])
            vlookup = _lookup_row(cur, vp["ID"])
            variations.append(_build_variation(vp, vmeta, vlookup))
        obj["variations"] = variations

    return obj


def _build_variation(post: dict, meta: dict, lookup: dict) -> dict:
    return {
        "id": post["ID"],
        "parent_id": _int(post.get("post_parent")),
        "status": post.get("post_status"),
        "date_created": _dt(post.get("post_date")),
        "date_modified": _dt(post.get("post_modified")),
        "sku": lookup.get("sku") or meta.get("_sku"),
        "price": _float(lookup.get("min_price") or meta.get("_price")),
        "regular_price": _float(meta.get("_regular_price")),
        "sale_price": _float(meta.get("_sale_price")) if meta.get("_sale_price") else None,
        "on_sale": bool(lookup.get("onsale")),
        "stock_quantity": _float(lookup.get("stock_quantity") or meta.get("_stock")),
        "stock_status": lookup.get("stock_status") or meta.get("_stock_status"),
        "manage_stock": meta.get("_manage_stock") == "yes",
        "virtual": bool(lookup.get("virtual")) or meta.get("_virtual") == "yes",
        "downloadable": bool(lookup.get("downloadable")) or meta.get("_downloadable") == "yes",
        "raw_attributes": meta.get("_product_attributes"),
    }


# ---------------------------------------------------------------------------
# Internal: order helpers
# ---------------------------------------------------------------------------

def _order_meta(cur, order_id: int) -> dict:
    cur.execute(
        "SELECT meta_key, meta_value FROM wp_wc_orders_meta WHERE order_id = %s", (order_id,)
    )
    return {r["meta_key"]: r["meta_value"] for r in cur.fetchall()}


def _order_items(cur, order_id: int) -> list:
    cur.execute(
        "SELECT order_item_id, order_item_name, order_item_type FROM wp_woocommerce_order_items WHERE order_id = %s",
        (order_id,),
    )
    items = cur.fetchall()
    result = []
    for item in items:
        cur.execute(
            "SELECT meta_key, meta_value FROM wp_woocommerce_order_itemmeta WHERE order_item_id = %s",
            (item["order_item_id"],),
        )
        im = {r["meta_key"]: r["meta_value"] for r in cur.fetchall()}
        entry: dict = {
            "id": item["order_item_id"],
            "name": item["order_item_name"],
            "type": item["order_item_type"],
        }
        if item["order_item_type"] == "line_item":
            entry.update({
                "product_id": _int(im.get("_product_id")),
                "variation_id": _int(im.get("_variation_id")) or None,
                "quantity": _float(im.get("_qty")),
                "subtotal": _float(im.get("_line_subtotal")),
                "total": _float(im.get("_line_total")),
                "subtotal_tax": _float(im.get("_line_subtotal_tax")),
                "total_tax": _float(im.get("_line_tax")),
                "tax_class": im.get("_tax_class"),
            })
        elif item["order_item_type"] == "shipping":
            entry.update({
                "method_id": im.get("method_id"),
                "method_title": im.get("method_title") or item["order_item_name"],
                "total": _float(im.get("cost")),
                "total_tax": _float(im.get("taxes")),
            })
        elif item["order_item_type"] == "fee":
            entry.update({
                "total": _float(im.get("line_total")),
                "tax_class": im.get("tax_class"),
                "tax_status": im.get("tax_status"),
                "total_tax": _float(im.get("line_tax")),
            })
        result.append(entry)
    return result


_ADDR_FIELDS = ("first_name", "last_name", "company", "address_1", "address_2",
                "city", "state", "postcode", "country", "phone", "email")


def _order_addresses(cur, order_id: int) -> dict:
    cur.execute(
        "SELECT address_type, first_name, last_name, company, address_1, address_2,"
        " city, state, postcode, country, email, phone"
        " FROM wp_wc_order_addresses WHERE order_id=%s",
        (order_id,),
    )
    result: dict = {}
    for row in cur.fetchall():
        atype = row.pop("address_type", None) if isinstance(row, dict) else row["address_type"]
        if isinstance(row, dict):
            result[atype] = {k: row.get(k) for k in _ADDR_FIELDS}
        else:
            result[atype] = dict(zip(_ADDR_FIELDS, row[1:]))
    return result


def _order_operational_data(cur, order_id: int) -> dict:
    cur.execute(
        "SELECT created_via, woocommerce_version, order_key,"
        " date_paid_gmt, date_completed_gmt,"
        " shipping_total_amount, discount_total_amount"
        " FROM wp_wc_order_operational_data WHERE order_id=%s",
        (order_id,),
    )
    row = cur.fetchone()
    if not row:
        return {}
    return {
        "created_via": row.get("created_via") or None,
        "order_key": row.get("order_key"),
        "date_paid": _dt(row.get("date_paid_gmt")),
        "date_completed": _dt(row.get("date_completed_gmt")),
        "shipping_total": _float(row.get("shipping_total_amount")),
        "discount_total": _float(row.get("discount_total_amount")),
    }


def _order_source(meta: dict, op_data: dict) -> dict:
    """Derive order source/channel from meta and operational data."""
    basalam_id = meta.get("_sync_basalam_hash_id")
    if basalam_id:
        source_meta: dict = {}
        for k in ("_basalam_fee_amount", "_basalam_balance_amount", "_basalam_purchase_count"):
            if k in meta:
                source_meta[k.lstrip("_")] = meta[k]
        for k, v in meta.items():
            if k.startswith("_sync_basalam_item_id_"):
                source_meta[k.lstrip("_")] = v
        return {
            "order_source":          "basalam",
            "source_channel":        "basalam",
            "external_order_id":     basalam_id,
            "external_marketplace":  "basalam",
            "created_via":           op_data.get("created_via"),
            "source_meta":           source_meta or None,
        }

    created_via = op_data.get("created_via") or ""
    if created_via == "checkout":
        return {
            "order_source":         "website",
            "source_channel":       "woocommerce_checkout",
            "external_order_id":    None,
            "external_marketplace": None,
            "created_via":          created_via,
            "source_meta":          None,
        }
    if created_via == "admin":
        return {
            "order_source":         "manual",
            "source_channel":       "admin",
            "external_order_id":    None,
            "external_marketplace": None,
            "created_via":          created_via,
            "source_meta":          None,
        }
    return {
        "order_source":         "unknown",
        "source_channel":       created_via or None,
        "external_order_id":    None,
        "external_marketplace": None,
        "created_via":          created_via or None,
        "source_meta":          None,
    }


def _build_order(order: dict, meta: dict, items: list,
                 addresses: dict | None = None, op_data: dict | None = None) -> dict:
    status = order.get("status", "")
    if status.startswith("wc-"):
        status = status[3:]
    addrs  = addresses or {}
    op     = op_data or {}
    source = _order_source(meta, op)
    return {
        "id": order["id"],
        "status": status,
        "currency": order.get("currency"),
        "date_created": _dt(order.get("date_created_gmt")),
        "date_modified": _dt(order.get("date_updated_gmt")),
        "date_paid": op.get("date_paid"),
        "date_completed": op.get("date_completed"),
        "total": _float(order.get("total_amount")),
        "tax_total": _float(order.get("tax_amount")),
        "shipping_total": op.get("shipping_total"),
        "discount_total": op.get("discount_total"),
        "customer_id": _int(order.get("customer_id")),
        "customer_note": order.get("customer_note"),
        "billing_email": order.get("billing_email"),
        "payment_method": order.get("payment_method"),
        "payment_method_title": order.get("payment_method_title"),
        "transaction_id": order.get("transaction_id"),
        "order_key": op.get("order_key"),
        "parent_order_id": _int(order.get("parent_order_id")) or None,
        "order_source": source["order_source"],
        "source_channel": source["source_channel"],
        "external_order_id": source["external_order_id"],
        "external_marketplace": source["external_marketplace"],
        "created_via": source["created_via"],
        "source_meta": source["source_meta"],
        "billing": addrs.get("billing", {k: None for k in _ADDR_FIELDS}),
        "shipping": addrs.get("shipping", {k: None for k in _ADDR_FIELDS}),
        "line_items": [i for i in items if i["type"] == "line_item"],
        "shipping_lines": [i for i in items if i["type"] == "shipping"],
        "fee_lines": [i for i in items if i["type"] == "fee"],
        "meta": {
            k: v for k, v in meta.items()
            if not k.startswith(("_billing_", "_shipping_"))
            and k not in ("_billing_address_index", "_shipping_address_index")
        },
    }


# ---------------------------------------------------------------------------
# PRODUCTS
# ---------------------------------------------------------------------------

@data_api.get("/products")
@require_api_key
def list_products():
    page, per_page = _page_params()
    offset = (page - 1) * per_page

    status = request.args.get("status")
    stock_status = request.args.get("stock_status")
    category_id = request.args.get("category_id", type=int)
    brand_id = request.args.get("brand_id", type=int)
    search = request.args.get("search", "").strip()
    modified_since = request.args.get("modified_since")
    sort_by = request.args.get("sort_by", "id")
    sort_dir = "ASC" if request.args.get("sort_dir", "desc").lower() == "asc" else "DESC"

    sort_col = {
        "id": "p.ID", "date": "p.post_date", "modified": "p.post_modified",
        "name": "p.post_title", "price": "l.min_price", "sales": "l.total_sales",
    }.get(sort_by, "p.ID")

    where = ["p.post_type = 'product'", "p.post_status != 'auto-draft'"]
    params: list = []

    if status:
        where.append("p.post_status = %s")
        params.append(status)
    if stock_status:
        where.append("l.stock_status = %s")
        params.append(stock_status)
    if search:
        where.append("(p.post_title LIKE %s OR l.sku LIKE %s OR l.global_unique_id LIKE %s)")
        params.extend([f"%{search}%", f"%{search}%", f"%{search}%"])
    if modified_since:
        where.append("p.post_modified >= %s")
        params.append(modified_since)
    if category_id:
        where.append("""p.ID IN (
            SELECT tr.object_id FROM wp_term_relationships tr
            JOIN wp_term_taxonomy tt ON tr.term_taxonomy_id = tt.term_taxonomy_id
            WHERE tt.taxonomy = 'product_cat' AND tt.term_id = %s)""")
        params.append(category_id)
    if brand_id:
        where.append("""p.ID IN (
            SELECT tr.object_id FROM wp_term_relationships tr
            JOIN wp_term_taxonomy tt ON tr.term_taxonomy_id = tt.term_taxonomy_id
            WHERE tt.taxonomy = 'product_brand' AND tt.term_id = %s)""")
        params.append(brand_id)

    where_sql = " AND ".join(where)

    with _db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"SELECT COUNT(*) as cnt FROM wp_posts p "
                f"LEFT JOIN wp_wc_product_meta_lookup l ON l.product_id = p.ID "
                f"WHERE {where_sql}",
                params,
            )
            total = cur.fetchone()["cnt"]
            cur.execute(
                f"""SELECT p.ID, p.post_title, p.post_name, p.post_status, p.post_type,
                           p.post_date, p.post_modified,
                           l.sku, l.global_unique_id, l.min_price, l.max_price,
                           l.stock_quantity, l.stock_status, l.onsale,
                           l.average_rating, l.rating_count, l.total_sales
                    FROM wp_posts p
                    LEFT JOIN wp_wc_product_meta_lookup l ON l.product_id = p.ID
                    WHERE {where_sql}
                    ORDER BY {sort_col} {sort_dir}
                    LIMIT %s OFFSET %s""",
                params + [per_page, offset],
            )
            rows = cur.fetchall()
            products = []
            for r in rows:
                terms = _terms_by_taxonomy(cur, r["ID"])
                products.append({
                    "id": r["ID"],
                    "name": r["post_title"],
                    "slug": r["post_name"],
                    "status": r["post_status"],
                    "sku": r["sku"],
                    "global_unique_id": r["global_unique_id"],
                    "price": _float(r["min_price"]),
                    "max_price": _float(r["max_price"]),
                    "on_sale": bool(r["onsale"]),
                    "stock_quantity": _float(r["stock_quantity"]),
                    "stock_status": r["stock_status"],
                    "average_rating": _float(r["average_rating"]),
                    "total_sales": _int(r["total_sales"]),
                    "date_created": _dt(r["post_date"]),
                    "date_modified": _dt(r["post_modified"]),
                    "categories": terms.get("product_cat", []),
                    "brands": terms.get("product_brand", []),
                })

    return _ok(products, pagination=_paginate(total, page, per_page))


@data_api.get("/products/<int:pid>")
@require_api_key
def get_product(pid):
    with _db() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM wp_posts WHERE ID = %s AND post_type = 'product'", (pid,))
            post = cur.fetchone()
            if not post:
                return _err("not_found", f"Product {pid} not found.", 404)
            meta = _meta_dict(cur, pid)
            terms = _terms_by_taxonomy(cur, pid)
            lookup = _lookup_row(cur, pid)
            product = _build_product(post, meta, terms, lookup, full=True, cur=cur)
    return _ok(product)


@data_api.get("/products/<int:pid>/variations")
@require_api_key
def get_product_variations(pid):
    with _db() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT ID FROM wp_posts WHERE ID = %s AND post_type = 'product'", (pid,))
            if not cur.fetchone():
                return _err("not_found", f"Product {pid} not found.", 404)
            cur.execute(
                "SELECT * FROM wp_posts WHERE post_parent = %s AND post_type = 'product_variation' ORDER BY menu_order",
                (pid,),
            )
            variations = []
            for vp in cur.fetchall():
                vmeta = _meta_dict(cur, vp["ID"])
                vlookup = _lookup_row(cur, vp["ID"])
                variations.append(_build_variation(vp, vmeta, vlookup))
    return _ok(variations)


def _media_mapping_for_product(hub_conn, pid: int) -> dict:
    """Return { attachment_id: {role, variation_id, sync_status, local_path, checksum, original_url} }
    from the persistent hub-state DB. Empty if the hub DB is unavailable."""
    if hub_conn is None:
        return {}
    try:
        with hub_conn.cursor() as cur:
            cur.execute(
                "SELECT attachment_id, role, variation_id, download_status, "
                "local_path, checksum, original_url "
                "FROM bdsk_local_media_index "
                "WHERE product_id = %s AND manifest_status = 'active'",
                (pid,),
            )
            out: dict = {}
            for r in cur.fetchall():
                out[int(r["attachment_id"])] = {
                    "role":         r["role"] or None,
                    "variation_id": int(r["variation_id"]) if r["variation_id"] else None,
                    "sync_status":  r["download_status"],
                    "local_path":   r["local_path"],
                    "checksum":     r["checksum"],
                    "original_url": r["original_url"],
                }
            return out
    except Exception:
        return {}


@data_api.get("/products/<int:pid>/images")
@require_api_key
def get_product_images(pid):
    with _db() as conn, _hub_db() as hub_conn:
        with conn.cursor() as cur:
            cur.execute("SELECT ID FROM wp_posts WHERE ID = %s AND post_type = 'product'", (pid,))
            if not cur.fetchone():
                return _err("not_found", f"Product {pid} not found.", 404)
            meta = _meta_dict(cur, pid)
            images = _product_images(cur, pid, meta)
            mapping = _media_mapping_for_product(hub_conn, pid)

            # If mirror doesn't have the attachment posts (e.g. test/partial export),
            # fall back to building image entries from the hub media index directly.
            if not images and mapping:
                for att_id, m in sorted(mapping.items()):
                    images.append({
                        "id":          att_id,
                        "title":       None,
                        "url":         m["original_url"],
                        "local_path":  m["local_path"],
                        "file":        None,
                        "position":    len(images),
                        "is_thumbnail": m["role"] == "main",
                    })

            for img in images:
                m = mapping.get(int(img["id"])) if img.get("id") else None
                img["role"]         = m["role"] if m else None
                img["variation_id"] = m["variation_id"] if m else None
                img["sync_status"]  = m["sync_status"] if m else "not_synced"
                # Prefer the mapping's verified local_path/checksum when present
                if m and m["local_path"]:
                    img["local_path"] = m["local_path"]
                if m and m["checksum"]:
                    img["checksum"] = m["checksum"]
    return _ok(images)


# ---------------------------------------------------------------------------
# CATEGORIES
# ---------------------------------------------------------------------------

@data_api.get("/categories")
@require_api_key
def list_categories():
    with _db() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT t.term_id as id, t.name, t.slug, tt.parent, tt.count
                FROM wp_terms t
                JOIN wp_term_taxonomy tt ON t.term_id = tt.term_id
                WHERE tt.taxonomy = 'product_cat'
                ORDER BY tt.parent ASC, t.name ASC
            """)
            cats = [dict(r) for r in cur.fetchall()]
    return _ok(cats)


@data_api.get("/categories/tree")
@require_api_key
def categories_tree():
    with _db() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT t.term_id as id, t.name, t.slug, tt.parent, tt.count
                FROM wp_terms t
                JOIN wp_term_taxonomy tt ON t.term_id = tt.term_id
                WHERE tt.taxonomy = 'product_cat'
                ORDER BY tt.parent ASC, t.name ASC
            """)
            cats = [dict(r) for r in cur.fetchall()]

    by_id = {c["id"]: {**c, "children": []} for c in cats}
    roots = []
    for c in by_id.values():
        if c["parent"] == 0:
            roots.append(c)
        elif c["parent"] in by_id:
            by_id[c["parent"]]["children"].append(c)
    return _ok(roots)


@data_api.get("/categories/<int:cid>")
@require_api_key
def get_category(cid):
    with _db() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT t.term_id as id, t.name, t.slug, tt.parent, tt.count
                FROM wp_terms t
                JOIN wp_term_taxonomy tt ON t.term_id = tt.term_id
                WHERE tt.taxonomy = 'product_cat' AND t.term_id = %s
            """, (cid,))
            cat = cur.fetchone()
            if not cat:
                return _err("not_found", f"Category {cid} not found.", 404)
            cat = dict(cat)
            ancestors = []
            parent_id = cat["parent"]
            while parent_id:
                cur.execute("""
                    SELECT t.term_id as id, t.name, t.slug, tt.parent
                    FROM wp_terms t JOIN wp_term_taxonomy tt ON t.term_id = tt.term_id
                    WHERE tt.taxonomy = 'product_cat' AND t.term_id = %s
                """, (parent_id,))
                anc = cur.fetchone()
                if not anc:
                    break
                ancestors.insert(0, {"id": anc["id"], "name": anc["name"], "slug": anc["slug"]})
                parent_id = anc["parent"]
            cat["ancestors"] = ancestors
    return _ok(cat)


@data_api.get("/categories/<int:cid>/products")
@require_api_key
def category_products(cid):
    page, per_page = _page_params()
    offset = (page - 1) * per_page
    with _db() as conn:
        with conn.cursor() as cur:
            cur.execute("""SELECT t.term_id FROM wp_terms t JOIN wp_term_taxonomy tt ON t.term_id=tt.term_id
                WHERE tt.taxonomy='product_cat' AND t.term_id=%s""", (cid,))
            if not cur.fetchone():
                return _err("not_found", f"Category {cid} not found.", 404)
            cur.execute("""SELECT COUNT(*) as cnt
                FROM wp_posts p
                JOIN wp_term_relationships tr ON p.ID = tr.object_id
                JOIN wp_term_taxonomy tt ON tr.term_taxonomy_id = tt.term_taxonomy_id
                WHERE tt.taxonomy='product_cat' AND tt.term_id=%s
                  AND p.post_type='product' AND p.post_status != 'auto-draft'""", (cid,))
            total = cur.fetchone()["cnt"]
            cur.execute("""
                SELECT p.ID, p.post_title, p.post_name, p.post_status,
                       p.post_date, p.post_modified,
                       l.sku, l.min_price, l.stock_quantity, l.stock_status, l.onsale
                FROM wp_posts p
                JOIN wp_term_relationships tr ON p.ID = tr.object_id
                JOIN wp_term_taxonomy tt ON tr.term_taxonomy_id = tt.term_taxonomy_id
                LEFT JOIN wp_wc_product_meta_lookup l ON l.product_id = p.ID
                WHERE tt.taxonomy='product_cat' AND tt.term_id=%s
                  AND p.post_type='product' AND p.post_status != 'auto-draft'
                ORDER BY p.ID DESC LIMIT %s OFFSET %s
            """, (cid, per_page, offset))
            products = [_product_row_summary(r) for r in cur.fetchall()]
    return _ok(products, pagination=_paginate(total, page, per_page))


# ---------------------------------------------------------------------------
# BRANDS
# ---------------------------------------------------------------------------

@data_api.get("/brands")
@require_api_key
def list_brands():
    with _db() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT t.term_id as id, t.name, t.slug, tt.count
                FROM wp_terms t
                JOIN wp_term_taxonomy tt ON t.term_id = tt.term_id
                WHERE tt.taxonomy = 'product_brand'
                ORDER BY t.name ASC
            """)
            brands = [dict(r) for r in cur.fetchall()]
    return _ok(brands)


@data_api.get("/brands/<int:bid>")
@require_api_key
def get_brand(bid):
    with _db() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT t.term_id as id, t.name, t.slug, tt.count
                FROM wp_terms t
                JOIN wp_term_taxonomy tt ON t.term_id = tt.term_id
                WHERE tt.taxonomy='product_brand' AND t.term_id=%s
            """, (bid,))
            brand = cur.fetchone()
            if not brand:
                return _err("not_found", f"Brand {bid} not found.", 404)
            brand = dict(brand)
            cur.execute("SELECT meta_key, meta_value FROM wp_termmeta WHERE term_id=%s", (bid,))
            brand["meta"] = {r["meta_key"]: r["meta_value"] for r in cur.fetchall()}
    return _ok(brand)


@data_api.get("/brands/<int:bid>/products")
@require_api_key
def brand_products(bid):
    page, per_page = _page_params()
    offset = (page - 1) * per_page
    with _db() as conn:
        with conn.cursor() as cur:
            cur.execute("""SELECT t.term_id FROM wp_terms t JOIN wp_term_taxonomy tt ON t.term_id=tt.term_id
                WHERE tt.taxonomy='product_brand' AND t.term_id=%s""", (bid,))
            if not cur.fetchone():
                return _err("not_found", f"Brand {bid} not found.", 404)
            cur.execute("""SELECT COUNT(*) as cnt
                FROM wp_posts p
                JOIN wp_term_relationships tr ON p.ID = tr.object_id
                JOIN wp_term_taxonomy tt ON tr.term_taxonomy_id = tt.term_taxonomy_id
                WHERE tt.taxonomy='product_brand' AND tt.term_id=%s
                  AND p.post_type='product' AND p.post_status != 'auto-draft'""", (bid,))
            total = cur.fetchone()["cnt"]
            cur.execute("""
                SELECT p.ID, p.post_title, p.post_name, p.post_status,
                       p.post_date, p.post_modified,
                       l.sku, l.min_price, l.stock_quantity, l.stock_status, l.onsale
                FROM wp_posts p
                JOIN wp_term_relationships tr ON p.ID = tr.object_id
                JOIN wp_term_taxonomy tt ON tr.term_taxonomy_id = tt.term_taxonomy_id
                LEFT JOIN wp_wc_product_meta_lookup l ON l.product_id = p.ID
                WHERE tt.taxonomy='product_brand' AND tt.term_id=%s
                  AND p.post_type='product' AND p.post_status != 'auto-draft'
                ORDER BY p.ID DESC LIMIT %s OFFSET %s
            """, (bid, per_page, offset))
            products = [_product_row_summary(r) for r in cur.fetchall()]
    return _ok(products, pagination=_paginate(total, page, per_page))


# ---------------------------------------------------------------------------
# ORDERS
# ---------------------------------------------------------------------------

_UNPAID_STATUSES = ("wc-pending", "wc-failed", "wc-on-hold")
_UNPAID_REASON = {
    "wc-pending":  "pending_payment",
    "wc-failed":   "payment_failed",
    "wc-on-hold":  "on_hold",
}


def _resolve_src(bs_id: str | None, cv: str) -> tuple[str, str, str | None, str | None]:
    """Return (order_source, source_channel, external_order_id, external_marketplace)."""
    if bs_id:
        return "basalam", "basalam", bs_id, "basalam"
    if cv == "checkout":
        return "website", "woocommerce_checkout", None, None
    if cv == "admin":
        return "manual", "admin", None, None
    return "unknown", cv or None, None, None


def _batch_src(cur, order_ids: list) -> tuple[dict, dict, dict]:
    """Batch-fetch Basalam hash IDs, created_via, and billing phones for a list of order IDs.
    Returns (src_map, op_map, phone_map).
    """
    if not order_ids:
        return {}, {}, {}
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
    return src_map, op_map, phone_map


@data_api.get("/orders")
@require_api_key
def list_orders():
    """List orders with filtering, pagination, and PII-masked contact fields.

    Filters: status, order_source, source_channel, external_marketplace,
             customer_id, date_from (alias: date_after), date_to (alias: date_before),
             has_phone (0|1), has_email (0|1)
    """
    page, per_page = _page_params()
    offset = (page - 1) * per_page

    status               = request.args.get("status")
    order_source         = request.args.get("order_source") or request.args.get("source")
    source_channel       = request.args.get("source_channel")
    external_marketplace = request.args.get("external_marketplace")
    customer_id          = request.args.get("customer_id", type=int)
    date_from            = request.args.get("date_from") or request.args.get("date_after")
    date_to              = request.args.get("date_to") or request.args.get("date_before")
    has_phone_arg        = request.args.get("has_phone")
    has_email_arg        = request.args.get("has_email")

    where:  list[str] = ["o.type = 'shop_order'"]
    params: list      = []

    if status:
        s = status if status.startswith("wc-") else f"wc-{status}"
        where.append("o.status = %s")
        params.append(s)
    if customer_id:
        where.append("o.customer_id = %s")
        params.append(customer_id)
    if date_from:
        where.append("o.date_created_gmt >= %s")
        params.append(date_from)
    if date_to:
        where.append("o.date_created_gmt <= %s")
        params.append(date_to)

    # Source/channel filters (subquery-based; fast enough for dev dataset)
    _bs_sub = "SELECT order_id FROM wp_wc_orders_meta WHERE meta_key='_sync_basalam_hash_id'"
    _op_cv  = "SELECT order_id FROM wp_wc_order_operational_data WHERE created_via=%s"
    if order_source == "basalam" or external_marketplace == "basalam":
        where.append(f"o.id IN ({_bs_sub})")
    elif order_source == "website":
        where.append(f"o.id NOT IN ({_bs_sub})")
        where.append(f"o.id IN ({_op_cv})")
        params.append("checkout")
    elif order_source == "manual":
        where.append(f"o.id NOT IN ({_bs_sub})")
        where.append(f"o.id IN ({_op_cv})")
        params.append("admin")
    elif order_source == "unknown":
        where.append(f"o.id NOT IN ({_bs_sub})")
        where.append(f"o.id NOT IN (SELECT order_id FROM wp_wc_order_operational_data WHERE created_via IN ('checkout','admin'))")

    if source_channel and not order_source:
        if source_channel == "basalam":
            where.append(f"o.id IN ({_bs_sub})")
        elif source_channel == "woocommerce_checkout":
            where.append(f"o.id IN ({_op_cv})")
            params.append("checkout")
        elif source_channel == "admin":
            where.append(f"o.id IN ({_op_cv})")
            params.append("admin")

    if has_email_arg == "1":
        where.append("o.billing_email IS NOT NULL AND o.billing_email != ''")
    elif has_email_arg == "0":
        where.append("(o.billing_email IS NULL OR o.billing_email = '')")

    if has_phone_arg == "1":
        where.append(
            "EXISTS (SELECT 1 FROM wp_wc_order_addresses pa"
            " WHERE pa.order_id=o.id AND pa.address_type='billing'"
            " AND pa.phone IS NOT NULL AND pa.phone != '')"
        )
    elif has_phone_arg == "0":
        where.append(
            "NOT EXISTS (SELECT 1 FROM wp_wc_order_addresses pa"
            " WHERE pa.order_id=o.id AND pa.address_type='billing'"
            " AND pa.phone IS NOT NULL AND pa.phone != '')"
        )

    where_sql = " AND ".join(where)

    with _db() as conn:
        with conn.cursor() as cur:
            cur.execute(f"SELECT COUNT(*) as cnt FROM wp_wc_orders o WHERE {where_sql}", params)
            total = cur.fetchone()["cnt"]
            cur.execute(
                f"""SELECT o.id, o.status, o.currency, o.total_amount, o.tax_amount,
                           o.customer_id, o.billing_email,
                           o.date_created_gmt, o.date_updated_gmt,
                           o.payment_method, o.payment_method_title, o.transaction_id
                    FROM wp_wc_orders o
                    WHERE {where_sql}
                    ORDER BY o.id DESC LIMIT %s OFFSET %s""",
                params + [per_page, offset],
            )
            rows      = cur.fetchall()
            order_ids = [r["id"] for r in rows]
            src_map, op_map, phone_map = _batch_src(cur, order_ids)

            orders = []
            for r in rows:
                st    = r["status"]
                oid   = r["id"]
                email = r["billing_email"] or None
                phone = phone_map.get(oid) or None
                bs_id = src_map.get(oid)
                cv    = op_map.get(oid, "")
                order_source_v, src_ch, ext_id, ext_mkt = _resolve_src(bs_id, cv)
                orders.append({
                    "id":                   oid,
                    "status":               st[3:] if st.startswith("wc-") else st,
                    "currency":             r["currency"],
                    "total":                _float(r["total_amount"]),
                    "tax_total":            _float(r["tax_amount"]),
                    "customer_id":          _int(r["customer_id"]),
                    "billing_email":        email,
                    "email_masked":         _mask_email(email),
                    "has_email":            bool(email),
                    "phone_masked":         _mask_phone(phone),
                    "has_phone":            bool(phone),
                    "date_created":         _dt(r["date_created_gmt"]),
                    "date_modified":        _dt(r["date_updated_gmt"]),
                    "payment_method":       r["payment_method"],
                    "payment_method_title": r["payment_method_title"],
                    "transaction_id":       r["transaction_id"],
                    "order_source":         order_source_v,
                    "source_channel":       src_ch,
                    "external_order_id":    ext_id,
                    "external_marketplace": ext_mkt,
                    "created_via":          cv or None,
                })
    return _ok(orders, pagination=_paginate(total, page, per_page))


@data_api.get("/orders/unpaid")
@require_api_key
def list_orders_unpaid():
    """Recovery candidates: pending, failed, and on-hold orders.

    Returns PII-masked contact fields only. Includes age_minutes and recovery reason.
    Filters: order_source, external_marketplace, date_from, date_to
    """
    page, per_page = _page_params()
    offset = (page - 1) * per_page

    order_source         = request.args.get("order_source") or request.args.get("source")
    external_marketplace = request.args.get("external_marketplace")
    date_from            = request.args.get("date_from") or request.args.get("date_after")
    date_to              = request.args.get("date_to") or request.args.get("date_before")

    where:  list[str] = ["o.type = 'shop_order'", "o.status IN ('wc-pending','wc-failed','wc-on-hold')"]
    params: list      = []

    if date_from:
        where.append("o.date_created_gmt >= %s")
        params.append(date_from)
    if date_to:
        where.append("o.date_created_gmt <= %s")
        params.append(date_to)

    _bs_sub = "SELECT order_id FROM wp_wc_orders_meta WHERE meta_key='_sync_basalam_hash_id'"
    if order_source == "basalam" or external_marketplace == "basalam":
        where.append(f"o.id IN ({_bs_sub})")
    elif order_source and order_source != "basalam":
        where.append(f"o.id NOT IN ({_bs_sub})")

    where_sql = " AND ".join(where)

    with _db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"SELECT COUNT(*) as cnt FROM wp_wc_orders o WHERE {where_sql}", params
            )
            total = cur.fetchone()["cnt"]
            cur.execute(
                f"""SELECT o.id, o.status, o.currency, o.total_amount, o.customer_id,
                           o.billing_email, o.date_created_gmt, o.payment_method
                    FROM wp_wc_orders o
                    WHERE {where_sql}
                    ORDER BY o.date_created_gmt ASC
                    LIMIT %s OFFSET %s""",
                params + [per_page, offset],
            )
            rows      = cur.fetchall()
            order_ids = [r["id"] for r in rows]
            src_map, op_map, phone_map = _batch_src(cur, order_ids)

            now_utc = datetime.now(timezone.utc)
            orders = []
            for r in rows:
                st    = r["status"]
                oid   = r["id"]
                email = r["billing_email"] or None
                phone = phone_map.get(oid) or None
                bs_id = src_map.get(oid)
                cv    = op_map.get(oid, "")
                order_source_v, src_ch, ext_id, ext_mkt = _resolve_src(bs_id, cv)

                # Age calculation
                created = r["date_created_gmt"]
                age_minutes: int | None = None
                if created:
                    try:
                        if isinstance(created, datetime):
                            delta = now_utc - created.replace(tzinfo=timezone.utc)
                        else:
                            dt = datetime.fromisoformat(str(created))
                            delta = now_utc - dt.replace(tzinfo=timezone.utc)
                        age_minutes = int(delta.total_seconds() / 60)
                    except Exception:
                        age_minutes = None

                st_clean = st[3:] if st.startswith("wc-") else st
                orders.append({
                    "id":                   oid,
                    "status":               st_clean,
                    "currency":             r["currency"],
                    "total":                _float(r["total_amount"]),
                    "customer_id":          _int(r["customer_id"]),
                    "email_masked":         _mask_email(email),
                    "has_email":            bool(email),
                    "phone_masked":         _mask_phone(phone),
                    "has_phone":            bool(phone),
                    "date_created":         _dt(r["date_created_gmt"]),
                    "payment_method":       r["payment_method"],
                    "order_source":         order_source_v,
                    "source_channel":       src_ch,
                    "external_order_id":    ext_id,
                    "external_marketplace": ext_mkt,
                    "age_minutes":          age_minutes,
                    "age_hours":            round(age_minutes / 60, 1) if age_minutes is not None else None,
                    "recovery_reason":      _UNPAID_REASON.get(st, "unpaid"),
                })
    return _ok(orders, pagination=_paginate(total, page, per_page))


@data_api.get("/analytics/orders-summary")
@require_api_key
def orders_summary():
    """Order count/total summary by status and source.

    Filters: date_from, date_to (applied to date_created_gmt)
    """
    date_from = request.args.get("date_from") or request.args.get("date_after")
    date_to   = request.args.get("date_to") or request.args.get("date_before")

    date_where  = ""
    date_params: list = []
    if date_from:
        date_where += " AND date_created_gmt >= %s"
        date_params.append(date_from)
    if date_to:
        date_where += " AND date_created_gmt <= %s"
        date_params.append(date_to)

    with _db() as conn:
        with conn.cursor() as cur:
            # Total orders
            cur.execute(
                f"SELECT COUNT(*) as cnt, SUM(total_amount) as total_sales"
                f" FROM wp_wc_orders WHERE type='shop_order'{date_where}",
                date_params,
            )
            totals = cur.fetchone()

            # Count by status
            cur.execute(
                f"SELECT status, COUNT(*) as cnt FROM wp_wc_orders"
                f" WHERE type='shop_order'{date_where} GROUP BY status ORDER BY cnt DESC",
                date_params,
            )
            by_status = [
                {"status": (r["status"][3:] if r["status"].startswith("wc-") else r["status"]),
                 "raw_status": r["status"], "count": r["cnt"]}
                for r in cur.fetchall()
            ]

            # Unpaid / recovery candidates
            cur.execute(
                f"SELECT COUNT(*) as cnt FROM wp_wc_orders"
                f" WHERE type='shop_order' AND status IN ('wc-pending','wc-failed','wc-on-hold'){date_where}",
                date_params,
            )
            unpaid_count = cur.fetchone()["cnt"]

            # Basalam orders
            bs_ids_query = (
                f"SELECT DISTINCT order_id FROM wp_wc_orders_meta WHERE meta_key='_sync_basalam_hash_id'"
            )
            cur.execute(
                f"SELECT COUNT(*) as cnt, SUM(o.total_amount) as total_sales"
                f" FROM wp_wc_orders o WHERE o.type='shop_order'"
                f" AND o.id IN ({bs_ids_query}){date_where}",
                date_params,
            )
            bs_row = cur.fetchone()
            basalam_count = bs_row["cnt"]
            basalam_sales = _float(bs_row["total_sales"])

            # Website (checkout) orders
            cur.execute(
                f"SELECT COUNT(*) as cnt, SUM(o.total_amount) as total_sales"
                f" FROM wp_wc_orders o"
                f" JOIN wp_wc_order_operational_data op ON op.order_id=o.id"
                f" WHERE o.type='shop_order' AND op.created_via='checkout'"
                f" AND o.id NOT IN ({bs_ids_query}){date_where}",
                date_params,
            )
            web_row = cur.fetchone()
            website_count = web_row["cnt"]
            website_sales = _float(web_row["total_sales"])

            # Manual (admin) orders
            cur.execute(
                f"SELECT COUNT(*) as cnt, SUM(o.total_amount) as total_sales"
                f" FROM wp_wc_orders o"
                f" JOIN wp_wc_order_operational_data op ON op.order_id=o.id"
                f" WHERE o.type='shop_order' AND op.created_via='admin'"
                f" AND o.id NOT IN ({bs_ids_query}){date_where}",
                date_params,
            )
            adm_row = cur.fetchone()
            manual_count = adm_row["cnt"]
            manual_sales = _float(adm_row["total_sales"])

            # Contact availability
            cur.execute(
                f"SELECT"
                f" SUM(CASE WHEN o.billing_email IS NOT NULL AND o.billing_email != '' THEN 1 ELSE 0 END) as has_email,"
                f" COUNT(*) as total"
                f" FROM wp_wc_orders o WHERE o.type='shop_order'{date_where}",
                date_params,
            )
            contact_row = cur.fetchone()
            has_email_count = contact_row["has_email"] or 0

            cur.execute(
                f"SELECT COUNT(DISTINCT a.order_id) as has_phone"
                f" FROM wp_wc_order_addresses a"
                f" JOIN wp_wc_orders o ON o.id=a.order_id"
                f" WHERE o.type='shop_order' AND a.address_type='billing'"
                f" AND a.phone IS NOT NULL AND a.phone != ''"
                f"{date_where.replace('date_created_gmt', 'o.date_created_gmt')}",
                date_params,
            )
            has_phone_count = cur.fetchone()["has_phone"] or 0

    total_count = int(totals["cnt"] or 0)
    total_sales = _float(totals["total_sales"])
    unknown_count = total_count - int(basalam_count) - int(website_count) - int(manual_count)

    return _ok({
        "total_orders":   total_count,
        "total_sales":    total_sales,
        "unpaid_count":   unpaid_count,
        "by_status":      by_status,
        "by_source": {
            "basalam":  {"count": int(basalam_count), "total_sales": basalam_sales},
            "website":  {"count": int(website_count), "total_sales": website_sales},
            "manual":   {"count": int(manual_count),  "total_sales": manual_sales},
            "unknown":  {"count": max(0, unknown_count), "total_sales": None},
        },
        "contact_availability": {
            "has_email": int(has_email_count),
            "has_phone": int(has_phone_count),
            "total":     total_count,
        },
        "filters_applied": {
            "date_from": date_from,
            "date_to":   date_to,
        },
    })


@data_api.get("/orders/<int:oid>")
@require_api_key
def get_order(oid):
    with _db() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM wp_wc_orders WHERE id=%s AND type='shop_order'", (oid,))
            order = cur.fetchone()
            if not order:
                return _err("not_found", f"Order {oid} not found.", 404)
            meta      = _order_meta(cur, oid)
            items     = _order_items(cur, oid)
            addresses = _order_addresses(cur, oid)
            op_data   = _order_operational_data(cur, oid)
    return _ok(_build_order(order, meta, items, addresses, op_data))


@data_api.get("/orders/<int:oid>/items")
@require_api_key
def get_order_items(oid):
    with _db() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT id FROM wp_wc_orders WHERE id=%s AND type='shop_order'", (oid,))
            if not cur.fetchone():
                return _err("not_found", f"Order {oid} not found.", 404)
            items = _order_items(cur, oid)
    return _ok(items)


# ---------------------------------------------------------------------------
# SYNC SUPPORT
# ---------------------------------------------------------------------------

@data_api.get("/sync/status")
@require_api_key
def sync_status():
    state_path = pathlib.Path(__file__).parent / "event_sync_state.json"
    event_state: dict = {}
    if state_path.exists():
        try:
            event_state = json.loads(state_path.read_text())
        except Exception:
            pass

    with _db() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) as cnt FROM wp_posts WHERE post_type='product' AND post_status NOT IN ('auto-draft','trash')")
            product_count = cur.fetchone()["cnt"]
            cur.execute("SELECT COUNT(*) as cnt FROM wp_wc_orders WHERE type='shop_order'")
            order_count = cur.fetchone()["cnt"]
            cur.execute("SELECT MAX(post_modified) as last FROM wp_posts WHERE post_type='product' AND post_status != 'auto-draft'")
            last_product = _dt(cur.fetchone()["last"])
            cur.execute("SELECT MAX(date_updated_gmt) as last FROM wp_wc_orders WHERE type='shop_order'")
            last_order = _dt(cur.fetchone()["last"])

    return _ok({
        "api_version": API_VERSION,
        "event_sync_cursor": event_state.get("after_id", 0),
        "product_count": product_count,
        "order_count": order_count,
        "last_product_modified": last_product,
        "last_order_modified": last_order,
    })


@data_api.get("/sync/changed/products")
@require_api_key
def changed_products():
    since = request.args.get("since")
    if not since:
        return _err("missing_param", "Required query parameter: since (ISO datetime, e.g. 2026-06-01T00:00:00)", 400)
    page, per_page = _page_params()
    offset = (page - 1) * per_page
    with _db() as conn:
        with conn.cursor() as cur:
            cur.execute("""SELECT COUNT(*) as cnt FROM wp_posts
                WHERE post_type='product' AND post_status != 'auto-draft' AND post_modified >= %s""", (since,))
            total = cur.fetchone()["cnt"]
            cur.execute("""SELECT ID, post_title, post_name, post_status, post_date, post_modified
                FROM wp_posts
                WHERE post_type='product' AND post_status != 'auto-draft' AND post_modified >= %s
                ORDER BY post_modified ASC LIMIT %s OFFSET %s""", (since, per_page, offset))
            rows = [{"id": r["ID"], "name": r["post_title"], "slug": r["post_name"],
                     "status": r["post_status"],
                     "date_created": _dt(r["post_date"]), "date_modified": _dt(r["post_modified"])}
                    for r in cur.fetchall()]
    return _ok(rows, pagination=_paginate(total, page, per_page))


@data_api.get("/sync/changed/orders")
@require_api_key
def changed_orders():
    since = request.args.get("since")
    if not since:
        return _err("missing_param", "Required query parameter: since (ISO datetime, e.g. 2026-06-01T00:00:00)", 400)
    page, per_page = _page_params()
    offset = (page - 1) * per_page
    with _db() as conn:
        with conn.cursor() as cur:
            cur.execute("""SELECT COUNT(*) as cnt FROM wp_wc_orders
                WHERE type='shop_order' AND date_updated_gmt >= %s""", (since,))
            total = cur.fetchone()["cnt"]
            cur.execute("""SELECT id, status, currency, total_amount, customer_id,
                       date_created_gmt, date_updated_gmt
                FROM wp_wc_orders WHERE type='shop_order' AND date_updated_gmt >= %s
                ORDER BY date_updated_gmt ASC LIMIT %s OFFSET %s""", (since, per_page, offset))
            rows = [{"id": r["id"],
                     "status": r["status"][3:] if r["status"].startswith("wc-") else r["status"],
                     "currency": r["currency"], "total": _float(r["total_amount"]),
                     "customer_id": _int(r["customer_id"]),
                     "date_created": _dt(r["date_created_gmt"]),
                     "date_modified": _dt(r["date_updated_gmt"])}
                    for r in cur.fetchall()]
    return _ok(rows, pagination=_paginate(total, page, per_page))


# ---------------------------------------------------------------------------
# HEALTH
# ---------------------------------------------------------------------------

@data_api.get("/mapping/basalam/<int:basalam_product_id>")
@require_api_key
def mapping_basalam_single(basalam_product_id: int):
    """Return the WooCommerce product ID for a given Basalam product ID."""
    vendor_id = request.args.get("vendor_id", "1399163")
    meta_key = f"sync_basalam_product_id_{vendor_id}"
    try:
        with _db() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """SELECT p.ID
                       FROM wp_posts p
                       INNER JOIN wp_postmeta pm ON p.ID = pm.post_id
                       WHERE pm.meta_key = %s AND pm.meta_value = %s
                         AND p.post_status = 'publish'
                       LIMIT 1""",
                    (meta_key, str(basalam_product_id)),
                )
                row = cur.fetchone()
        if not row:
            return _err("not_found", "No mapping found for this Basalam product ID", 404)
        return _ok({"basalam_product_id": basalam_product_id, "wc_product_id": row["ID"]})
    except Exception as e:
        return _err("db_error", str(e), 500)


@data_api.get("/mapping/basalam")
@require_api_key
def mapping_basalam_all():
    """Return all Basalam → WooCommerce product ID mappings."""
    vendor_id = request.args.get("vendor_id", "1399163")
    meta_key = f"sync_basalam_product_id_{vendor_id}"
    try:
        with _db() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """SELECT pm.meta_value AS basalam_product_id, p.ID AS wc_product_id
                       FROM wp_posts p
                       INNER JOIN wp_postmeta pm ON p.ID = pm.post_id
                       WHERE pm.meta_key = %s AND pm.meta_value != ''
                         AND p.post_status = 'publish'
                       ORDER BY p.ID""",
                    (meta_key,),
                )
                rows = cur.fetchall()
        mappings = [
            {"basalam_product_id": int(r["basalam_product_id"]), "wc_product_id": r["wc_product_id"]}
            for r in rows
        ]
        return _ok({"vendor_id": vendor_id, "count": len(mappings), "mappings": mappings})
    except Exception as e:
        return _err("db_error", str(e), 500)


@data_api.get("/health")
@require_api_key
def api_health():
    db_ok = False
    db_error = None
    product_count = 0
    order_count = 0
    try:
        with _db() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT COUNT(*) as cnt FROM wp_posts WHERE post_type='product' AND post_status NOT IN ('auto-draft','trash')")
                product_count = cur.fetchone()["cnt"]
                cur.execute("SELECT COUNT(*) as cnt FROM wp_wc_orders WHERE type='shop_order'")
                order_count = cur.fetchone()["cnt"]
                db_ok = True
    except Exception as exc:
        db_error = str(exc)

    return _ok({
        "api_version": API_VERSION,
        "mirror_db": "ok" if db_ok else "error",
        "mirror_db_error": db_error,
        "product_count": product_count,
        "order_count": order_count,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    })


# ---------------------------------------------------------------------------
# Shared list-view helper (used by category/brand product sub-endpoints)
# ---------------------------------------------------------------------------

def _product_row_summary(r: dict) -> dict:
    return {
        "id": r["ID"],
        "name": r["post_title"],
        "slug": r["post_name"],
        "status": r["post_status"],
        "sku": r["sku"],
        "price": _float(r["min_price"]),
        "on_sale": bool(r["onsale"]),
        "stock_quantity": _float(r["stock_quantity"]),
        "stock_status": r["stock_status"],
        "date_created": _dt(r["post_date"]),
        "date_modified": _dt(r["post_modified"]),
    }

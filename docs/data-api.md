# Behdashtik Data Hub — REST API Reference

Read-only REST API served by the hub dashboard (`hub.behdashtik.ir`).  
All data is read from the MySQL mirror DB (`behdashtik_wp_mirror`) — never from live WordPress.

**Base URL:** `https://hub.behdashtik.ir/api/v1`  
**API Version:** 1.0.0

---

## Authentication

Every request must include a single API key in the request header:

```
X-Hub-API-Key: <your-key>
```

The key is configured in `server2/config.json` → `data_api.key` or the environment variable `BDSK_DATA_API_KEY`.

If the key is missing or invalid, every endpoint returns:

```json
HTTP 401
{ "error": "unauthorized", "message": "Invalid or missing X-Hub-API-Key header." }
```

If the key is not configured on the server at all:

```json
HTTP 503
{ "error": "not_configured", "message": "Data API key is not configured on the server." }
```

**Generate a key:**
```bash
python3 -c "import secrets; print(secrets.token_hex(32))"
```

---

## Response format

All successful responses follow this envelope:

```json
{
  "data": <object or array>,
  "pagination": { "page": 1, "per_page": 20, "total": 150, "pages": 8 }
}
```

`pagination` is only present on list endpoints. Detail endpoints return just `"data": <object>`.

All error responses:
```json
{ "error": "<code>", "message": "<human-readable description>" }
```

---

## Pagination

All list endpoints accept:

| Parameter | Default | Max | Description |
|---|---|---|---|
| `page` | `1` | — | Page number |
| `per_page` | `20` | `100` | Results per page |

---

## Endpoints

### Health & Status

#### `GET /api/v1/health`

API availability check. Returns mirror DB connectivity and record counts.

**Example request:**
```bash
curl -H "X-Hub-API-Key: <key>" https://hub.behdashtik.ir/api/v1/health
```

**Example response:**
```json
{
  "data": {
    "api_version": "1.0.0",
    "mirror_db": "ok",
    "mirror_db_error": null,
    "product_count": 361,
    "order_count": 975,
    "timestamp": "2026-06-16T21:39:29.241048+00:00"
  }
}
```

---

### Products

#### `GET /api/v1/products`

List products (summary view). Supports filtering, search, and sorting.

**Query parameters:**

| Parameter | Type | Description |
|---|---|---|
| `status` | string | Filter by post status: `publish`, `draft`, `private` |
| `stock_status` | string | Filter by stock: `instock`, `outofstock`, `onbackorder` |
| `category_id` | integer | Filter by category term ID |
| `brand_id` | integer | Filter by brand term ID |
| `search` | string | Search by name, SKU, or barcode (global_unique_id) |
| `modified_since` | datetime | Only products modified at or after this ISO datetime |
| `sort_by` | string | `id` (default), `date`, `modified`, `name`, `price`, `sales` |
| `sort_dir` | string | `desc` (default), `asc` |

**Example:**
```bash
curl -H "X-Hub-API-Key: <key>" \
  "https://hub.behdashtik.ir/api/v1/products?status=publish&stock_status=instock&per_page=10"
```

**Response fields (per product):**

```json
{
  "id": 5968,
  "name": "لوسیون بدن وازلین",
  "slug": "vaseline-advanced-repair-200ml",
  "status": "draft",
  "sku": "VSL-001",
  "global_unique_id": "8712561478762",
  "price": 295000.0,
  "max_price": 295000.0,
  "on_sale": false,
  "stock_quantity": 5.0,
  "stock_status": "instock",
  "average_rating": 4.5,
  "total_sales": 12,
  "date_created": "2026-06-15T13:05:36",
  "date_modified": "2026-06-16T15:17:36",
  "categories": [{ "id": 48, "name": "محصولات مراقبت از پوست", "slug": "skin-care" }],
  "brands": [{ "id": 223, "name": "Vaseline", "slug": "vaseline" }]
}
```

---

#### `GET /api/v1/products/{id}`

Full product detail including description, images, SEO fields, and variations.

**Example:**
```bash
curl -H "X-Hub-API-Key: <key>" https://hub.behdashtik.ir/api/v1/products/5968
```

**Additional fields vs list view:**

| Field | Description |
|---|---|
| `description` | Full HTML product description (post_content) |
| `short_description` | Short description (post_excerpt) |
| `manage_stock` | Whether stock is managed |
| `backorders` | Backorder setting: `no`, `notify`, `yes` |
| `virtual` / `downloadable` | Boolean flags |
| `tax_status` / `tax_class` | Tax configuration |
| `review_count` | Number of reviews |
| `tags` | Product tags `[{id, name, slug}]` |
| `images` | Array of image objects (see below) |
| `seo` | SEO fields object (see below) |
| `raw_attributes` | PHP-serialized `_product_attributes` string |
| `variations` | Array of variation objects (see `/products/{id}/variations`) |

**Image object:**
```json
{
  "id": 5995,
  "title": "Product photo",
  "url": "https://dev.behdashtik.ir/wp-content/uploads/2026/06/photo.webp",
  "local_path": "/root/wordpress-data-hub/data/media/2026/06/photo.webp",
  "file": "2026/06/photo.webp",
  "position": 0,
  "is_thumbnail": true
}
```
`local_path` is `null` if the media sync hasn't downloaded the file yet.

**SEO object:**
```json
{
  "title": "خرید لوسیون وازلین",
  "description": "لوسیون بدن وازلین...",
  "focus_keyword": "لوسیون وازلین, لوسیون بدن",
  "score": 87
}
```

---

#### `GET /api/v1/products/{id}/variations`

All variations of a variable product.

**Response (array):**
```json
[
  {
    "id": 5969,
    "parent_id": 5968,
    "status": "publish",
    "sku": "VSL-001-200",
    "price": 295000.0,
    "regular_price": 295000.0,
    "sale_price": null,
    "on_sale": false,
    "stock_quantity": 5.0,
    "stock_status": "instock",
    "manage_stock": true,
    "virtual": false,
    "downloadable": false,
    "date_created": "2026-06-15T13:05:36",
    "date_modified": "2026-06-16T15:17:36",
    "raw_attributes": "<php-serialized string>"
  }
]
```

Returns `[]` for simple products with no variations.

---

#### `GET /api/v1/products/{id}/images`

Product images only (thumbnail + gallery), without loading the full product detail.

---

### Categories

#### `GET /api/v1/categories`

Flat list of all product categories.

**Response fields:** `id`, `name`, `slug`, `parent` (parent term ID, `0` = root), `count`.

---

#### `GET /api/v1/categories/tree`

Categories as a nested hierarchy. Each category has a `children` array.

**Example:**
```json
[
  {
    "id": 44,
    "name": "محصولات آرایشی",
    "slug": "cosmetic-products",
    "parent": 0,
    "count": 153,
    "children": [
      { "id": 45, "name": "آرایش صورت", "slug": "face-makeup", "parent": 44, "count": 21, "children": [] },
      { "id": 46, "name": "آرایش چشم", "slug": "eye-makeup", "parent": 44, "count": 47, "children": [] }
    ]
  }
]
```

---

#### `GET /api/v1/categories/{id}`

Single category detail, including `ancestors` breadcrumb.

**Example response:**
```json
{
  "data": {
    "id": 46,
    "name": "آرایش چشم",
    "slug": "eye-makeup",
    "parent": 44,
    "count": 47,
    "ancestors": [
      { "id": 44, "name": "محصولات آرایشی", "slug": "cosmetic-products" }
    ]
  }
}
```

---

#### `GET /api/v1/categories/{id}/products`

Products in a given category (paginated, summary view). Supports `page` / `per_page`.

---

### Brands

#### `GET /api/v1/brands`

List all brands (`product_brand` taxonomy).

**Response fields:** `id`, `name`, `slug`, `count`.

---

#### `GET /api/v1/brands/{id}`

Single brand with term meta (description, image ID, etc. stored by the brand plugin).

```json
{
  "data": {
    "id": 223,
    "name": "Vaseline",
    "slug": "vaseline",
    "count": 8,
    "meta": {
      "thumbnail_id": "5880",
      "display_type": ""
    }
  }
}
```

---

#### `GET /api/v1/brands/{id}/products`

Products for a given brand (paginated, summary view).

---

### Orders

#### `GET /api/v1/orders`

List orders. Supports filtering.

**Query parameters:**

| Parameter | Type | Description |
|---|---|---|
| `status` | string | Order status: `processing`, `completed`, `cancelled`, etc. (with or without `wc-` prefix) |
| `customer_id` | integer | Filter by WordPress customer user ID |
| `date_after` | datetime | Orders created at or after (ISO datetime) |
| `date_before` | datetime | Orders created at or before (ISO datetime) |

**Example:**
```bash
curl -H "X-Hub-API-Key: <key>" \
  "https://hub.behdashtik.ir/api/v1/orders?status=processing&date_after=2026-06-01T00:00:00"
```

**Response fields (per order):**

```json
{
  "id": 6026,
  "status": "processing",
  "currency": "IRT",
  "total": 652400.0,
  "tax_total": 0.0,
  "customer_id": 0,
  "billing_email": "customer@example.com",
  "date_created": "2026-06-16T21:36:47",
  "date_modified": "2026-06-16T21:37:32",
  "payment_method": "WC_Zibal",
  "payment_method_title": "زیبال",
  "transaction_id": "4632007628"
}
```

---

#### `GET /api/v1/orders/{id}`

Full order detail with billing/shipping addresses, line items, shipping lines, and order meta.

**Additional fields vs list view:**

| Field | Description |
|---|---|
| `billing` | Billing address object |
| `shipping` | Shipping address object |
| `customer_note` | Note left by the customer |
| `parent_order_id` | Parent order ID for refunds/child orders |
| `line_items` | Product line items array |
| `shipping_lines` | Shipping method lines array |
| `fee_lines` | Fee lines array |
| `meta` | All other order meta (attribution, custom fields, etc.) |

**Billing/shipping object:**
```json
{
  "first_name": "علی",
  "last_name": "رضایی",
  "company": null,
  "address_1": "خیابان ولیعصر",
  "address_2": null,
  "city": "تهران",
  "state": "Tehran",
  "postcode": "1234567890",
  "country": "IR",
  "phone": "09121234567",
  "email": "customer@example.com"
}
```

**Line item object:**
```json
{
  "id": 1234,
  "name": "پالت سایه 12 رنگ",
  "type": "line_item",
  "product_id": 4768,
  "variation_id": null,
  "quantity": 2.0,
  "subtotal": 598000.0,
  "total": 598000.0,
  "subtotal_tax": 0.0,
  "total_tax": 0.0,
  "tax_class": null
}
```

---

#### `GET /api/v1/orders/{id}/items`

All items for an order (line items + shipping lines + fee lines) without loading the full order.

---

### Sync Support

#### `GET /api/v1/sync/status`

Current state of the mirror: record counts, last modification timestamps, and event sync cursor position.

```json
{
  "data": {
    "api_version": "1.0.0",
    "event_sync_cursor": 260,
    "product_count": 361,
    "order_count": 975,
    "last_product_modified": "2026-06-17T01:07:35",
    "last_order_modified": "2026-06-16T21:37:32"
  }
}
```

Use `last_product_modified` / `last_order_modified` to decide whether to pull changed records.

---

#### `GET /api/v1/sync/changed/products?since=<datetime>`

Products whose `post_modified` is ≥ `since`. Use this to detect what has changed since your last pull.

**Required parameter:** `since` — ISO datetime, e.g. `2026-06-16T00:00:00`

**Example:**
```bash
curl -H "X-Hub-API-Key: <key>" \
  "https://hub.behdashtik.ir/api/v1/sync/changed/products?since=2026-06-16T00:00:00"
```

**Response:** paginated list of `{id, name, slug, status, date_created, date_modified}`, ordered by `date_modified ASC`.

After receiving this list, fetch full details with `GET /products/{id}` for each changed ID.

---

#### `GET /api/v1/sync/changed/orders?since=<datetime>`

Orders whose `date_updated_gmt` is ≥ `since`.

Same pattern as `sync/changed/products`.

---

## Error codes

| HTTP | `error` code | Meaning |
|---|---|---|
| 400 | `missing_param` | Required query parameter not provided |
| 401 | `unauthorized` | API key missing or invalid |
| 404 | `not_found` | Requested resource does not exist in mirror |
| 503 | `not_configured` | Server-side API key not configured |

---

## Safety & limitations

- **Read-only.** No endpoint modifies any data. All DB connections use the `mirror_readonly` MySQL user.
- **Mirror lag.** Data may be up to ~60 seconds behind live WordPress (event sync cadence). Full re-exports happen nightly.
- **Zero dates.** Draft products may have `date_created: null` due to WordPress zero-date handling. Handle `null` dates in your client.
- **`raw_attributes`** is a PHP-serialized string. Parse it client-side if needed; no decoded form is provided by the API.
- **`local_path`** on images is the server filesystem path — only useful if your app runs on the same host. External consumers should use `url`.
- **Max per_page is 100.** Use pagination for large result sets.
- **No write, no auth mutations, no personal data creation.** This API exposes mirrored e-commerce data only.

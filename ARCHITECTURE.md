# Supermarket Price Comparison — Backend & Data Pipeline Architecture

Targets: **Coto · Carrefour · Chango Más · Sodimac** (Argentina)
Stack: **Python · Django 5 · PostgreSQL 14+ · Celery/Redis · curl_cffi + Playwright**

---

## 1. Data model at a glance

```
Category ──┐
           ▼
        Product  (GLOBAL, keyed by EAN)  ◄── universal matching key
           ▲
           │ (nullable FK; null = unmatched listing awaiting EAN)
        StoreProduct  (per-store listing; holds store SKU + denormalized snapshot)
           │  ├── current_price / current_oup / current_price_per_measure   ← HOT read path
           │  ├─< Promotion        (drives OUP)
           │  ├─< PriceHistory     (append-only, PARTITIONED, 90-day retention)
           │  └─< PriceAggregate   (monthly rollup = the "archive" beyond 90 days)
        Store
        ScrapeRun  (observability: per store+shard run, status, errors)
```

**Why `StoreProduct` exists (not in the brief, but essential):** the brief lists
`Store / Product / PriceHistory / Promotion`, but if `PriceHistory` and
`Promotion` hung directly off a global `Product`, we could not represent *the same
EAN priced differently in two stores*. `StoreProduct` is the bridge that holds the
store-local SKU/URL and the per-store price series, while `Product` stays global
and EAN-keyed. This is the core of "match on EAN, never on SKU".

**The money field is `current_oup`.** Comparison queries never compute promo math
at read time and never touch `PriceHistory`; they sort the denormalized
`StoreProduct.current_oup` snapshot, backed by a partial index.

---

## 2. Optimal Unit Price (OUP) — how non-linear promos are handled

`catalog/pricing.py` is a pure-Python engine (no Django import → unit-testable).
For each listing it simulates buying `1..N` units under every valid promotion and
returns the **lowest achievable per-unit price**.

| Promo type | Model fields | OUP math (shelf = S) |
|---|---|---|
| `nxm` (2x1, 3x2) | `get_quantity`, `pay_quantity` | buy `get`, pay `pay` → `S·pay/get` |
| `nth_unit_pct` (70% off 2nd) | `min_quantity`, `nth_unit`, `discount_percent` | block of 2 → `(S + S·0.30)/2 = 0.65S` |
| `percent_off` | `discount_percent` | `S·(1−p)` |
| `bulk_price` (3 for $X) | `min_quantity`, `bulk_total` | `X/min_quantity` |
| `bank` (−15% Banco X) | `discount_percent`, `bank`, `is_stackable` | **conditional** → separate `oup_with_bank` |

Two figures are produced and stored:
- **`current_oup`** — best *unconditional* promo (everyone gets it). Used for
  cross-store ranking (apples-to-apples).
- **`oup_with_bank`** — layered stackable bank/payment discounts (shopper-specific;
  surfaced separately, never mixed into the comparison sort).

`price_per_measure()` then normalizes OUP → ARS/L or ARS/kg so 900 ml vs 1 L is
comparable.

---

## 3. Scraping Strategy Matrix

| Store | Platform | Hidden JSON API? | Recommended tool | Anti-bot | EAN source | Cost |
|---|---|---|---|---|---|---|
| **Carrefour** | **VTEX** | ✅ `/api/catalog_system/pub/products/search` | `curl_cffi` (impersonate) | Cloudflare edge (TLS/JA3) → beaten by impersonation | `items[].ean` in catalog JSON | 💲 cheap |
| **Chango Más** | **VTEX** | ✅ same VTEX endpoints | `curl_cffi` (impersonate) | Light edge rate-limiting | `items[].ean` in catalog JSON | 💲 cheap |
| **Coto** | Legacy / custom (**not** VTEX) | ⚠️ likely, **discover live** | `curl_cffi`; BeautifulSoup fallback | Moderate WAF / rate-limit; warm cookie needed | product-detail feed (`eanPrincipal`) | 💲–💲💲 |
| **Sodimac** | Falabella **Next.js** SSR | ⚠️ Next.js `_next/data` / XHR, gated | **Playwright + stealth** | **Datadome** (JA3 + canvas + behavioral) | resolved on **PDP** pass | 💲💲💲 expensive |

### Per-target detail

**Carrefour & Chango Más — VTEX (the easy 50%).**
Every VTEX storefront exposes a public catalog API. Walk it by category:
```
GET {base}/api/catalog_system/pub/products/search?fq=C:/{categoryId}/&_from=0&_to=49
```
Returns full product JSON: `items[].ean` (✅ the GTIN we match on), `sellers[].
commertialOffer` (`Price`, `ListPrice`, `Teasers` → promotions), availability.
Constraints: 50 items/page, hard `_from/_to` window of 2500 → **shard by category**.
Newer stores also expose Intelligent Search (`/api/io/_v/api/intelligent-search/
product_search`). `curl_cffi` with `impersonate="chrome"` presents a real Chrome
TLS handshake so the Cloudflare edge doesn't JA3-block us — no browser needed.
→ implemented in [`scraping/vtex.py`](apps/scraping/vtex.py).

**Coto — legacy, hidden endpoint to confirm.**
Not VTEX. Its search/category pages fetch a JSON-ish results feed; **discover the
exact path + params + required cookies via DevTools ▸ Network once**, pin the
signature, then hit it directly with `curl_cffi` behind a warmed session cookie.
HTML/BeautifulSoup is the fallback if no clean JSON exists. Moderate WAF only — no
headless needed. → [`scraping/coto.py`](apps/scraping/coto.py) (endpoint is a
template to verify, not a hard-coded fact).

**Sodimac — the hard 25%.**
Next.js SSR behind **Datadome**. Plain HTTP → challenge page. Use **Playwright +
playwright-stealth** through **residential AR proxies**; drive to a category page
and **intercept the hydration JSON** (`_next/data` / catalog XHR) via a response
handler rather than scraping the DOM. Detect the Datadome interstitial and *raise*
so the orchestrator retries with a fresh proxy. EAN isn't in the category feed →
a throttled **PDP pass** resolves the GTIN before linking (never match on skuId).
→ [`scraping/sodimac.py`](apps/scraping/sodimac.py).

### Tooling rationale
- **`curl_cffi`** over `requests`/`httpx`: it impersonates a browser's TLS/JA3
  fingerprint, which defeats the cheapest tier of edge bot-detection at ~zero cost
  vs. a headless browser. This is what makes 75% of targets a plain HTTP job.
- **Playwright + stealth** reserved for Datadome-class fingerprinting (Sodimac).
- **Residential AR proxies**: prices are geo-localized; an AR exit IP is required
  for correct pricing *and* to look legitimate. Rotated per run via `scraper_config`.

---

## 4. Orchestration & Automation

```
Celery Beat (America/Argentina/Buenos_Aires)
  ├─ dispatch_frequent                  API/HTTP stores every 30 minutes
  └─ dispatch_headless                  browser stores every 6 hours
       └─ dispatch_store_scrape(store)  ask registry for scraper, list category shards
            └─ scrape_shard(store,shard) ← UNIT of parallelism / retry / rate-limit
                 └─ pipeline.persist_listing(raw)  idempotent upsert per listing
```

Design choices ([`scraping/tasks.py`](apps/scraping/tasks.py),
[`config/celery.py`](config/celery.py)):

- **Shard = one store + one category.** Bounded memory, natural concurrency, and
  a failing category retries *alone* while the rest of the store completes.
- **Retries: exponential backoff + jitter** (`autoretry_for`, `retry_backoff`,
  `retry_jitter=True`, `max_retries=4`). Jitter is critical against 429/Datadome so
  retries don't synchronize into a thundering herd. `acks_late=True` re-queues a
  shard if a worker dies mid-run.
- **Politeness:** per-task `rate_limit`, staggered store starts, `prefetch=1`.
- **Idempotency:** `persist_listing` upserts on `(store, store_sku)` and
  `(store_product, captured_at)` → a retried shard converges, never double-counts.
- **Observability:** every run writes a `ScrapeRun` (seen/written/errors/last_error)
  → alert on `written` dropping vs. trailing average = a broken selector/endpoint.
- **Freshness:** `mark_stale_out_of_stock` flags listings unseen for >36 h as OOS.

**"Lightweight but scalable":** the whole thing is Celery + Redis + Beat — a few
processes to start, but horizontally scalable by adding workers, and shard tasks
parallelize across them. If you want *lighter*, `Django-Q2` swaps in with the same
task shapes; Celery is the scalable default.

---

## 5. 90-Day Retention (archive → prune)

`PriceHistory` is the biggest table (≈ listings × days). It is **RANGE-partitioned
by month** on `captured_at` via `django-postgres-extra`
([`catalog/models.py`](apps/catalog/models.py) → `PostgresPartitionedModel`,
[`catalog/partitioning.py`](apps/catalog/partitioning.py)).

Daily task [`prune_price_history`](apps/catalog/management/commands/prune_price_history.py):
1. **Archive** — roll expiring days into monthly `PriceAggregate`
   (avg/min/max OUP + sample count) so long-horizon inflation charts survive.
2. **Prune** — `DROP TABLE` any partition whose entire range predates the 90-day
   cutoff. This is **metadata-only**: instant, and it leaves **zero dead tuples** —
   the decisive win over a bulk `DELETE`, which would bloat the table and force an
   expensive `VACUUM`. Partition bounds are read from `pg_catalog`, so we never drop
   a partition still holding in-window rows.

`manage.py pgpartition` (Beat-scheduled) keeps the current+next month partitions
pre-created so month-boundary writes never miss a partition.

---

## 6. Presentation layer — the three shopper views

A dependency-free SPA ([`web/templates/dashboard.html`](apps/web/templates/dashboard.html))
served by a `TemplateView`, talking to four plain-Django JSON endpoints
([`web/api.py`](apps/web/api.py)). No DRF, no front-end build step, no Chart.js —
the price chart is hand-drawn inline SVG.

| View | Endpoint | What it does |
|---|---|---|
| **Comparar** | `GET /api/products/<ean>/compare/` | every store's current offer, ranked by OUP; cheapest highlighted; promo + discount badges |
| **Histórico** | `GET /api/products/<ean>/history/?days=90` | price-over-time line per store + per-store inflation %; daily ≤90 d, monthly aggregates beyond |
| **Carrito** | `POST /api/cart/optimize/` | per-store basket totals **and** the optimal split (cheapest store per item); flags missing items |
| _typeahead_ | `GET /api/products/?q=` | search that powers all three |

**Cart correctness (the subtle part).** Line totals use the *quantity-aware*
[`line_item_cost`](apps/catalog/pricing.py), **not** `current_oup × quantity`.
OUP is the best price at the *optimal* quantity; multiplying it by an arbitrary
requested quantity silently misprices non-linear promos. Verified end-to-end:

- **2×1 at qty 3** → charges for 2 units, not `oup×3` (which would undercharge).
- **3×2 milk at qty 2** → correctly gives *no* discount (promo needs 3 units).
- **Optimal split** across 4 stores computed the true floor and the savings vs.
  the best single store; a home-improvement store (Sodimac) is correctly flagged
  as missing the grocery items rather than faking a total.

The dashboard ships with an **offline demo backend** (a faithful JS port of the
pricing engine + a small AR catalog) that activates only when the live API is
unreachable — so the page is fully interactive as a static preview and documents
the exact `/api/*` contract. Delete it (or gate behind `DEBUG`) in production.
Run the real thing with [`seed_demo`](apps/catalog/management/commands/seed_demo.py):
`migrate → pgpartition --yes → seed_demo → runserver`.

## 7. Query optimization summary

- **Denormalized snapshot** on `StoreProduct` → the hot "cheapest now" query is a
  single-table, index-only scan; it never joins `PriceHistory` or recomputes promos.
- **`DISTINCT ON (product_id) ORDER BY product_id, current_oup`**
  ([`catalog/managers.py`](apps/catalog/managers.py) `cheapest_per_product`) backed
  by the **partial index** `(product, current_oup) WHERE is_available` → one index
  scan for "cheapest offer per product", no window functions.
- **Partial indexes** everywhere the query is filtered (available listings, active
  promos, unmatched listings) → smaller, hotter indexes, lower write cost.
- **Partitioning** bounds index size and makes retention O(1).
- **`update_fields=[...]`** on every hot-path `.save()` → minimal UPDATE, less WAL.

---

## 8. Layout

```
config/
  celery.py               Celery app + Beat schedule
  urls.py                 root URLconf
  settings_snippet.py     the settings that matter (merge into settings.py)
apps/catalog/
  models.py               Store, Category, Product, StoreProduct, Promotion,
                          PriceHistory (partitioned), PriceAggregate, ScrapeRun
  pricing.py              OUP engine + line_item_cost (pure Python, self-tested)
  cart.py                 basket optimizer (per-store totals + optimal split)
  managers.py             optimized read QuerySets
  partitioning.py         psqlextra partition lifecycle
  migrations/0002_*.py    initial monthly partitions
  management/commands/prune_price_history.py
  management/commands/seed_demo.py
apps/scraping/
  base.py                 RawListing/RawPromo + BaseScraper contract
  vtex.py                 Carrefour + Chango Más (curl_cffi, JSON API)
  coto.py                 Coto (legacy hidden API + BS4 fallback)
  sodimac.py              Sodimac (Playwright + stealth, Datadome)
  pipeline.py             store-agnostic persist: match → promos → OUP → snapshot → history
  registry.py             Store → scraper resolution
  tasks.py                Celery orchestration (fan-out, retries, freshness)
apps/web/
  api.py                  JSON endpoints: search / compare / history / cart
  views.py + urls.py      dashboard SPA shell + routes
  templates/dashboard.html  compare + history chart + cart (dependency-free, offline demo)
```

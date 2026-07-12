# Supermarket Price Tracking

A Django 5 backend and small dashboard for comparing Argentine supermarket prices across stores such as Coto, Carrefour, Chango Mas, and Sodimac.

The app stores products by EAN, keeps per-store listings separately, computes promo-aware optimal unit prices, tracks price history, and exposes a dashboard plus JSON APIs for comparison, history, and cart optimization.

## What It Does

- Scrapes store listings through store-specific scrapers.
- Matches products globally by EAN, not by store SKU.
- Calculates the best unconditional unit price for promotions such as 2x1, 3x2, percent-off, and bulk offers.
- Stores current price snapshots for fast comparison queries.
- Stores daily price history in PostgreSQL monthly partitions.
- Refreshes lightweight HTTP/API sources every 30 minutes and headless sources
  every six hours while Celery worker and Beat are running.
- Excludes offers older than 48 hours from current comparisons by default.
- Serves a lightweight dashboard at `http://127.0.0.1:8000/`.

## Project Layout

```text
config/
  settings.py          Django settings
  urls.py              Root URL routing
  celery.py            Celery app and scheduled jobs

apps/catalog/
  models.py            Store, Product, StoreProduct, Promotion, PriceHistory
  pricing.py           Promo-aware optimal unit price engine
  cart.py              Basket optimization logic
  migrations/          Database schema and price-history partitions

apps/scraping/
  base.py              RawListing and scraper contract
  vtex.py              VTEX scraper for Carrefour / Chango Mas
  coto.py              Coto scraper skeleton
  sodimac.py           Playwright scraper for Sodimac
  pipeline.py          Idempotent persistence pipeline
  tasks.py             Celery orchestration

apps/web/
  api.py               JSON endpoints
  urls.py              Dashboard and API routes
  templates/           Dashboard template
```

## Requirements

- Python 3.11+
- PostgreSQL 14+
- Redis, only for Celery/background scraping
- Chromium via Playwright, only for browser-based scraping

The local virtual environment already exists in this workspace as `venv`.

## Quick Start

Activate the virtual environment:

```powershell
cd "C:\Proyectos\supermarket tracking"
.\venv\Scripts\activate
```

Create a PostgreSQL database:

```powershell
psql -U postgres
```

```sql
CREATE USER supermarket_app WITH PASSWORD 'supermarket_app_password';
CREATE DATABASE price_compare OWNER supermarket_app;
GRANT ALL PRIVILEGES ON DATABASE price_compare TO supermarket_app;
\q
```

Set Django database environment variables:

```powershell
$env:DB_NAME="price_compare"
$env:DB_USER="supermarket_app"
$env:DB_PASSWORD="supermarket_app_password"
$env:DB_HOST="localhost"
$env:DB_PORT="5432"
```

Create tables, seed demo data, and run the server:

```powershell
python manage.py migrate
python manage.py pgpartition --yes
python manage.py seed_demo
python manage.py runserver
```

Open:

```text
http://127.0.0.1:8000/
```

## Raspberry Pi / Docker

The repository includes an ARM64-friendly Compose stack with Django/Gunicorn,
PostgreSQL, Redis, a Celery worker, and Celery Beat. See
[`RASPBERRY_PI_DOCKER.md`](RASPBERRY_PI_DOCKER.md). It publishes port 8080 by
default so it can coexist with another Django container using port 8000.

## Useful Commands

Run Django checks:

```powershell
python manage.py check
```

Remove demonstration data and configure the two supported real storefronts:

```powershell
python manage.py clear_demo_catalog --yes
python manage.py bootstrap_vtex_stores
```

`bootstrap_vtex_stores` makes live HTTP requests to Carrefour and Chango Más to
discover their public VTEX category trees. It then enables their scheduled
scrapers. Coto and Sodimac are intentionally not enabled by this command.

Check whether migrations are up to date:

```powershell
python manage.py makemigrations --check --dry-run
```

Run the standalone pricing self-check:

```powershell
$env:PYTHONPATH="apps"
python apps\catalog\pricing.py
```

Run Celery worker and beat, after Redis is running:

```powershell
celery -A config.celery worker -l info
celery -A config.celery beat -l info
```

## API Endpoints

```text
GET  /api/products/?q=leche
GET  /api/products/<ean>/compare/
GET  /api/products/<ean>/history/?days=90
POST /api/cart/optimize/
```

Example cart request:

```powershell
curl -X POST "http://127.0.0.1:8000/api/cart/optimize/" `
  -H "Content-Type: application/json" `
  -d "{\"items\":[{\"ean\":\"7790895000123\",\"quantity\":2}]}"
```

## More Documentation

- `LOCAL_SETUP.md` has the focused local setup steps.
- `ARCHITECTURE.md` explains the full data model, scraping pipeline, pricing logic, and retention strategy.

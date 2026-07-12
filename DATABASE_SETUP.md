# Local database setup

## Recommendation

Use PostgreSQL for this project, including for local development.

MySQL is a good relational database, but it is not the simpler choice for this
codebase. The application already uses PostgreSQL-specific features:

- `psqlextra.backend` as the Django database engine;
- native range partitions for `PriceHistory`;
- partial indexes for available and unmatched listings;
- PostgreSQL `DISTINCT ON` in `cheapest_per_product()`;
- PostgreSQL catalog queries when old history partitions are pruned.

Changing to MySQL would require replacing those features and rewriting existing
migrations. SQLite is useful for a very small prototype, but it also cannot run
the current schema unchanged. Running one local PostgreSQL instance keeps local
development close to production and is the lowest-risk option.

## Option A: create the database in an installed PostgreSQL server

The following commands assume PostgreSQL is installed and `psql` is available.

Open PowerShell and connect as the PostgreSQL administrator:

```powershell
psql -U postgres
```

Create a login and database. The password below is for local development only;
use a different secret outside your machine.

```sql
CREATE ROLE supermarket_app
    WITH LOGIN
    PASSWORD 'supermarket_app_password';

CREATE DATABASE price_compare
    WITH OWNER supermarket_app
    ENCODING 'UTF8';

\connect price_compare

GRANT USAGE, CREATE ON SCHEMA public TO supermarket_app;
```

Exit `psql`:

```sql
\q
```

Set the connection variables in the PowerShell session used to run Django:

```powershell
$env:DB_NAME="price_compare"
$env:DB_USER="supermarket_app"
$env:DB_PASSWORD="supermarket_app_password"
$env:DB_HOST="127.0.0.1"
$env:DB_PORT="5432"
```

Test the login:

```powershell
psql -h 127.0.0.1 -U supermarket_app -d price_compare -c "select current_database(), current_user;"
```

Create the application schema and demo data:

```powershell
.\venv\Scripts\activate
python manage.py migrate
python manage.py pgpartition --yes
python manage.py seed_demo
python manage.py check
python manage.py runserver
```

The dashboard will be available at <http://127.0.0.1:8000/>.

## Option B: run PostgreSQL locally with Docker

This avoids installing PostgreSQL directly. It creates a persistent Docker
volume called `supermarket-postgres-data`.

```powershell
docker volume create supermarket-postgres-data

docker run --name supermarket-postgres `
  -e POSTGRES_DB=price_compare `
  -e POSTGRES_USER=supermarket_app `
  -e POSTGRES_PASSWORD=supermarket_app_password `
  -p 5432:5432 `
  -v supermarket-postgres-data:/var/lib/postgresql/data `
  -d postgres:16
```

Wait until PostgreSQL reports that it is ready:

```powershell
docker logs supermarket-postgres
```

Then set the same `DB_*` variables from Option A and run the Django migration
commands. Useful lifecycle commands are:

```powershell
docker stop supermarket-postgres
docker start supermarket-postgres
```

## Verify the price-tracking tables

After migrations complete, connect to the database and inspect the tables and
history partitions:

```powershell
psql -h 127.0.0.1 -U supermarket_app -d price_compare
```

```sql
\dt

SELECT child.relname AS partition_name,
       pg_get_expr(child.relpartbound, child.oid) AS bounds
FROM pg_inherits
JOIN pg_class parent ON pg_inherits.inhparent = parent.oid
JOIN pg_class child ON pg_inherits.inhrelid = child.oid
WHERE parent.relname = 'catalog_pricehistory'
ORDER BY child.relname;
```

## What "real-time" means in the current application

`StoreProduct.current_*` is a latest-value snapshot, so the comparison API shows
the newest price as soon as a scrape persists it. Celery refreshes inexpensive
HTTP/API sources every 12 hours and browser-based sources every six hours.
Comparison is therefore near-real-time while the worker and Beat are running.
`PriceHistory` still has daily granularity.

Set `PRICE_MAX_AGE_HOURS` to control how long an unrefreshed offer may appear in
current comparisons. It defaults to 48 hours.

Coto and Sodimac scraping are disabled by default because their current modules
still require live endpoint verification and EAN-resolution work. After verifying
an integration, opt it in through that store's `scraper_config`:

```json
{"scraping_enabled": true}
```

For near-real-time comparison, a practical next iteration is:

1. Adjust the Beat frequencies per retailer as reliability and source limits
   become known.
2. Keep `StoreProduct.current_*` as the fast latest-price read model.
3. Change history from a daily `DateField` to a timestamped observation table,
   deduplicating unchanged prices and partitioning by observation time.
4. Store scrape freshness and show `last_seen_at` prominently; a price without a
   freshness indicator should not be presented as live.
5. Publish updates through polling first (for example every 30-60 seconds).
   Add WebSockets or server-sent events only if push updates are actually needed.

This design keeps comparison requests fast while preserving every meaningful
price change for charts and alerts.

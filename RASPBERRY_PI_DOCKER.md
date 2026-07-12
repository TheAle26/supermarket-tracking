# Deploy on a Raspberry Pi with Docker

This stack runs five containers:

- `web`: Django served by Gunicorn;
- `worker`: Celery scraping jobs;
- `beat`: Celery schedules;
- `db`: PostgreSQL 16 with persistent storage;
- `redis`: Celery broker with persistent storage.

Use Docker Compose 2.17 or newer because the stack waits on health-checked
dependencies. Verify it with `docker compose version`.

The images used by this project support 64-bit ARM. A 64-bit Raspberry Pi OS is
recommended. Check the architecture on the Pi:

```bash
uname -m
```

`aarch64` or `arm64` is suitable. A 32-bit `armv7l` installation may not have
prebuilt Python wheels for all scraper dependencies.

## 1. Copy the project to the Pi

Use Git, SCP, or your existing deployment process. Then enter the project:

```bash
cd /path/to/supermarket-tracking
```

The deployment uses `compose.yaml`, `Dockerfile`, and `.env` from this directory.

## 2. Create the environment file

```bash
cp .env.example .env
```

Generate a Django secret without installing anything on the host:

```bash
python3 -c "import secrets; print(secrets.token_urlsafe(50))"
```

Edit `.env`:

```bash
nano .env
```

At minimum, replace `DJANGO_SECRET_KEY` and `DB_PASSWORD`. Set
`DJANGO_ALLOWED_HOSTS` to the Pi's LAN address or DNS name. For example:

```dotenv
DJANGO_SECRET_KEY=paste-the-generated-secret-here
DB_PASSWORD=use-a-different-long-random-password
DJANGO_ALLOWED_HOSTS=192.168.1.50,supermarket.local,localhost,127.0.0.1
WEB_PORT=8080
```

The values shown by your `docker compose config` output must not still begin
with `replace-`. Production startup deliberately rejects those placeholders.

Port `8080` is the default because another Django container may already use
port `8000`. Change `WEB_PORT` if 8080 is also occupied.

Keep `.env` private. It is excluded from the Docker build context.

## 3. Build and start

From the project directory:

```bash
docker compose config
docker compose build
docker compose up -d
```

### Older Docker daemon compatibility

If the build reports an error similar to:

```text
client version 1.52 is too new. Maximum supported API version is 1.41
```

the Compose/buildx client is newer than the Docker daemon. This is unrelated to
other running containers. First inspect both sides:

```bash
docker version
docker compose version
```

The preferred long-term fix is to upgrade Docker Engine, CLI, buildx, and the
Compose plugin together from the same Docker package repository. Existing named
volumes and containers are not deleted by a package upgrade. A daemon restart
briefly stops containers; containers with `always` or `unless-stopped` restart
policies start again automatically.

Before upgrading, record the running containers and their restart policies:

```bash
docker ps --format 'table {{.Names}}\t{{.Image}}\t{{.Ports}}'
docker inspect -f '{{.Name}} -> {{.HostConfig.RestartPolicy.Name}}' $(docker ps -q)
```

For a temporary compatibility path, force the client to use the daemon's API,
build the image directly, and ask Compose not to rebuild it:

```bash
export DOCKER_API_VERSION=1.41
docker build -t supermarket-tracking:local .
docker compose up -d --no-build
```

Remove the override after Docker Engine is upgraded:

```bash
unset DOCKER_API_VERSION
```

The first startup waits for PostgreSQL, runs Django migrations, creates the
current price-history partitions, collects static files, and then starts
Gunicorn. The worker and scheduler start only after the web service is healthy.

Follow the first startup:

```bash
docker compose logs -f web
```

Check every service:

```bash
docker compose ps
```

The existing Docker application remains separate. This stack uses the Compose
project name `supermarket-tracking`, its own default network, and volumes named
`supermarket-tracking_postgres_data` and `supermarket-tracking_redis_data`.
Only host port 8080 is published by default. Check that it is free before startup:

```bash
docker ps --format 'table {{.Names}}\t{{.Ports}}'
sudo ss -ltnp | grep ':8080 ' || true
```

Open the dashboard at:

```text
http://RASPBERRY_PI_ADDRESS:8080/
```

## 4. Optional demo data and administrator

Seed the demonstration catalog once:

```bash
docker compose exec web python manage.py seed_demo
```

The demo command does **not** download real prices. To use only real data,
remove those fixture records and bootstrap the supported public storefronts:

```bash
docker compose exec web python manage.py clear_demo_catalog --yes
docker compose exec web python manage.py bootstrap_vtex_stores
```

The bootstrap command makes live Internet requests to Carrefour and Chango Más,
downloads their VTEX category trees, and saves the category IDs needed by the
scrapers. Trigger the first real refresh immediately instead of waiting for the
next 30-minute schedule:

```bash
docker compose exec web python manage.py shell -c "from scraping.tasks import dispatch_frequent; dispatch_frequent.delay()"
docker compose logs -f worker
```

The worker should log `Dispatching carrefour` / `Dispatching changomas`, then
one `scrape_shard` task for each category. To inspect saved real scrape runs:

```bash
docker compose exec web python manage.py shell -c "from catalog.models import ScrapeRun; print(list(ScrapeRun.objects.order_by('-started_at').values('store__slug','status','items_seen','items_written','last_error')[:20]))"
```

Create a Django administrator:

```bash
docker compose exec web python manage.py createsuperuser
```

## 5. Scraping behavior

Carrefour and Chango Mas use the lightweight HTTP/API schedule every 30 minutes.
Coto and Sodimac remain disabled until their live endpoints are verified. Do not
enable the Sodimac scraper in this image yet: its Playwright Python dependency is
installed, but a Raspberry Pi Chromium runtime is not included.

Inspect scheduled jobs and worker failures with:

```bash
docker compose logs -f worker beat
```

## 6. Updating the application

After copying or pulling new code:

```bash
docker compose build
docker compose up -d
docker image prune -f
```

The web startup safely reapplies migrations and ensures future partitions exist.

## 7. Back up PostgreSQL

Create a compressed SQL backup in the current directory:

```bash
docker compose exec -T db sh -c \
  'pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB"' | gzip > price_compare.sql.gz
```

Restore into an empty database:

```bash
gunzip -c price_compare.sql.gz | docker compose exec -T db sh -c \
  'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB"'
```

Copy backups away from the Pi periodically. Docker volumes protect data across
container replacements, but they are not a backup against SD-card failure.

## 8. Common commands

```bash
docker compose restart
docker compose stop
docker compose start
docker compose down
docker compose logs --tail=200 web worker beat
docker compose exec web python manage.py check
```

`docker compose down` preserves database and Redis volumes. Do not add `-v`
unless you intentionally want to delete all stored data.

## Reverse proxy / HTTPS

If an existing Nginx, Caddy, or Traefik container publishes this application,
proxy it to the Pi host's configured `WEB_PORT`. Then set:

```dotenv
DJANGO_ALLOWED_HOSTS=prices.example.com
DJANGO_CSRF_TRUSTED_ORIGINS=https://prices.example.com
DJANGO_SECURE_COOKIES=1
```

Restart the app after changing `.env`:

```bash
docker compose up -d --force-recreate web worker beat
```

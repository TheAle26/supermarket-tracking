"""
config/settings_snippet.py
==========================
The project-specific settings that matter for THIS system. Merge these into your
real ``config/settings.py``. Everything else is standard Django boilerplate.
"""

# --- Apps ------------------------------------------------------------------
INSTALLED_APPS = [
    # ... django.contrib.* ...
    "psqlextra",              # native partitioning support (must load its backend)
    "django_celery_beat",
    "catalog",
    "scraping",
]

# --- Database --------------------------------------------------------------
# psqlextra requires its own backend, which wraps the standard psycopg backend
# and adds partitioning DDL. Point it at PostgreSQL 14+ (declarative partitioning).
DATABASES = {
    "default": {
        "ENGINE": "psqlextra.backend",
        "NAME": "price_compare",
        "USER": "app",
        "PASSWORD": "***",
        "HOST": "localhost",
        "PORT": "5432",
        "CONN_MAX_AGE": 60,           # persistent connections for the read API
        "OPTIONS": {"pool": True},    # psycopg3 connection pooling
    }
}

# psqlextra partition manager (see catalog/partitioning.py).
PSQLEXTRA_PARTITIONING_MANAGER = "catalog.partitioning.manager"

# --- Celery ----------------------------------------------------------------
CELERY_BROKER_URL = "redis://localhost:6379/0"
CELERY_RESULT_BACKEND = "redis://localhost:6379/1"
CELERY_TASK_ACKS_LATE = True                 # re-queue tasks if a worker dies
CELERY_WORKER_PREFETCH_MULTIPLIER = 1        # fair dispatch for long shard tasks
CELERY_TASK_TIME_LIMIT = 60 * 30             # hard cap: 30 min per shard
CELERY_BEAT_SCHEDULER = "django_celery_beat.schedulers:DatabaseScheduler"

# --- Scraping defaults (overridable per-store via Store.scraper_config) -----
SCRAPING = {
    "default_timeout": 25,
    "proxy_pool": "residential-ar",          # geo-relevant AR exit IPs
    "max_shard_pages": 50,
}

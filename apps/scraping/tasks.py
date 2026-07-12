"""
scraping/tasks.py
=================
Celery orchestration. The design goal: *lightweight but scalable*.

Topology
--------
    beat (frequent HTTP / slower headless schedules)
      └─ dispatch_store_scrape(store)         # 1 task per store: fan out shards
           └─ scrape_shard(store, shard) ...   # 1 task per category: the unit of
                                               #   parallelism, retry, and isolation

Why shard-level tasks?
    * Parallelism: shards run concurrently across workers.
    * Blast radius: one failing category retries alone; the store still completes.
    * Rate control: per-store ``rate_limit`` throttles politely without blocking
      other stores.
    * Idempotency: ``pipeline.persist_listing`` upserts, so a retried shard never
      double-writes.

Retries use exponential backoff **with jitter** — essential against 429/Datadome
so retries don't synchronize into a thundering herd.
"""

from __future__ import annotations

import logging
from datetime import timedelta

from celery import shared_task
from django.utils import timezone

from catalog.models import ScrapeRun, Store

from .pipeline import persist_listing
from .registry import get_scraper

logger = logging.getLogger("scraping.tasks")


# ---------------------------------------------------------------------------
# Beat entrypoints: refresh cheap HTTP sources frequently and expensive browser
# sources less often. The legacy daily task remains useful as a manual full run.
# ---------------------------------------------------------------------------
@shared_task(name="scraping.dispatch_daily")
def dispatch_daily() -> None:
    """Manually dispatch every active store."""
    _dispatch_stores(Store.objects.filter(is_active=True), stagger_seconds=900)


@shared_task(name="scraping.dispatch_frequent")
def dispatch_frequent() -> None:
    """Refresh inexpensive HTTP/API stores twice daily (every 12 hours)."""
    stores = Store.objects.filter(is_active=True, requires_headless=False)
    _dispatch_stores(stores, stagger_seconds=60)


@shared_task(name="scraping.dispatch_headless")
def dispatch_headless() -> None:
    """Refresh browser-based stores on the slower Beat schedule."""
    stores = Store.objects.filter(is_active=True, requires_headless=True)
    _dispatch_stores(stores, stagger_seconds=300)


def _dispatch_stores(stores, stagger_seconds: int) -> None:
    active_since = timezone.now() - timedelta(hours=2)
    for i, store in enumerate(stores):
        if not _scraping_enabled(store):
            logger.info("Skipping %s: scraper requires explicit enablement", store.slug)
            continue
        already_running = store.runs.filter(
            status=ScrapeRun.Status.RUNNING,
            started_at__gte=active_since,
        ).exists()
        if already_running:
            logger.info("Skipping %s: a scrape is already running", store.slug)
            continue
        dispatch_store_scrape.apply_async(
            (store.id,), countdown=i * stagger_seconds
        )


@shared_task(name="scraping.dispatch_store")
def dispatch_store_scrape(store_id: int) -> None:
    """Fan a store out into one ``scrape_shard`` task per category shard."""
    store = Store.objects.get(id=store_id)
    if not _scraping_enabled(store):
        logger.warning("Skipping disabled scraper for %s", store.slug)
        return
    scraper = get_scraper(store)
    shards = scraper.shards()
    logger.info("Dispatching %s: %d shards", store.slug, len(shards))
    for shard in shards:
        scrape_shard.delay(store_id, shard)


def _scraping_enabled(store) -> bool:
    """Require an explicit opt-in for integrations that are still templates."""
    default = store.slug not in {"coto", "sodimac"}
    return bool((getattr(store, "scraper_config", None) or {}).get(
        "scraping_enabled", default
    ))


# ---------------------------------------------------------------------------
# The unit of work: one store + one category. Retried independently.
# ---------------------------------------------------------------------------
@shared_task(
    name="scraping.scrape_shard",
    bind=True,
    max_retries=4,
    autoretry_for=(Exception,),   # network/HTTP/parse errors -> retry the shard
    retry_backoff=30,             # 30s, 60s, 120s, 240s ...
    retry_backoff_max=1800,
    retry_jitter=True,            # randomize to avoid synchronized retry storms
    acks_late=True,               # re-queue if a worker dies mid-shard
    rate_limit="20/m",            # politeness throttle per worker
)
def scrape_shard(self, store_id: int, shard: str) -> dict:
    store = Store.objects.get(id=store_id)
    run = ScrapeRun.objects.create(store=store, shard=shard,
                                   status=ScrapeRun.Status.RUNNING)
    scraper = get_scraper(store)
    seen = written = errors = 0
    last_error = ""

    try:
        for raw in scraper.iter_listings(shard):
            seen += 1
            try:
                if persist_listing(store, raw):
                    written += 1
            except Exception as exc:  # per-item failure must not abort the shard
                errors += 1
                last_error = str(exc)
                logger.warning("persist failed store=%s sku=%s: %s",
                               store.slug, raw.store_sku, exc)
    except Exception as exc:
        # Hard shard failure (auth/anti-bot/timeout): record and let autoretry fire.
        run.status = ScrapeRun.Status.FAILED
        run.last_error = str(exc)
        run.error_count = errors + 1
        run.finished_at = timezone.now()
        run.save(update_fields=["status", "last_error", "error_count", "finished_at"])
        raise  # triggers Celery autoretry with backoff+jitter

    run.status = ScrapeRun.Status.PARTIAL if errors else ScrapeRun.Status.SUCCESS
    run.items_seen, run.items_written, run.error_count = seen, written, errors
    run.last_error, run.finished_at = last_error, timezone.now()
    run.save(update_fields=["status", "items_seen", "items_written",
                            "error_count", "last_error", "finished_at"])
    return {"store": store.slug, "shard": shard, "seen": seen, "written": written}


# ---------------------------------------------------------------------------
# Freshness sweep: mark listings we stopped seeing as out-of-stock.
# ---------------------------------------------------------------------------
@shared_task(name="scraping.mark_stale_out_of_stock")
def mark_stale_out_of_stock(hours: int = 36) -> int:
    """A product that vanished from the catalog for >`hours` is likely delisted/OOS."""
    from catalog.models import StoreProduct

    cutoff = timezone.now() - timezone.timedelta(hours=hours)
    return StoreProduct.objects.filter(
        is_available=True, last_seen_at__lt=cutoff
    ).update(is_available=False, updated_at=timezone.now())

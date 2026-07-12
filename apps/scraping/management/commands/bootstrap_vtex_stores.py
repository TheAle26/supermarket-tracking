"""Register real VTEX stores and discover their public root categories."""

from __future__ import annotations

from urllib.parse import urlparse

from curl_cffi import requests as cffi_requests
from django.core.management.base import BaseCommand, CommandError

from catalog.models import Store


VTEX_STORES = (
    {
        "name": "Carrefour",
        "slug": "carrefour",
        "base_url": "https://www.carrefour.com.ar",
    },
    {
        "name": "Chango Más",
        "slug": "changomas",
        "base_url": "https://www.masonline.com.ar",
    },
)


def root_category_ids(payload) -> list[str]:
    """Return stable, de-duplicated IDs from a VTEX category-tree response.

    We start at the root level to avoid crawling every leaf category twice. If a
    root contains more than VTEX's 2,500-item search window, its configuration
    can later be split into child IDs without changing the scraper.
    """
    if isinstance(payload, dict):
        payload = payload.get("data") or payload.get("categories") or []
    if not isinstance(payload, list):
        return []

    seen: set[str] = set()
    ids: list[str] = []
    for category in payload:
        if not isinstance(category, dict):
            continue
        category_id = category.get("id") or category.get("Id")
        if category_id is None:
            continue
        category_id = str(category_id).strip()
        if category_id and category_id not in seen:
            seen.add(category_id)
            ids.append(category_id)
    return ids


def fetch_root_categories(base_url: str, timeout: int, proxy: str = "") -> list[str]:
    """Fetch the public VTEX category tree from one storefront."""
    base = base_url.rstrip("/")
    endpoint = f"{base}/api/catalog_system/pub/category/tree/3"
    session = cffi_requests.Session(
        impersonate="chrome",
        proxies={"http": proxy, "https": proxy} if proxy else None,
        timeout=timeout,
        headers={"Accept": "application/json", "Accept-Language": "es-AR,es;q=0.9"},
    )
    response = session.get(endpoint)
    response.raise_for_status()
    categories = root_category_ids(response.json())
    if not categories:
        host = urlparse(base).netloc or base
        raise CommandError(f"{host} returned no usable root categories")
    return categories


class Command(BaseCommand):
    help = "Register Carrefour and Chango Más using their public VTEX category trees."

    def add_arguments(self, parser):
        parser.add_argument(
            "--store",
            choices=[store["slug"] for store in VTEX_STORES],
            action="append",
            dest="stores",
            help="Bootstrap only this store; repeat the option for multiple stores.",
        )
        parser.add_argument("--timeout", type=int, default=25)
        parser.add_argument("--proxy", default="", help="Optional proxy URL for the storefront requests.")
        parser.add_argument("--dry-run", action="store_true")

    def handle(self, *args, **options):
        selected = set(options["stores"] or [store["slug"] for store in VTEX_STORES])
        timeout = options["timeout"]
        if timeout < 1:
            raise CommandError("--timeout must be at least 1 second")

        for definition in VTEX_STORES:
            if definition["slug"] not in selected:
                continue
            self.stdout.write(f"Discovering VTEX categories for {definition['name']}…")
            category_ids = fetch_root_categories(
                definition["base_url"], timeout=timeout, proxy=options["proxy"]
            )
            self.stdout.write(f"  found {len(category_ids)} root categories: {', '.join(category_ids)}")
            if options["dry_run"]:
                continue

            existing = Store.objects.filter(slug=definition["slug"]).values_list(
                "scraper_config", flat=True
            ).first() or {}
            config = {
                **existing,
                "scraping_enabled": True,
                "category_ids": category_ids,
                "timeout": timeout,
            }
            Store.objects.update_or_create(
                slug=definition["slug"],
                defaults={
                    "name": definition["name"],
                    "platform": Store.Platform.VTEX,
                    "base_url": definition["base_url"],
                    "catalog_api_base": definition["base_url"],
                    "scraper_config": config,
                    "requires_headless": False,
                    "is_active": True,
                },
            )
            self.stdout.write(self.style.SUCCESS(
                f"  saved {definition['name']} and enabled real scraping"
            ))

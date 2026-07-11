"""
scraping/vtex.py
================
VTEX scraper — covers **Carrefour Argentina** and **Chango Más**, both of which
run on VTEX and expose a *public* catalog JSON API. No headless browser needed.

Why this is the cheap path
--------------------------
VTEX ships two public, unauthenticated catalog endpoints on every storefront:

  1. Legacy Catalog API (stable, well-documented, returns EAN directly):
       GET {base}/api/catalog_system/pub/products/search
           ?fq=C:/{categoryId}/           # filter by category tree
           &_from={n}&_to={n+49}          # 50 items/page, hard window of 2500
     -> each product has ``items[].ean`` (the GTIN we match on) and
        ``items[].sellers[].commertialOffer`` with Price / ListPrice / Teasers.

  2. Intelligent Search (newer, same data, better for keyword queries):
       GET {base}/api/io/_v/api/intelligent-search/product_search/*
           ?query=&count=50&page={p}&fq=category-1:{slug}

We use the Legacy Catalog API: it returns EAN reliably and paginates by category.

TLS fingerprinting
-------------------
The API sits behind the storefront edge (often Cloudflare). Plain ``requests``
gets JA3-fingerprinted and 403'd. ``curl_cffi`` with ``impersonate="chrome"``
presents a real browser TLS handshake, which sails through at a fraction of the
cost of a headless browser. Residential AR proxies are layered on for volume.
"""

from __future__ import annotations

import logging
from decimal import Decimal
from typing import Iterator, Optional

from curl_cffi import requests as cffi_requests  # TLS-impersonating HTTP client

from .base import BaseScraper, RawListing, RawPromo

logger = logging.getLogger("scraping.vtex")

PAGE_SIZE = 50           # VTEX hard limit per page
MAX_WINDOW = 2500        # VTEX refuses _from/_to beyond this; re-shard if hit


class VTEXScraper(BaseScraper):
    """Generic VTEX catalog scraper, configured per store via ``scraper_config``."""

    def _api_base(self) -> str:
        return (self.store.catalog_api_base or self.store.base_url).rstrip("/")

    def _session(self):
        # A fresh impersonating session per shard; proxy pulled from config so the
        # orchestrator can rotate residential AR exits per run.
        proxy = self.config.get("proxy")
        return cffi_requests.Session(
            impersonate="chrome",
            proxies={"http": proxy, "https": proxy} if proxy else None,
            timeout=self.config.get("timeout", 25),
            headers={"Accept": "application/json", "Accept-Language": "es-AR,es;q=0.9"},
        )

    # --- sharding -------------------------------------------------------
    def shards(self) -> list[str]:
        """Category ids to walk. Seeded from config; discover once via
        ``/api/catalog_system/pub/category/tree/3`` and cache in scraper_config."""
        return list(self.config.get("category_ids", []))

    # --- extraction -----------------------------------------------------
    def iter_listings(self, shard: str) -> Iterator[RawListing]:
        """Paginate one category and yield normalized listings.

        Raises on repeated HTTP failure so the orchestrator retries the *shard*
        rather than persisting a partial, misleading snapshot.
        """
        base = self._api_base()
        session = self._session()
        offset = 0

        while offset < MAX_WINDOW:
            url = (
                f"{base}/api/catalog_system/pub/products/search"
                f"?fq=C:/{shard}/&_from={offset}&_to={offset + PAGE_SIZE - 1}"
            )
            resp = session.get(url)
            if resp.status_code == 206:
                # VTEX returns 206 Partial Content for valid paginated ranges.
                pass
            elif resp.status_code == 404 or resp.status_code == 416:
                break  # past the end of the category
            elif resp.status_code != 200:
                # Let tenacity/Celery retry the shard on 403/429/5xx.
                resp.raise_for_status()

            products = resp.json()
            if not products:
                break

            for product in products:
                yield from self._parse_product(product)

            offset += PAGE_SIZE
            if len(products) < PAGE_SIZE:
                break  # last page

    # --- parsing --------------------------------------------------------
    def _parse_product(self, product: dict) -> Iterator[RawListing]:
        brand = product.get("brand", "")
        category_path = "/".join(product.get("categories", [""])[0].strip("/").split("/")) \
            if product.get("categories") else ""

        for item in product.get("items", []):
            ean = (item.get("ean") or "").strip() or None
            sellers = item.get("sellers", [])
            if not sellers:
                continue
            # Default seller = the retailer itself (1P). Marketplace 3P sellers
            # are captured via `seller` for transparency.
            offer = sellers[0].get("commertialOffer", {})
            seller_name = sellers[0].get("sellerName", "")

            price = _dec(offer.get("Price"))
            list_price = _dec(offer.get("ListPrice")) or price
            available = bool(offer.get("IsAvailable")) and (offer.get("AvailableQuantity", 0) > 0)

            net_content, unit = _parse_measure(item.get("measurementUnit"), item.get("unitMultiplier"), product)

            yield RawListing(
                store_sku=str(item.get("itemId", "")),
                store_product_id=str(product.get("productId", "")),
                ean=ean,
                name=item.get("nameComplete") or product.get("productName", ""),
                brand=brand,
                list_price=list_price,
                price=price,
                is_available=available,
                url=_first(product.get("link")) or product.get("linkText", ""),
                seller=seller_name,
                image_url=_first_image(item.get("images")),
                net_content=net_content,
                unit_of_measure=unit,
                category_path=category_path,
                promos=list(_parse_teasers(offer)),
                source_raw={"productId": product.get("productId"), "itemId": item.get("itemId")},
            )


# ---------------------------------------------------------------------------
# VTEX teaser -> RawPromo mapping. VTEX exposes promotions under
# commertialOffer.Teasers and .DiscountHighLight. Teaser semantics vary by store
# configuration, so we map the common shapes and stash the raw payload for audit.
# ---------------------------------------------------------------------------
def _parse_teasers(offer: dict) -> Iterator[RawPromo]:
    for teaser in offer.get("Teasers", []) or []:
        name = (teaser.get("name") or teaser.get("<Name>k__BackingField") or "").lower()
        raw = {"teaser": teaser}

        # 2x1 / 3x2 style — encoded in the teaser name or effect parameters.
        if "x" in name and any(f"{n}x" in name for n in "23456"):
            try:
                get_q, pay_q = _parse_nxm(name)
                yield RawPromo(
                    promo_type="nxm", label=name, get_quantity=get_q,
                    pay_quantity=pay_q, source_raw=raw,
                )
                continue
            except ValueError:
                pass

        # "% off Nth unit" — e.g. "70% en la 2da unidad".
        pct = _extract_percent(name)
        nth = _extract_nth(name)
        if pct and nth:
            yield RawPromo(
                promo_type="nth_unit_pct", label=name, min_quantity=nth,
                nth_unit=nth, discount_percent=pct, source_raw=raw,
            )
            continue

        # Flat percentage discount.
        if pct:
            yield RawPromo(promo_type="percent_off", label=name,
                           discount_percent=pct, source_raw=raw)


# ---------------------------------------------------------------------------
# small parsing helpers
# ---------------------------------------------------------------------------
def _dec(v) -> Optional[Decimal]:
    if v is None:
        return None
    try:
        return Decimal(str(v))
    except Exception:
        return None


def _first(v):
    return v[0] if isinstance(v, list) and v else (v if isinstance(v, str) else "")


def _first_image(images) -> str:
    if isinstance(images, list) and images:
        return images[0].get("imageUrl", "")
    return ""


def _parse_nxm(name: str) -> tuple[int, int]:
    """'2x1' -> (get=2, pay=1); '3x2' -> (get=3, pay=2)."""
    import re

    m = re.search(r"(\d+)\s*x\s*(\d+)", name)
    if not m:
        raise ValueError(name)
    return int(m.group(1)), int(m.group(2))


def _extract_percent(name: str) -> Optional[float]:
    import re

    m = re.search(r"(\d{1,2})\s*%", name)
    return float(m.group(1)) if m else None


def _extract_nth(name: str) -> Optional[int]:
    """Detect '2da/segunda unidad' -> 2, '3ra unidad' -> 3."""
    import re

    words = {"segunda": 2, "tercera": 3, "2da": 2, "3ra": 3, "2nd": 2, "3rd": 3}
    for w, n in words.items():
        if w in name:
            return n
    m = re.search(r"(\d)\s*(?:da|ra|ta)?\s*unidad", name)
    return int(m.group(1)) if m else None


def _parse_measure(unit, multiplier, product) -> tuple[Optional[Decimal], str]:
    """Best-effort net-content extraction. VTEX stores this inconsistently; the
    matcher/enrichment step can refine it. Returns (net_content, unit_of_measure)."""
    mapping = {"kg": "kg", "g": "g", "lt": "l", "l": "l", "ml": "ml", "un": "un"}
    u = mapping.get((unit or "").lower(), "")
    return (_dec(multiplier) if u else None), u

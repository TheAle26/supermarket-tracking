"""
scraping/sodimac.py
===================
Sodimac Argentina — the HARD target.

Platform : Falabella group stack, **Next.js** storefront (SSR + client hydration).
Anti-bot : **Datadome** (TLS/JA3 + canvas/WebGL fingerprint + behavioral scoring).
Strategy : Headless **Playwright + stealth** through **residential AR** proxies.
           Plain HTTP clients get a Datadome interstitial; a real browser context
           passes the fingerprint checks and lets the Next.js data endpoints load.

Two extraction modes, in order of preference:
    A. **Intercept the JSON.** Next.js pages hydrate from either an inlined
       ``__NEXT_DATA__`` script or an XHR to a catalog/search endpoint. We drive
       the browser to a category page and capture that JSON via a response
       handler — far more robust than scraping the rendered DOM.
    B. **DOM fallback.** If the payload shape changes, read product cards from the
       rendered grid as a stopgap while the JSON path is repaired.

EAN caveat: Sodimac category JSON often exposes only an internal SKU. The EAN is
resolved on the *product detail* payload — so for Sodimac the matcher does a
second, throttled pass on PDP endpoints to fetch the GTIN before linking. Never
fall back to matching on the internal SKU.
"""

from __future__ import annotations

import logging
from decimal import Decimal
from typing import Iterator, Optional

from .base import BaseScraper, RawListing

logger = logging.getLogger("scraping.sodimac")


class SodimacScraper(BaseScraper):
    """Playwright-driven scraper. Heavier than VTEX; use sparingly + concurrency-capped."""

    def shards(self) -> list[str]:
        # Category slugs/urls seeded in scraper_config after a one-time sitemap crawl.
        return list(self.config.get("category_urls", []))

    def iter_listings(self, shard: str) -> Iterator[RawListing]:
        # Imported lazily so environments that only run VTEX don't need Playwright.
        from playwright.sync_api import sync_playwright
        from playwright_stealth import stealth_sync

        proxy = self.config.get("proxy")
        captured: list[dict] = []

        with sync_playwright() as pw:
            browser = pw.chromium.launch(
                headless=True,
                proxy={"server": proxy} if proxy else None,
                args=["--disable-blink-features=AutomationControlled"],
            )
            context = browser.new_context(
                locale="es-AR",
                timezone_id="America/Argentina/Buenos_Aires",
                user_agent=self.config.get("user_agent"),
            )
            page = context.new_page()
            stealth_sync(page)  # patch navigator.webdriver, WebGL vendor, etc.

            # Mode A: intercept the catalog/search JSON the Next.js app fetches.
            def _on_response(resp):
                url = resp.url
                if any(k in url for k in ("/catalog", "/search", "/products", "_next/data")):
                    try:
                        captured.append(resp.json())
                    except Exception:
                        pass

            page.on("response", _on_response)

            page_num = 1
            while page_num <= self.config.get("max_pages", 20):
                page.goto(f"{shard}?page={page_num}", wait_until="networkidle",
                          timeout=45_000)
                self._assert_not_blocked(page)      # detect Datadome interstitial
                if not captured:
                    break
                batch = captured.copy()
                captured.clear()
                any_yielded = False
                for payload in batch:
                    for raw in self._parse_payload(payload):
                        any_yielded = True
                        yield raw
                if not any_yielded:
                    break
                page_num += 1

            context.close()
            browser.close()

    # --- helpers --------------------------------------------------------
    def _assert_not_blocked(self, page) -> None:
        """Raise so the orchestrator retries (with backoff + a fresh proxy) if
        Datadome served a challenge instead of content."""
        html = page.content()
        if "datadome" in html.lower() or "geo.captcha-delivery.com" in html.lower():
            raise RuntimeError("Datadome challenge encountered — rotate proxy and retry")

    def _parse_payload(self, payload: dict) -> Iterator[RawListing]:
        """Map Sodimac's catalog JSON to RawListing. Shape is defensively probed
        because Falabella changes it periodically; unknown shapes yield nothing
        (and the DOM fallback / alerting picks it up)."""
        products = (
            payload.get("data", {}).get("results")
            or payload.get("products")
            or []
        )
        for p in products:
            price = _dec(_dig(p, "prices", 0, "price", 0)) or _dec(p.get("price"))
            list_price = _dec(p.get("listPrice")) or price
            yield RawListing(
                store_sku=str(p.get("skuId") or p.get("id") or ""),
                store_product_id=str(p.get("productId") or ""),
                ean=None,  # resolved on the PDP pass; NEVER match on skuId
                name=p.get("displayName") or p.get("name", ""),
                brand=p.get("brand", ""),
                list_price=list_price,
                price=price,
                is_available=bool(p.get("isAvailable", True)),
                url=p.get("url", ""),
                image_url=_dig(p, "media", "mainImage") or "",
                source_raw={"skuId": p.get("skuId")},
            )


def _dec(v) -> Optional[Decimal]:
    try:
        return Decimal(str(v)) if v is not None else None
    except Exception:
        return None


def _dig(d, *keys):
    """Safe nested getter across dict/list."""
    cur = d
    for k in keys:
        try:
            cur = cur[k]
        except (KeyError, IndexError, TypeError):
            return None
    return cur

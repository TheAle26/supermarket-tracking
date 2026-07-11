"""
scraping/coto.py
================
Coto Digital (cotodigital3.coto.com.ar) — legacy / custom platform (NOT VTEX).

Reality check: Coto's stack has shifted between a legacy Oracle ATG / custom
front and newer iterations. Endpoint paths therefore MUST be verified live
(DevTools ▸ Network) rather than hard-coded from assumptions — treat the URL
below as a template to confirm, not gospel.

Strategy (in order of preference):
    A. **Hidden JSON search endpoint.** Coto's search/category pages fetch a JSON
       (or JSON-ish) results feed. Discover it once via the Network tab, pin the
       request signature (path + params + required cookies), and hit it directly
       with ``curl_cffi`` (TLS impersonation) — the cheap path, like VTEX.
    B. **HTML fallback (BeautifulSoup).** If no clean JSON exists, parse the
       server-rendered product grid. Slower and more brittle; used only until the
       JSON path is confirmed.

Anti-bot: moderate (edge WAF / rate limiting) — no Datadome-class challenge
observed, so headless is usually unnecessary; a warm session cookie + impersonated
TLS + AR proxy suffices. EAN is exposed on the product detail feed; resolve it
there, never from Coto's internal article code.
"""

from __future__ import annotations

import logging
from decimal import Decimal
from typing import Iterator, Optional

from curl_cffi import requests as cffi_requests

from .base import BaseScraper, RawListing

logger = logging.getLogger("scraping.coto")


class CotoScraper(BaseScraper):
    def _session(self):
        proxy = self.config.get("proxy")
        s = cffi_requests.Session(
            impersonate="chrome",
            proxies={"http": proxy, "https": proxy} if proxy else None,
            timeout=self.config.get("timeout", 25),
        )
        # Warm the session so the WAF sees a browser-like cookie jar before the
        # first data request (visit home once; endpoint confirmed at rollout).
        try:
            s.get(self.store.base_url)
        except Exception:
            pass
        return s

    def shards(self) -> list[str]:
        return list(self.config.get("category_ids", []))

    def iter_listings(self, shard: str) -> Iterator[RawListing]:
        """
        Mode A skeleton — CONFIRM the endpoint/params against live Network traffic
        before enabling in production. Falls back to raising so the orchestrator
        retries rather than silently emitting nothing.
        """
        session = self._session()
        base = (self.store.catalog_api_base or self.store.base_url).rstrip("/")
        page, page_size = 0, self.config.get("page_size", 50)

        while page < self.config.get("max_pages", 40):
            # NOTE: path + query are placeholders pinned during endpoint discovery.
            url = (
                f"{base}/sitios/cdigi/browse"
                f"?Nrpp={page_size}&No={page * page_size}&categoryId={shard}&format=json"
            )
            resp = session.get(url, headers={"Accept": "application/json"})
            if resp.status_code != 200:
                resp.raise_for_status()

            data = resp.json()
            records = _dig(data, "contents", 0, "records") or data.get("records") or []
            if not records:
                break
            for rec in records:
                item = self._parse_record(rec)
                if item:
                    yield item
            page += 1

    def _parse_record(self, rec: dict) -> Optional[RawListing]:
        attrs = rec.get("attributes", rec)
        ean = _first(attrs.get("product.eanPrincipal")) or _first(attrs.get("sku.ean"))
        price = _dec(_first(attrs.get("sku.activePrice")) or _first(attrs.get("product.price")))
        list_price = _dec(_first(attrs.get("sku.referencePrice"))) or price
        if price is None and list_price is None:
            return None
        return RawListing(
            store_sku=str(_first(attrs.get("sku.repositoryId")) or ""),
            store_product_id=str(_first(attrs.get("product.repositoryId")) or ""),
            ean=(ean or None),
            name=_first(attrs.get("product.displayName")) or "",
            brand=_first(attrs.get("product.brand")) or "",
            list_price=list_price,
            price=price,
            is_available=_first(attrs.get("sku.availabilityStatus")) != "OUTOFSTOCK",
            url=_first(attrs.get("product.route")) or "",
            source_raw={"repositoryId": _first(attrs.get("sku.repositoryId"))},
        )


def _dec(v) -> Optional[Decimal]:
    try:
        return Decimal(str(v)) if v not in (None, "") else None
    except Exception:
        return None


def _first(v):
    if isinstance(v, list):
        return v[0] if v else None
    return v


def _dig(d, *keys):
    cur = d
    for k in keys:
        try:
            cur = cur[k]
        except (KeyError, IndexError, TypeError):
            return None
    return cur

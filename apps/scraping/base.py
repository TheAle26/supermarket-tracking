"""
scraping/base.py
================
Scraper abstractions shared by every target.

The pipeline speaks in terms of a normalized ``RawListing`` dataclass. Each
concrete scraper (VTEX API, Next.js hydration, headless Playwright) is only
responsible for producing a stream of ``RawListing`` objects; the persistence +
matching + OUP steps are identical for all stores (see ``pipeline.py``). This is
what keeps per-store logic thin and the system modular.
"""

from __future__ import annotations

import abc
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Iterator, Optional


@dataclass
class RawPromo:
    """Normalized promotion as extracted from a store payload."""

    promo_type: str                      # maps to Promotion.Type
    label: str = ""
    min_quantity: int = 1
    get_quantity: Optional[int] = None
    pay_quantity: Optional[int] = None
    nth_unit: Optional[int] = None
    discount_percent: Optional[float] = None
    bulk_total: Optional[float] = None
    max_units: Optional[int] = None
    payment_method: str = ""
    bank: str = ""
    is_stackable: bool = True
    source_raw: dict = field(default_factory=dict)


@dataclass
class RawListing:
    """
    Store-agnostic listing record. EAN is mandatory for matching; a listing
    without a resolvable EAN is still persisted (unmatched) for later backfill.
    """

    store_sku: str
    ean: Optional[str]
    name: str
    list_price: Optional[Decimal]
    price: Optional[Decimal]                  # shelf price, single unit
    is_available: bool = True
    brand: str = ""
    url: str = ""
    seller: str = ""
    store_product_id: str = ""
    image_url: str = ""
    net_content: Optional[Decimal] = None
    unit_of_measure: str = ""
    category_path: str = ""                   # "Almacen/Aceites" style breadcrumb
    promos: list[RawPromo] = field(default_factory=list)
    source_raw: dict = field(default_factory=dict)


class BaseScraper(abc.ABC):
    """
    Contract every store scraper implements.

    ``iter_listings`` yields ``RawListing`` objects for a single *shard* (a
    category or collection). Sharding by category gives us: bounded memory,
    natural parallelism, and per-shard retry granularity in the orchestrator.
    """

    def __init__(self, store):
        self.store = store
        self.config = store.scraper_config or {}

    @abc.abstractmethod
    def shards(self) -> list[str]:
        """Return the list of shard identifiers (category/collection ids) to scrape."""

    @abc.abstractmethod
    def iter_listings(self, shard: str) -> Iterator[RawListing]:
        """Yield normalized listings for one shard. Should raise on hard failures
        so the orchestrator can retry the shard, not silently drop data."""

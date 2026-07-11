"""
scraping/pipeline.py
====================
Store-agnostic persistence pipeline. Consumes ``RawListing`` objects from any
scraper and performs, per listing, in a single idempotent transaction:

    1. Product resolution / upsert   -> by EAN (never by SKU).
    2. StoreProduct upsert           -> by (store, store_sku), links to Product.
    3. Promotion refresh             -> replace the listing's active promos.
    4. OUP computation               -> via catalog.pricing.
    5. Snapshot write                -> denormalized current_* on StoreProduct.
    6. PriceHistory upsert           -> one point per (listing, day), idempotent.

Idempotency is the contract: re-running a scrape for the same day converges to
the same DB state (safe to retry a failed shard without double-counting).
"""

from __future__ import annotations

import logging

from django.db import transaction
from django.utils import timezone

from catalog.models import PriceHistory, Product, Promotion, StoreProduct
from catalog.identifiers import normalize_gtin
from catalog.pricing import optimal_unit_price, price_per_measure

from .base import RawListing

logger = logging.getLogger("scraping.pipeline")


def persist_listing(store, raw: RawListing) -> bool:
    """Persist one normalized listing. Returns True if a row was written."""
    if not str(raw.store_sku or "").strip():
        raise ValueError("store_sku is required to persist a listing")
    if raw.price is None and raw.list_price is None:
        return False  # nothing priceable to record

    with transaction.atomic():
        product = _resolve_product(raw)

        # 2) Upsert the store listing on its natural key (store, store_sku).
        listing_defaults = dict(
            store_product_id=raw.store_product_id,
            ean_raw=raw.ean or "",
            url=raw.url,
            seller=raw.seller,
            is_available=raw.is_available,
            last_seen_at=timezone.now(),
        )
        # A source can temporarily omit its EAN. Do not let that transient
        # extraction failure unlink a listing that was matched previously.
        if product is not None:
            listing_defaults["product"] = product

        listing, _created = StoreProduct.objects.select_for_update().update_or_create(
            store=store,
            store_sku=str(raw.store_sku).strip(),
            defaults=listing_defaults,
        )

        # 3) Refresh promotions: wipe & re-insert the active set for this listing.
        #    Cheaper and simpler than diffing; the set is tiny (0-3 rows).
        _refresh_promotions(listing, raw)

        # 4) OUP: fold the best unconditional promo into a per-unit price.
        shelf = raw.price or raw.list_price
        specs = [p.to_spec() for p in listing.promotions.all()]
        oup_result = optimal_unit_price(shelf, specs)

        ppm = None
        if product:
            ppm = price_per_measure(oup_result.unit_price, product.base_measure)

        # 5) Denormalized snapshot for the hot comparison query.
        listing.current_list_price = raw.list_price
        listing.current_price = shelf
        listing.current_oup = oup_result.unit_price
        listing.current_price_per_measure = ppm
        listing.save(update_fields=[
            "current_list_price", "current_price", "current_oup",
            "current_price_per_measure", "updated_at",
        ])

        # 6) Append-only daily history point (idempotent upsert per day).
        PriceHistory.objects.update_or_create(
            store_product=listing,
            captured_at=timezone.localdate(),
            defaults=dict(
                list_price=raw.list_price,
                price=shelf,
                oup=oup_result.unit_price,
                is_available=raw.is_available,
            ),
        )
    return True


def _resolve_product(raw: RawListing):
    """Find-or-create the global product by EAN. Listings without an EAN are
    persisted unmatched (product=None) for a later backfill pass."""
    ean = normalize_gtin(raw.ean)
    if not ean:
        return None
    product, created = Product.objects.get_or_create(
        ean=ean,
        defaults=dict(
            name=raw.name, brand=raw.brand,
            net_content=raw.net_content, unit_of_measure=raw.unit_of_measure,
            image_url=raw.image_url,
        ),
    )
    # Enrich sparse fields on already-known products without clobbering curated data.
    if not created:
        dirty = []
        if not product.net_content and raw.net_content:
            product.net_content, product.unit_of_measure = raw.net_content, raw.unit_of_measure
            dirty += ["net_content", "unit_of_measure"]
        if not product.image_url and raw.image_url:
            product.image_url = raw.image_url
            dirty.append("image_url")
        if dirty:
            product.save(update_fields=dirty + ["updated_at"])
    return product


def _refresh_promotions(listing: StoreProduct, raw: RawListing) -> None:
    listing.promotions.all().delete()
    if not raw.promos:
        return
    Promotion.objects.bulk_create([
        Promotion(
            store_product=listing,
            promo_type=p.promo_type,
            label=p.label,
            min_quantity=p.min_quantity,
            get_quantity=p.get_quantity,
            pay_quantity=p.pay_quantity,
            nth_unit=p.nth_unit,
            discount_percent=p.discount_percent,
            bulk_total=p.bulk_total,
            max_units=p.max_units,
            payment_method=p.payment_method,
            bank=p.bank,
            is_stackable=p.is_stackable,
            source_raw=p.source_raw,
        )
        for p in raw.promos
    ])

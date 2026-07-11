"""
catalog/managers.py
==================
Custom QuerySets that encapsulate the optimized read paths. Keeping these here
(rather than ad-hoc ``.filter()`` chains in views) means the index-aware query
shapes live in one place and are reused everywhere.
"""

from datetime import timedelta

from django.conf import settings
from django.db import models
from django.db.models import Min, Q
from django.utils import timezone


def _fresh_cutoff():
    return timezone.now() - timedelta(hours=settings.PRICE_MAX_AGE_HOURS)


class StoreProductQuerySet(models.QuerySet):
    """Read paths over the denormalized listing snapshot."""

    def available(self):
        """Only matched, in-stock listings with a computed OUP."""
        return self.filter(
            is_available=True,
            product__isnull=False,
            current_oup__isnull=False,
            last_seen_at__gte=_fresh_cutoff(),
        )

    def cheapest_per_product(self):
        """
        One row per product: the cheapest available offer, by OUP.

        Uses PostgreSQL ``DISTINCT ON`` which, backed by the
        ``ix_sp_product_oup`` partial index ``(product, current_oup) WHERE
        is_available``, resolves to a single index scan — no window functions,
        no per-product subquery. This is the core "who's cheapest right now?" query.
        """
        return (
            self.available()
            .order_by("product_id", "current_oup")
            .distinct("product_id")
        )

    def for_basket(self, product_ids):
        """Cheapest offer per product for a shopping list (basket comparison)."""
        return self.available().filter(product_id__in=product_ids).cheapest_per_product()

    def stale(self, before):
        """Listings not refreshed since `before` — candidates for a re-scrape/mark-OOS."""
        return self.filter(last_seen_at__lt=before)


class ProductQuerySet(models.QuerySet):
    """Read paths over the global product catalog."""

    def with_price_summary(self):
        """
        Annotate each product with its market-wide minimum OUP across all
        available listings. Single aggregate JOIN, filtered to available rows so
        the partial index is used.
        """
        return self.annotate(
            min_oup=Min(
                "listings__current_oup",
                filter=Q(
                    listings__is_available=True,
                    listings__current_oup__isnull=False,
                    listings__last_seen_at__gte=_fresh_cutoff(),
                ),
            ),
        )

    def search(self, term):
        """Exact/typeahead search entry point (swap to trigram/FTS in prod)."""
        return self.filter(
            Q(name__icontains=term) | Q(brand__icontains=term) | Q(ean=term)
        )

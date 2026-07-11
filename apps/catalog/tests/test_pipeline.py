from contextlib import nullcontext
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from django.test import SimpleTestCase

from scraping.base import RawListing
from scraping.pipeline import persist_listing


class PersistenceSafetyTests(SimpleTestCase):
    def _raw(self, **overrides):
        values = {
            "store_sku": "sku-1",
            "ean": None,
            "name": "Product",
            "list_price": Decimal("120"),
            "price": Decimal("100"),
        }
        values.update(overrides)
        return RawListing(**values)

    def test_rejects_empty_store_sku(self):
        with self.assertRaisesRegex(ValueError, "store_sku"):
            persist_listing(SimpleNamespace(), self._raw(store_sku="  "))

    @patch("scraping.pipeline.PriceHistory.objects.update_or_create")
    @patch("scraping.pipeline.StoreProduct.objects.select_for_update")
    @patch("scraping.pipeline._resolve_product", return_value=None)
    @patch("scraping.pipeline.transaction.atomic", return_value=nullcontext())
    def test_missing_ean_does_not_clear_existing_product(
        self, _atomic, _resolve, select_for_update, _history
    ):
        existing_product = object()
        listing = MagicMock(product=existing_product)
        promotions = MagicMock()
        promotions.__iter__.return_value = iter([])
        listing.promotions.all.return_value = promotions
        select_for_update.return_value.update_or_create.return_value = (listing, False)

        persist_listing(SimpleNamespace(), self._raw())

        kwargs = select_for_update.return_value.update_or_create.call_args.kwargs
        self.assertNotIn("product", kwargs["defaults"])
        self.assertIs(listing.product, existing_product)

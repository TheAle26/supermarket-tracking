from decimal import Decimal

from django.test import SimpleTestCase

from catalog.identifiers import normalize_gtin
from catalog.models import Product


class IdentifierTests(SimpleTestCase):
    def test_normalizes_valid_gtin_with_separators(self):
        self.assertEqual(normalize_gtin("4006-3813 33931"), "4006381333931")

    def test_rejects_invalid_check_digit(self):
        self.assertIsNone(normalize_gtin("4006381333932"))

    def test_rejects_non_gtin_identifier(self):
        self.assertIsNone(normalize_gtin("store-sku-123"))

    def test_base_measure_remains_decimal(self):
        product = Product(net_content=Decimal("900"), unit_of_measure=Product.Unit.MILLILITER)

        self.assertEqual(product.base_measure, ("l", Decimal("0.900")))
        self.assertIsInstance(product.base_measure[1], Decimal)

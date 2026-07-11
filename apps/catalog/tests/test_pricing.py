from decimal import Decimal

from django.test import SimpleTestCase

from catalog.pricing import PromoSpec, line_item_cost, optimal_unit_price


class PricingTests(SimpleTestCase):
    def test_money_calculation_stays_decimal_native(self):
        promo = PromoSpec(
            promo_type="percent_off",
            discount_percent=Decimal("12.50"),
        )

        result = line_item_cost(Decimal("19.99"), [promo], quantity=3)

        self.assertEqual(result.total, Decimal("52.47"))
        self.assertIsInstance(result.total, Decimal)

    def test_bank_discounts_are_alternatives_not_compounded(self):
        promos = [
            PromoSpec(
                promo_type="bank",
                discount_percent=10,
                is_conditional=True,
                is_stackable=True,
            ),
            PromoSpec(
                promo_type="bank",
                discount_percent=20,
                is_conditional=True,
                is_stackable=True,
            ),
        ]

        result = optimal_unit_price(Decimal("1000"), promos)
        line = line_item_cost(
            Decimal("1000"), promos, quantity=2, include_conditional=True
        )

        self.assertEqual(result.unit_price_with_bank, Decimal("800.00"))
        self.assertEqual(line.total, Decimal("1600.00"))

    def test_nth_unit_cap_limits_discounted_blocks(self):
        promo = PromoSpec(
            promo_type="nth_unit_pct",
            min_quantity=2,
            nth_unit=2,
            discount_percent=50,
            max_units=2,
        )

        result = line_item_cost(Decimal("100"), [promo], quantity=4)

        self.assertEqual(result.total, Decimal("350.00"))

    def test_bulk_price_cap_limits_discounted_blocks(self):
        promo = PromoSpec(
            promo_type="bulk_price",
            min_quantity=3,
            bulk_total=240,
            max_units=3,
        )

        result = line_item_cost(Decimal("100"), [promo], quantity=6)

        self.assertEqual(result.total, Decimal("540.00"))

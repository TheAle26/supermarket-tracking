"""
manage.py seed_demo

Populates a small, realistic Argentine catalog (Coto / Carrefour / Chango Más /
Sodimac) so the dashboard's live API returns data. OUP + price-per-measure are
computed through the REAL engine (catalog.pricing), exactly as the scraper
pipeline would — this doubles as an integration smoke test of the pricing path.

    python manage.py migrate
    python manage.py pgpartition --yes     # create current-month partition first
    python manage.py seed_demo
    python manage.py runserver             # -> http://127.0.0.1:8000/
"""

from __future__ import annotations

from decimal import Decimal

from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone

from catalog.models import (
    PriceHistory, Product, Promotion, Store, StoreProduct,
)
from catalog.pricing import optimal_unit_price, price_per_measure

STORES = [
    ("Coto", "coto", Store.Platform.LEGACY, "https://www.cotodigital3.com.ar"),
    ("Carrefour", "carrefour", Store.Platform.VTEX, "https://www.carrefour.com.ar"),
    ("Chango Más", "changomas", Store.Platform.VTEX, "https://www.masonline.com.ar"),
    ("Sodimac", "sodimac", Store.Platform.NEXTJS, "https://www.sodimac.com.ar"),
]

# (ean, name, brand, net_content, unit) + per-store (shelf, promo-spec-kwargs|None)
PRODUCTS = [
    ("7790895000123", "Coca-Cola Original 2.25L", "Coca-Cola", Decimal("2.25"), "l", {
        "coto": (Decimal("2890"), dict(promo_type="nth_unit_pct", min_quantity=2, nth_unit=2,
                                       discount_percent=Decimal("70"), label="70% 2da unidad")),
        "carrefour": (Decimal("2750"), dict(promo_type="percent_off", discount_percent=Decimal("20"),
                                            label="20% descuento")),
        "changomas": (Decimal("2990"), dict(promo_type="nxm", get_quantity=2, pay_quantity=1, label="2x1")),
    }),
    ("7790742000015", "Leche Entera La Serenísima 1L", "La Serenísima", Decimal("1"), "l", {
        "coto": (Decimal("1290"), None),
        "carrefour": (Decimal("1250"), dict(promo_type="nxm", get_quantity=3, pay_quantity=2, label="3x2")),
        "changomas": (Decimal("1320"), None),
    }),
    ("7790070410122", "Aceite Girasol Natura 900ml", "Natura", Decimal("900"), "ml", {
        "coto": (Decimal("3450"), dict(promo_type="percent_off", discount_percent=Decimal("15"),
                                       label="15% descuento")),
        "carrefour": (Decimal("3390"), None),
        "changomas": (Decimal("3290"), dict(promo_type="nth_unit_pct", min_quantity=2, nth_unit=2,
                                            discount_percent=Decimal("50"), label="50% 2da unidad")),
    }),
    ("7793640000018", "Papel Higiénico Higienol 4u", "Higienol", Decimal("4"), "un", {
        "coto": (Decimal("2790"), None),
        "carrefour": (Decimal("2690"), dict(promo_type="nxm", get_quantity=2, pay_quantity=1, label="2x1")),
        "changomas": (Decimal("2850"), None),
        "sodimac": (Decimal("2990"), None),
    }),
    ("7791290791015", "Lavandina Ayudín 1L", "Ayudín", Decimal("1"), "l", {
        "coto": (Decimal("1490"), None),
        "carrefour": (Decimal("1420"), None),
        "changomas": (Decimal("1390"), dict(promo_type="percent_off", discount_percent=Decimal("15"),
                                            label="15% descuento")),
        "sodimac": (Decimal("1350"), dict(promo_type="percent_off", discount_percent=Decimal("20"),
                                          label="20% descuento")),
    }),
]


class Command(BaseCommand):
    help = "Seed a demo catalog + current price snapshot for the dashboard."

    @transaction.atomic
    def handle(self, *args, **opts):
        stores = {
            slug: Store.objects.update_or_create(
                slug=slug, defaults=dict(name=name, platform=platform, base_url=url,
                                         catalog_api_base=url, is_active=True)
            )[0]
            for name, slug, platform, url in STORES
        }

        for ean, name, brand, content, unit, offers in PRODUCTS:
            product, _ = Product.objects.update_or_create(
                ean=ean, defaults=dict(name=name, brand=brand,
                                       net_content=content, unit_of_measure=unit),
            )
            for slug, (shelf, promo_kwargs) in offers.items():
                store = stores[slug]
                sp, _ = StoreProduct.objects.update_or_create(
                    store=store, store_sku=f"{slug}-{ean}",
                    defaults=dict(product=product, is_available=True,
                                  last_seen_at=timezone.now()),
                )
                sp.promotions.all().delete()
                specs = []
                if promo_kwargs:
                    promo = Promotion.objects.create(store_product=sp, **promo_kwargs)
                    specs = [promo.to_spec()]

                # Compute OUP + snapshot exactly like the pipeline does.
                res = optimal_unit_price(shelf, specs)
                sp.current_list_price = shelf
                sp.current_price = shelf
                sp.current_oup = res.unit_price
                sp.current_price_per_measure = price_per_measure(res.unit_price, product.base_measure)
                sp.save()

                # Best-effort history point for the current day (needs the
                # current-month partition to exist — see docstring).
                try:
                    PriceHistory.objects.update_or_create(
                        store_product=sp, captured_at=timezone.localdate(),
                        defaults=dict(list_price=shelf, price=shelf, oup=res.unit_price),
                    )
                except Exception as exc:  # missing partition on a fresh DB
                    self.stderr.write(f"  (skipped history point: {exc})")

        self.stdout.write(self.style.SUCCESS(
            f"Seeded {len(PRODUCTS)} products across {len(STORES)} stores."
        ))

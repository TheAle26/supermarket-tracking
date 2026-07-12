"""Delete only the fixture rows created by ``seed_demo``."""

from __future__ import annotations

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from catalog.models import Product, Store, StoreProduct

from .seed_demo import PRODUCTS, STORES


class Command(BaseCommand):
    help = "Remove only demo listings/products/stores created by seed_demo."

    def add_arguments(self, parser):
        parser.add_argument("--yes", action="store_true", help="Confirm deletion of demo data.")

    @transaction.atomic
    def handle(self, *args, **options):
        if not options["yes"]:
            raise CommandError("Refusing to delete data without --yes")

        eans = [product[0] for product in PRODUCTS]
        slugs = [store[1] for store in STORES]
        demo_skus = [f"{slug}-{ean}" for slug in slugs for ean in eans]

        listings_deleted, _ = StoreProduct.objects.filter(store_sku__in=demo_skus).delete()
        products_deleted, _ = Product.objects.filter(ean__in=eans, listings__isnull=True).delete()
        stores_deleted, _ = Store.objects.filter(slug__in=slugs, listings__isnull=True).delete()

        self.stdout.write(self.style.SUCCESS(
            "Removed demo rows: "
            f"listing-related={listings_deleted}, products={products_deleted}, stores={stores_deleted}"
        ))

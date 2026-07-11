"""
catalog/models.py
=================
Core domain models for the Supermarket Price Comparison platform.

Design principles
-----------------
1.  **EAN is the universal join key.** Products are a *global*, store-agnostic
    entity keyed by GTIN/EAN-13. Store-specific identifiers (VTEX productId,
    SKU, itemId) live ONLY on ``StoreProduct`` and are never used for matching.

2.  **Read/write separation.** ``PriceHistory`` is an append-only, time-series
    table (partitioned, 90-day retention). ``StoreProduct`` carries a
    *denormalized snapshot* of the latest price + Optimal Unit Price (OUP) so
    that the hot "who is cheapest right now?" query never touches the history
    table.

3.  **The money field is OUP, not the shelf price.** Comparison queries sort on
    ``current_oup`` (Optimal Unit Price) which already folds in the best
    unconditional promotion (2x1, 70%-off-2nd, etc.). See ``catalog.pricing``.

4.  **Indexes are intentional.** Every field that appears in a WHERE / ORDER BY
    of a documented query path has a matching index. Nothing more (write cost).
"""

from decimal import Decimal

from django.core.validators import MinValueValidator
from django.db import models
from django.utils import timezone

# psqlextra gives us first-class native PostgreSQL declarative partitioning.
# PriceHistory is a RANGE-partitioned table so the 90-day purge is an O(1)
# `DROP TABLE partition` instead of a bloat-inducing bulk DELETE + VACUUM.
from psqlextra.models import PostgresPartitionedModel
from psqlextra.types import PostgresPartitioningMethod

from .managers import ProductQuerySet, StoreProductQuerySet


# ---------------------------------------------------------------------------
# Reference / taxonomy models
# ---------------------------------------------------------------------------
class Category(models.Model):
    """Lightweight, self-referential category tree (normalized once, not per store)."""

    name = models.CharField(max_length=160)
    slug = models.SlugField(max_length=180, unique=True)
    parent = models.ForeignKey(
        "self", null=True, blank=True, on_delete=models.SET_NULL, related_name="children"
    )

    class Meta:
        verbose_name_plural = "categories"
        indexes = [models.Index(fields=["parent"])]

    def __str__(self) -> str:
        return self.name


class Store(models.Model):
    """
    A retailer we scrape. ``platform`` + ``scraper_config`` drive the
    extraction strategy (see ``apps.scraping``) without hard-coding per-store
    logic in the pipeline.
    """

    class Platform(models.TextChoices):
        VTEX = "vtex", "VTEX (public catalog API)"
        NEXTJS = "nextjs", "Next.js / custom hydration"
        LEGACY = "legacy", "Legacy / custom HTML"

    name = models.CharField(max_length=120)
    slug = models.SlugField(max_length=80, unique=True)  # 'coto', 'carrefour', ...
    platform = models.CharField(max_length=16, choices=Platform.choices)

    base_url = models.URLField()
    # e.g. VTEX -> "https://www.carrefour.com.ar" (API base is derived from it)
    catalog_api_base = models.URLField(blank=True, default="")

    # Free-form knobs consumed by the scraper: seed categories/collections,
    # per-store rate limits, trade-policy id (VTEX), proxy pool name, etc.
    scraper_config = models.JSONField(default=dict, blank=True)

    # True => the storefront needs a headless browser (Datadome/Next.js SSR).
    requires_headless = models.BooleanField(default=False)
    is_active = models.BooleanField(default=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        indexes = [models.Index(fields=["is_active", "platform"])]

    def __str__(self) -> str:
        return self.name


# ---------------------------------------------------------------------------
# The global product (EAN-keyed) — the heart of universal matching
# ---------------------------------------------------------------------------
class Product(models.Model):
    """
    A physical product identified globally by its EAN/GTIN. One row here is
    shared by every store that carries the item, which is exactly what makes
    cross-store comparison possible.

    ``net_content`` + ``unit_of_measure`` let us compute a normalized
    *price-per-measure* (ARS/L, ARS/kg) so a 900 ml bottle can be compared
    against a 1 L bottle fairly.
    """

    class Unit(models.TextChoices):
        UNIT = "un", "unit"
        GRAM = "g", "gram"
        KILOGRAM = "kg", "kilogram"
        MILLILITER = "ml", "milliliter"
        LITER = "l", "liter"

    # EAN-13 / GTIN-14. Stored as text (leading zeros matter). This is THE key.
    ean = models.CharField(max_length=14, unique=True)

    name = models.CharField(max_length=255)  # canonical/normalized display name
    brand = models.CharField(max_length=120, blank=True, default="")
    category = models.ForeignKey(
        Category, null=True, blank=True, on_delete=models.SET_NULL, related_name="products"
    )

    # Normalization for price-per-measure. e.g. net_content=900, unit='ml'.
    net_content = models.DecimalField(
        max_digits=12, decimal_places=3, null=True, blank=True,
        validators=[MinValueValidator(0)],
    )
    unit_of_measure = models.CharField(max_length=4, choices=Unit.choices, blank=True, default="")

    image_url = models.URLField(blank=True, default="")
    is_active = models.BooleanField(default=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    objects = ProductQuerySet.as_manager()

    class Meta:
        indexes = [
            # `ean` already has a unique index (used for the scraper upsert).
            models.Index(fields=["brand"]),
            models.Index(fields=["category", "is_active"]),
            # Trigram/full-text search is added in a migration (see notes); the
            # btree above covers exact-brand filtering on listing pages.
        ]

    def __str__(self) -> str:
        return f"{self.name} [{self.ean}]"

    # ---- price-per-measure helper -------------------------------------
    @property
    def base_measure(self):
        """Return net_content converted to a canonical base unit (L or kg or unit).

        Used to derive ARS-per-liter / ARS-per-kg from an absolute price. Returns
        ``None`` when the product has no measurable content (matching is still
        valid, only the per-measure comparison is unavailable).
        """
        if not self.net_content or not self.unit_of_measure:
            return None
        factor = {
            self.Unit.MILLILITER: ("l", Decimal("0.001")),
            self.Unit.LITER: ("l", Decimal("1")),
            self.Unit.GRAM: ("kg", Decimal("0.001")),
            self.Unit.KILOGRAM: ("kg", Decimal("1")),
            self.Unit.UNIT: ("un", Decimal("1")),
        }.get(self.unit_of_measure)
        if not factor:
            return None
        base_unit, mult = factor
        return base_unit, self.net_content * mult


# ---------------------------------------------------------------------------
# Store <-> Product bridge (holds the store-local identifiers + hot snapshot)
# ---------------------------------------------------------------------------
class StoreProduct(models.Model):
    """
    One retailer's listing of a global :class:`Product`.

    * ``store_sku`` / ``store_item_id`` are kept ONLY for re-scraping and audit
      — never for cross-store matching.
    * ``product`` is nullable: a freshly scraped listing whose EAN we haven't
      resolved yet lives here in an "unmatched" state until the matcher links it.
    * The ``current_*`` columns are a *denormalized snapshot* refreshed on every
      scrape. They make the comparison query a single-table, index-only scan.
    """

    store = models.ForeignKey(Store, on_delete=models.CASCADE, related_name="listings")
    product = models.ForeignKey(
        Product, null=True, blank=True, on_delete=models.SET_NULL, related_name="listings"
    )

    # --- store-local identifiers (NOT used for matching) ---------------
    store_sku = models.CharField(max_length=64)          # VTEX skuId / itemId
    store_product_id = models.CharField(max_length=64, blank=True, default="")  # VTEX productId
    ean_raw = models.CharField(max_length=32, blank=True, default="")  # EAN as reported, for audit
    url = models.URLField(max_length=600, blank=True, default="")
    seller = models.CharField(max_length=80, blank=True, default="")  # VTEX marketplace seller

    is_available = models.BooleanField(default=True)

    # --- denormalized hot snapshot (updated by the pipeline each run) ---
    current_list_price = models.DecimalField(  # regular / struck-through price
        max_digits=12, decimal_places=2, null=True, blank=True
    )
    current_price = models.DecimalField(       # shelf price (single unit, no promo)
        max_digits=12, decimal_places=2, null=True, blank=True
    )
    current_oup = models.DecimalField(         # Optimal Unit Price (best unconditional promo)
        max_digits=12, decimal_places=2, null=True, blank=True
    )
    current_price_per_measure = models.DecimalField(  # ARS per L / kg, derived from OUP
        max_digits=14, decimal_places=4, null=True, blank=True
    )

    last_seen_at = models.DateTimeField(default=timezone.now)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    objects = StoreProductQuerySet.as_manager()

    class Meta:
        constraints = [
            # A SKU is unique within a store; this is the scraper's upsert target.
            models.UniqueConstraint(fields=["store", "store_sku"], name="uq_store_sku"),
        ]
        indexes = [
            # Hot path: "cheapest available listing for product X" ->
            # filter product + is_available, order by OUP. Partial index keeps it lean.
            models.Index(
                fields=["product", "current_oup"],
                name="ix_sp_product_oup",
                condition=models.Q(is_available=True),
            ),
            # Re-scrape / freshness sweeps per store.
            models.Index(fields=["store", "is_available"]),
            models.Index(fields=["last_seen_at"]),
            # Backfill worker that resolves unmatched listings.
            models.Index(
                fields=["store"], name="ix_sp_unmatched",
                condition=models.Q(product__isnull=True),
            ),
        ]

    def __str__(self) -> str:
        return f"{self.store.slug}:{self.store_sku}"


# ---------------------------------------------------------------------------
# Promotions (drives the OUP calculation)
# ---------------------------------------------------------------------------
class Promotion(models.Model):
    """
    A single promotional mechanic attached to a listing. The model is a *hybrid*:
    the numeric parameters common to the OUP math are explicit, queryable columns,
    while anything type-specific/future lives in ``params``. The original store
    payload is kept in ``source_raw`` for audit and re-derivation.

    See :func:`catalog.pricing.optimal_unit_price` for how these are evaluated.
    """

    class Type(models.TextChoices):
        NXM = "nxm", "N for the price of M (2x1, 3x2)"
        PERCENT_OFF = "percent_off", "Flat % off"
        NTH_UNIT_PCT = "nth_unit_pct", "% off the Nth unit (70% off 2nd)"
        BULK_PRICE = "bulk_price", "Buy N, pay fixed total"
        BANK = "bank", "Bank / payment-method discount"

    store_product = models.ForeignKey(
        StoreProduct, on_delete=models.CASCADE, related_name="promotions"
    )
    promo_type = models.CharField(max_length=16, choices=Type.choices)
    label = models.CharField(max_length=255, blank=True, default="")  # raw store text

    # --- generic numeric parameters (explicit for query + calculation) ---
    min_quantity = models.PositiveSmallIntegerField(default=1)   # units to trigger promo
    get_quantity = models.PositiveSmallIntegerField(null=True, blank=True)  # NxM: you GET N
    pay_quantity = models.PositiveSmallIntegerField(null=True, blank=True)  # NxM: you PAY M
    nth_unit = models.PositiveSmallIntegerField(null=True, blank=True)      # which unit is discounted
    discount_percent = models.DecimalField(  # 0..100
        max_digits=5, decimal_places=2, null=True, blank=True
    )
    bulk_total = models.DecimalField(  # BULK_PRICE: total ARS for `min_quantity` units
        max_digits=12, decimal_places=2, null=True, blank=True
    )
    max_units = models.PositiveSmallIntegerField(null=True, blank=True)  # promo cap per basket

    # --- payment conditions (BANK type) ---
    payment_method = models.CharField(max_length=40, blank=True, default="")  # visa/modo/mercadopago
    bank = models.CharField(max_length=60, blank=True, default="")
    # Bank discounts usually STACK on top of the shelf promo, but are conditional
    # (only apply if the shopper pays with that method). We keep them out of the
    # "unconditional" OUP and expose them as a separate best-case figure.
    is_stackable = models.BooleanField(default=True)

    params = models.JSONField(default=dict, blank=True)     # type-specific extras
    source_raw = models.JSONField(default=dict, blank=True)  # original teaser/payload

    priority = models.SmallIntegerField(default=0)          # apply/tie-break order
    valid_from = models.DateTimeField(default=timezone.now)
    valid_to = models.DateTimeField(null=True, blank=True)
    is_active = models.BooleanField(default=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        indexes = [
            # Load active promos for a listing (OUP recompute).
            models.Index(
                fields=["store_product", "valid_to"],
                name="ix_promo_active",
                condition=models.Q(is_active=True),
            ),
            models.Index(fields=["promo_type"]),
            models.Index(fields=["bank"], condition=~models.Q(bank=""), name="ix_promo_bank"),
        ]

    def __str__(self) -> str:
        return f"{self.get_promo_type_display()} @ {self.store_product_id}"

    def is_currently_valid(self, at=None) -> bool:
        at = at or timezone.now()
        if not self.is_active or self.valid_from > at:
            return False
        return self.valid_to is None or self.valid_to >= at

    def to_spec(self):
        """Convert to the plain dataclass consumed by ``catalog.pricing``.

        Keeping the calculator free of Django imports makes the OUP math trivially
        unit-testable and reusable outside the request cycle.
        """
        from .pricing import PromoSpec  # local import: avoid app-loading cycles

        return PromoSpec(
            promo_type=self.promo_type,
            min_quantity=self.min_quantity,
            get_quantity=self.get_quantity,
            pay_quantity=self.pay_quantity,
            nth_unit=self.nth_unit,
            discount_percent=self.discount_percent,
            bulk_total=self.bulk_total,
            max_units=self.max_units,
            is_conditional=(self.promo_type == self.Type.BANK) or bool(self.payment_method or self.bank),
            is_stackable=self.is_stackable,
            priority=self.priority,
        )


# ---------------------------------------------------------------------------
# Time-series price history — PARTITIONED, append-only, 90-day retention
# ---------------------------------------------------------------------------
class PriceHistory(PostgresPartitionedModel):
    """
    Append-only daily price snapshot. This is the largest table in the system
    (n_listings x days) and powers inflation/trend analytics.

    Retention (90 days) is enforced at the *partition* level: the table is
    RANGE-partitioned by month on ``captured_at``. Purging is a metadata-only
    ``DROP TABLE catalog_pricehistory_2025_03`` — no row-by-row DELETE, no dead
    tuples, no VACUUM storm. See ``catalog.management.commands.prune_price_history``.

    NOTE on partitioning + Django: on a partitioned table every UNIQUE / PK
    constraint must include the partition key. Hence the idempotency constraint
    below is ``(store_product, captured_at)`` rather than a plain unique on a
    surrogate — which also happens to be the natural "one point per listing per
    day" key we want for idempotent upserts.
    """

    store_product = models.ForeignKey(
        StoreProduct, on_delete=models.CASCADE, related_name="price_points"
    )

    list_price = models.DecimalField(max_digits=12, decimal_places=2, null=True)
    price = models.DecimalField(max_digits=12, decimal_places=2, null=True)   # shelf price
    oup = models.DecimalField(max_digits=12, decimal_places=2, null=True)     # optimal unit price
    is_available = models.BooleanField(default=True)

    # Date granularity = one snapshot per day. Also the partition key.
    captured_at = models.DateField(default=timezone.localdate)

    class PartitioningMeta:
        method = PostgresPartitioningMethod.RANGE
        key = ["captured_at"]

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["store_product", "captured_at"], name="uq_pricepoint_per_day"
            ),
        ]
        indexes = [
            # Trend query: last 90 days of a listing, newest first.
            models.Index(fields=["store_product", "-captured_at"], name="ix_ph_sp_date"),
            # Cross-store daily basket / inflation index scans by date.
            models.Index(fields=["captured_at"], name="ix_ph_date"),
        ]

    def __str__(self) -> str:
        return f"{self.store_product_id} @ {self.captured_at}: {self.oup}"


class PriceAggregate(models.Model):
    """
    Monthly rollup of :class:`PriceHistory`, written *before* a partition is
    dropped. This is the "archive" half of the retention policy: daily
    granularity lives for 90 days; beyond that we keep one compact row per
    (listing, month) so long-horizon inflation/trend charts survive cheaply.
    """

    store_product = models.ForeignKey(
        StoreProduct, on_delete=models.CASCADE, related_name="price_aggregates"
    )
    period = models.DateField()  # first day of the month, e.g. 2025-03-01
    avg_oup = models.DecimalField(max_digits=12, decimal_places=2, null=True)
    min_oup = models.DecimalField(max_digits=12, decimal_places=2, null=True)
    max_oup = models.DecimalField(max_digits=12, decimal_places=2, null=True)
    samples = models.IntegerField(default=0)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["store_product", "period"], name="uq_aggregate_period"
            ),
        ]
        indexes = [models.Index(fields=["store_product", "-period"])]

    def __str__(self) -> str:
        return f"{self.store_product_id} {self.period:%Y-%m}: avg {self.avg_oup}"


# ---------------------------------------------------------------------------
# Observability — every scrape run is auditable (drives retries/monitoring)
# ---------------------------------------------------------------------------
class ScrapeRun(models.Model):
    """One extraction run for one store (or store+category shard). Enables the
    orchestrator to retry failed shards, alert on regressions, and expose SLAs."""

    class Status(models.TextChoices):
        RUNNING = "running", "Running"
        SUCCESS = "success", "Success"
        PARTIAL = "partial", "Partial (some shards failed)"
        FAILED = "failed", "Failed"

    store = models.ForeignKey(Store, on_delete=models.CASCADE, related_name="runs")
    shard = models.CharField(max_length=120, blank=True, default="")  # category/collection id
    status = models.CharField(max_length=12, choices=Status.choices, default=Status.RUNNING)

    items_seen = models.IntegerField(default=0)
    items_written = models.IntegerField(default=0)
    error_count = models.IntegerField(default=0)
    last_error = models.TextField(blank=True, default="")

    started_at = models.DateTimeField(auto_now_add=True)
    finished_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        indexes = [
            models.Index(fields=["store", "-started_at"]),
            models.Index(fields=["status", "-started_at"]),
        ]

    def __str__(self) -> str:
        return f"{self.store.slug}/{self.shard or 'all'} [{self.status}]"

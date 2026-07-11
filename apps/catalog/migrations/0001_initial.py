# Generated for the supermarket price comparison project.

import django.core.validators
import django.db.models.deletion
import django.utils.timezone
import psqlextra.manager.manager
import psqlextra.models.partitioned
from django.db import migrations, models
from psqlextra.backend.migrations.operations import PostgresCreatePartitionedModel
from psqlextra.types import PostgresPartitioningMethod


class Migration(migrations.Migration):
    initial = True

    dependencies = []

    operations = [
        migrations.CreateModel(
            name="Category",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("name", models.CharField(max_length=160)),
                ("slug", models.SlugField(max_length=180, unique=True)),
                (
                    "parent",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="children",
                        to="catalog.category",
                    ),
                ),
            ],
            options={
                "verbose_name_plural": "categories",
                "indexes": [models.Index(fields=["parent"], name="catalog_cat_parent__5307be_idx")],
            },
        ),
        migrations.CreateModel(
            name="Store",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("name", models.CharField(max_length=120)),
                ("slug", models.SlugField(max_length=80, unique=True)),
                (
                    "platform",
                    models.CharField(
                        choices=[
                            ("vtex", "VTEX (public catalog API)"),
                            ("nextjs", "Next.js / custom hydration"),
                            ("legacy", "Legacy / custom HTML"),
                        ],
                        max_length=16,
                    ),
                ),
                ("base_url", models.URLField()),
                ("catalog_api_base", models.URLField(blank=True, default="")),
                ("scraper_config", models.JSONField(blank=True, default=dict)),
                ("requires_headless", models.BooleanField(default=False)),
                ("is_active", models.BooleanField(default=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
            ],
            options={
                "indexes": [models.Index(fields=["is_active", "platform"], name="catalog_sto_is_acti_a94fe0_idx")],
            },
        ),
        migrations.CreateModel(
            name="Product",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("ean", models.CharField(max_length=14, unique=True)),
                ("name", models.CharField(max_length=255)),
                ("brand", models.CharField(blank=True, default="", max_length=120)),
                (
                    "net_content",
                    models.DecimalField(
                        blank=True,
                        decimal_places=3,
                        max_digits=12,
                        null=True,
                        validators=[django.core.validators.MinValueValidator(0)],
                    ),
                ),
                (
                    "unit_of_measure",
                    models.CharField(
                        blank=True,
                        choices=[("un", "unit"), ("g", "gram"), ("kg", "kilogram"), ("ml", "milliliter"), ("l", "liter")],
                        default="",
                        max_length=4,
                    ),
                ),
                ("image_url", models.URLField(blank=True, default="")),
                ("is_active", models.BooleanField(default=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "category",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="products",
                        to="catalog.category",
                    ),
                ),
            ],
            options={
                "indexes": [
                    models.Index(fields=["brand"], name="catalog_pro_brand_f6911e_idx"),
                    models.Index(fields=["category", "is_active"], name="catalog_pro_categor_891fe8_idx"),
                ],
            },
        ),
        migrations.CreateModel(
            name="StoreProduct",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("store_sku", models.CharField(max_length=64)),
                ("store_product_id", models.CharField(blank=True, default="", max_length=64)),
                ("ean_raw", models.CharField(blank=True, default="", max_length=32)),
                ("url", models.URLField(blank=True, default="", max_length=600)),
                ("seller", models.CharField(blank=True, default="", max_length=80)),
                ("is_available", models.BooleanField(default=True)),
                ("current_list_price", models.DecimalField(blank=True, decimal_places=2, max_digits=12, null=True)),
                ("current_price", models.DecimalField(blank=True, decimal_places=2, max_digits=12, null=True)),
                ("current_oup", models.DecimalField(blank=True, decimal_places=2, max_digits=12, null=True)),
                ("current_price_per_measure", models.DecimalField(blank=True, decimal_places=4, max_digits=14, null=True)),
                ("last_seen_at", models.DateTimeField(default=django.utils.timezone.now)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "product",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="listings",
                        to="catalog.product",
                    ),
                ),
                (
                    "store",
                    models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="listings", to="catalog.store"),
                ),
            ],
            options={
                "indexes": [
                    models.Index(fields=["product", "current_oup"], name="ix_sp_product_oup", condition=models.Q(("is_available", True))),
                    models.Index(fields=["store", "is_available"], name="catalog_sto_store_i_9c409e_idx"),
                    models.Index(fields=["last_seen_at"], name="catalog_sto_last_se_67d310_idx"),
                    models.Index(fields=["store"], name="ix_sp_unmatched", condition=models.Q(("product__isnull", True))),
                ],
                "constraints": [models.UniqueConstraint(fields=("store", "store_sku"), name="uq_store_sku")],
            },
        ),
        migrations.CreateModel(
            name="Promotion",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                (
                    "promo_type",
                    models.CharField(
                        choices=[
                            ("nxm", "N for the price of M (2x1, 3x2)"),
                            ("percent_off", "Flat % off"),
                            ("nth_unit_pct", "% off the Nth unit (70% off 2nd)"),
                            ("bulk_price", "Buy N, pay fixed total"),
                            ("bank", "Bank / payment-method discount"),
                        ],
                        max_length=16,
                    ),
                ),
                ("label", models.CharField(blank=True, default="", max_length=255)),
                ("min_quantity", models.PositiveSmallIntegerField(default=1)),
                ("get_quantity", models.PositiveSmallIntegerField(blank=True, null=True)),
                ("pay_quantity", models.PositiveSmallIntegerField(blank=True, null=True)),
                ("nth_unit", models.PositiveSmallIntegerField(blank=True, null=True)),
                ("discount_percent", models.DecimalField(blank=True, decimal_places=2, max_digits=5, null=True)),
                ("bulk_total", models.DecimalField(blank=True, decimal_places=2, max_digits=12, null=True)),
                ("max_units", models.PositiveSmallIntegerField(blank=True, null=True)),
                ("payment_method", models.CharField(blank=True, default="", max_length=40)),
                ("bank", models.CharField(blank=True, default="", max_length=60)),
                ("is_stackable", models.BooleanField(default=True)),
                ("params", models.JSONField(blank=True, default=dict)),
                ("source_raw", models.JSONField(blank=True, default=dict)),
                ("priority", models.SmallIntegerField(default=0)),
                ("valid_from", models.DateTimeField(default=django.utils.timezone.now)),
                ("valid_to", models.DateTimeField(blank=True, null=True)),
                ("is_active", models.BooleanField(default=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "store_product",
                    models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="promotions", to="catalog.storeproduct"),
                ),
            ],
            options={
                "indexes": [
                    models.Index(fields=["store_product", "valid_to"], name="ix_promo_active", condition=models.Q(("is_active", True))),
                    models.Index(fields=["promo_type"], name="catalog_pro_promo_t_6c075c_idx"),
                    models.Index(fields=["bank"], name="ix_promo_bank", condition=models.Q(("bank", ""), _negated=True)),
                ],
            },
        ),
        PostgresCreatePartitionedModel(
            name="PriceHistory",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("list_price", models.DecimalField(decimal_places=2, max_digits=12, null=True)),
                ("price", models.DecimalField(decimal_places=2, max_digits=12, null=True)),
                ("oup", models.DecimalField(decimal_places=2, max_digits=12, null=True)),
                ("is_available", models.BooleanField(default=True)),
                ("captured_at", models.DateField(default=django.utils.timezone.localdate)),
                (
                    "store_product",
                    models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="price_points", to="catalog.storeproduct"),
                ),
            ],
            options={
                "indexes": [
                    models.Index(fields=["store_product", "-captured_at"], name="ix_ph_sp_date"),
                    models.Index(fields=["captured_at"], name="ix_ph_date"),
                ],
                "constraints": [models.UniqueConstraint(fields=("store_product", "captured_at"), name="uq_pricepoint_per_day")],
            },
            partitioning_options={
                "method": PostgresPartitioningMethod.RANGE,
                "key": ["captured_at"],
            },
            bases=(psqlextra.models.partitioned.PostgresPartitionedModel,),
            managers=[
                ("objects", psqlextra.manager.manager.PostgresManager()),
            ],
        ),
        migrations.CreateModel(
            name="PriceAggregate",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("period", models.DateField()),
                ("avg_oup", models.DecimalField(decimal_places=2, max_digits=12, null=True)),
                ("min_oup", models.DecimalField(decimal_places=2, max_digits=12, null=True)),
                ("max_oup", models.DecimalField(decimal_places=2, max_digits=12, null=True)),
                ("samples", models.IntegerField(default=0)),
                (
                    "store_product",
                    models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="price_aggregates", to="catalog.storeproduct"),
                ),
            ],
            options={
                "indexes": [models.Index(fields=["store_product", "-period"], name="catalog_pri_store_p_194c36_idx")],
                "constraints": [models.UniqueConstraint(fields=("store_product", "period"), name="uq_aggregate_period")],
            },
        ),
        migrations.CreateModel(
            name="ScrapeRun",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("shard", models.CharField(blank=True, default="", max_length=120)),
                (
                    "status",
                    models.CharField(
                        choices=[
                            ("running", "Running"),
                            ("success", "Success"),
                            ("partial", "Partial (some shards failed)"),
                            ("failed", "Failed"),
                        ],
                        default="running",
                        max_length=12,
                    ),
                ),
                ("items_seen", models.IntegerField(default=0)),
                ("items_written", models.IntegerField(default=0)),
                ("error_count", models.IntegerField(default=0)),
                ("last_error", models.TextField(blank=True, default="")),
                ("started_at", models.DateTimeField(auto_now_add=True)),
                ("finished_at", models.DateTimeField(blank=True, null=True)),
                (
                    "store",
                    models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="runs", to="catalog.store"),
                ),
            ],
            options={
                "indexes": [
                    models.Index(fields=["store", "-started_at"], name="catalog_scr_store_i_450afe_idx"),
                    models.Index(fields=["status", "-started_at"], name="catalog_scr_status_49a18f_idx"),
                ],
            },
        ),
    ]

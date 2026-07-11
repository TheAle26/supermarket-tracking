"""
manage.py prune_price_history [--retain-days 90] [--dry-run]

Enforces the rolling 90-day retention policy in two phases:

    1. ARCHIVE  — roll up expiring daily rows (those about to fall outside the
                  window) into monthly ``PriceAggregate`` rows, so long-horizon
                  trend/inflation charts survive at reduced granularity.
    2. PRUNE    — drop whole ``PriceHistory`` partitions that lie entirely
                  before the retention cutoff. Dropping a partition is a
                  metadata-only operation: instant, and it leaves ZERO dead
                  tuples (unlike a bulk DELETE, which would bloat the table and
                  trigger an expensive VACUUM).

Run daily from Celery Beat / cron. Idempotent and safe to re-run.
"""

from __future__ import annotations

from datetime import date, timedelta

from django.core.management.base import BaseCommand
from django.db import connection
from django.db.models import Avg, Max, Min, Count
from django.db.models.functions import TruncMonth
from django.utils import timezone

from catalog.models import PriceAggregate, PriceHistory


class Command(BaseCommand):
    help = "Archive-then-prune PriceHistory to enforce the rolling retention window."

    def add_arguments(self, parser):
        parser.add_argument("--retain-days", type=int, default=90)
        parser.add_argument("--dry-run", action="store_true",
                            help="Report what would happen; write nothing.")

    def handle(self, *args, **opts):
        retain_days = opts["retain_days"]
        dry_run = opts["dry_run"]
        cutoff = timezone.localdate() - timedelta(days=retain_days)
        self.stdout.write(f"Retention cutoff = {cutoff} (keep >= this date)")

        archived = self._archive(cutoff, dry_run)
        dropped = self._drop_old_partitions(cutoff, dry_run)

        self.stdout.write(self.style.SUCCESS(
            f"Done. archived_aggregate_rows={archived} partitions_dropped={dropped} "
            f"{'(dry-run)' if dry_run else ''}"
        ))

    # -- phase 1: archive expiring days into monthly aggregates -----------
    def _archive(self, cutoff: date, dry_run: bool) -> int:
        """Upsert monthly aggregates for every day strictly older than `cutoff`."""
        rows = (
            PriceHistory.objects.filter(captured_at__lt=cutoff)
            .annotate(period=TruncMonth("captured_at"))
            .values("store_product_id", "period")
            .annotate(
                avg_oup=Avg("oup"), min_oup=Min("oup"),
                max_oup=Max("oup"), samples=Count("id"),
            )
        )
        count = 0
        for r in rows.iterator(chunk_size=2000):
            count += 1
            if dry_run:
                continue
            PriceAggregate.objects.update_or_create(
                store_product_id=r["store_product_id"],
                period=r["period"],
                defaults=dict(
                    avg_oup=r["avg_oup"], min_oup=r["min_oup"],
                    max_oup=r["max_oup"], samples=r["samples"],
                ),
            )
        self.stdout.write(f"  archive: {count} (store_product, month) aggregates")
        return count

    # -- phase 2: drop partitions entirely older than the cutoff ----------
    def _drop_old_partitions(self, cutoff: date, dry_run: bool) -> int:
        """
        Enumerate PriceHistory child partitions and DROP those whose upper bound
        is <= cutoff. We read partition bounds straight from the catalog so we
        never drop a partition that still holds in-window data.

        (In production this is typically delegated to ``manage.py pgpartition
        --delete`` using catalog/partitioning.py; the explicit SQL here documents
        exactly what happens and works without extra config.)
        """
        parent = PriceHistory._meta.db_table
        dropped = 0
        with connection.cursor() as cur:
            cur.execute(
                """
                SELECT child.relname,
                       pg_get_expr(child.relpartbound, child.oid) AS bounds
                FROM pg_inherits
                JOIN pg_class parent ON pg_inherits.inhparent = parent.oid
                JOIN pg_class child  ON pg_inherits.inhrelid  = child.oid
                WHERE parent.relname = %s
                """,
                [parent],
            )
            for relname, bounds in cur.fetchall():
                upper = _partition_upper_bound(bounds)
                if upper is not None and upper <= cutoff:
                    self.stdout.write(f"  prune: {relname} (upper {upper} <= {cutoff})")
                    if not dry_run:
                        # Safe: identifier comes from pg_catalog, not user input.
                        cur.execute(f'DROP TABLE IF EXISTS "{relname}"')
                    dropped += 1
        return dropped


def _partition_upper_bound(bounds_expr: str):
    """Parse "FOR VALUES FROM ('2025-03-01') TO ('2025-04-01')" -> date(2025,4,1)."""
    import re

    m = re.search(r"TO \('(\d{4}-\d{2}-\d{2})", bounds_expr or "")
    if not m:
        return None
    y, mo, d = map(int, m.group(1).split("-"))
    return date(y, mo, d)

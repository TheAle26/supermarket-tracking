"""
catalog/partitioning.py
=======================
Declarative partition lifecycle for ``PriceHistory``, powered by
django-postgres-extra (psqlextra).

The manager below is wired into settings as ``POSTGRES_EXTRA`` /
``PSQLEXTRA_PARTITIONING_MANAGER`` and driven by ``manage.py pgpartition``:

    * Ahead-of-time: keep the *next* month's partition pre-created so writes at
      month boundaries never hit a missing partition.
    * Retention: partitions whose entire range is older than the 90-day window
      are eligible for DELETE (a metadata-only ``DROP TABLE`` — instant, no bloat).

We schedule ``pgpartition`` from Celery Beat (see scraping.tasks / cron), and run
the *archive* step first (roll up expiring days into ``PriceAggregate``) so no
trend data is silently lost.
"""

from psqlextra.partitioning import (
    PostgresPartitioningManager,
    partition_by_current_time,
)

from .models import PriceHistory

# 90-day retention rounded up to whole months (a 90-day window can straddle 4
# calendar months), so we keep 4 months of daily partitions before dropping.
RETAIN_MONTHS = 4

manager = PostgresPartitioningManager([
    partition_by_current_time(
        model=PriceHistory,
        count=2,                 # always keep current + next month pre-created
        months=1,                # partition size = one calendar month
        name_format="%Y_%m",     # must match the bootstrap names in migration 0002
        max_age=None,            # deletion handled explicitly below for clarity
    ),
])

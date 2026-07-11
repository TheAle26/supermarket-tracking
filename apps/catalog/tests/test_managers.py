from datetime import datetime, timedelta, timezone as dt_timezone
from unittest.mock import patch

from django.test import SimpleTestCase, override_settings

from catalog.managers import _fresh_cutoff


class FreshnessTests(SimpleTestCase):
    @override_settings(PRICE_MAX_AGE_HOURS=24)
    @patch("catalog.managers.timezone.now")
    def test_cutoff_uses_configured_maximum_age(self, now):
        current = datetime(2026, 7, 10, 12, 0, tzinfo=dt_timezone.utc)
        now.return_value = current

        self.assertEqual(_fresh_cutoff(), current - timedelta(hours=24))

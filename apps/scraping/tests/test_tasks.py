from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from django.test import SimpleTestCase

from scraping.tasks import _dispatch_stores, _scraping_enabled


class DispatchTests(SimpleTestCase):
    @patch("scraping.tasks.dispatch_store_scrape.apply_async")
    def test_skips_store_with_active_run(self, apply_async):
        active = SimpleNamespace(id=1, slug="active", runs=MagicMock())
        active.runs.filter.return_value.exists.return_value = True
        ready = SimpleNamespace(id=2, slug="ready", runs=MagicMock())
        ready.runs.filter.return_value.exists.return_value = False

        _dispatch_stores([active, ready], stagger_seconds=60)

        apply_async.assert_called_once_with((2,), countdown=60)

    def test_incomplete_scrapers_require_explicit_opt_in(self):
        self.assertFalse(_scraping_enabled(SimpleNamespace(slug="coto", scraper_config={})))
        self.assertTrue(_scraping_enabled(SimpleNamespace(
            slug="coto", scraper_config={"scraping_enabled": True}
        )))

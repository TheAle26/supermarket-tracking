from unittest.mock import MagicMock, patch

from django.test import SimpleTestCase

from scraping.management.commands.bootstrap_vtex_stores import (
    fetch_root_categories,
    root_category_ids,
)


class VtexBootstrapTests(SimpleTestCase):
    def test_extracts_root_ids_without_duplicates(self):
        payload = [
            {"id": 1, "name": "Food"},
            {"id": "2", "name": "Drinks"},
            {"id": 1, "name": "Duplicate"},
            {"name": "Missing ID"},
        ]

        self.assertEqual(root_category_ids(payload), ["1", "2"])

    @patch("scraping.management.commands.bootstrap_vtex_stores.cffi_requests.Session")
    def test_fetches_public_category_tree(self, session_class):
        response = MagicMock()
        response.json.return_value = [{"id": 8}, {"id": 9}]
        session_class.return_value.get.return_value = response

        result = fetch_root_categories("https://shop.example", timeout=10)

        self.assertEqual(result, ["8", "9"])
        session_class.return_value.get.assert_called_once_with(
            "https://shop.example/api/catalog_system/pub/category/tree/3"
        )
        response.raise_for_status.assert_called_once()

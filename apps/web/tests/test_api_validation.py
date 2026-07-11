import json

from django.test import RequestFactory, SimpleTestCase

from web.api import cart_optimize, history, search


class ApiValidationTests(SimpleTestCase):
    def setUp(self):
        self.factory = RequestFactory()

    def test_search_rejects_non_numeric_limit(self):
        response = search(self.factory.get("/api/products/", {"q": "milk", "limit": "many"}))

        self.assertEqual(response.status_code, 400)

    def test_history_rejects_out_of_range_days_before_database_lookup(self):
        response = history(
            self.factory.get("/api/products/123/history/", {"days": "0"}),
            "123",
        )

        self.assertEqual(response.status_code, 400)

    def test_cart_rejects_non_object_body(self):
        response = cart_optimize(
            self.factory.post(
                "/api/cart/optimize/",
                data=json.dumps([]),
                content_type="application/json",
            )
        )

        self.assertEqual(response.status_code, 400)

    def test_cart_rejects_invalid_quantity(self):
        response = cart_optimize(
            self.factory.post(
                "/api/cart/optimize/",
                data=json.dumps({"items": [{"ean": "123", "quantity": "lots"}]}),
                content_type="application/json",
            )
        )

        self.assertEqual(response.status_code, 400)

    def test_cart_rejects_string_boolean(self):
        response = cart_optimize(
            self.factory.post(
                "/api/cart/optimize/",
                data=json.dumps({"items": [], "include_conditional": "false"}),
                content_type="application/json",
            )
        )

        self.assertEqual(response.status_code, 400)

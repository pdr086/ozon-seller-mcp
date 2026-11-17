import json
import tempfile
import unittest
from pathlib import Path

import httpx

from ozon_seller import config
from ozon_seller.ozon import ANALYTICS, RETURNS, STOCKS, Seller, SellerError

FAKE_KEY = {"client_id": "12", "api_key": "synthetic-key-123"}


class SellerTests(unittest.TestCase):
    def seller(self, response_for):
        def handle(request):
            self.assertEqual(request.url.host, "api-seller.ozon.ru")
            self.assertEqual(request.headers["api-key"], FAKE_KEY["api_key"])
            return response_for(request.url.path, json.loads(request.content))

        seller = Seller(FAKE_KEY, transport=httpx.MockTransport(handle))
        self.addCleanup(seller.http.close)
        return seller

    def test_order_amount_and_empty_period(self):
        def reply(path, body):
            self.assertEqual(path, ANALYTICS)
            self.assertEqual(body["metrics"], ["ordered_units", "revenue"])
            return httpx.Response(200, json={"result": {
                "data": [{"dimensions": [{"id": "2026-10-01"}], "metrics": [2, 51.25]}],
                "totals": [2, 51.25],
            }})
        result = self.seller(reply).sales_summary("2026-10-01", "2026-10-02")
        self.assertEqual(result["ordered_amount"], "51.25")
        self.assertEqual(result["ordered_units"], 2)
        self.assertEqual(result["days_with_rows"], 1)

        empty = self.seller(lambda *_: httpx.Response(200, json={"result": {"data": [], "totals": []}}))
        self.assertEqual(empty.sales_summary("2026-10-01", "2026-10-02")["ordered_units"], 0)

    def test_stock_pagination_and_missing_scheme(self):
        def reply(path, body):
            self.assertEqual(path, STOCKS)
            if not body.get("cursor"):
                return httpx.Response(200, json={
                    "items": [{"product_id": 1, "offer_id": "one", "stocks": [
                        {"type": "fbo", "present": 3, "reserved": 2},
                        {"type": "fbs", "present": 7, "reserved": 6},
                    ]}], "cursor": "more",
                })
            self.assertEqual(body["cursor"], "more")
            return httpx.Response(200, json={
                "items": [{"product_id": 2, "offer_id": "two", "stocks": []}], "cursor": "",
            })
        result = self.seller(reply).low_stock(2, "both", 15)
        self.assertTrue(result["complete"])
        self.assertEqual(result["items"][0]["available"], 2)
        self.assertEqual(result["items_without_selected_stock"], 1)

    def test_returns_grouping_and_redaction(self):
        def reply(path, body):
            self.assertEqual(path, RETURNS)
            self.assertEqual(body["filter"]["logistic_return_date"]["time_from"], "2026-09-30T21:00:00Z")
            if "last_id" not in body:
                identifier, kind, more = 17, "FullReturn", True
            else:
                self.assertEqual(body["last_id"], 17)
                identifier, kind, more = 18, "Cancellation", False
            return httpx.Response(200, json={"returns": [{
                "id": identifier, "type": kind, "return_reason_name": "catalog reason",
                "product": {"offer_id": "one", "quantity": 3}, "order_number": "test-order-secret",
                "place": {"address": "test-address-secret"},
            }], "has_next": more})
        result = self.seller(reply).returns_summary("2026-10-01", "2026-10-02", 12)
        self.assertTrue(result["complete"])
        self.assertEqual(result["records"], 2)
        self.assertEqual({item["type"] for item in result["by_type"]}, {"FullReturn", "Cancellation"})
        self.assertNotIn("test-order-secret", str(result))
        self.assertNotIn("test-address-secret", str(result))

    def test_upstream_error_is_not_a_zero_or_raw_response(self):
        seller = self.seller(lambda *_: httpx.Response(429, json={"debug": FAKE_KEY}))
        with self.assertRaises(SellerError) as raised:
            seller.sales_summary()
        self.assertEqual(raised.exception.payload["error"], "rate_limited")
        self.assertNotIn(FAKE_KEY["api_key"], str(raised.exception.payload))

    def test_config_replace_and_permissions(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "credentials.json"
            config.save(path, FAKE_KEY)
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(config.load(path), FAKE_KEY)
            path.chmod(0o644)
            with self.assertRaises(SellerError):
                config.load(path)


if __name__ == "__main__":
    unittest.main()

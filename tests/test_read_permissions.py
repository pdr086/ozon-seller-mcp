import json
import unittest

import httpx

from ozon_seller import api_knowledge
from ozon_seller.ozon import ROLE_CACHE, Seller, SellerError


class ReadPermissionsTests(unittest.TestCase):
    def setUp(self):
        ROLE_CACHE.clear()

    def seller(self, role_methods, handler=None, api_key="synthetic-read-key"):
        self.calls = []

        def respond(request):
            self.calls.append(request.url.path)
            if request.url.path == "/v1/roles":
                return httpx.Response(200, json={"roles": [{"name": "Admin read only", "methods": role_methods}]})
            return handler(request) if handler else httpx.Response(200, json={"items": []})

        seller = Seller({"client_id": "12", "api_key": api_key}, transport=httpx.MockTransport(respond))
        self.addCleanup(seller.http.close)
        return seller

    def test_live_read_role_grants_unknown_read_method_but_blocks_mutations(self):
        seller = self.seller(["/v1/new/analytics/list", "/v1/product/archive", "/v2/chat/read", "https://other.example.test/"])
        result = seller.read_operation("/v1/new/analytics/list", {})
        self.assertTrue(result["ok"])
        for path in ["/v1/product/archive", "/v2/chat/read", "https://other.example.test/", "/v1/unknown/list"]:
            with self.assertRaises(SellerError) as error:
                seller.read_operation(path, {})
            self.assertEqual(error.exception.payload["error"], "operation_not_allowed")
        self.assertEqual(self.calls, ["/v1/roles", "/v1/new/analytics/list"])

    def test_permission_cache_is_separate_for_different_credentials(self):
        first = self.seller(["/v1/new/analytics/list"], api_key="first-synthetic-key")
        self.assertIn("/v1/new/analytics/list", first.read_methods())
        second = self.seller([], api_key="second-synthetic-key")
        with self.assertRaises(SellerError):
            second.read_operation("/v1/new/analytics/list", {})

    def test_reference_never_grants_execution_permission(self):
        seller = self.seller([])
        with self.assertRaises(SellerError):
            seller.read_operation("/v1/review/list", {})
        self.assertEqual(self.calls, ["/v1/roles"])

    def test_reference_schema_rejects_invalid_payload_before_network_call(self):
        seller = self.seller(["/v1/review/info"])
        description = seller.describe_operation("/v1/review/info")
        self.assertTrue(description["schema_available"])
        with self.assertRaises(SellerError) as error:
            seller.read_operation("/v1/review/info", {})
        self.assertEqual(error.exception.payload["error"], "invalid_arguments")
        self.assertEqual(self.calls, ["/v1/roles"])

    def test_raw_results_exclude_credentials_and_actual_api_key(self):
        def respond(_request):
            return httpx.Response(200, json={"api_key": "secret", "nested": {"access_token": "other-secret"}, "text": "synthetic-read-key"})
        seller = self.seller(["/v1/new/analytics/list"], respond)
        result = seller.read_operation("/v1/new/analytics/list", {})
        self.assertEqual(result["data"], {"nested": {}, "text": "[скрыто]"})

    def test_report_generation_and_status_checks_remain_read_only(self):
        methods = api_knowledge.readonly_methods([{"name": "Product", "methods": ["/v1/product/archive"]}, {
            "name": "Admin ReadOnly", "methods": ["/v1/report/products/create", "/v1/draft/create/info", "/v1/warehouse/create"]
        }])
        self.assertEqual(methods, frozenset({"/v1/report/products/create", "/v1/draft/create/info"}))


if __name__ == "__main__":
    unittest.main()

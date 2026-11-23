import json
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path
import httpx
from ozon_seller.store_reports import ADS, STATS, aggregate, allocate, performance, finance
from ozon_seller.ozon import SellerError


class StoreReportTests(unittest.TestCase):
    def test_forward_and_reversed_units_and_pack_size(self):
        result = aggregate([("7",2,Decimal(340)),("7",-1,Decimal(-170)),("8",3,Decimal(50))],
                           {"7":{"group":"A","variant":"plain","units_per_pack":5,"unit_cost_rub":3}})
        self.assertEqual(result["groups"]["A"], {"packs":2,"units":10,"reversed_packs":1,"known_cost_rub":"30","packs_without_cost":0})
        self.assertEqual(result["groups"]["unmapped"]["packs_without_cost"],3)

    def test_allocation_never_exceeds_spend(self):
        rule = {"first_group":"a","other_group":"b","cap_rub":100}
        self.assertEqual(allocate(Decimal("45.67"),rule),{"a":"45.67","b":"0.00"})
        self.assertEqual(allocate(Decimal("155.90"),rule),{"a":"100","b":"55.90"})

    def test_performance_separate_auth_and_reports(self):
        def handle(req):
            self.assertEqual(req.url.host,"api-performance.ozon.ru")
            if req.method=="POST":
                self.assertEqual(req.url.path,"/api/client/token")
                return httpx.Response(200,json={"access_token":"mock"})
            self.assertEqual(req.headers["Authorization"],"Bearer mock")
            self.assertEqual(req.url.params["dateFrom"],req.url.params["dateTo"])
            return httpx.Response(200,json={"rows":[{"moneySpent":"11,20","bonusSpent":"40","clicks":"3"}]})
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)
            with self.assertRaises(SellerError):
                performance(path,"2026-01-01",ADS)
            file=path/"performance.json"
            file.write_text(json.dumps({"client_id":"example","client_secret":"example"}))
            file.chmod(0o600)
            for kind in (ADS, STATS):
                result=performance(path,"2026-01-01",kind,httpx.MockTransport(handle))
                self.assertEqual(result["campaign_rows"][0]["clicks"],"3")
                if kind==ADS:self.assertEqual(result["money_spent_rub"],"11.20")

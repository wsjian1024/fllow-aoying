"""Offline regression checks for the observed pagination and data contracts."""
import unittest
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from binance_copytrade import ApiError, Client, dump, milliseconds, operation


def row(i):
    return {"symbol": "BTCUSDT", "side": "BUY", "positionSide": "LONG", "type": "MARKET",
            "executedQty": Decimal("0.12345678"), "avgPrice": Decimal("69984.80262000"),
            "totalPnl": Decimal("0.00000000"), "orderTime": i, "orderUpdateTime": i + 1}


class ClientTests(unittest.TestCase):
    def setUp(self):
        self.directory = TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.client = Client("5075281354358777856", Path(self.directory.name), interval=0)

    def test_actions_and_decimal_notional(self):
        for side, position, expected in [("BUY", "LONG", "开多"), ("SELL", "LONG", "平多"),
                                         ("SELL", "SHORT", "开空"), ("BUY", "SHORT", "平空")]:
            source = {**row(1794595907728), "side": side, "positionSide": position}
            result = operation(source, self.client.portfolio)
            self.assertEqual(result["action"], expected)
            self.assertEqual(Decimal(result["notional"]), source["executedQty"] * source["avgPrice"])
            self.assertEqual(result["time"], "2026-11-14T02:51:47.729+08:00")
        self.assertIn('"0.12345678"', dump(row(1)))

    def test_timezone(self):
        self.assertEqual(milliseconds("2026-11-08"), 1794067200000)
        self.assertEqual(milliseconds("2026-11-08T00:00:00+08:00"), milliseconds("2026-11-08"))

    def test_orders_follow_cursor_with_page_number_one(self):
        responses = [{"total": 11, "list": [row(i) for i in range(10)], "indexValue": "cursor-A"},
                     {"total": 11, "list": [row(10)], "indexValue": "cursor-B"}]
        with patch.object(self.client, "request", side_effect=responses) as request:
            result = self.client.history("orders", start=1, end=100)
        second = request.call_args_list[1].args[1]
        self.assertEqual(second["pageNumber"], 1)
        self.assertEqual(second["indexValue"], "cursor-A")
        self.assertEqual(second["startTime"], 1)
        self.assertEqual(result["count"], 11)
        self.assertTrue(result["complete"])

    def test_order_boundary_duplicate_is_removed(self):
        responses = [{"total": 11, "list": [row(i) for i in range(10)], "indexValue": "A"},
                     {"total": 11, "list": [row(9), row(10)], "indexValue": "B"}]
        with patch.object(self.client, "request", side_effect=responses):
            result = self.client.history("orders", start=1, end=100)
        self.assertEqual(result["count"], 11)
        self.assertEqual(result["rawCount"], 12)
        self.assertEqual(result["crossPageDuplicates"], 1)

    def test_positions_use_page_numbers_without_date_filter(self):
        responses = [{"total": 101, "list": [{"opened": i} for i in range(100)], "indexValue": None},
                     {"total": 101, "list": [{"opened": 100}], "indexValue": None}]
        with patch.object(self.client, "request", side_effect=responses) as request:
            result = self.client.history("positions", start=1, end=100)
        second = request.call_args_list[1].args[1]
        self.assertEqual(second["pageNumber"], 2)
        self.assertEqual(second["pageSize"], 100)
        self.assertNotIn("startTime", second)
        self.assertEqual(result["rowsByOpenedDesc"][0]["opened"], 100)

    def test_repeated_cursor_or_page_is_error(self):
        same = {"total": 30, "list": [row(i) for i in range(10)], "indexValue": "same"}
        with patch.object(self.client, "request", return_value=same):
            with self.assertRaisesRegex(ApiError, "repeated page"):
                self.client.history("orders", start=1, end=100)

    def test_incomplete_page_is_not_success(self):
        with patch.object(self.client, "request", return_value={"total": 12, "list": [row(1)]}):
            with self.assertRaisesRegex(ApiError, "expected 12"):
                self.client.history("orders", start=1, end=100)

    def test_http_200_business_error_is_not_empty_history(self):
        class Response:
            status = 200
            headers = {"Content-Type": "application/json"}
            def __enter__(self): return self
            def __exit__(self, *args): return False
            def read(self): return b'{"code":"000002","message":"illegal parameter","data":null,"success":false}'
        with patch("urllib.request.urlopen", return_value=Response()):
            with self.assertRaisesRegex(ApiError, "000002"):
                self.client.request("order-history", {"portfolioId": self.client.portfolio})


if __name__ == "__main__":
    unittest.main()

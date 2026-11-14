"""All notifications are mocked; these tests never send messages."""
import json
from decimal import Decimal
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from binance_copytrade import ApiError
from notify_wechat import PushError, Sender, Store, collect, dispatch, load_config, main, process, push_position_history, single_run


def order(i=1, qty="1.00000000"):
    return {"symbol": "BTCUSDT", "baseAsset": "BTC", "quoteAsset": "USDT", "type": "MARKET",
            "side": "BUY", "positionSide": "LONG", "executedQty": Decimal(qty),
            "avgPrice": Decimal("68025.48176000"), "totalPnl": Decimal("0.00000000"),
            "orderTime": 1794595900000 + i, "orderUpdateTime": 1794595901000 + i}


class NotifierTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cfg = load_config(Path(__file__).with_name("notify_config.example.json"))
        self.cfg.update(state_file=str(Path(self.tmp.name) / "state.sqlite3"), token="test-secret", uid="UID_test")
        self.store = Store(self.cfg["state_file"])
        self.addCleanup(self.store.db.close)
        self.store.bind(self.cfg)
        self.now = 1794637000.0

    def queued(self):
        return self.store.db.execute("SELECT * FROM outbox ORDER BY id").fetchall()

    def queue(self, key="a", kind="changes", expires=None):
        with self.store.db:
            self.store.enqueue(kind, key, "测试标题", "测试内容", self.now, expires)

    def send(self, sender, now=None):
        return dispatch(self.store, self.cfg, sender, clock=lambda: self.now if now is None else now)

    def test_first_run_is_baseline_and_identical_runs_are_silent(self):
        self.assertEqual(self.store.ingest(self.cfg, [order()], self.now), [])
        self.assertEqual(self.store.ingest(self.cfg, [order()], self.now + 60), [])
        self.assertEqual(self.queued(), [])
        self.assertEqual(self.store.get("last_fetch_at"), self.now + 60)

    def test_new_and_updated_orders_are_queued_once(self):
        self.store.ingest(self.cfg, [order()], self.now)
        changes = self.store.ingest(self.cfg, [order(), order(2)], self.now + 60)
        self.assertEqual(changes[0][0], "新操作")
        changes = self.store.ingest(self.cfg, [order(2, "2"), order()], self.now + 120)
        self.assertEqual(changes[0][0], "记录更新")
        self.store.ingest(self.cfg, [order(2, "2"), order()], self.now + 180)
        self.assertEqual(len(self.queued()), 2)

    def test_decimal_precision_spelling_is_not_a_change(self):
        self.store.ingest(self.cfg, [order(qty="1.0000")], self.now)
        self.assertEqual(self.store.ingest(self.cfg, [order(qty="1.0")], self.now + 60), [])

    def test_source_failure_does_not_advance_baseline(self):
        self.store.ingest(self.cfg, [order()], self.now)
        client = Mock()
        client.history.side_effect = ApiError("busy")
        with self.assertRaises(ApiError):
            process(self.store, self.cfg, client, self.now + 60)
        self.assertEqual(self.store.get("last_fetch_at"), self.now)
        self.assertEqual(self.queued(), [])

    def test_collect_catches_up_after_downtime(self):
        with self.store.db:
            self.store.put("last_fetch_at", self.now - 86400 * 3)
        client = Mock()
        client.history.return_value = {"rows": []}
        collect(self.store, self.cfg, client, self.now)
        self.assertEqual(client.history.call_args.kwargs["start"], int((self.now - 86400 * 5) * 1000))
        with self.store.db:
            self.store.put("last_fetch_at", self.now - 86400 * 31)
        with self.assertRaises(ApiError):
            collect(self.store, self.cfg, client, self.now)

    def test_accepted_outbox_is_not_sent_twice(self):
        self.queue()
        sender = Mock()
        sender.send.return_value = "receipt-1"
        self.assertEqual(self.send(sender), 1)
        self.assertEqual(self.send(sender, self.now + 60), 0)
        self.assertEqual(sender.send.call_count, 1)
        self.assertEqual(self.queued()[0]["status"], "accepted")

    def test_definite_rejection_retries_after_backoff(self):
        self.queue()
        sender = Mock()
        sender.send.side_effect = PushError("rejected")
        self.send(sender)
        self.assertEqual(self.queued()[0]["status"], "pending")
        self.send(sender, self.now + 60)
        self.assertEqual(sender.send.call_count, 1)
        sender.send.side_effect = None
        sender.send.return_value = "receipt"
        self.send(sender, self.now + 121)
        self.assertEqual(self.queued()[0]["status"], "accepted")

    def test_unknown_result_never_retries_automatically(self):
        self.queue()
        sender = Mock()
        sender.send.side_effect = PushError("timeout", uncertain=True)
        self.send(sender)
        self.send(sender, self.now + 86400)
        self.assertEqual(sender.send.call_count, 1)
        self.assertEqual(self.queued()[0]["status"], "unknown")

    def test_interrupted_send_becomes_unknown(self):
        self.queue()
        with self.store.db:
            self.store.db.execute("UPDATE outbox SET status='sending'")
        self.store.recover_inflight()
        self.assertEqual(self.queued()[0]["status"], "unknown")

    def test_reminder_once_per_actual_activation_and_no_send_renewal(self):
        activation = self.now - 47 * 3600
        self.store.mark_active(activation)
        client = Mock()
        client.history.return_value = {"rows": []}
        process(self.store, self.cfg, client, self.now)
        self.assertEqual(len(self.queued()), 1)
        self.assertIn("不是当前实时持仓", self.queued()[0]["content"])
        self.send(Mock(send=Mock(return_value="receipt")))
        self.assertEqual(self.store.get("activated_at"), activation)
        process(self.store, self.cfg, client, self.now + 60)
        self.assertEqual(len(self.queued()), 1)
        self.assertFalse(self.store.reminder_due(self.cfg, self.now + 3601))

    def test_no_activation_means_no_invented_deadline(self):
        self.assertFalse(self.store.reminder_due(self.cfg, self.now))

    def test_position_failure_does_not_block_order_change(self):
        self.store.ingest(self.cfg, [order()], self.now - 60)
        self.store.mark_active(self.now - 47 * 3600)
        client = Mock()
        client.history.side_effect = [{"rows": [order(), order(2)]}, ApiError("positions unavailable")]
        process(self.store, self.cfg, client, self.now)
        self.assertEqual([r["kind"] for r in self.queued()], ["changes"])
        self.assertTrue(self.store.reminder_due(self.cfg, self.now))

    def test_reactivation_supersedes_old_pending_reminder(self):
        self.queue("old", "reminder", self.now + 600)
        self.store.mark_active(self.now)
        self.assertEqual(self.queued()[0]["status"], "superseded")

    def test_expired_reminder_not_sent(self):
        self.queue("old", "reminder", self.now - 1)
        sender = Mock()
        self.send(sender)
        sender.send.assert_not_called()
        self.assertEqual(self.queued()[0]["status"], "expired")

    def test_wxpusher_unknown_result_reserves_tenth_slot(self):
        self.cfg["provider"] = "wxpusher"
        self.store.mark_active(self.now - 60)
        with self.store.db:
            self.store.put("activation_count", 9)
        self.queue("a")
        self.queue("b")
        sender = Mock()
        sender.send.side_effect = PushError("timeout", uncertain=True)
        self.send(sender)
        self.send(sender, self.now + 60)
        self.assertEqual(self.store.get("activation_count"), 10)
        self.assertEqual(sender.send.call_count, 1)

    def test_wxpusher_definite_rejection_releases_reserved_slot(self):
        self.cfg["provider"] = "wxpusher"
        self.store.mark_active(self.now - 60)
        self.queue()
        sender = Mock()
        sender.send.side_effect = PushError("rejected")
        self.send(sender)
        self.assertEqual(self.store.get("activation_count"), 0)

    def test_dispatch_refreshes_clock_between_messages(self):
        self.cfg["provider"] = "wxpusher"
        self.store.mark_active(self.now - 24 * 3600 + 10)
        self.queue("a")
        self.queue("b")
        sender = Mock(send=Mock(return_value="receipt"))
        times = iter([self.now, self.now, self.now + 20, self.now + 20])
        dispatch(self.store, self.cfg, sender, clock=lambda: next(times))
        self.assertEqual(sender.send.call_count, 1)
        self.assertEqual(self.queued()[0]["accepted_at"], self.now + 20)
        self.assertEqual(self.queued()[1]["status"], "pending")

    def test_dry_run_does_not_modify_disk_state(self):
        self.store.ingest(self.cfg, [order()], self.now)
        dry = Store(self.cfg["state_file"], dry_run=True)
        try:
            dry.ingest(self.cfg, [order(), order(2)], self.now + 60)
        finally:
            dry.db.close()
        self.assertEqual(self.queued(), [])
        self.assertEqual(self.store.get("last_fetch_at"), self.now)
        self.assertEqual(self.store.db.execute("SELECT count(*) FROM events").fetchone()[0], 1)

    def test_overlapping_runs_do_not_both_acquire_lock(self):
        with single_run(self.cfg["state_file"]) as first:
            with single_run(self.cfg["state_file"]) as second:
                self.assertTrue(first)
                self.assertFalse(second)

    def test_manual_action_lock_conflict_is_failure(self):
        with single_run(self.cfg["state_file"]):
            with patch("notify_wechat.load_config", return_value=self.cfg), patch("sys.argv", ["notify_wechat.py", "--mark-active-now"]):
                self.assertEqual(main(), 1)
        self.assertIsNone(self.store.get("activated_at"))

    def test_receiver_binding_prevents_using_another_state(self):
        with self.assertRaises(ValueError):
            self.store.bind({**self.cfg, "portfolio_id": "123"})

    def position_client(self, count=12):
        rows = [{"symbol": f"COIN{i}USDT", "side": "Long", "leverage": "10", "status": "All Closed",
                 "opened": 1794500000000 + i, "closed": 1794590000000 + i,
                 "avgCost": Decimal("100"), "avgClosePrice": Decimal("110"),
                 "closedVolume": Decimal("2"), "closingPnl": Decimal("20")}
                for i in range(count)]
        return Mock(history=Mock(return_value={"rows": rows}))

    def test_manual_positions_latest_ten_without_changing_baseline(self):
        self.store.ingest(self.cfg, [order()], self.now - 60)
        ids = push_position_history(self.store, self.cfg, self.position_client(), self.now)
        self.assertEqual(len(ids), 1)
        content = self.queued()[0]["content"]
        self.assertIn("本次选取 10 条，接口共 12 条", content)
        self.assertLess(content.index("COIN11USDT"), content.index("COIN2USDT"))
        self.assertNotIn("COIN1USDT", content)
        self.assertIn("非当前实时持仓", content)
        self.assertIn("平仓盈亏 20 USDT", content)
        self.assertEqual(self.store.get("last_fetch_at"), self.now - 60)
        self.assertEqual(self.store.db.execute("SELECT count(*) FROM events").fetchone()[0], 1)

    def test_manual_positions_all_split_and_isolate_dispatch(self):
        self.queue("unrelated")
        ids = push_position_history(self.store, self.cfg, self.position_client(21), self.now, limit=0)
        self.assertEqual(len(ids), 3)
        sender = Mock(send=Mock(return_value="receipt"))
        self.assertEqual(dispatch(self.store, self.cfg, sender, clock=lambda: self.now, message_ids=ids), 3)
        self.assertEqual(self.queued()[0]["status"], "pending")
        self.assertTrue(all(r["status"] == "accepted" for r in self.queued()[1:]))

    def test_manual_positions_preview_does_not_enqueue(self):
        with patch("builtins.print"):
            self.assertEqual(push_position_history(self.store, self.cfg, self.position_client(), self.now, dry_run=True), [])
        self.assertEqual(self.queued(), [])
        self.assertIsNone(self.store.get("initialized"))

    def test_manual_positions_fetch_failure_leaves_queue_unchanged(self):
        client = Mock(history=Mock(side_effect=ApiError("busy")))
        with self.assertRaises(ApiError):
            push_position_history(self.store, self.cfg, client, self.now)
        self.assertEqual(self.queued(), [])

    def test_manual_positions_cli_preview_never_constructs_sender(self):
        with patch("notify_wechat.load_config", return_value=self.cfg), \
             patch("notify_wechat.Client", return_value=self.position_client()), \
             patch("notify_wechat.Sender") as sender, patch("builtins.print"), \
             patch("sys.argv", ["notify_wechat.py", "--push-positions", "5", "--dry-run"]):
            self.assertEqual(main(), 0)
        sender.assert_not_called()
        self.assertEqual(self.queued(), [])

    def provider_response(self, payload):
        response = Mock()
        response.status = 200
        response.read.return_value = json.dumps(payload).encode()
        context = Mock()
        context.__enter__ = Mock(return_value=response)
        context.__exit__ = Mock(return_value=False)
        return patch("urllib.request.urlopen", return_value=context)

    def test_pushplus_protocol(self):
        with self.provider_response({"code": 200, "data": "short-code"}) as http:
            self.assertEqual(Sender(self.cfg).send("title", "body"), "short-code")
        body = json.loads(http.call_args.args[0].data)
        self.assertEqual(body["channel"], "wechat")
        self.assertEqual(body["template"], "txt")

    def test_wxpusher_checks_individual_target(self):
        self.cfg["provider"] = "wxpusher"
        for target_code in [1001, None]:
            with self.provider_response({"code": 1000, "data": [{"uid": "UID_test", "code": target_code}]}):
                with self.assertRaises(PushError) as caught:
                    Sender(self.cfg).send("title", "body")
                self.assertEqual(caught.exception.uncertain, target_code is None)
        with self.provider_response({"code": 1000, "data": [{"uid": "UID_test", "code": 1000, "messageContentId": 42}]}):
            self.assertEqual(Sender(self.cfg).send("title", "body"), "42")

    def test_malformed_success_is_unknown(self):
        for provider in ["pushplus", "wxpusher"]:
            self.cfg["provider"] = provider
            with self.provider_response({}):
                with self.assertRaises(PushError) as caught:
                    Sender(self.cfg).send("title", "body")
                self.assertTrue(caught.exception.uncertain)


if __name__ == "__main__":
    unittest.main()

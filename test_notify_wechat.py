"""All notifications are mocked; these tests never send messages."""
import json
from decimal import Decimal
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from binance_copytrade import ApiError
from notify_wechat import WECOM_TEXT_LIMIT, PushError, Sender, Store, collect, dispatch, load_config, main, process, push_position_history, single_run, validate_credentials


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
        self.cfg.update(state_file=str(Path(self.tmp.name) / "state.sqlite3"), key="test-secret")
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

    def test_scheduled_collection_only_requests_orders_and_no_keepalive(self):
        self.store.ingest(self.cfg, [order()], self.now - 60)
        with self.store.db:
            self.store.put("activated_at", self.now - 47 * 3600)
        client = Mock(history=Mock(return_value={"rows": [order()]}))
        sender = Mock(send=Mock(return_value="receipt"))
        process(self.store, self.cfg, client, self.now)
        self.send(sender)
        sender.send.assert_not_called()
        self.assertEqual(self.queued(), [])
        client.history.assert_called_once()
        self.assertEqual(client.history.call_args.args, ("orders",))
        client.history.return_value = {"rows": [order(), order(2)]}
        process(self.store, self.cfg, client, self.now + 60)
        self.assertEqual([r["kind"] for r in self.queued()], ["changes"])
        self.assertTrue(all(c.args == ("orders",) for c in client.history.call_args_list))
        self.assertEqual(self.send(sender, self.now + 60), 1)

    def test_legacy_reminders_cancelled_without_resetting_trade_progress(self):
        self.store.ingest(self.cfg, [order()], self.now)
        self.queue("trade")
        for state in ("pending", "failed", "unknown", "sending", "accepted"):
            self.queue(state, "reminder", self.now + 600)
            with self.store.db:
                self.store.db.execute("UPDATE outbox SET status=? WHERE unique_key=?", (state, state))
        with self.store.db:
            self.store.put("activated_at", self.now - 60)
            self.store.put("activation_count", 9)
        self.store.bind(self.cfg)
        self.store.bind(self.cfg)
        self.assertEqual([r["status"] for r in self.queued()],
                         ["pending", "cancelled", "cancelled", "cancelled", "cancelled", "accepted"])
        self.assertIsNone(self.store.get("activated_at"))
        self.assertIsNone(self.store.get("activation_count"))
        self.assertTrue(self.store.get("initialized"))
        self.assertEqual(self.store.get("last_fetch_at"), self.now)
        self.assertEqual(self.store.db.execute("SELECT count(*) FROM events").fetchone()[0], 1)
        self.assertEqual(self.send(Mock(send=Mock(return_value="receipt"))), 1)

    def test_dispatch_never_sends_legacy_reminders_even_without_migration(self):
        self.queue("old", "reminder", self.now + 600)
        sender = Mock()
        self.send(sender)
        dispatch(self.store, self.cfg, sender, clock=lambda: self.now,
                 message_ids=[self.queued()[0]["id"]])
        sender.send.assert_not_called()

    def test_old_config_reminder_settings_are_ignored(self):
        config = Path(self.tmp.name) / "legacy.json"
        config.write_text(json.dumps({"reminder_enabled": True, "reminder_lead_hours": -99,
                                      "window_hours": 0}), encoding="utf-8")
        loaded = load_config(config)
        self.assertNotIn("reminder_enabled", loaded)
        self.assertNotIn("reminder_lead_hours", loaded)
        self.assertNotIn("window_hours", loaded)

    def test_dispatch_refreshes_clock_between_messages(self):
        self.queue("a")
        self.queue("b", expires=self.now + 10)
        sender = Mock(send=Mock(return_value="receipt"))
        times = iter([self.now, self.now, self.now + 20, self.now + 20])
        dispatch(self.store, self.cfg, sender, clock=lambda: next(times))
        self.assertEqual(sender.send.call_count, 1)
        self.assertEqual(self.queued()[0]["accepted_at"], self.now + 20)
        self.assertEqual(self.queued()[1]["status"], "expired")

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
            with patch("notify_wechat.load_config", return_value=self.cfg), patch("sys.argv", ["notify_wechat.py", "--test-push"]):
                self.assertEqual(main(), 1)
        self.assertEqual(self.queued(), [])

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
        self.assertEqual(len(ids), 2)  # 10 rows exceed one 2048-byte WeCom text message
        content = "\n".join(r["content"] for r in self.queued())
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

    def test_wecom_protocol(self):
        with self.provider_response({"errcode": 0, "errmsg": "ok"}) as http:
            self.assertEqual(Sender(self.cfg).send("title", "body"), "ok")
        request = http.call_args.args[0]
        self.assertEqual(request.full_url, "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=test-secret")
        self.assertEqual(json.loads(request.data), {"msgtype": "text", "text": {"content": "title\nbody"}})
        with self.provider_response({"errcode": 0, "errmsg": "ok"}) as http:
            Sender(self.cfg).send("title", "title\nbody")
        self.assertEqual(json.loads(http.call_args.args[0].data)["text"]["content"], "title\nbody")

    def test_wecom_rejection_is_definite(self):
        with self.provider_response({"errcode": 45009, "errmsg": "api freq out of limit"}):
            with self.assertRaises(PushError) as caught:
                Sender(self.cfg).send("title", "body")
        self.assertFalse(caught.exception.uncertain)
        self.assertIn("45009", str(caught.exception))

    def test_malformed_success_is_unknown(self):
        for payload in [{}, {"errcode": "0"}, []]:
            with self.provider_response(payload):
                with self.assertRaises(PushError) as caught:
                    Sender(self.cfg).send("title", "body")
                self.assertTrue(caught.exception.uncertain)

    def test_oversized_message_is_rejected_before_sending(self):
        with patch("urllib.request.urlopen") as http:
            with self.assertRaises(PushError) as caught:
                Sender(self.cfg).send("title", "字" * 700)
        http.assert_not_called()
        self.assertFalse(caught.exception.uncertain)

    def test_wecom_key_from_url_env_and_validation(self):
        config = Path(self.tmp.name) / "wecom.json"
        url = "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=abc-123"
        config.write_text(json.dumps({"wecom": {"key": url}, "pushplus": {"token": "old"}}), encoding="utf-8")
        loaded = load_config(config)
        self.assertEqual(loaded["key"], "abc-123")
        self.assertNotIn("pushplus", loaded)
        with patch.dict("os.environ", {"WECOM_WEBHOOK_KEY": "from-env"}):
            self.assertEqual(load_config(config)["key"], "from-env")
        validate_credentials(loaded)
        for bad in ["", "xxx", "a b", "k&x=1"]:
            with self.assertRaises(ValueError):
                validate_credentials({**loaded, "key": bad})
        config.write_text(json.dumps({"provider": "pushplus"}), encoding="utf-8")
        with self.assertRaises(ValueError):
            load_config(config)

    def test_legacy_provider_state_is_migrated(self):
        for provider, uid in [("pushplus", ""), ("wxpusher", "UID_x")]:
            store = Store(str(Path(self.tmp.name) / f"{provider}.sqlite3"))
            self.addCleanup(store.db.close)
            with store.db:
                store.put("binding", [self.cfg["portfolio_id"], provider, self.cfg["recipient_id"], uid])
            store.bind(self.cfg)
            self.assertEqual(store.get("binding"), [self.cfg["portfolio_id"], "wecom", self.cfg["recipient_id"], ""])
        with self.store.db:
            self.store.put("binding", [self.cfg["portfolio_id"], "pushplus", "someone-else", ""])
        with self.assertRaises(ValueError):
            self.store.bind(self.cfg)

    def test_messages_fit_wecom_text_limit(self):
        self.store.ingest(self.cfg, [order()], self.now)
        self.store.ingest(self.cfg, [order()] + [order(i) for i in range(2, 40)], self.now + 60)
        push_position_history(self.store, self.cfg, self.position_client(40), self.now, limit=0)
        rows = self.queued()
        self.assertTrue(all(len(r["content"].encode()) <= WECOM_TEXT_LIMIT for r in rows))
        changes = [r for r in rows if r["kind"] == "changes"]
        self.assertEqual(sum(int(r["title"].split("｜")[1].split("条")[0]) for r in changes), 38)
        positions = [r for r in rows if r["kind"] == "positions"]
        self.assertGreater(len(positions), 4)
        self.assertTrue(positions[-1]["title"].endswith(f"（{len(positions)}/{len(positions)}）"))


if __name__ == "__main__":
    unittest.main()

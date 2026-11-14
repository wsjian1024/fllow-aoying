#!/usr/bin/env python3
"""One cron invocation: collect changes, persist an outbox, submit WeChat notifications.

Python 3.10+, standard library only, Linux/macOS. Provider acceptance is not delivery.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from decimal import Decimal
import fcntl
import json
import os
from pathlib import Path
import sqlite3
import sys
import time
import urllib.error
import urllib.request
import uuid

from binance_copytrade import ACTION, ApiError, Client, DEFAULT_PORTFOLIO, dump, local_time, record_hash


class PushError(RuntimeError):
    def __init__(self, message: str, *, uncertain: bool = False):
        super().__init__(message)
        self.uncertain = uncertain


def load_config(path: Path) -> dict:
    raw = json.loads(path.read_text(encoding="utf-8"))
    # Ignore retired settings in older deployment configs.
    for key in ("reminder_enabled", "reminder_lead_hours", "window_hours"):
        raw.pop(key, None)
    cfg = {"portfolio_id": DEFAULT_PORTFOLIO, "name": "熬鹰资本", "provider": "pushplus",
           "recipient_id": "my-wechat", "state_file": "state/wechat.sqlite3", "lookback_hours": 48,
           "max_catchup_days": 30, "max_pages": 100, "max_messages_per_run": 3,
           "pushplus": {}, "wxpusher": {}, **raw}
    if cfg["provider"] not in ("pushplus", "wxpusher"):
        raise ValueError("provider must be pushplus or wxpusher")
    if not isinstance(cfg["portfolio_id"], str) or not cfg["portfolio_id"].isdecimal():
        raise ValueError("portfolio_id must be a decimal STRING")
    for key in ("lookback_hours", "max_catchup_days", "max_pages", "max_messages_per_run"):
        if not isinstance(cfg[key], (int, float)) or isinstance(cfg[key], bool) or cfg[key] <= 0:
            raise ValueError(f"{key} must be positive")
    for key in ("max_pages", "max_messages_per_run"):
        if not isinstance(cfg[key], int):
            raise ValueError(f"{key} must be an integer")
    if cfg["max_messages_per_run"] > 3:
        raise ValueError("max_messages_per_run must be <= 3")
    if cfg["lookback_hours"] > cfg["max_catchup_days"] * 24:
        raise ValueError("lookback_hours exceeds max_catchup_days")
    cfg["state_file"] = str((path.resolve().parent / cfg["state_file"]).resolve())
    section = cfg[cfg["provider"]]
    env = section.get("token_env", "PUSHPLUS_TOKEN" if cfg["provider"] == "pushplus" else "WXPUSHER_APP_TOKEN")
    cfg["token"] = os.environ.get(env) or section.get("token", "")
    cfg["uid"] = os.environ.get("WXPUSHER_UID") or cfg["wxpusher"].get("uid", "")
    return cfg


def validate_credentials(cfg: dict):
    if not cfg["token"] or "填写" in cfg["token"] or "xxx" in cfg["token"]:
        raise ValueError("请在配置文件或指定环境变量中填写推送 token；--dry-run 无需凭据")
    if cfg["provider"] == "wxpusher" and (not cfg["uid"].startswith("UID_") or "xxx" in cfg["uid"]):
        raise ValueError("WxPusher 需要自己的 UID；--dry-run 无需凭据")


@contextmanager
def single_run(state_file: str):
    path = Path(state_file)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(str(path) + ".lock", "a", encoding="utf-8") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield False
            return
        try:
            yield True
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


class Store:
    def __init__(self, path: str, *, dry_run: bool = False):
        if dry_run:
            self.db = sqlite3.connect(":memory:")
            if Path(path).exists():
                source = sqlite3.connect(Path(path).as_uri() + "?mode=ro", uri=True)
                try:
                    source.backup(self.db)
                finally:
                    source.close()
        else:
            self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS events (
                fingerprint TEXT PRIMARY KEY, group_key TEXT NOT NULL,
                record TEXT NOT NULL, first_seen REAL NOT NULL);
            CREATE INDEX IF NOT EXISTS event_group ON events(group_key);
            CREATE TABLE IF NOT EXISTS outbox (
                id INTEGER PRIMARY KEY, kind TEXT NOT NULL, unique_key TEXT UNIQUE,
                title TEXT NOT NULL, content TEXT NOT NULL, created REAL NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending', attempts INTEGER NOT NULL DEFAULT 0,
                next_attempt REAL NOT NULL DEFAULT 0, receipt TEXT, error TEXT,
                accepted_at REAL, expires_at REAL);
        """)

    def get(self, key: str, default=None):
        row = self.db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else default

    def put(self, key: str, value):
        self.db.execute("INSERT OR REPLACE INTO meta VALUES (?,?)", (key, json.dumps(value)))

    def bind(self, cfg: dict):
        # Token rotation is allowed. A different recipient must use a different recipient_id / DB.
        binding = [cfg["portfolio_id"], cfg["provider"], cfg["recipient_id"], cfg["uid"] if cfg["provider"] == "wxpusher" else ""]
        old = self.get("binding")
        if old is not None and old != binding:
            raise ValueError("状态库属于其他组合或接收人；请为新配置使用另一个 state_file")
        with self.db:
            self.put("binding", binding)
            # Retire queued reminders without changing trade progress or sent records.
            self.db.execute("UPDATE outbox SET status='cancelled' WHERE kind='reminder' AND status IN ('pending','failed','unknown','sending')")
            self.db.execute("DELETE FROM meta WHERE key IN ('activated_at','activation_count')")

    def enqueue(self, kind: str, key: str, title: str, content: str, now: float, expires=None):
        cursor = self.db.execute("INSERT OR IGNORE INTO outbox(kind,unique_key,title,content,created,expires_at) VALUES (?,?,?,?,?,?)",
                                 (kind, key, title, content, now, expires))
        return cursor.lastrowid if cursor.rowcount else None

    def ingest(self, cfg: dict, rows: list[dict], now: float) -> list[tuple[str, dict]]:
        first = not self.get("initialized", False)
        changes = []
        with self.db:
            for row in sorted(rows, key=lambda r: (r["orderUpdateTime"], r["orderTime"])):
                # Decimal spelling changes such as 1.0 vs 1.000 must not create new events.
                normalized = {k: format(v.normalize(), "f") if isinstance(v, Decimal) else v for k, v in row.items()}
                fingerprint = record_hash(normalized)
                if self.db.execute("SELECT 1 FROM events WHERE fingerprint=?", (fingerprint,)).fetchone():
                    continue
                group = record_hash([cfg["portfolio_id"], row["symbol"], row["side"], row["positionSide"], row["orderTime"]])
                known = self.db.execute("SELECT 1 FROM events WHERE group_key=?", (group,)).fetchone()
                self.db.execute("INSERT INTO events VALUES (?,?,?,?)", (fingerprint, group, dump(row), now))
                if not first:
                    changes.append(("记录更新" if known else "新操作", row))
            for offset in range(0, len(changes), 10):
                chunk = changes[offset:offset + 10]
                title = f"{cfg['name']}｜{len(chunk)}条交易变化"
                body = [title, f"采集时间：{local_time(int(now * 1000))}", ""]
                for label, row in chunk:
                    body.extend([label + " · " + format_order(row), ""])
                body.extend(["同毫秒订单缺少唯一ID，‘记录更新’为候选匹配。", portfolio_url(cfg)])
                self.enqueue("changes", "changes:" + record_hash(chunk), title, "\n".join(body), now)
            self.put("initialized", True)
            self.put("last_fetch_at", now)
        return changes

    def recover_inflight(self):
        # A crash after sending but before recording the response is ambiguous; do not blindly resend.
        with self.db:
            self.db.execute("UPDATE outbox SET status='unknown',error='上次发送过程中中断；请核对微信后决定是否重试' WHERE status='sending'")


def portfolio_url(cfg: dict) -> str:
    return f"https://www.binance.com/zh-CN/copy-trading/lead-details/{cfg['portfolio_id']}"


def number(value) -> str:
    text = format(Decimal(str(value)), ",f")
    return text.rstrip("0").rstrip(".") if "." in text else text


def format_order(row: dict) -> str:
    action = ACTION.get((row["side"], row["positionSide"]), "未知方向")
    return (f"{local_time(row['orderUpdateTime'])}\n{row['symbol']} {action}"
            f"\n数量 {number(row['executedQty'])}｜均价 {number(row['avgPrice'])}"
            f"\n操作盈亏 {number(row['totalPnl'])} USDT")


def push_position_history(store: Store, cfg: dict, client: Client, now: float,
                          limit: int = 10, *, dry_run: bool = False) -> list[int]:
    if limit < 0:
        raise ValueError("仓位条数必须大于等于 0；0 表示全部")
    result = client.history("positions", start=0, end=int(now * 1000), max_pages=cfg["max_pages"])
    rows = sorted(result["rows"], key=lambda r: r["closed"], reverse=True)
    selected = rows[:limit] if limit else rows
    chunks = [selected[i:i + 10] for i in range(0, len(selected), 10)] or [[]]
    messages = []
    for index, chunk in enumerate(chunks, 1):
        suffix = f"（{index}/{len(chunks)}）" if len(chunks) > 1 else ""
        title = f"{cfg['name']}｜仓位历史{suffix}"
        lines = [title, f"查询时间：{local_time(int(now * 1000))}",
                 f"按平仓时间倒序，本次选取 {len(selected)} 条，接口共 {len(rows)} 条。",
                 "历史仓位记录（非当前实时持仓）", ""]
        for row in chunk:
            side = {"Long": "做多", "Short": "做空"}.get(row["side"], row["side"])
            state = {"All Closed": "全部平仓"}.get(row.get("status"), row.get("status", ""))
            lines.extend([f"{row['symbol']} {side} {row['leverage']}x｜{state}",
                          f"开仓：{local_time(row['opened'])}",
                          f"平仓：{local_time(row['closed'])}",
                          f"开仓均价 {number(row['avgCost'])}｜平仓均价 {number(row['avgClosePrice'])}",
                          f"已平数量 {number(row['closedVolume'])}｜平仓盈亏 {number(row['closingPnl'])} USDT", ""])
        if not rows:
            lines.append("当前接口没有返回仓位历史。")
        lines.append(portfolio_url(cfg))
        messages.append((title, "\n".join(lines)))
    if dry_run:
        for title, content in messages:
            print(f"\n[仓位历史预览] {title}\n{content}")
        print("dry-run：未向微信发送，未修改磁盘状态。")
        return []
    batch = uuid.uuid4().hex
    with store.db:
        ids = [store.enqueue("positions", f"positions:{batch}:{i}", title, content, now)
               for i, (title, content) in enumerate(messages)]
    print(f"仓位历史已入队：{len(selected)}/{len(rows)} 条记录，{len(ids)} 条消息。")
    return ids


class Sender:
    def __init__(self, cfg: dict):
        self.cfg = cfg

    def send(self, title: str, content: str) -> str:
        cfg = self.cfg
        if cfg["provider"] == "pushplus":
            url = "https://www.pushplus.plus/send"
            body = {"token": cfg["token"], "title": title, "content": content, "template": "txt", "channel": "wechat"}
        else:
            url = "https://wxpusher.zjiecode.com/api/send/message"
            body = {"appToken": cfg["token"], "uids": [cfg["uid"]], "content": content,
                    "contentType": 1, "summary": title[:100]}
        req = urllib.request.Request(url, data=json.dumps(body, ensure_ascii=False).encode(),
                                     headers={"Content-Type": "application/json", "User-Agent": "BinanceHistoryNotifier/1.0"})
        try:
            with urllib.request.urlopen(req, timeout=20) as response:
                raw = response.read()
                if response.status != 200:
                    raise PushError(f"HTTP {response.status}", uncertain=True)
        except urllib.error.HTTPError as exc:
            # A 5xx response might follow provider-side acceptance. Never assume it is safe to resend.
            raise PushError(f"推送 HTTP {exc.code}", uncertain=exc.code >= 500) from None
        except (urllib.error.URLError, TimeoutError, OSError):
            raise PushError("推送网络响应未知，请核对微信或服务后台", uncertain=True) from None
        try:
            response = json.loads(raw)
            if not isinstance(response, dict) or type(response.get("code")) is not int:
                raise PushError("推送缺少明确业务结果，是否受理未知", uncertain=True)
            if cfg["provider"] == "pushplus":
                if response.get("code") != 200:
                    raise PushError(f"PushPlus 拒绝，业务码 {response.get('code')}")
                if not isinstance(response.get("data"), str) or not response["data"]:
                    raise PushError("PushPlus 已受理但未返回有效流水号", uncertain=True)
                return response["data"]
            if response.get("code") != 1000:
                raise PushError(f"WxPusher 拒绝，业务码 {response.get('code')}")
            target = next((r for r in response.get("data", []) if r.get("uid") == cfg["uid"]), None)
            if target is None:
                raise PushError("WxPusher 未返回目标 UID 的结果", uncertain=True)
            if type(target.get("code")) is not int:
                raise PushError("WxPusher 目标缺少明确业务结果", uncertain=True)
            if target.get("code") != 1000:
                raise PushError(f"WxPusher 目标被拒绝，业务码 {target.get('code')}")
            return str(target.get("messageContentId") or target.get("messageId") or "accepted")
        except (ValueError, TypeError, AttributeError):
            raise PushError("推送返回无法解析，是否受理未知", uncertain=True) from None


def dispatch(store: Store, cfg: dict, sender: Sender, *, clock=time.time,
             message_ids: list[int] | None = None) -> int:
    now = clock()
    if message_ids == []:
        return 0
    scope = "" if message_ids is None else " AND id IN (" + ",".join("?" for _ in message_ids) + ")"
    parameters = [now, *(message_ids or []), cfg["max_messages_per_run"]]
    messages = store.db.execute("SELECT * FROM outbox WHERE status='pending' AND kind!='reminder' AND next_attempt<=?" + scope + " ORDER BY id LIMIT ?",
                                parameters).fetchall()
    accepted = 0
    for message in messages:
        now = clock()
        if message["expires_at"] is not None and now >= message["expires_at"]:
            with store.db:
                store.db.execute("UPDATE outbox SET status='expired' WHERE id=?", (message["id"],))
            continue
        with store.db:
            store.db.execute("UPDATE outbox SET status='sending',attempts=attempts+1 WHERE id=?", (message["id"],))
        try:
            receipt = sender.send(message["title"], message["content"])
        except PushError as exc:
            attempts = message["attempts"] + 1
            status = "unknown" if exc.uncertain else ("failed" if attempts >= 5 else "pending")
            with store.db:
                store.db.execute("UPDATE outbox SET status=?,error=?,next_attempt=? WHERE id=?",
                                 (status, str(exc), clock() + min(3600, 60 * 2 ** attempts), message["id"]))
            print(f"推送 {message['id']}: {status}，{exc}", file=sys.stderr)
            break
        accepted_at = clock()
        with store.db:
            store.db.execute("UPDATE outbox SET status='accepted',receipt=?,accepted_at=?,error=NULL WHERE id=?",
                             (receipt, accepted_at, message["id"]))
            store.put("last_accepted_at", accepted_at)
        accepted += 1
        print(f"推送 {message['id']}: 服务已受理（不代表微信已送达）")
    return accepted


def collect(store: Store, cfg: dict, client: Client, now: float) -> list[dict]:
    last = store.get("last_fetch_at", now)
    if now < last:
        raise ApiError("系统时钟早于上次采集时间，未推进状态")
    start = min(now, last) - cfg["lookback_hours"] * 3600
    if now - start > cfg["max_catchup_days"] * 86400:
        raise ApiError("采集中断过久，超过 max_catchup_days；请调整配置后补齐，不会静默跳过")
    orders = client.history("orders", start=int(start * 1000), end=int(now * 1000),
                            max_pages=cfg["max_pages"], page_size=50)["rows"]
    return orders


def process(store: Store, cfg: dict, client: Client, now: float, *, dry_run: bool = False) -> int:
    first = not store.get("initialized", False)
    orders = collect(store, cfg, client, now)
    changes = store.ingest(cfg, orders, now)
    print(f"{'首次建立基线' if first else '采集完成'}：{len(orders)}条记录，{len(changes)}条新变化。")
    if first:
        print("现有历史已记为基线，不推送旧记录。")
    if dry_run:
        for row in store.db.execute("SELECT title,content,status FROM outbox WHERE status IN ('pending','unknown','failed') ORDER BY id"):
            print(f"\n[预览/{row['status']}] {row['title']}\n{row['content']}")
        print("dry-run：未向微信发送，未修改磁盘状态。")
    return len(changes)


def status(store: Store, cfg: dict):
    print(dump({"initialized": store.get("initialized", False),
                "last_fetch": local_time(int(store.get("last_fetch_at", 0) * 1000)) if store.get("last_fetch_at") else None,
                "outbox": {r[0]: r[1] for r in store.db.execute("SELECT status,count(*) FROM outbox GROUP BY status")},
                "attention": [dict(r) for r in store.db.execute("SELECT id,kind,status,error FROM outbox WHERE status IN ('unknown','failed') ORDER BY id")]}))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=Path(__file__).with_name("notify_config.json"))
    parser.add_argument("--dry-run", action="store_true", help="读取真实数据并预览，不发送、不写入状态；可与 --push-positions 组合")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--push-positions", nargs="?", const=10, type=int, metavar="N", help="手动推送最新 N 条仓位历史；默认10，0表示全部")
    mode.add_argument("--status", action="store_true", help="查看状态，不访问网络")
    mode.add_argument("--test-push", action="store_true", help="发送一条连接测试消息")
    mode.add_argument("--retry-message", type=int, metavar="ID", help="核对后允许重试一条 unknown/failed 消息；可能重复，下次正常运行发送")
    args = parser.parse_args()
    if args.push_positions is not None and args.push_positions < 0:
        parser.error("--push-positions 必须大于等于 0；0 表示全部")
    if args.dry_run and (args.status or args.test_push or args.retry_message is not None):
        parser.error("--dry-run 只支持默认采集或 --push-positions")
    os.umask(0o077)
    try:
        cfg = load_config(args.config)
        if not any((args.dry_run, args.status, args.retry_message is not None)):
            validate_credentials(cfg)
        if args.dry_run or args.status:
            store = Store(cfg["state_file"], dry_run=True)
            try:
                store.bind(cfg)
                if args.status:
                    status(store, cfg)
                elif args.push_positions is not None:
                    push_position_history(store, cfg, Client(cfg["portfolio_id"], quiet=True), time.time(), args.push_positions, dry_run=True)
                else:
                    process(store, cfg, Client(cfg["portfolio_id"], quiet=True), time.time(), dry_run=True)
            finally:
                store.db.close()
            return 0
        with single_run(cfg["state_file"]) as acquired:
            if not acquired:
                if args.retry_message is not None or args.test_push or args.push_positions is not None:
                    print("任务占用状态库，本次人工操作没有执行；请稍后重试。", file=sys.stderr)
                    return 1
                print("上一次任务仍在执行，本次跳过。")
                return 0
            store = Store(cfg["state_file"])
            try:
                store.bind(cfg)
                store.recover_inflight()
                now = time.time()
                if args.retry_message is not None:
                    with store.db:
                        result = store.db.execute("UPDATE outbox SET status='pending',attempts=0,next_attempt=0 WHERE id=? AND status IN ('unknown','failed')", (args.retry_message,))
                    if not result.rowcount:
                        raise ValueError("消息不存在或状态不是 unknown/failed")
                    print("已重新加入队列；下次正常运行发送，可能与先前已受理消息重复。")
                    return 0
                if args.push_positions is not None:
                    ids = push_position_history(store, cfg, Client(cfg["portfolio_id"], quiet=True), now, args.push_positions)
                    accepted = dispatch(store, cfg, Sender(cfg), message_ids=ids)
                    print(f"本次仓位历史：{accepted}/{len(ids)} 条消息已受理。")
                    if accepted < len(ids):
                        print("其余消息保留在队列，可运行 --status 查看；待发送项由后续定时任务继续处理。")
                    return 0 if accepted == len(ids) else 1
                source_failed = False
                if args.test_push:
                    with store.db:
                        store.enqueue("test", f"test:{now}", "Binance 微信连接测试", f"接收测试：{cfg['name']}\n时间：{local_time(int(now * 1000))}\n请确认微信实际收到；服务受理不等于送达。", now)
                else:
                    try:
                        process(store, cfg, Client(cfg["portfolio_id"], quiet=True), now)
                    except ApiError as exc:
                        print(f"采集失败，未推进采集状态：{exc}", file=sys.stderr)
                        source_failed = True
                dispatch(store, cfg, Sender(cfg))
                attention = store.db.execute("SELECT count(*) FROM outbox WHERE status IN ('pending','unknown','failed')").fetchone()[0]
                if attention:
                    print(f"有 {attention} 条待发送或需处理消息，可用 --status 查看。", file=sys.stderr)
                return 1 if source_failed or attention else 0
            finally:
                store.db.close()
    except (ValueError, OSError, sqlite3.Error, ApiError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Read-only Binance copy-trading history probe, using Python's standard library.

No API key, login cookie, trading API or notification integration is used.
Raw responses retain the server's JSON numbers; normalized money fields are strings.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import hashlib
import json
from pathlib import Path
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from zoneinfo import ZoneInfo

BASE_URL = "https://www.binance.com/bapi/futures/v1/friendly/future/copy-trade/lead-portfolio"
DEFAULT_PORTFOLIO = "5075281354358777856"
TZ = ZoneInfo("Asia/Shanghai")
ACTION = {("BUY", "LONG"): "开多", ("SELL", "LONG"): "平多",
          ("SELL", "SHORT"): "开空", ("BUY", "SHORT"): "平空"}


class ApiError(RuntimeError):
    pass


def record_hash(value: object) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, default=str).encode()).hexdigest()


def dump(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2,
                      default=lambda x: str(x) if isinstance(x, Decimal) else _unsupported(x)) + "\n"


def _unsupported(value: object):
    raise TypeError(f"Cannot serialize {type(value).__name__}")


def milliseconds(value: str) -> int:
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=TZ)
    return int(dt.timestamp() * 1000)


def local_time(value: int | None) -> str | None:
    if value is None:
        return None
    return datetime.fromtimestamp(int(value) / 1000, TZ).isoformat(timespec="milliseconds")


def operation(row: dict, portfolio: str) -> dict:
    # No order ID is returned. This is a best-effort fingerprint, not an exchange ID.
    canonical = {"portfolioId": portfolio, **row}
    fingerprint = record_hash(canonical)
    return {
        "fingerprint": fingerprint,
        "symbol": row["symbol"],
        "action": ACTION.get((row.get("side"), row.get("positionSide")), "未知"),
        "time": local_time(row.get("orderUpdateTime")),
        "orderTime": local_time(row.get("orderTime")),
        "quantity": str(row["executedQty"]),
        "averagePrice": str(row["avgPrice"]),
        "notional": str(Decimal(str(row["executedQty"])) * Decimal(str(row["avgPrice"]))),
        "realizedPnl": str(row["totalPnl"]),
        "raw": row,
    }


class Client:
    def __init__(self, portfolio: str, output: Path | None = None, *, interval: float = 3.0,
                 retries: int = 2, quiet: bool = False):
        self.portfolio = portfolio
        self.output = output
        self.interval = interval
        self.retries = retries
        self.quiet = quiet
        self._last_call = 0.0
        self._call_number = 0
        if output is not None:
            (output / "raw").mkdir(parents=True, exist_ok=True)
        self.trace: list[dict] = []

    def request(self, endpoint: str, body: dict | None = None) -> dict:
        url = f"{BASE_URL}/{endpoint}"
        if body is None:
            url += "?" + urllib.parse.urlencode({"portfolioId": self.portfolio})
        for attempt in range(self.retries + 1):
            delay = self.interval - (time.monotonic() - self._last_call)
            if delay > 0:
                time.sleep(delay)
            request = urllib.request.Request(
                url, data=json.dumps(body).encode() if body is not None else None,
                headers={"User-Agent": "Mozilla/5.0", "Accept": "application/json",
                         "Content-Type": "application/json", "clienttype": "web"},
            )
            self._last_call = time.monotonic()
            try:
                with urllib.request.urlopen(request, timeout=25) as response:
                    raw, status = response.read(), response.status
                    content_type = response.headers.get("Content-Type", "")
            except urllib.error.HTTPError as exc:
                raw, status = exc.read(), exc.code
                content_type = exc.headers.get("Content-Type", "")
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                if attempt < self.retries:
                    time.sleep(2 ** (attempt + 1))
                    continue
                raise ApiError(f"{endpoint}: network error: {exc}") from exc
            self._call_number += 1
            file = self.output / "raw" / f"{self._call_number:03d}-{endpoint}.json" if self.output else None
            try:
                payload = json.loads(raw, parse_float=Decimal)
            except (ValueError, UnicodeDecodeError):
                if file is not None:
                    file = file.with_suffix(".html" if "html" in content_type else ".txt")
                    file.write_bytes(raw)
                raise ApiError(f"{endpoint}: HTTP {status}, non-JSON response (possibly WAF)")
            if file is not None:
                file.write_bytes(raw)
            if not isinstance(payload, dict):
                raise ApiError(f"{endpoint}: unexpected JSON shape")
            trace = {"url": url, "method": request.get_method(), "body": body,
                     "httpStatus": status, "code": payload.get("code"),
                     "fetchedAt": datetime.now(timezone.utc).isoformat(),
                     "rawFile": str(file.relative_to(self.output)) if file is not None else None}
            self.trace.append(trace)
            if self.output is not None:
                (self.output / "requests.json").write_text(dump(self.trace), encoding="utf-8")
            if status == 200 and payload.get("code") == "000000" and payload.get("success") is True:
                if not isinstance(payload.get("data"), dict):
                    raise ApiError(f"{endpoint}: successful response without an object in data")
                return payload["data"]
            retryable = status in (429, 500, 502, 503, 504) or payload.get("code") == "11012005"
            if retryable and attempt < self.retries:
                time.sleep(2 ** (attempt + 1))
                continue
            raise ApiError(f"{endpoint}: HTTP {status}, code={payload.get('code')}, message={payload.get('message')}")
        raise AssertionError("unreachable")

    def history(self, kind: str, *, start: int, end: int, max_pages: int = 50,
                page_size: int | None = None) -> dict:
        endpoint = {"orders": "order-history", "positions": "position-history"}[kind]
        rows, pages, seen_pages = [], [], set()
        seen_records = set()
        raw_count = 0
        page_size = page_size or (10 if kind == "orders" else 100)
        cursor = None
        total = None
        for page in range(1, max_pages + 1):
            body = {"portfolioId": self.portfolio, "pageSize": page_size,
                    "pageNumber": 1 if kind == "orders" else page}
            if kind == "orders":
                body.update(startTime=start, endTime=end)
                if cursor is not None:
                    body["indexValue"] = cursor
            data = self.request(endpoint, body)
            batch = data.get("list")
            if not isinstance(batch, list):
                raise ApiError(f"{endpoint}: missing list")
            page_hash = hashlib.sha256(dump(batch).encode()).hexdigest()
            if batch and page_hash in seen_pages:
                raise ApiError(f"{endpoint}: repeated page; refusing to report complete history")
            seen_pages.add(page_hash)
            # The observed cursor can repeat boundary records. Remove exact matches
            # from EARLIER pages only; do not collapse identical rows within a page.
            fresh = [r for r in batch if record_hash(r) not in seen_records]
            seen_records.update(record_hash(r) for r in batch)
            rows.extend(fresh)
            raw_count += len(batch)
            if total is None:
                total = data.get("total")
            pages.append({"request": body, "count": len(batch), "newCount": len(fresh),
                          "total": data.get("total"), "indexValue": data.get("indexValue")})
            if not self.quiet:
                print(f"{kind}: page {page}, {len(batch)} rows, fetched {len(rows)}, total {total}", file=sys.stderr)
            if len(batch) < page_size or (total is not None and len(rows) >= int(total)):
                if total is not None and len(rows) != int(total):
                    raise ApiError(f"{endpoint}: fetched {len(rows)} rows, expected {total}; raw responses retained")
                result = {"portfolioId": self.portfolio, "kind": kind,
                          "fetchedAt": datetime.now(timezone.utc).isoformat(),
                          "total": total, "count": len(rows), "complete": True,
                          "rawCount": raw_count, "crossPageDuplicates": raw_count - len(rows),
                          "pages": pages, "rows": rows}
                if kind == "orders":
                    result.update(startTime=start, endTime=end,
                                  operations=[operation(row, self.portfolio) for row in rows])
                else:
                    result["rowsByOpenedDesc"] = sorted(rows, key=lambda r: r["opened"], reverse=True)
                return result
            if kind == "orders":
                next_cursor = data.get("indexValue")
                if next_cursor is None or next_cursor == cursor:
                    raise ApiError(f"{endpoint}: missing or repeated cursor before completion")
                cursor = next_cursor
        raise ApiError(f"{endpoint}: max-pages={max_pages} reached; raw responses retained, history incomplete")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--portfolio-id", default=DEFAULT_PORTFOLIO)
    parser.add_argument("--kind", choices=["both", "orders", "positions", "detail"], default="both")
    parser.add_argument("--start", help="Order window start, ISO date/time; naive times use Asia/Shanghai")
    parser.add_argument("--end", help="Order window end, exact ISO date/time; date-only means midnight")
    parser.add_argument("--out", type=Path, help="New output directory (must not already exist)")
    parser.add_argument("--max-pages", type=int, default=50)
    args = parser.parse_args()
    now = datetime.now(TZ)
    try:
        start = milliseconds(args.start) if args.start else int((now - timedelta(days=7)).timestamp() * 1000)
        end = milliseconds(args.end) if args.end else int(now.timestamp() * 1000)
    except ValueError as exc:
        parser.error(f"Invalid ISO date/time: {exc}")
    if start >= end or args.max_pages < 1:
        parser.error("start must be earlier than end, and max-pages must be positive")
    if not args.portfolio_id.isdecimal():
        parser.error("portfolio-id must be a decimal string")
    output = args.out or Path("samples") / now.strftime("%Y%m%d-%H%M%S-%f")
    if output.exists():
        parser.error(f"Output directory already exists: {output}; choose a new directory")
    client = Client(args.portfolio_id, output)
    try:
        if args.kind == "detail":
            data = client.request("detail")
            (output / "detail.json").write_text(dump(data), encoding="utf-8")
            print(f"{data.get('nickname')}: {data.get('leadPortfolioId')}")
        else:
            for kind in (["orders", "positions"] if args.kind == "both" else [args.kind]):
                result = client.history(kind, start=start, end=end, max_pages=args.max_pages)
                (output / f"{kind}.json").write_text(dump(result), encoding="utf-8")
        print(f"Saved: {output.resolve()}")
        return 0
    except ApiError as exc:
        print(f"ERROR: {exc}\nPartial raw responses: {output.resolve()}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

"""Bounded, read-only probes to verify pagination and date filtering."""
import json
import time
import urllib.request
import urllib.error
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

OUT = Path('research/order-checks')
OUT.mkdir(exist_ok=True)
URL = 'https://www.binance.com/bapi/futures/v1/friendly/future/copy-trade/lead-portfolio/order-history'
PID = '5075281354358777856'
START = int(datetime(2026, 11, 8, tzinfo=ZoneInfo('Asia/Shanghai')).timestamp() * 1000)
END = int(time.time() * 1000)
BASE = {'portfolioId': PID, 'pageNumber': 1, 'pageSize': 10, 'startTime': START, 'endTime': END}

def request(name, body):
    req = urllib.request.Request(URL, data=json.dumps(body).encode(), headers={
        'User-Agent': 'Mozilla/5.0', 'Content-Type': 'application/json', 'Accept': 'application/json', 'clienttype': 'web'})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            raw, status = r.read(), r.status
    except urllib.error.HTTPError as e:
        raw, status = e.read(), e.code
    try:
        response = json.loads(raw)
    except ValueError:
        response = {'nonJsonPreview': raw[:200].decode(errors='replace')}
    record = {'url': URL, 'request': body, 'httpStatus': status, 'response': response}
    (OUT / f'{name}.json').write_text(json.dumps(record, ensure_ascii=False, indent=2))
    data = response.get('data') or {}
    rows = data.get('list') or []
    print(json.dumps({'case': name, 'status': status, 'code': response.get('code'), 'total': data.get('total'), 'rows': len(rows), 'indexValue': data.get('indexValue'), 'first': rows[0].get('orderTime') if rows else None, 'last': rows[-1].get('orderTime') if rows else None}, ensure_ascii=False), flush=True)
    time.sleep(0.4)
    return response

first = request('page1', BASE)
cursor = (first.get('data') or {}).get('indexValue')
request('page2_without_cursor', {**BASE, 'pageNumber': 2})
request('page2_with_cursor', {**BASE, 'pageNumber': 2, 'indexValue': cursor})
request('page1_with_cursor', {**BASE, 'indexValue': cursor})
request('page_size_50', {**BASE, 'pageSize': 50})
request('without_dates', {'portfolioId': PID, 'pageNumber': 1, 'pageSize': 10})
request('one_day', {**BASE, 'startTime': int(datetime(2026, 11, 14, tzinfo=ZoneInfo('Asia/Shanghai')).timestamp() * 1000)})

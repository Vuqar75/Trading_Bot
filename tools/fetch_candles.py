"""Download public Spot Testnet OHLCV, with pagination and a provenance manifest.

No signed requests, account credentials, bot configuration or live SQLite access.
"""
import argparse
import csv
import hashlib
import json
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import urlopen

ENDPOINT = 'https://testnet.binance.vision/api/v3/klines'


def fetch(start, end, output):
    if start >= end or start % 60000 or end % 60000:
        raise ValueError('Invalid range: use aligned minute timestamps')
    cursor, rows, requests = start, {}, 0
    while cursor < end:
        url = ENDPOINT + '?' + urlencode(dict(symbol='BTCUSDT',interval='1m',
                    startTime=cursor,endTime=end-1,limit=1000))
        for attempt in range(4):
            try:
                with urlopen(url,timeout=25) as response: batch=json.load(response)
                break
            except Exception:
                if attempt == 3: raise
                time.sleep(attempt+1)
        requests += 1
        if not batch: break
        for row in batch:
            if start <= int(row[0]) < end:
                if int(row[0]) in rows and rows[int(row[0])] != row:
                    raise ValueError('Conflicting duplicate candle')
                rows[int(row[0])] = row
        next_cursor = int(batch[-1][0])+60000
        if next_cursor <= cursor: raise ValueError('Non-advancing pagination')
        cursor = next_cursor
    if not rows: raise ValueError('No candles returned')
    output.parent.mkdir(parents=True,exist_ok=True)
    with output.open('w',newline='',encoding='utf-8') as f:
        writer=csv.writer(f)
        writer.writerow(['open_time','open','high','low','close','volume','close_time'])
        writer.writerows(r[:7] for _,r in sorted(rows.items()))
    meta=dict(source='Binance Spot Testnet',endpoint=ENDPOINT,symbol='BTCUSDT',interval='1m',
              requested_start_ms=start,requested_end_exclusive_ms=end,
              fetched_at_utc=datetime.now(timezone.utc).isoformat(),rows=len(rows),requests=requests,
              expected_rows=(end-start)//60000,first_ms=min(rows),last_ms=max(rows),
              gaps=sum(b-a!=60000 for a,b in zip(sorted(rows),sorted(rows)[1:])),
              sha256=hashlib.sha256(output.read_bytes()).hexdigest())
    output.with_suffix('.provenance.json').write_text(json.dumps(meta,indent=2),encoding='utf-8')
    return meta


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--start',required=True)
    parser.add_argument('--end',required=True,help='Exclusive UTC date')
    parser.add_argument('--output',required=True,type=Path)
    args=parser.parse_args()
    def stamp(value):return int(datetime.fromisoformat(value).replace(tzinfo=timezone.utc).timestamp()*1000)
    print(json.dumps(fetch(stamp(args.start),stamp(args.end),args.output),indent=2))

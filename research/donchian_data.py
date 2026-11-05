"""Validated local 15m aggregation plus separate earlier official daily archives."""
import csv,datetime as dt,hashlib,io,json,urllib.request,zipfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from research.strategy_study import SYMBOLS,PERIOD,load_symbol
from research.spot_diagnosis import verify_archives
from research.spot_hypothesis import load_2026

DAY=86400000
def ms(y,m=1,d=1):return int(dt.datetime(y,m,d,tzinfo=dt.timezone.utc).timestamp()*1000)

def aggregate(rows):
    if not rows or rows[0][0]%DAY:raise ValueError('Daily aggregation must start at UTC midnight')
    out=[]
    if len(rows)%96:raise ValueError('Incomplete UTC day')
    for start in range(0,len(rows),96):
        chunk=rows[start:start+96];t=chunk[0][0]
        if t%DAY or any(r[0]!=t+i*PERIOD for i,r in enumerate(chunk)):raise ValueError('Missing, duplicate or misplaced intraday candle')
        out.append((t,chunk[0][1],max(x[2] for x in chunk),min(x[3] for x in chunk),chunk[-1][4]))
    return out

def existing(symbol,root):
    root=Path(root);verify_archives(root)
    m=json.loads(Path('research/spot_independent_manifest_2026.json').read_text())
    for item in m['files']:
        if hashlib.sha256((root/item['file']).read_bytes()).hexdigest()!=item['sha256']:raise ValueError('2026 hash mismatch')
    return load_symbol(symbol,root)+load_2026(symbol,root)

def fetch_daily(item):
    symbol,year,month,root=item;name=f'{symbol}-1d-{year}-{month:02d}.zip'
    url=f'https://data.binance.vision/data/spot/monthly/klines/{symbol}/1d/{name}'
    def get(u):
        for i in range(3):
            try:
                with urllib.request.urlopen(u,timeout=45) as r:return r.read()
            except (OSError,TimeoutError):
                if i==2:raise
    digest=get(url+'.CHECKSUM').decode().split()[0].lower();p=root/name
    if not p.exists() or hashlib.sha256(p.read_bytes()).hexdigest()!=digest:
        data=get(url)
        if hashlib.sha256(data).hexdigest()!=digest:raise ValueError('Archive hash mismatch')
        p.write_bytes(data)
    return {'file':name,'sha256':digest,'url':url,'bytes':p.stat().st_size}

def earlier(root,manifest_path=None):
    root=Path(root);root.mkdir(parents=True,exist_ok=True)
    months=[(2020,11),(2020,12)]+[(y,m) for y in (2021,2022) for m in range(1,13)]
    if manifest_path and Path(manifest_path).exists():
        raw=json.loads(Path(manifest_path).read_text());manifest=raw['files'] if isinstance(raw,dict) else raw
        expected={f'{s}-1d-{y}-{m:02d}.zip' for s in SYMBOLS for y,m in months}
        if len(manifest)!=len(expected) or {x['file'] for x in manifest}!=expected:raise ValueError('Incomplete earlier manifest')
        missing=[(s,y,m,root) for s in SYMBOLS for y,m in months if not (root/f'{s}-1d-{y}-{m:02d}.zip').exists()]
        if missing:
            with ThreadPoolExecutor(max_workers=4) as pool:list(pool.map(fetch_daily,missing))
        for item in manifest:
            if hashlib.sha256((root/item['file']).read_bytes()).hexdigest()!=item['sha256']:raise ValueError('Earlier daily hash mismatch')
    else:
        with ThreadPoolExecutor(max_workers=4) as pool:manifest=list(pool.map(fetch_daily,[(s,y,m,root) for s in SYMBOLS for y,m in months]))
    series={}
    for s in SYMBOLS:
        rows=[]
        for y,m in months:
            with zipfile.ZipFile(root/f'{s}-1d-{y}-{m:02d}.zip') as z:
                if len(z.namelist())!=1:raise ValueError('Unexpected daily archive')
                for r in csv.reader(io.TextIOWrapper(z.open(z.namelist()[0]))):
                    if not r[0].isdigit():continue
                    t=int(r[0]);end=int(r[6]);scale=1000 if t>10**14 else 1;t//=scale;end//=scale;o,h,l,c=map(float,r[1:5])
                    if not (t%DAY==0 and end==t+DAY-1 and 0<l<=min(o,c)<=max(o,c)<=h):raise ValueError('Invalid historical day')
                    rows.append((t,o,h,l,c))
        start,end=ms(2020,11),ms(2023)
        if len(rows)!=(end-start)//DAY or any(x[0]!=start+i*DAY for i,x in enumerate(rows)):raise ValueError('Historical daily gaps or duplicates')
        series[s]=rows
    return series,manifest

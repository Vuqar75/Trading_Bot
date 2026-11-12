"""Bounded pinned public archives for a fixed research-only five-coin cohort."""
from concurrent.futures import ThreadPoolExecutor, as_completed
import datetime as dt
import hashlib
import json
from pathlib import Path
import shutil
import threading
import urllib.request

import numpy as np

from research.usdm_intraday_data import zipped

ASSETS=('XRPUSDT','ADAUSDT','DOGEUSDT','LINKUSDT','DOTUSDT')
M=60000;H=60*M;BASE='https://data.binance.vision/data/futures/um/monthly'
BUDGET=750*1024**2


def month_range(end, start="2024-12"):
    y,m=map(int,start.split("-"))
    if not 1<=m<=12 or start>end:raise ValueError("month bounds")
    while f'{y}-{m:02d}'<=end:
        yield y,m
        y,m=(y+1,1) if m==12 else (y,m+1)


def acquire(root,end='2025-12',start='2024-12'):
    root=Path(root);root.mkdir(parents=True,exist_ok=True)
    if shutil.disk_usage(root).free<2*1024**3:raise ValueError('research disk budget')
    manifest_path=root/'manifest.json'
    old=json.loads(manifest_path.read_text()) if manifest_path.exists() else {'files':[]}
    cache={x['file']:x for x in old['files'] if 'sha256' in x}
    used=sum(x.get('bytes',0) for x in cache.values());lock=threading.Lock()
    def get(url):
        with urllib.request.urlopen(url,timeout=40) as response:
            length=response.headers.get('Content-Length')
            if length and int(length)>32*1024**2:raise ValueError('archive exceeds file budget')
            raw=response.read(32*1024**2+1)
            if len(raw)>32*1024**2:raise ValueError('archive exceeds file budget')
            return raw
    def one(item):
        nonlocal used
        symbol,kind,y,m=item;name=f'{symbol}-fundingRate-{y}-{m:02d}.zip' if kind=='fundingRate' else f'{symbol}-1m-{y}-{m:02d}.zip'
        relative=('fundingRate/' if kind=='fundingRate' else 'minute/'+kind+'/')+name;path=root/relative
        if relative in cache:
            prior=cache[relative]
            if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest()!=prior['sha256']:raise ValueError('pinned source changed or missing')
            return prior
        url=BASE+'/'+kind+'/'+symbol+('' if kind=='fundingRate' else '/1m')+'/'+name
        try:
            digest=get(url+'.CHECKSUM').decode().split()[0].lower()
            if len(digest)!=64 or any(c not in '0123456789abcdef' for c in digest):raise ValueError('checksum format')
            raw=path.read_bytes() if path.exists() else get(url)
            if hashlib.sha256(raw).hexdigest()!=digest:raise ValueError('official source checksum mismatch')
            with lock:
                if used+len(raw)>BUDGET:raise ValueError('total raw archive budget')
                used+=len(raw)
            path.parent.mkdir(parents=True,exist_ok=True)
            if not path.exists():
                temp=path.with_suffix('.tmp');temp.write_bytes(raw);temp.replace(path)
            return {'file':relative,'symbol':symbol,'kind':kind,'year':y,'month':m,'url':url,'sha256':digest,'bytes':len(raw)}
        except Exception as exc:
            return {'file':relative,'symbol':symbol,'kind':kind,'year':y,'month':m,'url':url,'error':type(exc).__name__+': '+str(exc)}
    items=[(s,k,y,m) for s in ASSETS for y,m in month_range(end,start) for k in ('klines','markPriceKlines','fundingRate')]
    results={k:v for k,v in cache.items()}
    with ThreadPoolExecutor(max_workers=4) as pool:
        for future in as_completed([pool.submit(one,item) for item in items]):
            row=future.result();results[row['file']]=row
            payload={'cohort':list(ASSETS),'files':sorted(results.values(),key=lambda x:x['file']),
                     'unavailable':[r['file'] for r in results.values() if 'error' in r],'end_month':end}
            temp=manifest_path.with_suffix('.tmp');temp.write_text(json.dumps(payload,indent=2)+'\n');temp.replace(manifest_path)
    print(json.dumps({'files':len(results),'errors':len(payload['unavailable']),'bytes':used,'end_month':end},indent=2))
    if payload['unavailable']:raise ValueError('public archive acquisition incomplete; receipts preserved')


def load(root,symbol,start,begin,end):
    if symbol not in ASSETS:raise ValueError('unregistered asset')
    manifest=json.loads((root/'manifest.json').read_text());n=(end-start)//M
    trade=np.full((n,4),np.nan);mark=trade.copy();volume=np.full(n,np.nan);fund=[];proof=[]
    for item in sorted(manifest['files'],key=lambda r:(bool(r.get('supplement_missing_only')),r['file'])):
        if item['symbol']!=symbol:continue
        if 'error' in item:raise ValueError('unavailable pinned archive')
        path=root/item['file']
        if hashlib.sha256(path.read_bytes()).hexdigest()!=item['sha256']:raise ValueError('pinned archive changed')
        proof.append(item)
        for row in zipped(path):
            if not row[0].isdigit():continue
            t=int(row[0])
            if item['kind']=='fundingRate':
                interval=float(row[1]);rate=float(row[2])
                if not np.isfinite(interval) or not 0<interval<=24 or not np.isfinite(rate):raise ValueError('funding value')
                fund.append((t,rate,interval));continue
            if t%M or int(row[6])!=t+M-1:raise ValueError('minute timestamp')
            i=(t-start)//M
            if not 0<=i<n:continue
            o,h,l,c=map(float,row[1:5])
            if not np.isfinite([o,h,l,c]).all() or not 0<l<=min(o,c)<=max(o,c)<=h:raise ValueError('OHLC')
            dest=trade if item['kind']=='klines' else mark
            if np.isfinite(dest[i,0]):
                equal=np.array_equal(dest[i],np.array([o,h,l,c]))
                if item['kind']=='klines':equal=equal and volume[i]==float(row[5])
                if item.get('supplement_missing_only') and equal:continue
                raise ValueError('duplicate/conflicting price minute')
            dest[i]=o,h,l,c
            if item['kind']=='klines':
                v=float(row[5])
                if not np.isfinite(v) or v<0:raise ValueError('volume')
                volume[i]=v
    fund.sort()
    for a,b in zip(fund,fund[1:]):
        gap=b[0]-a[0]
        if gap<=0 or min(abs(gap-a[2]*H),abs(gap-b[2]*H))>1000:raise ValueError('funding gap or cadence contradiction')
    if not fund or fund[0][0]>begin or fund[-1][0]<end-fund[-1][2]*H-1000:raise ValueError('funding edge coverage')
    if not np.isfinite(trade).all() or not np.isfinite(mark).all() or not np.isfinite(volume).all():
        missing={'trade':int(np.isnan(trade[:,0]).sum()),'mark':int(np.isnan(mark[:,0]).sum()),'volume':int(np.isnan(volume).sum())}
        missing['dates']={kind:sorted(set(dt.datetime.fromtimestamp((start+int(i)*M)/1000,dt.timezone.utc).date().isoformat() for i in np.flatnonzero(np.isnan(a[:,0]))))[:10]
                          for kind,a in (('trade',trade),('mark',mark))}
        raise ValueError('price coverage missing '+json.dumps(missing))
    return trade,mark,volume,[(t,rate) for t,rate,_ in fund],proof


def supplement(root,symbol,kind,day):
    """Explicit observed daily supplement only after a diagnosed price gap."""
    if symbol not in ASSETS or kind not in ('klines','markPriceKlines'):raise ValueError('supplement contract')
    dt.date.fromisoformat(day)
    manifest_path=root/'manifest.json';manifest=json.loads(manifest_path.read_text())
    relative=f'daily/{kind}/{symbol}-1m-{day}.zip';path=root/relative
    old=next((x for x in manifest['files'] if x['file']==relative),None)
    if old:
        if hashlib.sha256(path.read_bytes()).hexdigest()!=old['sha256']:raise ValueError('pinned supplement changed')
        return
    url=f'https://data.binance.vision/data/futures/um/daily/{kind}/{symbol}/1m/{symbol}-1m-{day}.zip'
    with urllib.request.urlopen(url+'.CHECKSUM',timeout=40) as r:digest=r.read().decode().split()[0]
    if path.exists():raw=path.read_bytes()
    else:
        with urllib.request.urlopen(url,timeout=40) as r:raw=r.read(32*1024**2+1)
    if len(raw)>32*1024**2 or sum(x.get('bytes',0) for x in manifest['files'])+len(raw)>BUDGET:raise ValueError('supplement budget')
    if hashlib.sha256(raw).hexdigest()!=digest:raise ValueError('supplement checksum')
    path.parent.mkdir(parents=True,exist_ok=True)
    if not path.exists():
        temp=path.with_suffix('.tmp');temp.write_bytes(raw);temp.replace(path)
    manifest['files'].append({'file':relative,'symbol':symbol,'kind':kind,'day':day,'url':url,'sha256':digest,'bytes':len(raw),'supplement_missing_only':True})
    temp=manifest_path.with_suffix('.tmp');temp.write_text(json.dumps(manifest,indent=2)+'\n');temp.replace(manifest_path)


if __name__=='__main__':
    import sys
    acquire(Path(sys.argv[1]),sys.argv[2] if len(sys.argv)>2 else '2025-12',sys.argv[3] if len(sys.argv)>3 else '2024-12')

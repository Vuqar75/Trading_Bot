"""Pinned public static-archive acquisition/validation; no FAPI or credentials."""
import csv,datetime as dt,hashlib,io,json,math,urllib.request,zipfile
from concurrent.futures import ThreadPoolExecutor,as_completed
from pathlib import Path
import sys
BASE='https://data.binance.vision/data/futures/um/daily/metrics'
SYMBOLS=('BTCUSDT','ETHUSDT','SOLUSDT')
FIELDS=('sum_open_interest','count_toptrader_long_short_ratio','sum_toptrader_long_short_ratio','count_long_short_ratio','sum_taker_long_short_vol_ratio')


def acquire(root):
    root=Path(root);root.mkdir(parents=True,exist_ok=True);p=root/'manifest.json'
    prior=json.loads(p.read_text())['files'] if p.exists() else []
    cache={f['file']:f for f in prior if 'sha256' in f and (root/f['file']).is_file()}
    day=dt.date(2025,6,30);days=[]
    while day<=dt.date(2026,10,31):days.append(day.isoformat());day+=dt.timedelta(days=1)
    def fetch(item):
        symbol,day=item;name=f'{symbol}/{symbol}-metrics-{day}.zip';dest=root/name
        old=cache.get(name)
        if old and hashlib.sha256(dest.read_bytes()).hexdigest()==old['sha256']:return old
        url=f'{BASE}/{name}'
        try:
            with urllib.request.urlopen(url+'.CHECKSUM',timeout=20) as r:sha=r.read().decode().split()[0]
            with urllib.request.urlopen(url,timeout=20) as r:raw=r.read()
            if hashlib.sha256(raw).hexdigest()!=sha:raise ValueError('checksum_mismatch')
            dest.parent.mkdir(exist_ok=True);temp=dest.with_suffix('.tmp');temp.write_bytes(raw);temp.replace(dest)
            return {'file':name,'url':url,'sha256':sha,'bytes':len(raw),'symbol':symbol,'day':day}
        except Exception as exc:return {'file':name,'symbol':symbol,'day':day,'error':type(exc).__name__}
    files=[]
    with ThreadPoolExecutor(max_workers=6) as pool:
        futures=[pool.submit(fetch,(symbol,day)) for symbol in SYMBOLS for day in days]
        for future in as_completed(futures):
            files.append(future.result())
            if len(files)%100==0:
                p.write_text(json.dumps({'files':sorted(files,key=lambda x:x['file'])},indent=2)+'\n');print('metric_archives',len(files),'of',len(futures),flush=True)
    result={'files':sorted(files,key=lambda x:x['file']),'errors':sum('error' in f for f in files)}
    p.write_text(json.dumps(result,indent=2)+'\n');print('acquisition_complete',len(files),'errors',result['errors'],flush=True)


def load(root):
    import numpy as np
    root=Path(root);m=json.loads((root/'manifest.json').read_text());out={};quality={}
    for symbol in SYMBOLS:
        values={};bad=0;equal=0;unsorted=0;files=[x for x in m['files'] if x['symbol']==symbol]
        if any('error' in f for f in files):raise ValueError('metric_archive_unavailable')
        for f in files:
            raw=(root/f['file']).read_bytes()
            if hashlib.sha256(raw).hexdigest()!=f['sha256']:raise ValueError('metric_checksum_changed')
            with zipfile.ZipFile(io.BytesIO(raw)) as z:
                if len(z.namelist())!=1:raise ValueError('metric_zip_members')
                rows=list(csv.DictReader(io.TextIOWrapper(z.open(z.namelist()[0]))))
            last=-1
            for row in rows:
                t=int(dt.datetime.fromisoformat(row['create_time']).replace(tzinfo=dt.timezone.utc).timestamp()*1000)
                if t<last:unsorted+=1
                last=t
                if row['symbol']!=symbol or t%300000 or dt.datetime.fromtimestamp(t/1000,dt.timezone.utc).date().isoformat()!=f['day']:raise ValueError('metric_time_symbol')
                try:v=tuple(float(row[k]) for k in FIELDS)
                except (ValueError,TypeError):bad+=1;continue
                if any(not math.isfinite(x) or x<=0 for x in v):bad+=1;continue
                if t in values:
                    if values[t]!=v:raise ValueError('metric_conflicting_duplicate')
                    equal+=1
                values[t]=v
        ts=np.asarray(sorted(values),dtype=np.int64);a=np.asarray([values[int(t)] for t in ts],dtype=float)
        out[symbol]=(ts,a);quality[symbol]={'files':len(files),'rows':len(ts),'invalid_value_rows':bad,'equal_duplicates':equal,'out_of_order_rows':unsorted,'observed_gap_intervals':int((np.diff(ts)!=300000).sum())}
    return out,quality

if __name__=='__main__':acquire(Path(sys.argv[1]))

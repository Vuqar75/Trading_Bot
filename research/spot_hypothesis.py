"""One frozen daily trend filter; 2026 is read only in independent mode."""
import argparse,csv,datetime as dt,io,json,zipfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from research.strategy_study import PERIOD,SYMBOLS,download_one,load_symbol,signals
from research.spot_diagnosis import diagnostic,verify_archives

def bull_filter(rows):
    daily=[];last=None;out=[]
    for i,r in enumerate(rows):
        day=r[0]//86400000
        if day!=last:
            if i:daily.append(rows[i-1][4])
            last=day
        out.append(len(daily)>=30 and daily[-1]>1.02*sum(daily[-30:])/30)
    return out

def filtered(signals_,bull):
    return [x if x!=1 or bull[i] else 0 for i,x in enumerate(signals_)]

def load_2026(symbol,root):
    out=[];start=int(dt.datetime(2026,1,1,tzinfo=dt.timezone.utc).timestamp()*1000);end=int(dt.datetime(2026,11,1,tzinfo=dt.timezone.utc).timestamp()*1000)
    for month in range(1,11):
        p=root/f'{symbol}-15m-2026-{month:02d}.zip'
        with zipfile.ZipFile(p) as z:
            names=z.namelist()
            if len(names)!=1:raise ValueError('Invalid archive members')
            for r in csv.reader(io.TextIOWrapper(z.open(names[0]))):
                if not r[0].isdigit():continue
                t=int(r[0]);e=int(r[6]);scale=1000 if t>10**14 else 1;t//=scale;e//=scale
                o,h,l,c=map(float,r[1:5])
                if not (start<=t<end and t%PERIOD==0 and e==t+PERIOD-1 and 0<l<=min(o,c)<=max(o,c)<=h):raise ValueError('Invalid or incomplete candle')
                out.append((t,o,h,l,c))
    if len(out)!=(end-start)//PERIOD or any(r[0]!=start+i*PERIOD for i,r in enumerate(out)):raise ValueError('Missing, duplicate or out-of-range 2026 candles')
    return out

def main():
    p=argparse.ArgumentParser();p.add_argument('mode',choices=['development','independent']);p.add_argument('--data',required=True);p.add_argument('--output',required=True);a=p.parse_args();root=Path(a.data);verify_archives(root)
    manifest=[]
    if a.mode=='independent':
        if dt.datetime.now(dt.timezone.utc)<dt.datetime(2026,11,1,tzinfo=dt.timezone.utc):raise ValueError('Independent window is not fully closed')
        requests=[(s,2026,m,root,None) for s in SYMBOLS for m in range(1,11)]
        with ThreadPoolExecutor(max_workers=4) as ex:manifest=list(ex.map(download_one,requests))
    out={}
    for s in SYMBOLS:
        rows=load_symbol(s,root)
        if a.mode=='independent':rows+=load_2026(s,root)
        sig,_=signals(rows);candidate=filtered(sig['current'],bull_filter(rows))
        years=(2024,2025) if a.mode=='development' else (2026,)
        for year in years:
            start=int(dt.datetime(year,1,1,tzinfo=dt.timezone.utc).timestamp()*1000);end=int(dt.datetime(year+1,1,1,tzinfo=dt.timezone.utc).timestamp()*1000) if year<2026 else int(dt.datetime(2026,11,1,tzinfo=dt.timezone.utc).timestamp()*1000)
            for name,arr in [('current',sig['current']),('candidate',candidate),('hold',sig['hold']),('none',sig['none'])]:
                for bps in (5,10):out[f'{s}|{year}|{name}|{bps}']=diagnostic(rows,arr,s,start,end,bps=bps,hold=name=='hold')
        print('complete',a.mode,s,flush=True)
    Path(a.output).write_text(json.dumps({'data_through':'2026-10-31' if a.mode=='independent' else '2025-12-31','manifest':manifest,'results':out},indent=2),encoding='utf8')
if __name__=='__main__':main()

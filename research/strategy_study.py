"""Fixed preregistered Spot OHLC study. Never accesses accounts or places orders."""
import argparse, csv, datetime as dt, hashlib, io, json, math, os, statistics, urllib.request, zipfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

SYMBOLS=('BTCUSDT','ETHUSDT','SOLUSDT')
STEP={'BTCUSDT':0.00001,'ETHUSDT':0.0001,'SOLUSDT':0.001}
PERIOD=900000
BASE='https://data.binance.vision/data/spot/monthly/klines'
STRATEGIES=('current','trend','breakout','reversion','hold','none')

def download_one(item):
    symbol,year,month,root,pinned=item
    name=f'{symbol}-15m-{year}-{month:02d}.zip'; url=f'{BASE}/{symbol}/15m/{name}'
    p=root/name
    def get(u):
        for attempt in range(3):
            try:
                with urllib.request.urlopen(u,timeout=45) as r:return r.read()
            except (OSError,TimeoutError):
                if attempt==2:raise
    checksum=get(url+'.CHECKSUM').decode().split()[0].lower()
    if pinned is not None and checksum!=pinned:raise ValueError('Archive version changed: '+name)
    if not p.exists() or hashlib.sha256(p.read_bytes()).hexdigest()!=checksum:
        b=get(url)
        if hashlib.sha256(b).hexdigest()!=checksum:raise ValueError('Checksum mismatch: '+name)
        tmp=p.with_suffix('.tmp');tmp.write_bytes(b);os.replace(tmp,p)
    return {'file':name,'sha256':checksum,'url':url,'bytes':p.stat().st_size}

def load_symbol(symbol,root):
    rows=[]
    start=int(dt.datetime(2023,3,25,tzinfo=dt.timezone.utc).timestamp()*1000)
    for year in (2023,2024,2025):
        for month in range(1,13):
            p=root/f'{symbol}-15m-{year}-{month:02d}.zip'
            with zipfile.ZipFile(p) as z:
                names=z.namelist()
                if len(names)!=1:raise ValueError('Unexpected archive')
                for r in csv.reader(io.TextIOWrapper(z.open(names[0]))):
                    if not r[0].isdigit():continue
                    t=int(r[0]); end=int(r[6]);scale=1000 if t>10**14 else 1
                    t//=scale;end//=scale
                    if t<start:continue
                    o,h,l,c=map(float,r[1:5])
                    if not (0<l<=min(o,c)<=max(o,c)<=h and t%PERIOD==0 and end==t+PERIOD-1):raise ValueError('Invalid OHLC/closure '+str((symbol,t)))
                    rows.append((t,o,h,l,c))
    end=int(dt.datetime(2026,1,1,tzinfo=dt.timezone.utc).timestamp()*1000)
    if len(rows)!=(end-start)//PERIOD or any(r[0]!=start+i*PERIOD for i,r in enumerate(rows)):raise ValueError('Missing, duplicate or out-of-range candles: '+symbol)
    return rows

def signals(rows):
    closes=[r[4] for r in rows]; prefixes=[0.0];prefix2=[0.0]
    for p in closes:prefixes.append(prefixes[-1]+p);prefix2.append(prefix2[-1]+p*p)
    def avg(i,n):return (prefixes[i+1]-prefixes[i+1-n])/n
    out={s:[0]*len(rows) for s in STRATEGIES}; regimes=[];daily=[];last_day=None
    for i,(t,o,h,l,c) in enumerate(rows):
        day=t//86400000
        if day!=last_day:
            if i:daily.append(closes[i-1])
            last_day=day
        regimes.append('up' if len(daily)>=30 and daily[-1]>sum(daily[-30:])/30 else 'down' if len(daily)>=30 else 'unknown')
        for name,a,b in (('current',10,30),('trend',50,200)):
            if i>=b:
                old=avg(i-1,a)-avg(i-1,b);new=avg(i,a)-avg(i,b)
                out[name][i]=1 if old<=0<new else -1 if old>=0>new else 0
        if i>=20:
            out['breakout'][i]=1 if c>max(x[2] for x in rows[i-20:i]) else -1 if c<min(x[3] for x in rows[i-10:i]) else 0
            mean=avg(i,20);variance=max(0,(prefix2[i+1]-prefix2[i-19])/20-mean*mean)
            out['reversion'][i]=1 if c<mean-2*math.sqrt(variance) else -1 if c>=mean else 0
    return out,regimes

def simulate(rows,sig,regimes,symbol,year,cost_bps=5,kind='current',take_first=False):
    cash=1000.;qty=0.;entry=0.;entry_cost=0.;trades=[];turnover=0.;invested=0;equity=[];ambiguous=0;pending=0;entry_regime='unknown'
    start=int(dt.datetime(year,1,1,tzinfo=dt.timezone.utc).timestamp()*1000);end=int(dt.datetime(year+1,1,1,tzinfo=dt.timezone.utc).timestamp()*1000)
    ix=[i for i,r in enumerate(rows) if start<=r[0]<end]
    def buy(price,reg):
        nonlocal cash,qty,entry,entry_cost,turnover,entry_regime
        execp=price*(1+cost_bps/10000);q=math.floor((10/execp)/STEP[symbol])*STEP[symbol]
        gross=q*execp;fee=gross*.001
        if gross<5 or gross+fee>cash:return
        cash-=gross+fee;qty=q;entry=execp;entry_cost=gross+fee;turnover+=gross;entry_regime=reg
    def sell(price,reason,t):
        nonlocal cash,qty,entry,entry_cost,turnover
        execp=price*(1-cost_bps/10000);gross=qty*execp;fee=gross*.001;cash+=gross-fee;turnover+=gross
        trades.append({'pnl':gross-fee-entry_cost,'turnover':gross,'reason':reason,'regime':entry_regime,'time':t});qty=0;entry=entry_cost=0
    for j,i in enumerate(ix):
        t,o,h,l,c=rows[i]
        # Pending order was decided from the preceding completed candle only.
        if kind=='hold' and j==0:buy(o,regimes[i-1] if i else 'unknown')
        elif pending<0 and qty:sell(o,'signal',t)
        elif pending>0 and not qty:buy(o,regimes[i-1] if i else 'unknown')
        if qty and kind!='hold':
            stop=entry*.98;take=entry*1.04
            if o<=stop:sell(o,'stop_gap',t)
            elif o>=take:sell(o,'take_gap',t)
            elif l<=stop and h>=take:
                ambiguous+=1;sell(take if take_first else stop,'ambiguous',t)
            elif l<=stop:sell(stop,'stop',t)
            elif h>=take:sell(take,'take',t)
        if qty:invested+=1
        # Valuation includes hypothetical exit fees and spread/slippage, not zero-cost liquidation.
        equity.append(cash+qty*c*(1-cost_bps/10000)*.999)
        pending=sig[i] if kind not in ('hold','none') else 0
    if qty:sell(rows[ix[-1]][4],'period_end',rows[ix[-1]][0]+PERIOD)
    equity[-1]=cash
    peak=1000.;maxdd=0.;startdd=None;maxduration=0
    for i,v in enumerate(equity):
        if v>=peak:
            if startdd is not None:maxduration=max(maxduration,i-startdd);startdd=None
            peak=v
        else:
            if startdd is None:startdd=i
            maxdd=max(maxdd,(peak-v)/peak)
    if startdd is not None:maxduration=max(maxduration,len(equity)-startdd)
    pnls=[x['pnl'] for x in trades]
    return {'net':cash-1000,'max_dd_pct':maxdd*100,'max_recovery_hours':maxduration/4,'open_drawdown_at_end':startdd is not None,'trades':len(trades),'turnover':turnover,'exposure_pct':invested/len(ix)*100,'avg_trade':sum(pnls)/len(pnls) if pnls else None,'without_best_trade':sum(pnls)-max(pnls) if pnls else 0,'ambiguous':ambiguous,'trade_pnls':pnls,'regimes':{r:{'trades':sum(1 for x in trades if x['regime']==r),'net':sum(x['pnl'] for x in trades if x['regime']==r)} for r in ('up','down','unknown')}}

def main():
    p=argparse.ArgumentParser();p.add_argument('--data',required=True);p.add_argument('--output',required=True);a=p.parse_args();root=Path(a.data);root.mkdir(parents=True,exist_ok=True)
    pin_path=Path(__file__).with_name('strategy_manifest.json')
    pinned={x['file']:x['sha256'] for x in json.loads(pin_path.read_text(encoding='utf8'))['files']} if pin_path.exists() else None
    requests=[]
    for s in SYMBOLS:
        for y in (2023,2024,2025):
            for m in range(1,13):
                name=f'{s}-15m-{y}-{m:02d}.zip'
                if pinned is not None and name not in pinned:raise ValueError('Missing archive pin: '+name)
                requests.append((s,y,m,root,None if pinned is None else pinned[name]))
    with ThreadPoolExecutor(max_workers=4) as ex:manifest=list(ex.map(download_one,requests))
    result={};quality={}
    for symbol in SYMBOLS:
        rows=load_symbol(symbol,root);sigs,regimes=signals(rows);quality[symbol]={'candles':len(rows),'first_ms':rows[0][0],'last_ms':rows[-1][0],'gaps':0,'duplicates':0,'invalid_or_open':0}
        for year in (2024,2025):
            for kind in STRATEGIES:
                for scenario,bps,tf in (('base',5,False),('expensive',10,False),('take_first',5,True)):
                    result[f'{symbol}|{year}|{kind}|{scenario}']=simulate(rows,sigs[kind],regimes,symbol,year,bps,kind,tf)
        print('completed',symbol,flush=True)
    out={'created_utc':dt.datetime.now(dt.timezone.utc).isoformat(),'manifest':manifest,'quality':quality,'results':result}
    Path(a.output).write_text(json.dumps(out,indent=2),encoding='utf8')

if __name__=='__main__':main()

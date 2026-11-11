"""Fixed paper-only session study; never connects to exchange or account APIs."""
import bisect, csv, datetime as dt, hashlib, io, json, math, pathlib, sys, urllib.request, zipfile
from collections import Counter
from decimal import Decimal, ROUND_CEILING
import numpy as np
from research.usdm_intraday_data import zipped
from research.usdm_risk import funding as funding_amount

MINUTE=60000
START=int(dt.datetime(2024,12,1,tzinfo=dt.timezone.utc).timestamp()*1000)
END=int(dt.datetime(2026,10,2,tzinfo=dt.timezone.utc).timestamp()*1000)
N=(END-START)//MINUTE
SEGMENTS=[('2025H1','2025-01-01','2025-07-01'),('2025H2','2025-07-01','2026-01-01'),('2026H1','2026-01-01','2026-07-01'),('2026Q3','2026-07-01','2026-10-01')]

def idx(ms):
    if ms%MINUTE: raise ValueError('unaligned timestamp')
    return (ms-START)//MINUTE

def boundaries(day):
    z=dt.timezone(dt.timedelta(hours=-5))
    def at(d,h,m):return int(dt.datetime.combine(d,dt.time(h,m),z).timestamp()*1000)
    return {'previous':at(day-dt.timedelta(days=1),17,0),'first':at(day,9,30),'prelast':at(day,16,0),'signal':at(day,16,30),'close':at(day,17,0)}

def direction(a,b):return 'LONG' if a>0 and b<0 else 'SHORT' if a<=0 and b>=0 else None

def quantity(mark,min_notional):
    step=Decimal('.001');m=Decimal(str(mark));minimum=Decimal(str(min_notional))
    return float((minimum/m/step).to_integral_value(rounding=ROUND_CEILING)*step)

def archive_rows(root,x):
    path=root/x['file']
    if path.exists():raw=path.read_bytes()
    else:
        # The original cache can be ephemeral. Fetch only the pinned public bytes.
        with urllib.request.urlopen(x['url'],timeout=90) as f:raw=f.read()
    sha=hashlib.sha256(raw).hexdigest()
    if sha!=x['sha256']:raise ValueError('pinned SHA mismatch: '+str(path))
    if path.exists():rows=zipped(path)
    else:
        with zipfile.ZipFile(io.BytesIO(raw)) as z:
            if len(z.namelist())!=1:raise ValueError('unexpected zip member')
            rows=list(csv.reader(io.TextIOWrapper(z.open(z.namelist()[0]))))
    return rows,sha

def read_data(root,symbol):
    trade=np.full((N,2),np.nan);mark=np.full((N,4),np.nan);events=[];proof=[]
    for mf in ('research/usdm_1m_manifest.json','research/usdm_5m_manifest.json'):
        manifest=json.loads(pathlib.Path(mf).read_text())
        for x in manifest['files']:
            if x['symbol']!=symbol or not ((2024,12)<=(x['year'],x['month'])<=(2026,9)):continue
            kind=x['kind']
            if mf.endswith('1m_manifest.json') and kind not in ('klines','markPriceKlines'):continue
            if mf.endswith('5m_manifest.json') and kind!='fundingRate':continue
            rows,sha=archive_rows(root,x);proof.append({'file':x['file'],'sha256':sha})
            for row in rows:
                if not row[0].isdigit():continue
                t=int(row[0])
                if kind=='fundingRate':
                    interval,rate=int(row[1]),float(row[2])
                    if interval<=0 or not math.isfinite(rate):raise ValueError('bad funding')
                    events.append((t,rate));continue
                if t%MINUTE or int(row[6])!=t+MINUTE-1:raise ValueError('candle boundary')
                o,h,l,c=map(float,row[1:5]);i=idx(t)
                if not 0<=i<N:continue
                if not 0<l<=min(o,c)<=max(o,c)<=h:raise ValueError('invalid OHLC')
                dest=trade if kind=='klines' else mark
                if np.isfinite(dest[i,0]):raise ValueError('duplicate candle')
                dest[i]=(o,c) if kind=='klines' else (o,h,l,c)
    events.sort()
    if len({x[0] for x in events})!=len(events):raise ValueError('duplicate funding')
    return trade,mark,events,proof

def feature(trade,times):
    ix=[idx(times[k])-1 for k in ('previous','first','prelast','signal','close')]
    if min(ix)<0 or max(ix)>=len(trade):return None
    vals=trade[ix,1]
    if not np.isfinite(vals[:4]).all():return None
    p,o,s,c,last=vals
    return (o/p-1,c/s-1,last/c-1 if np.isfinite(last) else None)

def risk_move(trade,signal_ms):
    end=idx(signal_ms);begin=end-7*24*60-30
    if begin<0:return None
    c=trade[begin:end,1]
    if len(c)!=7*24*60+30 or not np.isfinite(c).all():return None
    return float(np.max(np.abs(c[30:]/c[:-30]-1)))

def ols(rows,two):
    if len(rows)<4:return None
    a=np.array(rows);x=np.column_stack((np.ones(len(a)),a[:,0],a[:,1])) if two else np.column_stack((np.ones(len(a)),a[:,0]));y=a[:,2]
    beta=np.linalg.lstsq(x,y,rcond=None)[0];den=np.sum((y-y.mean())**2)
    return {'n':len(rows),'coefficients':beta.tolist(),'r2':float(1-np.sum((y-x@beta)**2)/den) if den else None}

def simulate(trade,mark,events,day,times,direction_,cash,symbol,stress):
    entry=times['signal']+(5 if stress else 2)*MINUTE;exit=times['close']+(5 if stress else 1)*MINUTE
    ei,xi=idx(entry),idx(exit);history=risk_move(trade,times['signal'])
    if history is None:return {'rejection':'risk_history_missing'}
    if not (0<=ei<xi<len(trade)) or not np.isfinite(trade[ei]).all() or not np.isfinite(mark[ei]).all():return {'rejection':'entry_trade_or_mark_gap'}
    rawin,rawout=trade[ei,0],trade[xi,0];markin=mark[ei,0]
    q=quantity(markin,50 if symbol=='BTCUSDT' else 20)
    friction_bps=5 if stress else 2;sign=1 if direction_=='LONG' else -1
    fillin=rawin*(1+sign*friction_bps/10000);fillout=rawout*(1-sign*friction_bps/10000);notional=q*fillin
    if q*markin<(50 if symbol=='BTCUSDT' else 20)-1e-9:return {'rejection':'min_notional'}
    if not .97<=fillin/markin<=1.03:return {'rejection':'market_price_bound'}
    if notional>100:return {'rejection':'notional_cap','candidate_notional':notional}
    if notional*(1+.0005)>cash:return {'rejection':'insufficient_capital'}
    risk=notional*(history+.002+.0021)
    if risk>5:return {'rejection':'risk_cap','estimated_risk':risk}
    if not np.isfinite(trade[ei+1:xi+1]).all() or not np.isfinite(mark[ei+1:xi+1]).all():return {'unresolved':'post_entry_trade_or_mark_gap'}
    marks=mark[ei:xi]
    if (direction_=='SHORT' and max(np.max(marks[:,1]),mark[xi,0])>=fillin*1.98) or (direction_=='LONG' and min(np.min(marks[:,2]),mark[xi,0])<=fillin*.01):return {'unresolved':'liquidation_model_unavailable'}
    timestamps=[x[0] for x in events];before=bisect.bisect_right(timestamps,entry)-1;after=bisect.bisect_right(timestamps,exit)
    if before<0 or after>=len(events) or timestamps[after]-timestamps[before]>8*60*MINUTE+1000:return {'rejection':'funding_history_incomplete'}
    applicable=[(t,r) for t,r in events if entry<t<=exit];funding=0.
    for t,rate in applicable:
        j=idx(t)
        if not np.isfinite(mark[j,0]):return {'unresolved':'funding_mark_missing'}
        funding+=float(funding_amount(direction_,q,mark[j,0],rate))
    gross=q*(rawout-rawin)*sign;fees=.0005*q*(fillin+fillout)
    friction=q*(abs(fillin-rawin)+abs(fillout-rawout));net=gross-fees-friction+funding
    return {'direction':direction_,'signal_ms':times['signal'],'availability_ms':times['signal']+1000,'entry_ms':entry,'exit_ms':exit,'qty':q,'reference_in':rawin,'reference_out':rawout,'entry':fillin,'exit':fillout,'entry_notional':q*rawin,'estimated_risk':risk,'gross':gross,'fees':fees,'friction':friction,'funding':funding,'funding_events':applicable,'net':net,'hold_minutes':(exit-entry)//MINUTE}

def summarize(rows):
    trades=[r for r in rows if 'net' in r];gross=sum(r['gross'] for r in trades);fees=sum(r['fees'] for r in trades);friction=sum(r['friction'] for r in trades);funding=sum(r['funding'] for r in trades);net=sum(r['net'] for r in trades)
    if not math.isclose(gross-fees-friction+funding,net,abs_tol=1e-9):raise AssertionError('ledger mismatch')
    equity=peak=1000.;dd=0.
    for r in trades:equity+=r['net'];peak=max(peak,equity);dd=max(dd,(peak-equity)/peak)
    turnover=sum(r['entry_notional'] for r in trades)
    return {'trades':len(trades),'gross':gross,'fees':fees,'friction':friction,'funding':funding,'net':net,'avg_net':net/len(trades) if trades else None,'drawdown_closed_equity':dd,'avg_hold_min':sum(r['hold_minutes'] for r in trades)/len(trades) if trades else None,'breakeven_bps_per_entry_notional':10000*(gross+funding)/turnover if turnover else None,'without_best_trade':net-max((r['net'] for r in trades),default=0),'rejections':dict(Counter(r['rejection'] for r in rows if 'rejection' in r)),'unresolved':dict(Counter(r['unresolved'] for r in rows if 'unresolved' in r))}

def main(root,out):
    allrows=[];prediction={};proof={}
    for symbol in ('BTCUSDT','ETHUSDT'):
        trade,mark,events,proof[symbol]=read_data(root,symbol);pred={k:[] for k,_,_ in SEGMENTS};pred['all']=[];sessions=[]
        for segment,start,end in SEGMENTS:
            day=dt.date.fromisoformat(start);last=dt.date.fromisoformat(end)
            while day<last:
                t=boundaries(day);f=feature(trade,t);sessions.append((segment,day,t,f))
                if f is not None and f[2] is not None:pred[segment].append(f);pred['all'].append(f)
                day+=dt.timedelta(days=1)
        prediction[symbol]={k:{'single':ols(v,False),'combined':ols(v,True)} for k,v in pred.items()}
        cash={'base':1000.,'stress':1000.};halted={'base':False,'stress':False}
        for segment,day,t,f in sessions:
            for scenario in ('base','stress'):
                record={'symbol':symbol,'date':day.isoformat(),'segment':segment,'scenario':scenario,'signal_ms':t['signal'],'availability_ms':t['signal']+1000,'signal':None,'onfh':None,'slh':None,'lh':None}
                if f is None:record['rejection']='signal_data_missing'
                else:
                    record.update(onfh=f[0],slh=f[1],lh=f[2],signal=direction(f[0],f[1]))
                    if record['signal'] and halted[scenario]:record['rejection']='prior_position_unresolved'
                    elif record['signal']:
                        r=simulate(trade,mark,events,day,t,record['signal'],cash[scenario],symbol,scenario=='stress');record.update(r)
                        if 'net' in r:cash[scenario]+=r['net']
                        if 'unresolved' in r:halted[scenario]=True
                allrows.append(record)
    summary={}
    for scenario in ('base','stress'):
        for sym in ('BTCUSDT','ETHUSDT'):
            for group in ('all','LONG','SHORT','2025H1','2025H2','2026H1','2026Q3','check'):
                rows=[r for r in allrows if r['scenario']==scenario and r['symbol']==sym and (group=='all' or r.get('direction')==group or r['segment']==group or (group=='check' and r['segment'].startswith('2026')))]
                summary[f'{scenario}/{sym}/{group}']=summarize(rows)
    out.mkdir(parents=True,exist_ok=True)
    (out/'journal.json').write_text(json.dumps(allrows,indent=2,sort_keys=True,allow_nan=False))
    (out/'summary.json').write_text(json.dumps({'summary':summary,'predictability':prediction,'inputs':proof},indent=2,sort_keys=True,allow_nan=False))
    print(json.dumps({k:v for k,v in summary.items() if k.endswith('/all')},indent=2))

if __name__=='__main__':main(pathlib.Path(sys.argv[1]),pathlib.Path(sys.argv[2]))

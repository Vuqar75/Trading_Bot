"""Bounded BTC paper research. 15m OHLC approximations, never connects to trading."""
import argparse,datetime as dt,hashlib,json,math
from pathlib import Path
from research.strategy_study import load_symbol
from research.spot_hypothesis import load_2026
from research.spot_diagnosis import verify_archives
from research.adaptive_data import months,rows as archive_rows

BAR=900000;DAY=86400000;STEP=.00001
def stamp(y,m=1):return int(dt.datetime(y,m,1,tzinfo=dt.timezone.utc).timestamp()*1000)
def futures_data(root):
 root=Path(root);manifest=json.loads((root/'adaptive-futures-manifest.json').read_text());out=[];marks={};funding=[]
 for item in manifest['files']:
  p=root/item['file']
  if hashlib.sha256(p.read_bytes()).hexdigest()!=item['sha256']:raise ValueError('futures_checksum_mismatch')
 for y,m in months():
  suffix=f'BTCUSDT-15m-{y}-{m:02}.zip'
  for raw in archive_rows(root/suffix):
   if not raw[0].isdigit():continue
   t=int(raw[0]);e=int(raw[6]);o,h,l,c=map(float,raw[1:5])
   if t%BAR or e!=t+BAR-1 or not 0<l<=min(o,c)<=max(o,c)<=h:raise ValueError('bad_futures_candle')
   out.append((t,o,h,l,c))
  for raw in archive_rows(root/('MARK-'+suffix)):
   if not raw[0].isdigit():continue
   t=int(raw[0]);e=int(raw[6]);v=float(raw[1])
   if t%BAR or e!=t+BAR-1 or not math.isfinite(v) or v<=0 or t in marks:raise ValueError('bad_mark_candle')
   marks[t]=v
  for raw in archive_rows(root/f'BTCUSDT-fundingRate-{y}-{m:02}.zip'):
   if not raw[0].isdigit():continue
   t=int(raw[0]);interval=int(raw[1]);rate=float(raw[2])
   if interval<=0 or not math.isfinite(rate):raise ValueError('bad_funding')
   funding.append((t,interval,rate))
 for item in manifest.get('rest_gap_fills',[]):
  raw=(root/item['file']).read_bytes()
  if hashlib.sha256(raw).hexdigest()!=item['sha256']:raise ValueError('rest_checksum_mismatch')
  records=json.loads(raw)
  if len(records)!=item['records']:raise ValueError('rest_count_mismatch')
  for r in records:
   t=int(r[0]);e=int(r[6]);o,h,l,c=map(float,r[1:5])
   if t%BAR or e!=t+BAR-1 or not 0<l<=min(o,c)<=max(o,c)<=h:raise ValueError('bad_rest_candle')
   if item['file'].startswith('REST-klines-'):out.append((t,o,h,l,c))
   elif item['file'].startswith('REST-markPriceKlines-'):
    if t in marks:raise ValueError('duplicate_rest_mark')
    marks[t]=o
   else:raise ValueError('unexpected_rest_source')
 out.sort()
 if not out or any(b[0]-a[0]!=BAR for a,b in zip(out,out[1:])):raise ValueError('futures_gap_or_duplicate')
 if set(marks)!={r[0] for r in out}:raise ValueError('mark_gap_or_duplicate')
 if any(b[0]<=a[0] for a,b in zip(funding,funding[1:])):raise ValueError('funding_duplicate_or_unsorted')
 gaps=[(a[0],b[0]) for a,b in zip(funding,funding[1:]) if b[0]-a[0]!=b[1]*3600000]
 slots=[t//(8*3600000) for t,_,_ in funding];slot_gaps=sum(b!=a+1 for a,b in zip(slots,slots[1:]));intervals=sorted({x[1] for x in funding})
 mapped={}
 for t,interval,rate in funding:
  key=t//BAR*BAR
  if key not in marks:raise ValueError('funding_without_mark')
  mapped.setdefault(key,[]).append((t,rate,marks[key]))
 return out,mapped,{'funding_records':len(funding),'exact_interval_mismatches':len(gaps),'unique_nonoverlapping_8h_slot_gaps':slot_gaps,'reported_intervals_hours':intervals,'funding_mark':'15m mark candle open approximation','funding_completeness':'One event per nonoverlapping 8h slot is a documented paper assumption, not finality proof.','manifest_files':len(manifest['files'])},manifest

def signals(rows,kind):
 """Decision after close i, only information through i. Return intent at i."""
 result=[None]*len(rows);closes=[];high=[];low=[];count={'trend1':4,'trend4':16,'range':1}[kind]
 for i,(t,o,h,l,c) in enumerate(rows):
  if kind=='range':
   if len(closes)>=96:
    hi=max(high[-96:]);lo=min(low[-96:]);mid=(hi+lo)/2
    if hi/lo-1<=.03 and lo<=c<=lo+.2*(hi-lo) and mid/c-1>=.006:result[i]=('BUY',lo,hi,mid)
   closes.append(c);high.append(h);low.append(l)
  elif (t+BAR)%(count*BAR)==0:
   if len(closes)>=48:
    up=max(closes[-48:]);down=min(closes[-48:]);exitlo=min(closes[-24:]);exithi=max(closes[-24:]);result[i]=('TREND',c,up,down,exitlo,exithi)
   closes.append(c)
 return result

def regimes(rows):
 closes=[];out=[];regime='NO_TRADE'
 for t,o,h,l,c in rows:
  if (t+BAR)%(16*BAR)==0:
   closes.append(c)
   if len(closes)>=20:
    mean=sum(closes[-20:])/20;regime='TREND_UP' if c>mean*1.01 else 'TREND_DOWN' if c<mean*.99 else 'RANGE'
  out.append(regime)
 return out

def simulate(rows,intents,reg,start,end,*,variant,side='LONG',impact_bps=5,funding=None,combined=False,other_rows=None,other_intents=None,range_intents=None):
 cash=1000.;equity_peak=1000.;maxdd=0.;position=None;pending=None;closed=[];turn=fees=impact=funding_total=0.;bars=0;rejected=0;sum_exposure=0.;last_mark=0.;wait=BAR
 by_time=funding or {}
 for i,(t,o,h,l,c) in enumerate(rows):
  if t<start:continue
  if t>=end:break
  if combined and other_rows is not None and ((position and position['side']=='SHORT') or (pending and pending['kind']=='entry' and pending['side']=='SHORT' and pending['at']==i)):
   _,o,h,l,c=other_rows[i]
  if position:
   for ft,rate,mark in by_time.get(t,[]) if position['side']=='SHORT' else []:
    if ft>=position['opened'] and ft<t+BAR:
     val=position['qty']*mark*rate*(1 if position['side']=='SHORT' else -1);cash+=val;funding_total+=val;position['funding']+=val
  if pending and pending['at']==i:
   kind=pending['kind'];sig=pending['signal'];direction=pending['side'];pending=None
   if kind=='exit' and position:
    position['exit_next']=True
   elif kind=='entry' and position is None:
    strategy=sig[0]=='BUY'
    if combined and i>0 and reg[i-1]!=('RANGE' if strategy else 'TREND_UP' if direction=='LONG' else 'TREND_DOWN'):rejected+=1
    elif strategy and (i==0 or rows[i-1][3]<sig[1] or rows[i-1][2]>sig[2] or not (sig[1]<=o<=sig[2] and sig[3]/o-1>=.006)):rejected+=1
    else:
     px=o*(1+(impact_bps/10000 if direction=='LONG' else -impact_bps/10000));qty=math.floor((10/px)/STEP)*STEP;notional=qty*px
     if notional<5 or notional*1.001>cash:rejected+=1
     else:
      fee=notional*.001;cash-=fee;fees+=fee;turn+=notional;impact+=abs(px-o)*qty
      position={'side':direction,'qty':qty,'entry':px,'entry_ref':o,'fee':fee,'opened':t,'funding':0.,'target':sig[3] if strategy else None,'strategy':'range' if strategy else variant,'stop':px*(.99 if strategy else (.98 if direction=='LONG' else 1.02))}
  if position:
   p=position;long=p['side']=='LONG';ref=None;reason=None
   if p.pop('exit_next',False):ref=o;reason='signal'
   elif t-p['opened']>=(DAY if p['strategy']=='range' else 7*DAY):ref=o;reason='time'
   elif (long and o<=p['stop']) or (not long and o>=p['stop']):ref=o;reason='stop_gap'
   elif (long and l<=p['stop']) or (not long and h>=p['stop']):ref=p['stop'];reason='stop'
   elif p['strategy']=='range' and h>=p['target']:ref=p['target'];reason='target'
   if ref is not None:
    px=ref*(1-(impact_bps/10000 if long else -impact_bps/10000));fee=p['qty']*px*.001;gross=(ref-p['entry_ref'])*p['qty']*(1 if long else -1);net=(px-p['entry'])*p['qty']*(1 if long else -1)-p['fee']-fee+p['funding'];cash+=(px-p['entry'])*p['qty']*(1 if long else -1)-fee;fees+=fee;turn+=p['qty']*px;impact+=abs(px-ref)*p['qty'];closed.append({'net':net,'gross':gross,'funding':p['funding'],'reason':reason,'hold_ms':t-p['opened']});position=None
  if position:bars+=1;sum_exposure+=position['qty']*c
  exit_mark=(c*(1-(impact_bps/10000 if position['side']=='LONG' else -impact_bps/10000))) if position else 0
  mark=cash+((exit_mark-position['entry'])*position['qty']*(1 if position['side']=='LONG' else -1)-exit_mark*position['qty']*.001 if position else 0);last_mark=mark;equity_peak=max(equity_peak,mark);maxdd=max(maxdd,(equity_peak-mark)/equity_peak)
  mode=reg[i]
  if combined and other_intents is not None:
   sig=(range_intents[i] if (position and position['strategy']=='range') or (position is None and mode=='RANGE') else other_intents[i] if (position and position['side']=='SHORT') or (position is None and mode=='TREND_DOWN') else intents[i])
  else:sig=intents[i]
  if sig and pending is None and i+2<len(rows) and rows[i+2][0]<end:
   if position:
    if position['strategy']!='range' and sig[0]=='TREND' and ((position['side']=='LONG' and sig[1]<sig[4]) or (position['side']=='SHORT' and sig[1]>sig[5])):pending={'at':i+2,'kind':'exit','side':position['side'],'signal':sig}
   elif sig[0]=='BUY' and (not combined or mode=='RANGE'):pending={'at':i+2,'kind':'entry','side':'LONG','signal':sig}
   elif sig[0]=='TREND':
    want='LONG' if sig[1]>sig[2] else 'SHORT' if sig[1]<sig[3] else None
    if want and (side=='BOTH' or side==want) and (not combined or mode==('TREND_UP' if want=='LONG' else 'TREND_DOWN')):pending={'at':i+2,'kind':'entry','side':want,'signal':sig}
 n=sum(1 for r in rows if start<=r[0]<end);total=last_mark-1000
 return {'closed':len(closed),'gross_closed':sum(x['gross'] for x in closed),'net_marked':total,'realized_net':sum(x['net'] for x in closed),'open_net':total-sum(x['net'] for x in closed),'mean_trade_net':sum(x['net'] for x in closed)/len(closed) if closed else None,'mean_holding_hours':sum(x['hold_ms'] for x in closed)/len(closed)/3600000 if closed else None,'funding':funding_total,'fees':fees,'impact':impact,'turnover':turn,'exposure_fraction':bars/n if n else 0,'mean_notional':sum_exposure/n if n else 0,'max_drawdown_pct':maxdd*100,'best_trade':max((x['net'] for x in closed),default=None),'without_best_trade':total-max((x['net'] for x in closed),default=0),'rejected_entries':rejected,'max_entry_wait_minutes':wait/60000,'open_position':position is not None,'exit_reasons':{r:sum(x['reason']==r for x in closed) for r in ('stop','stop_gap','target','time','signal')}}

def main():
 p=argparse.ArgumentParser();p.add_argument('--data',required=True);p.add_argument('--futures',required=True);p.add_argument('--output',required=True);a=p.parse_args();root=Path(a.data);verify_archives(root)
 manifest2026=json.loads(Path('research/spot_independent_manifest_2026.json').read_text())
 for item in manifest2026['files']:
  if item['file'].startswith('BTCUSDT-') and hashlib.sha256((root/item['file']).read_bytes()).hexdigest()!=item['sha256']:raise ValueError('spot_2026_checksum_mismatch')
 spot=load_symbol('BTCUSDT',root)+load_2026('BTCUSDT',root);fut,funding,quality,manifest=futures_data(a.futures)
 common=stamp(2023,12);spot=[x for x in spot if x[0]>=common];assert [x[0] for x in spot]==[x[0] for x in fut]
 regs=regimes(spot);results={}
 for name in ('trend1','trend4','range'):
  for side in (('LONG','SHORT') if name!='range' else ('LONG',)):
   data=fut if side=='SHORT' else spot;sig=signals(data,name)
   for year in (2024,2025,2026):
    for bps in (5,10):
     results[f'{name}|{side}|{year}|{bps}']=simulate(data,sig,regs,stamp(year),stamp(year+1) if year<2026 else stamp(2026,11),variant=name,side=side,impact_bps=bps,funding=funding if side=='SHORT' else None)
  print('completed',name,flush=True)
 for name in ('trend1','trend4'):
  long_signals=signals(spot,name);short_signals=signals(fut,name);range_signals=signals(spot,'range')
  for year in (2024,2025,2026):
   for bps in (5,10):
    results[f'{name}+range|COMBINED|{year}|{bps}']=simulate(spot,long_signals,regs,stamp(year),stamp(year+1) if year<2026 else stamp(2026,11),variant=name,side='BOTH',impact_bps=bps,funding=funding,combined=True,other_rows=fut,other_intents=short_signals,range_intents=range_signals)
  print('completed combined',name,flush=True)
 out={'quality':quality,'data_end_exclusive_utc':'2026-11-01','results':results,'limitations':['No historical bid/ask/depth or partial fills; no live execution proof.','Funding uses actual rates and 15m mark-candle open approximation.','AI not involved in history; 2024–2026 are development data.'],'futures_manifest':manifest}
 Path(a.output).write_text(json.dumps(out,indent=2,sort_keys=True));print('saved',a.output,flush=True)
if __name__=='__main__':main()

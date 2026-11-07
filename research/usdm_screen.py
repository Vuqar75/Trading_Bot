"""Frozen, taker-only USD-M OHLC screening; no private endpoints or orders."""
import argparse,bisect,datetime as dt,gc,hashlib,json,math
from collections import Counter,deque
from pathlib import Path
from research.intraday_spot import SEGMENTS,STEPS,SYMBOLS,stamp
from research.usdm_intraday_data import read

H=3600000
FRAMES={'1m':(60000,360,180,720),'3m':(180000,120,60,240),'5m':(300000,72,36,144),'15m':(900000,24,12,48),'30m':(1800000,12,6,24),
 '1h':(H,48,24,96),'2h':(2*H,24,12,48),'4h':(4*H,12,6,24),'6h':(6*H,8,4,16),'8h':(8*H,6,3,12),'12h':(12*H,4,2,8),'1d':(24*H,60,30,60),'3d':(72*H,60,30,60),'1w':(168*H,60,30,60)}
def allowed(tf):return ('A','B','C','D') if tf in ('1m','3m','5m','15m','30m') else ('A','B') if tf=='1w' else ('A','B','C')
def aggregates(rows,step,span,anchor=0):
 if span%step:raise ValueError('unaligned interval')
 n=span//step;start=((span-(rows[0][0]-anchor)%span)%span)//step
 for i in range(start,len(rows)-n+1,n):
  r=rows[i:i+n];t=r[0][0]
  if any(x[0]!=t+j*step for j,x in enumerate(r)):raise ValueError('gap in aggregate')
  yield (t,r[0][1],max(x[2] for x in r),min(x[3] for x in r),r[-1][4],sum(x[5] for x in r))
def signals(rows,step,tf):
 span,n6,n3,n12=FRAMES[tf];anchor=4*24*H if tf=='1w' else 0
 qmax=deque();qmin=deque();qexitmax=deque();qexitmin=deque();qhi=deque();qlo=deque();closes=deque();volumes=deque();volume_entry=deque();sumc=sumv=sum_entry_volume=0.;previous=None;prevmean=None;prevflags={k:False for k in ('al','as','cl','cs')};out={}
 def add(q,i,x,maximise):
  while q and (q[-1][1]<=x if maximise else q[-1][1]>=x):q.pop()
  q.append((i,x))
 for i,(t,o,h,l,c,v) in enumerate(aggregates(rows,step,span,anchor)):
  for q,n in ((qmax,n6),(qmin,n6),(qexitmax,n3),(qexitmin,n3),(qhi,n12),(qlo,n12)):
   while q and q[0][0]<i-n:q.popleft()
  if len(closes)>=n12 and i>=max(n6,n3,n12) and previous is not None and prevmean is not None:
   mean=sumc/n12;high=qhi[0][1];low=qlo[0][1];mid=(high+low)/2;width=high-low
   al=c>qmax[0][1];ash=c<qmin[0][1];inside=width/mid<=.02
   cl=inside and low<=c<=low+.2*width;cs=inside and high-.2*width<=c<=high
   av=sum_entry_volume/n6
   event={'A':(al and not prevflags['al'],ash and not prevflags['as'],c<qexitmin[0][1],c>qexitmax[0][1]),
    'B':(mean>prevmean and previous<=prevmean and c>mean,mean<prevmean and previous>=prevmean and c<mean,c<mean,c>mean),
    'C':(cl and not prevflags['cl'],cs and not prevflags['cs'],False,False),
    'D':(av>0 and c>previous and c>o and (c/o-1)>=.0025 and v>=1.5*av,av>0 and c<previous and c<o and (1-c/o)>=.0025 and v>=1.5*av,c<previous,c>previous),
    'lo':low,'hi':high,'mid':mid,'close':c,'prev_high':prev_h,'prev_low':prev_l,'regime':'range' if inside else 'trend'}
   event['D']=(event['D'][0] and c>prev_h,event['D'][1] and c<prev_l,event['D'][2],event['D'][3])
   out[(t+span-rows[0][0])//step-1]=event
   prevflags.update(al=al,**{'as':ash},cl=cl,cs=cs);prevmean=mean
  elif len(closes)>=n12:prevmean=sumc/n12
  if len(closes)==n12:sumc-=closes.popleft();sumv-=volumes.popleft()
  if len(volume_entry)==n6:sum_entry_volume-=volume_entry.popleft()
  volume_entry.append(v);sum_entry_volume+=v
  closes.append(c);sumc+=c;volumes.append(v);sumv+=v;previous=c;prev_h=h;prev_l=l
  add(qmax,i,c,True);add(qmin,i,c,False);add(qexitmax,i,c,True);add(qexitmin,i,c,False);add(qhi,i,h,True);add(qlo,i,l,False)
 return out

def simulate(rows,marks,funding,events,symbol,tf,family,side,start,end,*,impact=5,delay=0,fee_bps=5,with_funding=True):
 step=rows[1][0]-rows[0][0];first=rows[0][0];begin=(start-first)//step;finish=(end-first)//step
 if begin<0 or finish>len(rows) or begin>=finish or len(marks)!=len(rows):raise ValueError('data window missing')
 fee=fee_bps/10000;slip=impact/10000;cash=1000.;peak=1000.;maxdd=0.;pos=None;pending=None;completed=[];marked=[];entries=[];reject=Counter();reasons=Counter();fees=friction=funding_net=turn=0.;exposed=0;worststreak=streak=0;ambiguous=liquidations=0
 span=FRAMES[tf][0];hold=(2*H if family=='D' else (6*H if family=='C' and tf in ('5m','15m') else 12*H if family=='A' and tf in ('5m','15m') else FRAMES[tf][1]*span*(2 if family=='A' else 1)))
 # Flat stretches can jump to the next relevant entry without changing PnL.
 relevant=[]
 for i,e in events.items():
  if begin<=i<finish:
   lo,sh=e[family][:2]
   if (lo and side!='SHORT') or (sh and side!='LONG'):relevant.append(i)
 relevant.sort()
 def close(raw,t,reason,closed=True,liquidation_fee=0.):
  nonlocal cash,pos,fees,friction,turn,streak,worststreak,liquidations
  direction=pos['direction'];q=pos['qty'];sgn=1 if direction=='LONG' else -1;price=raw*(1-slip*sgn);value=q*price;commission=value*fee;liq=value*liquidation_fee
  gross=q*(raw-pos['entry_raw'])*sgn;net=q*(price-pos['entry'])*sgn-pos['entry_fee']-commission-liq+pos['funding']
  cash+=q*(price-pos['entry'])*sgn-commission-liq;fees+=commission+liq;friction+=q*abs(price-raw);turn+=value
  record={'direction':direction,'gross':gross,'net':net,'entry_notional':pos['entry']*q,'funding':pos['funding'],'hold_h':(t-pos['opened'])/H,'reason':reason,'closed':closed,'entry_ms':pos['opened'],'exit_ms':t}
  (completed if closed else marked).append(record)
  if closed:
   reasons[reason]+=1;streak=streak+1 if net<0 else 0;worststreak=max(worststreak,streak)
   if reason=='liquidation':liquidations+=1
  pos=None
 i=begin
 while i<finish:
  if pos is None and pending is None:
   j=bisect.bisect_left(relevant,i)
   if j==len(relevant):break
   i=relevant[j]
  t,o,h,l,c,_=rows[i];mt,mo,mh,ml,mc,_=marks[i]
  if t!=mt:raise ValueError('trade/mark mismatch')
  if pending and pending['index']==i:
   action=pending;pending=None
   if action['kind']=='exit' and pos:close(o,t,action['reason'])
   elif action['kind']=='entry' and pos is None:
    d=action['direction'];sgn=1 if d=='LONG' else -1;price=o*(1+slip*sgn);sig=action['signal']
    if family=='C' and (not sig['lo']<=price<=sig['hi'] or ((sig['mid']/price-1) if d=='LONG' else (price/sig['mid']-1))<.006):reject['range_or_distance']+=1
    else:
     q=math.floor((10/price)/STEPS[symbol]+1e-10)*STEPS[symbol];v=q*price;entryfee=v*fee
     if v<5:reject['min_notional_assumption']+=1
     elif v+entryfee>cash:reject['capital']+=1
     else:
      cash-=entryfee;fees+=entryfee;friction+=q*abs(price-o);turn+=v;entries.append(t)
      pos={'direction':d,'qty':q,'entry':price,'entry_raw':o,'entry_fee':entryfee,'opened':t,'funding':0.,'lo':sig['lo'],'hi':sig['hi'],'mid':sig['mid']}
  if pos:
   exposed+=1;d=pos['direction'];sgn=1 if d=='LONG' else -1;q=pos['qty'];stop=pos['entry']*(1-.0075*sgn);target=pos['mid'] if family=='C' else None
   stop_hit=(l<=stop if d=='LONG' else h>=stop);mark_extreme=ml if d=='LONG' else mh
   for ft,rate in funding.get(i,[]) if with_funding else []:
    if ft<pos['opened']:continue
    amount=-sgn*q*mo*rate
    if stop_hit and ft>t:
     ambiguous+=1
     # Order inside this OHLC bar is unknown: count any debit, do not assert a credit.
     amount=min(amount,0.)
    pos['funding']+=amount;cash+=amount;funding_net+=amount
   margin=q*pos['entry']-pos['entry_fee']+pos['funding'];equity=margin+sgn*q*(mark_extreme-pos['entry']);liquidate=equity<=.005*q*mark_extreme
   if liquidate:
    close(l if d=='LONG' else h,t+step,'liquidation',liquidation_fee=.01);pending=None
   elif stop_hit:
    raw=min(o,stop) if d=='LONG' else max(o,stop);close(raw,t+step,'stop');pending=None
   elif target and ((h>=target) if d=='LONG' else (l<=target)) and not pending:pending={'kind':'exit','index':i+1+delay,'reason':'target'}
   elif t+step-pos['opened']>=hold and not pending:pending={'kind':'exit','index':i+1+delay,'reason':'time'}
  event=events.get(i)
  if event:
   lo,sh,exitlong,exitshort=event[family]
   if pos:
    exit_signal=(exitlong if pos['direction']=='LONG' else exitshort)
    if family=='C':exit_signal=(c<pos['lo'] if pos['direction']=='LONG' else c>pos['hi'])
    if exit_signal and not pending:pending={'kind':'exit','index':i+1+delay,'reason':'signal'}
   elif not pending:
    longok=bool(lo and side!='SHORT');shortok=bool(sh and side!='LONG')
    if longok and shortok:reject['conflict']+=1
    elif longok or shortok:pending={'kind':'entry','index':i+1+delay,'direction':'LONG' if longok else 'SHORT','signal':event}
  val=cash+(q*(mc-pos['entry'])*sgn-q*mc*(fee+slip) if pos else 0.);peak=max(peak,val);maxdd=max(maxdd,(peak-val)/peak)
  i+=1
 if pending:reject['end_pending']+=1
 open_result=None
 if pos:
  close(rows[finish-1][4],end,'mark_to_market',closed=False);open_result=marked[-1]['net']
  maxdd=max(maxdd,(peak-cash)/peak)
 trades=completed+marked;n=len(completed);wait=[(b-a)/H for a,b in zip(entries,entries[1:])]
 return {'net':cash-1000,'gross':sum(x['gross'] for x in trades),'funding':funding_net,'fees':fees,'impact':friction,'turnover':turn,'entries':len(entries),'closed':n,'open_mark_net':open_result,'exposure_pct':100*exposed/(finish-begin),'max_dd_pct':100*maxdd,'loss_streak':worststreak,'liquidations':liquidations,'ambiguous_funding':ambiguous,'mean_trade':sum(x['net'] for x in completed)/n if n else None,'mean_bps':sum(x['net']/x['entry_notional']*10000 for x in completed)/n if n else None,'mean_gross_bps':sum(x['gross']/x['entry_notional']*10000 for x in completed)/n if n else None,'mean_cost_bps':sum((x['gross']+x['funding']-x['net'])/x['entry_notional']*10000 for x in completed)/n if n else None,'best_trade':max((x['net'] for x in trades),default=0),'without_best_trade':sum(x['net'] for x in trades)-max((x['net'] for x in trades),default=0),'max_wait_h':max(wait) if wait else None,'mean_wait_h':sum(wait)/len(wait) if wait else None,'rejections':dict(reject),'exit_reasons':dict(reasons)}

def main():
 p=argparse.ArgumentParser();p.add_argument('--data',required=True);p.add_argument('--cache',required=True);p.add_argument('--output',required=True);p.add_argument('--refresh-d-from',help='Recompute only D after correcting its volume window');args=p.parse_args();root=Path(args.data);cache=Path(args.cache);cache.mkdir(parents=True,exist_ok=True)
 manifest=json.loads((root/'manifest.json').read_text());minute=json.loads((root/'minute-manifest.json').read_text())
 code=hashlib.sha256(b''.join(Path(p).read_bytes() for p in (__file__,'research/usdm_intraday_data.py','research/intraday_spot.py','research/usdm_risk.py'))).hexdigest();plan=hashlib.sha256(Path('USDM_TIMEFRAME_MATRIX_PLAN.md').read_bytes()+Path('USDM_INTRADAY_PLAN.md').read_bytes()).hexdigest();inputs=hashlib.sha256(json.dumps([manifest,minute],sort_keys=True).encode()).hexdigest();version=hashlib.sha256((code+plan+inputs).encode()).hexdigest()
 prior=json.loads(Path(args.refresh_d_from).read_text()) if args.refresh_d_from else None
 if prior and (prior['plan_sha256']!=plan or prior['input_manifest_sha256']!=inputs):raise ValueError('prior parameters or source differ')
 out={k:v for k,v in prior['results'].items() if '|D|' not in k} if prior else {};quality=prior['quality'] if prior else {}
 for symbol in SYMBOLS:
  funding_rows=read(root,manifest,symbol,'fundingRate');slots=[x[0]//(8*H) for x in funding_rows]
  if any(b!=a+1 for a,b in zip(slots,slots[1:])):raise ValueError('unconfirmed funding coverage '+symbol)
  for interval,frames in (('1m',('1m','3m')),('5m',tuple(k for k in FRAMES if k not in ('1m','3m')))):
   source=minute if interval=='1m' else manifest;step=60000 if interval=='1m' else 300000
   rows=read(root,source,symbol,'klines',interval=interval);marks=read(root,source,symbol,'markPriceKlines',interval=interval)
   if len(rows)!=len(marks) or any(a[0]!=b[0] for a,b in zip(rows,marks)):raise ValueError('mark and trade coverage disagree')
   fund={}
   for t,_,rate in funding_rows:fund.setdefault((t-rows[0][0])//step,[]).append((t,rate))
   quality[symbol+'|'+interval]={'trade_bars':len(rows),'mark_bars':len(marks),'funding_records':len(funding_rows),'funding_slot_gaps':0,'first_ms':rows[0][0],'last_ms':rows[-1][0]}
   for tf in frames:
    if prior and 'D' not in allowed(tf):continue
    name=f'{symbol}-{tf}-{version}.json';path=cache/name
    if path.exists():
     item=json.loads(path.read_text());out.update(item['results']);print('cached',symbol,tf,flush=True);continue
    ev=signals(rows,step,tf);item={}
    for family in (('D',) if prior else allowed(tf)):
     for side in ('LONG','SHORT','BOTH'):
      for segment,begin,end in SEGMENTS:
       for scenario,impact,delay in (('base',5,0),('stress',10,1)):
        k=f'{tf}|{family}|{side}|{symbol}|{segment}|{scenario}'
        item[k]=simulate(rows,marks,fund,ev,symbol,tf,family,side,stamp(begin),stamp(end),impact=impact,delay=delay)
       if tf in ('5m','15m') and family in ('A','C') and side=='LONG':
        k=f'{tf}|{family}|{side}|{symbol}|{segment}|spot_fee_no_funding'
        item[k]=simulate(rows,marks,fund,ev,symbol,tf,family,side,stamp(begin),stamp(end),impact=5,delay=0,fee_bps=10,with_funding=False)
    tmp=path.with_suffix('.tmp');tmp.write_text(json.dumps({'key':version,'results':item}));tmp.replace(path);out.update(item);del ev
    print('completed',symbol,tf,'results',len(item),flush=True)
   del rows,marks,fund;gc.collect()
 result={'created_utc':dt.datetime.now(dt.timezone.utc).isoformat(),'cache_key':version,'code_sha256':code,'plan_sha256':plan,'input_manifest_sha256':inputs,'quality':quality,'results':out}
 if prior:result['d_correction_from_sha256']=hashlib.sha256(Path(args.refresh_d_from).read_bytes()).hexdigest()
 Path(args.output).write_text(json.dumps(result),encoding='utf8')
if __name__=='__main__':main()

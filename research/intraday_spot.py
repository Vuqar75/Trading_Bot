"""Fixed 5m/15m Spot comparison; public OHLC archives, no accounts/orders."""
import argparse,csv,datetime as dt,hashlib,io,json,math,os,time,urllib.request,zipfile
from collections import Counter,deque
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

BAR=300000
SYMBOLS=('BTCUSDT','ETHUSDT','SOLUSDT')
STEPS={'BTCUSDT':.00001,'ETHUSDT':.0001,'SOLUSDT':.001}
BASE='https://data.binance.vision/data/spot/monthly/klines'
SEGMENTS=(('2025H1','2025-01-01','2025-07-01'),('2025H2','2025-07-01','2026-01-01'),('2026A','2026-01-01','2026-06-01'),('2026B','2026-06-01','2026-11-01'))
def stamp(s):return int(dt.datetime.fromisoformat(s).replace(tzinfo=dt.timezone.utc).timestamp()*1000)
def require_closed(now_ms,end_ms):
 if type(now_ms) is not int or type(end_ms) is not int or now_ms<end_ms:raise ValueError('period is not fully closed')
def get(url):
 for n in range(3):
  try:
   with urllib.request.urlopen(url,timeout=45) as response:return response.read()
  except OSError:
   if n==2:raise
   time.sleep(2**n)
def download(item):
 symbol,year,month,root=item;name=f'{symbol}-5m-{year}-{month:02d}.zip';url=f'{BASE}/{symbol}/5m/{name}';path=root/name
 checksum=get(url+'.CHECKSUM').decode().split()[0].lower()
 if len(checksum)!=64:raise ValueError('invalid source checksum')
 if not path.exists() or hashlib.sha256(path.read_bytes()).hexdigest()!=checksum:
  data=get(url)
  if hashlib.sha256(data).hexdigest()!=checksum:raise ValueError('archive checksum mismatch '+name)
  tmp=path.with_suffix('.tmp');tmp.write_bytes(data);os.replace(tmp,path)
 return {'file':name,'sha256':checksum,'bytes':path.stat().st_size,'url':url,'checked_utc':dt.datetime.now(dt.timezone.utc).isoformat()}
def months():return [(2024,12)]+[(2025,m) for m in range(1,13)]+[(2026,m) for m in range(1,11)]
def load(symbol,root):
 rows=[]
 for year,month in months():
  with zipfile.ZipFile(root/f'{symbol}-5m-{year}-{month:02d}.zip') as z:
   names=z.namelist()
   if len(names)!=1:raise ValueError('unexpected archive entries')
   for r in csv.reader(io.TextIOWrapper(z.open(names[0]))):
    if not r[0].isdigit():continue
    t,end=int(r[0]),int(r[6]);scale=1000 if t>10**14 else 1;t//=scale;end//=scale
    o,h,l,c,v=map(float,r[1:6])
    if t%BAR or end!=t+BAR-1 or not (0<l<=min(o,c)<=max(o,c)<=h) or v<0 or not all(map(math.isfinite,(o,h,l,c,v))):raise ValueError('invalid candle '+symbol+' '+str(t))
    rows.append((t,o,h,l,c,v))
 start=stamp('2024-12-01');end=stamp('2026-11-01')
 if len(rows)!=(end-start)//BAR or any(row[0]!=start+i*BAR for i,row in enumerate(rows)):raise ValueError('gap, duplicate or boundary error '+symbol)
 return rows
def aggregate_minutes(rows,source_ms,factor):
 if source_ms not in (60000,300000) or type(factor) is not int or factor<1 or len(rows)%factor:raise ValueError('incomplete aggregation')
 out=[]
 for i in range(0,len(rows),factor):
  group=rows[i:i+factor];t=group[0][0]
  if t%(factor*source_ms) or any(r[0]!=t+j*source_ms for j,r in enumerate(group)):raise ValueError('aggregation discontinuity')
  out.append((t,group[0][1],max(r[2] for r in group),min(r[3] for r in group),group[-1][4],sum(r[5] for r in group)))
 return out
def aggregate(rows,factor):
 if factor not in (1,3):raise ValueError('unsupported timeframe')
 return aggregate_minutes(rows,BAR,factor)
def calculate(rows,factor):
 """Previous-only windows; mapping keyed by close time, with edge crossings."""
 bars=aggregate(rows,factor);n6=72//factor;n3=36//factor;n12=144//factor
 signals={};prev_break=False;prev_range=False
 # Monotonic deques provide prior-only high/low with O(n) work.
 high6=deque();low3=deque();high12=deque();low12=deque()
 def add(q,i,v,is_max):
  while q and (q[-1][1]<=v if is_max else q[-1][1]>=v):q.pop()
  q.append((i,v))
 for i,(t,o,h,l,c,v) in enumerate(bars):
  for q,n in ((high6,n6),(low3,n3),(high12,n12),(low12,n12)):
   while q and q[0][0]<i-n:q.popleft()
  if i>=n12:
   hi=high12[0][1];lo=low12[0][1];mid=(hi+lo)/2
   above=c>high6[0][1];inside=(hi-lo)/mid<=.02 and lo<=c<=lo+.2*(hi-lo)
   signals[t+factor*BAR]={'break_buy':above and not prev_break,'break_sell':c<low3[0][1],
      'range_buy':inside and not prev_range,'lo':lo,'mid':mid,'hi':hi,
      'regime':'range' if (hi-lo)/mid<=.02 else 'trend','close':c}
   prev_break=above;prev_range=inside
  add(high6,i,c,True);add(low3,i,c,False);add(high12,i,h,True);add(low12,i,l,False)
 return signals

def simulate(rows,signals,symbol,start,end,kind,impact=5,delay=0):
 cash=1000.;qty=0.;entry_raw=entry_px=entry_cost=0.;opened=0;pos=None;pending=None
 trades=[];turnover=fees=friction=0.;rejected=Counter();exit_reasons=Counter();entry_times=[];inbars=0;peak=1000.;maxdd=0.;streak=longest=0;ambiguous=0
 slippage=impact/10000;fee_rate=.001
 def buy(raw,t,proposal):
  nonlocal cash,qty,entry_raw,entry_px,entry_cost,opened,pos,turnover,fees,friction
  price=raw*(1+slippage)
  if kind=='range' and (not proposal['lo']<=price<=proposal['hi'] or proposal['mid']/price-1<.006):rejected['range_or_distance_at_fill']+=1;return
  q=math.floor((10/price)/STEPS[symbol]+1e-10)*STEPS[symbol];value=q*price;fee=value*fee_rate
  if value<5:rejected['minimum']+=1;return
  if value+fee>cash:rejected['cash']+=1;return
  qty=q;entry_raw=raw;entry_px=price;entry_cost=value+fee;cash-=entry_cost;opened=t;turnover+=value;fees+=fee;friction+=q*(price-raw)
  pos={'regime':proposal['regime'],'lo':proposal['lo'],'mid':proposal['mid'],'transition':False};entry_times.append(t)
 def sell(raw,t,reason,closed=True):
  nonlocal cash,qty,turnover,fees,friction,streak,longest,pos
  price=raw*(1-slippage);value=qty*price;fee=value*fee_rate;gross=qty*(raw-entry_raw);net=value-fee-entry_cost
  cash+=value-fee;turnover+=value;fees+=fee;friction+=qty*(raw-price)
  trades.append({'net':net,'gross':gross,'entry_notional':qty*entry_px,'hold_h':(t-opened)/3600000,'reason':reason,'regime':pos['regime'],'transition':pos['transition'],'closed':closed,'t':t})
  if closed:
   exit_reasons[reason]+=1;streak=streak+1 if net<0 else 0;longest=max(longest,streak)
  qty=0.;pos=None
 idx0=(start-rows[0][0])//BAR;idx1=(end-rows[0][0])//BAR
 if idx0<0 or idx1>len(rows) or idx0>=idx1:raise ValueError('period outside data')
 for i in range(idx0,idx1):
  t,o,h,l,c,_=rows[i]
  if pending and pending['at']==t:
   event=pending;pending=None
   if event['side']=='BUY' and not qty:buy(o,t,event['signal'])
   elif event['side']=='SELL' and qty:sell(o,t,event['reason'])
  if qty:
   inbars+=1;stop=entry_px*.9925;target=pos['mid'] if kind=='range' else None
   if target and l<=stop and h>=target:ambiguous+=1
   if o<=stop:sell(o,t,'stop_gap');pending=None
   elif l<=stop:sell(stop,t+BAR,'stop');pending=None
   elif target and h>=target and not pending:pending={'side':'SELL','at':t+BAR+delay*BAR,'reason':'target'}
   elif t+BAR-opened>=((6 if kind=='range' else 12)*3600000) and not pending:pending={'side':'SELL','at':t+BAR+delay*BAR,'reason':'time'}
  sig=signals.get(t+BAR)
  if sig:
   if qty:
    if sig['break_buy' if kind=='breakout' else 'range_buy']:rejected['position_open']+=1
    if kind=='range' and c<pos['lo']:pos['transition']=True
    exit_signal=(sig['break_sell'] if kind=='breakout' else c<pos['lo'])
    if exit_signal and not pending:pending={'side':'SELL','at':t+BAR+delay*BAR,'reason':'signal'}
   elif sig['break_buy' if kind=='breakout' else 'range_buy']:
    if pending:rejected['pending']+=1
    else:pending={'side':'BUY','at':t+BAR+delay*BAR,'signal':sig}
  equity=cash+qty*c*(1-slippage)*(1-fee_rate);peak=max(peak,equity);maxdd=max(maxdd,(peak-equity)/peak)
 if pending:rejected['period_end_pending']+=1
 open_net=None
 if qty:
  last=rows[idx1-1][4];open_net=qty*last*(1-slippage)*(1-fee_rate)-entry_cost;sell(last,end,'mark_to_market',False)
 closed=[x for x in trades if x['closed']];p=[x['net'] for x in trades];wait=[(b-a)/3600000 for a,b in zip(entry_times,entry_times[1:])]
 return {'net':cash-1000,'gross':sum(x['gross'] for x in trades),'costs':fees+friction,'fees':fees,'spread_slip':friction,'turnover':turnover,
   'closed_trades':len(closed),'entries':len(entry_times),'rejected':dict(rejected),'exit_reasons':dict(exit_reasons),'open_mark_net':open_net,
   'avg_trade':sum(x['net'] for x in closed)/len(closed) if closed else None,'avg_bps':sum(x['net']/x['entry_notional']*10000 for x in closed)/len(closed) if closed else None,
   'avg_gross_bps':sum(x['gross']/x['entry_notional']*10000 for x in closed)/len(closed) if closed else None,'avg_cost_bps':sum((x['gross']-x['net'])/x['entry_notional']*10000 for x in closed)/len(closed) if closed else None,
   'exposure_pct':100*inbars/(idx1-idx0),'avg_hold_h':sum(x['hold_h'] for x in closed)/len(closed) if closed else None,'max_dd_pct':maxdd*100,
   'longest_loss_streak':longest,'without_best_trade':sum(p)-max(p) if p else 0,'best_trade':max(p) if p else None,
   'mean_entry_spacing_h':sum(wait)/len(wait) if wait else None,'max_entry_spacing_h':max(wait) if wait else None,
   'execution_delay_minutes':5*delay,'ambiguous_stop_target':ambiguous,
   'regimes':{name:{'net':sum(x['net'] for x in trades if x['regime']==name),'count':sum(x['regime']==name for x in trades)} for name in ('range','trend')},
   'range_transition':{'net':sum(x['net'] for x in trades if x['transition']),'count':sum(x['transition'] for x in trades)},'trades':trades}

def baseline(rows,symbol,start,end,impact,delay):
 i=(start-rows[0][0])//BAR+delay;j=(end-rows[0][0])//BAR-1
 if i<0 or j>=len(rows) or i>j:raise ValueError('baseline period outside data')
 a=rows[i][1];b=rows[j][4];price=a*(1+impact/10000)
 qty=math.floor((10/price)/STEPS[symbol]+1e-10)*STEPS[symbol]
 if qty*price<5:return {'cash':0.,'buy_hold_net':None,'reason':'minimum'}
 return {'cash':0.,'buy_hold_net':qty*b*(1-impact/10000)*.999-qty*price*1.001,'open_marked':True,'entry_notional':qty*price}

def compare_original_15m(symbol,root,rows):
 pins={x['file']:x['sha256'] for name in ('strategy_manifest.json','spot_independent_manifest_2026.json') for x in json.loads(Path('research',name).read_text(encoding='utf-8'))['files']}
 actual=aggregate(rows,3);index=0;mismatches=[];count=0
 for year,month in months():
  name=f'{symbol}-15m-{year}-{month:02d}.zip';p=root/name
  if name not in pins or hashlib.sha256(p.read_bytes()).hexdigest()!=pins[name]:raise ValueError('15m archive pin mismatch '+name)
  with zipfile.ZipFile(p) as z:
   if len(z.namelist())!=1:raise ValueError('15m archive entries')
   for r in csv.reader(io.TextIOWrapper(z.open(z.namelist()[0]))):
    if not r[0].isdigit():continue
    t=int(r[0]);end=int(r[6]);scale=1000 if t>10**14 else 1;t//=scale;end//=scale
    if index>=len(actual) or end!=t+3*BAR-1 or t!=actual[index][0]:raise ValueError('15m continuity mismatch')
    values=list(map(float,r[1:6]));expected=actual[index][1:]
    if any(not math.isclose(x,y,rel_tol=1e-9,abs_tol=1e-8) for x,y in zip(values,expected)):
     if len(mismatches)<3:mismatches.append(t)
     count+=1
    index+=1
 if index!=len(actual):raise ValueError('15m length mismatch')
 return {'bars':index,'ohlcv_mismatch_count':count,'first_mismatch_ms':mismatches,'source':'pinned original Binance Spot 15m'}

def main():
 p=argparse.ArgumentParser();p.add_argument('--data',required=True);p.add_argument('--output',required=True);p.add_argument('--manifest',help='Previously saved source manifest, verified offline');p.add_argument('--original-15m',required=True);p.add_argument('--range-only-from',help='Retain prior breakout and baseline results when correcting range execution');args=p.parse_args();root=Path(args.data);root.mkdir(parents=True,exist_ok=True)
 require_closed(int(time.time()*1000),stamp('2026-11-01'))
 req=[(s,y,m,root) for s in SYMBOLS for y,m in months()]
 if args.manifest:
  manifest=json.loads(Path(args.manifest).read_text())
  if {m['file'] for m in manifest}!={f'{s}-5m-{y}-{m:02d}.zip' for s,y,m,_ in req}:raise ValueError('manifest incomplete')
  for item in manifest:
   if hashlib.sha256((root/item['file']).read_bytes()).hexdigest()!=item['sha256']:raise ValueError('cached archive changed')
 else:
  with ThreadPoolExecutor(max_workers=4) as pool:manifest=list(pool.map(download,req))
 prior=json.loads(Path(args.range_only_from).read_text()) if args.range_only_from else None
 results={k:v for k,v in prior['results'].items() if k.startswith('breakout|')} if prior else {}
 quality={};baselines=prior['baselines'] if prior else {}
 for symbol in SYMBOLS:
  rows=load(symbol,root);quality[symbol]={'bars_5m':len(rows),'first_ms':rows[0][0],'last_ms':rows[-1][0],'gaps':0,'duplicates':0,'invalid':0}
  quality[symbol]['compare_15m']=compare_original_15m(symbol,Path(args.original_15m),rows)
  if not prior:
   for label,begin,end in SEGMENTS:
    for scenario,impact,delay in (('base',5,0),('cost_only',10,0),('delay_only',5,1),('stress',10,1)):
     baselines[f'{symbol}|{label}|{scenario}']=baseline(rows,symbol,stamp(begin),stamp(end),impact,delay)
  for factor in (1,3):
   signals=calculate(rows,factor)
   for label,begin,end in SEGMENTS:
    for kind in (('range',) if prior else ('breakout','range')):
     for scenario,impact,delay in (('base',5,0),('cost_only',10,0),('delay_only',5,1),('stress',10,1)):
      key=f'{kind}|{factor*5}m|{symbol}|{label}|{scenario}'
      results[key]=simulate(rows,signals,symbol,stamp(begin),stamp(end),kind,impact,delay)
   print('complete',symbol,factor*5,flush=True)
 output={'created_utc':dt.datetime.now(dt.timezone.utc).isoformat(),'manifest':manifest,'quality':quality,'baselines':baselines,'results':results}
 if prior:output['range_correction_from_sha256']=hashlib.sha256(Path(args.range_only_from).read_bytes()).hexdigest()
 Path(args.output).write_text(json.dumps(output,ensure_ascii=False),encoding='utf-8')
if __name__=='__main__':main()

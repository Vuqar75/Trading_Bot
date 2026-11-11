"""Frozen 2h extreme reversal adaptation; isolated historical research only."""
import csv,datetime as dt,gzip,hashlib,json,math,pathlib,sys
from collections import Counter
from decimal import Decimal,ROUND_DOWN
import numpy as np
from research.usdm_intraday_data import zipped
from research.usdm_risk import funding as funding_amount, liquidation

M=60000;BAR=120;UTC=dt.timezone.utc
def stamp(s):return int(dt.datetime.fromisoformat(s).replace(tzinfo=UTC).timestamp()*1000)
START=stamp('2024-12-01');BEGIN=stamp('2025-01-01');END=stamp('2026-11-01');N=(END-START)//M
SEGMENTS=[('2025H1',stamp('2025-01-01'),stamp('2025-07-01')),('2025H2',stamp('2025-07-01'),stamp('2026-01-01')),('2026H1',stamp('2026-01-01'),stamp('2026-07-01')),('2026H2',stamp('2026-07-01'),stamp('2026-11-01'))]
def ix(t):
 if t%M:raise ValueError('minute alignment')
 return (t-START)//M
def segment(t):
 for name,a,b in SEGMENTS:
  if a<=t<b:return name
 return None

def load_data(root,symbol):
 manifest=json.loads((root/'extreme-restore-manifest.json').read_text())
 if manifest['unavailable']:raise ValueError('source restore incomplete')
 trade=np.full((N,4),np.nan);mark=np.full((N,4),np.nan);fund=[];proof=[];overlap=[]
 files=[x for x in manifest['files'] if symbol in x['file']]
 # Archives before REST; retain primary archive when an overlapping REST version differs.
 files.sort(key=lambda x:('/rest/' in x['file'],x['file']))
 for x in files:
  path=root/x['file'];raw=path.read_bytes();sha=hashlib.sha256(raw).hexdigest()
  if sha!=x['sha256']:raise ValueError('source hash mismatch')
  name=x['file'];proof.append({'file':name,'sha256':sha})
  is_rest='/rest/' in name;kind='funding' if '/fundingRate/' in '/'+name else 'mark' if 'markPriceKlines' in name else 'trade'
  data=json.loads(raw) if is_rest else zipped(path)
  for r in data:
   if not str(r[0]).isdigit():continue
   t=int(r[0])
   if kind=='funding':
    gap,rate=int(r[1]),float(r[2])
    if gap<=0 or not math.isfinite(rate):raise ValueError('bad funding')
    fund.append((t,rate));continue
   if t%M or int(r[6])!=t+M-1:raise ValueError('invalid minute timestamps')
   o,h,l,c=map(float,r[1:5]);i=ix(t)
   if not 0<=i<N:continue
   if not 0<l<=min(o,c)<=max(o,c)<=h:raise ValueError('invalid OHLC')
   a=mark if kind=='mark' else trade
   if np.isfinite(a[i,0]):
    if not is_rest:raise ValueError('duplicate archive minute')
    if not np.array_equal(a[i],np.array([o,h,l,c])):overlap.append({'t':t,'kind':kind,'primary':a[i].tolist(),'rest':[o,h,l,c]})
    continue
   a[i]=o,h,l,c
 fund.sort()
 slots=[t//(8*60*M) for t,_ in fund]
 if len(set(slots))!=len(slots) or any(b-a!=1 for a,b in zip(slots,slots[1:])):raise ValueError('funding slot gap/duplicate')
 if not fund or fund[0][0]>BEGIN or fund[-1][0]<END-8*60*M:raise ValueError('funding coverage incomplete')
 return trade,mark,fund,proof,overlap

def build_signals(trade):
 if len(trade)%BAR:raise ValueError('incomplete final 2h')
 blocks=trade[:,0].reshape(-1,BAR);valid=np.isfinite(blocks).all(axis=1)
 closes=trade[BAR-1::BAR,3].copy();closes[~valid]=np.nan
 returns=np.full(len(closes),np.nan)
 with np.errstate(divide='ignore',invalid='ignore'):returns[1:]=np.log(closes[1:]/closes[:-1])
 out=[];invalid=0
 for i in range(360,len(returns)):
  boundary=START+(i+1)*BAR*M
  if boundary<BEGIN or boundary>=END:continue
  history=returns[i-360:i];r=returns[i]
  if not np.isfinite(r) or not np.isfinite(history).all():invalid+=1;continue
  sigma=float(np.std(history,ddof=1))
  if not math.isfinite(sigma) or sigma<=0:invalid+=1;continue
  direction='LONG' if r<=-3*sigma else 'SHORT' if r>=3*sigma else None
  if direction:out.append({'signal_ms':boundary,'available_ms':boundary+1000,'direction':direction,'r':float(r),'sigma':sigma,'close':float(closes[i]),'previous_close':float(closes[i-1]),'sigma_first_return_close_ms':START+(i-360+1)*BAR*M,'sigma_last_return_close_ms':boundary-BAR*M})
 return out,invalid

def size_for(price,mark,symbol,impact):
 step=Decimal('.001');p=Decimal(str(max(price,mark)))
 qty=(Decimal('100')/p/step).to_integral_value(rounding=ROUND_DOWN)*step
 q=float(qty)
 if q<.001:return None,'min_qty_or_cap'
 if q*mark<(50 if symbol=='BTCUSDT' else 20)-1e-9:return None,'min_notional'
 if q*max(price,mark)>100+1e-9:return None,'notional_cap'
 return q,None

def equity_update(account,equity):
 account['peak']=max(account['peak'],equity)
 account['max_dd']=max(account['max_dd'],(account['peak']-equity)/account['peak'])

def execute(signal,trade,mark,funding_by_min,account,symbol,stress,hold_minutes=120):
 if type(hold_minutes) is not int or hold_minutes<=0:raise ValueError('invalid holding time')
 sign=1 if signal['direction']=='LONG' else -1;impact=.001 if stress else .0005;fee=.0005
 entry_t=signal['signal_ms']+(6 if stress else 1)*M;ei=ix(entry_t)
 if entry_t>=END:return {'rejection':'end_of_observation'}
 if ei<0 or ei>=N or not np.isfinite(trade[ei]).all() or not np.isfinite(mark[ei]).all():return {'rejection':'entry_data_missing'}
 reference=float(trade[ei,0]);markin=float(mark[ei,0]);fill=reference*(1+sign*impact)
 if not .97<=fill/markin<=1.03:return {'rejection':'price_mark_bound'}
 q,error=size_for(fill,markin,symbol,impact)
 if error:return {'rejection':error}
 entryfee=q*fill*fee
 if q*fill+entryfee>account['cash']:return {'rejection':'insufficient_cash'}
 risk=q*fill*(.02+2*(fee+impact)+.0021)
 if risk>5:return {'rejection':'estimated_risk_cap','estimated_risk':risk}
 stop=fill*(1-.02*sign);deadline=entry_t+hold_minutes*M;accrued=0.;ambiguous=0;funding_events=[];out=None
 account['cash']-=entryfee;equity_update(account,account['cash'])
 for i in range(ei,min(ix(deadline)+1,N)):
  t=START+i*M
  if not np.isfinite(trade[i]).all() or not np.isfinite(mark[i]).all():return {'unresolved':'position_data_missing','entry_ms':entry_t,'last_verified_ms':t-M,'qty':q,'entry':fill}
  o,h,l,c=trade[i];mo,mh,ml,mc=mark[i]
  if t>=deadline:
   out=(t,float(o),'time',0.);break
  stophit=bool(l<=stop) if sign==1 else bool(h>=stop)
  markadverse=float(ml if sign==1 else mh)
  liquid=liquidation(signal['direction'],q,fill,markadverse,1,funding_total=accrued)['liquidated']
  for ft,rate in funding_by_min.get(i,[]):
   if ft<entry_t:continue
   amount=float(funding_amount(signal['direction'],q,float(mo),rate))
   uncertain=(stophit or liquid) and (ft>t or (ft==t and (o<=stop if sign==1 else o>=stop)))
   if uncertain:
    ambiguous+=1;amount=min(amount,0.)
   funding_events.append({'event_ms':ft,'rate':rate,'mark_minute_open':float(mo),'applied_usdt':amount,'ordering_ambiguous':bool(uncertain)})
   accrued+=amount;account['cash']+=amount
  # A funding debit can itself worsen isolated margin within this minute.
  liquid=liquid or liquidation(signal['direction'],q,fill,markadverse,1,funding_total=accrued)['liquidated']
  # Favorable then adverse intrabar marks produce a conservative upper bound on drawdown.
  favorable=float(mh if sign==1 else ml)
  for mp in (favorable,markadverse):
   eq=account['cash']+q*(mp-fill)*sign-q*mp*(fee+impact)
   equity_update(account,eq)
  if liquid:
   out=(t+M,float(l if sign==1 else h),'liquidation',.01);break
  if stophit:
   out=(t+M,float(min(o,stop) if sign==1 else max(o,stop)),'stop',0.);break
 if out is None:
  if deadline>=END:
   mp=float(mark[-1,3]) if np.isfinite(mark[-1,3]) else None
   estimate=None if mp is None else q*(mp*(1-sign*impact)-fill)*sign-entryfee-q*mp*(1-sign*impact)*fee+accrued
   return {'unresolved':'open_at_end','entry_ms':entry_t,'qty':q,'entry':fill,'final_mark':mp,'unrealized_net_estimate':estimate,'funding':accrued,'funding_events':funding_events}
  return {'unresolved':'exit_unavailable','entry_ms':entry_t,'qty':q,'entry':fill}
 exit_t,refout,reason,liqrate=out;fillout=refout*(1-sign*impact);exitfee=q*fillout*fee;liqfee=q*fillout*liqrate
 gross=q*(refout-reference)*sign;friction=q*(abs(fill-reference)+abs(fillout-refout));net=gross-entryfee-exitfee-liqfee-friction+accrued
 account['cash']+=q*(fillout-fill)*sign-exitfee-liqfee;equity_update(account,account['cash'])
 if not math.isclose(gross-entryfee-exitfee-liqfee-friction+accrued,net,abs_tol=1e-9):raise AssertionError('trade accounting')
 return {'entry_ms':entry_t,'exit_ms':exit_t,'direction':signal['direction'],'qty':q,'entry_reference':reference,'exit_reference':refout,'entry':fill,'exit':fillout,'entry_notional':q*reference,'mark_entry':markin,'stop':stop,'estimated_risk':risk,'gross':gross,'entry_fee':entryfee,'exit_fee':exitfee,'fees':entryfee+exitfee,'liquidation_fee':liqfee,'impact':friction,'funding':accrued,'funding_events':funding_events,'funding_ambiguous':ambiguous,'net':net,'reason':reason,'hold_min':(exit_t-entry_t)//M,'exit_segment':segment(exit_t)}

def summarize(records,account):
 trades=[x for x in records if 'net' in x];n=len(trades);gross=sum(x['gross'] for x in trades);fees=sum(x['fees'] for x in trades);impact=sum(x['impact'] for x in trades);fund=sum(x['funding'] for x in trades);liq=sum(x['liquidation_fee'] for x in trades);net=sum(x['net'] for x in trades);den=sum(x['entry_notional'] for x in trades)
 if not math.isclose(gross-fees-impact-liq+fund,net,abs_tol=1e-7):raise ValueError('summary arithmetic')
 return {'closed':n,'gross':gross,'fees':fees,'impact':impact,'liquidation_fee':liq,'funding':fund,'net':net,'mean_net':net/n if n else None,'break_even_bps':10000*(gross+fund-liq)/den if den else None,'max_drawdown':account['max_dd'],'mean_hold_min':sum(x['hold_min'] for x in trades)/n if n else None,'exit_reasons':dict(Counter(x['reason'] for x in trades)),'rejections':dict(Counter(x['rejection'] for x in records if 'rejection' in x)),'ignored_open':sum(x.get('ignored')=='position_open' for x in records),'unresolved':dict(Counter(x['unresolved'] for x in records if 'unresolved' in x)),'ending_cash':account['cash'],'without_best_trade':net-max((x['net'] for x in trades),default=0),'funding_ambiguous':sum(x['funding_ambiguous'] for x in trades)}

def main(root,out):
 allrecords=[];allsummary={};metadata={}
 for symbol in ('BTCUSDT','ETHUSDT'):
  trade,mark,fund,proof,overlap=load_data(root,symbol);signals,invalid=build_signals(trade)
  fby={}
  for t,rate in fund:
   fby.setdefault(ix(t-t%M),[]).append((t,rate))
  intervals=[(b['signal_ms']-a['signal_ms'])/3600000 for a,b in zip(signals,signals[1:])]
  metadata[symbol]={'inputs':proof,'overlap_conflicts':overlap,'trade_missing_minutes':int(np.isnan(trade[:,0]).sum()),'mark_missing_minutes':int(np.isnan(mark[:,0]).sum()),'invalid_2h_windows':invalid,'signals':len(signals),'long_signals':sum(x['direction']=='LONG' for x in signals),'short_signals':sum(x['direction']=='SHORT' for x in signals),'mean_signal_gap_hours':sum(intervals)/len(intervals) if intervals else None,'max_signal_gap_hours':max(intervals) if intervals else None}
  for scenario in ('base','stress'):
   account={'cash':1000.,'peak':1000.,'max_dd':0.};busy=0;halted=False;records=[]
   for sig in signals:
    r=dict(sig,symbol=symbol,scenario=scenario)
    if halted:r['rejection']='prior_position_unresolved'
    elif sig['signal_ms']<busy:r['ignored']='position_open'
    else:
     action=execute(sig,trade,mark,fby,account,symbol,scenario=='stress');r.update(action)
     if 'exit_ms' in action:busy=action['exit_ms']
     if 'unresolved' in action:halted=True
    records.append(r)
   allrecords.extend(records);allsummary[f'{symbol}/{scenario}/all']=summarize(records,account)
   for side in ('LONG','SHORT'):
    subset=[x for x in records if x['direction']==side];allsummary[f'{symbol}/{scenario}/{side}']=summarize(subset,{'cash':None,'max_dd':None})
   for name,_,_ in SEGMENTS:
    subset=[x for x in records if x.get('exit_segment')==name];allsummary[f'{symbol}/{scenario}/{name}']=summarize(subset,{'cash':None,'max_dd':None})
 out.mkdir(parents=True,exist_ok=True)
 (out/'journal.json').write_text(json.dumps(allrecords,indent=2,sort_keys=True,allow_nan=False),encoding='utf8')
 (out/'summary.json').write_text(json.dumps({'summary':allsummary,'metadata':metadata},indent=2,sort_keys=True,allow_nan=False),encoding='utf8')
 print(json.dumps({k:v for k,v in allsummary.items() if k.endswith('/all')},indent=2))

if __name__=='__main__':main(pathlib.Path(sys.argv[1]),pathlib.Path(sys.argv[2]))

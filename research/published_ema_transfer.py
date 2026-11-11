"""Frozen published EMA walk-forward transfer; offline USD-M simulation."""
import datetime as dt,gzip,hashlib,json,math,pathlib,sys
from collections import Counter
import numpy as np
from research.hourly_ml import load_stream
from research import extreme_reversal as core

M=core.M;H=60*M;START=core.START;BEGIN=core.BEGIN;END=core.END
FAST=(5,7,10,15,20,30);SLOW=(40,50,100,150,200)
PAIRS=[(f,s) for s in SLOW for f in FAST]
SCHEDULES={'14d_10d':(14*24,10*24),'7d_28d':(7*24,28*24)}

def select_train(pnl):
 if pnl.ndim!=2 or pnl.shape[1]!=30 or pnl.shape[0]<2 or not np.isfinite(pnl).all():raise ValueError('invalid training matrix')
 std=pnl.std(axis=0,ddof=1);means=pnl.mean(axis=0)
 with np.errstate(divide='ignore',invalid='ignore'):score=np.where(std>0,np.sqrt(365*24)*means/std,np.nan)
 if not np.isfinite(score).any():return None,None
 k=int(np.nanargmax(score));return k,score

def ema_prices(closes):
 x=np.asarray(closes,dtype=float);out={}
 if not np.isfinite(x).all() or (x<=0).any():raise ValueError('hour gap / invalid price')
 for n in sorted(set(FAST+SLOW)):
  e=np.full(len(x),np.nan);e[n-1]=x[:n].mean();alpha=2/(n+1)
  for i in range(n,len(x)):e[i]=alpha*x[i]+(1-alpha)*e[i-1]
  out[n]=e
 return out

def author_matrices(closes):
 ema=ema_prices(closes);directions=np.column_stack([np.where(ema[f]>ema[s],1.,-1.) for f,s in PAIRS])
 directions[:199]=np.nan;positions=np.vstack([np.full((1,30),np.nan),directions[:-1]])
 ret=np.r_[0.,np.diff(np.log(closes))];prev=np.vstack([np.full((1,30),np.nan),positions[:-1]])
 pnl=np.nan_to_num(positions*ret[:,None],nan=0.)-np.nan_to_num(np.abs(positions-prev)*.001,nan=0.)
 return directions,pnl

def selected_path(closes,schedule,symbol,matrices=None):
 directions,pnl=author_matrices(closes) if matrices is None else matrices;train_h,test_h=SCHEDULES[schedule];first=(BEGIN-START)//H-1;last=(END-START)//H-1
 desired=np.zeros(last-first,dtype=np.int8);which=np.full(last-first,-1,dtype=np.int16);choices=[]
 for start in range(first,last,test_h):
  finish=min(start+test_h,last);hist=pnl[start-train_h+1:start+1]
  if start-train_h+1<200 or hist.shape!=(train_h,30):raise ValueError('insufficient warmup')
  selected,scores=select_train(hist)
  if selected is not None:
   desired[start-first:finish-first]=directions[start:finish,selected].astype(np.int8);which[start-first:finish-first]=selected
  choices.append({'symbol':symbol,'schedule':schedule,'train_start_ms':START+(start-train_h+1)*H,'first_training_close_ms':START+(start-train_h+2)*H,'train_end_ms':START+(start+1)*H,'last_training_close_ms':START+(start+1)*H,'test_start_ms':START+(start+1)*H,'test_end_ms':START+(finish+1)*H,'clipped':finish-start<test_h,'selected_pair':None if selected is None else list(PAIRS[selected]),'selected_index':selected,'sharpe':None if selected is None else float(scores[selected])})
 return desired,which,choices

def diagnostic(closes,desired):
 first=(BEGIN-START)//H-1;prev=0;gross=0.;cost=0.;returns=[];switches=0
 for k,p in enumerate(desired):
  i=first+k;change=abs(int(p)-prev);c=.001*change
  value=int(p)*math.log(closes[i+1]/closes[i])-c;gross+=int(p)*math.log(closes[i+1]/closes[i]);cost+=c;returns.append(value);switches+=change;prev=int(p)
 x=np.asarray(returns);sd=x.std(ddof=1)
 return {'hours':len(x),'gross_signed_log':gross,'actual_transition_cost':cost,'log_net_before_final_close':float(x.sum()),'final_close_cost':.001*abs(prev),'log_net_after_final_close':float(x.sum()-.001*abs(prev)),'physical_turnover_units':switches,'sharpe_before_final_close':float(x.mean()/sd*np.sqrt(365*24)) if sd else None}

class Account:
 def __init__(self,symbol,stress,protect=True):
  self.symbol=symbol;self.stress=stress;self.protect=protect;self.impact=.001 if stress else .0005;self.fee=.0005
  self.cash=1000.;self.peak=1000.;self.dd=0.;self.position=None;self.pending=None;self.last_stop=-1;self.halted=None
  self.trades=[];self.events=[];self.signals=[];self.open_count=0
 def equity(self,value):
  self.peak=max(self.peak,value);self.dd=max(self.dd,(self.peak-value)/self.peak)
 def current(self):return self.position['sign'] if self.position else 0
 def signal(self,T,target,pair):
  row={'time_ms':T,'desired':int(target),'selected_pair':pair,'position':self.current(),'action':'hold'}
  if self.halted:row['action']='unresolved'
  elif self.pending:raise ValueError('unexpected overlapping pending actions')
  elif T<=self.last_stop:row['action']='blocked_after_stop'
  elif target!=self.current():
   self.pending={'signal_ms':T,'due_ms':T+(6 if self.stress else 1)*M,'target':int(target),'pair':pair};row['action']='scheduled'
  self.signals.append(row)
 def enter(self,t,target,raw,mark,signal):
  if not target:return
  fill=raw*(1+target*self.impact);q,err=core.size_for(fill,mark,self.symbol,self.impact)
  if err:self.events.append({'time_ms':t,'action':'reject','reason':err,'target':target,'signal_ms':signal});return
  if not .97<=fill/mark<=1.03:self.events.append({'time_ms':t,'action':'reject','reason':'price_mark_bound','target':target,'signal_ms':signal});return
  entry_fee=q*fill*self.fee;risk=q*fill*(.02+2*(self.fee+self.impact)+.0021)
  if q*fill+entry_fee>self.cash:reason='insufficient_cash'
  elif risk>5:reason='estimated_risk_cap'
  else:reason=None
  if reason:self.events.append({'time_ms':t,'action':'reject','reason':reason,'target':target,'signal_ms':signal});return
  self.cash-=entry_fee;self.open_count+=1
  self.position={'id':self.open_count,'sign':target,'direction':'LONG' if target==1 else 'SHORT','entry_ms':t,'signal_ms':signal,'qty':q,'entry_reference':raw,'entry':fill,'entry_fee':entry_fee,'entry_notional':q*raw,'mark_entry':mark,'estimated_risk':risk,'stop':fill*(1-.02*target),'funding':0.,'funding_events':[]}
  self.events.append({'time_ms':t,'action':'entry','trade_id':self.open_count,'direction':self.position['direction'],'qty':q,'reference':raw,'price':fill,'fee':entry_fee,'signal_ms':signal})
  self.equity(self.cash)
 def close(self,t,raw,reason,liqrate=0.):
  p=self.position;sign=p['sign'];q=p['qty'];fill=raw*(1-sign*self.impact);exit_fee=q*fill*self.fee;liqfee=q*fill*liqrate
  gross=q*(raw-p['entry_reference'])*sign;impact=q*(abs(p['entry']-p['entry_reference'])+abs(fill-raw))
  net=gross-p['entry_fee']-exit_fee-liqfee-impact+p['funding']
  self.cash+=q*(fill-p['entry'])*sign-exit_fee-liqfee;self.equity(self.cash)
  record={**p,'exit_ms':t,'exit_reference':raw,'exit':fill,'exit_fee':exit_fee,'fees':p['entry_fee']+exit_fee,'liquidation_fee':liqfee,'gross':gross,'impact':impact,'net':net,'reason':reason,'hold_hours':(t-p['entry_ms'])/H,'exit_segment':core.segment(t)}
  if not math.isclose(record['gross']-record['fees']-impact-liqfee+record['funding'],net,abs_tol=1e-9):raise ValueError('ledger mismatch')
  self.trades.append(record);self.events.append({'time_ms':t,'action':'exit','trade_id':p['id'],'reason':reason,'reference':raw,'price':fill,'fee':exit_fee,'net':net})
  self.position=None
 def tick(self,t,bar,mark,events):
  if self.halted:return
  if (self.position or (self.pending and self.pending['due_ms']==t)) and (not np.isfinite(bar[:4]).all() or not np.isfinite(mark).all()):
   self.halted={'time_ms':t,'reason':'price_or_mark_missing','position':self.position,'pending':self.pending};return
  if self.pending and self.pending['due_ms']==t:
   x=self.pending;self.pending=None;raw=float(bar[0]);mp=float(mark[0])
   if self.position:self.close(t,raw,'signal_reversal' if x['target'] else 'invalid_signal')
   if x['target']:self.enter(t,x['target'],raw,mp,x['signal_ms'])
  p=self.position
  if not p:return
  o,h,l,_=bar[:4];mo,mh,ml,_=mark;sign=p['sign'];q=p['qty']
  stophit=self.protect and bool(l<=p['stop'] if sign==1 else h>=p['stop'])
  ma=float(ml if sign==1 else mh);mf=float(mh if sign==1 else ml)
  def isliq():
   margin=q*p['entry']-p['entry_fee']+p['funding'];equity=margin+sign*q*(ma-p['entry'])
   return equity<=.005*q*ma
  liq=isliq()
  for ft,rate in events:
   if ft<p['entry_ms']:continue
   value=float(core.funding_amount(p['direction'],q,float(mo),rate))
   ambiguous=bool((stophit or liq) and (ft>t or (ft==t and (o<=p['stop'] if sign==1 else o>=p['stop']))))
   if ambiguous:value=min(value,0.)
   p['funding']+=value;self.cash+=value;p['funding_events'].append({'event_ms':ft,'rate':rate,'mark_minute_open':float(mo),'applied_usdt':value,'ambiguous':ambiguous})
  liq=liq or isliq()
  for mp in (mf,ma):self.equity(self.cash+q*(mp-p['entry'])*sign-q*mp*(self.fee+self.impact))
  if liq or stophit:
   reason='liquidation' if liq else 'stop';raw=float(l if sign==1 else h) if liq else float(min(o,p['stop']) if sign==1 else max(o,p['stop']))
   self.close(t+M,raw,reason,.01 if liq else 0.)
   self.last_stop=t+M
   if self.pending:
    self.events.append({'time_ms':t+M,'action':'cancel_pending','reason':'protective_exit','signal_ms':self.pending['signal_ms']});self.pending=None
 def finish(self,mark):
  p=self.position;open_record=None
  if p:
   if mark is None or not math.isfinite(mark):self.halted={'reason':'final_mark_missing','position':p}
   else:
    sign=p['sign'];q=p['qty'];fill=mark*(1-sign*self.impact);exit_fee=q*fill*self.fee
    net=q*(fill-p['entry'])*sign-p['entry_fee']-exit_fee+p['funding']
    open_record={**p,'final_mark':mark,'estimated_exit':fill,'estimated_exit_fee':exit_fee,'unrealized_net_estimate':net,'hold_hours_at_end':(END-p['entry_ms'])/H}
  expected=1000+sum(x['net'] for x in self.trades)+(open_record['unrealized_net_estimate'] if open_record else 0.)
  equity=None if self.halted else expected
  holds=np.array([x['hold_hours'] for x in self.trades]);net=sum(x['net'] for x in self.trades)
  def group(rows):return {'closed':len(rows),'gross':sum(x['gross'] for x in rows),'fees':sum(x['fees'] for x in rows),'impact':sum(x['impact'] for x in rows),'funding':sum(x['funding'] for x in rows),'liquidation_fees':sum(x['liquidation_fee'] for x in rows),'net':sum(x['net'] for x in rows)}
  return {'closed':len(self.trades),'long':sum(x['sign']==1 for x in self.trades),'short':sum(x['sign']==-1 for x in self.trades),'gross':sum(x['gross'] for x in self.trades),'fees':sum(x['fees'] for x in self.trades),'impact':sum(x['impact'] for x in self.trades),'funding':sum(x['funding'] for x in self.trades),'liquidation_fees':sum(x['liquidation_fee'] for x in self.trades),'net_closed':net,'open':open_record,'net_including_open':None if equity is None else equity-1000,'cash':self.cash,'equity':equity,'max_drawdown':self.dd,'hold_median_h':float(np.median(holds)) if len(holds) else None,'hold_p90_h':float(np.quantile(holds,.9)) if len(holds) else None,'hold_max_h':float(np.max(holds)) if len(holds) else None,'fraction_at_most_24h':float(np.mean(holds<=24)) if len(holds) else None,'exit_reasons':dict(Counter(x['reason'] for x in self.trades)),'rejects':dict(Counter(x['reason'] for x in self.events if x['action']=='reject')),'by_direction':{side:group([x for x in self.trades if x['direction']==side]) for side in ('LONG','SHORT')},'by_segment':{name:group([x for x in self.trades if x['exit_segment']==name]) for name,_,_ in core.SEGMENTS},'best_trade':max((x['net'] for x in self.trades),default=0),'unresolved':self.halted,'pending':self.pending}

def simulate(trade,mark,fund,desired,which,symbol,stress,protect=True,benchmark=False):
 a=Account(symbol,stress,protect);fby={}
 for t,r in fund:fby.setdefault((t-START)//M,[]).append((t,r))
 first=(BEGIN-START)//M;last=(END-START)//M
 for i in range(first,last):
  t=START+i*M;k=(i-first)//60
  if i%60==0 and (not benchmark or i==first):
   target=1 if benchmark else int(desired[k]);pair=None if benchmark or which[k]<0 else list(PAIRS[which[k]])
   a.signal(t,target,pair)
  if a.pending or a.position:a.tick(t,trade[i],mark[i],fby.get(i,()))
  if a.halted:break
 finalmark=float(mark[last-1,3]) if np.isfinite(mark[last-1,3]) else None
 return a,a.finish(finalmark)

def main(root,out):
 out.mkdir(parents=True,exist_ok=True);metadata={};allchoices=[];outputs={};diagnostics={};signals={};events={};ledger={}
 sample=np.load('research/published_ema_review/author-sample-train.npz')['pnl'];chosen,scores=select_train(sample)
 if PAIRS[chosen]!=(20,50) or not math.isclose(scores[chosen],.7077856309759551,abs_tol=1e-10):raise AssertionError('author sample selection mismatch')
 for symbol in ('BTCUSDT','ETHUSDT'):
  trade,mark,fund,proof,conflicts=load_stream(root,symbol)
  n=len(trade)//60;blocks=trade[:n*60].reshape(n,60,5)
  if not np.isfinite(blocks).all():raise ValueError('hourly input gap')
  closes=blocks[:,-1,3];matrices=author_matrices(closes);metadata[symbol]={'input_hashes':proof,'overlap_conflicts':conflicts,'minute_count':len(trade)}
  for schedule in SCHEDULES:
   desired,which,choices=selected_path(closes,schedule,symbol,matrices);allchoices.extend(choices)
   diagnostics[f'{symbol}/{schedule}']=diagnostic(closes,desired)
   for scenario in ('base','stress'):
    account,z=simulate(trade,mark,fund,desired,which,symbol,scenario=='stress')
    key=f'{symbol}/{schedule}/{scenario}';outputs[key]=z;signals[key]=account.signals;events[key]=account.events;ledger[key]=account.trades
    print('completed',key,'closed',z['closed'],'net',z['net_including_open'],flush=True)
  for scenario in ('base','stress'):
   account,z=simulate(trade,mark,fund,None,None,symbol,scenario=='stress',protect=False,benchmark=True)
   key=f'{symbol}/buy_hold/{scenario}';outputs[key]=z;signals[key]=account.signals;events[key]=account.events;ledger[key]=account.trades
   print('completed',key,'net',z['net_including_open'],flush=True)
 result={'source_author_sample':'research/published_ema_review/author-sample.json','metadata':metadata,'diagnostic_A':diagnostics,'futures_B':outputs,'cash_benchmark_net':0.,'model':'fixed published 30-pair EMA with two original schedules'}
 (out/'summary.json').write_text(json.dumps(result,indent=2,sort_keys=True,allow_nan=False),encoding='utf8')
 for name,obj in (('choices',allchoices),('signals',signals),('events',events),('ledger',ledger)):
  with gzip.open(out/(name+'.json.gz'),'wt',encoding='utf8',newline='\n') as f:json.dump(obj,f,sort_keys=True,allow_nan=False)
 print('completed total choices',len(allchoices),flush=True)

if __name__=='__main__':main(pathlib.Path(sys.argv[1]),pathlib.Path(sys.argv[2]))

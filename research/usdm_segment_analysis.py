"""Descriptive analysis of public REST snapshots, not an execution guarantee."""
import argparse, json, math, statistics
from decimal import Decimal, ROUND_DOWN, ROUND_CEILING

def pct(values,p):
 if not values:return None
 x=sorted(values);i=(len(x)-1)*p;lo=int(i);hi=math.ceil(i);return x[lo]+(x[hi]-x[lo])*(i-lo)

def analyse(rows, *, min_notional=50., step=.001, cap=100., taker_bps=5.):
 spread=[];cost_min=[];cost_max=[];moves=[];horizons={300000:[],900000:[]};history=[];quote_stale=0;unpaired=0;gaps=0;replays=0;last_trade=None;previous=None;n=0
 for row in rows:
  if row.get('kind')!='snapshot':gaps+=1;previous=None;history.clear();continue
  n+=1;s=row['value']['snapshots'];book=s['book'];depth=s['depth'];trade=s['trades'];b=book['data'];d=depth['data']
  ids=sorted(x['a'] for x in trade['data'] if type(x.get('a')) is int)
  if ids and last_trade is not None:
   if ids[0]>last_trade+1:gaps+=1
   if ids[-1]<=last_trade:replays+=1
  if ids:last_trade=max(last_trade or ids[-1],ids[-1])
  if any(x.get('source_time_status')!='reported_unverified' or not 0<=x['received_ms']-x['source_event_ms']<=5000 for x in (book,depth)):
   quote_stale+=1;previous=None;continue
  bid=float(b['bidPrice']);ask=float(b['askPrice']);mid=(bid+ask)/2
  if float(d['bids'][0][0])!=bid or float(d['asks'][0][0])!=ask:unpaired+=1
  bid=float(d['bids'][0][0]);ask=float(d['asks'][0][0]);mid=(bid+ask)/2
  if not 0<bid<ask:quote_stale+=1;previous=None;continue
  spread.append((ask-bid)/mid*10000)
  qmin=math.ceil(max(min_notional/ask,step)/step-1e-12)*step;qmax=math.floor(cap/ask/step+1e-12)*step
  def avg(levels,q):
   remaining=q;total=0.
   for price,size in levels:
    amount=min(remaining,float(size));total+=float(price)*amount;remaining-=amount
    if remaining<1e-12:return total/q
   return None
  def cost(q):
   if q<=0 or q*ask<min_notional or q*ask>cap:return None
   a=avg(d['asks'],q);z=avg(d['bids'],q)
   return (a-z)/mid*10000+2*taker_bps if a is not None and z is not None else None
  c=cost(qmin);cm=cost(qmax)
  if c is not None:cost_min.append(c)
  if cm is not None:cost_max.append(cm)
  t=book['received_ms']
  if previous is not None and 4000<=t-previous[0]<=12000:moves.append((mid/previous[1]-1)*10000)
  for horizon in horizons:
   candidates=[x for x in history if abs((t-x[0])-horizon)<=6000]
   if candidates:
    earlier=min(candidates,key=lambda x:abs((t-x[0])-horizon))
    horizons[horizon].append((mid/earlier[1]-1)*10000)
  history.append((t,mid));history=[x for x in history if t-x[0]<=906000]
  previous=(t,mid)
 def stats(a):return {'n':len(a),'p50':pct(a,.5),'p90':pct(a,.9),'p99':pct(a,.99),'min':min(a) if a else None,'max':max(a) if a else None}
 reference=pct(cost_min,.5)
 horizon_stats={str(k//60000)+'m':{'movement_bps':stats(v),'above_median_cost_long':sum(x>reference for x in v) if reference is not None else None,'above_median_cost_short':sum(-x>reference for x in v) if reference is not None else None} for k,v in horizons.items()}
 return {'snapshots':n,'gaps_detected':gaps,'trade_id_replays':replays,'unusable_time':quote_stale,'asynchronous_top_mismatch':unpaired,'spread_bps':stats(spread),'model_round_trip_bps_min':stats(cost_min),'model_round_trip_bps_max':stats(cost_max),'mid_change_bps_next_sample':stats(moves),'absolute_mid_change_bps_next_sample':stats([abs(x) for x in moves]),'horizons':horizon_stats,'limitations':'REST snapshots only, no continuous book, queue position, maker fill or guaranteed execution; gaps reflect only detected missed aggregate trades.'}

def main():
 p=argparse.ArgumentParser();p.add_argument('input');a=p.parse_args()
 with open(a.input,encoding='utf8') as f:print(json.dumps(analyse(json.loads(x) for x in f),sort_keys=True,indent=2))
if __name__=='__main__':main()

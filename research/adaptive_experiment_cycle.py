"""One research cycle over independently owned temporary-root databases.

Inputs must come from the trusted publisher and the public feed, never from a
trade API. This is not a daemon or a production trading adapter.
"""
import json,time
from pathlib import Path
from trading_bot.runtime_store import RuntimeStore
from trading_bot.futures_store import FuturesStore
from trading_bot.unified_queue import UnifiedQueue
from .adaptive_route_state import RouteState
from .adaptive_market_proof import quote as valid_quote
from .adaptive_snapshot_proof import read_trusted,verify,verify_baseline
from .adaptive_route_policy import route
from .adaptive_decision_queue import record,enqueue
from .adaptive_experiment_recovery import reconcile
from .adaptive_protection_monitor import observe
from .adaptive_spot_worker import process_entry,process_short,process_stop,process_short_stop

def cycle(path,*,source,decision_path,publisher_uid,markets,clock=lambda:int(time.time()*1000)):
 path=Path(path);manifest=json.loads((path/'manifest.json').read_text())
 if source not in ('AI','baseline') or manifest.get('source')!=source:raise ValueError('experiment_identity_conflict')
 start=manifest['start_ms'];q=UnifiedQueue(path/'queue.db');state=RouteState(path/'route.db',clock=clock)
 spot=RuntimeStore(path/'financial.db',{'mode':'paper','symbol':'BTCUSDT'},shared=True)
 future=FuturesStore(path/'financial.db',shared=True,fee_bps='10',slippage_bps='5')
 try:
  recovered=reconcile(q,state,path/'financial.db',now_ms=clock());phase=state.status()['phase']
  if recovered['status'] in ('unresolved_entry','unresolved_exit','unresolved_state'):return {'status':'reconciliation_required','reason':recovered['status']}
  if phase in ('OPEN','EXIT_PENDING'):
   current=state.status();market=current['market'];ctx=markets.get(market,{})
   try:
    price=valid_quote(ctx.get('quote'),market=market,now_ms=clock())
    funding_problem=None
    if market=='futures':
     try:
      from .adaptive_funding_tracker import register,advance
      pos=future.get('position')
      exists=future.db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='adaptive_funding_schedule'").fetchone()
      saved=future.db.execute('SELECT 1 FROM adaptive_funding_schedule WHERE position_id=?',(pos['id'],)).fetchone() if exists else None
      if not saved:
       jobs=q.db.execute("SELECT payload FROM unified_jobs WHERE command='/research_adaptive_entry' AND market='futures'").fetchall()
       matches=[json.loads(j[0]) for j in jobs if json.loads(j[0]).get('action_id')==current['action_id']]
       if len(matches)!=1 or matches[0].get('funding_evidence',{}).get('source')!='public':raise ValueError('funding_entry_evidence_missing')
       evidence=matches[0]['funding_evidence'];register(future,position_id=pos['id'],next_due_ms=evidence['next_due_ms'],observed_ms=evidence['observed_ms'])
      raw={'symbol':'BTCUSDT','time':price['event_ms'],'markPrice':price['price'],'nextFundingTime':ctx['quote']['next_due_ms']}
      advance(future,position_id=pos['id'],mark_response=raw,received_ms=price['received_ms'],history=ctx.get('funding_history'))
     except (ValueError,KeyError,TypeError) as exc:funding_problem=str(exc)[:120]
    summary=observe(q,spot if market=='spot' else future,state,quote=price['price'],quote_ms=price['event_ms'],bars=ctx.get('bars'),skip_bar=ctx.get('skip_bar'),risk_bar=ctx.get('risk_bar'),clock=clock)
    if funding_problem:summary['funding_error']=funding_problem
    if summary.get('job_id') and not summary.get('reason'):
     worker=process_stop if market=='spot' else process_short_stop
     outcome=worker(q,spot if market=='spot' else future,state,quote=price['price'],quote_ms=price['event_ms'],clock=clock)
     return {'status':'protection_exit','result':outcome,'protection':summary}
    protected=summary
   except (ValueError,KeyError,TypeError) as exc:protected={'state':'unavailable','reason':str(exc)[:120]}
  else:protected={'state':'no_position'}
  try:
   document=read_trusted(decision_path,owner_uid=publisher_uid);now=clock()
   if source=='AI':
    info=verify(document,now_ms=now,start_ms=start);decision=document['decision'];observed=document['answer_ms']
   else:
    info=verify_baseline(document,now_ms=now,start_ms=start);observed=info['data_ms']
    decision={'mode':info['mode'],'snapshot_id':info['snapshot_id'],'data_ms':info['data_ms'],'expires_ms':info['expires_ms'],'reasons':['local deterministic baseline'],'uncertainties':['not a model decision']}
   record(q,decision,source=source,symbol='BTCUSDT',version='regular-regime-horizon-v2',now_ms=now)
  except (OSError,ValueError,KeyError,TypeError):return {'status':'decision_unavailable','protection':protected}
  if state.status()['phase']!='IDLE':return {'status':'position_open','protection':protected,'mode':decision['mode']}
  choice=route(decision,snapshot_id=info['snapshot_id'],data_ms=info['data_ms'],now_ms=clock(),symbol='BTCUSDT',decision_version='regular-regime-horizon-v2',eligible=True,research_sandbox=True)
  if not choice['allowed']:return {'status':'blocked','reason':choice['reason'],'protection':protected}
  market=choice['market'];ctx=markets.get(market,{})
  try:
   candles=ctx['candles'];bars=candles['bars'];skip_bar=candles['skip_bar']
   expected='binance_spot_klines' if market=='spot' else 'binance_usdm_klines'
   if candles.get('market')!=market or candles.get('symbol')!='BTCUSDT' or candles.get('source')!=expected:raise ValueError('candle_source_invalid')
   if bars[-1][0]+900000<=max(start,observed):raise ValueError('signal_predates_decision_or_start')
   price=valid_quote(ctx['quote'],market=market,now_ms=clock())
   filters=ctx['filters']
   if filters.get('market')!=market or filters.get('symbol')!='BTCUSDT':raise ValueError('filter_source_invalid')
   funding=None
   if market=='futures':
    due=ctx['quote'].get('next_due_ms')
    if type(due) is not int or due<=clock():raise ValueError('funding_schedule_unavailable')
    funding={'symbol':'BTCUSDT','complete':True,'observed_ms':price['received_ms'],'next_due_ms':due,'source':'public'}
   outcome=enqueue(q,decision,source=source,symbol='BTCUSDT',version='regular-regime-horizon-v2',now_ms=clock(),bars=bars,skip_bar=skip_bar,funding_evidence=funding)
   if outcome['status']=='BLOCKED':return {'status':'blocked','reason':outcome['reason'],'protection':protected}
   if outcome['status']!='PENDING':return {'status':'already_processed','result':outcome['status'],'protection':protected}
   signal={'source':source,'symbol':'BTCUSDT','snapshot_id':info['snapshot_id'],'strategy':choice['strategy'],'direction':choice['direction']}
   args={'decision':decision,'signal':signal,'data_ms':info['data_ms'],'decision_version':'regular-regime-horizon-v2','bars':bars,'skip_bar':skip_bar,'quote':price['price'],'quote_ms':price['event_ms'],'filters':filters['filters'],'filters_ms':filters['received_ms'],'clock':clock}
   if market=='futures':args['funding_evidence']=funding
   result=(process_entry(q,spot,state,**args) if market=='spot' else process_short(q,future,state,**args))
   if market=='futures' and result.get('status')=='OPEN':
    from .adaptive_funding_tracker import register
    pos=future.get('position');register(future,position_id=pos['id'],next_due_ms=funding['next_due_ms'],observed_ms=funding['observed_ms'])
   return {'status':'entry_evaluated','route':choice,'result':result,'protection':protected}
  except (ValueError,KeyError,TypeError,IndexError) as exc:return {'status':'entry_blocked','reason':str(exc)[:120],'protection':protected}
 finally:future.close();spot.close();state.close();q.close()

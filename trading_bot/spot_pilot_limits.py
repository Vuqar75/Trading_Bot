"""Deterministic durable limits for the explicitly authorized paper pilot.

Call inside the same financial transaction as order admission or fill
application. This module never places an order or changes a pause.
"""
from decimal import Decimal,InvalidOperation

MAX_ENTRIES=3
MAX_LOSS=Decimal('1')
BAR=900000
HOUR=3600000

def _amount(value):
 try:v=Decimal(str(value))
 except (InvalidOperation,TypeError,ValueError):raise ValueError('pilot_amount_invalid') from None
 if not v.is_finite():raise ValueError('pilot_amount_invalid')
 return v

def entry_allowed(state):
 if not isinstance(state,dict) or state.get('version')!='btc-trend1-48-24-v1' or state.get('phase')!='active':return False,'pilot_not_active'
 entries=state.get('entries');realized=state.get('realized_usdt')
 if not isinstance(entries,list) or len(entries)!=len(set(entries)) or not all(isinstance(x,str) and x for x in entries):raise ValueError('pilot_entries_invalid')
 if len(entries)>=MAX_ENTRIES:return False,'pilot_entry_limit'
 if state.get('realized_unconfirmed') is True:return False,'pilot_realized_unconfirmed'
 if _amount(realized)<=-MAX_LOSS:return False,'pilot_loss_limit'
 return True,'allowed'

def validate_intent(state,*,quote_at,now_ms):
 allowed,reason=entry_allowed(state)
 if not allowed:raise ValueError(reason)
 if state.get('open_position_id'):raise ValueError('pilot_position_exists')
 intent=state.get('intent')
 if not isinstance(intent,dict) or intent.get('action')!='BUY':raise ValueError('pilot_intent_missing')
 start=state.get('start_ms');signal=intent.get('signal_boundary_ms');boundary=intent.get('execution_boundary_ms')
 committed=intent.get('committed_ms');event=intent.get('quote_event_ms');received=intent.get('quote_received_ms')
 if any(type(x) is not int for x in (start,signal,boundary,committed,event,received,now_ms)):raise ValueError('pilot_time_invalid')
 if signal%HOUR or signal<=start or boundary!=signal+BAR or not signal<=committed<=boundary+5000 or not committed<event<=received<=now_ms or not boundary<=event or not 0<=now_ms-event<=5000 or not 0<=now_ms-received<=5000 or now_ms>boundary+5000:raise ValueError('pilot_intent_stale')
 if isinstance(quote_at,bool) or not isinstance(quote_at,(int,float)) or abs(int(quote_at*1000)-received)>2:raise ValueError('pilot_quote_invalid')
 return True

def validate_exit(state,position,*,reason,price,quote_at,now_ms):
 if not isinstance(state,dict) or state.get('phase')!='active' or not isinstance(position,dict) or state.get('open_position_id')!=position.get('id'):raise ValueError('pilot_position_mismatch')
 intent=state.get('exit_intent')
 if not isinstance(intent,dict) or intent.get('reason')!=reason or intent.get('position_id')!=position.get('id'):raise ValueError('pilot_exit_intent_missing')
 event=intent.get('quote_event_ms');received=intent.get('quote_received_ms')
 if any(type(x) is not int for x in (event,received,now_ms)) or not 0<=now_ms-event<=5000 or not 0<=now_ms-received<=5000 or not event<=received<=now_ms:raise ValueError('pilot_exit_quote_stale')
 if isinstance(quote_at,bool) or not isinstance(quote_at,(float,int)) or abs(int(quote_at*1000)-received)>2:raise ValueError('pilot_exit_quote_invalid')
 raw=_amount(intent.get('raw_price'));executed=_amount(price);entry=_amount(position.get('entry_price'))
 if raw<=0 or entry<=0 or executed!=raw*Decimal('.9995'):raise ValueError('pilot_exit_price_invalid')
 if reason=='PILOT1H_STOP' and raw>entry*Decimal('.98'):raise ValueError('pilot_stop_not_met')
 if reason=='PILOT1H_TIME' and (not isinstance(position.get('opened_at'),(float,int)) or now_ms-position['opened_at']*1000<168*HOUR):raise ValueError('pilot_time_exit_not_due')
 if reason=='PILOT1H_SIGNAL':
  signal=intent.get('signal_boundary_ms');boundary=intent.get('execution_boundary_ms')
  if type(signal) is not int or signal%HOUR or type(boundary) is not int or boundary!=signal+BAR or not boundary<=event<=now_ms<=boundary+5000:raise ValueError('pilot_exit_signal_stale')
 if reason not in ('PILOT1H_STOP','PILOT1H_TIME','PILOT1H_SIGNAL'):raise ValueError('pilot_exit_reason_invalid')
 return True

def record_entry(state,*,client_id,position_id):
 allowed,reason=entry_allowed(state)
 positions=state.setdefault('entry_positions',{})
 if not isinstance(positions,dict):raise ValueError('pilot_entries_invalid')
 if client_id in state.get('entries',[]):
  if positions.get(client_id)!=position_id:raise ValueError('pilot_entry_identity_conflict')
  return False
 if not allowed:raise ValueError(reason)
 if not isinstance(client_id,str) or not client_id or not isinstance(position_id,str) or not position_id or state.get('open_position_id'):raise ValueError('pilot_entry_identity_invalid')
 state['entries'].append(client_id);positions[client_id]=position_id;state['open_position_id']=position_id;return True

def record_close(state,*,position_id,fill_id,pnl,known,remaining):
 if not isinstance(state,dict):raise ValueError('pilot_position_mismatch')
 if not isinstance(fill_id,str) or not fill_id or type(known) is not bool:raise ValueError('pilot_close_invalid')
 value=_amount(pnl);left=_amount(remaining)
 if left<0:raise ValueError('pilot_remaining_invalid')
 seen=state.setdefault('close_fills',{})
 if not isinstance(seen,dict):raise ValueError('pilot_close_invalid')
 evidence={'position_id':position_id,'pnl':str(value) if known else None,'remaining':str(left),'known':known}
 if fill_id in seen:
  if seen[fill_id]!=evidence:raise ValueError('pilot_close_identity_conflict')
  return False
 if state.get('open_position_id')!=position_id:raise ValueError('pilot_position_mismatch')
 if known:state['realized_usdt']=str(_amount(state['realized_usdt'])+value)
 else:state['realized_unconfirmed']=True
 seen[fill_id]=evidence
 if left==0:state['open_position_id']=None
 return True

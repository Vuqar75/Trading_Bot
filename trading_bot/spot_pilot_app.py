"""Bounded BTCUSDT paper pilot using the existing Spot financial handlers.

The pilot begins in an inactive state. This module cannot place real orders.
"""
import time
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from pathlib import Path
from .app import Application
from .runtime_exchange import PaperExchange
from .spot_public_data import SpotPublicData
from .spot_check_cycle import step,read_request,_fresh,expire_unsent_check
from .testnet_observer import ObservationError,number
from .spot_pilot_rules import VERSION,BAR,HOUR,evaluate

class SpotPilotApplication(Application):
 def __init__(self,store,exchange,reader,queue,*,control_dir='/var/lib/tradingbot-spot-pilot-control',clock=time.time,owner_uid=0,**kwargs):
  self.clock=clock;self.queue=queue;self.control_dir=control_dir;self.owner_uid=owner_uid
  if type(exchange) is not PaperExchange or type(reader) is not SpotPublicData or store.get('identity').get('symbol','BTCUSDT')!='BTCUSDT' or not store.shared:raise ObservationError('Pilot requires shared BTCUSDT paper')
  initial_paused=bool(store.get('paused'))
  initial_anchor=queue.db.execute('SELECT COALESCE(MAX(id),-1) FROM unified_updates').fetchone()[0]
  super().__init__(store,exchange,reader,**kwargs)
  with store.transaction():
   state=store.get('spot_pilot')
   if state is None:
    if number(store.get('position')['quantity']) or store.pending():raise ObservationError('Pilot transition requires cash and no pending orders')
    store.set('spot_pilot',{'version':VERSION,'phase':'awaiting_check','entries':[],'realized_usdt':'0','open_position_id':None,'initial_paused':initial_paused,'initial_anchor_update_id':initial_anchor})
   elif not isinstance(state,dict) or state.get('version')!=VERSION:raise ObservationError('Pilot state invalid')

 def verify_check(self):
  with self.store.transaction():
   test=self.store.get('spot_test_cycle');state=self.store.get('spot_pilot');position=self.store.get('position')
   if not isinstance(test,dict) or test.get('phase')!='COMPLETE' or test.get('net_pnl') is None or number(position['quantity']) or self.store.pending() or self.store.get('blocked'):raise ObservationError('Completed check requires reconciliation')
   if Decimal(str(self.store.get('cash')))-Decimal(str(test['initial_cash']))!=Decimal(str(test['net_pnl'])):raise ObservationError('Check cash and PnL mismatch')
   if not self.store.shared.ledger.reconcile()['balanced']:raise ObservationError('Check ledger mismatch')
   if state.get('phase')=='awaiting_check':state['phase']='ready_to_start';state['check_verified']=True;self.store.set('spot_pilot',state)
   return test

 def activate(self,request_path,*,owner_uid=0):
  request=read_request(request_path,owner_uid=owner_uid,now=self.clock())
  if request is None:return False
  self.verify_check()
  with self.store.transaction():
   self.queue.db.execute('BEGIN IMMEDIATE')
   try:
    test=self.store.get('spot_test_cycle');state=self.store.get('spot_pilot');pos=self.store.get('position')
    if state.get('phase')!='ready_to_start' or not state.get('check_verified') or number(pos['quantity']) or self.store.pending() or self.store.get('blocked') or self.store.shared.deployment_hold():raise ObservationError('Pilot activation requires reconciliation')
    if self.queue.db.execute("SELECT 1 FROM unified_jobs WHERE status='PENDING' LIMIT 1").fetchone():raise ObservationError('Pending commands require review')
    anchor=test.get('anchor_update_id')
    if type(anchor) is not int:raise ObservationError('Check queue anchor missing')
    paused=bool(test.get('previous_paused')) or bool(self.queue.db.execute("SELECT 1 FROM unified_updates WHERE id>? AND (command='/pause_all' OR (command='/pause' AND market='spot')) LIMIT 1",(anchor,)).fetchone())
    start=int(self.clock()*1000)
    state.update(phase='active',start_ms=start,next_execution_ms=(start//HOUR+1)*HOUR+BAR,activation_id=request['id'],last_action='WAIT')
    self.store.set('spot_pilot',state);self.store.set('paused',paused)
   finally:self.queue.db.rollback()
  return True

 def manual_close(self):
  pos=self.store.get('position');qty=number(pos['quantity']);request=self.store.get('close_request')
  if not request or not qty or self.store.pending() or self.store.get('blocked'):return False
  if not self.store.manual_request_valid(request,pos):
   with self.store.transaction():self.store.set('close_request',None);self.store.notify('[Spot paper] Подтверждение закрытия устарело.')
   return False
  price,event,received=_fresh(self.reader,self.clock);info=self.reader.get('/api/v3/exchangeInfo',{'symbol':self.symbol})['symbols'][0]
  quantity=self._quantity(info,qty,price);ok=self.execute('SELL',quantity,price*Decimal('.9995'),'manual:'+str(request['update_id']),'MANUAL',pos['id'],quote_at=received/1000)
  if ok:
   with self.store.transaction():self.store.set('close_request',None)
  return ok

 def next_wake_ms(self):
  state=self.store.get('spot_pilot') or {}
  return state.get('next_execution_ms') if state.get('phase')=='active' else None

 def _quote_after(self,committed):
  for _ in range(3):
   price,event,received=_fresh(self.reader,self.clock)
   if event>committed:return price,event,received
   time.sleep(.3)
  raise ObservationError('No fresh public trade after decision')

 def _exit(self,pos,reason,*,raw,event,received,signal=None,info=None):
  price=raw*Decimal('.9995');info=info or self.reader.get('/api/v3/exchangeInfo',{'symbol':self.symbol})['symbols'][0]
  qty=number(self._quantity(info,number(pos['quantity']),price))
  if qty!=number(pos['quantity']):raise ObservationError('Pilot exit would leave untradeable residual')
  with self.store.transaction():
   state=self.store.get('spot_pilot');current=self.store.get('position')
   if current['id']!=pos['id'] or self.store.pending():raise ObservationError('Pilot position changed')
   intent={'reason':reason,'position_id':pos['id'],'raw_price':str(raw),'quote_event_ms':event,'quote_received_ms':received}
   if signal:intent.update(signal_boundary_ms=signal['signal_boundary_ms'],execution_boundary_ms=signal['execution_boundary_ms'])
   state['exit_intent']=intent;self.store.set('spot_pilot',state)
  suffix=str(signal['signal_boundary_ms']) if signal else str(int(self.clock()//60))
  return self.execute('SELL',qty,price,'pilot:'+reason+':'+pos['id']+':'+suffix,reason,pos['id'],quote_at=received/1000)

 def _record_result(self,boundary,decision,result):
  with self.store.transaction():
   state=self.store.get('spot_pilot')
   if state.get('next_execution_ms')!=boundary:return
   state.update(next_execution_ms=boundary+HOUR,last_candle_ms=decision.get('signal_boundary_ms'),last_action=decision.get('action'),last_close=decision.get('close'),entry_high=decision.get('entry_high'),exit_low=decision.get('exit_low'),last_result=result)
   self.store.set('spot_pilot',state)

 def tick(self):
  expire_unsent_check(self.store)
  self.recover()
  if self.manual_close():return
  state=self.store.get('spot_pilot');boundary=state.get('next_execution_ms');now=int(self.clock()*1000)
  due=state.get('phase')=='active' and type(boundary) is int and boundary<=now<=boundary+5000
  inputs=None;protection=None;position=self.store.get('position')
  if due:
   # All pre-decision inputs are public and independent. The execution quote
   # is still obtained after the decision is durably committed.
   with ThreadPoolExecutor(max_workers=5) as pool:
    ticker=pool.submit(self.reader.get,'/api/v3/ticker/price',{'symbol':self.symbol})
    server=pool.submit(self.reader.get,'/api/v3/time')
    candles=pool.submit(self.reader.get,'/api/v3/klines',{'symbol':'BTCUSDT','interval':'15m','limit':200})
    filters=pool.submit(self.reader.get,'/api/v3/exchangeInfo',{'symbol':self.symbol})
    protect=pool.submit(_fresh,self.reader,self.clock) if number(position['quantity']) else None
    if protect:
     protection=protect.result();raw,event,received=protection
     self.last_protection_at=received/1000;self.last_protection_position=position['id']
     reason='PILOT1H_STOP' if raw<=number(position['entry_price'])*Decimal('.98') else 'PILOT1H_TIME' if int(self.clock()*1000)-int(position['opened_at']*1000)>=168*HOUR else None
     if reason:
      self._exit(position,reason,raw=raw,event=event,received=received,info=filters.result()['symbols'][0]);return
    quote=ticker.result();inputs=(server.result(),candles.result(),filters.result())
  elif state.get('phase')=='active' and number(position['quantity']):
   protection=_fresh(self.reader,self.clock);quote={'symbol':self.symbol,'price':str(protection[0])}
  else:quote=self.reader.get('/api/v3/ticker/price',{'symbol':self.symbol})
  at=self.clock()
  if quote.get('symbol')!=self.symbol or not number(quote.get('price')):raise ObservationError('Spot mark unavailable')
  with self.store.transaction():self.store.set('quote',{'price':str(number(quote['price'])),'time':at});self.store.set('error',None)
  control=Path(self.control_dir)
  if step(self,self.queue,control/'check.json',clock=self.clock,owner_uid=self.owner_uid):
   # Complete the authorized check promptly within the same worker turn.
   if (self.store.get('spot_test_cycle') or {}).get('phase')=='BUY_FILLED':step(self,self.queue,control/'check.json',clock=self.clock,owner_uid=self.owner_uid)
   return
  state=self.store.get('spot_pilot')
  if state.get('phase')=='awaiting_check' and (self.store.get('spot_test_cycle') or {}).get('phase')=='COMPLETE':self.verify_check();state=self.store.get('spot_pilot')
  if state.get('phase')=='ready_to_start':self.activate(control/'activate.json',owner_uid=self.owner_uid);state=self.store.get('spot_pilot')
  if state.get('phase')!='active':return
  pos=self.store.get('position');quantity=number(pos['quantity'])
  if quantity:
   if state.get('open_position_id')!=pos['id']:raise ObservationError('Pilot position ownership unknown')
   raw,event,received=protection or _fresh(self.reader,self.clock);self.last_protection_at=received/1000;self.last_protection_position=pos['id']
   with self.store.transaction():self.store.set('quote',{'price':str(raw),'time':received/1000});self.store.set('error',None)
   reason='PILOT1H_STOP' if raw<=number(pos['entry_price'])*Decimal('.98') else 'PILOT1H_TIME' if int(self.clock()*1000)-int(pos['opened_at']*1000)>=168*HOUR else None
   if reason:self._exit(pos,reason,raw=raw,event=event,received=received,info=inputs[2]['symbols'][0] if inputs else None);return
  boundary=state.get('next_execution_ms');now=int(self.clock()*1000)
  if type(boundary) is not int:raise ObservationError('Pilot boundary missing')
  if now<boundary:return
  if now>boundary+5000:
   self._record_result(boundary,{'action':'MISSED'},'missed_window');return
  if inputs is None:raise ObservationError('Pilot boundary inputs missing')
  server=int(inputs[0]['serverTime']);rows=inputs[1];observed=int(self.clock()*1000)
  if abs(server-observed)>5000:raise ObservationError('Pilot data clock mismatch')
  try:decision=evaluate(rows,server_ms=server,start_ms=state['start_ms'],position_open=bool(quantity))
  except ValueError as exc:raise ObservationError(str(exc)) from None
  if decision['execution_boundary_ms']!=boundary:raise ObservationError('Pilot candle boundary mismatch')
  action=decision['action']
  if action=='HOLD':self._record_result(boundary,decision,'no_signal');return
  if action=='BUY' and self.store.get('paused'):self._record_result(boundary,decision,'entry_skipped_paused');return
  committed=int(self.clock()*1000)
  with self.store.transaction():
   state=self.store.get('spot_pilot')
   if state.get('next_execution_ms')!=boundary or committed>boundary+5000:raise ObservationError('Pilot signal expired after database wait')
   if action=='BUY':state['intent']={**decision,'committed_ms':committed}
   self.store.set('spot_pilot',state)
  raw,event,received=self._quote_after(committed)
  if action=='SELL':
   ok=self._exit(pos,'PILOT1H_SIGNAL',raw=raw,event=event,received=received,signal=decision,info=inputs[2]['symbols'][0]);self._record_result(boundary,decision,'filled' if ok else 'not_filled');return
  price=raw*Decimal('1.0005');info=inputs[2]['symbols'][0]
  try:qty=number(self._quantity(info,self.amount/price,price))
  except ObservationError:self._record_result(boundary,decision,'entry_skipped_exchange_filter');return
  with self.store.transaction():
   state=self.store.get('spot_pilot');state['intent'].update(quote_event_ms=event,quote_received_ms=received);self.store.set('spot_pilot',state)
  try:ok=self.execute('BUY',qty,price,'pilot:entry:'+str(decision['signal_boundary_ms']),'PILOT1H_ENTRY',quote_at=received/1000)
  except ObservationError as exc:
   if self.store.pending():raise
   self._record_result(boundary,decision,'entry_blocked:'+str(exc)[:80]);return
  self._record_result(boundary,decision,'filled' if ok else 'not_filled')

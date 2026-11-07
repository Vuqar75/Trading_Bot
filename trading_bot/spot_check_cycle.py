"""Single-writer owner-authorized paper BUY/SELL check using runtime handlers.

The caller is the existing Spot worker. This module never starts a poller,
accesses credentials or submits a real/Testnet order.
"""
import json,os,re,stat,time
from decimal import Decimal
from pathlib import Path
from .runtime_exchange import PaperExchange,order_quantity
from .testnet_observer import ObservationError,number

def read_request(path,*,owner_uid=0,now=None):
 path=Path(path);now=time.time() if now is None else now
 try:
  parent=path.parent.lstat()
  if not stat.S_ISDIR(parent.st_mode) or parent.st_uid!=owner_uid or parent.st_mode&0o022:raise ValueError('check_control_directory_untrusted')
  fd=os.open(path,os.O_RDONLY|os.O_NOFOLLOW)
 except FileNotFoundError:return None
 try:
  info=os.fstat(fd)
  if not stat.S_ISREG(info.st_mode) or info.st_uid!=owner_uid or info.st_mode&0o027 or info.st_size>1024:raise ValueError('check_request_untrusted')
  with os.fdopen(fd,'rb',closefd=False) as f:request=json.loads(f.read(1025))
 finally:os.close(fd)
 if not isinstance(request,dict) or set(request) not in ({'schema','id','expires'},{'schema','id','expires','previous_paused','anchor_update_id'}) or request['schema']!=1 or not isinstance(request['id'],str) or not re.fullmatch('[a-zA-Z0-9_-]{1,48}',request['id']) or type(request['expires']) not in (int,float) or not now<request['expires']<=now+600:raise ValueError('check_request_invalid_or_expired')
 if 'previous_paused' in request and (type(request['previous_paused']) is not bool or type(request['anchor_update_id']) is not int or request['anchor_update_id'] < -1):raise ValueError('check_origin_invalid')
 return request

def _fresh(reader,clock):
 trades=reader.get('/api/v3/trades',{'symbol':'BTCUSDT','limit':1});after=int(reader.get('/api/v3/time')['serverTime']);received=int(clock()*1000)
 if not isinstance(trades,list) or len(trades)!=1 or type(trades[0].get('time')) is not int or type(trades[0].get('id')) is not int:raise ObservationError('Check quote unavailable')
 event=trades[0]['time']
 if not 0<=received-event<=5000 or abs(after-received)>5000 or event>after:raise ObservationError('Check quote stale or clock mismatch')
 return number(trades[0]['price']),event,received

def _no_conflicting_commands(queue,now):
 if queue.db.execute("SELECT 1 FROM unified_jobs WHERE status='PENDING' LIMIT 1").fetchone():raise ObservationError('Pending commands require review')
 if queue.db.execute("SELECT 1 FROM unified_proposals WHERE status='OPEN' AND expires>? LIMIT 1",(now,)).fetchone():raise ObservationError('Pending confirmation requires review')
 if any(p.get('status')=='OPEN' and p.get('expires',0)>now for p in queue.pair_proposals()):raise ObservationError('Pending pair proposal requires review')

def expire_unsent_check(store):
 """A PREPARED pilot or check order has no submit claim and can be expired.

 Never alter a claimed/unknown order: normal recovery must reconcile it.
 """
 with store.transaction():
  store.db.execute("UPDATE runtime_orders SET phase='SETTLED',status='EXPIRED' WHERE phase='PREPARED' AND reason IN ('SPOT_CHECK_BUY','SPOT_CHECK_SELL','PILOT1H_ENTRY','PILOT1H_STOP','PILOT1H_TIME','PILOT1H_SIGNAL')")

def step(app,queue,request_path,*,clock=time.time,owner_uid=0):
 if type(app.exchange) is not PaperExchange or app.store.get('identity').get('mode')!='paper' or app.symbol!='BTCUSDT':raise ObservationError('Check requires exact paper adapter and BTCUSDT')
 store=app.store;state=store.get('spot_test_cycle')
 if state is None:
  request=read_request(request_path,owner_uid=owner_uid,now=clock())
  if request is None:return False
  with store.transaction():
   queue.db.execute('BEGIN IMMEDIATE')
   try:
    if store.get('spot_test_cycle') is not None:raise ObservationError('Check already initialized')
    pos=store.get('position')
    if number(pos['quantity']) or store.pending() or store.get('blocked') or store.get('close_request'):raise ObservationError('Spot position or action requires review')
    if store.shared and (store.shared.deployment_hold() or not store.shared.ledger.reconcile()['balanced']):raise ObservationError('Paper ledger or hold requires review')
    if store.shared:
     assets=[r[0] for r in store.db.execute("SELECT DISTINCT asset FROM ledger_postings WHERE account='spot:reserved'")]
     if any(store.shared.ledger.balance('spot:reserved',asset)!=0 for asset in assets):raise ObservationError('Spot reserves require review')
    _no_conflicting_commands(queue,clock())
    pilot=store.get('spot_pilot') or {}
    current_anchor=queue.db.execute('SELECT COALESCE(MAX(id),-1) FROM unified_updates').fetchone()[0]
    anchor=request.get('anchor_update_id',pilot.get('initial_anchor_update_id',current_anchor))
    offset=queue.db.execute("SELECT value FROM unified_state WHERE key='offset'").fetchone()
    if not offset or anchor>=int(offset[0]):raise ObservationError('Check origin anchor unavailable')
    previous=request.get('previous_paused',pilot.get('initial_paused',bool(store.get('paused'))))
    state={'schema':1,'id':request['id'],'authorization':'owner_local_once','phase':'WAITING_BUY','request_expires':request['expires'],'initial_cash':str(store.get('cash')),'previous_paused':previous,'anchor_update_id':anchor}
    store.set('spot_test_cycle',state);store.set('paused',True)
   finally:queue.db.rollback()
 if not isinstance(state,dict) or state.get('authorization')!='owner_local_once':raise ObservationError('Check state invalid')
 if state['phase'] in ('COMPLETE','ABORTED','REVIEW'):return False
 expire_unsent_check(store);app.recover();state=store.get('spot_test_cycle');ident=state['id']
 if state['phase'] in ('WAITING_BUY','BUY_ARMED'):
  attempt=state.get('buy_attempt',0);key='paper-check:'+ident+':buy'+(':'+str(attempt) if attempt else '');old=store.db.execute('SELECT phase,status FROM runtime_orders WHERE business_key=?',(key,)).fetchone()
  if old:
   if old['phase']=='SETTLED' and old['status']!='FILLED':
    with store.transaction():
     state=store.get('spot_test_cycle')
     if old['status']=='EXPIRED' and attempt<3 and clock()<state['request_expires']:state.update(phase='WAITING_BUY',buy_attempt=attempt+1)
     else:state['phase']='ABORTED'
     store.set('spot_test_cycle',state)
   return True
  if clock()>=state['request_expires']:
   with store.transaction():state=store.get('spot_test_cycle');state['phase']='ABORTED';store.set('spot_test_cycle',state)
   return True
  price,event,received=_fresh(app.reader,clock);price*=Decimal('1.0005')
  info=app.reader.get('/api/v3/exchangeInfo',{'symbol':'BTCUSDT'})['symbols'][0]
  qty=number(order_quantity(info,Decimal('10')/price,price))
  # Check both filters before entry; an adverse future gap can still block sell.
  order_quantity(info,qty,price*Decimal('.999'))
  with store.transaction():
   state=store.get('spot_test_cycle')
   if state['phase'] not in ('WAITING_BUY','BUY_ARMED') or number(store.get('position')['quantity']) or store.pending():raise ObservationError('Check state changed')
   state.update(phase='BUY_ARMED',quote_event_ms=event,quote_received_ms=received);store.set('spot_test_cycle',state)
  app.execute('BUY',qty,price,key,'SPOT_CHECK_BUY',quote_at=received/1000)
  return True
 if state['phase'] in ('BUY_FILLED','SELL_ARMED'):
  pos=store.get('position')
  if pos['id']!=state.get('position_id') or number(pos['quantity'])!=number(state.get('quantity')):raise ObservationError('Check position changed')
  attempt=state.get('sell_attempt',0);key='paper-check:'+ident+':sell'+(':'+str(attempt) if attempt else '');old=store.db.execute('SELECT phase,status FROM runtime_orders WHERE business_key=?',(key,)).fetchone()
  if old:
   if old['phase']=='SETTLED' and old['status']!='FILLED':
    with store.transaction():
     state=store.get('spot_test_cycle')
     if old['status']=='EXPIRED' and attempt<3:state.update(phase='BUY_FILLED',sell_attempt=attempt+1)
     else:state['phase']='REVIEW'
     store.set('spot_test_cycle',state)
   return True
  price,event,received=_fresh(app.reader,clock);price*=Decimal('.9995')
  info=app.reader.get('/api/v3/exchangeInfo',{'symbol':'BTCUSDT'})['symbols'][0];qty=number(order_quantity(info,number(pos['quantity']),price))
  if qty!=number(pos['quantity']):raise ObservationError('Check sell would leave residual')
  with store.transaction():
   state=store.get('spot_test_cycle')
   if state['phase'] not in ('BUY_FILLED','SELL_ARMED') or store.get('position')['id']!=state['position_id'] or store.pending():raise ObservationError('Check position changed')
   state.update(phase='SELL_ARMED',quote_event_ms=event,quote_received_ms=received);store.set('spot_test_cycle',state)
  app.execute('SELL',qty,price,key,'SPOT_CHECK_SELL',state['position_id'],quote_at=received/1000)
  return True
 raise ObservationError('Check state requires review')

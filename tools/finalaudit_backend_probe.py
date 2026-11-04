import time,sys
from pathlib import Path
from tests import test_paper_wallet_commands as fx
from trading_bot.runtime_store import RuntimeStore
from trading_bot.futures_store import FuturesStore
from trading_bot.unified_receiver import MarketReceiver
from trading_bot.unified_worker import process_market
from trading_bot.cross_paper_book import CrossPaperBook
from trading_bot.cross_command_receiver import CrossCommandReceiver
from trading_bot.cross_worker import process_cross
from trading_bot.margin_paper_book import MarginPaperBook
from trading_bot.margin_command_receiver import MarginCommandReceiver
from trading_bot.margin_paper_strategy import MarginPaperStrategy
from trading_bot.margin_worker import process_margin
case=fx.WalletCommandTests('runTest');case.setUp()
try:
 q=case.q;s=case.shared;p=Path(case.tmp.name)/'shared.db'
 spot=RuntimeStore(p,{'mode':'paper','interval':'15m'},shared=True);future=FuturesStore(p,shared=True)
 sr=MarketReceiver(p,'spot');fr=MarketReceiver(p,'futures');cross=CrossPaperBook(s);margin=MarginPaperBook(s.ledger);cr=CrossCommandReceiver(cross);mr=MarginCommandReceiver(margin);MarginPaperStrategy(margin)
 try:
  ident=10
  def run(m):
   if m=='spot':process_market(q,sr,spot,'spot')
   elif m=='futures':process_market(q,fr,future,'futures')
   elif m=='futures_cross':process_cross(q,cr,cross)
   else:process_margin(q,mr,margin,s)
  for market in ('spot','futures','futures_cross','margin_cross'):
   ident+=1;q.receive(ident,True,'/mode '+market)
   for command in ('/status '+market,'/signal','/history','/report','/pause','/timeframe'):
    before={table:s.db.execute('select coalesce(max(id),0) from '+table).fetchone()[0] for table in ('runtime_outbox','futures_outbox')}
    ident+=1;q.receive(ident,True,command);run(market)
    row=q.db.execute('select status,result from unified_jobs where update_id=?',(ident,)).fetchone()
    assert row and row['status']!='PENDING' and row['result'],(market,command,dict(row) if row else None)
    answer=row['result']
    if answer=='Ответ сохранён в журнале рынка.':
     table='runtime_outbox' if market=='spot' else 'futures_outbox'
     msg=s.db.execute('select message from '+table+' where id>? order by id desc limit 1',(before[table],)).fetchone()
     assert msg and msg[0].strip(),(market,command,'missing durable market reply')
     answer=msg[0]
    if command=='/report':assert 'сегодня' in answer.lower() and 'за всё время' in answer.lower(),(market,command,answer)
    print(market,command,'completed with durable reply')
  ident+=1;q.receive(ident,False,'/pause_all');assert not q.db.execute('select 1 from unified_jobs where update_id=?',(ident,)).fetchone()
  with s.transaction():
   s._set('spot','paused',False);s._set('futures','paused',False)
   s.db.execute("UPDATE wallet_state SET paused=0 WHERE wallet IN ('futures_cross','margin_cross')")
  ident+=1;q.receive(ident,True,'/pause_all');uid=ident
  for m in ('spot','futures','futures_cross'):run(m)
  jobs=q.db.execute('select market,status from unified_jobs where update_id=?',(uid,)).fetchall();assert any(r['market']=='margin_cross' and r['status']=='PENDING' for r in jobs)
  assert spot.get('paused') and future.get('paused')
  assert s.db.execute("SELECT paused FROM wallet_state WHERE wallet='futures_cross'").fetchone()[0]==1
  assert s.db.execute("SELECT paused FROM wallet_state WHERE wallet='margin_cross'").fetchone()[0]==0
  assert not any('применены ко всем четырём' in x[0] for x in q.db.execute('select message from unified_outbox'))
  run('margin_cross');assert s.db.execute("SELECT paused FROM wallet_state WHERE wallet='margin_cross'").fetchone()[0]==1
  assert not q.db.execute('select 1 from unified_jobs where update_id=? and status=?',(uid,'PENDING')).fetchone()
  assert any('применены ко всем четырём' in x[0] for x in q.db.execute('select message from unified_outbox'))
  print('authorization and partial/final pause_all backend effects verified on synthetic fixture')

  # Seed paper-only test collateral and quotes. These are isolated temporary
  # databases, not real transfers or changes to the VM's financial journal.
  from decimal import Decimal
  now=time.time();stamp=int(now*1000);period=28800000
  with s.transaction():
   for market in ('futures_cross','margin_cross'):
    s.ledger._post('probe-seed:'+market,'FIXTURE',{},[(market+':free','USDT',Decimal(50)),('external:fixture','USDT',Decimal(-50))])
  cq={symbol:{'mark':price,'time':now,'funding_complete':True,'server_ms':stamp,'next_due_ms':(stamp//period+1)*period,'interval_ms':period} for symbol,price in (('BTCUSDT','100'),('ETHUSDT','200'))}
  cross.record_quotes(cq,now=now)
  filters={'lot_rules':[{'min':'0','max':'0','step':'.00000001'}],'min_notional':'0','max_notional':'0','observed_ms':stamp}
  mq={symbol:{'price':price,'time_ms':stamp,'server_ms':stamp,'source':'fixture','price_kind':'last_public_trade','filters':filters} for symbol,price in (('BTCUSDT','100'),('ETHUSDT','200'))}
  margin.record_quotes(mq,now_ms=stamp);mr.protection_checked_ms=stamp
  for market in ('spot','futures','futures_cross','margin_cross'):
   ident+=1;q.receive(ident,True,'/mode '+market)
   ident+=1;uid=ident;q.receive(uid,True,'/resume')
   def crash(point):
    if point=='after_destination_commit':raise RuntimeError('synthetic crash before queue acknowledgement')
   try:
    if market=='spot':process_market(q,sr,spot,'spot',fault=crash)
    elif market=='futures':process_market(q,fr,future,'futures',fault=crash)
    elif market=='futures_cross':process_cross(q,cr,cross,fault=crash)
    else:process_margin(q,mr,margin,s,fault=crash)
    raise AssertionError('fault was not reached')
   except RuntimeError as exc:
    assert 'synthetic crash' in str(exc)
   run(market)
   row=q.db.execute('SELECT status FROM unified_jobs WHERE update_id=?',(uid,)).fetchone();assert row['status']=='DONE'
   paused=(spot.get('paused') if market=='spot' else future.get('paused') if market=='futures' else
           bool(s.db.execute('SELECT paused FROM wallet_state WHERE wallet=?',(market,)).fetchone()[0]))
   assert not paused,(market,'resume not applied')
   q.receive(uid,True,'/resume');run(market)
   assert q.db.execute('SELECT count(*) FROM unified_jobs WHERE update_id=?',(uid,)).fetchone()[0]==1
   print(market,'resume applied once across crash and duplicate')

  # Spot and isolated manually-confirmed close is a request for the trading
  # process, not an immediate synthetic fill. Verify the bound request and
  # stable replay after switching mode without executing a protective exit.
  for market,store,table in (('spot',spot,'runtime_fills'),('futures',future,'futures_events')):
   ident+=1;q.receive(ident,True,'/mode '+market)
   pos=store.get('position');assert pos and Decimal(str(pos['quantity']))>0
   ident+=1;q.receive(ident,True,'/close',positions={market:pos});run(market)
   proposal=q.db.execute('SELECT id FROM unified_proposals WHERE market=? ORDER BY rowid DESC LIMIT 1',(market,)).fetchone()
   assert proposal,(market,'proposal missing')
   ident+=1;q.receive(ident,True,'/mode '+('futures' if market=='spot' else 'spot'))
   ident+=1;uid=ident;q.receive(uid,True,'/confirm '+proposal['id'])
   before=s.db.execute('SELECT count(*) FROM '+table).fetchone()[0]
   run(market);req=store.get('close_request')
   assert req and req['target']==pos['id'] and Decimal(req['quantity'])==Decimal(str(pos['quantity']))
   q.receive(uid,True,'/confirm '+proposal['id']);run(market)
   assert s.db.execute('SELECT count(*) FROM '+table).fetchone()[0]==before
   assert q.db.execute('SELECT count(*) FROM unified_jobs WHERE update_id=?',(uid,)).fetchone()[0]==1
   print(market,'close confirmation bound to original position across mode switch and duplicate; no fill')
 finally:sr.close();fr.close();spot.close();future.close()
finally:case.doCleanups()

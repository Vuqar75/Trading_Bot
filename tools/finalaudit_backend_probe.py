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
 finally:sr.close();fr.close();spot.close();future.close()
finally:case.doCleanups()

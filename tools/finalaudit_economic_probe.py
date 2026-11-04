from fractions import Fraction as F
from tests import test_margin_paper_book as mf
from tests import test_cross_paper_book as cf
from tests import test_runtime as sf
from tests import test_isolated_futures as ff
m=mf.MarginPaperBookTests('runTest');m.setUp()
try:
 initial=F(m.book.risk(m.quotes,now_ms=1000)['equity'])
 m.book.open('oracle-long','position','BTCUSDT','LONG','.09','5',2,m.quotes,now_ms=1000)
 entry=F('100.02');exit=F('99.98');quantity=F('.09');fee_rate=F('.001');principal=quantity*entry+quantity*entry*fee_rate-F(5);interest=principal*F('.00001')
 expected_loss=quantity*(entry-F(100))+quantity*(F(100)-exit)+quantity*entry*fee_rate+quantity*exit*fee_rate+interest
 m.book.close('oracle-close','BTCUSDT','position','.09',m.quotes,now_ms=1000)
 actual=initial-F(m.book.risk(m.quotes,now_ms=1000)['equity'])
 assert actual==expected_loss,(actual,expected_loss)
 assert m.ledger.db.execute("select count(*) from margin_loans where status='OPEN'").fetchone()[0]==0
 print('margin LONG fixed-price economic oracle: spread, entry/exit fees, loan interest and repayment agree')
finally:m.doCleanups()

m=mf.MarginPaperBookTests('runTest');m.setUp()
try:
 with m.ledger.transaction():
  m.ledger._post('oracle-reduce','FIXTURE',{},[('margin_cross:free','USDT',__import__('decimal').Decimal(-45)),('external:fixture','USDT',__import__('decimal').Decimal(45))])
 m.book.open('oracle-liquidation-short','position','BTCUSDT','SHORT','.09','5',2,m.quotes,now_ms=1000)
 shock={symbol:{**q,'price':'1000' if symbol=='BTCUSDT' else '200','time_ms':2000,'server_ms':2000} for symbol,q in m.quotes.items()}
 m.book.record_quotes(shock,now_ms=2000)
 qty=F('.09');entry=F('99.98');exit=F('1000.2');fee=F('.001')
 cash=F(5)+qty*entry*(1-fee);debt=qty*(1+F('.00001'))
 scale=10**20;affordable=cash/(exit*(1+fee));buy_qty=F((affordable*scale).numerator//(affordable*scale).denominator,scale)
 result=m.book.liquidate('oracle-margin-liquidation-short',shock,now_ms=2000)
 remaining=F(result['positions'][0]['remaining_debt'])
 assert remaining==debt-buy_qty,(remaining,debt-buy_qty)
 assert m.ledger.db.execute("SELECT paused FROM wallet_state WHERE wallet='margin_cross'").fetchone()[0]==1
 print('margin liquidation SHORT: independent affordable lot and retained crypto debt agree')
finally:m.doCleanups()
c=cf.CrossPaperBookTests('runTest');c.setUp()
try:
 before=F(c.shared.ledger.balance('futures_cross:free'));p=c.book.open('oracle','BTCUSDT','LONG','5',1,c.quotes,now=100)
 quantity=F(p['quantity']);entry=F(p['entry']);fill=F('99.98');entry_fee=quantity*entry*F('0.0005');exit_fee=quantity*fill*F('0.0005');gross=quantity*(fill-entry)
 c.book.close('oracle-close','BTCUSDT',p['id'],p['quantity'],c.quotes,now=100)
 actual=F(c.shared.ledger.balance('futures_cross:free'));expected=before-entry_fee+gross-exit_fee
 assert actual==expected,(actual,expected)
 print('futures cross LONG fixed-price economic oracle: collateral returned, slippage, fees and PnL agree')
finally:c.doCleanups()

# The expected formulas use rational arithmetic and fixed market prices; they
# do not call the report or ledger's PnL calculation to derive expectations.
m=mf.MarginPaperBookTests('runTest');m.setUp()
try:
 initial=F(m.book.risk(m.quotes,now_ms=1000)['equity'])
 m.book.open('oracle-short','position','ETHUSDT','SHORT','.04','5',2,m.quotes,now_ms=1000)
 qty=F('.04');entry=F('199.96');exit=F('200.04');fee=F('.001');interest=qty*F('.00001')
 expected=qty*(exit-entry)+qty*entry*fee+(qty+interest)*exit*fee+interest*exit
 result=m.book.close('oracle-short-close','ETHUSDT','position','.04',m.quotes,now_ms=1000)
 actual=initial-F(m.book.risk(m.quotes,now_ms=1000)['equity'])
 assert actual==expected,(actual,expected)
 assert F(result['remaining_debt'])==0 and m.ledger.reconcile()['balanced']
 print('margin SHORT: independent slippage, fees, crypto interest and full repayment agree')
finally:m.doCleanups()

c=cf.CrossPaperBookTests('runTest');c.setUp()
try:
 before=F(c.shared.ledger.balance('futures_cross:free'))
 p=c.book.open('oracle-short','ETHUSDT','SHORT','5',1,c.quotes,now=100)
 qty=F(p['quantity']);entry=F(p['entry']);exit=F('200.04');fee=F('.0005')
 expected=before-qty*entry*fee+qty*(entry-exit)-qty*exit*fee
 c.book.close('oracle-short-close','ETHUSDT',p['id'],p['quantity'],c.quotes,now=100)
 assert F(c.shared.ledger.balance('futures_cross:free'))==expected
 print('futures cross SHORT: independent margin release, slippage, fees and PnL agree')
finally:c.doCleanups()

c=cf.CrossPaperBookTests('runTest');c.setUp()
try:
 long,short=c.positions()
 for op,symbol,p,mark,sign in (('fund-long','BTCUSDT',long,'100',-1),('fund-short','ETHUSDT',short,'200',1)):
  event=c.book.funding(op,symbol,p['id'],28000000,0,'.001',mark,'fixture',now=28000)
  assert F(event['value'])==sign*F(p['quantity'])*F(mark)*F('.001')
  assert c.book.funding(op,symbol,p['id'],28000000,0,'.001',mark,'fixture',now=28001)==event
 assert c.shared.db.execute("select count(*) from ledger_events where kind='CROSS_FUNDING'").fetchone()[0]==2
 print('cross funding LONG/SHORT: independent rate × mark × quantity and duplicate handling agree')
finally:c.doCleanups()

s=sf.RuntimeTests('runTest');s.setUp()
try:
 p=s.buy();s.app.execute('SELL',p['quantity'],__import__('decimal').Decimal('102'),'oracle-exit','TEST',p['id'])
 expected=F('.1')*(F(102)-F(100))-F('.1')*(F(100)+F(102))*F('.001')
 assert F(s.store.get('pnl'))==expected and F(s.store.get('cash'))==F(1000)+expected
 print('spot: independent proceeds, cost and two commissions agree')
finally:s.doCleanups()

for direction,price in (('LONG',102),('SHORT',98)):
 f=ff.IsolatedTests('runTest');f.setUp()
 try:
  p=f.open(direction);qty=F(p['quantity']);entry=F(p['entry']);sign=1 if direction=='LONG' else -1
  funding=f.s.apply_funding(p['id'],[{'time':2000,'rate':'.001','mark':'100'}],3000)
  expected_funding=-sign*qty*F(100)*F('.001')
  assert F(f.s.get('position')['funding'])==expected_funding
  f.close(price)
  expected=sign*qty*(F(price)-entry)-qty*(entry+F(price))*F('.0005')+expected_funding
  assert F(f.s.get('pnl'))==expected,(direction,f.s.get('pnl'),expected)
  print('isolated',direction,': independent margin, fees, PnL and funding agree')
 finally:f.doCleanups()

f=ff.IsolatedTests('runTest');f.setUp()
try:
 import json
 p=f.open('SHORT');qty=F(p['quantity']);entry=F(p['entry']);margin=F(p['margin']);exit=F(1000)
 f.close(1000)
 raw=margin+qty*(entry-exit)-qty*(entry+exit)*F('.0005')
 event=json.loads(f.s.db.execute("SELECT payload FROM futures_events WHERE event='CLOSE'").fetchone()[0])
 assert raw<0 and F(event['cap_adjustment'])==-raw
 assert F(f.s.get('cash'))==F(1000)-margin and F(f.s.get('pnl'))==-margin
 print('isolated liquidation: independent capped loss and model adjustment agree')
finally:f.doCleanups()

c=cf.CrossPaperBookTests('runTest');c.setUp()
try:
 initial=F(c.shared.ledger.balance('futures_cross:free'));long,short=c.positions()
 marks={'BTCUSDT':c.quote('1',101),'ETHUSDT':c.quote('4000',101)}
 c.book.record_quotes(marks,now=101)
 total=initial
 for p,mark in ((long,F(1)),(short,F(4000))):
  qty=F(p['quantity']);entry=F(p['entry']);direction=1 if p['direction']=='LONG' else -1
  entryfee=qty*entry*F('.0005');fill=mark*(1-direction*F('.0002'))
  total+=-entryfee+direction*qty*(fill-entry)-qty*fill*F('.0005')
 result=c.book.close('oracle-liquidation',long['symbol'],long['id'],long['quantity'],marks,now=101,liquidate=True)
 assert F(result['adjustment'])==max(F(0),-total)
 assert F(c.shared.ledger.balance('futures_cross:free'))==max(F(0),total)
 assert F(c.shared.ledger.balance('spot:free'))==1000
 print('cross liquidation: independent cash, exit costs and deficit adjustment agree; spot preserved')
finally:c.doCleanups()

m=mf.MarginPaperBookTests('runTest');m.setUp()
try:
 with m.ledger.transaction():
  m.ledger._post('oracle-reduce','FIXTURE',{},[('margin_cross:free','USDT',__import__('decimal').Decimal(-45)),('external:fixture','USDT',__import__('decimal').Decimal(45))])
 m.book.open('oracle-liquidation','position','BTCUSDT','LONG','.09','5',2,m.quotes,now_ms=1000)
 shock={symbol:{**q,'price':'1' if symbol=='BTCUSDT' else '200','time_ms':2000,'server_ms':2000} for symbol,q in m.quotes.items()}
 m.book.record_quotes(shock,now_ms=2000)
 principal=F('.09')*F('100.02')*(1+F('.001'))-5
 debt=principal*(1+F('.00001'))
 proceeds=F('.09')*F('.9998')*(1-F('.001'))
 result=m.book.liquidate('oracle-margin-liquidation',shock,now_ms=2000)
 remaining=F(result['positions'][0]['remaining_debt'])
 assert remaining==debt-proceeds,(remaining,debt-proceeds)
 assert m.ledger.db.execute("SELECT paused FROM wallet_state WHERE wallet='margin_cross'").fetchone()[0]==1
 print('margin liquidation LONG: independent loan, interest, liquidation proceeds and retained debt agree')
finally:m.doCleanups()

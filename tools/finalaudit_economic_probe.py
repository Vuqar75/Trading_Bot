from fractions import Fraction as F
from tests import test_margin_paper_book as mf
from tests import test_cross_paper_book as cf
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
c=cf.CrossPaperBookTests('runTest');c.setUp()
try:
 before=F(c.shared.ledger.balance('futures_cross:free'));p=c.book.open('oracle','BTCUSDT','LONG','5',1,c.quotes,now=100)
 quantity=F(p['quantity']);entry=F(p['entry']);fill=F('99.98');entry_fee=quantity*entry*F('0.0005');exit_fee=quantity*fill*F('0.0005');gross=quantity*(fill-entry)
 c.book.close('oracle-close','BTCUSDT',p['id'],p['quantity'],c.quotes,now=100)
 actual=F(c.shared.ledger.balance('futures_cross:free'));expected=before-entry_fee+gross-exit_fee
 assert actual==expected,(actual,expected)
 print('futures cross LONG fixed-price economic oracle: collateral returned, slippage, fees and PnL agree')
finally:c.doCleanups()

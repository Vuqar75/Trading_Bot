import sqlite3,unittest
from decimal import Decimal
from research.adaptive_experiment_fence import install
from tests import test_adaptive_atomic_spot as spot_fixture
from tests import test_adaptive_atomic_futures as futures_fixture
from tests import test_cross_paper_book as cross_fixture
from trading_bot.margin_paper_book import MarginPaperBook

class SpotFenceTest(unittest.TestCase):
 def setUp(self):
  fixture=spot_fixture.AtomicSpotTest();fixture.setUp();self.addCleanup(fixture.doCleanups);self.fixture=fixture
 def test_fence_blocks_legacy_spot_but_permits_guarded_route(self):
  f=self.fixture;install(f.store.db)
  with self.assertRaisesRegex(sqlite3.IntegrityError,'adaptive admission required'):
   f.store.prepare('BUY',Decimal('.01'),Decimal('100'),'legacy','LEGACY')
  self.assertEqual(f.store.db.execute('SELECT count(*) FROM runtime_orders').fetchone()[0],0)
  self.assertEqual(f.call()['status'],'filled')
  self.assertEqual(f.store.db.execute('SELECT count(*) FROM adaptive_admission_token').fetchone()[0],0)
 def test_fence_blocks_unrouted_cross_and_margin(self):
  f=self.fixture;install(f.store.db)
  for table in ('cross_positions','margin_positions'):
   with self.assertRaisesRegex(sqlite3.IntegrityError,'adaptive market unavailable'):
    if table=='cross_positions':f.store.db.execute("INSERT INTO cross_positions VALUES('BTCUSDT','id','LONG','1','100','100',1,'0',1)")
    else:f.store.db.execute("INSERT INTO margin_positions VALUES('id','BTCUSDT','LONG','1','100','100',1,NULL,1)")

class FuturesFenceTest(unittest.TestCase):
 def setUp(self):
  fixture=futures_fixture.AtomicFuturesTest();fixture.setUp();self.addCleanup(fixture.doCleanups);self.fixture=fixture
 def test_fence_blocks_legacy_futures_but_permits_guarded_route(self):
  f=self.fixture;install(f.store.db);cash=f.store.get('cash')
  with self.assertRaisesRegex(sqlite3.IntegrityError,'adaptive admission required'):
   f.store.open_position('SHORT',Decimal('100'),f.now,'legacy')
  self.assertIsNone(f.store.get('position'));self.assertEqual(f.store.get('cash'),cash)
  self.assertEqual(f.call()['status'],'filled')
  self.assertEqual(f.store.db.execute('SELECT count(*) FROM adaptive_admission_token').fetchone()[0],0)

class CrossFenceTest(unittest.TestCase):
 def test_real_cross_handler_is_blocked_and_rolls_back(self):
  f=cross_fixture.CrossPaperBookTests();f.setUp();self.addCleanup(f.doCleanups)
  MarginPaperBook(f.shared.ledger);install(f.shared.db)
  before=f.shared.ledger.balance('futures_cross:free','USDT')
  with self.assertRaisesRegex(sqlite3.IntegrityError,'adaptive market unavailable'):
   f.book.open('legacy','BTCUSDT','LONG','5',1,f.quotes,now=100)
  self.assertEqual(f.shared.ledger.balance('futures_cross:free','USDT'),before)
  self.assertEqual(f.shared.db.execute('SELECT count(*) FROM cross_positions').fetchone()[0],0)

 def test_real_margin_handler_is_blocked_and_rolls_back(self):
  f=cross_fixture.CrossPaperBookTests();f.setUp();self.addCleanup(f.doCleanups)
  shared=f.shared;book=MarginPaperBook(shared.ledger)
  proposal=shared.propose_transfer('margin-fund','futures_isolated','margin_cross','20',now=99)
  shared.confirm_transfer(proposal['id'],now=99)
  with shared.ledger.transaction():shared.db.execute("UPDATE wallet_state SET paused=0 WHERE wallet='margin_cross'")
  rules={'lot_rules':[{'min':'0','max':'0','step':'0.00000000000000000001'}],'min_notional':'0','max_notional':'0','observed_ms':1000}
  quotes={'BTCUSDT':{'price':'100','time_ms':1000,'server_ms':1000,'source':'fixture','price_kind':'last_public_trade','filters':rules}}
  quotes['ETHUSDT']={**quotes['BTCUSDT'],'price':'200'}
  book.record_quotes(quotes,now_ms=1000);install(shared.db)
  before=shared.ledger.balance('margin_cross:free','USDT')
  with self.assertRaisesRegex(sqlite3.IntegrityError,'adaptive market unavailable'):
   book.open('legacy-margin','id','BTCUSDT','LONG','.09','5',2,quotes,now_ms=1000)
  self.assertEqual(shared.ledger.balance('margin_cross:free','USDT'),before)
  self.assertEqual(shared.db.execute('SELECT count(*) FROM margin_positions').fetchone()[0],0)

class IndependentExperimentTest(unittest.TestCase):
 def test_spot_and_short_in_separate_experiments_do_not_block_each_other(self):
  a=spot_fixture.AtomicSpotTest();a.setUp();self.addCleanup(a.doCleanups)
  b=futures_fixture.AtomicFuturesTest();b.setUp();self.addCleanup(b.doCleanups)
  install(a.store.db);install(b.store.db)
  self.assertNotEqual(a.fin,b.fin)
  self.assertEqual(a.call()['status'],'filled');self.assertEqual(b.call()['status'],'filled')
  self.assertGreater(Decimal(a.store.get('position')['quantity']),0)
  self.assertEqual(b.store.get('position')['direction'],'SHORT')

import sqlite3,tempfile,unittest
from decimal import Decimal
from pathlib import Path
from trading_bot.runtime_store import RuntimeStore
from trading_bot.futures_store import FuturesStore
from trading_bot.paper_shared_store import SharedPaperStore
from trading_bot.paper_consolidation import consolidate_snapshots
from trading_bot.cross_paper_book import CrossPaperBook
from trading_bot.margin_paper_book import MarginPaperBook
from trading_bot.unified_queue import UnifiedQueue
from research.adaptive_exposure_guard import require_clear
from research.adaptive_route_state import RouteState
from research.adaptive_shadow import PERIOD
from trading_bot.runtime_exchange import PaperExchange
from trading_bot.app import Application

class ExposureGuardTests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);p=Path(self.tmp.name)
  spot=RuntimeStore(p/'spot',{'mode':'paper','symbol':'BTCUSDT'});spot.close();future=FuturesStore(p/'future');future.close()
  self.fin=p/'financial';self.qpath=p/'queue';consolidate_snapshots(p/'spot',p/'future',self.fin,captured_at=1)
  shared=SharedPaperStore(self.fin);CrossPaperBook(shared);MarginPaperBook(shared.ledger);shared.close()
  q=UnifiedQueue(self.qpath);q.close();self.db=sqlite3.connect(self.fin,isolation_level=None);self.addCleanup(self.db.close)
  self.db.execute('ATTACH DATABASE ? AS route_queue',(str(self.qpath),))
 def test_clean_and_queue_rejects_pending(self):
  self.db.execute('BEGIN IMMEDIATE');self.assertTrue(require_clear(self.db));self.db.rollback()
  q=UnifiedQueue(self.qpath)
  try:q.receive(1,True,'/pause')
  finally:q.close()
  self.db.execute('BEGIN IMMEDIATE')
  try:
   with self.assertRaisesRegex(ValueError,'queued_action'):require_clear(self.db)
  finally:self.db.rollback()
 def test_cross_and_margin_position_rejected_under_same_lock(self):
  for table in ('cross_positions','margin_positions'):
   self.db.execute('BEGIN IMMEDIATE')
   try:
    if table=='cross_positions':self.db.execute("INSERT INTO cross_positions VALUES('BTCUSDT','p','LONG','0.1','100','10',1,'0',1000)")
    else:self.db.execute("INSERT INTO margin_positions VALUES('p','BTCUSDT','SHORT','0.1','100','10',1,NULL,1000)")
    with self.assertRaisesRegex(ValueError,'position_exists'):require_clear(self.db)
   finally:self.db.rollback()
 def test_rejects_no_transaction_or_unattached_queue(self):
  with self.assertRaisesRegex(ValueError,'financial_transaction'):require_clear(self.db)
  d=sqlite3.connect(self.fin,isolation_level=None);d.execute('BEGIN IMMEDIATE')
  try:
   with self.assertRaisesRegex(ValueError,'attached_queue'):require_clear(d)
  finally:d.rollback();d.close()
 def test_isolated_real_spot_fill_and_queued_manual_pause(self):
  now=20000*PERIOD;route=RouteState(self.fin.parent/'route',clock=lambda:now)
  decision={'mode':'TREND_UP','snapshot_id':'s','data_ms':now,'expires_ms':now+PERIOD,'reasons':['r'],'uncertainties':['u']}
  signal={'source':'AI','symbol':'BTCUSDT','snapshot_id':'s','strategy':'trend1','direction':'LONG'}
  store=RuntimeStore(self.fin,{'mode':'paper','symbol':'BTCUSDT'},shared=True)
  q=UnifiedQueue(self.qpath)
  try:
   app=Application(store,PaperExchange(),None);store.command(1,True,'/resume')
   self.db.execute('BEGIN IMMEDIATE');self.assertTrue(require_clear(self.db));self.db.rollback()
   route.begin_entry('entry',decision,signal,snapshot_id='s',data_ms=now,symbol='BTCUSDT',decision_version='regular-regime-horizon-v2',quote_ms=now,expiry_ms=now+5000,eligible=True,research_sandbox=True)
   self.assertTrue(app.execute('BUY','.1',Decimal('100'),'entry','TEST'))
   self.assertEqual(route.confirm_open('entry',self.fin)['phase'],'OPEN')
   q.receive(2,True,'/pause');job=q.pending('spot')[0]
   store.command(job['update_id'],True,job['command'],routed=True);q.acknowledge(job['id'],'applied')
   self.assertTrue(store.get('paused'));self.assertGreater(Decimal(store.get('position')['quantity']),0)
   self.assertEqual(route.status()['phase'],'OPEN')
  finally:q.close();store.close();route.close()

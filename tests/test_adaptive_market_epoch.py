import sqlite3,tempfile,unittest
from pathlib import Path
from research.adaptive_study_supervisor import bootstrap
from research.adaptive_market_epoch import start_epoch,ingest_epoch
from research.adaptive_study_backup import create,verify
class EpochTests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name)/'study';bootstrap(self.root,clock=lambda:900000)
 def tearDown(self):self.tmp.cleanup()
 def test_preserves_accounts_and_versions(self):
  before={n:(self.root/n/'financial.db').read_bytes() for n in ('ai','baseline')}
  db=sqlite3.connect(self.root/'market-evidence.db',isolation_level=None)
  db.execute('CREATE TABLE market_conflicts(market TEXT,start_ms INTEGER,payload TEXT,observed_ms INTEGER)');db.execute("INSERT INTO market_conflicts VALUES('futures',0,'old',123)")
  epoch=start_epoch(self.root,market='futures',clock=lambda:1800000);self.assertEqual(epoch['proof']['ai']['spot_cash'],'500')
  rows=[[0,1,2,1,1],[900000,1,2,1,1]];c={'market':'futures','symbol':'BTCUSDT','source':'binance_usdm_klines','bars':rows[:-1],'skip_bar':rows[-1]}
  self.assertTrue(ingest_epoch(db,market='futures',candles=c,observed_ms=1800000)['entry_blocked'])
  c['bars']=[[900000,1,2,1,1],[1800000,1,2,1,1]];c['skip_bar']=[2700000,1,2,1,1]
  self.assertFalse(ingest_epoch(db,market='futures',candles=c,observed_ms=3600000)['entry_blocked'])
  c['bars'][0][4]=1.1;self.assertTrue(ingest_epoch(db,market='futures',candles=c,observed_ms=3600001)['entry_blocked'])
  bad=dict(c,source='binance_spot_klines')
  with self.assertRaisesRegex(ValueError,'invalid_market'):ingest_epoch(db,market='futures',candles=bad,observed_ms=3600001)
  bad=dict(c,skip_bar=[4500000,1,2,1,1])
  with self.assertRaisesRegex(ValueError,'invalid_candles'):ingest_epoch(db,market='futures',candles=bad,observed_ms=5400000)
  self.assertEqual(db.execute('select count(*) from market_conflicts').fetchone()[0],1);self.assertEqual(db.execute('select count(*) from epoch_conflicts').fetchone()[0],1)
  self.assertEqual(before,{n:(self.root/n/'financial.db').read_bytes() for n in ('ai','baseline')});db.close()
  manifest,archive=create(self.root,version='epoch-test',clock=lambda:3600002)
  self.assertEqual(verify(archive)['start_ms'],900000)
  self.assertIn('market-evidence.db',manifest['files'])
 def test_route_or_reserve_blocks(self):
  p=self.root/'ai'/'route.db';db=sqlite3.connect(p);db.execute("UPDATE route_state SET phase='RECONCILE' WHERE id=1");db.commit();db.close()
  with self.assertRaisesRegex(ValueError,'route_not_flat'):start_epoch(self.root,market='futures',clock=lambda:1800000)
 def test_second_epoch_without_conflict_is_rejected(self):
  start_epoch(self.root,market='futures',clock=lambda:1800000)
  with self.assertRaisesRegex(ValueError,'epoch_already_active'):start_epoch(self.root,market='futures',clock=lambda:1800001)
if __name__=='__main__':unittest.main()

import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
import concurrent.futures
import time
from pathlib import Path

from trading_bot.paper_pair_migration import tag_legacy_spot_orders
from trading_bot.runtime_store import RuntimeStore


class PairMigrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.path=Path(self.tmp.name)/'legacy.db'
        store=RuntimeStore(self.path,{'mode':'paper','symbol':'BTCUSDT'})
        try:
            with store.transaction():
                store.db.execute("INSERT INTO runtime_orders VALUES('old','old-key','BUY','0.1','100','SETTLED','FILLED','fixture',1)")
                store.db.execute('INSERT INTO runtime_fills VALUES(?,?,?)',('trade','old',json.dumps({'symbol':'BTCUSDT','qty':'0.1'})))
        finally:store.close()

    def read(self, sql, params=()):
        with sqlite3.connect(self.path) as db:return db.execute(sql,params).fetchall()

    def test_repeat_and_restart_preserve_legacy_orders_and_money(self):
        before=(self.read('SELECT * FROM runtime_orders'),self.read('SELECT * FROM runtime_fills'),
                self.read("SELECT key,value FROM runtime_meta WHERE key IN ('cash','position','pnl') ORDER BY key"))
        self.assertFalse(tag_legacy_spot_orders(self.path)['replayed'])
        self.assertTrue(tag_legacy_spot_orders(self.path)['replayed'])
        after=(self.read('SELECT * FROM runtime_orders'),self.read('SELECT * FROM runtime_fills'),
               self.read("SELECT key,value FROM runtime_meta WHERE key IN ('cash','position','pnl') ORDER BY key"))
        self.assertEqual(before,after)
        self.assertEqual(json.loads(self.read("SELECT value FROM runtime_meta WHERE key='pair_order_symbol:old'")[0][0]),'BTCUSDT')
        store=RuntimeStore(self.path,{'mode':'paper','symbol':'BTCUSDT'});store.close()

    def test_refuses_unpaused_pending_and_conflicting_symbol(self):
        for sql in ("UPDATE runtime_meta SET value='false' WHERE key='paused'",
                    "UPDATE runtime_orders SET phase='UNKNOWN' WHERE client_id='old'",
                    "INSERT INTO runtime_meta VALUES('pair_order_symbol:old','\"SOLUSDT\"')"):
            with self.subTest(sql=sql):
                with sqlite3.connect(self.path) as db:db.execute('SAVEPOINT fixture');db.execute(sql)
                with self.assertRaises(ValueError):tag_legacy_spot_orders(self.path)
                with sqlite3.connect(self.path) as db:
                    db.execute("UPDATE runtime_meta SET value='true' WHERE key='paused'")
                    db.execute("UPDATE runtime_orders SET phase='SETTLED' WHERE client_id='old'")
                    db.execute("DELETE FROM runtime_meta WHERE key='pair_order_symbol:old'")
                self.assertEqual(self.read("SELECT 1 FROM runtime_meta WHERE key='pair_legacy_btc_migration'"),[])

    def test_actual_process_death_before_and_after_commit(self):
        script='''import os,sys\nfrom trading_bot.paper_pair_migration import tag_legacy_spot_orders\ndef fault(stage):\n if stage==sys.argv[2]:os._exit(74)\ntag_legacy_spot_orders(sys.argv[1],fault=fault)\n'''
        for stage in ('before_commit','after_commit'):
            with self.subTest(stage=stage):
                child=subprocess.run([sys.executable,'-B','-c',script,str(self.path),stage],timeout=20,capture_output=True)
                self.assertEqual(child.returncode,74,child.stderr.decode())
                exists=bool(self.read("SELECT 1 FROM runtime_meta WHERE key='pair_legacy_btc_migration'"))
                self.assertEqual(exists,stage=='after_commit')
        self.assertTrue(tag_legacy_spot_orders(self.path)['replayed'])

    def test_new_intent_is_bound_and_conflict_fails_before_claim(self):
        from decimal import Decimal
        from trading_bot.testnet_observer import ObservationError
        store=RuntimeStore(self.path,{'mode':'paper','symbol':'BTCUSDT'})
        try:
            with store.transaction():store.set('paused',False)
            client=store.prepare('BUY',Decimal('0.1'),Decimal('100'),'new-key','fixture')
            self.assertEqual(store.get('pair_order_symbol:'+client),'BTCUSDT')
            with store.transaction():store.set('pair_order_symbol:'+client,'SOLUSDT')
            with self.assertRaisesRegex(ObservationError,'unsupported pair recovery'):
                store.claim(client)
            row=store.db.execute('SELECT phase FROM runtime_orders WHERE client_id=?',(client,)).fetchone()
            self.assertEqual(row[0],'PREPARED')
        finally:store.close()

    def test_waiting_for_real_sqlite_lock_rechecks_pause(self):
        with sqlite3.connect(self.path,isolation_level=None) as lock:
            lock.execute('BEGIN IMMEDIATE')
            lock.execute("UPDATE runtime_meta SET value='false' WHERE key='paused'")
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                future=pool.submit(tag_legacy_spot_orders,self.path,timeout=3)
                time.sleep(0.15)
                self.assertFalse(future.done())
                lock.commit()
                with self.assertRaisesRegex(ValueError,'paused'):
                    future.result(timeout=3)
        self.assertEqual(self.read("SELECT 1 FROM runtime_meta WHERE key='pair_legacy_btc_migration'"),[])

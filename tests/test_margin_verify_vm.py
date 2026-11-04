import importlib.util
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

spec=importlib.util.spec_from_file_location('margin_verify_test_module',Path(__file__).resolve().parents[1]/'tools/margin_verify_vm.py')
verify=importlib.util.module_from_spec(spec);spec.loader.exec_module(verify)


class MarginVerifyVmTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);root=Path(self.tmp.name)
        self.financial=root/'financial.db';self.queue=root/'queue.db'
        db=sqlite3.connect(self.financial)
        db.executescript("""CREATE TABLE ledger_meta(key TEXT PRIMARY KEY,value TEXT);
        CREATE TABLE runtime_meta(key TEXT PRIMARY KEY,value TEXT);
        CREATE TABLE futures_meta(key TEXT PRIMARY KEY,value TEXT);
        CREATE TABLE wallet_state(wallet TEXT PRIMARY KEY,paused INTEGER);
        CREATE TABLE ledger_events(id TEXT PRIMARY KEY,payload TEXT);
        CREATE TABLE ledger_postings(event_id TEXT,ordinal INTEGER,amount TEXT);
        INSERT INTO ledger_meta VALUES('deployment_hold','false');
        INSERT INTO runtime_meta VALUES('paused','false');INSERT INTO futures_meta VALUES('paused','false');
        INSERT INTO wallet_state VALUES('spot',0),('futures_isolated',0),('futures_cross',1),('margin_cross',0);
        INSERT INTO ledger_events VALUES('old','immutable');INSERT INTO ledger_postings VALUES('old',0,'5');""")
        db.commit();db.close();sqlite3.connect(self.queue).close()

    def run_hold(self):
        with patch.object(verify,'FINANCIAL',self.financial),patch.object(verify,'QUEUE',self.queue):verify._emergency_hold()

    def test_emergency_hold_pauses_all_without_changing_events(self):
        self.run_hold();self.run_hold()
        with sqlite3.connect(self.financial) as db:
            self.assertEqual(db.execute("SELECT value FROM ledger_meta WHERE key='deployment_hold'").fetchone()[0],'true')
            self.assertEqual(db.execute('SELECT count(*) FROM wallet_state WHERE paused=0').fetchone()[0],0)
            self.assertEqual(db.execute('SELECT * FROM ledger_events').fetchall(),[('old','immutable')])
            self.assertEqual(db.execute('SELECT * FROM ledger_postings').fetchall(),[('old',0,'5')])

    def test_missing_pause_rolls_back_entire_emergency_hold(self):
        with sqlite3.connect(self.financial) as db:db.execute("DELETE FROM wallet_state WHERE wallet='margin_cross'")
        with self.assertRaisesRegex(ValueError,'Missing wallet'):self.run_hold()
        with sqlite3.connect(self.financial) as db:
            self.assertEqual(db.execute("SELECT value FROM ledger_meta WHERE key='deployment_hold'").fetchone()[0],'false')
            self.assertEqual(db.execute("SELECT value FROM runtime_meta WHERE key='paused'").fetchone()[0],'false')

    def test_history_extension_is_allowed_but_modification_is_rejected(self):
        baseline=Path(self.tmp.name)/'baseline.db';source=sqlite3.connect(self.financial);target=sqlite3.connect(baseline)
        try:source.backup(target)
        finally:target.close();source.close()
        with sqlite3.connect(self.financial) as db:db.execute("INSERT INTO ledger_events VALUES('new','extra')")
        verify._history_preserved(baseline,self.financial)
        with sqlite3.connect(self.financial) as db:db.execute("UPDATE ledger_postings SET amount='6' WHERE event_id='old'")
        with self.assertRaisesRegex(ValueError,'history changed'):verify._history_preserved(baseline,self.financial)


if __name__=='__main__':unittest.main()

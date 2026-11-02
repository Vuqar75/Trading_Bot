import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from trading_bot.cross_unit_switch import switch_unit_files,restore_unit_files,hold_writer_locks
from trading_bot.cross_snapshot_guard import financial_history_unchanged
from trading_bot.paper_consolidation import consolidate_snapshots
from trading_bot.paper_shared_store import SharedPaperStore
from trading_bot.runtime_store import RuntimeStore
from trading_bot.futures_store import FuturesStore


class CrossUnitSwitchTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name);self.units=self.root/'units';self.candidate=self.root/'candidate';self.archive=self.root/'archive'
        for p in (self.units,self.candidate,self.archive):p.mkdir()
        self.names=('spot.service','futures.service');self.writers=self.root/'writers';self.writers.write_text('0')
        for n in self.names:
            (self.units/n).write_text('old '+n);(self.candidate/n).write_text('new '+n)

    def safe(self):
        if self.writers.read_text()!='0':raise ValueError('A financial writer is running')
        return True

    def switch(self):return switch_unit_files(self.units,self.candidate,self.archive,self.names,assert_safe=self.safe)

    def test_kill_after_one_unit_and_resume_without_starting_writers(self):
        code=("import os,sys; from pathlib import Path; from trading_bot.cross_unit_switch import switch_unit_files; "
              "r=Path(sys.argv[1]); assert_safe=lambda: (r/'writers').read_text()=='0'; "
              "fault=lambda p: os._exit(73) if p=='switched:spot.service' else None; "
              "switch_unit_files(r/'units',r/'candidate',r/'archive',('spot.service','futures.service'),assert_safe=assert_safe,fault=fault)")
        child=subprocess.run([sys.executable,'-c',code,str(self.root)])
        self.assertEqual(child.returncode,73)
        self.assertEqual((self.units/'spot.service').read_text(),'new spot.service')
        self.assertEqual((self.units/'futures.service').read_text(),'old futures.service')
        self.assertEqual(self.writers.read_text(),'0')
        self.assertTrue(self.switch()['all_switched'])
        self.assertEqual((self.units/'futures.service').read_text(),'new futures.service')
        self.assertEqual((self.archive/'spot.service.before').read_text(),'old spot.service')

    def test_replay_refuses_external_change_and_running_writer(self):
        self.switch();(self.units/'spot.service').write_text('unexpected')
        with self.assertRaisesRegex(ValueError,'Unexpected'):self.switch()
        (self.units/'spot.service').write_text('new spot.service');self.writers.write_text('1')
        with self.assertRaisesRegex(ValueError,'writer'):self.switch()
        self.assertEqual((self.units/'futures.service').read_text(),'new futures.service')

    def test_writer_appearing_between_unit_changes_stops_further_switch(self):
        def fault(point):
            if point=='switched:spot.service':self.writers.write_text('1')
        with self.assertRaisesRegex(ValueError,'writer'):
            switch_unit_files(self.units,self.candidate,self.archive,self.names,assert_safe=self.safe,fault=fault)
        self.assertEqual((self.units/'spot.service').read_text(),'new spot.service')
        self.assertEqual((self.units/'futures.service').read_text(),'old futures.service')

    def test_restore_refuses_new_financial_history_and_never_touches_database(self):
        self.switch();financial=self.root/'financial.db';financial.write_bytes(b'new financial events')
        def reject():raise ValueError('New financial events cannot be rolled back')
        with self.assertRaisesRegex(ValueError,'financial'):
            restore_unit_files(self.units,self.archive,self.names,assert_safe=self.safe,verify_rollback=reject)
        self.assertEqual((self.units/'spot.service').read_text(),'new spot.service')
        self.assertEqual(financial.read_bytes(),b'new financial events')
        with self.assertRaisesRegex(ValueError,'safety not proven'):
            restore_unit_files(self.units,self.archive,self.names,assert_safe=self.safe,verify_rollback=lambda:False)
        self.assertTrue(restore_unit_files(self.units,self.archive,self.names,assert_safe=self.safe,verify_rollback=lambda:True)['all_restored'])
        self.assertEqual((self.units/'spot.service').read_text(),'old spot.service')
        self.assertEqual(financial.read_bytes(),b'new financial events')

    def test_real_process_locks_exclude_new_workers_through_switch(self):
        financial=self.root/'financial.db';financial.write_bytes(b'fixture')
        code="import sys\nfrom trading_bot.app import process_lock\nwith process_lock(sys.argv[1]):\n print('acquired')\n"
        with hold_writer_locks(financial):
            self.switch()
            for suffix in ('.spot.lock','.futures.lock','.cross.lock','.delivery.lock'):
                child=subprocess.run([sys.executable,'-c',code,str(financial)+suffix],capture_output=True,text=True)
                self.assertNotEqual(child.returncode,0)
                self.assertNotIn('acquired',child.stdout)
        for suffix in ('.spot.lock','.futures.lock','.cross.lock','.delivery.lock'):
            child=subprocess.run([sys.executable,'-c',code,str(financial)+suffix],capture_output=True,text=True)
            self.assertEqual(child.returncode,0)

    def test_existing_worker_blocks_fence_and_releases_partial_locks(self):
        from trading_bot.app import process_lock
        financial=self.root/'financial.db';financial.write_bytes(b'fixture')
        with process_lock(str(financial)+'.futures.lock'):
            with self.assertRaisesRegex(ValueError,'still running'):
                with hold_writer_locks(financial):pass
            with process_lock(str(financial)+'.spot.lock'):pass
        with hold_writer_locks(financial):pass

    def test_crash_after_one_unit_requires_reacquiring_real_writer_locks(self):
        from trading_bot.app import process_lock
        financial=self.root/'financial.db';financial.write_bytes(b'fixture')
        code=("import os,sys\nfrom pathlib import Path\nfrom trading_bot.cross_unit_switch import switch_unit_files,hold_writer_locks\n"
              "r=Path(sys.argv[1])\nwith hold_writer_locks(r/'financial.db'):\n"
              " switch_unit_files(r/'units',r/'candidate',r/'archive',('spot.service','futures.service'),assert_safe=lambda:True,"
              "fault=lambda p:os._exit(73) if p=='switched:spot.service' else None)\n")
        child=subprocess.run([sys.executable,'-c',code,str(self.root)])
        self.assertEqual(child.returncode,73)
        with process_lock(str(financial)+'.spot.lock'):
            with self.assertRaisesRegex(ValueError,'still running'):
                with hold_writer_locks(financial):self.switch()
            self.assertEqual((self.units/'futures.service').read_text(),'old futures.service')
        with hold_writer_locks(financial):self.assertTrue(self.switch()['all_switched'])

    def test_new_committed_transfer_blocks_restore_through_real_guard(self):
        spot=self.root/'spot.db';futures=self.root/'futures.db';financial=self.root/'actual.db';baseline=self.root/'baseline.db'
        s=RuntimeStore(spot,{'mode':'paper','interval':'15m'});f=FuturesStore(futures);s.close();f.close()
        consolidate_snapshots(spot,futures,financial,captured_at=1)
        with closing(sqlite3.connect(financial)) as source,closing(sqlite3.connect(baseline)) as target:source.backup(target)
        self.switch()
        with closing(SharedPaperStore(financial)) as shared:
            transfer=shared.propose_transfer('test','spot','futures_isolated','1',now=100)
            shared.confirm_transfer(transfer['id'],now=100)
        with self.assertRaisesRegex(ValueError,'Rollback safety not proven'):
            restore_unit_files(self.units,self.archive,self.names,assert_safe=self.safe,
                               verify_rollback=lambda:financial_history_unchanged(baseline,financial))
        self.assertEqual((self.units/'spot.service').read_text(),'new spot.service')
        with closing(SharedPaperStore(financial)) as shared:
            self.assertEqual(shared.db.execute("SELECT count(*) FROM ledger_events WHERE kind='TRANSFER'").fetchone()[0],1)

import sqlite3
import subprocess
import sys
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path
from trading_bot.app import Application
from trading_bot.futures_store import FuturesStore
from trading_bot.paper_consolidation import consolidate_snapshots
from trading_bot.runtime_exchange import PaperExchange
from trading_bot.runtime_store import RuntimeStore
from trading_bot.unified_queue import UnifiedQueue


class SharedProcessRehearsalTests(unittest.TestCase):
    def test_two_actual_entry_points_with_network_blocked_and_restart(self):
        with tempfile.TemporaryDirectory(prefix='paper-process-test-') as td:
            root=Path(td)
            identity={'schema':1,'mode':'paper','symbol':'BTCUSDT','interval':'15m','amount':'10','account':'paper'}
            spot=RuntimeStore(root/'source-spot.db',identity);future=FuturesStore(root/'source-futures.db')
            try:
                Application(spot,PaperExchange(),None)
                future.command(1,True,'/resume');future.open_position('SHORT',Decimal(100),1000,'entry')
                with future.transaction():future.set('paused',True)
                for src,name in ((spot.db,'spot-copy.db'),(future.db,'future-copy.db')):
                    with sqlite3.connect(root/name) as dst:src.backup(dst)
            finally:spot.close();future.close()
            consolidate_snapshots(root/'spot-copy.db',root/'future-copy.db',root/'shared.db',captured_at=1)
            q=UnifiedQueue(root/'queue.db');q.close()
            with sqlite3.connect(root/'observation.db') as db:db.execute('CREATE TABLE samples(ts REAL PRIMARY KEY)')
            script=Path(__file__).resolve().parents[1]/'tools/rehearse_shared_processes.py'
            result=subprocess.run([sys.executable,'-B',str(script),str(root)],capture_output=True,text=True,timeout=90)
            self.assertEqual(result.returncode,0,result.stdout+'\n'+result.stderr)
            self.assertIn('TWO_WORKERS_HOLD_QUEUE_BACKUP_RESTART_OK',result.stdout)

import json
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path

from trading_bot.unified_queue import UnifiedQueue


class PairQueueOfflineTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.path=Path(self.tmp.name)/'queue.db'
        self.q=UnifiedQueue(self.path,allow_pair_experiment=True)
        self.addCleanup(lambda:self.q.close())
        self.snap={'spot':{'symbols':['BTCUSDT'],'version':3},
                   'futures':{'symbols':['BTCUSDT'],'version':4}}

    def proposal(self):
        return self.q.db.execute('SELECT * FROM unified_pair_proposals').fetchone()

    def test_bound_market_version_and_one_confirmation_after_mode_change(self):
        self.q.receive(1,True,'/pair SOLUSDT',now=100,pairs=self.snap)
        row=self.proposal();self.assertEqual(row['market'],'spot');self.assertEqual(row['version'],3)
        self.q.receive(1,True,'/pair SOLUSDT',now=101,pairs={'spot':{'symbols':['ETHUSDT'],'version':99}})
        self.q.receive(2,True,'/mode futures',now=102)
        self.q.receive(3,True,'/confirm '+row['id'],now=103)
        self.q.receive(4,True,'/confirm '+row['id'],now=104)
        jobs=self.q.pending('spot');self.assertEqual(len(jobs),1)
        payload=json.loads(jobs[0]['payload'])
        self.assertEqual((payload['previous'],payload['version'],payload['target']),(['BTCUSDT'],3,['SOLUSDT']))
        self.assertEqual(self.q.pending('futures'),[])
        self.assertEqual(self.q.state('mode'),'futures')

    def test_foreign_expired_and_cancelled_confirmations_do_not_enqueue(self):
        self.q.receive(1,False,'/pair SOLUSDT',now=100,pairs=self.snap)
        self.assertIsNone(self.proposal())
        self.q.receive(2,True,'/pair SOLUSDT',now=100,pairs=self.snap)
        token=self.proposal()['id']
        self.q.receive(3,False,'/confirm '+token,now=101)
        self.q.receive(4,True,'/confirm '+token,now=220)
        self.assertEqual(self.q.pending('spot'),[])
        self.q.receive(5,True,'/cancel '+token,now=221)
        self.assertEqual(self.q.pending('spot'),[])

    def test_restart_keeps_snapshot_and_deduplicates_update(self):
        self.q.receive(1,True,'/pair SOLUSDT',now=100,pairs=self.snap)
        token=self.proposal()['id'];self.q.close()
        self.q=UnifiedQueue(self.path,allow_pair_experiment=True)
        self.q.receive(1,True,'/pair SOLUSDT',now=101,pairs={'spot':{'symbols':['ETHUSDT'],'version':9}})
        self.q.receive(2,True,'/confirm '+token,now=102)
        payload=json.loads(self.q.pending('spot')[0]['payload'])
        self.assertEqual((payload['previous'],payload['version']),(['BTCUSDT'],3))

    def test_unknown_snapshot_invalid_symbol_or_stale_never_proposes(self):
        self.q.receive(1,True,'/pair SOLUSDT',now=100,pairs={})
        self.q.receive(2,True,'/pair SOL/USDT',now=101,pairs=self.snap)
        self.q.receive(3,True,'/pair SOLUSDT',now=102,pairs=self.snap,fresh=False)
        self.assertIsNone(self.proposal())
        self.assertEqual(self.q.pending('spot'),[])

    def test_two_confirmations_from_competing_connections_create_one_job(self):
        self.q.receive(1,True,'/pair SOLUSDT',now=100,pairs=self.snap)
        token=self.proposal()['id'];gate=threading.Barrier(2);errors=[]
        def worker(ident):
            q=UnifiedQueue(self.path,allow_pair_experiment=True)
            try:
                gate.wait();q.receive(ident,True,'/confirm '+token,now=101)
            except Exception as exc:errors.append(exc)
            finally:q.close()
        # Unique Telegram updates normally arrive in order; use the same update
        # identity here to exercise concurrent retry without violating offset order.
        threads=[threading.Thread(target=worker,args=(2,)) for _ in range(2)]
        for thread in threads:thread.start()
        for thread in threads:thread.join(10)
        self.assertFalse(any(t.is_alive() for t in threads));self.assertEqual(errors,[])
        self.assertEqual(len(self.q.pending('spot')),1)

    def test_third_asset_close_preserves_market_position_and_quantity(self):
        for offset,market in enumerate(('futures_cross','margin_cross')):
            ident=10+offset*10
            self.q.receive(ident,True,'/mode '+market,now=100)
            self.q.receive(ident+1,True,'/close SOLUSDT',now=100,
                           positions={market:{'SOLUSDT':{'id':'position-sol','quantity':'0.25'}}})
            token=self.q.add_proposal(ident+1,market,'position-sol','0.25',now=101)
            self.q.receive(ident+2,True,'/mode spot',now=102)
            self.q.receive(ident+3,True,'/confirm '+token,now=103)
            payload=json.loads(self.q.pending(market)[0]['payload'])
            self.assertEqual((payload['symbol'],payload['position'],payload['quantity']),('SOLUSDT','position-sol','0.25'))
            message=self.q.db.execute("SELECT message FROM unified_outbox WHERE message LIKE '%позицию position-sol%' ORDER BY id DESC").fetchone()[0]
            self.assertIn('SOL',message)

    def test_spot_close_displays_actual_asset(self):
        self.q.receive(1,True,'/close',now=100,
                       positions={'spot':{'id':'spot-sol','quantity':'0.25','symbol':'SOLUSDT'}})
        self.q.add_proposal(1,'spot','spot-sol','0.25',now=101)
        message=self.q.db.execute("SELECT message FROM unified_outbox WHERE message LIKE '%spot-sol%' ORDER BY id DESC").fetchone()[0]
        self.assertIn('SOLUSDT',message);self.assertIn(' SOL',message)

    def test_process_death_before_and_after_confirmation_commit(self):
        self.q.receive(1,True,'/pair SOLUSDT',now=100,pairs=self.snap)
        token=self.proposal()['id']
        script='''import os,sys\nfrom trading_bot.unified_queue import UnifiedQueue\nq=UnifiedQueue(sys.argv[1],allow_pair_experiment=True)\ndef fault(stage):\n if stage==sys.argv[3]:os._exit(73)\nq.fault=fault\nq.receive(2,True,"/confirm "+sys.argv[2],now=101)\n'''
        for stage in ('before_commit','after_commit'):
            child=subprocess.run([sys.executable,'-B','-c',script,str(self.path),token,stage],capture_output=True,timeout=20)
            self.assertEqual(child.returncode,73,child.stderr.decode())
            self.assertEqual(len(self.q.pending('spot')),0 if stage=='before_commit' else 1)
        self.q.receive(2,True,'/confirm '+token,now=102)
        self.assertEqual(len(self.q.pending('spot')),1)


if __name__=='__main__':unittest.main()

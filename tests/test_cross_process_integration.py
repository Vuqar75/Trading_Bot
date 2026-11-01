import ast
import json
import os
import signal
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from contextlib import closing
from decimal import Decimal
from pathlib import Path

from trading_bot.futures_store import FuturesStore
from trading_bot.cross_paper_book import CrossPaperBook
from trading_bot.paper_consolidation import consolidate_snapshots
from trading_bot.paper_shared_store import SharedPaperStore
from trading_bot.runtime_store import RuntimeStore
from trading_bot.unified_queue import UnifiedQueue


class ThreePaperProcessesTests(unittest.TestCase):
    def test_three_real_entrypoints_on_fake_data_and_separate_database(self):
        if os.name!='posix':self.skipTest('Process locking requires Linux')
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);spot=root/'spot.db';future=root/'future.db';shared_path=root/'shared.db';queue_path=root/'queue.db'
            identity={'schema':1,'mode':'paper','symbol':'BTCUSDT','interval':'15m','amount':'10','account':'paper'}
            s=RuntimeStore(spot,identity);f=FuturesStore(future)
            try:
                f.command(1,True,'/resume');f.open_position('SHORT',Decimal(100),1000,'test-open')
                old=f.get('position')
                with f.transaction():f.set('paused',True)
            finally:s.close();f.close()
            consolidate_snapshots(spot,future,shared_path,captured_at=time.time())
            q=UnifiedQueue(queue_path);self.addCleanup(q.close)
            source=Path(__file__).resolve().parents[1]/'tools/rehearse_shared_processes.py'
            tree=ast.parse(source.read_text(encoding='utf-8'))
            child=next(n.value.value for n in tree.body if isinstance(n,ast.Assign) and any(isinstance(x,ast.Name) and x.id=='child' for x in n.targets))
            cross_child='''import socket,sys,time
from unittest.mock import patch
import trading_bot.cross_app as app
class Fake:
 def schedule(self,symbol):
  now=int(time.time()*1000);return {'symbol':symbol,'mark':'100' if symbol=='BTCUSDT' else '200','server_ms':now,'next_due_ms':(now//28800000+1)*28800000,'interval_ms':28800000}
 def funding(self,*args):return []
 def candles(self,symbol,interval,server):
  period=900000;last=(server//period-1)*period
  return [(last-(39-i)*period,'100') for i in range(40)]
def deny(*args,**kwargs):raise AssertionError('network prohibited')
with patch.object(socket,'socket',deny),patch.object(app,'CrossPublicData',Fake):
 sys.argv=['cross-app','--db',sys.argv[1],'--queue',sys.argv[2],'--poll-seconds','5'];app.main()
'''
            processes=[];logs=[]
            def start(mode):
                log=open(root/(mode+'.log'),'a');logs.append(log)
                script=cross_child if mode=='cross' else child
                args=[sys.executable,'-B','-c',script]
                args+=([str(shared_path),str(queue_path)] if mode=='cross' else [mode,str(shared_path),str(queue_path)])
                proc=subprocess.Popen(args,cwd=Path(__file__).resolve().parents[1],stdout=log,stderr=log)
                processes.append(proc);return proc
            def wait(predicate,timeout=35):
                deadline=time.monotonic()+timeout
                while time.monotonic()<deadline:
                    if any(p.poll() is not None for p in processes):
                        raise AssertionError('Paper worker exited: '+'; '.join(p.name+': '+p.read_text(errors='replace')[-1500:] for p in root.glob('*.log')))
                    try:
                        if predicate():return
                    except (sqlite3.Error,KeyError,TypeError):pass
                    time.sleep(.2)
                raise AssertionError('Offline process rehearsal timed out')
            def query(sql,params=()):
                with closing(sqlite3.connect(shared_path)) as db:return db.execute(sql,params).fetchone()
            def job_done(uid,market):
                row=q.db.execute('SELECT status FROM unified_jobs WHERE update_id=? AND market=?',(uid,market)).fetchone()
                return row is not None and row[0]=='DONE'
            try:
                start('spot');start('futures');cross=start('cross')
                wait(lambda:all((root/name).exists() for name in ('runtime-health.json','futures-health.json','cross-health.json')))
                wait(lambda:query("SELECT count(*) FROM wallet_state WHERE wallet='futures_cross'")[0]==1)
                memory=Path('/proc/meminfo').read_text()
                available=int(next(line.split()[1] for line in memory.splitlines() if line.startswith('MemAvailable:')))
                rss=[]
                for proc in processes:
                    status=Path('/proc',str(proc.pid),'status').read_text()
                    rss.append(int(next(line.split()[1] for line in status.splitlines() if line.startswith('VmRSS:'))))
                print('OFFLINE_THREE_WORKERS_MEM_AVAILABLE_KIB',available,'RSS_KIB',rss,flush=True)
                self.assertGreater(available,100*1024)
                q.receive(1,True,'/transfer futures_isolated futures_cross 50')
                wait(lambda:job_done(1,'wallet'))
                token=query("SELECT id FROM transfer_proposals WHERE request_id='telegram:1'")[0]
                q.receive(2,True,'/confirm '+token);wait(lambda:job_done(2,'wallet'))
                state=SharedPaperStore(shared_path)
                try:
                    self.assertEqual(state.ledger.balance('futures_cross:free'),Decimal(50))
                    restored=state.snapshot('futures')['position']
                    for key in ('id','direction','quantity','margin','funding'):self.assertEqual(restored[key],old[key])
                    self.assertTrue(state.ledger.reconcile()['balanced'])
                finally:state.close()
                q.receive(3,True,'/mode futures_cross');q.receive(4,True,'/report');q.receive(5,True,'/balance_report today')
                wait(lambda:job_done(4,'futures_cross') and job_done(5,'wallet'))
                self.assertIn('Futures cross',q.db.execute('SELECT result FROM unified_jobs WHERE update_id=4').fetchone()[0])
                q.receive(6,True,'/resume');wait(lambda:job_done(6,'futures_cross'))
                wait(lambda:query("SELECT paused FROM wallet_state WHERE wallet='futures_cross'")[0]==0)
                positions=[]
                def prepare_position():
                    state=SharedPaperStore(shared_path)
                    try:
                        book=CrossPaperBook(state)
                        try:positions.append(book.open('offline-explicit','BTCUSDT','LONG','5',1,book.stored_quotes(),now=time.time()));return True
                        except ValueError as exc:
                            if 'Incomplete cross data' in str(exc) or 'Stale cross mark' in str(exc):return False
                            raise
                    finally:state.close()
                wait(prepare_position);position=positions[0]
                q.receive(7,True,'/close BTCUSDT',positions={'futures_cross':{'BTCUSDT':position}})
                wait(lambda:job_done(7,'futures_cross'))
                proposal=q.db.execute("SELECT id FROM unified_proposals WHERE market='futures_cross'").fetchone()[0]
                q.receive(8,True,'/confirm '+proposal);wait(lambda:job_done(8,'futures_cross'))
                self.assertEqual(query("SELECT count(*) FROM ledger_events WHERE kind='CROSS_CLOSE'")[0],1)
                q.receive(9,True,'/pause_all')
                wait(lambda:all(job_done(9,m) for m in ('spot','futures','futures_cross')))
                wait(lambda:query("SELECT paused FROM wallet_state WHERE wallet='futures_cross'")[0]==1)
                q.receive(10,True,'/resume');wait(lambda:job_done(10,'futures_cross'))
                wait(lambda:query("SELECT paused FROM wallet_state WHERE wallet='futures_cross'")[0]==0)
                cross.send_signal(signal.SIGTERM);cross.wait(timeout=20);processes.remove(cross)
                start('cross');wait(lambda:query("SELECT paused FROM wallet_state WHERE wallet='futures_cross'")[0]==1)
                self.assertEqual(query("SELECT count(*) FROM ledger_events WHERE kind='CROSS_OPEN'")[0],1)
            finally:
                for p in processes:
                    if p.poll() is None:p.send_signal(signal.SIGTERM)
                for p in processes:
                    try:p.wait(timeout=20)
                    except subprocess.TimeoutExpired:p.kill();p.wait()
                for log in logs:log.close()


if __name__=='__main__':unittest.main()

import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch
from trading_bot.app import Application
from trading_bot.futures_store import FuturesStore
from trading_bot.runtime_exchange import PaperExchange
from trading_bot.runtime_store import RuntimeStore
from trading_bot.unified_queue import UnifiedQueue
from tools import vm_install_shared_paper as cutover


class CutoverRehearsalTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.root=Path(self.tmp.name)
        home=self.root/'home';spotdir=home/'Trading_Bot_paper_state';futdir=home/'Trading_Bot_futures_state'
        (spotdir/'monitor').mkdir(parents=True);futdir.mkdir();self.home=home
        self.spot=spotdir/'runtime-paper.db';self.future=futdir/'futures-paper.db';self.queue=spotdir/'unified-commands.db';self.observer=spotdir/'monitor/observation.db'
        ident={'schema':1,'mode':'paper','symbol':'BTCUSDT','interval':'15m','amount':'10','account':'paper'}
        spot=RuntimeStore(self.spot,ident);fut=FuturesStore(self.future)
        try:
            Application(spot,PaperExchange(),None);spot.command(1,True,'/resume')
            fut.command(1,True,'/resume');fut.open_position('SHORT',Decimal(100),1000,'entry')
            now=time.time()
            with spot.transaction():spot.set('quote',{'price':'100','time':now})
            with fut.transaction():fut.set('quote',{'price':'100','time':now})
            self.position=fut.get('position')
        finally:spot.close();fut.close()
        q=UnifiedQueue(self.queue);q.close()
        with sqlite3.connect(self.observer) as db:db.execute('create table samples(ts real primary key)')
        self.target=home/'paper-financial/shared-paper.db';self.units=self.root/'units';self.units.mkdir()
        self.code=self.root/'candidate';(self.code/'deploy').mkdir(parents=True)
        repo=Path(__file__).resolve().parents[1]
        for target,source in cutover.MAP.items():shutil.copy2(repo/'deploy'/source,self.code/'deploy'/source)
        for unit in cutover.UNIT_FILES:(self.units/unit).write_text('old unit '+unit)
        self.states={u:True for u in cutover.UNITS};self.new=False;self.fail_new=False;self.advance=False;self.held=None;self.inject=None

    def tearDown(self):
        if self.held:self.held.close()

    def _fake_output(self,args,**kwargs):
        if args==['hostname']:return 'tradingbot-paper-test\n'
        if args[0]=='id':return str(os.getuid())+'\n'
        if args[0]=='git':return 'candidate-sha\n' if 'rev-parse' in args else ''
        raise AssertionError(args)

    def _counts(self):
        return {'spot':int(self.states['tradingbot-spot.service']),'futures':int(self.states['tradingbot-futures.service']),'monitor':int(self.states['tradingbot-monitor.service'])}

    def _health(self,path,mode):
        target=self.target if self.new else (self.spot if mode=='paper' else self.future)
        table='runtime_meta' if mode=='paper' else 'futures_meta'
        with sqlite3.connect(target) as db:m={k:json.loads(v) for k,v in db.execute('select key,value from '+table)}
        return {'mode':mode,'paused':m['paused'],'quote':m['quote'],'error':m.get('error'),'blocked':m.get('blocked'),'telegram_error':m.get('telegram_error')}

    def _call(self,*args):
        if args[0]=='systemd-analyze':return
        if args[:2]==('systemctl','daemon-reload'):return
        if args[:2]==('systemctl','stop'):
            for u in args[2:]:
                if u in self.states:self.states[u]=False
            return
        if args[:2]==('systemctl','start'):
            unit=args[2]
            if unit=='tradingbot-paper-backup.service':return
            is_new='shared financial journal' in (self.units/'tradingbot-futures.service').read_text()
            if is_new and self.fail_new and unit=='tradingbot-spot.service':raise RuntimeError('injected startup failure')
            self.states[unit]=True
            if is_new:
                self.new=True
                if self.advance and unit=='tradingbot-futures.service':
                    f=FuturesStore(self.target,shared=True)
                    try:p=f.get('position');f.close_position(Decimal(p['entry']),p['funding_cursor'],'protective','STOP',p['id'])
                    finally:f.close()
                if unit=='tradingbot-spot.service':
                    import fcntl
                    self.held=open(str(self.target)+'.delivery.lock','a');fcntl.flock(self.held,fcntl.LOCK_EX|fcntl.LOCK_NB)
            return
        if args[:2]==('systemctl','kill'):
            unit=args[-1];table='runtime_meta' if 'spot' in unit else 'futures_meta'
            with sqlite3.connect(self.target) as db:db.execute('update '+table+" set value='false' where key='paused'")
            return
        raise AssertionError(args)

    def _run(self,fail_new=False,advance=False):
        self.fail_new=fail_new;self.advance=advance
        def fake_run(args,**kwargs):
            if args[:2]==['systemctl','stop']:
                for u in args[2:]:
                    if u in self.states:self.states[u]=False
                return subprocess.CompletedProcess(args,0)
            raise AssertionError(args)
        patches=[patch.object(cutover,'HOME',self.home),patch.object(cutover,'SPOT',self.spot),patch.object(cutover,'FUTURE',self.future),
                 patch.object(cutover,'QUEUE',self.queue),patch.object(cutover,'OBSERVER',self.observer),patch.object(cutover,'TARGET',self.target),
                 patch.object(cutover,'CODE',self.code),patch.object(cutover,'UNIT_DIR',self.units),patch.object(cutover,'active',lambda u:self.states[u]),
                 patch.object(cutover,'process_counts',self._counts),patch.object(cutover,'health',self._health),patch.object(cutover,'call',self._call),
                 patch.object(cutover,'test_notification',lambda _:self.inject() if self.inject else None),patch.object(cutover.os,'geteuid',lambda:0),patch.object(cutover.os,'chown',lambda *a:None),
                 patch.object(cutover.time,'sleep',lambda _:None),patch.object(cutover.subprocess,'check_output',self._fake_output),patch.object(cutover.subprocess,'run',fake_run),
                 patch.object(cutover.sys,'argv',['cutover','--expected-commit','candidate-sha','--execute'])]
        from contextlib import ExitStack
        with ExitStack() as stack:
            for p in patches:stack.enter_context(p)
            cutover.main()

    def test_rehearsed_cutover_preserves_position_and_restores_entries(self):
        self._run()
        from trading_bot.paper_shared_store import SharedPaperStore
        s=SharedPaperStore(self.target)
        try:
            self.assertEqual(s.snapshot('futures')['position'],self.position)
            self.assertFalse(s.snapshot('spot')['paused']);self.assertFalse(s.snapshot('futures')['paused'])
            self.assertFalse(s.deployment_hold());self.assertTrue(s.ledger.reconcile()['balanced'])
        finally:s.close()
        archives=list(self.home.glob('before-wallet-*'));self.assertEqual(len(archives),1)
        self.assertEqual(json.loads((archives[0]/'manifest.json').read_text())['stage'],'complete')

    def test_manual_pause_received_during_cutover_is_preserved(self):
        def inject():
            q=UnifiedQueue(self.queue)
            try:
                q.receive(10,True,'/pause')
                q.acknowledge(q.pending('spot')[0]['id'],'paused')
            finally:q.close()
        self.inject=inject;self._run()
        from trading_bot.paper_shared_store import SharedPaperStore
        s=SharedPaperStore(self.target)
        try:
            self.assertTrue(s.snapshot('spot')['paused'])
            self.assertFalse(s.snapshot('futures')['paused'])
        finally:s.close()

    def test_release_process_death_before_and_after_commit(self):
        from trading_bot.paper_consolidation import consolidate_snapshots
        from trading_bot.paper_shared_store import SharedPaperStore
        for path,table in ((self.spot,'runtime_meta'),(self.future,'futures_meta')):
            with sqlite3.connect(path) as db:db.execute('update '+table+" set value='true' where key='paused'")
        self.target.parent.mkdir()
        consolidate_snapshots(self.spot,self.future,self.target,captured_at=time.time())
        s=SharedPaperStore(self.target);s.set_deployment_hold(True);s.close()
        code=("import os,sys; from trading_bot.paper_shared_store import SharedPaperStore; "
              "from tools.vm_install_shared_paper import release_entries; "
              "s=SharedPaperStore(sys.argv[1]); s.fault=lambda p: os._exit(71) if p==sys.argv[3] else None; "
              "release_entries(s,sys.argv[2],0,{'spot':False,'futures':False})")
        for point in ('before_commit','after_commit'):
            proc=subprocess.run([sys.executable,'-c',code,str(self.target),str(self.queue),point])
            self.assertEqual(proc.returncode,71)
            s=SharedPaperStore(self.target)
            try:
                self.assertEqual(s.deployment_hold(),point=='before_commit')
                self.assertEqual(s.snapshot('spot')['paused'],point=='before_commit')
                self.assertEqual(s.snapshot('futures')['paused'],point=='before_commit')
                self.assertTrue(s.ledger.reconcile()['balanced'])
            finally:s.close()

    def test_failed_start_rolls_back_old_services_paused_when_safe(self):
        with self.assertRaises(RuntimeError):self._run(fail_new=True)
        archive=next(self.home.glob('before-wallet-*'))
        self.assertEqual(json.loads((archive/'manifest.json').read_text())['stage'],'rolled_back_paused')
        self.assertTrue((self.units/'tradingbot-spot.service').read_text().startswith('old unit'))
        for path,table in ((self.spot,'runtime_meta'),(self.future,'futures_meta')):
            with sqlite3.connect(path) as db:self.assertTrue(json.loads(db.execute('select value from '+table+" where key='paused'").fetchone()[0]))

    def test_financial_change_refuses_destructive_rollback(self):
        with self.assertRaises(RuntimeError):self._run(fail_new=True,advance=True)
        archive=next(self.home.glob('before-wallet-*'))
        self.assertEqual(json.loads((archive/'manifest.json').read_text())['stage'],'manual_reconciliation_required')
        self.assertTrue(self.target.exists())
        self.assertTrue((self.units/'tradingbot-spot.service').read_text().startswith('[Unit]'))

    def test_process_death_at_each_persisted_cutover_stage_preserves_copies(self):
        stages={'preflight':'preflight','spot_pause_committed':'preflight',
                'spot_paused':'spot_paused','futures_pause_committed':'spot_paused',
                'futures_paused':'futures_paused','entries_paused':'entries_paused',
                'source_backups_complete':'source_backups_complete',
                'consolidated_verified':'consolidated_verified',
                'source_archived:runtime-paper.db':'consolidated_verified',
                'unit_replaced:tradingbot-spot.service':'consolidated_verified',
                'old_writers_archived_and_units_installed':'old_writers_archived_and_units_installed',
                'new_workers_started_on_hold':'new_workers_started_on_hold',
                'healthy_on_hold_and_backup_complete':'healthy_on_hold_and_backup_complete',
                'release_entries_committed':'healthy_on_hold_and_backup_complete','complete':'complete'}
        code=("import os,sys; sys.path.insert(0,'tests'); "
              "from test_vm_shared_cutover import CutoverRehearsalTests; "
              "from tools import vm_install_shared_paper as c; "
              "case=CutoverRehearsalTests(); case.setUp(); print('ROOT',case.root,flush=True); "
              "target=sys.argv[1]; c.STAGE_HOOK=lambda stage: os._exit(77) if stage==target else None; "
              "case._run()")
        for stage,manifest_stage in stages.items():
            with self.subTest(stage=stage):
                proc=subprocess.run([sys.executable,'-c',code,stage],capture_output=True,text=True,timeout=30)
                self.assertEqual(proc.returncode,77,proc.stderr)
                root=Path(next(x[5:] for x in proc.stdout.splitlines() if x.startswith('ROOT ')))
                try:
                    archive=next((root/'home').glob('before-wallet-*'))
                    self.assertEqual(json.loads((archive/'manifest.json').read_text())['stage'],manifest_stage)
                    if stage not in ('complete',):
                        with self.assertRaises(RuntimeError):cutover.require_no_interrupted_cutover(root/'home')
                    for name in ('Trading_Bot_paper_state/runtime-paper.db','Trading_Bot_futures_state/futures-paper.db'):
                        with sqlite3.connect(root/'home'/name) as db:
                            self.assertEqual(db.execute('PRAGMA integrity_check').fetchone()[0],'ok')
                    shared=root/'home/paper-financial/shared-paper.db'
                    if shared.exists():
                        from trading_bot.paper_shared_store import SharedPaperStore
                        store=SharedPaperStore(shared)
                        try:
                            self.assertTrue(store.ledger.reconcile()['balanced'])
                            self.assertEqual(store.snapshot('futures')['position']['direction'],'SHORT')
                            self.assertEqual(store.deployment_hold(),stage not in ('release_entries_committed','complete'))
                        finally:store.close()
                        self.assertTrue((archive/'spot.db').exists())
                        self.assertTrue((archive/'futures.db').exists())
                finally:shutil.rmtree(root)

    def test_interrupted_or_missing_manifest_blocks_reexecution(self):
        archive=self.home/'before-wallet-interrupted';archive.mkdir()
        for payload in (None, {'stage':'consolidated_verified'}, {'stage':'manual_reconciliation_required'}):
            if payload is not None:(archive/'manifest.json').write_text(json.dumps(payload))
            with self.assertRaisesRegex(RuntimeError,'cutover|archive'):self._run()
            self.assertFalse(self.target.exists())
            self.assertTrue(all(self.states.values()))

    def test_concurrent_manual_pause_aborts_and_leaves_both_markets_paused(self):
        original=cutover.read_meta
        def raced(path,table):
            result=original(path,table)
            if table=='futures_meta':
                with sqlite3.connect(self.spot) as db:db.execute("update runtime_meta set value='true' where key='paused'")
            return result
        with patch.object(cutover,'read_meta',raced):
            with self.assertRaises(RuntimeError):self._run()
        for path,table in ((self.spot,'runtime_meta'),(self.future,'futures_meta')):
            with sqlite3.connect(path) as db:self.assertTrue(json.loads(db.execute('select value from '+table+" where key='paused'").fetchone()[0]))
        self.assertFalse(self.target.exists())

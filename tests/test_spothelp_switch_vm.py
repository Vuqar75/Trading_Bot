import importlib.util
import json
import sqlite3
import subprocess
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

from trading_bot.cross_paper_book import CrossPaperBook
from trading_bot.futures_store import FuturesStore
from trading_bot.margin_paper_book import MarginPaperBook
from trading_bot.paper_consolidation import consolidate_snapshots
from trading_bot.paper_shared_store import SharedPaperStore
from trading_bot.runtime_store import RuntimeStore
from trading_bot.unified_queue import UnifiedQueue

spec=importlib.util.spec_from_file_location('spothelp_switch_test_module',Path(__file__).resolve().parents[1]/'tools/spothelp_switch_vm.py')
switcher=importlib.util.module_from_spec(spec);spec.loader.exec_module(switcher)


class SpothelpSwitchVmTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);root=Path(self.tmp.name)
        self.root=root;self.financial=root/'financial.db';self.queue=root/'queue.db';self.observer=root/'observation.db'
        spot=RuntimeStore(root/'spot.db',{'mode':'paper','interval':'15m'});future=FuturesStore(root/'future.db')
        import time
        now=time.time();stamp=int(now*1000)
        future.command(1,True,'/resume');future.open_position('SHORT',Decimal(100),stamp,'existing');future.command(2,True,'/pause')
        spot.close();future.close();consolidate_snapshots(root/'spot.db',root/'future.db',self.financial,captured_at=now)
        shared=SharedPaperStore(self.financial)
        try:
            with shared.transaction():
                shared._set('spot','quote',{'price':'100','time':now});shared._set('futures','quote',{'price':'100','time':now,'server_ms':stamp})
            cross=CrossPaperBook(shared)
            quotes={s:{'mark':'100','time':now,'funding_complete':True,'server_ms':stamp,'next_due_ms':(stamp//28800000+1)*28800000,'interval_ms':28800000} for s in ('BTCUSDT','ETHUSDT')}
            cross.record_quotes(quotes,now=now)
            margin=MarginPaperBook(shared.ledger)
            filt={'lot_rules':[{'min':'0','max':'0','step':'.0001'}],'min_notional':'0','max_notional':'0','observed_ms':stamp}
            mq={k:{'price':'100','time_ms':stamp,'server_ms':stamp,'source':'fixture','price_kind':'last_public_trade','filters':filt} for k in ('BTCUSDT','ETHUSDT')}
            margin.record_quotes(mq,now_ms=stamp)
        finally:shared.close()
        q=UnifiedQueue(self.queue);q.close()
        db=sqlite3.connect(self.observer);db.execute('CREATE TABLE samples(id INTEGER PRIMARY KEY)');db.commit();db.close()
        self.unitdir=root/'units';self.unitdir.mkdir();self.archive=root/'archive';self.archive.mkdir();self.stages=self.archive/'candidate-units';self.stages.mkdir()
        self.target=root/'target';self.target.mkdir();self.marker=root/'interlock'
        self.candidate='a'*40
        (self.archive/'prepared.json').write_text(json.dumps({'candidate':self.candidate,'target':str(self.target)}))
        for name in switcher.ALL_UNITS:
            (self.unitdir/name).write_text('WorkingDirectory=/opt/trading-bot-finalaudit-29d1fc6\n')
            (self.stages/name).write_text('WorkingDirectory='+str(self.target)+'\n')
        self.states={n:'active' for n in ('tradingbot-spot.service','tradingbot-futures.service','tradingbot-cross.service','tradingbot-margin.service','tradingbot-monitor.service',switcher.TIMER)};self.states[switcher.BACKUP]='inactive'
        self.states.update({n:'active' for n in switcher.AUX_TIMERS});self.states.update({n:'inactive' for n in switcher.AUX_SERVICES})
        self.start_failure=None;self.stop_failure=False;self.aux_stop_failure=False;self.simulate_new_event=False
        self.commands=[]

    def execute(self,*,inject_switch_failure=False,fault_unit='tradingbot-spot.service'):
        import trading_bot.cross_unit_switch as units
        original_switch=units.switch_unit_files
        def fail_switch(*args,**kwargs):
            def fault(where):
                if where=='switched:'+fault_unit:
                    if self.simulate_new_event:
                        shared=SharedPaperStore(self.financial)
                        try:
                            with shared.ledger.transaction():
                                shared.ledger._post('fixture:new-event','FIXTURE',{},[('reserve:free','USDT',Decimal(1)),('external:fixture','USDT',Decimal(-1))])
                        finally:shared.close()
                    raise RuntimeError('simulated switch failure')
            return original_switch(*args,**dict(kwargs,fault=fault))
        def fake_command(*args,**kwargs):
            self.commands.append(args)
            action=args[0]
            if action=='stop':
                for index,name in enumerate(args[1:]):
                    self.states[name]='inactive'
                    if self.aux_stop_failure and index==0 and args[1]==switcher.WATCH_TIMER:self.aux_stop_failure=False;raise subprocess.CalledProcessError(1,args)
                    if self.stop_failure and index==0 and args[1]=='tradingbot-spot.service':self.stop_failure=False;raise subprocess.CalledProcessError(1,args)
            if action=='start':
                if args[1]==self.start_failure:self.start_failure=None;raise subprocess.CalledProcessError(1,args)
                self.states[args[1]]='active'
                if args[1]=='tradingbot-futures.service' and self.simulate_new_event:
                    self.simulate_new_event=False;shared=SharedPaperStore(self.financial)
                    try:
                        with shared.ledger.transaction():shared.ledger._post('fixture:new-event','FIXTURE',{},[('reserve:free','USDT',Decimal(1)),('external:fixture','USDT',Decimal(-1))])
                    finally:shared.close()
            return ''
        def output(*args,**kwargs):
            argv=args[0]
            if argv==['hostname']:return 'tradingbot-paper-test\n'
            if 'rev-parse' in argv:return self.candidate+'\n'
            if 'status' in argv:return ''
            raise AssertionError(argv)
        def counts():
            names={'spot':'tradingbot-spot.service','futures':'tradingbot-futures.service','cross':'tradingbot-cross.service',
                   'margin':'tradingbot-margin.service','monitor':'tradingbot-monitor.service'}
            return {key:int(self.states[unit]=='active') for key,unit in names.items()}
        def unit_property(unit,key):
            return {'DropInPaths':str(self.unitdir/(unit+'.d')/'91-paper-spothelp-interlock.conf'),
                    'FragmentPath':str(self.unitdir/unit),'NeedDaemonReload':'no','MainPID':'123'}[key]
        patches=[patch.object(switcher,'ARCHIVE',self.archive),
                 patch.object(switcher,'FINANCIAL',self.financial),patch.object(switcher,'QUEUE',self.queue),
                 patch.object(switcher,'OBSERVER',self.observer),patch.object(switcher,'UNITDIR',self.unitdir),
                 patch.object(switcher,'MARKER',self.marker),patch.object(switcher,'active',side_effect=lambda u:self.states[u]),
                 patch.object(switcher,'property_value',side_effect=unit_property),
                 patch.object(switcher,'process_counts',side_effect=counts),
                 patch.object(switcher,'command',side_effect=fake_command),patch('os.geteuid',return_value=0),
                 patch.object(switcher.subprocess,'check_output',side_effect=output),patch.object(switcher.signal,'signal'),
                 patch.object(units,'switch_unit_files',side_effect=fail_switch if inject_switch_failure else original_switch)]
        from contextlib import ExitStack, nullcontext
        import trading_bot.spot_only_update_lock as spotlock
        patches.append(patch.object(spotlock,'hold_stopped_spot_locks',side_effect=lambda p:nullcontext()))
        with ExitStack() as stack:
            for p in patches:stack.enter_context(p)
            switcher.main()

    def test_only_spot_stops_and_restarts_on_hold(self):
        self.execute()
        for name in ('tradingbot-spot.service','tradingbot-futures.service','tradingbot-cross.service','tradingbot-margin.service','tradingbot-monitor.service'):
            self.assertEqual(self.states[name],'active')
        self.assertEqual((self.unitdir/'tradingbot-spot.service').read_text(),'WorkingDirectory='+str(self.target)+'\n')
        for command in self.commands:
            if command[0] in ('start','stop','restart'):
                self.assertEqual(command[1:],('tradingbot-spot.service',))
        shared=SharedPaperStore(self.financial)
        try:
            self.assertTrue(shared.deployment_hold())
            self.assertTrue(shared.snapshot('spot')['paused'])
        finally:shared.close()

    def test_switch_failure_restores_spot_and_does_not_stop_other_workers(self):
        with self.assertRaisesRegex(RuntimeError,'simulated switch failure'):
            self.execute(inject_switch_failure=True)
        for name in ('tradingbot-futures.service','tradingbot-cross.service','tradingbot-margin.service','tradingbot-monitor.service'):
            self.assertEqual(self.states[name],'active')
        self.assertEqual((self.unitdir/'tradingbot-spot.service').read_text(),'WorkingDirectory=/opt/trading-bot-finalaudit-29d1fc6\n')
        self.assertFalse(self.marker.exists())
        for command in self.commands:
            if command[0] in ('start','stop','restart'):
                self.assertEqual(command[1:],('tradingbot-spot.service',))

    def test_new_event_is_never_replaced_by_old_backup(self):
        self.simulate_new_event=True
        with self.assertRaisesRegex(RuntimeError,'simulated switch failure'):
            self.execute(inject_switch_failure=True)
        shared=SharedPaperStore(self.financial)
        try:
            self.assertTrue(shared.db.execute("SELECT 1 FROM ledger_events WHERE id='fixture:new-event'").fetchone())
            self.assertTrue(shared.deployment_hold())
        finally:shared.close()
        for command in self.commands:
            if command[0] in ('start','stop','restart'):
                self.assertEqual(command[1:],('tradingbot-spot.service',))

if __name__=='__main__':unittest.main()

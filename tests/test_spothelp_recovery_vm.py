import importlib.util,json,subprocess,sys,tempfile,unittest
from contextlib import ExitStack,nullcontext
from pathlib import Path
from unittest.mock import patch
from trading_bot.spot_only_interlock import arm_interlock
from trading_bot.cross_unit_switch import switch_unit_files

spec=importlib.util.spec_from_file_location('spothelp_recovery_test_module',Path(__file__).resolve().parents[1]/'tools/spothelp_recovery_vm.py')
recovery=importlib.util.module_from_spec(spec);spec.loader.exec_module(recovery)

class SpothelpRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);root=Path(self.tmp.name)
        self.archive=root/'archive';self.archive.mkdir();self.stages=self.archive/'candidate-units';self.stages.mkdir()
        self.target=root/'target';self.target.mkdir();self.units=root/'units';self.units.mkdir();self.marker=root/'interlock'
        self.financial=root/'financial.db';self.financial.touch()
        (self.archive/'prepared.json').write_text(json.dumps({'target':str(self.target),'candidate':'a'*40}))
        for name in recovery.ALL_UNITS:
            (self.units/name).write_text('WorkingDirectory='+recovery.OLD+'\n')
            (self.stages/name).write_text('WorkingDirectory='+str(self.target)+'\n')
        self.started=[];self.counts={'spot':0,'futures':1,'cross':1,'margin':1,'monitor':1}
    def arm(self):
        arm_interlock(self.units,self.marker,recovery.SERVICES,recovery.INTERLOCK,reload_units=lambda:True)
    def partial(self):
        self.arm();archive=self.archive/'unit-archive';archive.mkdir();(self.archive/'stopped').mkdir();(self.archive/'stopped/financial.db').touch()
        def crash(point):
            if point=='switched:tradingbot-spot.service':raise RuntimeError('crash')
        with self.assertRaises(RuntimeError):switch_unit_files(self.units,self.stages,archive,recovery.ALL_UNITS,assert_safe=lambda:True,fault=crash)
    def run_recovery(self,held=True):
        import trading_bot.spot_only_update_lock as units
        def output(args,**kw):return 'tradingbot-paper-test\n' if args==['hostname'] else 'a'*40+'\n'
        patches=[patch.object(recovery,'ARCHIVE',self.archive),patch.object(recovery,'UNITDIR',self.units),patch.object(recovery,'MARKER',self.marker),
                 patch.object(recovery,'FINANCIAL',self.financial),patch.object(recovery,'candidate_allowed',return_value=True),
                 patch.object(recovery,'on_hold',return_value=held),patch.object(recovery,'counts',side_effect=lambda:self.counts),
                 patch.object(recovery,'command',side_effect=lambda *x:self.started.append(x)),patch.object(recovery.subprocess,'check_output',side_effect=output),
                 patch('os.geteuid',return_value=0),patch.object(units,'hold_stopped_spot_locks',side_effect=lambda p:nullcontext())]
        with ExitStack() as stack:
            for p in patches:stack.enter_context(p)
            recovery.recover()
    def test_old_spot_recovers_without_restarting_other_workers(self):
        self.arm();self.run_recovery()
        self.assertFalse(self.marker.exists())
        self.assertEqual([x[1] for x in self.started if x[0]=='start'],['tradingbot-spot.service'])

    def test_crash_after_spot_file_switch_preserves_new_financial_event(self):
        self.partial()
        self.financial.write_bytes(b'new durable event')
        self.run_recovery()
        self.assertEqual(self.financial.read_bytes(),b'new durable event')
        self.assertEqual([x[1] for x in self.started if x[0]=='start'],['tradingbot-spot.service'])
        self.assertFalse(self.marker.exists())

    def test_interlocked_candidate_with_live_spot_is_refused(self):
        self.partial();self.counts['spot']=1
        with self.assertRaisesRegex(ValueError,'active workers'):
            self.run_recovery()
        self.assertTrue(self.marker.exists());self.assertFalse(self.started)

if __name__=='__main__':unittest.main()

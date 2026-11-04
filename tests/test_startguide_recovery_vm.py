import importlib.util,json,subprocess,sys,tempfile,unittest
from contextlib import ExitStack,nullcontext
from pathlib import Path
from unittest.mock import patch
from trading_bot.cross_service_interlock import arm_interlock
from trading_bot.cross_unit_switch import switch_unit_files

spec=importlib.util.spec_from_file_location('startguide_recovery_test_module',Path(__file__).resolve().parents[1]/'tools/startguide_recovery_vm.py')
recovery=importlib.util.module_from_spec(spec);spec.loader.exec_module(recovery)

class StartguideRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);root=Path(self.tmp.name)
        self.archive=root/'archive';self.archive.mkdir();self.stages=self.archive/'candidate-units';self.stages.mkdir()
        self.target=root/'target';self.target.mkdir();self.units=root/'units';self.units.mkdir();self.marker=root/'interlock'
        self.financial=root/'financial.db';self.financial.touch()
        (self.archive/'prepared.json').write_text(json.dumps({'target':str(self.target),'candidate':'a'*40}))
        for name in recovery.ALL_UNITS:
            (self.units/name).write_text('WorkingDirectory='+recovery.OLD+'\n')
            (self.stages/name).write_text('WorkingDirectory='+str(self.target)+'\n')
        self.started=[];self.counts={k:0 for k in ('spot','futures','cross','margin','monitor')}
    def arm(self):
        arm_interlock(self.units,self.marker,recovery.SERVICES,recovery.INTERLOCK,reload_units=lambda:True)
    def partial(self):
        self.arm();archive=self.archive/'unit-archive';archive.mkdir();(self.archive/'stopped').mkdir();(self.archive/'stopped/financial.db').touch()
        def crash(point):
            if point=='switched:tradingbot-spot.service':raise RuntimeError('crash')
        with self.assertRaises(RuntimeError):switch_unit_files(self.units,self.stages,archive,recovery.ALL_UNITS,assert_safe=lambda:True,fault=crash)
    def run_recovery(self,held=True):
        import trading_bot.cross_unit_switch as units
        def output(args,**kw):return 'tradingbot-paper-test\n' if args==['hostname'] else 'a'*40+'\n'
        patches=[patch.object(recovery,'ARCHIVE',self.archive),patch.object(recovery,'UNITDIR',self.units),patch.object(recovery,'MARKER',self.marker),
                 patch.object(recovery,'FINANCIAL',self.financial),patch.object(recovery,'candidate_allowed',return_value=True),
                 patch.object(recovery,'on_hold',return_value=held),patch.object(recovery,'counts',side_effect=lambda:self.counts),
                 patch.object(recovery,'command',side_effect=lambda *x:self.started.append(x)),patch.object(recovery.subprocess,'check_output',side_effect=output),
                 patch('os.geteuid',return_value=0),patch.object(units,'hold_writer_locks',side_effect=lambda p:nullcontext())]
        with ExitStack() as stack:
            for p in patches:stack.enter_context(p)
            recovery.recover()
    def test_old_units_are_restarted_only_after_interlock_released(self):
        self.arm();self.run_recovery()
        self.assertFalse(self.marker.exists());self.assertEqual([x[1] for x in self.started if x[0]=='start'],list(recovery.START))
        self.assertTrue(all((self.units/n).read_text()=='WorkingDirectory='+recovery.OLD+'\n' for n in recovery.ALL_UNITS))
    def test_partial_switch_completes_candidate_without_restoring_database(self):
        self.partial();self.run_recovery()
        self.assertFalse(self.marker.exists());self.assertTrue(all((self.units/n).read_text()=='WorkingDirectory='+str(self.target)+'\n' for n in recovery.ALL_UNITS))
        self.assertEqual(self.financial.read_bytes(),b'')
    def test_process_death_during_unit_switch_is_resumed_on_hold(self):
        self.arm();archive=self.archive/'unit-archive';archive.mkdir();(self.archive/'stopped').mkdir();(self.archive/'stopped/financial.db').touch()
        script=r"""import os,sys
from trading_bot.cross_unit_switch import switch_unit_files
units=sys.argv[4:]
def crash(point):
 if point=='switched:tradingbot-spot.service':os._exit(74)
switch_unit_files(sys.argv[1],sys.argv[2],sys.argv[3],units,assert_safe=lambda:True,fault=crash)
"""
        child=subprocess.run([sys.executable,'-B','-c',script,str(self.units),str(self.stages),str(archive),*recovery.ALL_UNITS],capture_output=True,timeout=20)
        self.assertEqual(child.returncode,74,child.stderr.decode())
        self.run_recovery()
        self.assertFalse(self.marker.exists());self.assertTrue(all((self.units/n).read_text()=='WorkingDirectory='+str(self.target)+'\n' for n in recovery.ALL_UNITS))
        self.assertEqual(self.financial.read_bytes(),b'')
    def test_candidate_workers_and_new_events_are_never_rolled_back(self):
        for name in recovery.ALL_UNITS:(self.units/name).write_bytes((self.stages/name).read_bytes())
        self.financial.write_bytes(b'new durable event')
        self.counts['spot']=1
        self.run_recovery()
        self.assertEqual(self.financial.read_bytes(),b'new durable event')
        self.assertTrue(all((self.units/n).read_text()=='WorkingDirectory='+str(self.target)+'\n' for n in recovery.ALL_UNITS))

    def test_partial_switch_with_live_worker_is_refused(self):
        self.partial();self.counts['spot']=1
        with self.assertRaisesRegex(ValueError,'active workers'):self.run_recovery()
        self.assertTrue(self.marker.exists());self.assertFalse(self.started)
    def test_interlocked_candidate_with_active_worker_is_refused(self):
        self.arm()
        for name in recovery.ALL_UNITS:(self.units/name).write_bytes((self.stages/name).read_bytes())
        self.counts['spot']=1
        with self.assertRaisesRegex(ValueError,'active workers'):self.run_recovery()
        self.assertTrue(self.marker.exists());self.assertFalse(self.started)

    def test_unpaused_before_freeze_only_restores_timers(self):
        self.run_recovery(held=False)
        self.assertEqual([x[1] for x in self.started],list(recovery.TIMERS))
    def test_unknown_unit_is_refused(self):
        (self.units/recovery.SERVICES[0]).write_text('unexpected')
        with self.assertRaisesRegex(ValueError,'Unexpected unit content'):self.run_recovery()
        self.assertFalse(self.started)

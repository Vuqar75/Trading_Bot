import tempfile
import unittest
from pathlib import Path

from trading_bot.cross_unit_switch import switch_unit_files,restore_unit_files
from trading_bot.pair_rollout_plan import MODULES,TRADING,stage_units


class PairRolloutPlanTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.root=Path(self.tmp.name)
        self.units=self.root/'units';self.units.mkdir();self.stage=self.root/'stage';self.archive=self.root/'archive';self.archive.mkdir()
        self.old=Path('/opt/fixture-old');self.new=Path('/opt/fixture-candidate')
        extras={'tradingbot-spot.service':'Environment=SHARED_PAPER_DB=x\nEnvironment=UNIFIED_PAPER_QUEUE=x\nEnvironment=FUTURES_OUTBOX_DB=x\n',
                'tradingbot-futures.service':'Environment=UNIFIED_PAPER_QUEUE=x\n'}
        for name,module in MODULES.items():
            args=(' --shared-paper' if name=='tradingbot-futures.service' else
                  ' --db /tmp/financial --queue /tmp/queue' if name in ('tradingbot-cross.service','tradingbot-margin.service') else '')
            (self.units/name).write_text('[Service]\nWorkingDirectory='+str(self.old)+'\n'+extras.get(name,'')+
                                         'ExecStart=/usr/bin/python3 -B '+module+args+'\n')

    def test_stage_and_partial_switch_restore_without_financial_changes(self):
        original={n:(self.units/n).read_bytes() for n in MODULES}
        manifest=stage_units(self.units,self.stage,installed_root=self.old,candidate_root=self.new)
        self.assertEqual(set(manifest),set(MODULES))
        for name in MODULES:
            staged=(self.stage/name).read_text()
            self.assertIn('WorkingDirectory='+str(self.new),staged)
            self.assertEqual('--experimental-pair' in staged,name in TRADING)
            self.assertEqual((self.units/name).read_bytes(),original[name])
        def crash(stage):
            if stage=='switched:tradingbot-spot.service':raise RuntimeError('fixture switch failure')
        with self.assertRaises(RuntimeError):
            switch_unit_files(self.units,self.stage,self.archive,MODULES,assert_safe=lambda:True,fault=crash)
        with self.assertRaisesRegex(ValueError,'Rollback safety not proven'):
            restore_unit_files(self.units,self.archive,MODULES,assert_safe=lambda:True,verify_rollback=lambda:False)
        restore_unit_files(self.units,self.archive,MODULES,assert_safe=lambda:True,verify_rollback=lambda:True)
        self.assertEqual({n:(self.units/n).read_bytes() for n in MODULES},original)

    def test_extra_poller_and_unsafe_stage_are_rejected(self):
        path=self.units/'tradingbot-futures.service';path.write_text(path.read_text().replace('--shared-paper','--shared-paper --telegram'))
        with self.assertRaisesRegex(ValueError,'polling'):stage_units(self.units,self.stage,installed_root=self.old,candidate_root=self.new)
        self.assertFalse(self.stage.exists())
        with self.assertRaises(ValueError):stage_units(self.units,self.units/'inside',installed_root=self.old,candidate_root=self.new)

    def test_distinct_backup_root_requires_verified_per_unit_mapping(self):
        name='tradingbot-paper-backup.service';path=self.units/name
        path.write_text(path.read_text().replace(str(self.old),'/opt/fixture-backup'))
        with self.assertRaisesRegex(ValueError,'structure'):
            stage_units(self.units,self.stage,installed_root=self.old,candidate_root=self.new)
        self.assertFalse(self.stage.exists())
        for invalid in ({'unknown.service':'/opt/fixture-backup'},{name:'relative'},{name:str(self.new)}):
            with self.assertRaisesRegex(ValueError,'roots'):
                stage_units(self.units,self.stage,installed_root=self.old,candidate_root=self.new,installed_roots=invalid)
        stage_units(self.units,self.stage,installed_root=self.old,candidate_root=self.new,
                    installed_roots={name:'/opt/fixture-backup'})
        self.assertIn('WorkingDirectory='+str(self.new),(self.stage/name).read_text())
        self.assertIn('WorkingDirectory=/opt/fixture-backup',path.read_text())

    def test_next_rollout_preserves_existing_pair_flags_exactly_once(self):
        stage_units(self.units,self.stage,installed_root=self.old,candidate_root=self.new)
        next_stage=self.root/'next';next_root=Path('/opt/fixture-next')
        with self.assertRaises(ValueError):stage_units(self.stage,next_stage,installed_root=self.new,candidate_root=next_root)
        stage_units(self.stage,next_stage,installed_root=self.new,candidate_root=next_root,pair_already_enabled=True)
        for name in MODULES:
            data=(next_stage/name).read_text()
            self.assertEqual(data.count('--experimental-pair'),int(name in TRADING))

    def test_staging_accepts_repository_service_templates(self):
        deploy=Path(__file__).resolve().parents[1]/'deploy'
        names={'tradingbot-spot.service':'vm-cross-spot-paper.service',
               'tradingbot-futures.service':'vm-cross-isolated-paper.service',
               'tradingbot-cross.service':'vm-cross-futures-paper.service',
               'tradingbot-margin.service':'vm-spot-margin-paper.service',
               'tradingbot-monitor.service':'vm-cross-paper-monitor.service',
               'tradingbot-paper-backup.service':'vm-shared-paper-backup.service',
               'tradingbot-external-backup@.service':'tradingbot-external-backup@.service',
               'tradingbot-watchdog.service':'tradingbot-watchdog.service',
               'tradingbot-heartbeat.service':'tradingbot-heartbeat.service'}
        for target,source in names.items():
            text=(deploy/source).read_text()
            text=text.replace('/opt/trading-bot-cross',str(self.old)).replace('/opt/trading-bot-margin',str(self.old)).replace('/opt/trading-bot-paper',str(self.old)).replace('/opt/trading-bot-wallet',str(self.old))
            (self.units/target).write_text(text)
        stage_units(self.units,self.stage,installed_root=self.old,candidate_root=self.new)
        self.assertIn('deploy/cloud_paper_launch.py --experimental-pair',(self.stage/'tradingbot-spot.service').read_text())

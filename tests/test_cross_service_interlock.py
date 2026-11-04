import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from trading_bot.cross_service_interlock import arm_interlock,release_interlock
from trading_bot.cross_unit_switch import switch_unit_files


class CrossServiceInterlockTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name);self.units=self.root/'units';self.units.mkdir();self.marker=self.root/'cutover-in-progress'
        self.names=('spot.service','futures.service','cross.service');self.operation='cross:freeze:fixture'

    def arm(self):return arm_interlock(self.units,self.marker,self.names,self.operation,reload_units=lambda:True)

    def test_crash_after_first_dropin_has_no_marker_and_can_resume(self):
        code=("import os,sys\nfrom pathlib import Path\nfrom trading_bot.cross_service_interlock import arm_interlock\n"
              "r=Path(sys.argv[1])\narm_interlock(r/'units',r/'cutover-in-progress',('spot.service','futures.service','cross.service'),"
              "'cross:freeze:fixture',reload_units=lambda:True,fault=lambda p:os._exit(73) if p=='dropin:spot.service' else None)\n")
        child=subprocess.run([sys.executable,'-c',code,str(self.root)])
        self.assertEqual(child.returncode,73);self.assertFalse(self.marker.exists())
        self.assertTrue(self.arm());self.assertEqual(self.marker.read_text(),self.operation)
        for name in self.names:
            self.assertIn('ConditionPathExists=!'+str(self.marker),(self.units/(name+'.d')/'90-paper-cross-interlock.conf').read_text())

    def test_reload_failure_never_creates_marker(self):
        with self.assertRaisesRegex(ValueError,'reload failed'):
            arm_interlock(self.units,self.marker,self.names,self.operation,reload_units=lambda:False)
        self.assertFalse(self.marker.exists())

    def test_release_requires_explicit_safe_proof_and_preserves_foreign_marker(self):
        self.arm()
        with self.assertRaisesRegex(ValueError,'not proven'):
            release_interlock(self.units,self.marker,self.names,self.operation,verify_safe=lambda:False)
        self.assertTrue(self.marker.exists())
        with self.assertRaisesRegex(ValueError,'belongs to another'):
            arm_interlock(self.units,self.marker,self.names,'cross:freeze:other',reload_units=lambda:True)
        self.assertTrue(release_interlock(self.units,self.marker,self.names,self.operation,verify_safe=lambda:True))
        self.assertFalse(self.marker.exists())

    def test_marker_survives_kill_after_arming(self):
        code=("import os,sys\nfrom pathlib import Path\nfrom trading_bot.cross_service_interlock import arm_interlock\n"
              "r=Path(sys.argv[1])\narm_interlock(r/'units',r/'cutover-in-progress',('spot.service','futures.service','cross.service'),"
              "'cross:freeze:fixture',reload_units=lambda:True,fault=lambda p:os._exit(74) if p=='marker_armed' else None)\n")
        child=subprocess.run([sys.executable,'-c',code,str(self.root)])
        self.assertEqual(child.returncode,74);self.assertEqual(self.marker.read_text(),self.operation)
        self.assertTrue(self.arm());self.assertTrue(self.marker.exists())

    @unittest.skipUnless(shutil.which('systemd-analyze'),'systemd-analyze unavailable')
    def test_real_systemd_condition_blocks_start_until_marker_removed(self):
        condition='ConditionPathExists=!'+str(self.marker)
        before=subprocess.run(['systemd-analyze','condition',condition],capture_output=True,text=True)
        self.assertEqual(before.returncode,0)
        self.arm()
        blocked=subprocess.run(['systemd-analyze','condition',condition],capture_output=True,text=True)
        self.assertNotEqual(blocked.returncode,0)
        release_interlock(self.units,self.marker,self.names,self.operation,verify_safe=lambda:True)
        after=subprocess.run(['systemd-analyze','condition',condition],capture_output=True,text=True)
        self.assertEqual(after.returncode,0)

    @unittest.skipUnless(shutil.which('systemd-analyze'),'systemd-analyze unavailable')
    def test_partial_switch_keeps_persistent_systemd_start_interlock(self):
        candidate=self.root/'candidate';candidate.mkdir();archive=self.root/'archive';archive.mkdir()
        for name in self.names:
            (self.units/name).write_text('old '+name);(candidate/name).write_text('new '+name)
        self.arm()
        def crash(point):
            if point=='switched:spot.service':raise RuntimeError('injected crash')
        with self.assertRaisesRegex(RuntimeError,'injected'):
            switch_unit_files(self.units,candidate,archive,self.names,assert_safe=lambda:True,fault=crash)
        self.assertTrue(self.marker.exists())
        self.assertEqual((self.units/'futures.service').read_text(),'old futures.service')
        for _ in self.names:
            blocked=subprocess.run(['systemd-analyze','condition','ConditionPathExists=!'+str(self.marker)],capture_output=True)
            self.assertNotEqual(blocked.returncode,0)
        self.assertTrue(switch_unit_files(self.units,candidate,archive,self.names,assert_safe=lambda:True)['all_switched'])

    @unittest.skipUnless(shutil.which('systemd-analyze'),'systemd-analyze unavailable')
    def test_killed_mid_switch_keeps_interlock_and_requires_writer_fence(self):
        from trading_bot.app import process_lock
        from trading_bot.cross_unit_switch import hold_writer_locks,restore_unit_files
        candidate=self.root/'candidate';candidate.mkdir();archive=self.root/'archive';archive.mkdir()
        financial=self.root/'financial.db';financial.write_bytes(b'fixture')
        for name in self.names:
            (candidate/name).write_text('new '+name)
            if name!='cross.service':(self.units/name).write_text('old '+name)
        self.arm()
        code=("import os,sys\nfrom pathlib import Path\nfrom trading_bot.cross_unit_switch import switch_unit_files,hold_writer_locks\n"
              "r=Path(sys.argv[1])\nwith hold_writer_locks(r/'financial.db'):\n"
              " switch_unit_files(r/'units',r/'candidate',r/'archive',('spot.service','futures.service','cross.service'),"
              "new_units=('cross.service',),assert_safe=lambda:True,fault=lambda p:os._exit(75) if p=='switched:spot.service' else None)\n")
        child=subprocess.run([sys.executable,'-c',code,str(self.root)])
        self.assertEqual(child.returncode,75);self.assertTrue(self.marker.exists())
        blocked=subprocess.run(['systemd-analyze','condition','ConditionPathExists=!'+str(self.marker)],capture_output=True)
        self.assertNotEqual(blocked.returncode,0)
        with process_lock(str(financial)+'.spot.lock'):
            with self.assertRaisesRegex(ValueError,'still running'):
                with hold_writer_locks(financial):pass
        with hold_writer_locks(financial):
            result=restore_unit_files(self.units,archive,self.names,assert_safe=lambda:True,verify_rollback=lambda:True)
            self.assertTrue(result['all_restored'])
        self.assertEqual((self.units/'spot.service').read_text(),'old spot.service')
        self.assertFalse((self.units/'cross.service').exists())
        self.assertTrue(self.marker.exists())

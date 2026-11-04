import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from trading_bot.cross_cutover_journal import STAGES,cutover_journal
from trading_bot.cross_cutover_machine import run_cutover_phases


class CrossCutoverMachineTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name);self.op='cross:freeze:fixture';self.sha='a'*40

    def callbacks(self):
        actions={};probes={}
        for stage in STAGES[1:]:
            path=self.root/('action-'+stage)
            def action(p=path):
                with p.open('x') as stream:stream.write('done')
            actions[stage]=action;probes[stage]=lambda p=path:'DONE' if p.exists() else 'NOT_STARTED'
        return actions,probes

    def test_crash_after_effect_probes_before_retry_and_waits_for_reboot(self):
        code=("import os,sys\nfrom pathlib import Path\nfrom trading_bot.cross_cutover_journal import STAGES,cutover_journal\n"
              "from trading_bot.cross_cutover_machine import run_cutover_phases\n"
              "r=Path(sys.argv[1]);actions={};probes={}\n"
              "for s in STAGES[1:]:\n"
              " p=r/('action-'+s)\n"
              " actions[s]=lambda p=p:p.write_text('done')\n"
              " probes[s]=lambda p=p:'DONE' if p.exists() else 'NOT_STARTED'\n"
              "with cutover_journal(r,'cross:freeze:fixture','a'*40) as j:\n"
              " run_cutover_phases(j,actions,probes,fault=lambda p:os._exit(77) if p=='after_action:units_switched' else None)\n")
        child=subprocess.run([sys.executable,'-c',code,str(self.root)])
        self.assertEqual(child.returncode,77)
        actions,probes=self.callbacks()
        with cutover_journal(self.root,self.op,self.sha) as journal:
            self.assertEqual(journal.state['intent'],'units_switched')
            self.assertEqual(run_cutover_phases(journal,actions,probes),'awaiting_reboot')
            self.assertEqual(run_cutover_phases(journal,actions,probes),'awaiting_reboot')
            with self.assertRaisesRegex(ValueError,'Reboot'):
                run_cutover_phases(journal,actions,probes,after_reboot=True)
            self.assertFalse((self.root/'action-released').exists())
            self.assertEqual(run_cutover_phases(journal,actions,probes,after_reboot=True,verify_reboot=lambda:True),'complete')
            self.assertEqual(run_cutover_phases(journal,actions,probes),'complete')

    def test_unknown_observation_keeps_intent_and_does_not_execute(self):
        actions,probes=self.callbacks();probes['interlock_armed']=lambda:'UNKNOWN'
        with cutover_journal(self.root,self.op,self.sha) as journal:
            with self.assertRaisesRegex(ValueError,'uncertain'):
                run_cutover_phases(journal,actions,probes)
            self.assertEqual(journal.state['stage'],'created')
            self.assertEqual(journal.state['intent'],'interlock_armed')
        self.assertFalse((self.root/'action-interlock_armed').exists())

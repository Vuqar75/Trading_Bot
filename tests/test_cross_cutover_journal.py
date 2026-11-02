import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from trading_bot.cross_cutover_journal import cutover_journal


class CrossCutoverJournalTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name);self.op='cross:freeze:fixture';self.sha='a'*40

    def test_intent_and_completion_survive_real_process_death(self):
        code=("import os,sys\nfrom trading_bot.cross_cutover_journal import cutover_journal\n"
              "with cutover_journal(sys.argv[1],'cross:freeze:fixture','a'*40,"
              "fault=lambda p:os._exit(76) if p==sys.argv[3] else None) as journal:\n"
              " getattr(journal,sys.argv[2])('interlock_armed')\n")
        for point in ('before_save','after_save'):
            child=subprocess.run([sys.executable,'-c',code,str(self.root),'begin',point])
            self.assertEqual(child.returncode,76)
            with cutover_journal(self.root,self.op,self.sha) as journal:
                self.assertEqual(journal.state['stage'],'created')
                self.assertEqual(journal.state['intent'],None if point=='before_save' else 'interlock_armed')
        for point in ('before_save','after_save'):
            child=subprocess.run([sys.executable,'-c',code,str(self.root),'complete',point])
            self.assertEqual(child.returncode,76)
            with cutover_journal(self.root,self.op,self.sha) as journal:
                self.assertEqual(journal.state['stage'],'created' if point=='before_save' else 'interlock_armed')
                self.assertEqual(journal.state['intent'],'interlock_armed' if point=='before_save' else None)

    def test_concurrent_owner_and_different_identity_are_rejected(self):
        code=("import sys\nfrom trading_bot.cross_cutover_journal import cutover_journal\n"
              "with cutover_journal(sys.argv[1],'cross:freeze:fixture','a'*40): pass\n")
        with cutover_journal(self.root,self.op,self.sha) as journal:
            child=subprocess.run([sys.executable,'-c',code,str(self.root)],capture_output=True)
            self.assertNotEqual(child.returncode,0)
            with self.assertRaisesRegex(ValueError,'transition'):journal.begin('allocated')
            journal.begin('interlock_armed');journal.begin('interlock_armed')
        with self.assertRaisesRegex(ValueError,'identity mismatch'):
            with cutover_journal(self.root,self.op,'b'*40):pass

    def test_private_directory_and_symlink_lock_required(self):
        os.chmod(self.root,0o755)
        with self.assertRaisesRegex(ValueError,'Private'):
            with cutover_journal(self.root,self.op,self.sha):pass
        os.chmod(self.root,0o700);(self.root/'journal.lock').symlink_to(self.root/'other')
        with self.assertRaises(OSError):
            with cutover_journal(self.root,self.op,self.sha):pass

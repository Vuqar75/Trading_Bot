"""Queue interoperability between the installed and help-only code versions."""
import importlib.util
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from trading_bot.unified_queue import UnifiedQueue


class HelpMixedQueueTests(unittest.TestCase):
    def test_existing_worker_acknowledges_new_pollers_job_and_replay(self):
        old = '29d1fc6061902b00c49739b4b74c2f6285a15a3d'
        root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            command = ['git', 'show', old + ':trading_bot/unified_queue.py']
            result = subprocess.run(command, cwd=root, capture_output=True)
            if result.returncode and shutil.which('git.exe'):
                result = subprocess.run(['git.exe', *command[1:]], cwd=root, capture_output=True)
            self.assertEqual(result.returncode, 0, 'Unable to read installed-version queue source')
            source = result.stdout
            path = tmp / 'old_queue.py'
            path.write_bytes(source)
            spec = importlib.util.spec_from_file_location('trading_bot.old_queue_for_test', path)
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            new = UnifiedQueue(tmp / 'queue.db')
            old_worker = module.UnifiedQueue(tmp / 'queue.db')
            try:
                new.receive(1, True, '/mode futures_cross', now=100)
                new.receive(2, True, '/status futures_cross', now=100)
                new.receive(3, True, '/help', now=100)
                new.receive(3, True, '/help', now=100)
                jobs = old_worker.pending('futures_cross')
                self.assertEqual(len(jobs), 1)
                old_worker.acknowledge(jobs[0]['id'], 'Готово')
                self.assertEqual(new.pending('futures_cross'), [])
                before = new.db.execute('SELECT COUNT(*) FROM unified_outbox').fetchone()[0]
                new.receive(4, True, '/start', now=100)
                new.receive(4, True, '/start', now=100)
                after = new.db.execute('SELECT COUNT(*) FROM unified_outbox').fetchone()[0]
                self.assertEqual(after - before, 1)
                self.assertEqual(new.state('mode'), 'futures_cross')
                self.assertEqual(new.db.execute('SELECT COUNT(*) FROM unified_jobs').fetchone()[0], 1)
            finally:
                old_worker.close()
                new.close()

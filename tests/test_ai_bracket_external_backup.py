import sqlite3
import tempfile
import unittest
import signal
from pathlib import Path
from contextlib import closing
from types import SimpleNamespace
from unittest.mock import patch

from deploy.ai_bracket_external_backup import FREE_SPACE_RESERVE, check_work_space, inspect, terminate


class BackupInspectionTest(unittest.TestCase):
    def test_space_guard_includes_wal(self):
        with tempfile.TemporaryDirectory() as tmp:
            work = Path(tmp)
            source = work / 'paper.sqlite'
            source.write_bytes(b'abc')
            source.with_name('paper.sqlite-wal').write_bytes(b'de')
            needed = FREE_SPACE_RESERVE + 6 * 5
            with patch('shutil.disk_usage', return_value=SimpleNamespace(free=needed - 1)):
                with self.assertRaisesRegex(SystemExit, 'INSUFFICIENT_DISK'):
                    check_work_space(source, work)
            with patch('shutil.disk_usage', return_value=SimpleNamespace(free=needed)):
                check_work_space(source, work)

    def test_termination_unwinds_staging_and_preserves_receipt(self):
        with tempfile.TemporaryDirectory() as tmp:
            receipt = Path(tmp) / 'bracket-success.json'
            receipt.write_text('previous verified receipt', encoding='utf-8')
            with self.assertRaisesRegex(SystemExit, 'TERMINATED'):
                with tempfile.TemporaryDirectory(prefix='bracket-external-', dir=tmp) as staging:
                    (Path(staging) / 'snapshot.sqlite').touch()
                    terminate(signal.SIGTERM, None)
            self.assertFalse(Path(staging).exists())
            self.assertEqual(receipt.read_text(encoding='utf-8'), 'previous verified receipt')

    def test_balanced_restore_and_corrupted_posting(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'restored.sqlite'
            with closing(sqlite3.connect(path)) as db:
                db.execute('PRAGMA application_id=1095320146')
                db.executescript('''
                    CREATE TABLE ledger_events(id TEXT, posting_count INTEGER);
                    CREATE TABLE ledger_postings(event_id TEXT,account TEXT,asset TEXT,amount TEXT);
                    CREATE TABLE bracket_plans(id TEXT,payload TEXT,state TEXT);
                    CREATE TABLE bracket_fills(id TEXT);
                    CREATE TABLE bracket_funding(id TEXT);
                    INSERT INTO ledger_events VALUES('seed',2);
                    INSERT INTO ledger_postings VALUES('seed','ai_bracket:free','USDT','1000');
                    INSERT INTO ledger_postings VALUES('seed','external:capital','USDT','-1000');
                ''')
            result = inspect(path)
            self.assertEqual(result['balances']['free'], '1000')
            self.assertEqual(result['events'], 1)
            self.assertEqual(result['plans'], [])
            with closing(sqlite3.connect(path)) as db:
                db.execute("UPDATE ledger_postings SET amount='-999' WHERE account='external:capital'")
                db.commit()
            with self.assertRaisesRegex(ValueError, 'archive_unbalanced'):
                inspect(path)


if __name__ == '__main__':
    unittest.main()

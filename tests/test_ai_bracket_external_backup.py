import sqlite3
import tempfile
import unittest
from pathlib import Path
from contextlib import closing

from deploy.ai_bracket_external_backup import inspect


class BackupInspectionTest(unittest.TestCase):
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

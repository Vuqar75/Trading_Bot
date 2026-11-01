"""Verify a coordinated cross-cutover backup by restoring temporary copies.

Does not stop or fence writers and does not restore a live database. Caller is
responsible for choosing the current financial/queue/observer sources and for
taking the coordinated backup after all source writers have stopped.
"""
import hashlib
import json
import shutil
import sqlite3
import tempfile
from contextlib import closing
from pathlib import Path

from .paper_shared_store import SharedPaperStore
from .paper_wallet_ledger import PaperWalletLedger


def verify_restorable_backup(backup, *, freeze_id, scratch_parent):
    if not isinstance(freeze_id,str) or not freeze_id.startswith('cross:freeze:'):
        raise ValueError('Explicit freeze identity required')
    root=Path(backup)
    if root.is_symlink() or not root.is_dir():raise ValueError('Invalid backup directory')
    root=root.resolve(strict=True)
    manifest_path=root/'manifest.json'
    if manifest_path.is_symlink():raise ValueError('Invalid backup manifest')
    manifest=json.loads(manifest_path.read_text(encoding='utf-8'))
    if manifest.get('schema')!=1 or not isinstance(manifest.get('files'),list):raise ValueError('Invalid backup manifest')
    names=('financial.db','queue.db','observation.db')
    files=manifest['files']
    if len(files)!=len(names) or {entry.get('name') for entry in files}!=set(names):raise ValueError('Incomplete backup set')
    parent=Path(scratch_parent).resolve(strict=True)
    if not parent.is_dir():raise ValueError('Invalid restore location')
    with tempfile.TemporaryDirectory(prefix='cross-restore-check-',dir=parent) as temp:
        restored=Path(temp)
        for entry in files:
            name=entry['name'];source=root/name
            if source.is_symlink() or not source.is_file() or source.resolve(strict=True).parent!=root:
                raise ValueError('Invalid backup file')
            dest=restored/name;shutil.copyfile(source,dest)
            with dest.open('rb') as stream:digest=hashlib.file_digest(stream,'sha256').hexdigest()
            if digest!=entry.get('sha256'):raise ValueError('Backup hash mismatch')
            with closing(sqlite3.connect(dest.as_uri()+'?mode=ro',uri=True)) as db:
                if db.execute('PRAGMA integrity_check').fetchone()[0]!='ok' or db.execute('PRAGMA foreign_key_check').fetchone():
                    raise ValueError('Restored backup integrity failure')
        with closing(sqlite3.connect((restored/'financial.db').as_uri()+'?mode=ro',uri=True)) as db:
            db.row_factory=sqlite3.Row
            shared=object.__new__(SharedPaperStore);shared.db=db
            shared.ledger=object.__new__(PaperWalletLedger);shared.ledger.db=db
            if not shared.ledger.reconcile()['balanced']:raise ValueError('Restored journal mismatch')
            states={market:shared._assert_consistent(market) for market in ('spot','futures')}
            row=db.execute("SELECT value FROM ledger_meta WHERE key='cross_cutover_freeze'").fetchone()
            frozen=json.loads(row[0]) if row else {}
            if frozen.get('request_id')!=freeze_id or not shared.deployment_hold() or any(states[m]['paused'] is not True for m in states):
                raise ValueError('Backup does not contain expected paused freeze')
        with closing(sqlite3.connect((restored/'queue.db').as_uri()+'?mode=ro',uri=True)) as db:
            offset=db.execute("SELECT value FROM unified_state WHERE key='offset'").fetchone()
            pending=db.execute("SELECT count(*) FROM unified_jobs WHERE status='PENDING'").fetchone()[0]
            if not offset or int(offset[0])<frozen['queue_offset']:raise ValueError('Backup queue offset regressed')
            from .cross_rollout import _queue_anchor
            if _queue_anchor(db,frozen['queue_offset'])!=frozen.get('queue_anchor'):raise ValueError('Backup queue history changed')
        with closing(sqlite3.connect((restored/'observation.db').as_uri()+'?mode=ro',uri=True)) as db:
            if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='samples'").fetchone():raise ValueError('Observation history missing')
        return {'freeze_id':freeze_id,'queue_offset':int(offset[0]),'pending_commands':pending,'ledger_balanced':True}

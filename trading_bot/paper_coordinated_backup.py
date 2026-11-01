"""Experimental coordinated SQLite backups for the single-file paper runtime.

This code is not installed. No network operations are performed while locks are
held. A backup locks financial DB before inbox and observer; a queue command can
be pending after its financial effect, which is safe only with idempotent replay.
"""
import hashlib
import json
import os
import shutil
import sqlite3
import tempfile
import time
from contextlib import closing
from pathlib import Path


def coordinated_backup(financial,queue,observation,destination,*,timeout=2,fault=lambda p:None):
    sources=[Path(x).resolve(strict=True) for x in (financial,queue,observation)]
    destination=Path(destination).absolute()
    if len(set(sources))!=3 or destination.exists() or not destination.parent.is_dir():
        raise ValueError('Distinct sources and new destination required')
    stage=Path(tempfile.mkdtemp(prefix='.paper-backup-',dir=destination.parent));os.chmod(stage,0o700)
    locks=[]
    deadline=time.monotonic()+timeout
    try:
        for path in sources:
            remaining=deadline-time.monotonic()
            if remaining<=0:raise TimeoutError('Backup lock budget exhausted')
            db=sqlite3.connect(path.as_uri()+'?mode=rw',uri=True,isolation_level=None,timeout=remaining)
            locks.append(db)
            if db.execute('PRAGMA journal_mode').fetchone()[0].lower()!='delete':
                raise ValueError('Rollback journal required for coordinated backup')
            db.execute('BEGIN IMMEDIATE')
        entries=[]
        for path,name,required in zip(sources,('financial.db','queue.db','observation.db'),('ledger_events','unified_updates','samples')):
            dest=stage/name
            with closing(sqlite3.connect(path.as_uri()+'?mode=ro',uri=True,timeout=timeout)) as src, closing(sqlite3.connect(dest)) as target:
                if not src.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",(required,)).fetchone():raise ValueError('Wrong backup source schema')
                def progress(status,remaining,total):
                    if time.monotonic()>deadline:raise TimeoutError('Backup exceeded lock time')
                src.backup(target,pages=256,progress=progress)
                target.set_progress_handler(lambda: int(time.monotonic()>deadline),1000)
                if target.execute('PRAGMA integrity_check').fetchone()[0]!='ok':raise ValueError('Backup integrity failure')
                if target.execute('PRAGMA foreign_key_check').fetchone():raise ValueError('Backup foreign key failure')
            with open(dest,'rb') as f:
                digest=hashlib.file_digest(f,'sha256').hexdigest();os.fsync(f.fileno())
            entries.append({'name':name,'sha256':digest})
            if time.monotonic()>deadline:raise TimeoutError('Backup exceeded lock time')
        # Validate the journal against both shadows in the same immutable backup.
        from .paper_shared_store import SharedPaperStore
        from .paper_wallet_ledger import PaperWalletLedger
        with closing(sqlite3.connect((stage/'financial.db').as_uri()+'?mode=ro',uri=True)) as check:
            check.row_factory=sqlite3.Row
            shared=object.__new__(SharedPaperStore);shared.db=check
            shared.ledger=object.__new__(PaperWalletLedger);shared.ledger.db=check
            if not shared.ledger.reconcile()['balanced']:raise ValueError('Backup ledger integrity mismatch')
            shared._assert_consistent('spot');shared._assert_consistent('futures')
        if time.monotonic()>deadline:raise TimeoutError('Backup exceeded lock time')
        manifest={'schema':1,'captured_at':time.time(),'files':entries}
        path=stage/'manifest.json';path.write_text(json.dumps(manifest,sort_keys=True));os.chmod(path,0o600)
        with open(path,'rb') as f:os.fsync(f.fileno())
        directory=os.open(stage,os.O_RDONLY)
        try:os.fsync(directory)
        finally:os.close(directory)
        fault('before_publish')
        if destination.exists():raise ValueError('Destination already exists')
        os.rename(stage,destination)
        directory=os.open(destination.parent,os.O_RDONLY)
        try:os.fsync(directory)
        finally:os.close(directory)
        fault('after_publish')
        return manifest
    finally:
        for db in reversed(locks):
            try:
                if db.in_transaction:db.rollback()
            finally:db.close()
        if stage.exists():shutil.rmtree(stage)

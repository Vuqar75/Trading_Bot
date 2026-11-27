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

MAX_BUFFERED_BACKUP = 24*1024*1024


def require_workspace(directory, sources, copies):
    needed = 0
    for path in map(Path, sources):
        needed += path.stat().st_size
        try:
            needed += path.with_name(path.name + '-wal').stat().st_size
        except FileNotFoundError:
            pass
    if shutil.disk_usage(directory).free < 512 * 1024 * 1024 + copies * needed + 1024 * 1024:
        raise ValueError('Backup workspace insufficient')


def backup_phase(name, root, started):
    sizes = (p.stat() for p in root.rglob('*') if p.is_file())
    allocated = sum(getattr(info,'st_blocks',(info.st_size+511)//512)*512 for info in sizes)
    print(json.dumps({'backup_phase':name, 'allocated_bytes':allocated,
                      'elapsed_ms':round((time.monotonic()-started)*1000)}), flush=True)


def terminate_backup(_signum, _frame):
    raise SystemExit('Backup interrupted')


def coordinated_backup(financial,queue,observation,destination,*,timeout=2,fault=lambda p:None):
    sources=[Path(x).resolve(strict=True) for x in (financial,queue,observation)]
    destination=Path(destination).absolute()
    if len(set(sources))!=3 or destination.exists() or not destination.parent.is_dir():
        raise ValueError('Distinct sources and new destination required')
    require_workspace(destination.parent,sources,1)
    started=time.monotonic()
    sizes=[p.stat().st_size for p in sources]
    if sum(sizes)<=MAX_BUFFERED_BACKUP:
        # Warm file pages before acquiring a write lock. The bytes read here
        # are discarded; only SQLite's locked snapshot is ever published.
        for path,left in zip(sources,sizes):
            with path.open('rb') as source:
                while left:
                    chunk=source.read(min(left,1024*1024))
                    if not chunk:break
                    left-=len(chunk)
    stage=Path(tempfile.mkdtemp(prefix='.paper-backup-',dir=destination.parent));os.chmod(stage,0o700)
    locks=[]
    buffered=[]
    lock_started=time.monotonic()
    deadline=lock_started+timeout
    def release():
        while locks:
            db=locks.pop()
            try:
                if db.in_transaction:db.rollback()
            finally:db.close()
    try:
        for path in sources:
            remaining=deadline-time.monotonic()
            if remaining<=0:raise TimeoutError('Backup lock budget exhausted')
            db=sqlite3.connect(path.as_uri()+'?mode=rw',uri=True,isolation_level=None,timeout=remaining)
            locks.append(db)
            if db.execute('PRAGMA journal_mode').fetchone()[0].lower()!='delete':
                raise ValueError('Rollback journal required for coordinated backup')
            db.execute('BEGIN IMMEDIATE')
        captured_at=time.time()
        names=('financial.db','queue.db','observation.db')
        # For small snapshots, keep filesystem sync out of the write-lock interval.
        # Larger sets retain the disk-backed path.
        copied_bytes=sum(db.execute('PRAGMA page_count').fetchone()[0] *
                         db.execute('PRAGMA page_size').fetchone()[0] for db in locks)
        use_buffer=copied_bytes<=MAX_BUFFERED_BACKUP
        for path,name,required in zip(sources,names,('ledger_events','unified_updates','samples')):
            dest=stage/name
            target=sqlite3.connect(':memory:' if use_buffer else dest)
            if use_buffer:buffered.append((target,dest))
            try:
                with closing(sqlite3.connect(path.as_uri()+'?mode=ro',uri=True,timeout=timeout)) as src:
                    if not src.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",(required,)).fetchone():raise ValueError('Wrong backup source schema')
                    def progress(status,remaining,total):
                        if time.monotonic()>deadline:raise TimeoutError('Backup copy exceeded lock time: '+name)
                    src.backup(target,pages=256,progress=progress)
            finally:
                if not use_buffer:target.close()
            if time.monotonic()>deadline:raise TimeoutError('Backup copy exceeded lock time: '+name)
        release()
        print(json.dumps({'backup_phase':'coordinated_locked_copy',
                          'lock_ms':round((time.monotonic()-lock_started)*1000),
                          'buffered_bytes':copied_bytes if use_buffer else 0}),flush=True)
        fault('after_unlock')
        for target,dest in buffered:
            with closing(sqlite3.connect(dest)) as filedb:target.backup(filedb)
        backup_phase('coordinated_copy',stage,started)
        entries=[]
        for name in names:
            dest=stage/name
            with closing(sqlite3.connect(dest.as_uri()+'?mode=ro&immutable=1',uri=True)) as target:
                if target.execute('PRAGMA integrity_check').fetchone()[0]!='ok':raise ValueError('Backup integrity failure')
                if target.execute('PRAGMA foreign_key_check').fetchone():raise ValueError('Backup foreign key failure')
            with open(dest,'rb') as f:
                digest=hashlib.file_digest(f,'sha256').hexdigest();os.fsync(f.fileno())
            entries.append({'name':name,'sha256':digest})
        # Validate the journal against both shadows in the same immutable backup.
        from .paper_shared_store import SharedPaperStore
        from .paper_wallet_ledger import PaperWalletLedger
        with closing(sqlite3.connect((stage/'financial.db').as_uri()+'?mode=ro',uri=True)) as check:
            check.row_factory=sqlite3.Row
            shared=object.__new__(SharedPaperStore);shared.db=check
            shared.ledger=object.__new__(PaperWalletLedger);shared.ledger.db=check
            if not shared.ledger.reconcile()['balanced']:raise ValueError('Backup ledger integrity mismatch')
            shared._assert_consistent('spot');shared._assert_consistent('futures')
        backup_phase('coordinated_verified',stage,started)
        manifest={'schema':1,'captured_at':captured_at,'files':entries}
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
        release()
        for target,_ in buffered:target.close()
        if stage.exists():shutil.rmtree(stage)

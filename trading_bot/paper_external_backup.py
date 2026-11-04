"""Private, coordinated backup packaging; upload transport is explicitly injected.

This module does not enable a schedule, create a bucket, or restore a live DB.
"""
import hashlib
import json
import os
import re
import sqlite3
import tarfile
import tempfile
import time
from contextlib import closing
from pathlib import Path

from .paper_coordinated_backup import coordinated_backup

NAMES=('financial.db','queue.db','observation.db','manifest.json','metadata.json')


def prepare(financial, queue, observation, directory, *, commit, now=None):
    if not isinstance(commit,str) or not re.fullmatch('[0-9a-f]{40}',commit):
        raise ValueError('Explicit code commit required')
    directory=Path(directory)
    if not directory.is_dir():raise ValueError('Private staging directory required')
    os.chmod(directory,0o700)
    target=directory/'snapshot'
    manifest=coordinated_backup(financial,queue,observation,target,timeout=15)
    schemas={}
    for name in NAMES[:3]:
        with closing(sqlite3.connect((target/name).as_uri()+'?mode=ro',uri=True)) as db:
            schemas[name]={'user_version':db.execute('PRAGMA user_version').fetchone()[0],
                           'schema_version':db.execute('PRAGMA schema_version').fetchone()[0]}
    metadata={'format':1,'commit':commit,'captured_at':manifest['captured_at'],
              'schemas':schemas,'restore':'isolated-only; reconcile events before any cutover'}
    (target/'metadata.json').write_text(json.dumps(metadata,sort_keys=True),encoding='utf-8')
    os.chmod(target/'metadata.json',0o600)
    path=directory/'paper-backup.tar.gz'
    with tarfile.open(path,'x:gz') as archive:
        for name in NAMES:archive.add(target/name,arcname=name,recursive=False)
    os.chmod(path,0o600)
    with path.open('rb') as f:digest=hashlib.file_digest(f,'sha256').hexdigest()
    return {'path':str(path),'sha256':digest,'size':path.stat().st_size,'captured_at':metadata['captured_at'],'commit':commit}


def inspect_restore(archive, destination, *, expected_sha256):
    """Extract only named regular files into an empty, separate new directory."""
    archive=Path(archive);destination=Path(destination)
    if not re.fullmatch('[0-9a-f]{64}',expected_sha256):raise ValueError('Expected archive digest required')
    with archive.open('rb') as f:
        if hashlib.file_digest(f,'sha256').hexdigest()!=expected_sha256:raise ValueError('Archive checksum mismatch')
    if destination.exists() or not destination.parent.is_dir():raise ValueError('New isolated destination required')
    destination.mkdir(mode=0o700)
    try:
        with tarfile.open(archive,'r:gz') as tar:
            members=tar.getmembers()
            if len(members)!=len(NAMES) or {m.name for m in members}!=set(NAMES) or any(not m.isfile() for m in members):
                raise ValueError('Unexpected backup content')
            for member in members:
                source=tar.extractfile(member)
                with source,(destination/member.name).open('xb') as dest:
                    while True:
                        chunk=source.read(1024*1024)
                        if not chunk:break
                        dest.write(chunk)
                os.chmod(destination/member.name,0o600)
        manifest=json.loads((destination/'manifest.json').read_text(encoding='utf-8'))
        meta=json.loads((destination/'metadata.json').read_text(encoding='utf-8'))
        entries=manifest['files']
        if len(entries)!=3 or {x['name'] for x in entries}!=set(NAMES[:3]) or meta['captured_at']!=manifest['captured_at']:
            raise ValueError('Incomplete backup manifest')
        for item in entries:
            path=destination/item['name']
            with path.open('rb') as f:
                if hashlib.file_digest(f,'sha256').hexdigest()!=item['sha256']:raise ValueError('Snapshot checksum mismatch')
            with closing(sqlite3.connect(path.as_uri()+'?mode=ro',uri=True)) as db:
                if db.execute('PRAGMA integrity_check').fetchone()[0]!='ok' or db.execute('PRAGMA foreign_key_check').fetchone():
                    raise ValueError('Snapshot integrity mismatch')
        from .paper_shared_store import SharedPaperStore
        from .paper_wallet_ledger import PaperWalletLedger
        with closing(sqlite3.connect((destination/'financial.db').as_uri()+'?mode=ro',uri=True)) as db:
            db.row_factory=sqlite3.Row
            shared=object.__new__(SharedPaperStore);shared.db=db;shared.ledger=object.__new__(PaperWalletLedger);shared.ledger.db=db
            if not shared.ledger.reconcile()['balanced']:raise ValueError('Backup ledger mismatch')
            for market in ('spot','futures'):shared._assert_consistent(market)
            from .cross_paper_book import CrossPaperBook
            from .paper_pair_runtime_offline import margin_book_view
            cross=object.__new__(CrossPaperBook);cross.shared=shared;cross.db=db;cross.ledger=shared.ledger;cross._check()
            margin_book_view(db,shared.ledger)
        return {'commit':meta['commit'],'captured_at':meta['captured_at'],'balanced':True,'isolated':True}
    except BaseException:
        import shutil
        shutil.rmtree(destination)
        raise


def upload(prepared, upload_fn, record_fn):
    """Record success only after the upload backend confirms this exact object."""
    result=upload_fn(Path(prepared['path']),prepared['sha256'])
    if not isinstance(result,dict) or result.get('sha256')!=prepared['sha256'] or not result.get('object'):
        raise ValueError('Unconfirmed backup upload')
    record_fn({'object':result['object'],'sha256':result['sha256'],'captured_at':prepared['captured_at']})
    return result

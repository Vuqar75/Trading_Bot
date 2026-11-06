"""Move the original journal, never initialize a substitute budget.

Source scheduled tasks/processes MUST be disabled first. The persistent fence
is an additional guard for updated executors, not distributed fencing against
old binaries or an administrator deliberately activating another copy.
"""
import argparse
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import tempfile
import uuid

from .adaptive_budget_resilience import backup, check_backup, verify

LOCKS = ('interactive-once.lock', 'approved-followup-once.lock', 'adaptive-next-once.lock')


def freeze_export(state, *, source_stopped=False):
    state = Path(state)
    if not source_stopped:
        raise ValueError('disable_all_source_tasks_and_processes_first')
    if (state/'server-owner.json').exists():
        raise ValueError('server_is_already_active')
    # Persist before backup: any failure leaves source disabled, never sends.
    fence = state/'migration-source-frozen.json'
    if fence.exists():
        migration = json.loads(fence.read_text())
    else:
        migration = {'migration_id': str(uuid.uuid4()), 'role': 'SOURCE_FROZEN'}
        fd = os.open(fence, os.O_CREAT|os.O_EXCL|os.O_WRONLY, 0o600)
        with os.fdopen(fd, 'w') as f:
            json.dump(migration, f, sort_keys=True)
            f.flush(); os.fsync(f.fileno())
        fd = os.open(state, os.O_RDONLY|os.O_DIRECTORY)
        try: os.fsync(fd)
        finally: os.close(fd)
    if migration.get('role') != 'SOURCE_FROZEN' or not migration.get('migration_id'):
        raise ValueError('invalid_source_fence')
    folder, manifest = backup(state)
    manifest['migration'] = migration
    path = folder/'manifest.json'
    path.write_text(json.dumps(manifest, sort_keys=True)+'\n')
    with path.open('rb') as f: os.fsync(f.fileno())
    check_backup(folder)
    return folder


def import_original(folder, target):
    folder, target = Path(folder), Path(target)
    if target.exists() or target.is_symlink():
        raise ValueError('target_must_not_exist')
    manifest = json.loads((folder/'manifest.json').read_text())
    names = set(manifest['sha256'])
    allowed = {'experiment-budget.db', *LOCKS}
    if not {'experiment-budget.db', *LOCKS[:2]} <= names or not names <= allowed:
        raise ValueError('unexpected_archive_files')
    if any((folder/n).is_symlink() for n in names):
        raise ValueError('symlink_archive_rejected')
    migration = manifest.get('migration', {})
    if migration.get('role') != 'SOURCE_FROZEN' or not migration.get('migration_id'):
        raise ValueError('source_not_frozen')
    check_backup(folder)
    with closing(sqlite3.connect((folder/'experiment-budget.db').resolve().as_uri()+'?mode=ro', uri=True)) as db:
        original_rows = verify(db)
    target.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix='ai-import-', dir=target.parent))
    os.chmod(staging, 0o700)
    try:
        for name in names:
            shutil.copyfile(folder/name, staging/name)
            os.chmod(staging/name, 0o600)
            if hashlib.sha256((staging/name).read_bytes()).hexdigest() != manifest['sha256'][name]:
                raise ValueError('copy_hash_mismatch')
            with (staging/name).open('rb') as f: os.fsync(f.fileno())
        with closing(sqlite3.connect((staging/'experiment-budget.db').resolve().as_uri()+'?mode=ro', uri=True)) as db:
            if verify(db) != original_rows:
                raise ValueError('import_rows_changed')
        marker = staging/'server-owner.json'
        marker.write_text(json.dumps({'role': 'ACTIVE_SERVER', 'migration_id': migration['migration_id'],
                                     'imported_sha256': manifest['sha256']['experiment-budget.db']}, sort_keys=True)+'\n')
        os.chmod(marker, 0o600)
        with marker.open('rb') as f: os.fsync(f.fileno())
        fd = os.open(staging, os.O_RDONLY|os.O_DIRECTORY)
        try: os.fsync(fd)
        finally: os.close(fd)
        # mkdir publishes the claim atomically without replacing an existing target.
        target.mkdir(mode=0o700)
        for p in staging.iterdir():
            if p.name != 'server-owner.json': os.replace(p, target/p.name)
        os.replace(marker, target/marker.name)  # service activation marker is last
        fd = os.open(target, os.O_RDONLY|os.O_DIRECTORY)
        try: os.fsync(fd)
        finally: os.close(fd)
        fd = os.open(target.parent, os.O_RDONLY|os.O_DIRECTORY)
        try: os.fsync(fd)
        finally: os.close(fd)
    finally:
        shutil.rmtree(staging)
    return {'attempts': len(original_rows), 'reserved_micro_usd': sum(r[1] for r in original_rows),
            'migration_id': migration['migration_id']}


def main():
    p = argparse.ArgumentParser(description=__doc__)
    sub = p.add_subparsers(dest='action', required=True)
    source = sub.add_parser('freeze-export')
    source.add_argument('--state', required=True)
    source.add_argument('--source-stopped', action='store_true', required=True)
    dest = sub.add_parser('import-original')
    dest.add_argument('--archive', required=True)
    dest.add_argument('--target', required=True)
    args = p.parse_args()
    if args.action == 'freeze-export':
        print(freeze_export(args.state, source_stopped=args.source_stopped))
    else:
        print(json.dumps(import_original(args.archive, args.target), sort_keys=True))


if __name__ == '__main__': main()

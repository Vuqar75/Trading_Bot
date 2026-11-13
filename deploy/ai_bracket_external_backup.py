"""Consistent, host-encrypted external archive of the isolated bracket ledger.

Runs as the existing budget-backup service's ExecStartPost. Restores are checked
read-only in a temporary directory; this script never starts another writer.
"""
import hashlib
import gzip
import base64
import json
import os
import shutil
import sqlite3
import subprocess
import tempfile
import time
from decimal import Decimal
from pathlib import Path
from contextlib import closing

from research import adaptive_regular_backup as existing

SOURCE = Path('/var/lib/ai-bracket-study/paper.sqlite')
WORK = Path('/var/lib/tradingbot-ai-budget-backup')
PREFIX = 'paper-backups/daily/ai-bracket-study/'
CHUNK_BYTES = 2_000_000


def file_hash(path):
    digest = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def inspect(path):
    db = sqlite3.connect(Path(path).resolve().as_uri() + '?mode=ro&immutable=1', uri=True)
    try:
        if db.execute('PRAGMA application_id').fetchone()[0] != 0x41494252:
            raise ValueError('archive_application_id')
        if db.execute('PRAGMA integrity_check').fetchone()[0] != 'ok' or db.execute('PRAGMA foreign_key_check').fetchall():
            raise ValueError('archive_integrity')
        counts = dict(db.execute('SELECT event_id,COUNT(*) FROM ledger_postings GROUP BY event_id'))
        if any(counts.get(i, 0) != n for i, n in db.execute('SELECT id,posting_count FROM ledger_events')):
            raise ValueError('archive_posting_count')
        totals = {}
        balances = {}
        for event, account, asset, amount in db.execute('SELECT event_id,account,asset,amount FROM ledger_postings'):
            amount = Decimal(amount)
            if not amount.is_finite():
                raise ValueError('archive_posting_amount')
            totals[(event, asset)] = totals.get((event, asset), Decimal(0)) + amount
            balances[(account, asset)] = balances.get((account, asset), Decimal(0)) + amount
        if any(totals.values()):
            raise ValueError('archive_unbalanced')
        plans = []
        for ident, payload, raw in db.execute('SELECT id,payload,state FROM bracket_plans ORDER BY id'):
            plan = json.loads(payload)
            state = json.loads(raw)
            plans.append([ident, state.get('status'), state.get('open_qty'), state.get('remaining'),
                          plan.get('symbol'), plan.get('direction'), plan.get('entry'), plan.get('stop'), plan.get('target')])
        return {
            'plans': plans,
            'balances': {name: str(balances.get(('ai_bracket:' + name, 'USDT'), Decimal(0)))
                         for name in ('free', 'reserved', 'margin', 'funding_buffer')},
            'events': db.execute('SELECT COUNT(*) FROM ledger_events').fetchone()[0],
            'fills': db.execute('SELECT COUNT(*) FROM bracket_fills').fetchone()[0],
            'funding_events': db.execute('SELECT COUNT(*) FROM bracket_funding').fetchone()[0],
        }
    finally:
        db.close()


def main():
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    if not SOURCE.is_file() or SOURCE.is_symlink():
        raise ValueError('bracket_source_missing')
    with tempfile.TemporaryDirectory(prefix='bracket-external-', dir=WORK) as temp:
        temp = Path(temp)
        copied = temp / 'snapshot.sqlite'
        with closing(sqlite3.connect(SOURCE.resolve().as_uri() + '?mode=ro', uri=True, timeout=15)) as src:
            with closing(sqlite3.connect(copied)) as dst:
                src.backup(dst)
        summary = inspect(copied)
        if copied.stat().st_size > 2_000_000_000:
            raise ValueError('bracket_archive_too_large')
        # systemd-creds limits one plaintext credential to 1 MiB. Wrap a random
        # data key with the host key and authenticate bounded compressed chunks.
        key = os.urandom(32)
        result = subprocess.run(['systemd-creds', 'encrypt', '--with-key=host', '--name=AI_BRACKET_BACKUP', '-', '-'],
                                input=key, capture_output=True, timeout=60)
        if result.returncode:
            raise ValueError('bracket_encryption_failed')
        compressed = temp / 'snapshot.gz'
        with copied.open('rb') as src, compressed.open('wb') as out:
            with gzip.GzipFile(fileobj=out, mode='wb', mtime=0) as gz:
                shutil.copyfileobj(src, gz, 1024 * 1024)
        existing.PREFIX = PREFIX
        stamp = int(time.time() * 1000)
        pieces = []
        with compressed.open('rb') as stream:
            for number in range(1_000_000):
                chunk = stream.read(CHUNK_BYTES)
                if not chunk:
                    break
                nonce = os.urandom(12)
                aad = b'ai-bracket-v2:' + number.to_bytes(8, 'big')
                blob = nonce + AESGCM(key).encrypt(nonce, chunk, aad)
                name, digest, downloaded = existing.transfer(blob, {'created_utc_ms': stamp})
                (temp / ('chunk-' + str(number))).write_bytes(downloaded)
                pieces.append({'object': name, 'sha256': digest})
        if not pieces:
            raise ValueError('bracket_restore_format')
        manifest = {'version': 'ai-bracket-backup-v2', 'wrapped_key': base64.b64encode(result.stdout).decode(),
                    'chunks': pieces, 'sqlite_sha256': file_hash(copied), 'summary': summary}
        obj, sha, downloaded = existing.transfer(json.dumps(manifest, sort_keys=True).encode(), {'created_utc_ms': stamp})
        saved = json.loads(downloaded)
        if saved != manifest:
            raise ValueError('bracket_manifest_mismatch')
        result = subprocess.run(['systemd-creds', 'decrypt', '--name=AI_BRACKET_BACKUP', '-', '-'],
                                input=base64.b64decode(saved['wrapped_key'], validate=True), capture_output=True, timeout=60)
        if result.returncode:
            raise ValueError('bracket_restore_key')
        restored_gz = temp / 'restored.gz'
        with restored_gz.open('wb') as out:
            for number, piece in enumerate(saved['chunks']):
                blob = (temp / ('chunk-' + str(number))).read_bytes()
                if hashlib.sha256(blob).hexdigest() != piece['sha256']:
                    raise ValueError('bracket_chunk_hash')
                out.write(AESGCM(result.stdout).decrypt(blob[:12], blob[12:], b'ai-bracket-v2:' + number.to_bytes(8, 'big')))
        restored = temp / 'isolated-restored.sqlite'
        with gzip.open(restored_gz, 'rb') as src, restored.open('wb') as out:
            shutil.copyfileobj(src, out, 1024 * 1024)
        if file_hash(restored) != saved['sqlite_sha256']:
            raise ValueError('bracket_restore_hash')
        if inspect(restored) != summary:
            raise ValueError('bracket_restore_mismatch')
        receipt = {'object': obj, 'sha256': sha, 'captured_ms': int(time.time() * 1000),
                   'sqlite_sha256': saved['sqlite_sha256'], 'host_bound': True, 'chunks': len(pieces), 'summary': summary}
        pending = WORK / 'bracket-success.next'
        pending.write_text(json.dumps(receipt, sort_keys=True) + '\n')
        os.chmod(pending, 0o600)
        os.replace(pending, WORK / 'bracket-success.json')
        print(json.dumps({'status': 'bracket_backup_verified', **receipt}, sort_keys=True))


if __name__ == '__main__':
    try:
        main()
    except Exception:
        raise SystemExit('BRACKET_EXTERNAL_BACKUP_FAILED') from None

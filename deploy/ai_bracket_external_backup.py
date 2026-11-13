"""Consistent, host-encrypted external archive of the isolated bracket ledger.

Runs as the existing budget-backup service's ExecStartPost. Restores are checked
read-only in a temporary directory; this script never starts another writer.
"""
import hashlib
import gzip
import json
import os
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
        raw = copied.read_bytes()
        if len(raw) > 64_000_000:
            raise ValueError('bracket_archive_too_large')
        # systemd-creds limits one plaintext credential to 1 MiB. Wrap a random
        # data key with the host key and authenticate the compressed SQLite.
        key = os.urandom(32)
        nonce = os.urandom(12)
        result = subprocess.run(['systemd-creds', 'encrypt', '--with-key=host', '--name=AI_BRACKET_BACKUP', '-', '-'],
                                input=key, capture_output=True, timeout=60)
        if result.returncode:
            raise ValueError('bracket_encryption_failed')
        wrapped = result.stdout
        ciphertext = AESGCM(key).encrypt(nonce, gzip.compress(raw, mtime=0), b'ai-bracket-v1')
        envelope = b'AIBR1' + len(wrapped).to_bytes(4, 'big') + wrapped + nonce + ciphertext
        existing.PREFIX = PREFIX
        obj, sha, downloaded = existing.transfer(envelope, {'created_utc_ms': int(time.time() * 1000)})
        if downloaded[:5] != b'AIBR1':
            raise ValueError('bracket_restore_format')
        n = int.from_bytes(downloaded[5:9], 'big')
        result = subprocess.run(['systemd-creds', 'decrypt', '--name=AI_BRACKET_BACKUP', '-', '-'],
                                input=downloaded[9:9+n], capture_output=True, timeout=60)
        if result.returncode:
            raise ValueError('bracket_restore_key')
        restored_raw = gzip.decompress(AESGCM(result.stdout).decrypt(downloaded[9+n:21+n], downloaded[21+n:], b'ai-bracket-v1'))
        if hashlib.sha256(restored_raw).digest() != hashlib.sha256(raw).digest():
            raise ValueError('bracket_restore_hash')
        restored = temp / 'isolated-restored.sqlite'
        restored.write_bytes(restored_raw)
        if inspect(restored) != summary:
            raise ValueError('bracket_restore_mismatch')
        receipt = {'object': obj, 'sha256': sha, 'captured_ms': int(time.time() * 1000),
                   'sqlite_sha256': hashlib.sha256(raw).hexdigest(), 'host_bound': True, 'summary': summary}
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

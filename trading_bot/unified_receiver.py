"""Exactly-once local command application.

No network. A caller must retry the
same stable request identity after an uncertain result, then acknowledge the
source queue separately. Never call a network operation in the handler.
"""
import hashlib
import json
import sqlite3
import time
from decimal import Decimal, InvalidOperation
from pathlib import Path


class MarketReceiver:
    def __init__(self, path, market, fault=lambda point: None):
        if market not in ('spot', 'futures'):
            raise ValueError('Unsupported market')
        self.market = market
        self.table = 'runtime_meta' if market == 'spot' else 'futures_meta'
        self.db = sqlite3.connect(Path(path).resolve().as_uri()+'?mode=rw', uri=True, isolation_level=None, timeout=5)
        self.db.execute('PRAGMA synchronous=FULL')
        self.fault = fault
        if not self.db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (self.table,)).fetchone():
            self.db.close()
            raise ValueError('Expected dedicated paper database')
        identity = self._get('identity')
        if not isinstance(identity, dict) or identity.get('mode') != ('paper' if market == 'spot' else 'futures-paper'):
            self.db.close()
            raise ValueError('Expected dedicated paper database')

    def close(self):
        self.db.close()

    def _get(self, key, default=None):
        row = self.db.execute('SELECT value FROM '+self.table+' WHERE key=?', (key,)).fetchone()
        return json.loads(row[0]) if row else default

    def _set(self, key, value):
        self.db.execute('INSERT OR REPLACE INTO '+self.table+' VALUES(?,?)', (key, json.dumps(value, sort_keys=True)))

    def apply(self, ident, command, payload=None, *, now=None):
        if type(ident) is not int or ident < 0:
            raise ValueError('Invalid command identity')
        payload = {} if payload is None else payload
        canonical = json.dumps([self.market, command, payload], sort_keys=True, separators=(',', ':'))
        fingerprint = hashlib.sha256(canonical.encode()).hexdigest()
        key = 'unified-applied:'+str(ident)
        now = time.time() if now is None else now
        self.db.execute('BEGIN IMMEDIATE')
        try:
            prior = self._get(key)
            if prior:
                if prior['fingerprint'] != fingerprint:
                    raise ValueError('Conflicting command identity')
                self.db.commit()
                return prior['result']
            if command == '/pause':
                self._set('paused', True)
                result = 'Входы на паузе; контроль позиции продолжается.'
            elif command == '/resume':
                pos = self._get('position')
                unresolved = self.market == 'spot' and self.db.execute("SELECT 1 FROM runtime_orders WHERE phase!='SETTLED' LIMIT 1").fetchone()
                if self._get('blocked') or self._get('error') or unresolved or (payload.get('expires') is not None and now >= payload['expires']) or (self.market == 'spot' and (not isinstance(pos, dict) or not pos.get('fees_known'))):
                    result = 'Входы заблокированы до успешной проверки.'
                else:
                    self._set('paused', False)
                    result = 'Paper-входы разрешены.'
            elif command == '/confirmed_close':
                pos = self._get('position')
                try:
                    quantity = Decimal(str(payload['quantity']))
                    current = Decimal(str(pos['quantity'])) if isinstance(pos, dict) else Decimal(0)
                    valid = quantity.is_finite() and quantity > 0 and quantity == current and pos['id'] == payload['position'] and now < payload['expires']
                except (KeyError, TypeError, InvalidOperation):
                    valid = False
                unresolved = self.market == 'spot' and self.db.execute("SELECT 1 FROM runtime_orders WHERE phase!='SETTLED' LIMIT 1").fetchone()
                if not valid or self._get('blocked') or self._get('error') or self._get('close_request') or unresolved:
                    result = 'Закрытие отклонено: позиция изменилась или требуется проверка.'
                else:
                    self._set('paused', True)
                    self._set('close_request', {'target': pos['id'], 'quantity': str(quantity), 'update_id': ident, 'expires': payload['expires']})
                    result = 'Подтверждённый запрос сохранён; исполнение ожидается.'
            else:
                raise ValueError('Unsupported receiver command')
            self._set(key, {'fingerprint': fingerprint, 'result': result})
            self.fault('before_commit')
            self.db.commit()
            self.fault('after_commit')
            return result
        except BaseException:
            self.db.rollback()
            raise

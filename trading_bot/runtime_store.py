"""Single-database execution state for the runnable Spot application.

Dedicated schema: never migrates or opens the legacy bot's database.
"""
import hashlib
import json
import sqlite3
import time
import uuid
from contextlib import contextmanager
from decimal import Decimal
from pathlib import Path
from .testnet_observer import ObservationError, number

TERMINAL = {'FILLED', 'CANCELED', 'REJECTED', 'EXPIRED', 'EXPIRED_IN_MATCH'}


class RuntimeStore:
    def __init__(self, path, identity, fault=lambda point: None):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.fault = fault
        self.db = sqlite3.connect(path, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.execute('PRAGMA synchronous=FULL')
        self.db.execute('PRAGMA foreign_keys=ON')
        try:
            with self.transaction():
                tables = {r[0] for r in self.db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")}
                expected_tables = {'runtime_meta', 'runtime_orders', 'runtime_fills', 'runtime_commands', 'runtime_outbox', 'runtime_closes'}
                if tables and tables != expected_tables:
                    raise ObservationError('Use a new dedicated runtime database')
                for sql in (
                    'CREATE TABLE IF NOT EXISTS runtime_meta(key TEXT PRIMARY KEY,value TEXT NOT NULL)',
                    'CREATE TABLE IF NOT EXISTS runtime_orders(client_id TEXT PRIMARY KEY,business_key TEXT UNIQUE NOT NULL,side TEXT NOT NULL,quantity TEXT NOT NULL,price TEXT NOT NULL,phase TEXT NOT NULL,status TEXT,reason TEXT NOT NULL,created REAL NOT NULL)',
                    'CREATE TABLE IF NOT EXISTS runtime_fills(trade_id TEXT PRIMARY KEY,client_id TEXT NOT NULL REFERENCES runtime_orders(client_id),payload TEXT NOT NULL)',
                    'CREATE TABLE IF NOT EXISTS runtime_commands(update_id INTEGER PRIMARY KEY,fingerprint TEXT NOT NULL)',
                    'CREATE TABLE IF NOT EXISTS runtime_outbox(id INTEGER PRIMARY KEY,message TEXT NOT NULL)',
                    'CREATE TABLE IF NOT EXISTS runtime_closes(client_id TEXT NOT NULL,ts REAL NOT NULL,pnl TEXT NOT NULL,known INTEGER NOT NULL)',
                ):
                    self.db.execute(sql)
                if tables and self.get('schema') != 1:
                    raise ObservationError('Unsupported runtime schema')
                self.set('schema', 1)
                previous = self.get('identity')
                if previous is not None and previous != identity:
                    raise ObservationError('Runtime identity changed; use another database')
                self.set('identity', identity)
                if self.get('position') is None:
                    self.set('position', {'id': uuid.uuid4().hex, 'quantity': '0', 'cost': '0', 'fees_known': True})
                for key, value in [('cash', '1000'), ('paused', True), ('offset', 0), ('pnl', '0')]:
                    if self.get(key) is None:
                        self.set(key, value)
        except BaseException:
            self.db.close()
            raise

    def close(self):
        self.db.close()

    @contextmanager
    def transaction(self):
        self.db.execute('BEGIN IMMEDIATE')
        try:
            yield
            self.fault('before_commit')
            self.db.commit()
            self.fault('after_commit')
        except BaseException:
            self.db.rollback()
            raise

    def get(self, key, default=None):
        row = self.db.execute('SELECT value FROM runtime_meta WHERE key=?', (key,)).fetchone()
        return json.loads(row[0]) if row else default

    def set(self, key, value):
        self.db.execute('INSERT OR REPLACE INTO runtime_meta VALUES(?,?)', (key, json.dumps(value, sort_keys=True)))

    def notify(self, message):
        self.db.execute('INSERT INTO runtime_outbox(message) VALUES(?)', (message,))

    def pending(self):
        return self.db.execute("SELECT * FROM runtime_orders WHERE phase!='SETTLED' ORDER BY created").fetchall()

    def loss(self):
        start = int(time.time() // 86400) * 86400
        rows = self.db.execute('SELECT pnl,known FROM runtime_closes WHERE ts>=?', (start,)).fetchall()
        if any(not r['known'] for r in rows):
            raise ObservationError('Daily PnL contains unvalued fees')
        return -sum((Decimal(r['pnl']) for r in rows), Decimal(0))

    def prepare(self, side, quantity, price, business_key, reason, *, target=None, daily_limit=Decimal('5')):
        with self.transaction():
            if self.db.execute('SELECT 1 FROM runtime_orders WHERE business_key=?', (business_key,)).fetchone():
                return None
            if self.pending() or self.get('blocked'):
                raise ObservationError('Recovery or review required')
            pos = self.get('position')
            qty = number(quantity)
            if not qty:
                raise ObservationError('Zero quantity')
            if side == 'BUY':
                if self.get('paused') or number(pos['quantity']) or not pos['fees_known'] or self.loss() >= daily_limit:
                    raise ObservationError('Entry not allowed')
                if qty * price * Decimal('1.002') > number(self.get('cash')):
                    raise ObservationError('Insufficient budget')
            elif side == 'SELL':
                if target != pos['id'] or qty > number(pos['quantity']):
                    raise ObservationError('Stale or excessive close')
            else:
                raise ObservationError('Invalid side')
            client = 'rt' + uuid.uuid4().hex
            self.db.execute('INSERT INTO runtime_orders VALUES(?,?,?,?,?,\'PREPARED\',NULL,?,?)',
                            (client, business_key, side, str(qty), str(price), reason, time.time()))
            self.set('binding:' + client, pos['id'])
            return client

    def claim(self, client):
        with self.transaction():
            order = self.db.execute('SELECT * FROM runtime_orders WHERE client_id=?', (client,)).fetchone()
            if order is None or order['phase'] != 'PREPARED':
                raise ObservationError('Order not eligible to send')
            pos = self.get('position')
            if self.get('blocked') or self.get('binding:' + client) != pos['id']:
                raise ObservationError('Position changed or recovery required')
            if self.db.execute("SELECT 1 FROM runtime_orders WHERE client_id!=? AND phase!='SETTLED'", (client,)).fetchone():
                raise ObservationError('Another order is unresolved')
            if order['side'] == 'BUY' and (self.get('paused') or number(pos['quantity'])):
                raise ObservationError('Entry no longer allowed')
            if order['side'] == 'SELL' and number(order['quantity']) > number(pos['quantity']):
                raise ObservationError('Close quantity changed')
            self.db.execute("UPDATE runtime_orders SET phase='SUBMITTING' WHERE client_id=?", (client,))
        return dict(order)

    def apply(self, client, report):
        """Validate cumulative snapshot, then apply only unseen fills atomically."""
        with self.transaction():
            saved = self.db.execute('SELECT * FROM runtime_orders WHERE client_id=?', (client,)).fetchone()
            if saved is None or saved['phase'] not in {'SUBMITTING', 'UNKNOWN', 'ACK', 'SETTLED'}:
                raise ObservationError('Unexpected order state')
            order, fills = report['order'], report['fills']
            expected = (client, 'BTCUSDT', saved['side'], number(saved['quantity']))
            actual = (order['clientOrderId'], order['symbol'], order['side'], number(order['origQty']))
            if actual != expected:
                raise ObservationError('Order identity or quantity conflict')
            status = order['status']
            qty = sum((number(f['qty']) for f in fills), Decimal(0))
            quote = sum((number(f['quoteQty']) for f in fills), Decimal(0))
            if qty != number(order['executedQty']) or quote != number(order['cummulativeQuoteQty']):
                raise ObservationError('Fill totals disagree')
            if status not in TERMINAL | {'NEW', 'PARTIALLY_FILLED', 'PENDING_CANCEL'}:
                raise ObservationError('Unknown status')
            if qty > expected[3] or (status == 'FILLED' and qty != expected[3]) or (status == 'PARTIALLY_FILLED' and not 0 < qty < expected[3]) or (status in {'NEW', 'REJECTED'} and qty):
                raise ObservationError('Status conflicts with fills')
            if saved['status'] in TERMINAL and saved['status'] != status:
                raise ObservationError('Terminal status changed')
            encoded = {}
            for f in fills:
                if f['symbol'] != 'BTCUSDT' or str(f['orderId']) != str(order['orderId']) or f['isBuyer'] is not (saved['side'] == 'BUY'):
                    raise ObservationError('Fill identity conflict')
                if not number(f['qty']) or not number(f['quoteQty']):
                    raise ObservationError('Zero fill')
                key = str(f['id'])
                if key in encoded:
                    raise ObservationError('Duplicate fill')
                encoded[key] = json.dumps(f, sort_keys=True, separators=(',', ':'))
            existing = self.db.execute('SELECT trade_id,payload FROM runtime_fills WHERE client_id=?', (client,)).fetchall()
            if any(encoded.get(r['trade_id']) != r['payload'] for r in existing):
                raise ObservationError('Previously recorded fill missing or changed')
            pos = self.get('position')
            base, cost = number(pos['quantity']), number(pos['cost'])
            if saved['side'] == 'SELL' and self.get('entry_snapshot:' + client) is None:
                self.set('entry_snapshot:' + client, {'price': pos.get('entry_price', str(cost/base) if base else '0'), 'is_cost': 'entry_price' not in pos, 'fees': pos.get('entry_fees')})
            cash = Decimal(self.get('cash'))
            pnl = Decimal(0)
            known = pos['fees_known']
            for f in fills:
                key = str(f['id'])
                previous = self.db.execute('SELECT client_id,payload FROM runtime_fills WHERE trade_id=?', (key,)).fetchone()
                if previous:
                    if previous['client_id'] != client or previous['payload'] != encoded[key]:
                        raise ObservationError('Fill ID collision')
                    continue
                q, v, fee = number(f['qty']), number(f['quoteQty']), number(f['commission'])
                asset = f['commissionAsset']
                if not isinstance(asset, str) or not asset:
                    raise ObservationError('Invalid fee asset')
                known = known and (fee == 0 or asset in {'BTC', 'USDT'})
                if saved['side'] == 'BUY':
                    received = q - (fee if asset == 'BTC' else 0)
                    if received <= 0:
                        raise ObservationError('Invalid entry fee')
                    debit = v + (fee if asset == 'USDT' else 0)
                    if base == 0:
                        pos = {**pos, 'id': uuid.uuid4().hex, 'opened_at': time.time()}
                    cash -= debit
                    base += received
                    cost += debit
                else:
                    debit = q + (fee if asset == 'BTC' else 0)
                    if debit > base:
                        raise ObservationError('Exit exceeds owned position')
                    basis = cost * debit / base
                    credit = v - (fee if asset == 'USDT' else 0)
                    cash += credit
                    pnl += credit - basis
                    base -= debit
                    cost -= basis
                self.db.execute('INSERT INTO runtime_fills VALUES(?,?,?)', (key, client, encoded[key]))
            if base == 0:
                cost = Decimal(0)
            if saved['side'] == 'SELL' and (pnl or len(fills) > len(existing)):
                self.db.execute('INSERT INTO runtime_closes VALUES(?,?,?,?)', (client, time.time(), str(pnl), int(known)))
                self.set('pnl', str(Decimal(self.get('pnl')) + pnl))
            if saved['side'] == 'BUY' and qty and len(fills) > len(existing):
                entry_fees = {}
                for f in fills:
                    asset = f['commissionAsset']
                    entry_fees[asset] = str(Decimal(entry_fees.get(asset, '0')) + number(f['commission']))
                pos = {**pos, 'entry_price': str(quote/qty), 'entry_fees': entry_fees}
            if saved['side'] == 'SELL':
                self.set('order_net:' + client, str(sum((Decimal(r[0]) for r in self.db.execute('SELECT pnl FROM runtime_closes WHERE client_id=?', (client,))), Decimal(0))))
            self.set('cash', str(cash))
            self.set('position', {**pos, 'quantity': str(base), 'cost': str(cost), 'fees_known': known})
            phase = 'SETTLED' if status in TERMINAL else 'ACK'
            self.db.execute('UPDATE runtime_orders SET phase=?,status=? WHERE client_id=?', (phase, status, client))
            if status == 'REJECTED':
                self.set('blocked', 'ORDER_REJECTED_REVIEW')
                self.set('paused', True)
            if len(fills) > len(existing) or (phase == 'SETTLED' and saved['phase'] != 'SETTLED'):
                from .trade_notifications import spot_receipt
                fees = {}
                for f in fills:
                    asset = f['commissionAsset']
                    fees[asset] = fees.get(asset, Decimal(0)) + number(f['commission'])
                entry = self.get('entry_snapshot:' + client, {})
                self.notify(spot_receipt(mode=self.get('identity')['mode'], side=saved['side'], status=status,
                    quantity=qty, average=quote/qty if qty else 0, fees=fees, remaining=base,
                    reason=saved['reason'], interval=self.get('selected_interval', self.get('identity').get('interval', '15m')),
                    client=client, entry=entry.get('price'), entry_is_cost=entry.get('is_cost', False), entry_fees=entry.get('fees'),
                    net=self.get('order_net:' + client, '0'), known=known))

    def command(self, update_id, authorized, command, target=None):
        fingerprint = hashlib.sha256(json.dumps([authorized, command, target]).encode()).hexdigest()
        with self.transaction():
            prior = self.db.execute('SELECT fingerprint FROM runtime_commands WHERE update_id=?', (update_id,)).fetchone()
            if prior:
                if prior[0] != fingerprint:
                    raise ObservationError('Update identity conflict')
                return
            if update_id < self.get('offset'):
                raise ObservationError('Unexpected older update')
            self.db.execute('INSERT INTO runtime_commands VALUES(?,?)', (update_id, fingerprint))
            if authorized:
                if command in {'/pause', '/stop', '/resume'}:
                    allowed = command == '/resume'
                    if allowed and (self.pending() or self.get('blocked') or not self.get('position')['fees_known']):
                        message = 'Входы заблокированы: требуется сверка или оценка комиссий.'
                    else:
                        self.set('paused', not allowed)
                        message = 'Новые входы разрешены.' if allowed else 'Новые входы запрещены; контроль позиции продолжается.'
                elif command == '/close':
                    self.set('paused', True)
                    pos = self.get('position')
                    if target != pos['id'] or not number(pos['quantity']):
                        message = 'Нет соответствующей позиции.'
                    elif self.pending() or self.get('blocked'):
                        message = 'Закрытие заблокировано до сверки.'
                    else:
                        self.set('close_request', {'target': target, 'update_id': update_id})
                        message = 'Запрос закрытия сохранён; это ещё не исполнение.'
                elif command == 'STALE_COMMAND':
                    message = 'Команда устарела. Проверьте /status и отправьте новую команду.'
                elif command == '/history':
                    rows = self.db.execute('SELECT ts,pnl,known FROM runtime_closes ORDER BY rowid DESC LIMIT 10').fetchall()
                    message = '\n'.join(f"PnL {r['pnl']} USDT" + ('' if r['known'] else ' + неоценённая комиссия') for r in rows) or 'История закрытий пуста.'
                elif command == '/status':
                    from .telegram_reports import status
                    message = status(self)
                elif command == '/signal':
                    from .telegram_reports import signal_report
                    message = signal_report(self)
                elif command.startswith('/timeframe'):
                    parts = command.split()
                    current = self.get('selected_interval', self.get('identity').get('interval', '15m'))
                    if len(parts) == 1:
                        message = 'Таймфрейм: ' + current + '. Выбор: /timeframe 1m, 5m, 15m или 1h. Сначала /pause и закрытие позиции.'
                    elif len(parts) != 2 or parts[1] not in {'1m', '5m', '15m', '1h'}:
                        message = 'Используйте /timeframe 1m, /timeframe 5m, /timeframe 15m или /timeframe 1h.'
                    elif not self.get('paused') or self.pending() or self.get('blocked') or number(self.get('position')['quantity']) or self.get('close_request'):
                        message = 'Смена запрещена: нужны пауза, отсутствие позиции, незавершённых ордеров и блокировок.'
                    else:
                        if parts[1] != current:
                            self.set('interval_changes', self.get('interval_changes', []) + [{'from': current, 'to': parts[1], 'time': time.time()}])
                        self.set('selected_interval', parts[1])
                        self.set('last_candle', None)
                        self.set('signal', None)
                        message = 'Таймфрейм: ' + parts[1] + '. Входы остаются на паузе. Смена параметров — новый участок наблюдения; прежние результаты не сравнивайте как один фиксированный тест.'
                else:
                    message = '/status /signal /history /pause /resume /close /stop /timeframe. BTCUSDT Spot; плеча нет.'
                self.notify(message)
            self.set('offset', update_id + 1)

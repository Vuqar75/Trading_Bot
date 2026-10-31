"""Separate isolated futures paper wallet. No real exchange actions exist."""
import hashlib
import json
import sqlite3
import time
import uuid
from contextlib import contextmanager
from decimal import Decimal
from pathlib import Path
from .testnet_observer import ObservationError, number
from .futures_data import INTERVALS, signed_number
from .telegram_reports import when


class FuturesStore:
    def __init__(self, path, *, margin='10', fee_bps='5', slippage_bps='2', maintenance_bps='50', fault=lambda p: None):
        self.margin, self.fee, self.slip, self.maintenance = map(number, (margin, fee_bps, slippage_bps, maintenance_bps))
        if not 0 < self.margin <= 10 or self.fee >= 100 or self.slip >= 100 or not 0 < self.maintenance < 1000:
            raise ObservationError('Invalid futures paper costs/risk')
        self.fault = fault
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.execute('PRAGMA synchronous=FULL')
        self.db.execute('PRAGMA foreign_keys=ON')
        try:
            with self.transaction():
                tables = {r[0] for r in self.db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")}
                expected = {'futures_meta', 'futures_events', 'futures_funding', 'futures_commands', 'runtime_outbox'}
                if tables and tables != expected:
                    raise ObservationError('Futures paper requires a separate database')
                self.db.execute('CREATE TABLE IF NOT EXISTS futures_meta(key TEXT PRIMARY KEY,value TEXT NOT NULL)')
                self.db.execute('CREATE TABLE IF NOT EXISTS futures_events(id INTEGER PRIMARY KEY,business_key TEXT UNIQUE NOT NULL,ts REAL NOT NULL,event TEXT NOT NULL,payload TEXT NOT NULL)')
                self.db.execute('CREATE TABLE IF NOT EXISTS futures_funding(position_id TEXT NOT NULL,ts INTEGER NOT NULL,payload TEXT NOT NULL,PRIMARY KEY(position_id,ts))')
                self.db.execute('CREATE TABLE IF NOT EXISTS futures_commands(update_id INTEGER PRIMARY KEY,fingerprint TEXT NOT NULL)')
                self.db.execute('CREATE TABLE IF NOT EXISTS runtime_outbox(id INTEGER PRIMARY KEY,message TEXT NOT NULL)')
                identity = {'schema': 1, 'mode': 'futures-paper', 'symbol': 'BTCUSDT', 'margin': str(self.margin),
                            'fee_bps': str(self.fee), 'slippage_bps': str(self.slip), 'maintenance_bps': str(self.maintenance)}
                if tables and self.get('identity') != identity:
                    raise ObservationError('Futures identity changed; use separate database')
                self.set('identity', identity)
                for key, default in [('cash', '1000'), ('paused', True), ('leverage', 1), ('interval', '15m'), ('offset', 0), ('pnl', '0')]:
                    if self.get(key) is None:
                        self.set(key, default)
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
        row = self.db.execute('SELECT value FROM futures_meta WHERE key=?', (key,)).fetchone()
        return json.loads(row[0]) if row else default

    def set(self, key, value):
        self.db.execute('INSERT OR REPLACE INTO futures_meta VALUES(?,?)', (key, json.dumps(value, sort_keys=True)))

    def notify(self, text):
        self.db.execute('INSERT INTO runtime_outbox(message) VALUES(?)', (text,))

    def pending(self):
        return []  # Paper fills are atomic; no network orders/partial fills.

    def event(self, key, event, payload):
        self.db.execute('INSERT INTO futures_events(business_key,ts,event,payload) VALUES(?,?,?,?)',
                        (key, time.time(), event, json.dumps(payload, sort_keys=True)))

    def loss(self):
        start = int(time.time() // 86400)*86400
        rows = self.db.execute("SELECT payload FROM futures_events WHERE event='CLOSE' AND ts>=?", (start,)).fetchall()
        return -sum((Decimal(json.loads(r[0])['net']) for r in rows), Decimal(0))

    def open_position(self, direction, mark, server, key):
        if direction not in {'LONG', 'SHORT'} or number(mark) <= 0:
            raise ObservationError('Invalid futures entry')
        with self.transaction():
            if self.db.execute('SELECT 1 FROM futures_events WHERE business_key=?', (key,)).fetchone():
                return False
            if self.get('position') or self.get('paused') or self.get('blocked') or self.loss() >= 5:
                return False
            cash = number(self.get('cash'))
            if cash < self.margin:
                return False
            leverage = self.get('leverage')
            if type(leverage) is not int or not 1 <= leverage <= 10:
                raise ObservationError('Invalid leverage')
            buying = direction == 'LONG'
            fill = number(mark)*(1 + (1 if buying else -1)*self.slip/10000)
            quantity = self.margin*leverage/fill
            pos = {'id': uuid.uuid4().hex, 'direction': direction, 'entry': str(fill), 'quantity': str(quantity),
                   'margin': str(self.margin), 'leverage': leverage, 'entry_fee': str(quantity*fill*self.fee/10000),
                   'funding': '0', 'opened_ms': server, 'opened_at': server/1000, 'funding_cursor': server}
            self.set('cash', str(cash-self.margin))
            self.set('position', pos)
            self.event(key, 'OPEN', pos)
            from .trade_notifications import futures_receipt
            self.notify(futures_receipt('OPEN', pos, interval=self.get('interval')))
            return True

    def apply_funding(self, target, events, end):
        with self.transaction():
            pos = self.get('position')
            if not pos or pos['id'] != target:
                raise ObservationError('Funding position changed')
            if end < pos['funding_cursor']:
                raise ObservationError('Funding time regressed')
            funding = signed_number(pos['funding'])
            previous = pos['opened_ms']
            for row in events:
                stamp = row['time']
                if type(stamp) is not int or not previous < stamp <= end:
                    raise ObservationError('Invalid funding order')
                previous = stamp
                payload = json.dumps(row, sort_keys=True)
                saved = self.db.execute('SELECT payload FROM futures_funding WHERE position_id=? AND ts=?', (target, stamp)).fetchone()
                if saved:
                    if saved[0] != payload:
                        raise ObservationError('Funding event changed')
                    continue
                rate, mark = signed_number(row['rate']), number(row['mark'])
                if not mark or abs(rate) >= 1:
                    raise ObservationError('Invalid funding event')
                amount = number(pos['quantity'])*mark*rate*(-1 if pos['direction'] == 'LONG' else 1)
                funding += amount
                self.db.execute('INSERT INTO futures_funding VALUES(?,?,?)', (target, stamp, payload))
            self.set('position', {**pos, 'funding': str(funding), 'funding_cursor': end})

    def valuation(self, mark):
        pos = self.get('position')
        cash = Decimal(self.get('cash'))
        if not pos:
            return {'equity': cash, 'gross': Decimal(0), 'isolated': Decimal(0), 'maintenance': Decimal(0)}
        mark = number(mark)
        qty = number(pos['quantity'])
        gross = (mark-number(pos['entry']))*qty*(1 if pos['direction'] == 'LONG' else -1)
        isolated = number(pos['margin'])-number(pos['entry_fee'])+signed_number(pos['funding'])+gross
        exit_fill = mark*(1+(1 if pos['direction'] == 'SHORT' else -1)*self.slip/10000)
        exit_fee = qty*exit_fill*self.fee/10000
        exit_slippage = qty*mark*self.slip/10000
        return {'equity': cash+max(Decimal(0), isolated-exit_fee-exit_slippage), 'gross': gross, 'isolated': isolated,
                'maintenance': qty*mark*self.maintenance/10000+exit_fee+exit_slippage}

    def close_position(self, mark, server, key, reason, target, *, reverse=None, candle=None):
        with self.transaction():
            if self.db.execute('SELECT 1 FROM futures_events WHERE business_key=?', (key,)).fetchone():
                return False
            pos = self.get('position')
            if not pos or pos['id'] != target:
                return False
            if pos['funding_cursor'] < server:
                raise ObservationError('Funding must be reconciled before close')
            buying = pos['direction'] == 'SHORT'
            fill = number(mark)*(1+(1 if buying else -1)*self.slip/10000)
            if fill <= 0:
                raise ObservationError('Invalid exit mark')
            qty, margin = number(pos['quantity']), number(pos['margin'])
            gross = (fill-number(pos['entry']))*qty*(1 if pos['direction'] == 'LONG' else -1)
            fees = number(pos['entry_fee'])+qty*fill*self.fee/10000
            raw = margin+gross+signed_number(pos['funding'])-fees
            returned = max(Decimal(0), raw)
            net = returned-margin
            payload = {**pos, 'exit': str(fill), 'reason': reason, 'gross': str(gross), 'fees': str(fees),
                       'net': str(net), 'cap_adjustment': str(returned-raw), 'closed_ms': server}
            self.set('cash', str(Decimal(self.get('cash'))+returned))
            self.set('pnl', str(Decimal(self.get('pnl'))+net))
            self.set('position', None)
            self.set('close_request', None)
            self.set('reverse', reverse)
            if candle is not None:
                self.set('last_candle', candle)
            self.event(key, 'CLOSE', payload)
            from .trade_notifications import futures_receipt
            self.notify(futures_receipt('CLOSE', pos, interval=self.get('interval'), payload=payload))
            return True

    def record_equity(self, mark, server):
        with self.transaction():
            value = self.valuation(mark)
            peak = max(Decimal(self.get('equity_peak', '1000')), value['equity'])
            drawdown = max(Decimal(self.get('max_drawdown', '0')), peak-value['equity'])
            self.set('equity_peak', str(peak))
            self.set('max_drawdown', str(drawdown))
            self.set('equity', str(value['equity']))
            payload = json.dumps({'mark': str(mark), 'equity': str(value['equity']), 'max_drawdown': str(drawdown), 'server_ms': server}, sort_keys=True)
            self.db.execute("INSERT OR IGNORE INTO futures_events(business_key,ts,event,payload) VALUES(?,?,'EQUITY',?)", ('equity:'+str(server//60000), time.time(), payload))

    def status_text(self):
        pos, quote = self.get('position'), self.get('quote')
        lines = ['Виртуальные USD-M futures | BTCUSDT | Isolated',
                 f"Таймфрейм: {self.get('interval')} | Плечо: {self.get('leverage')}×",
                 f'Маржа на вход: {self.margin} USDT',
                 'Входы: '+('на паузе' if self.get('paused') else 'разрешены'),
                 f"Свободный баланс: {Decimal(self.get('cash')):.4f} USDT",
                 f"Реализованный PnL: {Decimal(self.get('pnl')):+.4f} USDT",
                 f"Макс. выборочная просадка: {Decimal(self.get('max_drawdown', '0')):.4f} USDT"]
        if pos:
            lines += [f"Позиция: {pos['direction']}, {pos['quantity']} BTC, вход {Decimal(pos['entry']):.2f}",
                      f"Funding позиции: {Decimal(pos['funding']):+.4f} USDT"]
        else:
            lines.append('Позиция: нет')
        if quote:
            age = max(0, int(time.time()-quote['time']))
            lines.append(f"Mark price: {Decimal(quote['price']):.2f}, {when(quote['time'])}, {age} сек. назад"+(' — УСТАРЕЛА' if age > 120 else ''))
            value = self.valuation(Decimal(quote['price']))
            lines.append(f"Расчётная equity после модельного закрытия: {value['equity']:.4f} USDT")
            if pos:
                lines.append(f"Открытый gross PnL по mark: {value['gross']:+.4f} USDT")
        lines += ['Источник: публичный Binance USD-M, не спотовый Testnet',
                  'Ошибка: '+str(self.get('error') or 'нет'), 'Сделки моделируются, на биржу не отправляются.']
        return '\n'.join(lines)

    def command(self, update_id, authorized, command, target=None):
        fingerprint = hashlib.sha256(json.dumps([authorized, command, target]).encode()).hexdigest()
        with self.transaction():
            saved = self.db.execute('SELECT fingerprint FROM futures_commands WHERE update_id=?', (update_id,)).fetchone()
            if saved:
                if saved[0] != fingerprint:
                    raise ObservationError('Command identity conflict')
                return
            if update_id < self.get('offset'):
                raise ObservationError('Unexpected old command')
            self.db.execute('INSERT INTO futures_commands VALUES(?,?)', (update_id, fingerprint))
            if authorized:
                parts = command.split()
                cmd = parts[0] if parts else ''
                if cmd in {'/pause', '/stop', '/resume'}:
                    if cmd == '/resume' and (self.get('blocked') or self.get('error')):
                        text = 'Входы заблокированы до успешной проверки данных.'
                    else:
                        self.set('paused', cmd != '/resume')
                        text = 'Входы на паузе; позиция контролируется.' if cmd != '/resume' else 'Paper-входы разрешены.'
                elif cmd == '/close':
                    self.set('paused', True)
                    pos = self.get('position')
                    if pos and pos['id'] == target:
                        self.set('close_request', {'target': target, 'update_id': update_id})
                        text = 'Запрос закрытия сохранён, исполнение ещё не подтверждено.'
                    else:
                        text = 'Соответствующей позиции нет.'
                elif cmd in {'/leverage', '/timeframe'}:
                    key = 'leverage' if cmd == '/leverage' else 'interval'
                    allowed = {str(i) for i in range(1, 11)} if key == 'leverage' else set(INTERVALS)
                    if len(parts) == 1:
                        text = f"{cmd}: {self.get(key)}. Выбор: "+', '.join(sorted(allowed))
                    elif len(parts) != 2 or parts[1] not in allowed:
                        text = 'Некорректное значение: '+', '.join(sorted(allowed))
                    elif not self.get('paused') or self.get('position') or self.get('close_request') or self.get('blocked'):
                        text = 'Сначала пауза и закрытие позиции; блокировок быть не должно.'
                    else:
                        self.set(key, int(parts[1]) if key == 'leverage' else parts[1])
                        self.set('last_candle', None)
                        self.set('signal', None)
                        text = 'Настройка сохранена, входы остаются на паузе.'
                elif cmd == '/status':
                    text = self.status_text()
                elif cmd == '/signal':
                    snap = self.get('signal')
                    if not snap:
                        text = 'Проверка стратегии ещё не выполнена.'
                    else:
                        text = f"SMA 10/30 | Futures BTCUSDT | {self.get('interval')}\nПроверка: {when(snap['time'])}\nСигнал: {snap['action']}\n{snap['reason']}"
                        if 'fast' in snap:
                            text += f"\nSMA 10: {snap['old_fast']:.2f} → {snap['fast']:.2f}\nSMA 30: {snap['old_slow']:.2f} → {snap['slow']:.2f}"
                elif cmd == '/history':
                    rows = self.db.execute("SELECT ts,payload FROM futures_events WHERE event='CLOSE' ORDER BY id DESC LIMIT 10").fetchall()
                    text = '\n\n'.join(f"{when(r['ts'])} | {p['direction']} {p['leverage']}×\nNet {Decimal(p['net']):+.4f}, комиссии {Decimal(p['fees']):.4f}, funding {Decimal(p['funding']):+.4f} USDT | {p['reason']}" for r in rows for p in [json.loads(r['payload'])]) or 'История пуста.'
                elif cmd == 'STALE_COMMAND':
                    text = 'Команда устарела, отправьте новую.'
                else:
                    text = '/status /signal /history /pause /stop /resume /close /leverage 1..10 /timeframe 1m|5m|15m|1h. Только futures paper isolated.'
                self.notify(text)
            self.set('offset', update_id+1)

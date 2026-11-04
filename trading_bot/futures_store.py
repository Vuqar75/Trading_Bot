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
    def __init__(self, path, *, margin='10', fee_bps='5', slippage_bps='2', maintenance_bps='50', fault=lambda p: None, shared=False,
                 symbol='BTCUSDT', allow_pair_experiment=False):
        from .paper_wallet_ledger import valid_asset
        if (not isinstance(symbol,str) or not symbol.endswith('USDT') or not valid_asset(symbol[:-4]) or symbol[:-4]=='USDT'):
            raise ObservationError('Invalid futures symbol')
        if symbol!='BTCUSDT' and not allow_pair_experiment:
            raise ObservationError('Selectable futures pairs are not integrated')
        self.symbol=symbol
        self.margin, self.fee, self.slip, self.maintenance = map(number, (margin, fee_bps, slippage_bps, maintenance_bps))
        if not 0 < self.margin <= 10 or self.fee >= 100 or self.slip >= 100 or not 0 < self.maintenance < 1000:
            raise ObservationError('Invalid futures paper costs/risk')
        self.fault = fault
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.shared = None
        if shared:
            from .paper_shared_store import SharedPaperStore
            self.shared = SharedPaperStore(path, fault=fault)
        self.db = self.shared.db if self.shared else sqlite3.connect(path, isolation_level=None)
        self.outbox_table = 'futures_outbox' if shared else 'runtime_outbox'
        self.db.row_factory = sqlite3.Row
        self.db.execute('PRAGMA synchronous=FULL')
        self.db.execute('PRAGMA foreign_keys=ON')
        try:
            with self.transaction():
                tables = {r[0] for r in self.db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")}
                expected = {'futures_meta', 'futures_events', 'futures_funding', 'futures_commands', self.outbox_table}
                if tables and (not expected.issubset(tables) if self.shared else tables != expected):
                    raise ObservationError('Futures paper requires a separate database')
                self.db.execute('CREATE TABLE IF NOT EXISTS futures_meta(key TEXT PRIMARY KEY,value TEXT NOT NULL)')
                self.db.execute('CREATE TABLE IF NOT EXISTS futures_events(id INTEGER PRIMARY KEY,business_key TEXT UNIQUE NOT NULL,ts REAL NOT NULL,event TEXT NOT NULL,payload TEXT NOT NULL)')
                self.db.execute('CREATE TABLE IF NOT EXISTS futures_funding(position_id TEXT NOT NULL,ts INTEGER NOT NULL,payload TEXT NOT NULL,PRIMARY KEY(position_id,ts))')
                self.db.execute('CREATE TABLE IF NOT EXISTS futures_commands(update_id INTEGER PRIMARY KEY,fingerprint TEXT NOT NULL)')
                self.db.execute('CREATE TABLE IF NOT EXISTS '+self.outbox_table+'(id INTEGER PRIMARY KEY,message TEXT NOT NULL)')
                identity = {'schema': 1, 'mode': 'futures-paper', 'symbol': self.symbol, 'margin': str(self.margin),
                            'fee_bps': str(self.fee), 'slippage_bps': str(self.slip), 'maintenance_bps': str(self.maintenance)}
                if tables and self.get('identity') != identity:
                    raise ObservationError('Futures identity changed; use separate database')
                self.set('identity', identity)
                for key, default in [('cash', '1000'), ('paused', True), ('leverage', 1), ('interval', '15m'), ('offset', 0), ('pnl', '0')]:
                    if self.get(key) is None:
                        self.set(key, default)
            self.pair_version=self.get('pair_selection_version',0)
            if type(self.pair_version) is not int or self.pair_version<0:raise ObservationError('Invalid futures pair version')
        except BaseException:
            self.close()
            raise

    def close(self):
        if self.shared:self.shared.close()
        else:self.db.close()

    @contextmanager
    def transaction(self):
        if self.shared:
            self.shared.fault=self.fault
            with self.shared.market_transaction('futures'):
                self._require_bound_symbol()
                yield
            return
        self.db.execute('BEGIN IMMEDIATE')
        try:
            self._require_bound_symbol()
            yield
            self.fault('before_commit')
            self.db.commit()
            self.fault('after_commit')
        except BaseException:
            self.db.rollback()
            raise

    def _require_bound_symbol(self):
        if not self.db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='futures_meta'").fetchone():return
        identity=self.get('identity')
        if identity is not None and identity.get('symbol','BTCUSDT')!=self.symbol:
            raise ObservationError('Futures pair version superseded')
        if hasattr(self,'pair_version') and self.get('pair_selection_version',0)!=self.pair_version:
            raise ObservationError('Futures pair version superseded')

    def get(self, key, default=None):
        row = self.db.execute('SELECT value FROM futures_meta WHERE key=?', (key,)).fetchone()
        return json.loads(row[0]) if row else default

    def set(self, key, value):
        if self.shared and not self.db.in_transaction:
            raise ObservationError('Shared state writes require a transaction')
        self.db.execute('INSERT OR REPLACE INTO futures_meta VALUES(?,?)', (key, json.dumps(value, sort_keys=True)))

    def notify(self, text):
        self.db.execute('INSERT INTO '+self.outbox_table+'(message) VALUES(?)', (text,))

    def pending(self):
        return []  # Paper fills are atomic; no network orders/partial fills.

    def event(self, key, event, payload):
        self.db.execute('INSERT INTO futures_events(business_key,ts,event,payload) VALUES(?,?,?,?)',
                        (key, time.time(), event, json.dumps(payload, sort_keys=True)))

    def loss(self):
        start = int(time.time() // 86400)*86400
        rows = self.db.execute("SELECT payload FROM futures_events WHERE event='CLOSE' AND ts>=?", (start,)).fetchall()
        return -sum((Decimal(json.loads(r[0])['net']) for r in rows), Decimal(0))

    def open_position(self, direction, mark, server, key, *, metadata=None, quote_symbol=None, observed_at=None):
        if direction not in {'LONG', 'SHORT'} or number(mark) <= 0:
            raise ObservationError('Invalid futures entry')
        with self.transaction():
            if self.db.execute('SELECT 1 FROM futures_events WHERE business_key=?', (key,)).fetchone():
                return False
            if self.shared and self.shared.deployment_hold():
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
            used_margin=self.margin
            if self.symbol!='BTCUSDT':
                if quote_symbol!=self.symbol or isinstance(observed_at,bool) or not isinstance(observed_at,(int,float)) or not 0<=time.time()-observed_at<=120:
                    raise ObservationError('Futures quote identity or freshness not confirmed')
                from .paper_pair_catalog import usd_m_perpetual_candidate
                quantity=number(usd_m_perpetual_candidate(metadata,self.symbol,quantity,fill)['quantity'])
                used_margin=quantity*fill/leverage
            pos = {'id': uuid.uuid4().hex, 'direction': direction, 'entry': str(fill), 'quantity': str(quantity),
                   'margin': str(used_margin), 'leverage': leverage, 'entry_fee': str(quantity*fill*self.fee/10000),
                   'funding': '0', 'opened_ms': server, 'opened_at': server/1000, 'funding_cursor': server}
            if self.symbol!='BTCUSDT':pos['symbol']=self.symbol
            self.set('cash', str(cash-used_margin))
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
                if pos.get('symbol','BTCUSDT')!='BTCUSDT' and row.get('symbol')!=pos['symbol']:
                    raise ObservationError('Funding symbol conflicts with position')
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

    def valuation(self, mark, *, quote_symbol=None, observed_at=None):
        pos = self.get('position')
        cash = Decimal(self.get('cash'))
        if not pos:
            return {'equity': cash, 'gross': Decimal(0), 'isolated': Decimal(0), 'maintenance': Decimal(0)}
        if pos.get('symbol','BTCUSDT')!='BTCUSDT' and (quote_symbol!=pos['symbol'] or
                isinstance(observed_at,bool) or not isinstance(observed_at,(int,float)) or not 0<=time.time()-observed_at<=120):
            raise ObservationError('Futures valuation quote identity or freshness not confirmed')
        mark = number(mark)
        qty = number(pos['quantity'])
        gross = (mark-number(pos['entry']))*qty*(1 if pos['direction'] == 'LONG' else -1)
        isolated = number(pos['margin'])-number(pos['entry_fee'])+signed_number(pos['funding'])+gross
        exit_fill = mark*(1+(1 if pos['direction'] == 'SHORT' else -1)*self.slip/10000)
        exit_fee = qty*exit_fill*self.fee/10000
        exit_slippage = qty*mark*self.slip/10000
        return {'equity': cash+max(Decimal(0), isolated-exit_fee-exit_slippage), 'gross': gross, 'isolated': isolated,
                'maintenance': qty*mark*self.maintenance/10000+exit_fee+exit_slippage}

    def close_position(self, mark, server, key, reason, target, *, reverse=None, candle=None, quote_symbol=None, observed_at=None):
        with self.transaction():
            if self.db.execute('SELECT 1 FROM futures_events WHERE business_key=?', (key,)).fetchone():
                return False
            pos = self.get('position')
            if not pos or pos['id'] != target:
                return False
            if pos.get('symbol','BTCUSDT')!='BTCUSDT' and (quote_symbol!=pos['symbol'] or
                    isinstance(observed_at,bool) or not isinstance(observed_at,(int,float)) or not 0<=time.time()-observed_at<=120):
                raise ObservationError('Futures close quote identity or freshness not confirmed')
            if reason == 'MANUAL' and not self.manual_request_valid(self.get('close_request'), pos):
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

    @staticmethod
    def manual_request_valid(request, pos, now=None):
        if not isinstance(request, dict) or not isinstance(pos, dict) or request.get('target') != pos.get('id'):
            return False
        if 'quantity' not in request:
            return True
        try:
            return number(request['quantity']) == number(pos['quantity']) and number(pos['quantity']) > 0 and (time.time() if now is None else now) < request['expires']
        except (KeyError, TypeError, ValueError, ObservationError):
            return False

    def record_equity(self, mark, server, *, quote_symbol=None, observed_at=None):
        with self.transaction():
            value = self.valuation(mark,quote_symbol=quote_symbol,observed_at=observed_at)
            peak = max(Decimal(self.get('equity_peak', '1000')), value['equity'])
            drawdown = max(Decimal(self.get('max_drawdown', '0')), peak-value['equity'])
            self.set('equity_peak', str(peak))
            self.set('max_drawdown', str(drawdown))
            self.set('equity', str(value['equity']))
            payload = json.dumps({'mark': str(mark), 'equity': str(value['equity']), 'max_drawdown': str(drawdown), 'server_ms': server}, sort_keys=True)
            self.db.execute("INSERT OR IGNORE INTO futures_events(business_key,ts,event,payload) VALUES(?,?,'EQUITY',?)", ('equity:'+str(server//60000), time.time(), payload))

    def status_text(self):
        from .display_amount import display_amount as fmt
        pos, quote = self.get('position'), self.get('quote')
        symbol=self.get('identity').get('symbol','BTCUSDT');asset=symbol[:-4]
        if quote and quote.get('symbol','BTCUSDT')!=symbol:quote=None
        lines = ['Виртуальные фьючерсы USD-M | '+symbol+' | Изолированная маржа (Isolated)',
                 f"Таймфрейм: {self.get('interval')} | Плечо: {self.get('leverage')}×",
                 f'Маржа на вход: {fmt(self.margin)} USDT',
                 'Входы: '+('на паузе' if self.get('paused') else 'разрешены'),
                 f"Свободный баланс: {fmt(self.get('cash'))} USDT",
                 f"Реализованный PnL: {fmt(self.get('pnl'),signed=True)} USDT",
                 f"Макс. выборочная просадка: {fmt(self.get('max_drawdown', '0'))} USDT"]
        if pos:
            lines += [f"Позиция: {pos['direction']}, {fmt(pos['quantity'],asset)} {asset}, вход {fmt(pos['entry'])} USDT",
                      f"Funding (начислено + / списано −): {fmt(pos['funding'],signed=True)} USDT"]
        else:
            lines.append('Позиция: нет')
        if quote:
            age = max(0, int(time.time()-quote['time']))
            lines.append(f"Расчётная цена фьючерса (mark): {fmt(quote['price'])} USDT, {when(quote['time'])}, {age} сек. назад"+(' — УСТАРЕЛА' if age > 120 else ''))
            try:
                value = self.valuation(Decimal(quote['price']),quote_symbol=symbol,observed_at=quote['time'])
            except ObservationError:
                lines.append('Оценка недоступна: котировка устарела или не подтверждена.')
            else:
                lines.append(f"Средства с учётом модельного закрытия: {fmt(value['equity'])} USDT")
                if pos:
                    lines.append(f"Результат движения цены без комиссий: {fmt(value['gross'],signed=True)} USDT")
        from .user_text import error_text
        lines += ['Источник: публичный Binance USD-M, не спотовый Testnet',
                  'Ошибка: '+error_text(self.get('error')), 'Сделки моделируются, на биржу не отправляются.']
        return '\n'.join(lines)

    def command(self, update_id, authorized, command, target=None, *, routed=False):
        fingerprint = hashlib.sha256(json.dumps([authorized, command, target]).encode()).hexdigest()
        with self.transaction():
            saved = self.db.execute('SELECT fingerprint FROM futures_commands WHERE update_id=?', (update_id,)).fetchone()
            if saved:
                if saved[0] != fingerprint:
                    raise ObservationError('Command identity conflict')
                return
            if not routed and update_id < self.get('offset'):
                raise ObservationError('Unexpected old command')
            self.db.execute('INSERT INTO futures_commands VALUES(?,?)', (update_id, fingerprint))
            if authorized:
                parts = command.split()
                cmd = parts[0] if parts else ''
                if cmd in {'/pause', '/stop', '/resume'}:
                    if cmd == '/resume' and (self.get('blocked') or self.get('error') or (self.shared and self.shared.deployment_hold())):
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
                        text = f"{cmd}: {self.get(key)}. Выбор: "+', '.join(sorted(allowed, key=int) if key == 'leverage' else sorted(allowed))
                    elif len(parts) != 2 or parts[1] not in allowed:
                        text = 'Некорректное значение: '+', '.join(sorted(allowed, key=int) if key == 'leverage' else sorted(allowed))
                    elif self.shared and self.shared.deployment_hold():
                        text = 'Изменение настроек недоступно до завершения проверки переноса.'
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
                    if not snap or snap.get('symbol','BTCUSDT')!=self.symbol:
                        text = 'Проверка стратегии ещё не выполнена.'
                    else:
                        action = {'BUY': 'пересечение вверх — LONG', 'SELL': 'пересечение вниз — SHORT', 'HOLD': 'нового пересечения нет'}.get(snap['action'], 'не определён')
                        age = max(0, int(time.time()-snap['time']))
                        text = f"SMA 10/30 | Futures {self.symbol} | {self.get('interval')}\nПроверка: {when(snap['time'])}, {age} сек. назад\nСигнал: {action}\n{snap['reason']}"
                        if age > 120:
                            text += '\nДанные устарели.'
                        if 'fast' in snap:
                            from .display_amount import display_amount as fmt
                            text += f"\nSMA 10: {fmt(str(snap['old_fast']))} → {fmt(str(snap['fast']))}\nSMA 30: {fmt(str(snap['old_slow']))} → {fmt(str(snap['slow']))}"
                elif cmd == '/history':
                    rows = self.db.execute("SELECT ts,payload FROM futures_events WHERE event='CLOSE' ORDER BY id DESC LIMIT 10").fetchall()
                    from .trade_notifications import REASONS
                    from .display_amount import display_amount as fmt
                    text = '\n\n'.join(f"{when(r['ts'])} | {p.get('symbol','BTCUSDT')} | {p['direction']} {p['leverage']}×\nЧистый результат {fmt(p['net'],signed=True)}, комиссии {fmt(p['fees'])}, funding {fmt(p['funding'],signed=True)} USDT | {REASONS.get(p['reason'], 'Не указана')}" for r in rows for p in [json.loads(r['payload'])]) or 'История пуста.'
                elif cmd == '/report':
                    from .telegram_reports import futures_daily_report
                    text = futures_daily_report(self)
                elif cmd == 'STALE_COMMAND':
                    text = 'Команда устарела, отправьте новую.'
                else:
                    from .user_text import FUTURES_HELP
                    text = FUTURES_HELP
                self.notify(text)
            if not routed:
                self.set('offset', update_id+1)

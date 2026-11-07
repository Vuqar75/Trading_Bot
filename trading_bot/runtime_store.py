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
    def __init__(self, path, identity, fault=lambda point: None, *, shared=False):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.fault = fault
        self.shared = None
        if shared:
            if identity.get('mode') != 'paper':raise ObservationError('Shared storage requires paper mode')
            from .paper_shared_store import SharedPaperStore
            self.shared = SharedPaperStore(path, fault=fault)
        self.db = self.shared.db if self.shared else sqlite3.connect(path, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.execute('PRAGMA synchronous=FULL')
        self.db.execute('PRAGMA foreign_keys=ON')
        try:
            with self.transaction():
                tables = {r[0] for r in self.db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")}
                expected_tables = {'runtime_meta', 'runtime_orders', 'runtime_fills', 'runtime_commands', 'runtime_outbox', 'runtime_closes'}
                if tables and (not expected_tables.issubset(tables) if self.shared else tables != expected_tables):
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
            self.close()
            raise

    def close(self):
        if self.shared:self.shared.close()
        else:self.db.close()

    @contextmanager
    def transaction(self):
        if self.shared:
            self.shared.fault=self.fault
            with self.shared.market_transaction('spot'):
                yield
            return
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
        if self.shared and not self.db.in_transaction:
            raise ObservationError('Shared state writes require a transaction')
        self.db.execute('INSERT OR REPLACE INTO runtime_meta VALUES(?,?)', (key, json.dumps(value, sort_keys=True)))

    def notify(self, message):
        self.db.execute('INSERT INTO runtime_outbox(message) VALUES(?)', (message,))

    def pending(self):
        return self.db.execute("SELECT * FROM runtime_orders WHERE phase!='SETTLED' ORDER BY created").fetchall()

    def _order_symbol(self, client, *, history=False):
        # Unannotated orders come from the legacy BTC-only implementation.
        # New pairs remain disabled until accounting and protection are wired.
        symbol=self.get('pair_order_symbol:'+client)
        if symbol is None:
            if self.get('pair_legacy_btc_migration') is not None:
                raise ObservationError('Migrated order symbol is missing')
            symbol='BTCUSDT'
        from .paper_wallet_ledger import valid_asset
        identity=self.get('identity') or {}
        if (not isinstance(symbol,str) or not symbol.endswith('USDT') or
                not valid_asset(symbol[:-4]) or symbol[:-4]=='USDT'):
            raise ObservationError('Invalid order symbol')
        if symbol!='BTCUSDT' and (identity.get('mode')!='paper' or identity.get('symbol')!=symbol):
            row=self.db.execute('SELECT phase FROM runtime_orders WHERE client_id=?',(client,)).fetchone() if history else None
            if identity.get('mode')!='paper' or row is None or row['phase']!='SETTLED':
                raise ObservationError('Order symbol requires unsupported pair recovery')
        return symbol

    @staticmethod
    def manual_request_valid(request, pos, now=None):
        if not isinstance(request, dict) or not isinstance(pos, dict) or request.get('target') != pos.get('id'):
            return False
        if 'quantity' not in request:  # Pre-existing non-unified commands.
            return True
        try:
            return number(request['quantity']) == number(pos['quantity']) and number(pos['quantity']) > 0 and (time.time() if now is None else now) < request['expires']
        except (KeyError, TypeError, ValueError, ObservationError):
            return False

    def loss(self):
        start = int(time.time() // 86400) * 86400
        rows = self.db.execute('SELECT pnl,known FROM runtime_closes WHERE ts>=?', (start,)).fetchall()
        if any(not r['known'] for r in rows):
            raise ObservationError('Daily PnL contains unvalued fees')
        return -sum((Decimal(r['pnl']) for r in rows), Decimal(0))

    def prepare(self, side, quantity, price, business_key, reason, *, target=None, daily_limit=Decimal('5'), quote_at=None, expected_symbol=None, expected_pair_version=None):
        with self.transaction():
            if self.db.execute('SELECT 1 FROM runtime_orders WHERE business_key=?', (business_key,)).fetchone():
                return None
            if self.pending() or self.get('blocked'):
                raise ObservationError('Recovery or review required')
            pos = self.get('position')
            identity=self.get('identity')
            if expected_symbol is not None and identity.get('symbol','BTCUSDT')!=expected_symbol:
                raise ObservationError('Spot pair version superseded')
            if expected_pair_version is not None and self.get('pair_selection_version',0)!=expected_pair_version:
                raise ObservationError('Spot pair version superseded')
            if reason in ('DONCHIAN60_ENTRY','DONCHIAN60_EXIT'):
                state=self.get('donchian60',{});stamp=time.time()
                if (isinstance(quote_at,bool) or not isinstance(quote_at,(int,float)) or not 0<=stamp-quote_at<=5 or
                    not isinstance(state,dict) or state.get('phase')!='executing' or
                    type(state.get('quote_trade_ms')) is not int or not 0<=stamp*1000-state['quote_trade_ms']<=5000 or
                    stamp*1000>state.get('boundary_ms',0)+300000):
                    raise ObservationError('Donchian execution window or quote expired after database wait')
            if identity.get('symbol','BTCUSDT')!='BTCUSDT':
                if (isinstance(quote_at,bool) or not isinstance(quote_at,(int,float)) or
                        not 0<=time.time()-quote_at<=120):
                    raise ObservationError('Pair quote stale after database wait')
            qty = number(quantity)
            if not qty:
                raise ObservationError('Zero quantity')
            check_side={'SPOT_CHECK_BUY':'BUY','SPOT_CHECK_SELL':'SELL'}.get(reason)
            if check_side:
                if side!=check_side or identity.get('mode')!='paper' or identity.get('symbol','BTCUSDT')!='BTCUSDT':raise ObservationError('Check requires BTCUSDT paper')
                from .spot_check_guard import validate
                try:validate(self.get('spot_test_cycle'),side=side,business_key=business_key,quantity=qty,price=price,quote_at=quote_at,now_ms=int(time.time()*1000))
                except ValueError as exc:raise ObservationError(str(exc)) from None
                if side=='SELL' and (self.get('spot_test_cycle') or {}).get('position_id')!=target:raise ObservationError('Check position changed')
            if side == 'BUY':
                if self.shared and self.shared.deployment_hold():raise ObservationError('Paper deployment hold active')
                pilot=self.get('spot_pilot')
                if isinstance(pilot,dict) and pilot.get('phase') in ('active','awaiting_check','checking','ready_to_start') and reason not in ('PILOT1H_ENTRY','SPOT_CHECK_BUY'):
                    raise ObservationError('Pilot prohibits other automatic entries')
                if reason=='PILOT1H_ENTRY':
                    if identity.get('mode')!='paper' or identity.get('symbol','BTCUSDT')!='BTCUSDT':raise ObservationError('Pilot requires BTCUSDT paper')
                    from .spot_pilot_limits import validate_intent
                    try:validate_intent(pilot,quote_at=quote_at,now_ms=int(time.time()*1000))
                    except ValueError as exc:raise ObservationError(str(exc)) from None
                if (self.get('paused') and not check_side) or number(pos['quantity']) or not pos['fees_known'] or self.loss() >= daily_limit:
                    raise ObservationError('Entry not allowed')
                if qty * price * Decimal('1.002') > number(self.get('cash')):
                    raise ObservationError('Insufficient budget')
            elif side == 'SELL':
                if target != pos['id'] or qty > number(pos['quantity']):
                    raise ObservationError('Stale or excessive close')
                if reason in ('PILOT1H_STOP','PILOT1H_TIME','PILOT1H_SIGNAL'):
                    from .spot_pilot_limits import validate_exit
                    try:validate_exit(self.get('spot_pilot'),pos,reason=reason,price=price,quote_at=quote_at,now_ms=int(time.time()*1000))
                    except ValueError as exc:raise ObservationError(str(exc)) from None
                if reason == 'MANUAL' and not self.manual_request_valid(self.get('close_request'), pos):
                    raise ObservationError('Confirmed close is stale')
            else:
                raise ObservationError('Invalid side')
            client = 'rt' + uuid.uuid4().hex
            self.db.execute('INSERT INTO runtime_orders VALUES(?,?,?,?,?,\'PREPARED\',NULL,?,?)',
                            (client, business_key, side, str(qty), str(price), reason, time.time()))
            symbol=self.get('identity').get('symbol','BTCUSDT')
            self.set('pair_order_symbol:' + client, symbol)
            self._order_symbol(client)
            if symbol!='BTCUSDT' or reason in ('DONCHIAN60_ENTRY','DONCHIAN60_EXIT','PILOT1H_ENTRY','PILOT1H_STOP','PILOT1H_TIME','PILOT1H_SIGNAL','SPOT_CHECK_BUY','SPOT_CHECK_SELL'):self.set('pair_order_quote_at:'+client,quote_at)
            self.set('binding:' + client, pos['id'])
            if reason == 'MANUAL':
                self.set('manual_binding:' + client, self.get('close_request'))
            return client

    def claim(self, client, *, expected_symbol=None, expected_pair_version=None):
        with self.transaction():
            order = self.db.execute('SELECT * FROM runtime_orders WHERE client_id=?', (client,)).fetchone()
            if order is None or order['phase'] != 'PREPARED':
                raise ObservationError('Order not eligible to send')
            self._order_symbol(client)
            if expected_symbol is not None and self._order_symbol(client)!=expected_symbol:
                raise ObservationError('Spot pair version superseded')
            if expected_pair_version is not None and self.get('pair_selection_version',0)!=expected_pair_version:
                raise ObservationError('Spot pair version superseded')
            if order['reason'] in ('DONCHIAN60_ENTRY','DONCHIAN60_EXIT'):
                observed=self.get('pair_order_quote_at:'+client);state=self.get('donchian60',{});stamp=time.time()
                if (isinstance(observed,bool) or not isinstance(observed,(int,float)) or not 0<=stamp-observed<=5 or
                    not isinstance(state,dict) or state.get('phase')!='executing' or type(state.get('quote_trade_ms')) is not int or
                    not 0<=stamp*1000-state['quote_trade_ms']<=5000 or stamp*1000>state.get('boundary_ms',0)+300000):
                    self.db.execute("UPDATE runtime_orders SET phase='SETTLED',status='EXPIRED' WHERE client_id=?",(client,))
                    return None
            if order['side']=='BUY' and order['reason']=='PILOT1H_ENTRY':
                from .spot_pilot_limits import validate_intent
                try:validate_intent(self.get('spot_pilot'),quote_at=self.get('pair_order_quote_at:'+client),now_ms=int(time.time()*1000))
                except ValueError:
                    self.db.execute("UPDATE runtime_orders SET phase='SETTLED',status='EXPIRED' WHERE client_id=?",(client,))
                    return None
            check_side={'SPOT_CHECK_BUY':'BUY','SPOT_CHECK_SELL':'SELL'}.get(order['reason'])
            if check_side:
                from .spot_check_guard import validate
                try:validate(self.get('spot_test_cycle'),side=check_side,business_key=order['business_key'],quantity=order['quantity'],price=order['price'],quote_at=self.get('pair_order_quote_at:'+client),now_ms=int(time.time()*1000))
                except ValueError:
                    self.db.execute("UPDATE runtime_orders SET phase='SETTLED',status='EXPIRED' WHERE client_id=?",(client,))
                    return None
            if self._order_symbol(client)!='BTCUSDT':
                observed=self.get('pair_order_quote_at:'+client)
                if (isinstance(observed,bool) or not isinstance(observed,(int,float)) or
                        not 0<=time.time()-observed<=120):
                    self.db.execute("UPDATE runtime_orders SET phase='SETTLED',status='EXPIRED' WHERE client_id=?",(client,))
                    self.notify('Котировка пары устарела до отправки; намерение отменено.')
                    return None
            pos = self.get('position')
            if self.get('blocked') or self.get('binding:' + client) != pos['id']:
                raise ObservationError('Position changed or recovery required')
            if check_side=='SELL' and (self.get('spot_test_cycle') or {}).get('position_id')!=pos['id']:
                raise ObservationError('Check position changed')
            if order['side']=='SELL' and order['reason'] in ('PILOT1H_STOP','PILOT1H_TIME','PILOT1H_SIGNAL'):
                from .spot_pilot_limits import validate_exit
                try:validate_exit(self.get('spot_pilot'),pos,reason=order['reason'],price=order['price'],quote_at=self.get('pair_order_quote_at:'+client),now_ms=int(time.time()*1000))
                except ValueError:
                    self.db.execute("UPDATE runtime_orders SET phase='SETTLED',status='EXPIRED' WHERE client_id=?",(client,))
                    return None
            pilot=self.get('spot_pilot')
            if (order['side']=='BUY' and isinstance(pilot,dict) and pilot.get('phase') in ('active','awaiting_check','checking','ready_to_start') and order['reason'] not in ('PILOT1H_ENTRY','SPOT_CHECK_BUY')):
                self.db.execute("UPDATE runtime_orders SET phase='SETTLED',status='EXPIRED' WHERE client_id=?",(client,))
                return None
            if self.db.execute("SELECT 1 FROM runtime_orders WHERE client_id!=? AND phase!='SETTLED'", (client,)).fetchone():
                raise ObservationError('Another order is unresolved')
            if order['side'] == 'BUY' and ((self.get('paused') and not check_side) or number(pos['quantity'])):
                raise ObservationError('Entry no longer allowed')
            if order['side'] == 'BUY' and self.shared and self.shared.deployment_hold():
                raise ObservationError('Paper deployment hold active')
            if order['side'] == 'SELL' and number(order['quantity']) > number(pos['quantity']):
                raise ObservationError('Close quantity changed')
            if order['reason'] == 'MANUAL':
                request = self.get('manual_binding:' + client)
                if request != self.get('close_request') or not self.manual_request_valid(request, pos):
                    self.db.execute("UPDATE runtime_orders SET phase='SETTLED',status='EXPIRED' WHERE client_id=?", (client,))
                    if request == self.get('close_request'):
                        self.set('close_request', None)
                    self.notify('Подтверждение закрытия устарело или позиция изменилась; сделка не отправлена.')
                    return None
            self.db.execute("UPDATE runtime_orders SET phase='SUBMITTING' WHERE client_id=?", (client,))
        return dict(order)

    def apply(self, client, report):
        """Validate cumulative snapshot, then apply only unseen fills atomically."""
        with self.transaction():
            saved = self.db.execute('SELECT * FROM runtime_orders WHERE client_id=?', (client,)).fetchone()
            if saved is None or saved['phase'] not in {'SUBMITTING', 'UNKNOWN', 'ACK', 'SETTLED'}:
                raise ObservationError('Unexpected order state')
            order, fills = report['order'], report['fills']
            symbol=self._order_symbol(client)
            expected = (client, symbol, saved['side'], number(saved['quantity']))
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
                if f['symbol'] != symbol or str(f['orderId']) != str(order['orderId']) or f['isBuyer'] is not (saved['side'] == 'BUY'):
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
                base_asset=symbol[:-4]
                known = known and (fee == 0 or asset in {base_asset, 'USDT'})
                if saved['side'] == 'BUY':
                    received = q - (fee if asset == base_asset else 0)
                    if received <= 0:
                        raise ObservationError('Invalid entry fee')
                    debit = v + (fee if asset == 'USDT' else 0)
                    if base == 0:
                        pos = {**pos, 'id': uuid.uuid4().hex, 'opened_at': time.time()}
                        if symbol!='BTCUSDT':pos['symbol']=symbol
                        else:pos.pop('symbol',None)
                    cash -= debit
                    base += received
                    cost += debit
                    if saved['reason']=='PILOT1H_ENTRY':
                        from .spot_pilot_limits import record_entry
                        pilot=self.get('spot_pilot')
                        try:record_entry(pilot,client_id=client,position_id=pos['id'])
                        except ValueError as exc:raise ObservationError(str(exc)) from None
                        self.set('spot_pilot',pilot)
                else:
                    debit = q + (fee if asset == base_asset else 0)
                    if debit > base:
                        raise ObservationError('Exit exceeds owned position')
                    basis = cost * debit / base
                    credit = v - (fee if asset == 'USDT' else 0)
                    cash += credit
                    pnl += credit - basis
                    base -= debit
                    cost -= basis
                    pilot=self.get('spot_pilot')
                    if isinstance(pilot,dict) and pilot.get('open_position_id')==pos['id']:
                        from .spot_pilot_limits import record_close
                        try:record_close(pilot,position_id=pos['id'],fill_id=key,pnl=credit-basis,known=known,remaining=base)
                        except ValueError as exc:raise ObservationError(str(exc)) from None
                        self.set('spot_pilot',pilot)
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
            if saved['reason'] in ('SPOT_CHECK_BUY','SPOT_CHECK_SELL') and len(fills)>len(existing):
                check=self.get('spot_test_cycle')
                if not isinstance(check,dict) or check.get('authorization')!='owner_local_once':raise ObservationError('Check identity conflict')
                attempt=check.get(saved['side'].lower()+'_attempt',0)
                if type(attempt) is not int or not 0<=attempt<=3 or saved['business_key']!='paper-check:'+str(check.get('id'))+':'+saved['side'].lower()+(':'+str(attempt) if attempt else ''):raise ObservationError('Check identity conflict')
                if saved['side']=='BUY':
                    if check.get('phase')!='BUY_ARMED' or status!='FILLED':raise ObservationError('Check buy not confirmed')
                    check.update(phase='BUY_FILLED',position_id=pos['id'],quantity=str(base),buy_client_id=client,buy_time=time.time(),buy_price=str(quote/qty),buy_fee=str(sum((number(f['commission']) for f in fills),Decimal(0))))
                else:
                    if check.get('phase')!='SELL_ARMED' or check.get('position_id')!=pos['id']:raise ObservationError('Check sell identity conflict')
                    check.update(phase='COMPLETE' if status=='FILLED' and base==0 else 'REVIEW',sell_client_id=client,sell_time=time.time(),sell_price=str(quote/qty),sell_fee=str(sum((number(f['commission']) for f in fills),Decimal(0))),net_pnl=str(pnl) if known else None)
                self.set('spot_test_cycle',check)
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
                    reason=saved['reason'], interval=('1h' if saved['reason'].startswith('PILOT1H_') else self.get('selected_interval', self.get('identity').get('interval', '15m'))),
                    client=client, entry=entry.get('price'), entry_is_cost=entry.get('is_cost', False), entry_fees=entry.get('fees'),
                    net=self.get('order_net:' + client, '0'), known=known, symbol=symbol))

    def command(self, update_id, authorized, command, target=None, *, routed=False,expected_setting=None,expires=None):
        identity=[authorized,command,target]
        if expected_setting is not None:identity.extend([expected_setting,expires])
        fingerprint = hashlib.sha256(json.dumps(identity).encode()).hexdigest()
        with self.transaction():
            prior = self.db.execute('SELECT fingerprint FROM runtime_commands WHERE update_id=?', (update_id,)).fetchone()
            if prior:
                if prior[0] != fingerprint:
                    raise ObservationError('Update identity conflict')
                return
            if not routed and update_id < self.get('offset'):
                raise ObservationError('Unexpected older update')
            self.db.execute('INSERT INTO runtime_commands VALUES(?,?)', (update_id, fingerprint))
            if expected_setting is not None:
                actual=self.get('selected_interval',self.get('identity').get('interval','15m'))
                if (not routed or not command.startswith('/timeframe ') or type(expires) not in (int,float) or time.time()>=expires or actual!=expected_setting):
                    self.notify('Настройка изменилась или кнопка устарела; запросите новое меню.')
                    return
            if authorized:
                if command in {'/pause', '/stop', '/resume'}:
                    allowed = command == '/resume'
                    if allowed and (self.pending() or self.get('blocked') or not self.get('position')['fees_known'] or (self.shared and self.shared.deployment_hold())):
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
                    from .telegram_reports import history
                    message = history(self)
                elif command == '/report':
                    from .telegram_reports import daily_report
                    message = daily_report(self)
                elif command == '/status':
                    from .telegram_reports import status
                    message = status(self)
                elif command == '/signal':
                    from .telegram_reports import signal_report
                    message = signal_report(self)
                elif command.startswith('/timeframe'):
                    parts = command.split()
                    current = self.get('selected_interval', self.get('identity').get('interval', '15m'))
                    if self.get('spot_pilot') is not None:
                        message = 'Экспериментальный Spot-пилот: часовые свечи UTC; изменение интервала недоступно.'
                    elif self.get('donchian60') is not None:
                        message = 'Donchian60: дневные свечи UTC; изменение интервала недоступно в этом режиме.'
                    elif len(parts) == 1:
                        message = 'Таймфрейм: ' + current + '. Выбор: /timeframe 1m, 5m, 15m или 1h. Сначала /pause и закрытие позиции.'
                    elif len(parts) != 2 or parts[1] not in {'1m', '5m', '15m', '1h'}:
                        message = 'Используйте /timeframe 1m, /timeframe 5m, /timeframe 15m или /timeframe 1h.'
                    elif self.shared and self.shared.deployment_hold():
                        message = 'Изменение настроек недоступно до завершения проверки переноса.'
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
                    from .user_text import SPOT_HELP
                    message = SPOT_HELP
                self.notify(message)
            if not routed:
                self.set('offset', update_id + 1)

"""Runnable BTCUSDT Spot paper/Testnet application. No production trading host."""
import argparse
import hashlib
import hmac
import json
import logging
import os
import signal
import tempfile
import time
from contextlib import contextmanager
from decimal import Decimal
from pathlib import Path
from .core import signal_details
from .runtime_exchange import PaperExchange, TestnetExchange, market_snapshot, order_quantity
from .runtime_store import RuntimeStore
from .testnet_observer import ObservationError, SpotTestnetReader, number, write_report

log = logging.getLogger('runtime')


@contextmanager
def process_lock(path):
    import fcntl
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    with open(path, 'a') as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ObservationError('Another runtime owns this database') from None
        yield


class TelegramTransport:
    """Owner private-chat polling. Credentials come only from explicit env names."""
    def __init__(self, token, owner, transport=None, started=None):
        if not token or not owner or not owner.isdigit():
            raise ObservationError('Telegram token and numeric owner required')
        self.token, self.owner = token, owner
        self.transport = transport or SpotTestnetReader._http
        self.started = started if started is not None else time.time()

    def call(self, method, data):
        from urllib.request import Request
        request = Request('https://api.telegram.org/bot' + self.token + '/' + method,
            data=json.dumps(data).encode(), headers={'Content-Type': 'application/json'}, method='POST')
        result = self.transport(request)
        if result.get('ok') is not True:
            raise ObservationError('Telegram request not acknowledged')
        return result['result']

    def poll(self, store):
        updates = self.call('getUpdates', {'offset': store.get('offset'), 'timeout': 0, 'limit': 20, 'allowed_updates': ['message']})
        previous = -1
        for update in updates:
            ident = update.get('update_id')
            if type(ident) is not int or ident <= previous:
                raise ObservationError('Invalid Telegram update ordering')
            previous = ident
            msg = update.get('message', {})
            authorized = (hmac.compare_digest(str(msg.get('from', {}).get('id', '')), self.owner)
                and hmac.compare_digest(str(msg.get('chat', {}).get('id', '')), self.owner)
                and msg.get('chat', {}).get('type') == 'private')
            text = msg.get('text', '') if authorized else ''
            command = text.split()[0].split('@')[0] if isinstance(text, str) and text.split() else ''
            if authorized and command in {'/timeframe', '/leverage'} and isinstance(text, str):
                command = ' '.join([command] + text.split()[1:])
            if authorized and ((len(text.split()) != 1 and not command.startswith(('/timeframe ', '/leverage '))) or 'edit_date' in msg):
                command = ''
            if command in {'/resume', '/close'} or command.startswith(('/timeframe ', '/leverage ')):
                stamp = msg.get('date', 0)
                if type(stamp) is not int or stamp < int(self.started) or not 0 <= time.time() - stamp <= 120:
                    command = 'STALE_COMMAND'
                elif command == '/close' and stamp < int((store.get('position') or {}).get('opened_at', 0)):
                    command = 'STALE_COMMAND'
            # Command, intent request, response and offset share one durable commit.
            # On DB error stop here: never poll with a higher offset.
            store.command(ident, authorized, command, (store.get('position') or {}).get('id') if command == '/close' else None)

    def flush(self, store):
        for row in store.db.execute('SELECT id,message FROM runtime_outbox ORDER BY id LIMIT 5').fetchall():
            self.call('sendMessage', {'chat_id': self.owner, 'text': row['message'][:4000]})
            with store.transaction():
                store.db.execute('DELETE FROM runtime_outbox WHERE id=?', (row['id'],))


class Application:
    def __init__(self, store, exchange, reader, *, interval='15m', amount='10', daily_loss='5', stop='2', take='4'):
        if store.get('identity')['mode'] != exchange.mode:
            raise ObservationError('Exchange and database mode disagree')
        self.store, self.exchange, self.reader = store, exchange, reader
        self.interval = interval
        self.amount, self.daily_loss, self.stop, self.take = map(number, (amount, daily_loss, stop, take))
        if not 0 < self.amount <= 10 or not self.daily_loss or not 0 < self.stop < 100 or not self.take:
            raise ObservationError('Invalid risk limits (maximum order budget 10 USDT)')
        with store.transaction():
            store.set('paused', True)  # Restart never resumes entry automatically.
            for row in store.db.execute("SELECT client_id FROM runtime_orders WHERE phase='PREPARED'").fetchall():
                store.db.execute("UPDATE runtime_orders SET phase='SETTLED',status='EXPIRED' WHERE client_id=?", (row[0],))
            if store.pending():
                store.set('blocked', 'RECOVERY_REQUIRED')

    def recover(self):
        for row in self.store.pending():
            report = (self.exchange.inspect_saved(row) if self.exchange.mode == 'paper'
                      else self.exchange.inspect(row['client_id']))
            try:
                self.store.apply(row['client_id'], report)
            except Exception:
                with self.store.transaction():
                    self.store.set('blocked', 'ACCOUNTING_CONTRADICTION')
                raise
        if self.store.get('blocked') not in {'ACCOUNTING_CONTRADICTION', 'ORDER_REJECTED_REVIEW'}:
            with self.store.transaction():
                self.store.set('blocked', 'ORDER_PENDING' if self.store.pending() else None)

    def enable_paper_entries(self):
        """Explicit local operator action; startup and restarts remain paused."""
        if self.exchange.mode != 'paper':
            raise ObservationError('Local entry activation is paper only')
        with self.store.transaction():
            if self.store.pending() or self.store.get('blocked') or not self.store.get('position')['fees_known']:
                raise ObservationError('Recovery or fee review required')
            self.store.set('paused', False)

    def execute(self, side, quantity, price, key, reason, target=None):
        if side == 'BUY' and number(quantity) * price > self.amount:
            raise ObservationError('Entry exceeds configured order budget')
        client = self.store.prepare(side, quantity, price, key, reason, target=target, daily_limit=self.daily_loss)
        if client is None:
            return
        self.store.claim(client)  # Durable BEFORE the only submit attempt.
        try:
            self.exchange.submit(client, side, quantity)
        except Exception:
            with self.store.transaction():
                self.store.db.execute("UPDATE runtime_orders SET phase='UNKNOWN' WHERE client_id=?", (client,))
                self.store.set('blocked', 'SUBMISSION_UNKNOWN')
            return
        # Do not trust submit response for settlement: query by saved client ID.
        self.recover()

    def protective_close(self):
        # Protection uses a fresh ticker even when candle loading is unavailable.
        pos = self.store.get('position')
        qty = number(pos['quantity'])
        if not qty or self.store.pending() or self.store.get('blocked'):
            return False
        quote = self.reader.get('/api/v3/ticker/price', {'symbol': 'BTCUSDT'})
        if quote.get('symbol') != 'BTCUSDT':
            raise ObservationError('Quote symbol mismatch')
        price = number(quote['price'])
        if not price:
            raise ObservationError('Zero quote')
        request = self.store.get('close_request')
        movement = (price / (number(pos['cost']) / qty) - 1) * 100
        reason = ('MANUAL' if request and request['target'] == pos['id'] else
                  'STOP' if movement <= -self.stop else 'TAKE' if movement >= self.take else None)
        if not reason:
            return False
        info = self.reader.get('/api/v3/exchangeInfo', {'symbol': 'BTCUSDT'})['symbols'][0]
        if self.exchange.mode == 'testnet':
            known = {r[0] for r in self.store.db.execute('SELECT client_id FROM runtime_orders')}
            if any(o['clientOrderId'] not in known for o in self.exchange.open_orders()):
                raise ObservationError('External orders require review')
            if self.exchange.balances().get('BTC', Decimal(0)) < qty:
                raise ObservationError('Insufficient owned BTC')
        quantity = order_quantity(info, qty, price)
        # Bound retries to an explicit command or time bucket. An ambiguous submit
        # leaves a pending intent and can only be queried, never submitted again.
        suffix = str(request['update_id']) if reason == 'MANUAL' else str(int(time.time() // 60))
        self.execute('SELL', quantity, price, 'risk:' + pos['id'] + ':' + suffix, reason, pos['id'])
        with self.store.transaction():
            self.store.set('quote', {'price': str(price), 'time': time.time()})
            self.store.set('close_request', None)
            self.store.set('error', None)
        return True

    def tick(self):
        self.interval = self.store.get('selected_interval', self.interval)
        self.recover()
        if self.protective_close():
            return
        price, candles, server = market_snapshot(self.reader, self.interval)
        info = self.reader.get('/api/v3/exchangeInfo', {'symbol': 'BTCUSDT'})['symbols'][0]
        if self.exchange.mode == 'testnet':
            # Unexpected open orders or insufficient holdings require review, never
            # silently adopt positions created outside this dedicated runtime.
            known = {r[0] for r in self.store.db.execute('SELECT client_id FROM runtime_orders')}
            if any(o['clientOrderId'] not in known for o in self.exchange.open_orders()):
                raise ObservationError('External open orders require review')
            balances = self.exchange.balances()
            if not self.store.pending() and balances.get('BTC', Decimal(0)) < number(self.store.get('position')['quantity']):
                raise ObservationError('Account BTC below owned position')
            available = balances.get('USDT', Decimal(0))
        else:
            available = number(self.store.get('cash'))
        pos = self.store.get('position')
        stamp = candles[-1][0]
        details = signal_details([float(p) for _, p in candles])
        with self.store.transaction():
            self.store.set('quote', {'price': str(price), 'time': time.time()})
            self.store.set('signal', {**details, 'interval': self.interval, 'candle': stamp, 'time': time.time()})
            self.store.set('error', None)
        if self.store.pending() or self.store.get('blocked'):
            return
        request = self.store.get('close_request')
        quantity = number(pos['quantity'])
        fresh = self.store.get('last_candle') != stamp
        close_reason = None
        if request and request['target'] == pos['id'] and quantity:
            close_reason = 'MANUAL'
        if quantity:
            entry = number(pos['cost']) / quantity
            movement = (price / entry - 1) * 100
            close_reason = close_reason or ('STOP' if movement <= -self.stop else 'TAKE' if movement >= self.take else 'SIGNAL' if fresh and details['action'] == 'SELL' else None)
        if close_reason:
            q = order_quantity(info, quantity, price)
            key = 'close:' + pos['id'] + ':' + (str(request['update_id']) if request and close_reason == 'MANUAL' else str(stamp))
            self.execute('SELL', q, price, key, close_reason, pos['id'])
        elif fresh and not quantity and details['action'] == 'BUY' and not self.store.get('paused'):
            if self.amount * Decimal('1.002') > available:
                raise ObservationError('Insufficient exchange balance')
            q = order_quantity(info, self.amount / price, price)
            self.execute('BUY', q, price, 'entry:' + str(stamp), 'SIGNAL')
        with self.store.transaction():
            self.store.set('last_candle', stamp)
            if request:
                self.store.set('close_request', None)


def demo():
    """Offline end-to-end run, never reads credentials or contacts a service."""
    with tempfile.TemporaryDirectory() as folder:
        store = RuntimeStore(Path(folder) / 'demo.db', {'mode': 'paper'})
        try:
            app = Application(store, PaperExchange(), None)
            store.command(1, True, '/resume')
            app.execute('BUY', '0.0001', Decimal('70000'), 'demo-entry', 'DEMO')
            pos = store.get('position')
            store.command(2, True, '/close', pos['id'])
            app.execute('SELL', pos['quantity'], Decimal('71000'), 'demo-close', 'DEMO', pos['id'])
            print(json.dumps({'mode': 'OFFLINE_DEMO', 'orders': store.db.execute('SELECT COUNT(*) FROM runtime_orders').fetchone()[0],
                'position': store.get('position')['quantity'], 'net_pnl': store.get('pnl'),
                'pending': len(store.pending())}, indent=2))
        finally:
            store.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--demo', action='store_true')
    parser.add_argument('--mode', choices=['paper', 'testnet'], default='paper')
    parser.add_argument('--db', default='data/runtime-paper.db')
    parser.add_argument('--interval', choices=['1m', '5m', '15m', '1h'], default='15m')
    parser.add_argument('--poll-seconds', type=int, default=10)
    parser.add_argument('--amount', default='10')
    parser.add_argument('--enable-testnet-orders', action='store_true')
    parser.add_argument('--telegram', action='store_true')
    parser.add_argument('--once', action='store_true')
    parser.add_argument('--allow-paper-signal', action='store_true', help='Allow explicit SIGUSR1 activation of paper entries')
    args = parser.parse_args()
    if args.demo:
        demo()
        return
    if args.poll_seconds < 5:
        parser.error('poll-seconds must be at least 5')
    if args.allow_paper_signal and (args.mode != 'paper' or args.enable_testnet_orders or args.once):
        parser.error('paper signal activation requires continuous paper mode without Testnet orders')
    if args.mode == 'testnet' and not args.enable_testnet_orders:
        parser.error('Testnet mode requires --enable-testnet-orders; still starts paused')
    os.umask(0o077)
    args.db = str(Path(args.db).resolve())
    key = os.getenv('BINANCE_TESTNET_API_KEY') if args.mode == 'testnet' else None
    secret = os.getenv('BINANCE_TESTNET_API_SECRET') if args.mode == 'testnet' else None
    exchange = TestnetExchange(key, secret, enabled=True) if args.mode == 'testnet' else PaperExchange()
    reader = exchange if args.mode == 'testnet' else SpotTestnetReader()
    identity = {'schema': 1, 'mode': args.mode, 'symbol': 'BTCUSDT', 'interval': args.interval, 'amount': args.amount,
                'account': hashlib.sha256(key.encode()).hexdigest() if key else 'paper'}
    telegram = TelegramTransport(os.getenv('RUNTIME_TELEGRAM_BOT_TOKEN'), os.getenv('RUNTIME_TELEGRAM_OWNER_ID')) if args.telegram else None
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    running = True
    activate = False
    def stop(*_):
        nonlocal running
        running = False
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    def activate_paper(*_):
        nonlocal activate
        activate = True
    if args.allow_paper_signal:
        signal.signal(signal.SIGUSR1, activate_paper)
    with process_lock(args.db + '.lock'):
        store = RuntimeStore(args.db, identity)
        try:
            app = Application(store, exchange, reader, interval=args.interval, amount=args.amount)
            while running:
                if activate:
                    activate = False
                    try:
                        app.enable_paper_entries()
                    except ObservationError:
                        log.warning('Paper activation blocked by recovery or fee review')
                # Trading/risk loop runs before Telegram to avoid a failed poll
                # indefinitely delaying position checks.
                try:
                    app.tick()
                except Exception as exc:
                    with store.transaction():
                        store.set('paused', True)
                        store.set('error', {'stage': 'trading', 'type': type(exc).__name__, 'time': time.time(), 'reason': str(exc) if isinstance(exc, ObservationError) else 'Internal failure'})
                    log.error('Trading cycle failed: %s', type(exc).__name__)
                if telegram:
                    try:
                        telegram.poll(store)
                        telegram.flush(store)
                        with store.transaction():
                            store.set('telegram_error', None)
                    except Exception as exc:
                        with store.transaction():
                            store.set('telegram_error', {'type': type(exc).__name__, 'time': time.time()})
                        log.error('Telegram cycle failed: %s', type(exc).__name__)
                write_report(Path(args.db).parent / 'runtime-health.json', {'time': time.time(), 'mode': args.mode,
                    'paused': store.get('paused'), 'pending': len(store.pending()), 'error': store.get('error'),
                    'quote': store.get('quote'), 'blocked': store.get('blocked'), 'telegram_error': store.get('telegram_error')})
                if args.once:
                    if store.get('error'):
                        raise SystemExit(1)
                    return
                for _ in range(args.poll_seconds):
                    if not running:
                        break
                    time.sleep(1)
        finally:
            store.close()


if __name__ == '__main__':
    main()

"""Isolated BTCUSDT perpetual paper runtime. Public market data, virtual wallet."""
import argparse
from contextlib import ExitStack
import json
import logging
import os
import signal
import tempfile
import time
from decimal import Decimal
from pathlib import Path
from .app import TelegramTransport, process_lock
from .core import signal_details
from .futures_data import FuturesData, INTERVALS
from .futures_store import FuturesStore
from .testnet_observer import ObservationError, number, write_report


class FuturesApplication:
    def __init__(self, store, reader):
        self.store, self.reader = store, reader
        with store.transaction():
            store.set('paused', True)

    def tick(self):
        self._tick()
        quote = self.store.get('quote')
        self.store.record_equity(Decimal(quote['price']), quote['server_ms'])

    def _tick(self):
        s = self.store
        mark, server = self.reader.mark()
        pos = s.get('position')
        with s.transaction():
            s.set('quote', {'price': str(mark), 'time': time.time(), 'server_ms': server})
        if pos:
            # Fetch overlapping full position history: catch delayed publications
            # while still open. Closed positions are not retroactively corrected.
            events = self.reader.funding(pos['opened_ms']+1, server)
            s.apply_funding(pos['id'], events, server)
            pos = s.get('position')
            value = s.valuation(mark)
            movement = (mark/number(pos['entry'])-1)*100*(1 if pos['direction'] == 'LONG' else -1)
            request = s.get('close_request')
            if request and not s.manual_request_valid(request, pos):
                with s.transaction():
                    if s.get('close_request') == request:
                        s.set('close_request', None)
                        s.notify('Подтверждение закрытия устарело или позиция изменилась.')
                request = None
            reason = ('MODEL_LIQUIDATION' if value['isolated'] <= value['maintenance'] else
                      'MANUAL' if request and request['target'] == pos['id'] else
                      'STOP' if movement <= -2 else 'TAKE' if movement >= 4 else None)
            if reason:
                candle = (server//(INTERVALS[s.get('interval')]*1000)-1)*INTERVALS[s.get('interval')]*1000
                closed = s.close_position(mark, server, 'close:'+pos['id'], reason, pos['id'], candle=candle)
                if not closed and reason == 'MANUAL':
                    protective = ('MODEL_LIQUIDATION' if value['isolated'] <= value['maintenance'] else
                                  'STOP' if movement <= -2 else 'TAKE' if movement >= 4 else None)
                    if protective:
                        closed = s.close_position(mark, server, 'close:'+pos['id'], protective, pos['id'], candle=candle)
                if not closed:
                    return
                with s.transaction():
                    s.set('reverse', None)
                    s.set('error', None)
                return
        interval = s.get('interval')
        candles = self.reader.candles(interval, server)
        stamp = candles[-1][0]
        details = signal_details([float(p) for _, p in candles])
        fresh = s.get('last_candle') != stamp
        reason = 'Нового пересечения нет.'
        direction = 'LONG' if details['action'] == 'BUY' else 'SHORT' if details['action'] == 'SELL' else None
        pos = s.get('position')
        if fresh and pos and direction and direction != pos['direction']:
            s.close_position(mark, server, 'close:'+pos['id'], 'REVERSAL', pos['id'], reverse={'direction': direction, 'candle': stamp}, candle=stamp)
            reason = 'Обратное пересечение: позиция закрыта. Противоположный вход возможен со следующей свечи.'
        elif fresh and not pos:
            reverse = s.get('reverse')
            if reverse and stamp > reverse['candle']:
                trend = 'LONG' if details['fast'] > details['slow'] else 'SHORT' if details['fast'] < details['slow'] else None
                direction = reverse['direction'] if trend == reverse['direction'] else direction
            if direction:
                opened = s.open_position(direction, mark, server, f'open:{stamp}:{direction}')
                reason = f'Виртуальный вход {direction}.' if opened else 'Вход пропущен: пауза, позиция, дневной лимит или недостаток маржи.'
            with s.transaction():
                s.set('reverse', None)
        elif not fresh:
            reason = 'Свеча уже обработана. Повторного входа нет.'
        with s.transaction():
            s.set('signal', {**details, 'candle': stamp, 'time': time.time(), 'reason': reason})
            s.set('last_candle', stamp)
            s.set('error', None)


def demo():
    with tempfile.TemporaryDirectory() as directory:
        store = FuturesStore(Path(directory)/'futures.db', slippage_bps='0')
        try:
            store.command(1, True, '/leverage 2')
            store.command(2, True, '/resume')
            store.open_position('SHORT', Decimal(100), 1000, 'demo-open')
            pos = store.get('position')
            store.apply_funding(pos['id'], [{'time': 2000, 'rate': '.001', 'mark': '100'}], 3000)
            store.close_position(Decimal(98), 3000, 'demo-close', 'DEMO', pos['id'])
            print(json.dumps({'mode': 'FUTURES_PAPER_OFFLINE_DEMO', 'position': store.get('position'),
                'net_pnl': store.get('pnl'), 'cash': store.get('cash'),
                'events': store.db.execute('SELECT COUNT(*) FROM futures_events').fetchone()[0]}, indent=2))
        finally:
            store.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--demo', action='store_true')
    parser.add_argument('--db', default='data/futures-paper.db')
    parser.add_argument('--once', action='store_true')
    parser.add_argument('--telegram', action='store_true')
    parser.add_argument('--poll-seconds', type=int, default=10)
    parser.add_argument('--margin', default='10')
    parser.add_argument('--fee-bps', default='5')
    parser.add_argument('--slippage-bps', default='2')
    parser.add_argument('--maintenance-bps', default='50')
    parser.add_argument('--allow-paper-signal', action='store_true')
    parser.add_argument('--shared-paper', action='store_true', help='Use an offline consolidated paper database (experimental)')
    args = parser.parse_args()
    if args.demo:
        demo()
        return
    if args.poll_seconds < 5 or (args.once and args.allow_paper_signal):
        parser.error('poll-seconds >=5; signal activation requires continuous mode')
    os.umask(0o077)
    db = str(Path(args.db).resolve())
    telegram = TelegramTransport(os.getenv('FUTURES_TELEGRAM_BOT_TOKEN'), os.getenv('FUTURES_TELEGRAM_OWNER_ID')) if args.telegram else None
    running, activate = True, False
    def stop(*_):
        nonlocal running
        running = False
    def request_activate(*_):
        nonlocal activate
        activate = True
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    if args.allow_paper_signal:
        signal.signal(signal.SIGUSR1, request_activate)
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    with ExitStack() as locks:
        locks.enter_context(process_lock(db+('.futures.lock' if args.shared_paper else '.lock')))
        if telegram:
            locks.enter_context(process_lock(db+'.delivery.lock'))
        store = FuturesStore(db, margin=args.margin, fee_bps=args.fee_bps,
                             slippage_bps=args.slippage_bps, maintenance_bps=args.maintenance_bps, shared=args.shared_paper)
        unified = None
        receiver = None
        worker_started = time.time()
        try:
            unified_path = os.getenv('UNIFIED_PAPER_QUEUE')
            if unified_path:
                if telegram:
                    raise ObservationError('Unified futures must not poll Telegram')
                from .unified_queue import UnifiedQueue
                from .unified_receiver import MarketReceiver
                unified = UnifiedQueue(unified_path)
                receiver = MarketReceiver(db, 'futures')
            app = FuturesApplication(store, FuturesData())
            while running:
                try:
                    app.tick()
                except Exception as exc:
                    with store.transaction():
                        store.set('paused', True)
                        store.set('error', {'type': type(exc).__name__, 'reason': str(exc) if isinstance(exc, ObservationError) else 'Internal failure', 'time': time.time()})
                    logging.error('Futures cycle failed: %s', type(exc).__name__)
                if unified:
                    try:
                        from .unified_worker import process_market
                        process_market(unified, receiver, store, 'futures', started=worker_started)
                    except Exception as exc:
                        logging.error('Futures command queue failed: %s', type(exc).__name__)
                if activate:
                    activate = False
                    if not store.get('error') and not store.get('blocked') and not (store.shared and store.shared.deployment_hold()):
                        with store.transaction():
                            store.set('paused', False)
                if telegram:
                    try:
                        telegram.poll(store)
                        telegram.flush(store)
                    except Exception as exc:
                        logging.error('Telegram cycle failed: %s', type(exc).__name__)
                write_report(Path(db).parent/'futures-health.json', {
                    'time': time.time(), 'mode': 'futures-paper', 'paused': store.get('paused'),
                    'error': store.get('error'), 'quote': store.get('quote'), 'position': store.get('position'),
                    'cash': store.get('cash'), 'pnl': store.get('pnl'), 'equity': store.get('equity'),
                    'max_drawdown': store.get('max_drawdown')})
                if args.once:
                    if store.get('error'):
                        raise SystemExit(1)
                    return
                for _ in range(args.poll_seconds):
                    if not running:
                        break
                    time.sleep(1)
        finally:
            if receiver:
                receiver.close()
            if unified:
                unified.close()
            store.close()


if __name__ == '__main__':
    main()

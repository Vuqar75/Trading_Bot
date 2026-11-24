"""Observe one accepted plan without writing to the trading ledger."""

import argparse
import json
import os
import sqlite3
from contextlib import closing
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR
from pathlib import Path
from queue import Empty, Full, Queue
from threading import Thread

from .ai_limit_plan import canonical, dec, quote, text

MAX_BYTES = 24 * 1024 * 1024
MAX_TARGET_TRADES = 256
MAX_GAP_MS = 5000
END_GRACE_MS = 30000


class Capture:
    """One private, size-bounded journal. Only the background thread writes it."""

    def __init__(self, path):
        self.path = Path(path)
        self.queue = Queue(maxsize=256)
        self.plan = None
        self.used = self.path.exists()
        self.complete = False
        self.error = None
        self.thread = None

    def start(self, plan, armed_ms):
        if self.used:
            return
        self.used = True
        self.plan = plan
        self.armed_ms = armed_ms
        self.end_ms = plan['expires_ms'] + plan['hold_ms'] + END_GRACE_MS
        self.submit({'plan': {key: plan[key] for key in (
            'plan_id', 'symbol', 'direction', 'entry', 'stop', 'target', 'quantity',
            'fee', 'funding_buffer_rate', 'stop_slippage', 'expires_ms', 'hold_ms')},
            'filters': plan['snapshot']['filters'], 'armed_ms': armed_ms})
        self.thread = Thread(target=self.write, daemon=True, name='paper-ioc-shadow')
        try:
            self.thread.start()
        except RuntimeError:
            self.error = 'writer_unavailable'
            self.thread = None

    def submit(self, row):
        if self.error:
            return
        try:
            self.queue.put_nowait(row)
        except Full:
            self.error = 'queue_full'

    def write(self):
        size = 0
        try:
            with self.path.open('x', encoding='utf-8') as output:
                while True:
                    try:
                        row = self.queue.get(timeout=1)
                    except Empty:
                        if self.error:
                            break
                        continue
                    if row is None:
                        break
                    line = canonical(row) + '\n'
                    size += len(line)
                    if size > MAX_BYTES:
                        self.error = 'size_limit'
                        break
                    output.write(line)
                    if row.get('complete'):
                        output.flush()
                        os.fsync(output.fileno())
                        break
        except OSError:
            self.error = 'journal_unavailable'

    def target_trades(self, events):
        plan = self.plan
        direction = 1 if plan['direction'] == 'LONG' else -1
        target = dec(plan['target'])
        matches = []
        for event in events:
            if (event['buyer_maker'] == (direction == -1)
                    and direction * (dec(event['price']) - target) > 0):
                matches.append(event)
                if len(matches) > MAX_TARGET_TRADES:
                    return None
        return matches

    def stop_observations(self, components, now):
        plan = self.plan
        side = 1 if plan['direction'] == 'LONG' else -1
        stop = dec(plan['stop'])
        stamps = []
        for component in components:
            published = component['source_ms']
            event = component['event_ms']
            received = component['received_ms']
            if not now - MAX_GAP_MS <= min(published, event) <= max(published, event) <= received <= now:
                continue
            key = 'p' if component['kind'] == 'mark' else ('b' if side == 1 else 'a')
            if side * (dec(component['source'][key]) - stop) <= 0:
                stamps.append(min(published, event))
        return stamps

    def observe(self, now, confirmed_quote, trades, healthy, stops=(), filters_ready=False):
        if self.plan is None or self.error or self.complete:
            return
        self.submit({'at_ms': now, 'quote': confirmed_quote, 'target_trades': trades,
                     'healthy': healthy and trades is not None, 'stop_stamps': stops,
                     'filters_ready': filters_ready})
        if now >= self.end_ms and not self.error:
            self.submit({'complete': True})
            self.complete = self.error is None

    def close(self):
        if self.thread is not None:
            self.submit(None)
            self.thread.join(timeout=1)


def entry(header, book, now, stress):
    plan, filters = header['plan'], header['filters']
    quote(book, plan['symbol'], now, entry=True)
    side = 1 if plan['direction'] == 'LONG' else -1
    key = 'ask' if side == 1 else 'bid'
    tick, qty = dec(filters['tick_size']), dec(plan['quantity'])
    raw = dec(book[key]) * (1 + side * stress)
    rounding = ROUND_CEILING if side == 1 else ROUND_FLOOR
    price = (raw / tick).to_integral_value(rounding=rounding) * tick
    stop, target, fee, buffer = map(dec, (plan['stop'], plan['target'], plan['fee'], plan['funding_buffer_rate']))
    stop_fill = stop * (1 - side * dec(plan['stop_slippage']))
    loss = side * (price - stop_fill) + fee * (price + stop_fill) + price * buffer
    reward = side * (target - price) - fee * (price + target) - price * buffer
    if not (side * (price - stop) > 0 and side * (target - price) > 0 and loss > 0
            and reward >= dec('1.5') * loss and qty * loss <= 5
            and qty >= dec(filters['min_qty']) and qty % dec(filters['step_size']) == 0
            and dec(filters['min_notional']) <= qty * price <= 100 and qty <= dec(book[key + '_qty'])):
        return None
    return price


def replay(header, rows, funding, stress):
    plan = header['plan']
    side = 1 if plan['direction'] == 'LONG' else -1
    qty, fee, stop, target, slip = map(dec, (
        plan['quantity'], plan['fee'], plan['stop'], plan['target'], plan['stop_slippage']))
    opened = None
    previous = header['armed_ms']
    for row in rows:
        if row.get('complete'):
            break
        now, book = row['at_ms'], row['quote']
        if not 0 <= now - previous <= MAX_GAP_MS or not row['healthy'] or book is None:
            return {'status': 'unknown', 'reason': 'market_observation_gap'}
        previous = now
        try:
            quote(book, plan['symbol'], now)
        except (KeyError, ValueError, TypeError):
            return {'status': 'unknown', 'reason': 'quote_unconfirmed'}
        if opened is None:
            if now >= plan['expires_ms']:
                return {'status': 'no_entry', 'reason': 'entry_expired'}
            if book['received_ms'] <= header['armed_ms']:
                continue
            if not row['filters_ready']:
                return {'status': 'unknown', 'reason': 'entry_filters_unconfirmed'}
            try:
                price = entry(header, book, now, stress)
            except (KeyError, ValueError, TypeError):
                return {'status': 'unknown', 'reason': 'entry_quote_unconfirmed'}
            if price is None:
                return {'status': 'no_entry', 'reason': 'first_quote_outside_limits'}
            opened = now
        key = 'bid' if side == 1 else 'ask'
        exit_price = dec(book[key])
        reason = None
        closed = now
        if (any(opened <= stamp <= now for stamp in row['stop_stamps'])
                or side * (dec(book['mark']) - stop) <= 0 or side * (exit_price - stop) <= 0):
            reason = 'SL'
        elif now >= opened + plan['hold_ms']:
            reason = 'TIME'
        target_trades = [trade for trade in row['target_trades'] if opened < trade['event_ms'] <= now]
        if reason:
            if target_trades:
                return {'status': 'unknown', 'reason': 'conflicting_exit_order'}
            if dec(book[key + '_qty']) < qty:
                return {'status': 'unknown', 'reason': 'partial_protective_exit'}
            exit_price *= 1 - side * (slip + stress)
        elif target_trades:
            trade = target_trades[0]
            if dec(trade['qty']) < qty:
                return {'status': 'unknown', 'reason': 'partial_target_exit'}
            exit_price, reason, closed = target, 'TP', trade['event_ms']
        if reason is None:
            continue
        if funding['through_ms'] < closed or funding['from_ms'] > opened:
            return {'status': 'unknown', 'reason': 'funding_unconfirmed'}
        relevant = [event for event in funding['events'] if event['symbol'] == plan['symbol']
                    and opened <= event['event_ms'] <= closed]
        if any(event['event_ms'] in (opened, closed) for event in relevant):
            return {'status': 'unknown', 'reason': 'funding_at_execution_boundary'}
        payments = sum((-side * qty * dec(event['mark']) * dec(event['rate'])
                        for event in relevant), Decimal(0))
        gross = side * qty * (exit_price - price)
        fees = qty * fee * (price + exit_price)
        return {'status': 'closed', 'reason': reason, 'opened_ms': opened, 'closed_ms': closed,
                'entry': text(price), 'exit': text(exit_price), 'gross': text(gross), 'fees': text(fees),
                'funding': text(payments), 'net': text(gross - fees + payments)}
    return {'status': 'unknown', 'reason': 'no_confirmed_exit'}


def paired_result(path, header, rows):
    plan_id = header['plan']['plan_id']
    with closing(sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True)) as db:
        db.execute('BEGIN')
        meta = {key: json.loads(value) for key, value in db.execute(
            "SELECT key,value FROM bracket_meta WHERE key IN ('started_ms','funding_through','blocked')")}
        state = db.execute('SELECT state FROM bracket_plans WHERE id=?', (plan_id,)).fetchone()
        if state is None:
            return {'status': 'unknown', 'reason': 'passive_plan_missing'}
        state = json.loads(state[0])
        if meta['blocked'] in ('funding_history_conflict', 'late_missing_funding_event'):
            return {'status': 'unknown', 'reason': 'funding_history_unconfirmed'}
        funding = {'from_ms': meta['started_ms'], 'through_ms': meta['funding_through'],
                   'events': [json.loads(row[0]) for row in db.execute('SELECT payload FROM bracket_funding')]}
    passive = None
    if state['status'] in ('CLOSED', 'CANCELED'):
        passive = dec(state['gross']) - dec(state['fees']) + dec(state['funding'])
    outcomes = {}
    for label, stress in (('base', Decimal(0)), ('stress', dec('.0001'))):
        outcome = replay(header, rows, funding, stress)
        if passive is not None and outcome['status'] in ('closed', 'no_entry'):
            outcome['delta_net'] = text(dec(outcome.get('net', '0')) - passive)
        outcomes[label] = outcome
    return {'plan_id': plan_id, 'passive_status': state['status'],
            'passive_net': None if passive is None else text(passive), **outcomes}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('journal', type=Path)
    parser.add_argument('state_db', type=Path, help='Paper SQLite or its verified backup. Opened read-only.')
    args = parser.parse_args()
    try:
        if args.journal.stat().st_size > MAX_BYTES:
            raise ValueError('size_limit')
        with args.journal.open() as source:
            header = json.loads(next(source))
            rows = [json.loads(line) for line in source]
    except (OSError, ValueError, StopIteration):
        rows = []
    if not rows or rows[-1] != {'complete': True}:
        print(canonical({'status': 'unknown', 'reason': 'incomplete_journal'}))
        return
    print(canonical(paired_result(args.state_db, header, rows)))


if __name__ == '__main__':
    main()

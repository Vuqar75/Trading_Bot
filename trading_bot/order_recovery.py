"""Simulation-only startup and periodic reconciliation.

Call recover immediately on each process startup, before allowing dispatch. The
snapshot contract is synthetic; no Binance/Telegram/HTTP adapter is included.
"""
import json
import sqlite3
from fractions import Fraction
from dataclasses import asdict
import time
from .fill_ledger import Fill, SimulatedFills, reconcile_fills, snapshot, amount
from .order_journal import OrderJournal, State, no_fault
from .order_reconciliation import reconcile
from .recovery_pages import PageCollector, SnapshotSimulator


class RecoveryExchange(SnapshotSimulator):
    def __init__(self, snapshot_id, fills, orders, quote_balance, base_balance, page_size=2):
        super().__init__(snapshot_id, fills, page_size)
        # JSON round trip detaches caller-owned dictionaries.
        self._orders=json.loads(json.dumps(orders,sort_keys=True))
        self.quote_balance=Fraction(quote_balance)
        self.base_balance=Fraction(base_balance)
    def query(self, client_id):
        result=self._orders.get(client_id)
        return dict(result) if result is not None else None
    def clients(self): return frozenset(self._orders)


def _exchange_proof(exchange):
    return (exchange.get_manifest(), json.dumps(exchange._orders,sort_keys=True),
            exchange.quote_balance, exchange.base_balance)


def _rows(db, table, order):
    return tuple(tuple(r) for r in db.execute('SELECT * FROM '+table+' ORDER BY '+order))


def _fingerprint(db):
    extra = (('close_reservations','intent_id'),) if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='close_reservations'").fetchone() else ()
    return tuple(_rows(db,t,k) for t,k in [('order_intents','intent_id'),('order_events','event_id'),
                 ('ledger_fills','trade_id'),('ledger_account','singleton'),('ledger_control','singleton'),('recovery_generation','singleton')] + list(extra))


def _mark(journal, reason):
    with journal._transaction():
        row=journal.db.execute('SELECT blocked,reason FROM ledger_control WHERE singleton=1').fetchone()
        if row is None: raise ValueError('Ledger not initialized')
        if row['reason'] not in ('', 'RECOVERY_INCOMPLETE') or (row['blocked'] and not row['reason']):
            return False
        journal.db.execute('CREATE TABLE IF NOT EXISTS recovery_generation(singleton INTEGER PRIMARY KEY CHECK(singleton=1), generation INTEGER NOT NULL)')
        journal.db.execute('INSERT OR IGNORE INTO recovery_generation VALUES(1,0)')
        journal.db.execute('UPDATE recovery_generation SET generation=generation+1 WHERE singleton=1')
        journal.db.execute('UPDATE ledger_control SET blocked=1,reason=? WHERE singleton=1',(reason,))
        return journal.db.execute('SELECT generation FROM recovery_generation').fetchone()[0]


def recover(journal, exchange, fault=no_fault):
    """Bounded recovery cycle; every incomplete path keeps a persistent gate.

    Only a complete, consistent synthetic snapshot is committed. Call on startup
    and periodically; no background thread or live trading integration is started.
    """
    if not isinstance(exchange, RecoveryExchange): raise ValueError('Simulation only')
    generation = _mark(journal,'RECOVERY_INCOMPLETE')
    if not generation: return 'contradiction'
    fault('gate_committed')
    baseline=_fingerprint(journal.db)
    proof=_exchange_proof(exchange)
    manifest=proof[0]
    collector=PageCollector(manifest)
    try:
        for index in range(len(manifest.page_hashes)):
            # Bounded retries permit a repeated page while preventing an endless loop.
            for _ in range(3):
                page=exchange.get_page(index)
                collector.add(page)
                if index in collector.pages: break
            if index not in collector.pages: return 'incomplete'
            fault('page_received')
        fills=collector.finish()
        if fills is None or _exchange_proof(exchange)!=proof: return 'incomplete'
    except ValueError:
        _mark(journal,'CONTRADICTION')
        return 'contradiction'
    except (IndexError, KeyError, OSError, TimeoutError):
        return 'incomplete'
    fault('pages_complete')
    memory=sqlite3.connect(':memory:',isolation_level=None)
    try:
        journal.db.backup(memory)
        memory.row_factory=sqlite3.Row
        memory.execute('PRAGMA foreign_keys=ON')
        shadow=object.__new__(OrderJournal)
        shadow.db=memory; shadow.clock_ms=journal.clock_ms; shadow.fault=no_fault
        memory.execute("UPDATE ledger_control SET blocked=0,reason='' WHERE singleton=1")
        intents=[shadow._decode(r) for r in memory.execute('SELECT * FROM order_intents ORDER BY intent_id')]
        active=[i for i in intents if i.state in (State.SUBMITTING,State.UNKNOWN,State.ACKNOWLEDGED)]
        if exchange.clients() != frozenset(i.client_order_id for i in active): return 'incomplete'
        by_client={i.client_order_id:i for i in active}
        counts={client:0 for client in by_client}
        for fill in fills:
            if fill.client_order_id not in counts:
                _mark(journal,'CONTRADICTION'); return 'contradiction'
            counts[fill.client_order_id]+=1
        for intent in active:
            order=exchange.query(intent.client_order_id)
            status=order.get('status') if isinstance(order,dict) else None
            if ((status=='NEW' and counts[intent.client_order_id]) or
                (status in ('PARTIALLY_FILLED','FILLED') and not counts[intent.client_order_id])):
                _mark(journal,'CONTRADICTION'); return 'contradiction'
            if status == 'PARTIALLY_FILLED' and intent.request.quantity is not None and sum((amount(f.quantity) for f in fills if f.client_order_id == intent.client_order_id), Fraction(0)) >= amount(intent.request.quantity):
                _mark(journal,'CONTRADICTION'); return 'contradiction'
            if status == 'FILLED' and intent.request.quantity is None:
                return 'incomplete'  # Quote-budget fills require authoritative cumulative totals.
            if status == 'FILLED' and intent.request.quantity is not None:
                executed = sum((amount(f.quantity) for f in fills if f.client_order_id == intent.client_order_id), Fraction(0))
                if executed != amount(intent.request.quantity):
                    _mark(journal,'CONTRADICTION'); return 'contradiction'
            _,result=reconcile(shadow,exchange,intent.intent_id)
            if result not in ('recorded','already_recorded'): return 'incomplete'
        source={f.trade_id:json.dumps(asdict(f),sort_keys=True,separators=(',',':')) for f in fills}
        for row in memory.execute('SELECT trade_id,payload FROM ledger_fills'):
            if source.get(row['trade_id']) != row['payload']:
                _mark(journal,'CONTRADICTION'); return 'contradiction'
        # Recompute accounting from the immutable history, rather than trusting
        # cached cost/PnL. Derive opening USDT from stored history's cash flows;
        # the stand contract fixes the opening BTC balance at zero.
        old_report=snapshot(shadow)
        old_fills=[Fill(**json.loads(r['payload'])) for r in memory.execute('SELECT payload FROM ledger_fills ORDER BY sequence')]
        opening=Fraction(old_report['quote_balance'])
        for f in old_fills:
            v=amount(f.quote); fee=amount(f.fee,True) if f.fee_asset=='USDT' else Fraction(0)
            opening += v+fee if f.side=='BUY' else -(v-fee)
        if opening < 0:
            _mark(journal,'CONTRADICTION'); return 'contradiction'
        def reset_and_replay(history):
            memory.execute('DELETE FROM ledger_fills')
            memory.execute("UPDATE ledger_account SET quote_balance=?,base_balance='0',cost='0',pnl='0',pending_fees=0,last_sequence=-1 WHERE singleton=1",(str(opening),))
            for fill in history:
                intent=by_client.get(fill.client_order_id)
                if intent is None: return False
                if reconcile_fills(shadow,SimulatedFills([fill]),intent.intent_id) != 'recorded': return False
            return True
        if not reset_and_replay(old_fills):
            _mark(journal,'CONTRADICTION'); return 'contradiction'
        rebuilt=snapshot(shadow)
        keys=('quote_balance','base_balance','cost','realized_pnl_excluding_unknown_fees','pending_fees','last_sequence','third_asset_fees')
        if any(rebuilt[k] != old_report[k] for k in keys):
            _mark(journal,'CONTRADICTION'); return 'contradiction'
        if not reset_and_replay(fills):
            _mark(journal,'CONTRADICTION'); return 'contradiction'
        report=snapshot(shadow)
        if (Fraction(report['quote_balance']) != exchange.quote_balance or
            Fraction(report['base_balance']) != exchange.base_balance):
            _mark(journal,'CONTRADICTION'); return 'contradiction'
        fault('before_final_commit')
        # The adapter call is outside the SQLite transaction. The simulated
        # snapshot is immutable; a real adapter needs an equivalent contract.
        if _exchange_proof(exchange)!=proof: return 'incomplete'
        with journal._transaction():
            if journal.db.execute('SELECT generation FROM recovery_generation').fetchone()[0] != generation or _fingerprint(journal.db)!=baseline: return 'incomplete'
            for row in memory.execute('SELECT * FROM order_intents'):
                journal.db.execute('UPDATE order_intents SET state=?,revision=?,updated_ms=?,exchange_order_id=?,exchange_status=? WHERE intent_id=?',
                                   (row['state'],row['revision'],row['updated_ms'],row['exchange_order_id'],row['exchange_status'],row['intent_id']))
            current_events={r[0] for r in journal.db.execute('SELECT event_id FROM order_events')}
            for row in memory.execute('SELECT * FROM order_events ORDER BY event_id'):
                if row['event_id'] not in current_events:
                    journal.db.execute('INSERT INTO order_events VALUES(?,?,?,?,?,?,?)',tuple(row))
            current_fills={r[0] for r in journal.db.execute('SELECT trade_id FROM ledger_fills')}
            for row in memory.execute('SELECT * FROM ledger_fills'):
                if row['trade_id'] not in current_fills:
                    journal.db.execute('INSERT INTO ledger_fills VALUES(?,?,?,?)',tuple(row))
            row=memory.execute('SELECT * FROM ledger_account WHERE singleton=1').fetchone()
            journal.db.execute('UPDATE ledger_account SET quote_balance=?,base_balance=?,cost=?,pnl=?,pending_fees=?,last_sequence=? WHERE singleton=1',tuple(row)[1:])
            if journal.db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='close_reservations'").fetchone():
                journal.db.execute("UPDATE close_reservations SET active=0 WHERE intent_id IN (SELECT intent_id FROM order_intents WHERE state='ACKNOWLEDGED' AND exchange_status='FILLED')")
            journal.db.execute("UPDATE ledger_control SET blocked=0,reason='' WHERE singleton=1")
            fault('inside_final_commit')
        fault('after_final_commit')
        return 'complete'
    finally:
        memory.close()


class RecoverySession:
    """Opt-in stand session: startup gate and bounded periodic tick; no bot integration."""
    def __init__(self, path, interval_seconds=30, clock=time.monotonic):
        if interval_seconds <= 0: raise ValueError('Invalid interval')
        self.journal=OrderJournal(path)
        try:
            _mark(self.journal, 'RECOVERY_INCOMPLETE')
        except BaseException:
            self.journal.close()
            raise
        self.interval_seconds=interval_seconds
        self.clock=clock
        self.next_due=clock()
    def tick(self, exchange):
        now=self.clock()
        if now < self.next_due: return 'not_due'
        self.next_due=now+self.interval_seconds
        return recover(self.journal,exchange)
    def close(self): self.journal.close()
    def __enter__(self): return self
    def __exit__(self,*args): self.close()

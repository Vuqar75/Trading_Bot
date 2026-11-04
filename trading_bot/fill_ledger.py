"""Experimental offline spot fill ledger. Never use with a live account."""
import json
import re
import sqlite3
from dataclasses import dataclass, asdict
from fractions import Fraction
from .order_journal import State, DispatchBlocked

@dataclass(frozen=True)
class Fill:
    trade_id: str
    order_id: str
    client_order_id: str
    sequence: int
    side: str
    quantity: str
    quote: str
    fee_asset: str
    fee: str

class SimulatedFills:
    is_simulator = True
    def __init__(self, fills): self.fills = list(fills)
    def query_fills(self, client_order_id):
        return [f for f in self.fills if f.client_order_id == client_order_id]

def amount(value, allow_zero=False):
    if not isinstance(value, str) or not re.fullmatch(r'[0-9]{1,18}(?:\.[0-9]{1,18})?', value):
        raise ValueError('Invalid decimal')
    n = Fraction(value)
    if n < 0 or (not allow_zero and n == 0): raise ValueError('Invalid amount')
    return n

def enc(n): return str(n)

def initialize(journal, opening_quote):
    opening = amount(opening_quote, True)
    with journal._transaction():
        tables = {r[0] for r in journal.db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if 'ledger_control' in tables:
            return
        journal.db.execute('CREATE TABLE ledger_control(singleton INTEGER PRIMARY KEY CHECK(singleton=1), blocked INTEGER NOT NULL, reason TEXT NOT NULL)')
        journal.db.execute("INSERT INTO ledger_control VALUES(1,0,'')")
        journal.db.execute('CREATE TABLE ledger_account(singleton INTEGER PRIMARY KEY CHECK(singleton=1), quote_balance TEXT NOT NULL, base_balance TEXT NOT NULL, cost TEXT NOT NULL, pnl TEXT NOT NULL, pending_fees INTEGER NOT NULL, last_sequence INTEGER NOT NULL)')
        journal.db.execute('INSERT INTO ledger_account VALUES(1,?,\'0\',\'0\',\'0\',0,-1)', (enc(opening),))
        journal.db.execute('CREATE TABLE ledger_fills(trade_id TEXT PRIMARY KEY, intent_id TEXT NOT NULL REFERENCES order_intents(intent_id), sequence INTEGER UNIQUE NOT NULL, payload TEXT NOT NULL)')

def snapshot(journal):
    r=journal.db.execute('SELECT * FROM ledger_account WHERE singleton=1').fetchone()
    c=journal.db.execute('SELECT blocked,reason FROM ledger_control WHERE singleton=1').fetchone()
    third_fees = {}
    for row in journal.db.execute('SELECT payload FROM ledger_fills'):
        f = json.loads(row[0])
        if f['fee_asset'] not in ('BTC', 'USDT'):
            third_fees[f['fee_asset']] = third_fees.get(f['fee_asset'], Fraction(0)) + amount(f['fee'], True)
    return dict(third_asset_fees={k:enc(v) for k,v in third_fees.items()}, quote_balance=r['quote_balance'], base_balance=r['base_balance'], cost=r['cost'],
                realized_pnl_excluding_unknown_fees=r['pnl'], net_pnl=None if r['pending_fees'] else r['pnl'],
                pending_fees=r['pending_fees'], last_sequence=r['last_sequence'], blocked=bool(c[0]), reason=c[1])

def reconcile_fills(journal, simulator, intent_id, fault=lambda p:None):
    if getattr(simulator,'is_simulator',False) is not True: raise ValueError('Only simulated fills are supported')
    intent=journal.get(intent_id)
    try: fills=simulator.query_fills(intent.client_order_id)
    except Exception: return 'unavailable'
    if not isinstance(fills,list): return 'unavailable'
    with journal._transaction():
        control=journal.db.execute('SELECT blocked FROM ledger_control WHERE singleton=1').fetchone()
        if control is None: raise ValueError('Ledger not initialized')
        if control[0]: return 'blocked'
        journal.db.execute('SAVEPOINT batch')
        try:
            intent=journal.get(intent_id)
            if intent.state != State.ACKNOWLEDGED or not intent.exchange_order_id: raise ValueError('Order not confirmed')
            account=journal.db.execute('SELECT * FROM ledger_account WHERE singleton=1').fetchone()
            cash,base,cost,pnl=map(Fraction,(account['quote_balance'],account['base_balance'],account['cost'],account['pnl']))
            pending,last=account['pending_fees'],account['last_sequence']
            prior = journal.db.execute('SELECT payload FROM ledger_fills WHERE intent_id=?',(intent_id,)).fetchall()
            total_qty = sum((amount(json.loads(r[0])['quantity']) for r in prior), Fraction(0))
            total_quote = sum((amount(json.loads(r[0])['quote']) for r in prior), Fraction(0))
            for f in fills:
                if not isinstance(f, Fill): raise ValueError('Invalid fill')
                if not all(isinstance(x,str) and re.fullmatch(r'[A-Za-z0-9_.:-]{1,100}',x) for x in (f.trade_id,f.order_id,f.client_order_id,f.fee_asset)): raise ValueError('Invalid identifiers')
                if f.order_id != intent.exchange_order_id or f.client_order_id != intent.client_order_id or f.side != intent.request.side: raise ValueError('Order mismatch')
                if type(f.sequence) is not int or not 0 <= f.sequence <= 9223372036854775807: raise ValueError('Invalid sequence')
                q,v,fee=amount(f.quantity),amount(f.quote),amount(f.fee,True)
                payload=json.dumps(asdict(f),sort_keys=True,separators=(',',':'))
                existing=journal.db.execute('SELECT intent_id,payload FROM ledger_fills WHERE trade_id=?',(f.trade_id,)).fetchone()
                if existing:
                    if existing['payload'] != payload or existing['intent_id'] != intent_id: raise ValueError('Conflicting trade ID')
                    continue
                if f.sequence <= last: raise ValueError('Missing or reordered trade history')
                total_qty += q; total_quote += v
                if intent.request.quantity is not None and total_qty > amount(intent.request.quantity): raise ValueError('Executed quantity exceeds order')
                if intent.request.quote_quantity is not None and total_quote > amount(intent.request.quote_quantity): raise ValueError('Executed quote exceeds order')
                base_fee=fee if f.fee_asset=='BTC' else Fraction(0)
                quote_fee=fee if f.fee_asset=='USDT' else Fraction(0)
                if f.fee_asset not in ('BTC','USDT') and fee: pending+=1
                if f.side=='BUY':
                    if q <= base_fee or cash < v+quote_fee: raise ValueError('Invalid buy balance')
                    base+=q-base_fee; cost+=v+quote_fee; cash-=v+quote_fee
                elif f.side=='SELL':
                    removed=q+base_fee
                    if removed>base or quote_fee>v: raise ValueError('Invalid sell balance')
                    allocated=cost*removed/base
                    base-=removed; cost-=allocated; cash+=v-quote_fee; pnl+=v-quote_fee-allocated
                else: raise ValueError('Invalid side')
                journal.db.execute('INSERT INTO ledger_fills VALUES(?,?,?,?)',(f.trade_id,intent_id,f.sequence,payload))
                last=f.sequence
            journal.db.execute('UPDATE ledger_account SET quote_balance=?,base_balance=?,cost=?,pnl=?,pending_fees=?,last_sequence=? WHERE singleton=1',(enc(cash),enc(base),enc(cost),enc(pnl),pending,last))
            fault('before_commit')
            journal.db.execute('RELEASE batch')
        except (ValueError, TypeError, OverflowError, sqlite3.IntegrityError):
            journal.db.execute('ROLLBACK TO batch')
            journal.db.execute('RELEASE batch')
            journal.db.execute("UPDATE ledger_control SET blocked=1,reason='CONTRADICTION' WHERE singleton=1")
            return 'contradiction'
    fault('after_commit')
    return 'recorded'

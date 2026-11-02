"""Offline evidence model for funding periods; not wired to a trading worker.

The public endpoint does not provide an immutable completeness certificate.
Callers must supply a source-specific verifier that proves the entire interval
and position history; a count of returned rows is never such a proof.
"""
from dataclasses import dataclass
from decimal import Decimal,InvalidOperation


@dataclass(frozen=True)
class FundingEvent:
    symbol: str
    funding_time: int
    rate: str
    mark_price: str
    rate_type: str = 'Regular'


@dataclass(frozen=True)
class FundingPeriod:
    symbol: str
    expected_due: int
    next_due: int


@dataclass(frozen=True)
class PositionLifetime:
    position_id: str
    symbol: str
    opened_ms: int
    closed_ms: int | None = None


def _decimal(value, *, positive=False):
    if not isinstance(value,str):raise ValueError('Decimal string required')
    try:result=Decimal(value)
    except InvalidOperation:raise ValueError('Invalid decimal') from None
    if not result.is_finite() or (positive and result<=0):raise ValueError('Invalid decimal')
    return result


def verify_period(period, events, positions, *, history_proof, positions_proof,
                  verify_history, verify_positions, accepted=None):
    """Resolve one published Regular event without mutating financial state.

    History proof must attest the complete half-open period, including delayed
    publication; position proof must attest complete lifecycle history over the
    same range. Verifier callbacks are external trust boundaries, and are never
    inferred from the number or last page of records. Production must not use a
    test verifier. None or anything other than literal True fails closed.
    """
    if (not isinstance(period,FundingPeriod) or not period.symbol or
        type(period.expected_due) is not int or type(period.next_due) is not int or
        not 0<=period.expected_due<period.next_due):raise ValueError('Invalid period')
    if not isinstance(history_proof,str) or not history_proof or verify_history(history_proof,period) is not True:
        raise ValueError('Funding history completeness unproven')
    if not isinstance(positions_proof,str) or not positions_proof or verify_positions(positions_proof,period) is not True:
        raise ValueError('Position history completeness unproven')
    if not isinstance(events,(tuple,list)) or not isinstance(positions,(tuple,list)):
        raise ValueError('Invalid history')
    seen=set();regular=[]
    for event in events:
        if (not isinstance(event,FundingEvent) or event.symbol!=period.symbol or
            type(event.funding_time) is not int or
            not period.expected_due<=event.funding_time<period.next_due or
            event.rate_type not in ('Regular','Special')):raise ValueError('Event outside period or invalid')
        if abs(_decimal(event.rate))>=1:raise ValueError('Invalid funding rate')
        _decimal(event.mark_price,positive=True)
        key=(event.funding_time,event.rate_type)
        if key in seen:raise ValueError('Duplicate funding event')
        seen.add(key)
        if event.rate_type=='Regular':regular.append(event)
    if len(regular)!=1 or len(events)!=1:raise ValueError('Missing or ambiguous Regular funding')
    event=regular[0]
    if accepted is not None and (not isinstance(accepted,FundingEvent) or accepted!=event):
        raise ValueError('Previously accepted funding changed')
    owners=[];ids=set()
    for position in positions:
        if (not isinstance(position,PositionLifetime) or not position.position_id or position.symbol!=period.symbol or
            type(position.opened_ms) is not int or position.opened_ms<0 or
            (position.closed_ms is not None and (type(position.closed_ms) is not int or position.closed_ms<position.opened_ms))):
            raise ValueError('Invalid position history')
        if position.position_id in ids:raise ValueError('Duplicate position history')
        ids.add(position.position_id)
        if position.opened_ms<=event.funding_time and (position.closed_ms is None or event.funding_time<position.closed_ms):
            owners.append(position.position_id)
    if len(owners)>1:raise ValueError('Ambiguous position at funding time')
    return {'period':period,'event':event,'position_id':owners[0] if owners else None,
            'history_proof':history_proof,'positions_proof':positions_proof}


def initialize_receipts(db):
    """Create an offline receipt table within the caller's migration transaction."""
    if not db.in_transaction:raise ValueError('Caller transaction required')
    db.execute('''CREATE TABLE IF NOT EXISTS cross_funding_evidence_receipts(
        symbol TEXT NOT NULL, expected_due INTEGER NOT NULL, next_due INTEGER NOT NULL,
        funding_time INTEGER NOT NULL, rate TEXT NOT NULL, mark_price TEXT NOT NULL,
        rate_type TEXT NOT NULL, position_id TEXT, history_proof TEXT NOT NULL, positions_proof TEXT NOT NULL,
        PRIMARY KEY(symbol,expected_due))''')


def record_verified(db, result):
    """Persist the verified identity in the caller's financial transaction.

    Returns True for a new receipt, False for the exact replay. The caller must
    perform any corresponding postings in the same SQLite transaction and only
    when True; this helper by itself does not apply or commit funding.
    """
    if not db.in_transaction:raise ValueError('Caller transaction required')
    period=result['period'];event=result['event'];owner=result['position_id']
    if not isinstance(period,FundingPeriod) or not isinstance(event,FundingEvent):raise ValueError('Verified identity required')
    if (event.symbol!=period.symbol or event.rate_type!='Regular' or type(period.expected_due) is not int or
        type(period.next_due) is not int or type(event.funding_time) is not int or
        not 0<=period.expected_due<=event.funding_time<period.next_due or
        (owner is not None and (not isinstance(owner,str) or not owner)) or
        not isinstance(result.get('history_proof'),str) or not result['history_proof'] or
        not isinstance(result.get('positions_proof'),str) or not result['positions_proof']):
        raise ValueError('Verified identity required')
    if abs(_decimal(event.rate))>=1:raise ValueError('Invalid funding rate')
    _decimal(event.mark_price,positive=True)
    values=(period.symbol,period.expected_due,period.next_due,event.funding_time,event.rate,event.mark_price,event.rate_type,owner,result['history_proof'],result['positions_proof'])
    old=db.execute('SELECT symbol,expected_due,next_due,funding_time,rate,mark_price,rate_type,position_id,history_proof,positions_proof FROM cross_funding_evidence_receipts WHERE symbol=? AND expected_due=?',values[:2]).fetchone()
    if old is not None:
        if tuple(old)!=values:raise ValueError('Previously accepted funding changed')
        return False
    db.execute('INSERT INTO cross_funding_evidence_receipts VALUES(?,?,?,?,?,?,?,?,?,?)',values)
    return True

"""Offline reconciliation of ambiguous submits. No fill accounting or network."""
from .order_journal import State, Receipt, DispatchBlocked


def reconcile(journal, simulator, intent_id):
    """Look up only by the durable client ID; never submit or infer absence.

    Return the current intent and a conservative outcome. A receipt records an
    observed order status only; all states remain ineligible for dispatch.
    """
    if getattr(simulator, 'is_simulator', False) is not True:
        raise ValueError('Only a simulator is supported')
    intent = journal.get(intent_id)
    if intent.state not in (State.SUBMITTING, State.UNKNOWN, State.ACKNOWLEDGED):
        return intent, 'blocked'
    try:
        result = simulator.query(intent.client_order_id)
    except Exception:
        return journal.get(intent_id), 'unresolved'
    if not isinstance(result, dict):
        return journal.get(intent_id), 'unresolved'
    required = {'client_order_id', 'exchange_order_id', 'symbol', 'side',
                'quantity', 'quote_quantity', 'status'}
    if set(result) != required:
        return journal.get(intent_id), 'unresolved'
    expected = (intent.client_order_id, intent.request.symbol, intent.request.side,
                intent.request.quantity, intent.request.quote_quantity)
    observed = tuple(result[k] for k in ('client_order_id', 'symbol', 'side', 'quantity', 'quote_quantity'))
    if observed != expected or result['status'] not in ('NEW', 'FILLED', 'PARTIALLY_FILLED'):
        return journal.get(intent_id), 'unresolved'
    receipt = Receipt(result['exchange_order_id'], result['status'])
    try:
        receipt.validate()
    except (TypeError, ValueError):
        return journal.get(intent_id), 'unresolved'
    if intent.state == State.ACKNOWLEDGED:
        same = (intent.exchange_order_id, intent.exchange_status) == (receipt.exchange_order_id, receipt.status)
        return journal.get(intent_id), 'already_recorded' if same else 'unresolved'
    try:
        saved = journal._transition(intent_id, intent.state, State.ACKNOWLEDGED,
                                    'RESPONSE_RECORDED', receipt=receipt)
    except DispatchBlocked:
        return journal.get(intent_id), 'unresolved'
    return saved, 'recorded'

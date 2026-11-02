"""Explicit paper policy for associating a published event with a period.

Binance documents inclusive millisecond query bounds, but does not promise
that fundingTime equals the previously observed nextFundingTime. A period is
therefore an explicitly modelled half-open interval, not a time tolerance.
Both the period deadline and the original reported timestamp must be retained.
No missing, duplicate, ambiguous, or future event is inferred or manufactured.
"""


def assign_period(reported_ms, windows, *, opened_ms, closed_ms=None):
    """Select exactly one evidenced [deadline, next_deadline) window.

    A paper position must exist at both the period deadline and the actual
    event timestamp; otherwise ownership across a close is ambiguous.
    """
    if (type(reported_ms) is not int or type(opened_ms) is not int or
        reported_ms < 0 or opened_ms < 0 or
        (closed_ms is not None and (type(closed_ms) is not int or closed_ms < opened_ms))):
        raise ValueError('Invalid funding event or position lifetime')
    hits=[]
    for due,end in windows:
        if type(due) is not int or type(end) is not int or due<0 or end<=due:
            raise ValueError('Invalid evidenced funding window')
        if due<=reported_ms<end:
            if not opened_ms<=due or (closed_ms is not None and (due>closed_ms or reported_ms>closed_ms)):
                raise ValueError('Funding ownership across position boundary is ambiguous')
            hits.append(due)
    if len(hits)!=1:raise ValueError('Funding period is missing or ambiguous')
    return hits[0]


def require_unique_regular(events, due, next_due):
    """Validate a complete period's mapped events without guessing missing data."""
    if not isinstance(events,list):raise ValueError('Invalid funding events')
    kinds=set()
    for event in events:
        if not isinstance(event,dict) or event.get('period_due')!=due:
            raise ValueError('Funding event period mismatch')
        stamp=event.get('reported_time');kind=event.get('rate_type','Regular')
        if type(stamp) is not int or not due<=stamp<next_due or kind not in ('Regular','Special') or kind in kinds:
            raise ValueError('Duplicate or ambiguous funding event')
        kinds.add(kind)
    if 'Regular' not in kinds:raise ValueError('Expected funding payment is not published')

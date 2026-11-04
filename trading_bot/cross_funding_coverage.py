"""Conservative offline validation of one observed funding interval.

The previous deadline and interval must have been durably observed while the
position was open.  This function does not fetch data or certify the exchange;
the caller must use an authenticated-by-TLS public response, invalidate data
before fetching, and atomically apply every returned event before reopening risk.
"""


def check_funding_window(*, prior_due, prior_interval, server_ms, observed_due, observed_interval, events):
    values=(prior_due,prior_interval,server_ms,observed_due,observed_interval)
    if any(type(x) is not int or x <= 0 for x in values) or not isinstance(events,list):
        raise ValueError('Invalid funding schedule')
    if prior_interval!=observed_interval:
        raise ValueError('Funding interval changed; coverage unknown')
    if server_ms<prior_due:
        if observed_due!=prior_due or events:
            raise ValueError('Funding deadline changed before settlement')
        return {'complete':True,'next_due':prior_due,'events':[]}
    if server_ms>=prior_due+prior_interval:
        raise ValueError('More than one interval elapsed; coverage unknown')
    if observed_due!=prior_due+prior_interval:
        raise ValueError('Funding deadline not advanced as expected')
    seen=set();has_regular=False
    for event in events:
        if not isinstance(event,dict) or event.get('time')!=prior_due:
            raise ValueError('Funding history has unexpected timestamp')
        kind=event.get('rate_type','Regular')
        if kind not in ('Regular','Special') or kind in seen:
            raise ValueError('Funding history has duplicate or unknown type')
        seen.add(kind);has_regular|=kind=='Regular'
    if not has_regular:
        raise ValueError('Expected funding payment is not published')
    return {'complete':True,'next_due':observed_due,'events':events}

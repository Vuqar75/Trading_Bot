"""Causal bar analogues for a 15-minute setup with four bars of holding time."""
from decimal import Decimal

POLICY = '15m-retest-60m-v1'
HOLD_MINUTES = 60


def historical_windows(rows):
    # Four complete bars from entry-bar open. Even a fill at that open has
    # 45 minutes left after its entry bar. Ignore favorable entry-bar touches.
    windows = []
    for end in range(len(rows), 3, -4):
        bars = rows[end-4:end]
        opening = Decimal(str(bars[0][1]))
        windows.append([(Decimal(str(b[3])) / opening, Decimal(str(b[2])) / opening)
                        for b in bars])
    return windows


def annotate(candidate, snapshot, windows):
    long = candidate['direction'] == 'LONG'
    market = Decimal(snapshot['quote']['ask' if long else 'bid'])
    entry, stop, target = [Decimal(candidate[key]) for key in ('entry', 'stop', 'target')]
    limit, sl, tp = [price / market for price in (entry, stop, target)]
    entries = wins = 0
    for first, *following in windows:
        low, high = first
        if not (low < limit if long else high > limit):
            continue
        entries += 1
        if low <= sl if long else high >= sl:
            continue
        for low, high in following:
            if low <= sl if long else high >= sl:
                break
            if high > tp if long else low < tp:
                wins += 1
                break
    if wins < 3:
        return None
    obstacles = sorted({Decimal(x['price']) for x in snapshot['levels']
                        if x['kind'] == ('resistance' if long else 'support')
                        and min(entry,target) < Decimal(x['price']) < max(entry,target)},
                       reverse=not long)
    return dict(candidate, hold_min=HOLD_MINUTES, analog_counts=[len(windows),entries,wins],
                levels_before_tp=len(obstacles), first_obstacle=str(obstacles[0]) if obstacles else None)


def shortlist(candidates, snapshot, rows):
    windows = historical_windows(rows)
    annotated = (annotate(c, snapshot, windows) for c in candidates)
    ranked = sorted((c for c in annotated if c), key=lambda c: (
        -c['analog_counts'][2], c['levels_before_tp'],
        abs(Decimal(c['target']) - Decimal(c['entry'])), c['entry_level'], c['stop_level'], c['target_level']))
    chosen = []
    for direction in ('LONG','SHORT'):
        seen = set()
        for c in ranked:
            if c['direction'] != direction:
                continue
            pair = (Decimal(c['entry']), Decimal(c['stop']))
            if pair in seen:
                continue
            seen.add(pair)
            chosen.append(c)
            if len(seen) == 4:
                break
    return chosen

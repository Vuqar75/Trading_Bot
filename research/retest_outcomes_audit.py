"""Conditional OHLC exit envelopes for the frozen retest shortlist. No executor or HTTP."""
import gzip
import json
import sys
from collections import Counter, defaultdict
from decimal import Decimal as D
from pathlib import Path

from .ai_limit_plan import digest

BAR = 900000
FEE = D('.0005')
IMPACT = D('.001')


def exits(bars, limit, sl, tp):
    """Signed OHLC: higher is favorable for either side. sl includes the BBO proxy."""
    for i, (opening, high, low, close) in enumerate(bars):
        first, partial = i == 0, i == 4
        if first and low >= limit:
            return
        if not first and opening <= sl:
            yield 'SL', i, opening, opening
            return
        if not first and opening > tp:
            yield 'TP', i, tp, tp
            return
        if low <= sl:
            # An intra-bar jump may cross the stop. Bound first trigger by bar low.
            yield 'SL', i, low, sl
        if high > tp:
            yield 'TP', i, tp, tp
        if partial:
            # Only an unknown prefix of the fifth candle precedes the deadline.
            yield 'TIME', i, max(low, sl), min(high, tp)
            return
        if low <= sl or (not first and high > tp) or (first and close > tp):
            return
    yield 'CENSORED', 4, None, None


def read(path):
    return json.loads(Path(path).read_text(encoding='utf-8-sig'))


def price_result(side, entry, quantity, signed_market, reason, exit_quote_factor, funding):
    trade = side * signed_market
    quote = trade if reason == 'TP' else trade * exit_quote_factor
    fill = quote if reason == 'TP' else quote * (1-side*IMPACT)
    movement = quantity*side*(trade-entry)
    spread = quantity*side*(trade-quote)
    impact = quantity*side*(quote-fill)
    fees = quantity*(entry+fill)*FEE
    gross = quantity*side*(fill-entry)
    net = gross-fees+funding
    assert abs(net-(movement-spread-impact-fees+funding)) < D('1e-20')
    return dict(movement=movement,spread=spread,impact=impact,gross=gross,fees=fees,funding=funding,net=net)


def funding_range(events, side, quantity, scale, start, ttl, i, reason):
    # Windows provide outer bounds on event ownership, including uncertain ties.
    entry_end = start + ttl
    earliest = start + i*BAR
    latest = start + (i+1)*BAR
    if i == 0:
        earliest = start
    if i == 4:
        latest = entry_end + 4*BAR
    low = high = D(0)
    for t, mark, rate in events:
        if start < t <= latest:
            value = -side*quantity*scale*mark*rate
            if entry_end < t < earliest:
                low += value
                high += value
            else:
                low += min(0,value)
                high += max(0,value)
    return low, high


def run(directory, output):
    captures = read(directory/'nov20-sources.json')
    first = read('research/PAPER_DAY1_20261118.json')
    second = read(directory/'nov20-raw.json')
    snapshots = [c['snapshot'] for c in first['cycles']]
    snapshots.extend(json.loads(q['snapshot']) for q in second['budget']['requests'] if q['result'])
    shortlist = read('research/RETEST_60M_COMPARISON.json')
    funding = read('research/RETEST_FUNDING_202611.json')
    tail = read('research/RETEST_FINAL_BAR_202611.json')
    assert tail['klines'][0][0] == tail['start_ms'] and tail['klines'][0][6] == tail['end_ms']
    funding.extend(r for r in tail['fundingRate'] if r not in funding)
    events = [(r['fundingTime'], D(r['markPrice']), D(r['fundingRate'])) for r in funding]
    assert len(set(t for t,_,_ in events)) == len(events) == 36
    later = defaultdict(set)
    for v in captures.values():
        for b in v['candles']:
            later[b[0]].add(tuple(b))
    detail, summaries = [], []
    for index, (snapshot, listing) in enumerate(zip(snapshots,shortlist,strict=True),1):
        assert snapshot['snapshot_id']==listing['source']
        rows=captures[listing['source']]['candles']
        assert digest(rows)==listing['candle_hash']
        assert len(rows)==960 and all(b[0]==rows[0][0]+j*BAR for j,b in enumerate(rows))
        quote=snapshot['quote']
        bid,ask=D(quote['bid']),D(quote['ask'])
        ttl=listing['entry_deadline']-listing['as_of_ms']
        assert 0<ttl<=BAR
        for n,c in enumerate(listing['new'],1):
            key=f'{index:02d}.{n}'
            entry,stop,target,quantity=[D(c[k]) for k in ('entry','stop','target','quantity_cap')]
            side=1 if c['direction']=='LONG' else -1
            reference=ask if side==1 else bid
            factor=(bid if side==1 else ask)/((ask+bid)/2)
            counts=Counter()
            cross=Counter()
            old_wins=0
            totals={k:defaultdict(D) for k in ('low','high')}
            for j in range(0,len(rows),4):
                block=rows[j:j+4]
                start=block[0][0]
                extra=rows[j+4:j+5]
                extension='snapshot'
                if not extra:
                    alternatives=later[start+4*BAR]
                    extra=list(alternatives) if len(alternatives)==1 else []
                    extension='other_saved_snapshot' if extra else 'missing_or_conflicting'
                    if not alternatives and start + 4*BAR == tail['start_ms']:
                        extra=[tail['klines'][0][:6]]
                        extension='public_label_only'
                scaling=reference/D(block[0][1])
                oriented=[]
                for b in block+extra:
                    o,h,l,cl=[side*D(v)*scaling for v in b[1:5]]
                    oriented.append((o,max(h,l),min(h,l),cl))
                outcomes=list(exits(oriented,side*entry,side*stop/factor,side*target))
                if not outcomes:
                    counts['no_contact']+=1
                    continue
                old_win=False
                if oriented[0][2]>side*stop:
                    for _,h,l,_ in oriented[1:4]:
                        if l<=side*stop:
                            break
                        if h>side*target:
                            old_win=True
                            break
                old_wins+=old_win
                options=defaultdict(list)
                for reason,i,lo,hi in outcomes:
                    if lo is None:
                        options[reason]=[]
                        continue
                    fmin,fmax=funding_range(events,side,quantity,scaling,start,ttl,i,reason)
                    for price,f in [(lo,fmin),(hi,fmax)]:
                        options[reason].append(price_result(side,entry,quantity,price,reason,factor,f))
                label='/'.join(sorted(options))
                counts[label]+=1
                cross['old_hit' if old_win else 'other',label]+=1
                record=dict(candidate=key,start_ms=start,ttl_ms=ttl,old_hit=old_win,outcomes=label,fifth_bar=extension)
                for reason, values in options.items():
                    if values:
                        record[reason]=[min(v['net'] for v in values),max(v['net'] for v in values)]
                if 'CENSORED' not in options:
                    values=[v for series in options.values() for v in series]
                    for direction,select in [('low',min),('high',max)]:
                        end=select(values,key=lambda v:v['net'])
                        record[direction]=end
                        for name,value in end.items():
                            totals[direction][name]+=value
                detail.append(record)
            assert 240-counts['no_contact']==c['analog_counts'][1] and old_wins==c['analog_counts'][2]
            summaries.append(dict(id=key,source=listing['source'],candidate=c,counts=dict(counts),
                                  previous_hits=old_wins,old_groups={str(k):v for k,v in cross.items()},
                                  complete_only=totals))
    output.mkdir(exist_ok=True)
    def encode(x):return json.dumps(x,default=str,ensure_ascii=False,sort_keys=True)
    (output/'summary.json').write_text(encode(summaries)+'\n',encoding='utf8')
    lines=''.join(encode(r)+'\n' for r in detail)
    (output/'windows.jsonl.gz').write_bytes(gzip.compress(lines.encode(),mtime=0))
    return summaries


if __name__=='__main__':
    run(Path(sys.argv[1]),Path('research/retest_outcomes'))

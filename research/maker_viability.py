"""Fixed-path fee sensitivity from saved aggregates; no market data or backtest."""
import csv, gzip, hashlib, json, pathlib

ROOT=pathlib.Path(__file__).resolve().parent.parent
SOURCE=ROOT/'research/usdm_screen_summary.csv.gz'
EXPECTED='1be8d70ac22aa62f3155854eee677fe9862ee719188a64b7d24dbc4ad8ea21b5'
OUT=ROOT/'research/maker_viability_96h.json'
SEGMENTS=('2025H1','2025H2','2026A','2026B')

def compute(gross,funding,impact,turnover):
    # SHORT: entry fill notional - exit fill notional = reference gross - impact.
    # Valid here because every position is short, closed, with no liquidation.
    if turnover<=0:raise ValueError('invalid turnover')
    entry=(turnover+gross-impact)/2
    exit_=(turnover-gross+impact)/2
    if entry<=0 or exit_<=0:raise ValueError('invalid side notionals')
    entry_fee=entry*.0002;exit_fee=exit_*.0005
    before=gross+funding;after=before-entry_fee-exit_fee;denom=turnover/2
    bps=lambda n:10000*n/denom
    return {'gross':gross,'funding':funding,'before_costs':before,'turnover':turnover,'denominator':denom,
            'entry_notional_saved_fill':entry,'exit_notional_saved_fill':exit_,
            'entry_maker_fee':entry_fee,'exit_taker_fee':exit_fee,'fee_total':entry_fee+exit_fee,
            'before_costs_bps':bps(before),'fees_bps':bps(entry_fee+exit_fee),
            'remaining':after,'remaining_bps':bps(after),'old_impact':impact}

def calculate():
    if hashlib.sha256(SOURCE.read_bytes()).hexdigest()!=EXPECTED:raise ValueError('saved summary hash mismatch')
    scenarios={}
    with gzip.open(SOURCE,'rt',encoding='utf8',newline='') as f:
        for row in csv.DictReader(f):
            if tuple(row[k] for k in ('timeframe','family','side','symbol'))!=('1h','A','SHORT','BTCUSDT'):continue
            if row['scenario'] not in ('base','stress'):continue
            scenarios.setdefault(row['scenario'],[]).append(row)
    out={}
    for name,rows in scenarios.items():
        if len(rows)!=4 or set(r['segment'] for r in rows)!=set(SEGMENTS):raise ValueError('incomplete saved segments')
        for r in rows:
            if int(r['liquidations']) or r['open_mark_net'] or int(r['entries'])!=int(r['closed']):raise ValueError('unsupported position state')
            if abs(float(r['gross'])+float(r['funding'])-float(r['fees'])-float(r['impact'])-float(r['net']))>1e-8:raise ValueError('saved ledger mismatch')
        sums={k:sum(float(r[k]) for r in rows) for k in ('gross','funding','impact','turnover')}
        full=compute(**sums);n=sum(int(r['closed']) for r in rows)
        segments={r['segment']:{'closed':int(r['closed']),**compute(*(float(r[k]) for k in ('gross','funding','impact','turnover')))} for r in rows}
        skipped={}
        for r in rows:
            keep={k:sums[k]-float(r[k]) for k in sums}
            skipped[r['segment']]={'trades_skipped':int(r['closed']),'trades_kept':n-int(r['closed']),**compute(**keep)}
        # Unbiased simple-random deletion on the fixed list, not an estimated fill rate.
        random_thinning={str(k):{'skipped':k,'expected_remaining':full['remaining']*(n-k)/n} for k in (0,round(n*.1),round(n*.5))}
        # Cost per bp uses original full D; no re-simulation or changed sizes.
        degradation={str(b):{'additional_bps':b,'additional_cost':full['denominator']*b/10000,
                              'remaining_after':full['remaining']-full['denominator']*b/10000} for b in (0,.5,1,1.3,2)}
        best=max(float(r['best_trade']) for r in rows)
        old_net=sum(float(r['net']) for r in rows)
        out[name]={'closed':n,'time_exit_count':sum(json.loads(r['exit_reasons']).get('time',0) for r in rows),
                   'ambiguous_funding_bars':sum(int(r['ambiguous_funding']) for r in rows),
                   'aggregate':full,'segments':segments,'drop_segment_retrospective':skipped,
                   'random_fixed_list_expectation':random_thinning,'additional_cost_sensitivity':degradation,
                   'old_taker_net':old_net,'old_taker_best_trade_net':best,'old_taker_net_without_best':old_net-best}
    if set(out)!=set(('base','stress')):raise ValueError('both scenarios required')
    return {'source_sha256':EXPECTED,'strategy':'1h A SHORT BTCUSDT, saved 96-hour threshold, not 48h',
            'limits':'fixed saved path; no trade-level maker fills, true queue or post-skip routing',**out}

def main():
    result=calculate();OUT.write_text(json.dumps(result,indent=2,sort_keys=True)+'\n',encoding='utf8')
    print(json.dumps(result,indent=2,sort_keys=True))

if __name__=='__main__':main()

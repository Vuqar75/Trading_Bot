"""Accounting review of saved results only; no backtests, imports of engines or network.

The zero-impact column is a fixed-path accounting sensitivity. It does not
recompute orders, stops, quantity rounding, fill probability or trade selection.
"""
import argparse
from collections import defaultdict
import csv
from decimal import Decimal
import gzip
import hashlib
import io
import json
from pathlib import Path


SOURCE_SHA256 = '1be8d70ac22aa62f3155854eee677fe9862ee719188a64b7d24dbc4ad8ea21b5'
ROOT = Path(__file__).resolve().parents[1]
SHORT_FRAMES = ('1m','3m','5m','15m','30m')
FIELDS = ('gross','fees','impact','funding','net','closed','entries','turnover')
GRAIN = ('timeframe','family','side','symbol','segment','scenario')


def number(row, field):
    return Decimal(row[field]) if row[field] else Decimal(0)


def aggregate(rows):
    result = {f: sum((number(r,f) for r in rows), Decimal(0)) for f in FIELDS}
    result['fixed_path_net_without_impact'] = result['gross']+result['funding']-result['fees']
    result['before_costs_including_funding'] = result['gross']+result['funding']
    return result


def review(source):
    raw = Path(source).read_bytes()
    if hashlib.sha256(raw).hexdigest() != SOURCE_SHA256:
        raise ValueError('saved_source_hash_changed_review_required')
    rows = list(csv.DictReader(io.StringIO(gzip.decompress(raw).decode())))
    if len(rows) != 3360 or len({tuple(r[k] for k in GRAIN) for r in rows}) != len(rows):
        raise ValueError('saved_result_grain_changed_or_duplicates')
    residual = max(abs(number(r,'net')-(number(r,'gross')-number(r,'fees')-number(r,'impact')+number(r,'funding'))) for r in rows)
    if residual > Decimal('0.0000001'):
        raise ValueError('accounting_does_not_reconcile')
    scenarios = {}
    for scenario in ('base','stress'):
        selected = [r for r in rows if r['scenario']==scenario]
        groups = defaultdict(list)
        for r in selected:
            groups[(r['timeframe'],r['family'],r['side'])].append(r)
        if len(groups)!=138 or any(len(v)!=12 for v in groups.values()):
            raise ValueError('incomplete_asset_segment_coverage')
        short = {k:v for k,v in groups.items() if k[0] in SHORT_FRAMES}
        if len(short)!=60:
            raise ValueError('short_timeframe_coverage_changed')
        totals = {k:aggregate(v) for k,v in short.items()}
        scenarios[scenario] = {
            'groups':len(short),
            'positive_net':sum(v['net']>0 for v in totals.values()),
            'positive_fixed_path_without_impact':sum(v['fixed_path_net_without_impact']>0 for v in totals.values()),
            'positive_before_fees_and_impact':sum(v['before_costs_including_funding']>0 for v in totals.values()),
            'btc_positive_fixed_path_without_impact':sum(aggregate([r for r in v if r['symbol']=='BTCUSDT'])['fixed_path_net_without_impact']>0 for v in short.values()),
        }
    key_rows = []
    for tf,family in [('5m','A'),('15m','A'),('5m','C'),('15m','C'),('1h','A'),('1h','B'),('4h','A'),('4h','B')]:
        selected = [r for r in rows if r['timeframe']==tf and r['family']==family and r['side']=='BOTH' and r['scenario']=='base' and r['symbol']=='BTCUSDT']
        if len(selected)!=4:raise ValueError('primary_table_coverage_changed')
        key_rows.append(dict(timeframe=tf,family=family,symbol='BTCUSDT',side='BOTH',scenario='base',**aggregate(selected)))
    near_miss = {}
    for scenario in ('base','stress'):
        per_asset = {}
        for symbol in ('BTCUSDT','ETHUSDT','SOLUSDT'):
            selected = [r for r in rows if r['timeframe']=='8h' and r['family']=='B' and r['side']=='SHORT' and r['scenario']==scenario and r['symbol']==symbol]
            if len(selected)!=4:raise ValueError('positive_exception_coverage_changed')
            per_asset[symbol] = dict(aggregate(selected),
                positive_segments=sum(number(r,'net')>0 for r in selected),
                max_wait_hours=max(number(r,'max_wait_h') for r in selected),
                segments=[{'segment':r['segment'],'net':number(r,'net'),'closed':int(r['closed'])} for r in selected])
        total = sum((v['net'] for v in per_asset.values()),Decimal(0))
        near_miss[scenario] = {'assets':per_asset,'total_net':total,
                              'without_best_asset':total-max(v['net'] for v in per_asset.values())}
    return {'source_commit':'1dbd5ff337fcecd6b1fc08a1c3b3e3bbc9afccb0',
            'source_path':'research/usdm_screen_summary.csv.gz',
            'source_sha256':SOURCE_SHA256,'source_rows':len(rows),
            'accounting_max_abs_residual':residual,'short_timeframe_sensitivity':scenarios,
            'primary_btc_table':key_rows,'positive_8h_short_exception':near_miss,
            'limits':['Fixed-path attribution, not a new simulation',
                      'Separate accounts and reset segments; sums are not one account return',
                      'LONG, SHORT, BOTH, base and stress are related simulations, not independent evidence',
                      'Saved results use hypothetical historical steps and 10-USDT size; current executable filters differ',
                      'No trade-level replay or independent future sample is performed by this review']}


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--input',type=Path,default=ROOT/'research/usdm_screen_summary.csv.gz')
    p.add_argument('--output',type=Path,required=True)
    args=p.parse_args()
    result=review(args.input)
    args.output.parent.mkdir(parents=True,exist_ok=True)
    args.output.write_text(json.dumps(result,ensure_ascii=False,indent=2,default=str)+'\n')
    print(json.dumps({'rows_checked':result['source_rows'],
                      'short_timeframe_sensitivity':result['short_timeframe_sensitivity'],
                      'accounting_max_abs_residual':str(result['accounting_max_abs_residual'])},ensure_ascii=False))


if __name__=='__main__':main()

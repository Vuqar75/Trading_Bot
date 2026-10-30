"""Validate accounting, export metric tables and data-quality facts for a replay."""
import argparse
import csv
import hashlib
import json
import math
from datetime import datetime, timezone
from pathlib import Path


def summarize(report_path, candles_path, output):
    report=json.loads(report_path.read_text(encoding='utf-8'))
    digest=hashlib.sha256(candles_path.read_bytes()).hexdigest()
    assert digest==report['input']['sha256'],'Dataset hash mismatch'
    with candles_path.open(encoding='utf-8',newline='') as f: candles=list(csv.DictReader(f))
    closes=[float(c['close']) for c in candles]
    moves=[abs(b/a-1)*100 for a,b in zip(closes,closes[1:])]
    maximum=max(range(len(moves)),key=moves.__getitem__)
    def utc(ms):return datetime.fromtimestamp(ms/1000,timezone.utc).isoformat()
    quality=dict(rows=len(candles),first_utc=utc(int(candles[0]['open_time'])),
                 last_open_utc=utc(int(candles[-1]['open_time'])),
                 gaps=sum(int(b['open_time'])-int(a['open_time'])!=60000 for a,b in zip(candles,candles[1:])),
                 zero_volume=sum(float(c['volume'])==0 for c in candles),
                 max_minute_close_move_pct=moves[maximum],
                 max_move_at_utc=utc(int(candles[maximum+1]['open_time'])),
                 moves_over_1pct=sum(x>1 for x in moves),sha256=digest)
    metrics=[]
    for phase in ('train_candidates','holdout','holdout_stress'):
        for row in report[phase]:
            assert abs(row['reconciliation_error'])<1e-8
            assert abs(row['balance_error'])<1e-8
            if 'journal' in row:
                events=row['journal'];closing=[r for r in events if r['event']=='CLOSE']
                assert len(closing)==row['trades']
                assert abs(sum(r['net_pnl'] for r in closing)-row['net_pnl'])<1e-8
                assert abs(sum(r['entry_fee']+r['exit_fee'] for r in closing)-row['fees'])<1e-8
                peak=1000;dd=0
                for point in row['equity']:
                    peak=max(peak,point['equity']);dd=max(dd,peak-point['equity'])
                assert abs(dd-row['max_drawdown'])<1e-8
                for o,c in zip(events[::2],events[1::2]):
                    assert o['event']=='OPEN' and c['event']=='CLOSE'
                    assert o['position_id']==c['position_id'] and o['ts']<=c['ts']
            metrics.append(dict(phase=phase,variant=row['variant']['name'],
                                slope_bps=row['variant']['min_slope_bps'],
                                selected_on_train=row['variant'] in report['selected_on_train'],
                                start_utc=utc(row['start_ms']),end_utc=utc(row['end_ms']),
                                **row['costs'],**{k:row[k] for k in ('trades','long_trades','short_trades',
                                'net_pnl','gross_pnl','gross_before_slippage','max_drawdown',
                                'max_drawdown_pct','fees','slippage_cost','fees_share_of_loss_pct',
                                'fees_share_of_costs_pct','loss_cap_adjustment','forced_end_close')}))
    output.mkdir(parents=True,exist_ok=True)
    with (output/'metrics.csv').open('w',encoding='utf-8',newline='') as f:
        writer=csv.DictWriter(f,fieldnames=metrics[0]);writer.writeheader();writer.writerows(metrics)
    summary=dict(input=report['input'],quality=quality,winner_on_train=report['winner_on_train'],
                 selected_on_train=report['selected_on_train'],selection_rule=report['selection_rule'],
                 metrics=metrics,limitations=report['limitations'],accounting_validation='passed')
    (output/'summary.json').write_text(json.dumps(summary,ensure_ascii=False,indent=2),encoding='utf-8')
    return summary


if __name__=='__main__':
    p=argparse.ArgumentParser()
    p.add_argument('--report',type=Path,required=True)
    p.add_argument('--candles',type=Path,required=True)
    p.add_argument('--output',type=Path,required=True)
    a=p.parse_args()
    summary=summarize(a.report,a.candles,a.output)
    print(json.dumps({'quality':summary['quality'],'accounting_validation':summary['accounting_validation']},indent=2))

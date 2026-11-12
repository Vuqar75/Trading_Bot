"""Audit complete dynamic-exit fill journals without replaying prices."""
import gzip
import hashlib
import json
import math
from pathlib import Path
import sys


def same(a, b):
    if not math.isclose(a, b, rel_tol=1e-9, abs_tol=1e-7):
        raise ValueError(f'financial mismatch {a} {b}')


def audit(summary_path):
    summary_path = Path(summary_path)
    summary = json.loads(summary_path.read_text())
    stage = 'validation' if summary['stage'].startswith('validation') else 'development'
    journal_path = summary_path.parent/(stage+'_journal.json.gz')
    if hashlib.sha256(journal_path.read_bytes()).hexdigest() != summary['journal_sha256']:
        raise ValueError('journal checksum')
    journals = json.load(gzip.open(journal_path, 'rt'))
    checked = {}
    for key, journal in journals.items():
        saved = summary['results'][key]
        stress = key.endswith('stress'); impact = .001 if stress else .0005
        delay = (6 if stress else 1)*60000
        cash, prior_exit = 1000., -1
        scheduled = {(r['time_ms'], r['desired']): r for r in journal['signals'] if r['action']=='scheduled'}
        for p in journal['trades']:
            q, side = p['qty'], p['sign']
            if p['entry_ms'] != p['signal_ms']+delay or p['entry_ms'] < prior_exit or p['exit_ms'] > p['deadline_ms']:
                raise ValueError('timing/nonoverlap')
            if (p['signal_ms'], side) not in scheduled:
                raise ValueError('queue entry missing')
            entry_context = scheduled[(p['signal_ms'], side)]['selected_pair']
            if entry_context.get('action') != 'entry' or entry_context['entry_side'] != side:
                raise ValueError('entry evidence')
            if p['reason']=='mean_exit':
                signal = scheduled.get((p['exit_ms']-delay, 0))
                if signal is None:
                    raise ValueError('mean exit signal missing')
                context = signal['selected_pair']
                line = context.get('exit_line', context.get('sma5'))
                if not (context['close'] > line if side==1 else context['close'] < line):
                    raise ValueError('mean not reached on closed bar')
            if p['reason']=='time' and p['exit_ms'] != p['deadline_ms']:
                raise ValueError('time exit')
            asset = key.split('/')[0] if '/' in key else summary['selected'].split('/')[0]
            cap = 150 if asset=='BTCUSDT' else 100
            if q*max(p['entry'], p['mark_entry']) > cap+1e-8 or p['estimated_risk'] > 5+1e-8:
                raise ValueError('risk/cap')
            step = .01 if asset=='SOLUSDT' else .001
            same(q/step, round(q/step))
            if q*p['mark_entry'] < {'BTCUSDT':50,'ETHUSDT':20,'SOLUSDT':5}[asset]-1e-8:
                raise ValueError('minimum notional proxy')
            if q*p['entry']+p['entry_fee'] > cash+1e-8:
                raise ValueError('capital')
            same(p['stop'], p['entry']*(1-.02*side))
            same(p['entry'], p['entry_reference']*(1+side*impact))
            same(p['exit'], p['exit_reference']*(1-side*impact))
            same(p['fees'], .0005*q*(p['entry']+p['exit']))
            same(p['gross'], q*side*(p['exit_reference']-p['entry_reference']))
            same(p['impact'], q*(abs(p['entry']-p['entry_reference'])+abs(p['exit']-p['exit_reference'])))
            funding = 0.; seen = set()
            for e in p['funding_events']:
                if not p['entry_ms'] <= e['event_ms'] < p['exit_ms'] or e['event_ms'] in seen:
                    raise ValueError('funding timing')
                seen.add(e['event_ms'])
                amount = -side*q*e['mark_minute_open']*e['rate']
                same(e['applied_usdt'], min(amount, 0) if e['ambiguous'] else amount)
                funding += e['applied_usdt']
            same(p['funding'], funding)
            same(p['net'], q*side*(p['exit']-p['entry'])-p['fees']-p['liquidation_fee']+funding)
            cash += p['net']; prior_exit = p['exit_ms']
        same(cash, saved['cash']); same(cash, 1000+saved['net'])
        if len(journal['trades']) != saved['closed'] or saved['open'] or saved['pending'] or saved['unresolved']:
            raise ValueError('end state')
        for name in ('gross', 'fees', 'impact', 'funding', 'liquidation_fee', 'net'):
            same(sum(p[name] for p in journal['trades']), saved[name])
        checked[key] = {'closed': saved['closed'], 'fill_cost_funding_capital': 'PASS'}
    if summary['trading_admitted']:
        raise ValueError('unexpected admission')
    result = {'status': 'PASS', 'checked': checked, 'total_closed': sum(v['closed'] for v in checked.values()), 'new_simulations': 0}
    (summary_path.parent/(stage+'_audit.json')).write_text(json.dumps(result, indent=2)+'\n')
    print(json.dumps(result, indent=2))


if __name__ == '__main__':
    audit(sys.argv[1])

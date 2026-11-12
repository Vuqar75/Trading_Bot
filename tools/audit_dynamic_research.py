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
        scheduled = {(r['time_ms'], r['desired']): r for r in journal['signals'] if r['action'] in ('scheduled','scheduled_conditional')}
        for p in journal['trades']:
            q, side = p['qty'], p['sign']
            conditional='entry_window_end' in p
            timing_ok=(p['activation_ms']==p['signal_ms']+delay and p['activation_ms']<=p['entry_ms']<p['order_expiry_ms'] and
                       p['entry_window_end']==p['entry_ms']+60000 and p['entry_window_end']<=p['order_expiry_ms']) if conditional else p['entry_ms']==p['signal_ms']+delay
            if not timing_ok or p['entry_ms'] < prior_exit or p['exit_ms'] > p['deadline_ms']:
                raise ValueError('timing/nonoverlap')
            if (p['signal_ms'], side) not in scheduled:
                raise ValueError('queue entry missing')
            entry_context = scheduled[(p['signal_ms'], side)]['selected_pair']
            if entry_context.get('action') != 'entry' or entry_context['entry_side'] != side:
                raise ValueError('entry evidence')
            if conditional:
                o,h,l,_=p['entry_bar_ohlc'];trigger=entry_context['trigger_reference']
                if not (h>trigger if side==1 else l<trigger):raise ValueError('conditional trigger not crossed')
                same(p['entry_reference'],max(o,trigger) if side==1 else min(o,trigger))
                same(p['activation_ms'],entry_context['activation_ms']);same(p['order_expiry_ms'],entry_context['expiry_ms'])
            if p['reason'] in ('mean_exit','trend_exit'):
                signal = scheduled.get((p['exit_ms']-delay, 0))
                if signal is None:
                    raise ValueError('mean exit signal missing')
                context = signal['selected_pair']
                line = context.get('exit_line', context.get('sma5'))
                if p['reason']=='mean_exit' and 'exit_anchor' in p:
                    reached=context['close']>=line if side==1 else context['close']<=line
                else:reached=(context['close'] > line if side==1 else context['close'] < line) if p['reason']=='mean_exit' else (context['close'] < line if side==1 else context['close'] > line)
                if not reached:
                    raise ValueError('mean not reached on closed bar')
            if p['reason']=='time' and p['exit_ms'] != p['deadline_ms']:
                raise ValueError('time exit')
            asset = p.get('symbol') or (key.split('/')[0] if '/' in key else summary['selected'].split('/')[0])
            cap = 150 if asset=='BTCUSDT' else 100
            if q*max(p['entry'], p['mark_entry']) > cap+1e-8 or p['estimated_risk'] > 5+1e-8:
                raise ValueError('risk/cap')
            step = .01 if asset=='SOLUSDT' else .001 if asset in ('BTCUSDT','ETHUSDT') else 1
            same(q/step, round(q/step))
            if q*p['mark_entry'] < {'BTCUSDT':50,'ETHUSDT':20,'SOLUSDT':5}.get(asset,50)-1e-8:
                raise ValueError('minimum notional proxy')
            if q*p['entry']+p['entry_fee'] > cash+1e-8:
                raise ValueError('capital')
            if 'candidates' in entry_context:
                candidates=entry_context['candidates'];atr_recovery=p.get('stop_rule')=='volatility_two_atr'
                ordered=sorted(candidates,key=lambda r:(-r['score' if atr_recovery else 'room'],r['symbol']))
                if not ordered or ordered[0]['symbol']!=asset or ordered[0]['entry_side']!=side:raise ValueError('portfolio chose future/other candidate')
                for candidate in candidates:
                    if atr_recovery:
                        same(candidate['room'],candidate['entry_side']*(candidate['entry_mean']-candidate['close'])/candidate['close'])
                        same(candidate['score'],candidate['entry_side']*(candidate['entry_mean']-candidate['close'])/candidate['atr'])
                    else:same(candidate['room'],abs(candidate['vwap']/candidate['close']-1))
            if p.get('stop_rule')=='volatility_two_atr':
                atr=entry_context['atr'];fill=p['entry']
                distance=min(.05,max(.005,2*atr/fill))
                same(p['stop_atr'],atr);same(p['stop_pct'],distance)
                same(p['stop'],fill*(1-side*distance))
                same(p['estimated_risk'],q*fill*(distance+2*(.0005+impact)+.0021))
                if not (entry_context['hour_source_end_ms']<=p['signal_ms']<entry_context['expiry_ms']
                        and entry_context['daily_source_end_ms']<=p['signal_ms']
                        and p['entry_ms']<entry_context['expiry_ms']):raise ValueError('hour/day lookahead or expiry')
                previous,current=entry_context['partial_rsi_previous'],entry_context['partial_rsi']
                if not (previous<5<=current if side==1 else previous>95>=current):raise ValueError('RSI recovery absent')
                if not side*(entry_context['close']-entry_context['daily_sma200'])>0:raise ValueError('daily trend absent')
                if not side*(entry_context['previous_close']-entry_context['daily_sma200'])>0:raise ValueError('prior daily trend absent')
                if not (side*(fill-entry_context['daily_sma200'])>0 and side*(entry_context['entry_mean']-fill)/fill>=.006 and atr/fill>=.006):raise ValueError('delayed recovery geometry')
            elif 'stop_reference' in p:
                same(p['stop'],entry_context['stop_reference'])
                distance=side*(p['entry']-p['stop'])/p['entry']
                boundary_bad=False
                if 'trigger_reference' in p:
                    same(p['trigger_reference'],entry_context['trigger_reference'])
                    boundary_bad=(side*(p['entry_reference']-p['trigger_reference'])<0) if conditional else (side*(p['entry_reference']-p['trigger_reference'])<=0)
                if not 0<distance<=.02 or boundary_bad:
                    raise ValueError('known stop or delayed trigger')
                same(p['estimated_risk'],q*p['entry']*(distance+2*(.0005+impact)+.0021))
                if 'exit_anchor' in p:
                    same(p['exit_anchor'],entry_context['exit_anchor'])
                    end,start=entry_context['oi_end_ms'],entry_context['oi_start_ms'];setup=entry_context['setup_end_ms']
                    if not (0<=setup-end<=300000 and 0<=setup-3600000-start<=300000 and end<=p['signal_ms']-900000 and entry_context['anchor_asof_ms']<=setup-900000):
                        raise ValueError('OI/anchor lookahead')
                    drop=entry_context['oi_after']/entry_context['oi_before']-1;same(drop,entry_context['oi_drop'])
                    if p['variant']=='oi_filter' and drop>-.01:raise ValueError('OI filter')
                    def stress_net(reference):
                        fill=reference*(1-side*.001)
                        return side*(fill-p['entry'])-.0005*(p['entry']+fill)
                    if not stress_net(p['exit_anchor'])>0 or stress_net(p['exit_anchor'])+1e-8 < -stress_net(p['stop']):raise ValueError('entry cost geometry')
            else:
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

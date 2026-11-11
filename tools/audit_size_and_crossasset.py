"""Independent output audit; reads saved artifacts, no replay/network/API."""
import gzip,json,math,datetime as dt
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]/'research'
FOLDERS=['size_feasibility_results/rsi','size_feasibility_results/retest','rsi_size_2026_results','rsi_crossasset_results']
all_audits={}
for folder in FOLDERS:
    p=ROOT/folder
    if not (p/'summary.json').exists():continue
    summary=json.loads((p/'summary.json').read_text())
    ledger=json.load(gzip.open(p/'trades.json.gz','rt'))
    audits={}
    for key,rows in ledger.items():
        x=summary['results'][key];symbol=key.split('_')[0] if key.startswith(('BTC','ETH','SOL')) else 'BTCUSDT'
        stress='stress' in key;step=.01 if symbol=='SOLUSDT' else .001
        impact=.001 if stress else .0005;previous=0
        for t in rows:
            def eq(a,b):assert math.isclose(a,b,abs_tol=1e-8),(folder,key,t['id'],a,b)
            eq(t['gross'],t['qty']*t['sign']*(t['exit_reference']-t['entry_reference']))
            eq(t['net'],t['gross']-t['fees']-t['impact']+t['funding']-t['liquidation_fee'])
            eq(t['entry'],t['entry_reference']*(1+t['sign']*impact))
            eq(t['exit'],t['exit_reference']*(1-t['sign']*impact))
            eq(t['fees'],t['entry_fee']+t['exit_fee'])
            eq(t['fees'],.0005*t['qty']*(t['entry']+t['exit']))
            eq(t['funding'],sum(f['applied_usdt'] for f in t['funding_events']))
            assert all(t['entry_ms']<=f['event_ms']<=t['exit_ms'] for f in t['funding_events'])
            assert t['entry_ms']-t['signal_ms']==(6 if stress else 1)*60000
            assert previous<=t['entry_ms']<t['exit_ms'];previous=t['exit_ms']
            assert t['estimated_risk']<=5
            assert t['entry_notional']<= (150 if symbol=='BTCUSDT' else 100)+1e-8
            eq(t['qty']/step,round(t['qty']/step))
        closed=sum(t['net'] for t in rows)
        eq(closed,x['net_closed'])
        if not x['open']:eq(closed,x['net_including_open'])
        years={str(y):{'closed':sum(dt.datetime.fromtimestamp(t['exit_ms']/1000,dt.timezone.utc).year==y for t in rows),
                       'closed_net':sum(t['net'] for t in rows if dt.datetime.fromtimestamp(t['exit_ms']/1000,dt.timezone.utc).year==y)} for y in (2025,2026)}
        audits[key]={'audited_closed':len(rows),'result':'PASS','open_position':bool(x['open']),'exit_year_subtotals':years}
    (p/'independent_audit.json').write_text(json.dumps(audits,indent=2)+'\n')
    all_audits[folder]=audits
print(json.dumps(all_audits,indent=2))
p=ROOT/'rsi_crossasset_results'
if (p/'summary.json').exists():
    s=json.loads((p/'summary.json').read_text())['results']
    audit=all_audits['rsi_crossasset_results'];gates={}
    for symbol in ('BTCUSDT','ETHUSDT','SOLUSDT'):
        pairs=[s[symbol+'_'+mode] for mode in ('base','stress')];z=pairs[1]
        checks={
            'counts':all(x['closed']>=60 and x['long']>=20 and x['short']>=20 for x in pairs),
            'positive_both':all(x['net_including_open']>0 for x in pairs),
            'positive_each_exit_year':all(v['closed_net']>0 for mode in ('base','stress') for v in audit[symbol+'_'+mode]['exit_year_subtotals'].values()),
            'stress_without_best':z['net_including_open']-z['best_trade']>0,
            'drawdown':all(x['max_drawdown']<=.02 for x in pairs),
            'intraday':all(x['fraction_at_most_24h']>=.9 for x in pairs)}
        gates[symbol]={'checks':checks,'pass_all':all(checks.values())}
    (p/'gates.json').write_text(json.dumps(gates,indent=2)+'\n')

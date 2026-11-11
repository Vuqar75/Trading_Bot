"""Audit saved counterfactual trades and archive source-screening receipts."""
import gzip
import hashlib
import json
import math
from pathlib import Path
import subprocess

root=Path(__file__).resolve().parents[1]
out=root/'research/rsi15m_verification_results'
s=json.loads((out/'attribution.json').read_text())
rows=json.load(gzip.open(out/'attribution_trades.json.gz','rt'))
audit={}
for key, trades in rows.items():
    previous=0
    delay=int(key.split('_')[0].replace('delay',''))
    impact=int(key.split('_')[1].replace('impact',''))/10000
    for x in trades:
        def eq(a,b): assert math.isclose(a,b,abs_tol=1e-8),(key,x['id'],a,b)
        eq(x['gross'],x['qty']*x['sign']*(x['exit_reference']-x['entry_reference']))
        eq(x['net'],x['gross']-x['fees']-x['impact']+x['funding']-x['liquidation_fee'])
        eq(x['entry'],x['entry_reference']*(1+x['sign']*impact))
        eq(x['exit'],x['exit_reference']*(1-x['sign']*impact))
        eq(x['fees'],.0005*x['qty']*(x['entry']+x['exit']))
        eq(x['funding'],sum(f['applied_usdt'] for f in x['funding_events']))
        assert x['entry_ms']-x['signal_ms']==delay*60000
        assert previous<=x['entry_ms']<x['exit_ms']
        assert 50<=x['entry_notional']<=100 and x['estimated_risk']<=5
        previous=x['exit_ms']
    eq(sum(x['net'] for x in trades),s['results'][key]['net_including_open'])
    audit[key]={'closed_trades':len(trades),'ledger_time_cost_and_limits':'PASS'}
(out/'attribution_audit.json').write_text(json.dumps(audit,indent=2)+'\n')
receipts=[]
for name, folder in [('vwap','external-vwap-review'),('crosssection','external-crosssection-review')]:
    checkout=root.parent/folder
    if not checkout.exists():
        checkout=Path('/workspace/work')/folder
    commit=subprocess.check_output(['git','rev-parse','HEAD'],cwd=checkout,text=True).strip()
    files=subprocess.check_output(['git','ls-files'],cwd=checkout,text=True).splitlines()
    receipts.append({'name':name,'commit':commit,'files':{
        f:hashlib.sha256((checkout/f).read_bytes()).hexdigest() for f in files}})
(root/'research/external_strategy_review/followup_receipts.json').write_text(json.dumps(receipts,indent=2)+'\n')
print(json.dumps(audit,indent=2))

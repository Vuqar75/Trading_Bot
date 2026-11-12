"""Independent arithmetic/as-of and execution audit of a fixed error penalty."""
import gzip
import hashlib
import json
import math
from pathlib import Path
import sys

from research import panel_net_calibration as c
from research.usdm_intraday_data import zipped
from tools.audit_dynamic_research import audit,same


def main(out,raw,root):
    audit(out/'development.json')
    summary=json.loads((out/'development.json').read_text())
    original=json.load(gzip.open(raw/'predictions.json.gz','rt'));rows=json.load(gzip.open(raw/'dataset.json.gz','rt'))
    rows={(r['T'],r['symbol']):r for r in rows}
    adjusted=json.load(gzip.open(out/'corrected_predictions.json.gz','rt'))
    if len(original)!=len(adjusted):raise ValueError('missing corrected forecast')
    buckets={}
    for r in original:
        row=rows[(r['T'],r['symbol'])]
        for side,field in ((1,'long'),(-1,'short')):
            target=row['labels'][str(side)]
            if r[field]<.0005 or target is None:continue
            day=r['T']//c.DAY*c.DAY
            bucket=buckets.setdefault((r['model'],side,day),{'values':[],'end':0})
            bucket['values'].append(target['net_return']-r[field]);bucket['end']=max(bucket['end'],row['label_end_ms'])
    verified=set();qualified=0
    for before,after in zip(original,adjusted):
        if (before['T'],before['symbol'],before['model'])!=(after['T'],after['symbol'],after['model']):raise ValueError('association')
        day=before['T']//c.DAY*c.DAY
        for side,field in ((1,'long'),(-1,'short')):
            info=after['calibration'][str(side)]
            same(after['raw_'+field],before[field])
            key=(before['model'],side,day,before['T']-day>=246*c.p.M)
            if key not in verified:
                history=[]
                for (model,s,d),b in buckets.items():
                    if model!=before['model'] or s!=side:continue
                    if not day-30*c.DAY<=d<day or d+c.DAY+246*c.p.M>before['T']:continue
                    if b['end']>before['T']:raise ValueError('future target leaked')
                    history.append({'day_start_ms':d,'release_ms':d+c.DAY+246*c.p.M,
                        'mean_error':float(sum(b['values'])/len(b['values'])),'count':len(b['values']),'latest_label_end_ms':b['end']})
                history.sort(key=lambda b:b['day_start_ms']);count=sum(b['count'] for b in history)
                ready=len(history)>=14 and count>=100
                if info['ready']!=ready or info['days']!=len(history) or info['observations']!=count:raise ValueError('history eligibility')
                if info['latest_label_end_ms']!=max((b['latest_label_end_ms'] for b in history),default=None):raise ValueError('asof evidence')
                if ready:
                    values=sorted(b['mean_error'] for b in history);x=.25*(len(values)-1);i=math.floor(x)
                    percentile=values[i]+(x-i)*(values[min(i+1,len(values)-1)]-values[i]);same(info['penalty'],min(0,percentile))
                elif info['penalty'] is not None:raise ValueError('unready penalty')
                verified.add(key)
            expected=before[field]+info['penalty'] if info['ready'] else -1.
            same(after[field],expected)
            if after[field]>before[field] and info['ready']:raise ValueError('forecast increased')
            qualified+=after[field]>=.0005
    journals=json.load(gzip.open(out/'development_journal.json.gz','rt'))
    amap={(r['model'],r['T'],r['symbol']):r for r in adjusted};fund={s:{} for s in c.p.ASSETS}
    for s in c.p.ASSETS:
        for source in summary['sources'][s]:
            if source['kind']!='fundingRate':continue
            path=root/source['file']
            if hashlib.sha256(path.read_bytes()).hexdigest()!=source['sha256']:raise ValueError('fund source drift')
            for row in zipped(path):
                if row[0].isdigit():fund[s][int(row[0])]=float(row[2])
    matched=0
    for case,journal in journals.items():
        model=case.split('/')[0];result=summary['results'][case];by={s:0. for s in c.p.ASSETS};months=set();quarters={'2025Q3':0.,'2025Q4':0.}
        import datetime as dt
        for p in journal['trades']:
            symbol,T,side=p['symbol'],p['signal_ms'],p['sign'];r=amap[(model,T,symbol)];field='long' if side==1 else 'short'
            same(p['predicted_stress_net'],r[field])
            if r[field]<.0005 or not r['calibration'][str(side)]['ready']:raise ValueError('unqualified corrected entry')
            if case.endswith('stress'):
                target=rows[(T,symbol)]['labels'][str(side)]
                for f in ('net','gross','fees','impact','funding','qty','entry','exit','entry_ms','exit_ms'):same(p[f],target[f])
                if p['reason']!=target['reason']:raise ValueError('target exit')
                matched+=1
            expected={t:rate for t,rate in fund[symbol].items() if p['entry_ms']<=t<p['exit_ms']}
            if expected!={e['event_ms']:e['rate'] for e in p['funding_events']}:raise ValueError('actual fund event missing')
            by[symbol]+=p['net'];months.add(dt.datetime.fromtimestamp(p['entry_ms']/1000,dt.timezone.utc).strftime('%Y-%m'))
            quarters['2025Q3' if p['exit_ms']<c.p.core.stamp('2025-10-01') else '2025Q4']+=p['net']
        for s in c.p.ASSETS:same(by[s],result['by_asset'][s]['net'])
        same(result['without_best_asset'],result['net']-max(by.values()))
        if len(months)!=result['active_months']:raise ValueError('active months')
        for q,v in quarters.items():same(v,result['quarters'][q])
    checks={m:c.p.criteria(summary['results'][m+'/base'],summary['results'][m+'/stress']) for m in c.p.MODELS}
    allowed=[m for m in c.p.MODELS if all(checks[m].values())];allowed.sort(key=lambda m:(-summary['results'][m+'/stress']['net'],summary['results'][m+'/stress']['drawdown'],m))
    if checks!=summary['checks'] or summary['selected']!=(allowed[0] if allowed else None):raise ValueError('selection gate')
    result={'status':'PASS','directional_forecasts_checked':2*len(adjusted),'eligible_adjusted_forecasts':qualified,
            'independent_daily_penalties':len(verified),'stress_fills_matched':matched,'selected':summary['selected'],'refits':0}
    (out/'calibration_execution_audit.json').write_text(json.dumps(result,indent=2)+'\n');print(json.dumps(result,indent=2))


if __name__=='__main__':main(Path(sys.argv[1]),Path(sys.argv[2]),Path(sys.argv[3]))

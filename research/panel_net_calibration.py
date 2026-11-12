"""One registered past-only empirical correction, with no training or APIs."""
import gzip
import hashlib
import json
from pathlib import Path
import sys

import numpy as np

from research import panel_net_ml as p

DAY=1440*p.M


def day_errors(predictions,rows):
    rowmap={(r['T'],r['symbol']):r for r in rows};buckets={}
    for r in predictions:
        row=rowmap[(r['T'],r['symbol'])]
        for side,field in ((1,'long'),(-1,'short')):
            target=row['labels'][str(side)]
            if r[field]<.0005 or target is None:continue
            day=r['T']//DAY*DAY;key=(r['model'],side,day)
            b=buckets.setdefault(key,{'errors':[],'latest_label_end_ms':0})
            b['errors'].append(target['net_return']-r[field])
            b['latest_label_end_ms']=max(b['latest_label_end_ms'],row['label_end_ms'])
    result={}
    for (model,side,day),b in buckets.items():
        result.setdefault((model,side),[]).append({'day_start_ms':day,'release_ms':day+DAY+246*p.M,
            'mean_error':float(np.mean(b['errors'])),'count':len(b['errors']),'latest_label_end_ms':b['latest_label_end_ms']})
    for key in result:result[key].sort(key=lambda r:r['day_start_ms'])
    return result


def correct(predictions,rows):
    buckets=day_errors(predictions,rows);output=[];cache={}
    for r in predictions:
        new=dict(r);new['raw_long'],new['raw_short']=r['long'],r['short'];new['calibration']={}
        day=r['T']//DAY*DAY
        for side,field in ((1,'long'),(-1,'short')):
            key=(r['model'],side,day,r['T']-day>=246*p.M)
            if key not in cache:
                known=[b for b in buckets.get((r['model'],side),[]) if day-30*DAY<=b['day_start_ms']<day and b['release_ms']<=r['T']]
                count=sum(b['count'] for b in known);ready=len(known)>=14 and count>=100
                penalty=min(0.,float(np.percentile([b['mean_error'] for b in known],25))) if ready else None
                cache[key]={'ready':ready,'days':len(known),'observations':count,'penalty':penalty,
                    'latest_label_end_ms':max((b['latest_label_end_ms'] for b in known),default=None),
                    'history_sha256':hashlib.sha256(json.dumps(known,sort_keys=True,separators=(',',':')).encode()).hexdigest()}
            info=cache[key];new['calibration'][str(side)]=info
            # Explicit sentinel for an ineligible forecast, never an estimate.
            new[field]=r[field]+info['penalty'] if info['ready'] else -1.
        output.append(new)
    return output


def run(root,raw,out):
    project=Path(__file__).resolve().parents[1];receipt=json.loads((project/'PANEL_NET_CALIBRATION_INPUTS.json').read_text())
    for n,h in receipt['sha256'].items():
        if hashlib.sha256((project/n).read_bytes()).hexdigest()!=h:raise ValueError('registered input changed '+n)
    if hashlib.sha256((root/'manifest.json').read_bytes()).hexdigest()!=receipt['manifest_sha256']:raise ValueError('source drift')
    out.mkdir(parents=True,exist_ok=True)
    rows=json.load(gzip.open(raw/'dataset.json.gz','rt'));predictions=json.load(gzip.open(raw/'predictions.json.gz','rt'))
    adjusted=correct(predictions,rows);p.common.write_gzip(out/'corrected_predictions.json.gz',adjusted)
    p.core.START=p.core.stamp('2024-12-01');p.core.BEGIN=p.core.stamp('2025-01-01');p.core.END=p.core.stamp('2026-01-01')
    markets={};proofs={}
    for s in p.ASSETS:
        a,b,v,f,proof=p.source.load(root,s,p.core.START,p.core.BEGIN,p.core.END)
        markets[s]=(a,b,v,f);proofs[s]=proof;print('verified correction input',s,flush=True)
    results,journals,checks={},{},{}
    signature=hashlib.sha256(json.dumps(receipt,sort_keys=True).encode()).hexdigest()
    for model in p.MODELS:
        for stress in (False,True):
            key=model+('/stress' if stress else '/base');name=key.replace('/','-');part=out/(name+'.json.gz');meta=out/(name+'-receipt.json')
            if meta.exists():
                old=json.loads(meta.read_text())
                if old['signature']!=signature or old['sha256']!=hashlib.sha256(part.read_bytes()).hexdigest():raise ValueError('correction checkpoint drift')
                payload=json.load(gzip.open(part,'rt'));results[key],journals[key]=payload['result'],payload['journal']
            else:
                results[key],journals[key]=p.simulate(markets,adjusted,model,stress,p.core.stamp('2025-07-01'),p.core.END)
                p.common.write_gzip(part,{'result':results[key],'journal':journals[key]})
                meta.write_text(json.dumps({'signature':signature,'sha256':hashlib.sha256(part.read_bytes()).hexdigest()},indent=2)+'\n')
            print(key,results[key]['closed'],results[key]['net'],flush=True)
        checks[model]=p.criteria(results[model+'/base'],results[model+'/stress'])
    allowed=[m for m in p.MODELS if all(checks[m].values())];allowed.sort(key=lambda m:(-results[m+'/stress']['net'],results[m+'/stress']['drawdown'],m))
    p.common.write_gzip(out/'development_journal.json.gz',journals)
    summary={'stage':'development2025H2','selected':allowed[0] if allowed else None,'results':results,'checks':checks,
        'sources':proofs,'universe':list(p.ASSETS),'refits':0,'trading_admitted':False,
        'journal_sha256':hashlib.sha256((out/'development_journal.json.gz').read_bytes()).hexdigest()}
    (out/'development.json').write_text(json.dumps(summary,indent=2)+'\n')
    (out/'selection.json').write_text(json.dumps({'selected':summary['selected'],'development_sha256':hashlib.sha256((out/'development.json').read_bytes()).hexdigest(),'validation_performed':False,'trading_admitted':False},indent=2)+'\n')
    print(json.dumps({'selected':summary['selected'],'checks':checks},indent=2))


if __name__=='__main__':run(Path(sys.argv[1]),Path(sys.argv[2]),Path(sys.argv[3]))

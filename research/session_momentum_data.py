"""Restore only frozen public archives; never replace a pinned revision."""
import hashlib, json, pathlib, sys, urllib.request
from concurrent.futures import ThreadPoolExecutor

root=pathlib.Path(sys.argv[1]); root.mkdir(parents=True, exist_ok=True)
items=[]
for name in ('research/usdm_1m_manifest.json','research/usdm_5m_manifest.json'):
    m=json.loads(pathlib.Path(name).read_text())
    for x in m['files']:
        if x['symbol'] not in ('BTCUSDT','ETHUSDT') or 'sha256' not in x: continue
        if not ((x['year'],x['month'])>=(2024,12) and (x['year'],x['month'])<=(2026,9)): continue
        if name.endswith('1m_manifest.json') and x['kind'] not in ('klines','markPriceKlines'): continue
        if name.endswith('5m_manifest.json') and x['kind']!='fundingRate': continue
        items.append(x)

def fetch(x):
    path=root/x['file']; path.parent.mkdir(parents=True,exist_ok=True)
    if path.exists(): data=path.read_bytes()
    else:
        with urllib.request.urlopen(x['url'],timeout=90) as f: data=f.read()
    sha=hashlib.sha256(data).hexdigest()
    if sha!=x['sha256']: return {'file':x['file'],'error':'sha256_mismatch','actual':sha,'expected':x['sha256']}
    if not path.exists():
        temp=path.with_suffix('.part');temp.write_bytes(data);temp.replace(path)
    return {'file':x['file'],'sha256':sha,'bytes':len(data)}

def safe(x):
    try:return fetch(x)
    except Exception as e:return {'file':x['file'],'error':type(e).__name__}

with ThreadPoolExecutor(max_workers=6) as pool: result=list(pool.map(safe,items))
out={'files':result,'errors':sum('error' in x for x in result)}
(root/'restore-report.json').write_text(json.dumps(out,indent=2))
print(json.dumps({'files':len(result),'errors':out['errors'],'failed':[x for x in result if 'error' in x]}))
if out['errors']:sys.exit(1)

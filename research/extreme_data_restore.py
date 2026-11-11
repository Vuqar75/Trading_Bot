"""Restore only missing SHA-pinned public archives to a user-specified D: cache."""
import concurrent.futures,hashlib,json,pathlib,sys,urllib.request

root=pathlib.Path(sys.argv[1])
if root.drive.upper()!='D:':raise ValueError('restore requires D: cache')
revised={('BTCUSDT','klines'):'da3ed73cd4316854f48567321752ddc53f976cc37c9ef1fa57c1230eb0e17fec',
         ('ETHUSDT','klines'):'41cd347fd1ed1875a3ba78e245e377626dcd68528f742ba7a72694158967e26f',
         ('BTCUSDT','markPriceKlines'):'2c8f71e53dcb28d94deb310cff7544d4cebc033f2e4e6db77258ecd445acc31b',
         ('ETHUSDT','markPriceKlines'):'6c1c11a2e92c3be00f2a45041861e4edf7520469dfe69fc543e51904675934b9'}
items=[]
for mf in ('research/usdm_1m_manifest.json','research/usdm_5m_manifest.json'):
    d=json.loads(pathlib.Path(mf).read_text())
    for x in d['files']:
        if x.get('symbol') not in ('BTCUSDT','ETHUSDT') or (x.get('year',0),x.get('month',0))<(2024,12):continue
        if mf.endswith('1m_manifest.json') and x['kind'] not in ('klines','markPriceKlines'):continue
        if mf.endswith('5m_manifest.json') and x['kind']!='fundingRate':continue
        if mf.endswith('1m_manifest.json') and (x['year'],x['month'])==(2026,10):
            x=dict(x,old_sha256=x['sha256'],sha256=revised[(x['symbol'],x['kind'])],file='revised-oct-2026/'+x['file'])
        items.append(x)
    if mf.endswith('1m_manifest.json'):
        items.extend(x for x in d.get('rest_gap_fills',[]) if x['symbol'] in ('BTCUSDT','ETHUSDT'))

def one(x):
    path=root/x['file'];path.parent.mkdir(parents=True,exist_ok=True)
    try:
        if path.exists():raw=path.read_bytes(); origin='cache'
        else:
            with urllib.request.urlopen(x['url'],timeout=90) as f:raw=f.read()
            origin='download'
        actual=hashlib.sha256(raw).hexdigest()
        if actual!=x['sha256']:return {'file':x['file'],'status':'revision_mismatch','expected':x['sha256'],'actual':actual}
        if not path.exists():
            temp=path.with_suffix(path.suffix+'.part');temp.write_bytes(raw);temp.replace(path)
        return {'file':x['file'],'status':'verified','sha256':actual,'bytes':len(raw),'origin':origin}
    except Exception as e:return {'file':x['file'],'status':'unavailable','error':type(e).__name__}

with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:result=list(pool.map(one,items))
out={'files':result,'unavailable':[x for x in result if x['status']!='verified']}
(root/'extreme-restore-manifest.json').write_text(json.dumps(out,indent=2),encoding='utf8')
print(json.dumps({'files':len(result),'missing':out['unavailable']},indent=2))

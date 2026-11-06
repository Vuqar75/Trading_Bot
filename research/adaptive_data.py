"""Fetch immutable Binance public USD-M futures data; never uses account APIs."""
import concurrent.futures,datetime as dt,hashlib,json,os,time,urllib.request,urllib.parse,zipfile,csv,io
from pathlib import Path
BASE='https://data.binance.vision/data/futures/um/monthly'
def months():return [(2023,12)]+[(y,m) for y in (2024,2025,2026) for m in range(1,13) if (y,m)<=(2026,10)]
def fetch(item):
 kind,y,m,root=item
 name=f'BTCUSDT-15m-{y}-{m:02}.zip' if kind in ('klines','mark') else f'BTCUSDT-fundingRate-{y}-{m:02}.zip'
 folder={'klines':'klines/BTCUSDT/15m','mark':'markPriceKlines/BTCUSDT/15m','funding':'fundingRate/BTCUSDT'}[kind];url=f'{BASE}/{folder}/{name}';p=root/(('MARK-' if kind=='mark' else '')+name)
 def read(u):
  for i in range(3):
   try:
    with urllib.request.urlopen(u,timeout=25) as r:return r.read()
   except (OSError,TimeoutError):
    if i==2:raise
    time.sleep(2**i)
 checksum=read(url+'.CHECKSUM').decode().split()[0].lower()
 if not p.exists() or hashlib.sha256(p.read_bytes()).hexdigest()!=checksum:
  raw=read(url)
  if hashlib.sha256(raw).hexdigest()!=checksum:raise ValueError('checksum_mismatch')
  temp=p.with_suffix('.tmp');temp.write_bytes(raw);os.replace(temp,p)
 return {'file':p.name,'sha256':checksum,'url':url,'bytes':p.stat().st_size,'kind':kind}
def download(root):
 root=Path(root);root.mkdir(parents=True,exist_ok=True)
 with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:items=list(pool.map(fetch,[(k,y,m,root) for k in ('klines','funding','mark') for y,m in months()]))
 manifest={'captured_utc':dt.datetime.now(dt.timezone.utc).isoformat(),'files':items,'rest_gap_fills':gap_supplements(root)};(root/'adaptive-futures-manifest.json').write_text(json.dumps(manifest,indent=2,sort_keys=True));return manifest
def gap_supplements(root):
 root=Path(root);result=[]
 for kind,day in [('klines','2026-10-28'),('markPriceKlines','2026-06-29'),('markPriceKlines','2026-10-28'),('markPriceKlines','2026-10-29')]:
  start=int(dt.datetime.fromisoformat(day).replace(tzinfo=dt.timezone.utc).timestamp()*1000);params=urllib.parse.urlencode({'symbol':'BTCUSDT','interval':'15m','startTime':start,'endTime':start+86400000-1,'limit':100});url='https://fapi.binance.com/fapi/v1/'+kind+'?'+params
  with urllib.request.urlopen(url,timeout=20) as r:raw=r.read()
  values=json.loads(raw)
  if len(values)!=96 or any(int(x[0])!=start+i*900000 or int(x[6])!=start+(i+1)*900000-1 for i,x in enumerate(values)):raise ValueError('rest_supplement_incomplete')
  name='REST-'+kind+'-'+day+'.json';(root/name).write_bytes(raw);result.append({'file':name,'sha256':hashlib.sha256(raw).hexdigest(),'url':url,'captured_utc':dt.datetime.now(dt.timezone.utc).isoformat(),'records':len(values)})
 return result
def rows(path):
 with zipfile.ZipFile(path) as z:
  if len(z.namelist())!=1:raise ValueError('invalid_archive')
  return list(csv.reader(io.TextIOWrapper(z.open(z.namelist()[0]))))
if __name__=='__main__':
 import sys
 x=download(sys.argv[1]);print('downloaded',len(x['files']),'bytes',sum(y['bytes'] for y in x['files']))

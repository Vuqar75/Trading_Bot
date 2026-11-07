"""Checksum-pinned public USD-M archive acquisition and strict parsing."""
import csv,datetime as dt,hashlib,io,json,os,sys,time,urllib.error,urllib.request,zipfile
import urllib.parse
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from research.intraday_spot import BAR,SYMBOLS,months,stamp
BASE='https://data.binance.vision/data/futures/um/monthly'
def get(url):
 for i in range(3):
  try:
   with urllib.request.urlopen(url,timeout=40) as r:return r.read()
  except (OSError,TimeoutError):
   if i==2:raise
   time.sleep(2**i)
def fetch(item):
 root,kind,s,y,m=item
 name=f'{s}-fundingRate-{y}-{m:02d}.zip' if kind=='fundingRate' else f'{s}-5m-{y}-{m:02d}.zip'
 folder=f'{kind}/{s}' if kind=='fundingRate' else f'{kind}/{s}/5m'
 url=f'{BASE}/{folder}/{name}';path=root/kind/name;path.parent.mkdir(parents=True,exist_ok=True)
 try:
  digest=get(url+'.CHECKSUM').decode().split()[0].lower()
  if path.exists() and hashlib.sha256(path.read_bytes()).hexdigest()!=digest:raise ValueError('source archive revision '+str(path))
  if not path.exists():
   raw=get(url)
   if hashlib.sha256(raw).hexdigest()!=digest:raise ValueError('archive checksum mismatch')
   tmp=path.with_suffix('.tmp');tmp.write_bytes(raw);os.replace(tmp,path)
  return {'file':str(path.relative_to(root)),'kind':kind,'symbol':s,'year':y,'month':m,'sha256':digest,'url':url,'bytes':path.stat().st_size,'checked_utc':dt.datetime.now(dt.timezone.utc).isoformat()}
 except Exception as e:return {'file':str(path.relative_to(root)),'kind':kind,'symbol':s,'year':y,'month':m,'url':url,'error':type(e).__name__}
def acquire(root):
 root=Path(root);root.mkdir(parents=True,exist_ok=True)
 req=[(root,k,s,y,m) for k in ('klines','markPriceKlines','fundingRate') for s in SYMBOLS for y,m in months()]
 with ThreadPoolExecutor(max_workers=6) as pool:items=list(pool.map(fetch,req))
 manifest={'created_utc':dt.datetime.now(dt.timezone.utc).isoformat(),'files':items};(root/'manifest.json').write_text(json.dumps(manifest,indent=2));return manifest
def fetch_minute(item):
 root,kind,s,y,m=item;name=f'{s}-1m-{y}-{m:02d}.zip';url=f'{BASE}/{kind}/{s}/1m/{name}';path=root/'minute'/kind/name;path.parent.mkdir(parents=True,exist_ok=True)
 try:
  digest=get(url+'.CHECKSUM').decode().split()[0].lower()
  if path.exists() and hashlib.sha256(path.read_bytes()).hexdigest()!=digest:raise ValueError('source revision')
  if not path.exists():
   raw=get(url)
   if hashlib.sha256(raw).hexdigest()!=digest:raise ValueError('checksum mismatch')
   tmp=path.with_suffix('.tmp');tmp.write_bytes(raw);os.replace(tmp,path)
  return {'file':str(path.relative_to(root)),'kind':kind,'symbol':s,'year':y,'month':m,'sha256':digest,'url':url,'bytes':path.stat().st_size,'checked_utc':dt.datetime.now(dt.timezone.utc).isoformat()}
 except Exception as e:return {'file':str(path.relative_to(root)),'kind':kind,'symbol':s,'year':y,'month':m,'url':url,'error':type(e).__name__}
def acquire_minutes(root):
 root=Path(root);req=[(root,k,s,y,m) for k in ('klines','markPriceKlines') for s in SYMBOLS for y,m in months()]
 with ThreadPoolExecutor(max_workers=6) as pool:items=list(pool.map(fetch_minute,req))
 manifest={'created_utc':dt.datetime.now(dt.timezone.utc).isoformat(),'files':items};(root/'minute-manifest.json').write_text(json.dumps(manifest,indent=2));return manifest
def zipped(path):
 with zipfile.ZipFile(path) as z:
  if len(z.namelist())!=1:raise ValueError('unexpected member')
  return list(csv.reader(io.TextIOWrapper(z.open(z.namelist()[0]))))
def supplement(root,interval='5m'):
 if interval not in ('1m','5m'):raise ValueError('invalid supplement interval')
 step=60000 if interval=='1m' else BAR;count=86400000//step
 root=Path(root);path=root/('minute-manifest.json' if interval=='1m' else 'manifest.json');manifest=json.loads(path.read_text());out=[]
 for symbol in SYMBOLS:
  for kind,day in [('klines','2026-10-28'),('markPriceKlines','2026-06-29'),('markPriceKlines','2026-10-28'),('markPriceKlines','2026-10-29')]:
   start=stamp(day);params=urllib.parse.urlencode({'symbol':symbol,'interval':interval,'startTime':start,'endTime':start+86400000-1,'limit':count})
   url='https://fapi.binance.com/fapi/v1/'+kind+'?'+params;raw=get(url);values=json.loads(raw)
   if len(values)!=count or any(int(v[0])!=start+i*step or int(v[6])!=start+(i+1)*step-1 for i,v in enumerate(values)):raise ValueError('REST supplement incomplete')
   name=f'{"minute/" if interval=="1m" else ""}rest/{symbol}-{kind}-{day}.json';p=root/name;p.parent.mkdir(parents=True,exist_ok=True);digest=hashlib.sha256(raw).hexdigest()
   if p.exists() and hashlib.sha256(p.read_bytes()).hexdigest()!=digest:raise ValueError('REST revision requires review')
   if not p.exists():p.write_bytes(raw)
   out.append({'file':name,'sha256':digest,'url':url,'symbol':symbol,'kind':kind,'records':len(values),'received_utc':dt.datetime.now(dt.timezone.utc).isoformat()})
 manifest['rest_gap_fills']=out;path.write_text(json.dumps(manifest,indent=2));return out
def read(root,manifest,symbol,kind,*,interval='5m'):
 step=60000 if interval=='1m' else BAR
 root=Path(root);out=[];entries=[x for x in manifest['files'] if x['symbol']==symbol and x['kind']==kind]
 if len(entries)!=len(months()) or any('error' in x for x in entries):raise ValueError('archive unavailable: '+symbol+' '+kind)
 for x in entries:
  p=root/x['file']
  if hashlib.sha256(p.read_bytes()).hexdigest()!=x['sha256']:raise ValueError('archive hash changed')
  for r in zipped(p):
   if not r[0].isdigit():continue
   if kind=='fundingRate':
    t,interval,rate=int(r[0]),int(r[1]),float(r[2])
    if interval<=0 or abs(rate)>.1:raise ValueError('invalid funding')
    out.append((t,interval,rate))
   else:
    t,end=int(r[0]),int(r[6]);o,h,l,c=map(float,r[1:5]);volume=float(r[5])
    if t%step or end!=t+step-1 or not 0<l<=min(o,c)<=max(o,c)<=h:raise ValueError('bad USD-M candle')
    out.append((t,o,h,l,c,volume))
 if kind!='fundingRate':
  for x in manifest.get('rest_gap_fills',[]):
   if x['symbol']!=symbol or x['kind']!=kind:continue
   raw=(root/x['file']).read_bytes()
   if hashlib.sha256(raw).hexdigest()!=x['sha256']:raise ValueError('REST hash changed')
   values=json.loads(raw)
   if len(values)!=x['records']:raise ValueError('REST count changed')
   for r in values:
    t,end=int(r[0]),int(r[6]);o,h,l,c=map(float,r[1:5]);volume=float(r[5])
    if t%step or end!=t+step-1 or not 0<l<=min(o,c)<=max(o,c)<=h:raise ValueError('bad REST candle')
    out.append((t,o,h,l,c,volume))
  out.sort()
 if kind=='fundingRate':
  if any(b[0]<=a[0] for a,b in zip(out,out[1:])):raise ValueError('duplicate funding')
 else:
  start=stamp('2024-12-01');end=stamp('2026-11-01')
  gaps=[]
  for a,b in zip(out,out[1:]):
   if b[0]-a[0]!=step:gaps.append((a[0],b[0],(b[0]-a[0])//step-1))
  if not out or out[0][0]!=start or out[-1][0]!=end-step or gaps:raise ValueError('candle gaps '+symbol+' '+kind+' '+str(gaps[:5]))
 return out
if __name__=='__main__':
 if len(sys.argv)>2 and sys.argv[2]=='supplement':print(json.dumps({'supplements':len(supplement(sys.argv[1]))}))
 elif len(sys.argv)>2 and sys.argv[2]=='supplement1m':print(json.dumps({'supplements':len(supplement(sys.argv[1],'1m'))}))
 elif len(sys.argv)>2 and sys.argv[2]=='minutes':
  m=acquire_minutes(sys.argv[1]);print(json.dumps({'files':len(m['files']),'failed':[x for x in m['files'] if 'error' in x]}))
 else:
  m=acquire(sys.argv[1]);bad=[x for x in m['files'] if 'error' in x];print(json.dumps({'files':len(m['files']),'failed':bad},ensure_ascii=False))

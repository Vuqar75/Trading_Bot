"""Check finite public trade volume and funding rates in pinned source files."""
import hashlib,json,math,sys
from pathlib import Path
from research.usdm_intraday_data import zipped
root=Path(sys.argv[1]);count={'trade_volume':0,'funding_rate':0}
for name in ('manifest.json','minute-manifest.json'):
 manifest=json.loads((root/name).read_text())
 for x in manifest['files']:
  if x['kind'] not in ('klines','fundingRate'):continue
  p=root/x['file']
  if hashlib.sha256(p.read_bytes()).hexdigest()!=x['sha256']:raise ValueError('hash mismatch')
  for r in zipped(p):
   if not r[0].isdigit():continue
   k='trade_volume' if x['kind']=='klines' else 'funding_rate';v=float(r[5] if k=='trade_volume' else r[2])
   if not math.isfinite(v) or (k=='trade_volume' and v<0):raise ValueError('invalid '+k)
   count[k]+=1
 for x in manifest.get('rest_gap_fills',[]):
  if x['kind']!='klines':continue
  raw=(root/x['file']).read_bytes()
  if hashlib.sha256(raw).hexdigest()!=x['sha256']:raise ValueError('REST hash mismatch')
  for r in json.loads(raw):
   v=float(r[5])
   if not math.isfinite(v) or v<0:raise ValueError('invalid REST volume')
   count['trade_volume']+=1
print(json.dumps(count,sort_keys=True))

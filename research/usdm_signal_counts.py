"""Count raw closed-bar signals separately from filled/accepted entries."""
import argparse,gc,hashlib,json
from pathlib import Path
from research.intraday_spot import SYMBOLS,SEGMENTS,stamp
from research.usdm_intraday_data import read
from research.usdm_screen import FRAMES,allowed,signals
p=argparse.ArgumentParser();p.add_argument('--data',required=True);p.add_argument('--output',required=True);a=p.parse_args();root=Path(a.data)
man={name:json.loads((root/name).read_text()) for name in ('manifest.json','minute-manifest.json')};out={}
for symbol in SYMBOLS:
 for interval,frames in (('1m',('1m','3m')),('5m',tuple(k for k in FRAMES if k not in ('1m','3m')))):
  step=60000 if interval=='1m' else 300000;manifest=man['minute-manifest.json' if interval=='1m' else 'manifest.json']
  rows=read(root,manifest,symbol,'klines',interval=interval);origin=rows[0][0]
  for tf in frames:
   ev=signals(rows,step,tf)
   windows=[(label,stamp(start),stamp(end)) for label,start,end in SEGMENTS]
   for fam in allowed(tf):
    for label,_,_ in windows:out[f'{tf}|{fam}|{symbol}|{label}']={'eligible_closes':0,'long_signals':0,'short_signals':0,'simultaneous':0}
   # Signal is known at end of its base bar; never assign it to an earlier period.
   for i,event in ev.items():
    close=origin+(i+1)*step;label=next((name for name,start,end in windows if start<close<=end),None)
    if label is None:continue
    for fam in allowed(tf):
     long,short=event[fam][:2];bucket=out[f'{tf}|{fam}|{symbol}|{label}']
     bucket['eligible_closes']+=1;bucket['long_signals']+=int(long);bucket['short_signals']+=int(short);bucket['simultaneous']+=int(long and short)
   del ev;print('signals',symbol,tf,flush=True)
  del rows;gc.collect()
source=hashlib.sha256(json.dumps([man['manifest.json'],man['minute-manifest.json']],sort_keys=True).encode()).hexdigest()
Path(a.output).write_text(json.dumps({'input_manifest_sha256':source,'counts':out},indent=2,sort_keys=True)+'\n',encoding='utf8')

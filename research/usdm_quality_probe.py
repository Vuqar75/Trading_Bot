"""Report source coverage without inventing missing events or prices."""
import json,sys
from research.usdm_intraday_data import read
from research.intraday_spot import SYMBOLS
root=sys.argv[1];manifest=json.load(open(root+'/manifest.json'));out={}
for symbol in SYMBOLS:
 for kind in ('klines','markPriceKlines','fundingRate'):
  try:
   rows=read(root,manifest,symbol,kind)
   result={'records':len(rows),'first_ms':rows[0][0],'last_ms':rows[-1][0]}
   if kind=='fundingRate':
    slots=[x[0]//(8*3600000) for x in rows]
    result['slot_gaps']=sum(b!=a+1 for a,b in zip(slots,slots[1:]));result['interval_hours']=sorted({x[1] for x in rows})
   out[symbol+'|'+kind]=result
  except Exception as exc:out[symbol+'|'+kind]={'error':str(exc)}
print(json.dumps(out,sort_keys=True))

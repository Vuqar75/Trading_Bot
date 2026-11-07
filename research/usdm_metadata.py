"""Save a non-account public USD-M exchangeInfo subset and raw response hash."""
import datetime as dt,hashlib,json,sys,urllib.request
url='https://fapi.binance.com/fapi/v1/exchangeInfo'
with urllib.request.urlopen(url,timeout=30) as response:raw=response.read()
d=json.loads(raw);wanted={'BTCUSDT','ETHUSDT','SOLUSDT'};out=[]
for s in d['symbols']:
 if s.get('symbol') not in wanted:continue
 if s.get('contractType')!='PERPETUAL':raise ValueError('not USD-M perpetual')
 out.append({k:s.get(k) for k in ('symbol','status','contractType','baseAsset','quoteAsset','marginAsset','filters')})
if {x['symbol'] for x in out}!=wanted:raise ValueError('missing metadata')
result={'url':url,'received_utc':dt.datetime.now(dt.timezone.utc).isoformat(),'sha256_raw':hashlib.sha256(raw).hexdigest(),'symbols':out,'note':'Current public metadata; not historical filters or account-specific margin brackets.'}
open(sys.argv[1],'w',encoding='utf8').write(json.dumps(result,indent=2)+'\n')

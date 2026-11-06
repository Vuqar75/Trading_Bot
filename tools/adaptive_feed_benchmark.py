"""Public-only latency rehearsal; never creates orders or invokes an AI API."""
import argparse,json,time
from concurrent.futures import ThreadPoolExecutor
from research import adaptive_public_feed as feed
from research.adaptive_study_supervisor import collect_markets

def main():
 parser=argparse.ArgumentParser();parser.add_argument('--root',required=True);args=parser.parse_args()
 cache={};history={};_,warm_errors=collect_markets(args.root,signal_window=False,filter_cache=cache,history_cache=history,warm_history=True)
 def trial(market):
  try:
   q=feed.spot_quote() if market=='spot' else feed.futures_quote()
   host,path=(feed.SPOT,'/api/v3/klines') if market=='spot' else (feed.FUTURES,'/fapi/v1/klines')
   rows=feed.get(host,path,{'symbol':'BTCUSDT','interval':'15m','limit':3})
   return market,{'event_ms':q['event_ms'],'rows':len(rows)}
  except Exception:return market,{'error':'source_unavailable'}
 start=time.monotonic()
 with ThreadPoolExecutor(max_workers=2) as pool:result=dict(pool.map(trial,('spot','futures')))
 elapsed=time.monotonic()-start;now=int(time.time()*1000)
 ages={k:(now-v['event_ms'] if 'event_ms' in v else None) for k,v in result.items()}
 print(json.dumps({'elapsed_ms':round(elapsed*1000),'quote_age_ms':ages,'warm_errors':warm_errors,'result':result,'latency_only_within_five_seconds':elapsed<5 and all(x is not None and 0<=x<=5000 for x in ages.values()),'note':'latency rehearsal only; not a boundary execution or trade'},sort_keys=True))
if __name__=='__main__':main()

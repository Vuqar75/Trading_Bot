"""Public-only latency rehearsal; never creates orders or invokes an AI API."""
import argparse,json,time
from research.adaptive_study_supervisor import collect_markets

def main():
 parser=argparse.ArgumentParser();parser.add_argument('--root',required=True);args=parser.parse_args()
 cache={};_,warm_errors=collect_markets(args.root,signal_window=False,filter_cache=cache)
 start=time.monotonic();markets,errors=collect_markets(args.root,signal_window=True,filter_cache=cache);elapsed=time.monotonic()-start
 now=int(time.time()*1000);ages={name:(now-item['quote']['event_ms'] if item.get('quote') else None) for name,item in markets.items()}
 print(json.dumps({'elapsed_ms':round(elapsed*1000),'quote_age_ms':ages,'warm_errors':warm_errors,'errors':errors,'within_five_seconds':elapsed<5 and all(x is not None and 0<=x<=5000 for x in ages.values())},sort_keys=True))
if __name__=='__main__':main()

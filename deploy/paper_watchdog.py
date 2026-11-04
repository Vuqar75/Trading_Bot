"""One local watchdog pass; schedule with a single systemd timer."""
import sys
import time
import argparse
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from trading_bot.paper_watchdog_runtime import collect,publish

parser=argparse.ArgumentParser();parser.add_argument('--home',default='/var/lib/tradingbot');args=parser.parse_args()
home=Path(args.home);now=time.time()
result=publish(home/'paper-financial/shared-paper.db',collect(home,now=now),now=now)
print('Watchdog pass complete:',result,flush=True)

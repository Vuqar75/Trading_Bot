"""One bounded, idempotent public-data continuation for the isolated study."""
import argparse,json,sqlite3,time
from pathlib import Path
from .adaptive_candle_confirmation import PERIOD,compare
from .adaptive_market_epoch import start_epoch
from .adaptive_public_feed import closed_15m

IDENTITY='usdm-continuation-20261107-v1'

def run(root,*,previous_epoch,clock=lambda:int(time.time()*1000),sleeper=time.sleep,fetch=closed_15m):
 root=Path(root);path=root/'market-evidence.db'
 if root.is_symlink() or path.is_symlink() or not path.is_file():raise ValueError('study_state_invalid')
 with sqlite3.connect(path.resolve().as_uri()+'?mode=ro',uri=True) as db:
  if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='epoch_continuations'").fetchone():
   row=db.execute('SELECT epoch_id,previous_epoch,window_sha256 FROM epoch_continuations WHERE id=?',(IDENTITY,)).fetchone()
   if row:
    if row[1]!=previous_epoch:raise ValueError('continuation_previous_epoch_mismatch')
    return {'epoch_id':row[0],'window_sha256':row[2],'replayed':True}
 start=clock()
 while True:
  now=clock();offset=now%PERIOD
  if now-start>PERIOD:raise ValueError('continuation_wait_expired')
  if 610000<=offset<=790000:break
  sleeper(min(30,max(1,(610000-offset)/1000 if offset<610000 else (PERIOD-offset+610000)/1000)))
 first=fetch('futures',clock=clock);sleeper(12);second=fetch('futures',clock=clock)
 checked=compare(first,second)
 if checked['state']!='accepted':raise ValueError('continuation_window_unconfirmed')
 result=start_epoch(root,market='futures',clock=clock,continuation={'id':IDENTITY,'previous_epoch':previous_epoch,'first':first,'second':second})
 return {**result,'window_sha256':checked['digest'],'first_received_ms':checked['first']['received_ms'],'second_received_ms':checked['second']['received_ms'],'original_publication':'unknown'}

def main():
 parser=argparse.ArgumentParser();parser.add_argument('--root',required=True);parser.add_argument('--previous-epoch',type=int,required=True);args=parser.parse_args()
 print(json.dumps(run(args.root,previous_epoch=args.previous_epoch),sort_keys=True))

if __name__=='__main__':main()

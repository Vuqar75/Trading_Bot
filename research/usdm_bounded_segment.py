"""Time- and size-bounded independent USD-M REST snapshots, never orders."""
import argparse, json, os, shutil, time
from .usdm_book_capture import capture

MAX_BYTES=20*1024*1024

def collect(path, duration=1800, interval=5, *, clock=time.monotonic, wall=time.time, sleep=time.sleep, sample=capture):
 if not 1<=duration<=1800 or interval<5: raise ValueError('capture bounds')
 if shutil.disk_usage(os.path.dirname(path) or '.').free < 1024**3: raise ValueError('insufficient disk')
 fd=os.open(path,os.O_CREAT|os.O_EXCL|os.O_WRONLY,0o600)
 start=clock(); deadline=start+duration; samples=0; gaps=0
 with os.fdopen(fd,'w',encoding='utf8') as out:
  while clock()<deadline:
   started=clock()
   try: item={'kind':'snapshot','value':sample()}; samples+=1
   except (ValueError, OSError) as exc: item={'kind':'gap','error':type(exc).__name__,'received_ms':int(wall()*1000)};gaps+=1
   line=json.dumps(item,sort_keys=True,separators=(',',':'))+'\n'
   if out.tell()+len(line.encode())>MAX_BYTES: raise ValueError('capture disk limit')
   out.write(line);out.flush()
   delay=min(interval-(clock()-started),deadline-clock())
   if delay>0:sleep(delay)
  out.flush();os.fsync(out.fileno())
 return {'duration_s':clock()-start,'snapshots':samples,'gaps':gaps}

def main():
 p=argparse.ArgumentParser();p.add_argument('--output',required=True);a=p.parse_args();print(json.dumps(collect(a.output)))
if __name__=='__main__':main()

"""Arm one owner-authorized paper check or pilot activation, never an order.

The Spot worker consumes root-owned request files. This tool never inserts
orders, changes balances, or sends network requests.
"""
import grp,json,os,secrets,sqlite3,stat,sys,time
from contextlib import closing
from pathlib import Path

HOME=Path('/var/lib/tradingbot');ARCHIVE=HOME/'spotpilot-updates/20261107c'
FINANCIAL=HOME/'paper-financial/shared-paper.db';QUEUE=HOME/'Trading_Bot_paper_state/unified-commands.db'
CONTROL=Path('/var/lib/tradingbot-spot-pilot-control')

def main():
 if os.geteuid()!=0 or len(sys.argv)!=2 or sys.argv[1] not in ('check','activate'):raise ValueError('Root and one explicit control action required')
 action=sys.argv[1]
 if not (ARCHIVE/'release-proof.json').is_file() or not (ARCHIVE/'ready.json').is_file():raise ValueError('Verified rollout release required')
 with closing(sqlite3.connect(FINANCIAL.as_uri()+'?mode=ro',uri=True,timeout=5)) as db, closing(sqlite3.connect(QUEUE.as_uri()+'?mode=ro',uri=True,timeout=5)) as queue:
  def get(key):
   row=db.execute('SELECT value FROM runtime_meta WHERE key=?',(key,)).fetchone();return json.loads(row[0]) if row else None
  row=db.execute("SELECT value FROM ledger_meta WHERE key='spotpilot_update_freeze'").fetchone()
  if not row:raise ValueError('Original pause evidence unavailable')
  frozen=json.loads(row[0]);pause=frozen['paused']['spot'];anchor=frozen['queue_offset']-1
  if type(pause) is not bool or type(anchor) is not int or anchor < -1:raise ValueError('Original pause evidence invalid')
  pilot=get('spot_pilot');check=get('spot_test_cycle');position=get('position')
  if not isinstance(pilot,dict) or get('paused') is not True or get('blocked') or float(position['quantity'])!=0 or db.execute("SELECT 1 FROM runtime_orders WHERE phase!='SETTLED' LIMIT 1").fetchone():raise ValueError('Spot state requires review')
  if queue.execute("SELECT 1 FROM unified_jobs WHERE status='PENDING' LIMIT 1").fetchone():raise ValueError('Pending commands require review')
  if action=='check' and (pilot.get('phase')!='awaiting_check' or check is not None):raise ValueError('Check already used or pilot state changed')
  if action=='activate' and (pilot.get('phase') not in ('awaiting_check','ready_to_start') or not isinstance(check,dict) or check.get('phase')!='COMPLETE'):raise ValueError('Completed check is required')
 gid=grp.getgrnam('tradingbot').gr_gid
 try:CONTROL.mkdir(mode=0o750)
 except FileExistsError:pass
 info=CONTROL.lstat()
 if not stat.S_ISDIR(info.st_mode) or info.st_uid!=0 or info.st_mode&0o022:raise ValueError('Control directory is untrusted')
 os.chown(CONTROL,0,gid);os.chmod(CONTROL,0o750)
 request={'schema':1,'id':action+'_'+secrets.token_hex(12),'expires':time.time()+300}
 if action=='check':request.update(previous_paused=pause,anchor_update_id=anchor)
 flags=os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW
 fd=os.open(CONTROL/(action+'.json'),flags,0o640)
 try:
  os.fchown(fd,0,gid)
  with os.fdopen(fd,'w',encoding='utf-8',closefd=False) as file:
   json.dump(request,file,sort_keys=True);file.flush();os.fsync(file.fileno())
 finally:os.close(fd)
 print(json.dumps({'action':action,'request':request['id'],'expires':request['expires'],'original_pause':pause},sort_keys=True))

if __name__=='__main__':main()

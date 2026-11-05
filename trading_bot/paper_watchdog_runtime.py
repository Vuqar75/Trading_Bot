"""Collect local paper health and publish transitions to the existing outbox.

The publisher never polls Telegram, submits orders, changes pauses or balances.
It cannot detect loss of the entire VM: that requires an external monitor.
"""
import json
import copy
import os
import sqlite3
import subprocess
import time
from contextlib import closing
from pathlib import Path

from .paper_watchdog import assess,transitions,recovery_proofs,format_event

UNITS={'spot':'tradingbot-spot.service','futures':'tradingbot-futures.service',
       'cross':'tradingbot-cross.service','margin':'tradingbot-margin.service',
       'monitor':'tradingbot-monitor.service','backup_timer':'tradingbot-paper-backup.timer'}
HEALTH={'spot':'runtime-health.json','futures':'futures-health.json',
        'cross':'cross-health.json','margin':'margin-health.json'}


def _read_json(path):
    try:
        with path.open(encoding='utf-8') as f:return json.load(f)
    except (OSError,ValueError,TypeError):return None


def collect(home,*,now=None,service_status=None):
    home=Path(home);now=time.time() if now is None else now
    if service_status is None:
        def service_status(unit):
            try:return subprocess.check_output(['systemctl','show',unit,'--property=ActiveState','--value'],text=True,timeout=5).strip()
            except (OSError,subprocess.SubprocessError):return 'unknown'
    services={name:service_status(unit) for name,unit in UNITS.items()}
    financial=home/'paper-financial/shared-paper.db';queue=home/'Trading_Bot_paper_state/unified-commands.db'
    health={name:_read_json(financial.parent/file) for name,file in HEALTH.items()}
    # No historical pause cause is inferred from the current boolean.
    for market,value in health.items():
        if isinstance(value,dict) and value.get('paused') is True:
            value['pause_source']='unknown'
    financial_available=False
    try:
        with closing(sqlite3.connect(financial.as_uri()+'?mode=ro',uri=True,timeout=2)) as db:
            financial_available=True
            hold=db.execute("SELECT value FROM ledger_meta WHERE key='deployment_hold'").fetchone()
            if hold and json.loads(hold[0]) is True:
                for value in health.values():
                    if isinstance(value,dict) and value.get('paused') is True:value['pause_source']='deployment'
            for market,table in (('spot','runtime_meta'),('futures','futures_meta')):
                row=db.execute('SELECT value FROM '+table+" WHERE key='blocked'").fetchone()
                if isinstance(health.get(market),dict) and row:health[market]['blocked']=json.loads(row[0])
            conflict=db.execute("SELECT 1 FROM cross_runtime WHERE key='funding_history_conflict'").fetchone()
            if isinstance(health.get('cross'),dict) and conflict:health['cross']['blocked']='funding_history_conflict'
            for market,query in (('cross','SELECT server_ms FROM cross_quotes'),('margin','SELECT payload FROM margin_market_quotes')):
                rows=db.execute(query).fetchall();times=[]
                for row in rows:
                    t=row[0] if market=='cross' else json.loads(row[0]).get('time_ms')
                    if type(t) in (int,float):times.append(t/1000)
                if isinstance(health.get(market),dict):health[market]['data_at']=min(times) if len(times)>=2 else None
    except (OSError,sqlite3.Error,ValueError,TypeError):financial_available=False
    qstate={'pending':None}
    try:
        with closing(sqlite3.connect(queue.as_uri()+'?mode=ro',uri=True,timeout=2)) as db:
            rows=db.execute("SELECT payload FROM unified_jobs WHERE status='PENDING'").fetchall()
            times=[]
            for row in rows:
                value=json.loads(row[0]).get('received_at')
                if type(value) in (float,int):times.append(value)
            actions={}
            for market in ('spot','futures','futures_cross','margin_cross'):
                row=db.execute("SELECT update_id,command FROM unified_jobs WHERE market=? AND command IN ('/pause','/resume') AND status='DONE' ORDER BY update_id DESC LIMIT 1",(market,)).fetchone()
                actions[market]={'id':row[0],'command':row[1]} if row else {'id':0,'command':None}
                h=health.get({'futures_cross':'cross','margin_cross':'margin'}.get(market,market))
                if isinstance(h,dict) and h.get('paused') is True and h.get('pause_source')!='deployment' and row and row[1]=='/pause':h['pause_source']='manual'
            qstate={'pending':len(rows),'oldest_received':min(times) if len(times)==len(rows) and times else None,'actions':actions}
    except (OSError,sqlite3.Error,ValueError,TypeError):pass
    local=None;backup_root=home/'paper-coordinated-backups'
    try:
        for path in backup_root.glob('paper-*/manifest.json'):
            m=_read_json(path)
            if isinstance(m,dict) and {x.get('name') for x in m.get('files',[]) if isinstance(x,dict)}=={'financial.db','queue.db','observation.db'}:
                stamp=m.get('captured_at')
                if type(stamp) in (int,float) and (local is None or stamp>local):local=stamp
    except OSError:pass
    remote_file=home/'paper-external-backups/hourly-success.json';remote=_read_json(remote_file) or {}
    try:
        available=int(next(x.split()[1] for x in Path('/proc/meminfo').read_text().splitlines() if x.startswith('MemAvailable:')))*1024
    except (OSError,ValueError,StopIteration):available=None
    try:space=os.statvfs(home);disk=space.f_bavail*space.f_frsize
    except OSError:disk=None
    return {'services':services,'health':health,'queue':qstate,'financial_available':financial_available,'backups':{'local':local,'external':remote.get('captured_at'),'external_configured':remote_file.exists()},
            'resources':{'memory_available_bytes':available,'disk_free_bytes':disk}}


def publish(financial,snapshot,*,now=None,reminder_seconds=3600):
    snapshot=copy.deepcopy(snapshot)
    with closing(sqlite3.connect(financial,isolation_level=None,timeout=5)) as db:
        db.execute('BEGIN IMMEDIATE')
        try:
            now=time.time() if now is None else now
            db.execute('CREATE TABLE IF NOT EXISTS paper_watchdog_state(key TEXT PRIMARY KEY,value TEXT NOT NULL)')
            row=db.execute("SELECT value FROM paper_watchdog_state WHERE key='incidents'").fetchone()
            old=json.loads(row[0]) if row else {}
            row=db.execute("SELECT value FROM paper_watchdog_state WHERE key='pause_agreements'").fetchone()
            agreements=json.loads(row[0]) if row else None
            actions=snapshot.get('queue',{}).get('actions')
            if agreements is None and isinstance(actions,dict):
                agreements={}
                for market,job in (('cross','futures_cross'),('margin','margin_cross')):
                    h=snapshot.get('health',{}).get(market) or {};action=actions.get(job) or {}
                    agreements[market]={'active':h.get('paused') is True,'anchor':action.get('id',0)}
            if isinstance(agreements,dict) and isinstance(actions,dict):
                for market,job in (('cross','futures_cross'),('margin','margin_cross')):
                    entry=agreements.get(market) or {};h=snapshot.get('health',{}).get(market) or {};action=actions.get(job) or {}
                    if h.get('paused') is False or (action.get('command')=='/resume' and action.get('id',0)>entry.get('anchor',0)):
                        entry['active']=False
                    if entry.get('active') and h.get('paused') is True and h.get('pause_source')!='deployment':h['pause_source']='agreed'
                db.execute("INSERT OR REPLACE INTO paper_watchdog_state VALUES('pause_agreements',?)",(json.dumps(agreements,sort_keys=True),))
            problems=assess(snapshot,now)
            # Legacy alarms about agreed pauses and unconfigured external
            # backup do not become spurious recovery messages at deployment.
            for key in ('pause:cross','pause:margin'):
                if key not in problems:old.pop(key,None)
            if 'setup:external' in problems:old.pop('backup:external',None)
            row=db.execute("SELECT value FROM paper_watchdog_state WHERE key='queue_sample'").fetchone()
            prior=json.loads(row[0]) if row else None
            pending=snapshot.get('queue',{}).get('pending');streak=0
            if type(pending) is int and pending>=0:
                if (isinstance(prior,dict) and type(prior.get('pending')) is int and
                    type(prior.get('at')) in (int,float) and 0<=now-prior['at']<=900 and pending>prior['pending']):
                    streak=prior.get('growth_streak',0)+1
                if streak>=2:problems['queue:growing']={'message':'Очередь: число ожидающих команд растёт','last_success':None}
                db.execute("INSERT OR REPLACE INTO paper_watchdog_state VALUES('queue_sample',?)",(json.dumps({'pending':pending,'at':now,'growth_streak':streak}),))
            state,events=transitions(problems,old,now,reminder_seconds=reminder_seconds,healthy_keys=recovery_proofs(snapshot,now))
            for event in events:
                parts=event['key'].split(':');market=parts[-1] if parts[-1] in ('spot','futures','cross','margin') else None
                if market:
                    h=snapshot.get('health',{}).get(market) or {};event['observed_at']=now;event['health_at']=h.get('time')
                    quote=h.get('quote') or {}
                    if market in ('cross','margin'):event['data_at']=h.get('data_at')
                    elif market=='futures':
                        server=quote.get('server_ms');event['data_at']=server/1000 if isinstance(server,(int,float)) else None
                        event['received_at']=quote.get('time')
                    else:event['data_at']=None;event['received_at']=quote.get('time')
                    value=h.get('protection_checked_at');event['protection_at']=value/1000 if market=='margin' and isinstance(value,(float,int)) else value
                db.execute('INSERT INTO futures_outbox(message) VALUES(?)',(format_event(event),))
            db.execute("INSERT OR REPLACE INTO paper_watchdog_state VALUES('incidents',?)",(json.dumps(state,sort_keys=True),))
            db.commit();return {'problems':len(problems),'new_messages':len(events)}
        except BaseException:
            db.rollback();raise

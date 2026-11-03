"""Collect local paper health and publish transitions to the existing outbox.

The publisher never polls Telegram, submits orders, changes pauses or balances.
It cannot detect loss of the entire VM: that requires an external monitor.
"""
import json
import os
import sqlite3
import subprocess
import time
from contextlib import closing
from datetime import datetime,timezone
from pathlib import Path

from .paper_watchdog import assess,transitions

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
    # Current schema doesn't persist pause provenance: do not call it manual.
    for value in health.values():
        if isinstance(value,dict) and value.get('paused') is True:value['pause_source']='unknown'
    qstate={'pending':None}
    try:
        with closing(sqlite3.connect(queue.as_uri()+'?mode=ro',uri=True,timeout=2)) as db:
            rows=db.execute("SELECT payload FROM unified_jobs WHERE status='PENDING'").fetchall()
            times=[]
            for row in rows:
                value=json.loads(row[0]).get('received_at')
                if type(value) in (float,int):times.append(value)
            qstate={'pending':len(rows),'oldest_received':min(times) if len(times)==len(rows) and times else None}
    except (OSError,sqlite3.Error,ValueError,TypeError):pass
    local=None;backup_root=home/'paper-coordinated-backups'
    try:
        for path in backup_root.glob('paper-*/manifest.json'):
            m=_read_json(path)
            if isinstance(m,dict) and {x.get('name') for x in m.get('files',[]) if isinstance(x,dict)}=={'financial.db','queue.db','observation.db'}:
                stamp=m.get('captured_at')
                if type(stamp) in (int,float) and (local is None or stamp>local):local=stamp
    except OSError:pass
    remote=_read_json(home/'paper-external-backups/hourly-success.json') or {}
    try:
        available=int(next(x.split()[1] for x in Path('/proc/meminfo').read_text().splitlines() if x.startswith('MemAvailable:')))*1024
    except (OSError,ValueError,StopIteration):available=None
    try:space=os.statvfs(home);disk=space.f_bavail*space.f_frsize
    except OSError:disk=None
    return {'services':services,'health':health,'queue':qstate,'backups':{'local':local,'external':remote.get('captured_at')},
            'resources':{'memory_available_bytes':available,'disk_free_bytes':disk}}


def publish(financial,snapshot,*,now=None,reminder_seconds=3600):
    now=time.time() if now is None else now;problems=assess(snapshot,now)
    with closing(sqlite3.connect(financial,isolation_level=None,timeout=5)) as db:
        db.execute('BEGIN IMMEDIATE')
        try:
            db.execute('CREATE TABLE IF NOT EXISTS paper_watchdog_state(key TEXT PRIMARY KEY,value TEXT NOT NULL)')
            row=db.execute("SELECT value FROM paper_watchdog_state WHERE key='incidents'").fetchone()
            old=json.loads(row[0]) if row else {}
            row=db.execute("SELECT value FROM paper_watchdog_state WHERE key='queue_sample'").fetchone()
            prior=json.loads(row[0]) if row else None
            pending=snapshot.get('queue',{}).get('pending');streak=0
            if type(pending) is int and pending>=0:
                if (isinstance(prior,dict) and type(prior.get('pending')) is int and
                    type(prior.get('at')) in (int,float) and 0<=now-prior['at']<=900 and pending>prior['pending']):
                    streak=prior.get('growth_streak',0)+1
                if streak>=2:problems['queue:growing']={'message':'Очередь: число ожидающих команд растёт','last_success':None}
                db.execute("INSERT OR REPLACE INTO paper_watchdog_state VALUES('queue_sample',?)",(json.dumps({'pending':pending,'at':now,'growth_streak':streak}),))
            state,events=transitions(problems,old,now,reminder_seconds=reminder_seconds)
            for event in events:
                last=event.get('last_success')
                try:last=datetime.fromtimestamp(last,timezone.utc).isoformat() if isinstance(last,(int,float)) else 'нет данных'
                except (ValueError,OverflowError,OSError):last='нет данных'
                label='ВОССТАНОВЛЕНО' if event['kind']=='recovered' else 'ВНИМАНИЕ'
                message=f"[Контроль paper] {label}: {event['message']}. Последняя успешная проверка: {last} UTC."
                db.execute('INSERT INTO futures_outbox(message) VALUES(?)',(message,))
            db.execute("INSERT OR REPLACE INTO paper_watchdog_state VALUES('incidents',?)",(json.dumps(state,sort_keys=True),))
            db.commit();return {'problems':len(problems),'new_messages':len(events)}
        except BaseException:
            db.rollback();raise

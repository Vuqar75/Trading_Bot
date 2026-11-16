"""Bounded phase/queue evidence, separate from the financial ledger and public UI.
Durations are monotonic; phase CPU belongs to the calling thread, not wall wait.
The optional background writer owns only this diagnostic JSON, never SQLite.
"""
from contextlib import contextmanager, nullcontext
from collections import deque
import json,os,queue,sqlite3,threading,time
from pathlib import Path

class TimedCursor(sqlite3.Cursor):
    def span(self,name):
        probe=getattr(self.connection,'probe',None)
        return probe.phase(name) if probe else nullcontext()
    def execute(self,sql,parameters=()):
        verb=sql.lstrip().split(None,1)[0].upper() if sql.strip() else ''
        name='sql_'+(verb.lower() if verb in {'SELECT','INSERT','UPDATE','DELETE','PRAGMA','BEGIN','RELEASE','SAVEPOINT','ROLLBACK'} else 'other')
        with self.span(name):return super().execute(sql,parameters)
    def executemany(self,sql,seq):
        with self.span('sql_many'):return super().executemany(sql,seq)
    def fetchone(self):
        with self.span('sql_fetch'):return super().fetchone()
    def fetchall(self):
        with self.span('sql_fetch'):return super().fetchall()
    def fetchmany(self,*args):
        with self.span('sql_fetch'):return super().fetchmany(*args)
    def __iter__(self):return self
    def __next__(self):
        with self.span('sql_step'):return super().__next__()

class TimedConnection(sqlite3.Connection):
    probe=None
    def cursor(self,factory=None):return super().cursor(factory or (TimedCursor if self.probe else sqlite3.Cursor))
    def execute(self,sql,parameters=()):
        if self.probe:return self.cursor().execute(sql,parameters)
        return super().execute(sql,parameters)
    def executemany(self,sql,seq):
        if self.probe:return self.cursor().executemany(sql,seq)
        return super().executemany(sql,seq)
    def _commit(self):return super().commit()
    def commit(self):
        with self.probe.phase('sql_commit') if self.probe else nullcontext():return self._commit()
    def rollback(self):
        with self.probe.phase('sql_rollback') if self.probe else nullcontext():return super().rollback()

class Probe:
    VERSION='bounded-phase-cpu-sql-v2'
    SCHEDULED_PHASES={'main_poller','main_snapshot','main_publish','book_verify','sql_commit'}
    def __init__(self,path,*,mono=time.monotonic_ns,cpu=time.thread_time_ns,resource=None,limit=2000000,write=None):
        self.path=Path(path);self.mono=mono;self.cpu=cpu;self.resource=resource or self.linux_resources
        self.limit=limit;self.lock=threading.Lock();self.phases={};self.ring=deque(maxlen=64);self.episodes=deque(maxlen=8)
        self.slow_phases=deque(maxlen=24);self.main_ident=threading.get_ident();self.seen_gap=None;self.resource_cache={};self.resource_at=0;self.started=None;self.dropped=0;self.evicted=0;self.last_write={};self.write=write or self.atomic
        self.next_write=0;self.work=queue.Queue(maxsize=1);self.stop=threading.Event();self.writer=threading.Thread(target=self._writer,daemon=True,name='phase-evidence');self.writer.start()
    @contextmanager
    def phase(self,name):
        wall=self.mono();cpu=self.cpu()
        sampled=name in self.SCHEDULED_PHASES and threading.get_ident()==self.main_ident
        before=self.scheduler_sample() if sampled else None
        try:yield
        finally:
            duration=max(0,self.mono()-wall);used=max(0,self.cpu()-cpu)
            if sampled and duration>=100000000:
                after=self.scheduler_sample()
                self.slow_phases.append({'name':name,'started_ns':wall,'ended_ns':wall+duration,
                    'wall_ns':duration,'thread_cpu_ns':used,'before':before,'after':after})
            with self.lock:
                # Names are fixed by implementation; never store SQL, params,
                # response bodies, paths to credentials, or arbitrary errors.
                if name not in self.phases and len(self.phases)>=48:name='other'
                value=self.phases.setdefault(name,{'calls':0,'wall_ns':0,'thread_cpu_ns':0,'max_wall_ns':0})
                value['calls']+=1;value['wall_ns']+=duration;value['thread_cpu_ns']+=used;value['max_wall_ns']=max(value['max_wall_ns'],duration)
    def scheduler_sample(self):
        """Main-only slow-span evidence. Counters are observations, not attribution.

        No clock is substituted for source E/T. This never opens SQLite or
        captures arbitrary task names, request text, credentials or arguments.
        """
        out={'sample_ns':self.mono(),'thread_id':threading.get_native_id()}
        try:
            import resource
            r=resource.getrusage(resource.RUSAGE_THREAD)
            out['thread_switches']=[r.ru_nvcsw,r.ru_nivcsw,r.ru_inblock,r.ru_oublock]
        except (ImportError,AttributeError,OSError):out['thread_switches']=None
        for key,path in [('schedstat','/proc/thread-self/schedstat'),
                         ('cgroup_cpu','/sys/fs/cgroup/system.slice/ai-bracket-paper.service/cpu.stat')]:
            try:
                raw=Path(path).read_text()
                out[key]=[int(v) for v in raw.split()] if key=='schedstat' else {k:int(v) for k,v in (line.split() for line in raw.splitlines())}
            except (OSError,ValueError):out[key]=None
        return out
    def linux_resources(self):
        out={'sample_ns':self.mono(),'wall_ms':int(time.time()*1000),'process_cpu_ns':time.process_time_ns()}
        try:
            import resource
            r=resource.getrusage(resource.RUSAGE_THREAD)
            out['main_rusage']={k:getattr(r,k) for k in ['ru_inblock','ru_oublock','ru_nvcsw','ru_nivcsw','ru_minflt','ru_majflt']}
        except (ImportError,AttributeError,OSError):out['main_rusage']=None
        for key,path in [('cgroup_cpu','/sys/fs/cgroup/system.slice/ai-bracket-paper.service/cpu.stat'),('process_io','/proc/self/io')]:
            try:out[key]={k.rstrip(':'):int(v) for k,v in (s.split() for s in Path(path).read_text().splitlines())}
            except (OSError,ValueError):out[key]=None
        try:out['main_schedstat']=[int(n) for n in Path('/proc/self/task/'+str(os.getpid())+'/schedstat').read_text().split()]
        except (OSError,ValueError):out['main_schedstat']=None
        for name in ('cpu','io'):
            try:out['psi_'+name]=Path('/proc/pressure/'+name).read_text().strip()
            except OSError:out['psi_'+name]=None
        try:out['host_cpu_ticks']=[int(n) for n in Path('/proc/stat').read_text().splitlines()[0].split()[1:]]
        except (OSError,ValueError):out['host_cpu_ticks']=None
        return out
    def resources(self,force=False):
        if force or self.mono()-self.resource_at>=1000000000 or not self.resource_cache:
            try:self.resource_cache=self.resource()
            except (OSError,ValueError):self.resource_cache={'error':'resource_unavailable','sample_ns':self.mono()}
            self.resource_at=self.mono()
        return self.resource_cache
    def begin(self):
        with self.lock:self.phases={}
        self.slow_phases.clear()
        self.started=(self.mono(),self.cpu(),self.resources())
    def finish(self,feed,gap=None,reports=None):
        if self.started is None:return
        capture_start=self.mono();capture_cpu=self.cpu()
        start,cpu,resources=self.started;new_gap=gap and gap.get('at_ms')!=self.seen_gap
        row={'started_ns':start,'ended_ns':self.mono(),'main_thread_cpu_ns':max(0,self.cpu()-cpu),'resource_before':resources,'resource_after':self.resources(force=bool(new_gap)),'writer_previous':self.last_write,'local_backup':getattr(self,'backup_status',None),'transport':{}}
        row['slow_main_phases']=list(self.slow_phases)
        with self.lock:row['phases']={k:dict(v) for k,v in self.phases.items()}
        if hasattr(feed,'lock'):
            with feed.lock:
                for symbol,st in feed.states.items():
                    first=st['queue'][0] if st['queue'] else None
                    row['transport'][symbol]={'queued':len(st['queue']),'received':st['received'],'last_id':st['last'],'fault':st['fault'],'reason':st['reason'],
                        'oldest_queue_wait_ns':max(0,self.mono()-first['_received_ns']) if first else None}
        for symbol,report in (reports or {}).items():
            if not isinstance(report,dict):continue
            row['transport'].setdefault(symbol,{})['dequeued_batch']={k:report.get(k) for k in ('requested_after_id','first_id','last_id','rows','first_event_ms','last_event_ms','oldest_age_ms','newest_age_ms','batch_processing_ms','batch_lock_wait_ms','stale_rows')}
            row['transport'][symbol]['dequeued_batch']['oldest_dequeued_wait_ms']=report.get('stream',{}).get('oldest_dequeued_wait_ms')
        row['wall_ns']=row['ended_ns']-start
        for episode in self.episodes:
            if episode['post_remaining']:
                episode['cycles'].append(row);episode['post_remaining']-=1
        if new_gap:
            if len(self.episodes)==self.episodes.maxlen:self.evicted+=1
            self.seen_gap=gap['at_ms'];self.episodes.append({'at_ms':gap['at_ms'],'source':gap.get('source'),'http_recent':list(getattr(feed,'http_timings',[]))[-8:],'cycles':list(self.ring)+[row],'post_remaining':8})
        row['capture_wall_ns']=self.mono()-capture_start;row['capture_thread_cpu_ns']=self.cpu()-capture_cpu
        self.ring.append(row)
        now=self.mono()
        if new_gap or now>=self.next_write:
            payload={'version':self.VERSION,'captured_ms':int(time.time()*1000),'recent':list(self.ring),'episodes':[{**e,'cycles':list(e['cycles'])} for e in self.episodes],
                     'evicted_episodes':self.evicted,'coalesced_publications':self.dropped,'writer_previous':self.last_write}
            try:self.work.put_nowait(payload)
            except queue.Full:
                try:self.work.get_nowait();self.dropped+=1
                except queue.Empty:pass
                try:self.work.put_nowait(payload)
                except queue.Full:self.dropped+=1
            self.next_write=now+10000000000
    def atomic(self,raw):
        temp=self.path.with_suffix('.tmp');fd=os.open(temp,os.O_WRONLY|os.O_CREAT|os.O_TRUNC|os.O_NOFOLLOW,0o600)
        with os.fdopen(fd,'wb') as f:f.write(raw);f.flush();os.fsync(f.fileno())
        os.replace(temp,self.path);fd=os.open(self.path.parent,os.O_RDONLY|os.O_DIRECTORY)
        try:os.fsync(fd)
        finally:os.close(fd)
    def _writer(self):
        while not self.stop.is_set() or not self.work.empty():
            try:value=self.work.get(timeout=.2)
            except queue.Empty:continue
            started=self.mono();cpu=self.cpu();serialized=None;serialize_cpu=None;write_started=None
            try:
                raw=json.dumps(value,separators=(',',':')).encode()
                while len(raw)>self.limit and (value['episodes'] or len(value['recent'])>1):
                    if value['episodes']:value['episodes'].pop(0);value['evicted_episodes']+=1
                    else:value['recent']=value['recent'][len(value['recent'])//2:]
                    raw=json.dumps(value,separators=(',',':')).encode()
                if len(raw)>self.limit:raise ValueError('diagnostic_limit')
                serialized=self.mono();serialize_cpu=self.cpu()-cpu;write_started=serialized
                self.write(raw);status='ok'
            except (OSError,ValueError):status='write_failed'
            self.last_write={'status':status,'wall_ns':self.mono()-started,'thread_cpu_ns':self.cpu()-cpu,'at_ms':int(time.time()*1000),
                'serialization_wall_ns':serialized-started if serialized is not None else None,
                'serialization_thread_cpu_ns':serialize_cpu,
                'disk_wall_ns':self.mono()-write_started if write_started is not None else None}
    def close(self):self.stop.set();self.writer.join(timeout=1)

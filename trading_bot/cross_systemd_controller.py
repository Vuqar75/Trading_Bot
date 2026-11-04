"""Systemd adapter for the explicitly authorized paper-only cross cutover.

It never invokes a shell, sends an order, or reads Telegram configuration.
Health checks are conservative and include process locks and fresh output from
the currently started services. It is not a distributed fencing mechanism.
"""
import fcntl
import json
import os
import subprocess
import time
import re
from pathlib import Path


class SystemdPaperController:
    SPOT='tradingbot-spot.service';FUTURES='tradingbot-futures.service';CROSS='tradingbot-cross.service'
    MONITOR='tradingbot-monitor.service';TIMER='tradingbot-paper-backup.timer';BACKUP='tradingbot-paper-backup.service'

    def __init__(self,financial,unit_dir,shared,*,timeout=90):
        self.financial=Path(financial).resolve(strict=True);self.unit_dir=Path(unit_dir).resolve(strict=True)
        self.shared=shared;self.timeout=timeout

    def _call(self,*args):
        return subprocess.run(['systemctl',*args],stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True,timeout=self.timeout)

    def _property(self,unit,key):
        result=self._call('show',unit,'--property='+key,'--value')
        return result.stdout.strip() if result.returncode==0 else None

    def _loaded(self,unit):return self._property(unit,'LoadState')=='loaded'

    def interlocked_services(self):return (self.SPOT,self.FUTURES,self.CROSS)

    def reload_units(self):return self._call('daemon-reload').returncode==0

    def interlock_loaded(self,marker):
        for unit in self.interlocked_services():
            if not self._loaded(unit):
                if unit==self.CROSS and not (self.unit_dir/unit).exists():continue
                return False
            paths=self._property(unit,'DropInPaths')
            expected=str(self.unit_dir/(unit+'.d')/'90-paper-cross-interlock.conf')
            if paths is None or paths.split()!=[expected]:return False
            if self._property(unit,'NeedDaemonReload')!='no':return False
        return True

    def stop_all(self):
        units=[self.SPOT,self.FUTURES,self.MONITOR,self.TIMER,self.BACKUP]
        if self._loaded(self.CROSS):units.append(self.CROSS)
        if self._call('stop',*units).returncode:raise ValueError('Could not stop all paper services')

    def stopped(self):
        for unit in (self.SPOT,self.FUTURES,self.CROSS,self.MONITOR,self.TIMER,self.BACKUP):
            if not self._loaded(unit):
                if unit==self.CROSS and self._property(unit,'ActiveState') in (None,'inactive'):continue
                return False
            if self._property(unit,'ActiveState')!='inactive':return False
        return True

    def enable_cross(self):
        if self._call('enable',self.CROSS).returncode:raise ValueError('Could not enable cross paper service')

    def disable_cross(self):
        # A previous rollback may have removed the unit before a crash.
        if not (self.unit_dir/self.CROSS).exists() and self._call('is-enabled',self.CROSS).returncode!=0:return
        if self._call('disable',self.CROSS).returncode:raise ValueError('Could not disable cross paper service')

    def start_all(self):
        for unit in (self.FUTURES,self.SPOT,self.CROSS,self.MONITOR,self.TIMER):
            if self._call('start',unit).returncode:raise ValueError('Could not start a paper service')

    def start_old_paused(self):
        for unit in (self.FUTURES,self.SPOT,self.MONITOR,self.TIMER):
            if self._call('start',unit).returncode:raise ValueError('Could not start old paper service')

    def loaded_units_match(self,unit_dir,units):
        return all(self._property(n,'FragmentPath')==str(Path(unit_dir)/n) and self._property(n,'NeedDaemonReload')=='no' for n in units)

    def old_units_loaded(self,unit_dir,units,new_units):
        return all(self._property(n,'FragmentPath')==str(Path(unit_dir)/n) and self._property(n,'NeedDaemonReload')=='no' for n in units if n not in new_units) and all(not (Path(unit_dir)/n).exists() for n in new_units)

    def boot_id(self):
        try:return Path('/proc/sys/kernel/random/boot_id').read_text().strip()
        except OSError:return None

    def backup_root(self):return self.financial.parent.parent/'paper-coordinated-backups'

    def live_backup(self):
        """Run the configured same-disk backup and sample available RAM."""
        root=self.backup_root()
        def sets():
            if not root.exists():return set()
            if root.is_symlink() or not root.is_dir():raise ValueError('Invalid backup directory')
            return {p.name for p in root.iterdir() if not p.is_symlink() and p.is_dir() and re.fullmatch(r'paper-\d{8}T\d{12}Z',p.name)}
        before=sets();deadline=time.monotonic()+self.timeout;samples=[]
        process=subprocess.Popen(['systemctl','start',self.BACKUP],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
        try:
            while process.poll() is None:
                if time.monotonic()>=deadline:raise TimeoutError('Live backup did not complete')
                available=int(next(line.split()[1] for line in Path('/proc/meminfo').read_text().splitlines() if line.startswith('MemAvailable:')))
                samples.append(available);time.sleep(0.05)
            if process.returncode or not samples:raise ValueError('Live backup or memory sampling failed')
            created=sorted(sets()-before)
            if not created:raise ValueError('No new coordinated backup found')
            return {'path':str(root/created[-1]),'min_available_kib':min(samples),'samples':len(samples)}
        finally:
            if process.poll() is None:
                process.terminate()
                try:process.wait(timeout=2)
                except subprocess.TimeoutExpired:process.kill();process.wait()

    def _counts(self):
        names={'spot':b'trading_bot.app','futures':b'trading_bot.futures_app',
               'cross':b'trading_bot.cross_app','monitor':b'trading_bot.paper_monitor'}
        counts={name:0 for name in names}
        for proc in Path('/proc').iterdir():
            if not proc.name.isdigit():continue
            try:args=(proc/'cmdline').read_bytes().split(b'\0')
            except OSError:continue
            for name,argument in names.items():counts[name]+=int(argument in args)
        return counts

    def _relay_locked(self):
        path=Path(str(self.financial)+'.delivery.lock')
        if path.is_symlink() or not path.is_file():return False
        fd=os.open(path,os.O_RDWR|getattr(os,'O_NOFOLLOW',0))
        try:
            try:fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
            except BlockingIOError:return True
            fcntl.flock(fd,fcntl.LOCK_UN);return False
        finally:os.close(fd)

    def _service_start_wall(self,unit):
        value=self._property(unit,'ExecMainStartTimestampMonotonic')
        try:
            micros=int(value)
            if micros<=0:return None
            return time.time()-(time.monotonic()-micros/1_000_000)
        except (TypeError,ValueError,OSError):return None

    def _healthy(self,*,cross,paused):
        names=[self.SPOT,self.FUTURES,self.MONITOR,self.TIMER]+([self.CROSS] if cross else [])
        if any(self._property(n,'ActiveState')!='active' for n in names):return False
        expected={'spot':1,'futures':1,'cross':1 if cross else 0,'monitor':1}
        if self._counts()!=expected or not self._relay_locked():return False
        try:
            available=int(next(line.split()[1] for line in Path('/proc/meminfo').read_text().splitlines() if line.startswith('MemAvailable:')))
            if available<102400:return False
            now=time.time();root=self.financial.parent
            for unit,name,mode in ((self.SPOT,'runtime-health.json','paper'),(self.FUTURES,'futures-health.json','futures-paper')):
                data=json.loads((root/name).read_text());started=self._service_start_wall(unit)
                if started is None or data.get('mode')!=mode or not isinstance(data.get('time'),(float,int)) or data['time']<started-1 or not 0<=now-data['time']<=120:return False
                if data.get('error') or data.get('blocked') or data.get('telegram_error') or not data.get('quote') or not 0<=now-data['quote'].get('time',0)<=120:return False
                market='spot' if unit==self.SPOT else 'futures'
                if type(data.get('paused')) is not bool or data['paused']!=self.shared._get(market,'paused'):return False
                if paused and not data['paused']:return False
                if market=='spot' and data.get('pending')!=0:return False
            if cross:
                data=json.loads((root/'cross-health.json').read_text());started=self._service_start_wall(self.CROSS)
                if started is None or data.get('mode')!='futures-cross-paper' or not isinstance(data.get('time'),(float,int)) or data['time']<started-1 or not 0<=now-data['time']<=120:return False
                row=self.shared.db.execute("SELECT paused FROM wallet_state WHERE wallet='futures_cross'").fetchone()
                if not row or data.get('error') or (data.get('funding_pages')!='loaded_available_pages' or data.get('funding_paper_model')!='matches_paper_assumption' or data.get('funding_publication_finality')!='not_guaranteed_by_source' or data.get('quotes_fresh') is not True or data.get('protection_fresh') is not True) or data.get('paused')!=bool(row[0]) or (paused and not row[0]):return False
            if any(self.shared._get(m,'blocked') or self.shared._get(m,'error') for m in ('spot','futures')):return False
            return self.shared.ledger.reconcile()['balanced']
        except (OSError,ValueError,TypeError,KeyError,StopIteration):return False

    def _wait(self,*,cross,paused):
        deadline=time.monotonic()+self.timeout
        while time.monotonic()<deadline:
            if self._healthy(cross=cross,paused=paused):return True
            time.sleep(2)
        return False

    def wait_healthy_paused(self):return self._wait(cross=True,paused=True)
    def wait_healthy_old_paused(self):return self._wait(cross=False,paused=True)
    def wait_healthy_after_release(self):return self._wait(cross=True,paused=False)

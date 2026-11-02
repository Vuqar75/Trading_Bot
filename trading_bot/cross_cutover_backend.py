"""Staged paper cross cutover actions; requires a verified host controller.

No service manager or network is called from a SQLite transaction. The caller
must hold the cutover journal's host lock throughout each invocation and supply
a controller whose probes account for systemd and unexpected local writers.
This module does not itself authorize or launch a production deployment.
"""
import json
import os
from pathlib import Path
from .cross_backup_verifier import verify_restorable_backup
from .cross_migration_verifier import verify_same_price_migration
from .cross_rollout import freeze_entries,release_entries_after_cutover
from .cross_rollback_guard import check_cross_code_rollback
from .cross_service_interlock import arm_interlock,release_interlock
from .cross_unit_switch import hold_writer_locks,switch_unit_files,restore_unit_files,_write
from .cross_verified_allocation import allocate_verified_cross
from .paper_coordinated_backup import coordinated_backup


class CrossCutoverBackend:
    def __init__(self,shared,queue,observer,unit_dir,candidate_dir,archive,marker,units,new_units,operation,controller,*,fault=lambda p:None):
        self.shared=shared;self.queue=queue;self.financial=Path(shared.db.execute('PRAGMA database_list').fetchone()[2]).resolve(strict=True)
        self.queue_path=Path(queue.db.execute('PRAGMA database_list').fetchone()[2]).resolve(strict=True)
        self.observer=Path(observer).resolve(strict=True);self.unit_dir=Path(unit_dir).resolve(strict=True)
        self.candidate=Path(candidate_dir).resolve(strict=True);self.archive=Path(archive).resolve(strict=True)
        self.marker=Path(marker);self.units=tuple(units);self.new_units=tuple(new_units)
        self.operation=operation;self.controller=controller;self.fault=fault
        self.before=self.archive/'before';self.after=self.archive/'after';self.bootfile=self.archive/'boot-before-reboot'
        self.switch_archive=self.archive/'unit-archive';self.switch_archive.mkdir(mode=0o700,exist_ok=True)
        self.allocation='cross:initial:'+operation.removeprefix('cross:freeze:')
        self.release_id='cross:release:'+operation.removeprefix('cross:freeze:')

    def _check_marker(self):
        return self.marker.is_file() and not self.marker.is_symlink() and self.marker.read_text()==self.operation

    def _paused_hold(self):
        try:
            return self.shared.deployment_hold() and all(self.shared._get(m,'paused') is True for m in ('spot','futures'))
        except (ValueError,TypeError):return False

    def _safe_stopped(self):
        return self._check_marker() and self._paused_hold() and self.controller.stopped() is True

    def _units_match(self):
        try:
            return all(not (self.unit_dir/n).is_symlink() and (self.unit_dir/n).is_file() and (self.unit_dir/n).read_bytes()==(self.candidate/n).read_bytes() for n in self.units)
        except OSError:return False

    def _reload(self):return self.controller.reload_units() is True

    def _arm(self):
        arm_interlock(self.unit_dir,self.marker,self.controller.interlocked_services(),self.operation,reload_units=self._reload)
        if self.controller.interlock_loaded(self.marker) is not True:raise ValueError('Systemd interlock not proven loaded')

    def _freeze(self):freeze_entries(self.shared,self.queue,self.operation)

    def _stop(self):
        if not self._check_marker() or not self._paused_hold():raise ValueError('Cutover is not safely frozen')
        self.controller.stop_all()
        if self.controller.stopped() is not True:raise ValueError('Services or writers did not stop')
        with hold_writer_locks(self.financial):
            if not self._safe_stopped():raise ValueError('Stopped state changed')

    def _backup(self,target):
        if not self._safe_stopped():raise ValueError('Backup requires stopped writers and hold')
        with hold_writer_locks(self.financial):
            if not target.exists():coordinated_backup(self.financial,self.queue_path,self.observer,target,timeout=15)
            result=verify_restorable_backup(target,freeze_id=self.operation,scratch_parent=self.archive)
            if result['pending_commands']:raise ValueError('Backup contains unprocessed commands')
        return result

    def _switch(self):
        if not self._safe_stopped():raise ValueError('Unit switch requires stopped writers and hold')
        with hold_writer_locks(self.financial):
            switch_unit_files(self.unit_dir,self.candidate,self.switch_archive,self.units,
                              new_units=self.new_units,assert_safe=self._safe_stopped,fault=self.fault)
            if not self._reload() or self.controller.interlock_loaded(self.marker) is not True:
                raise ValueError('Switched units or interlock were not loaded')

    def _start_paused(self):
        if not self._units_match() or not self._paused_hold():raise ValueError('Switched units and hold required')
        if self.marker.exists():
            with hold_writer_locks(self.financial):
                if (not self._safe_stopped() or not self._reload() or self.controller.interlock_loaded(self.marker) is not True
                    or self.controller.loaded_units_match(self.unit_dir,self.units) is not True):
                    raise ValueError('Cannot safely release service interlock')
                self.controller.enable_cross()
                release_interlock(self.unit_dir,self.marker,self.controller.interlocked_services(),self.operation,
                                  verify_safe=lambda:self._safe_stopped() and self._units_match())
        elif self.controller.loaded_units_match(self.unit_dir,self.units) is not True:
            raise ValueError('Loaded units are uncertain')
        self.controller.start_all()
        if self.controller.wait_healthy_paused() is not True or not self._paused_hold():
            raise ValueError('Paused runtime health not proven')

    def _allocate(self):
        self._arm();self._stop()
        with hold_writer_locks(self.financial):
            allocate_verified_cross(self.shared,self.queue,self.allocation,assert_quiescent=self._safe_stopped,fault=self.fault)

    def _backup_after(self):
        self._backup(self.after)
        verify_same_price_migration(self.before/'financial.db',self.after/'financial.db',scratch_parent=self.archive)

    def _live_backup(self,label,*,require_paused):
        info=self.controller.live_backup()
        if not isinstance(info,dict) or type(info.get('min_available_kib')) is not int or info['min_available_kib']<102400 or type(info.get('samples')) is not int or info['samples']<1:
            raise ValueError('Live backup memory headroom not proven')
        path=Path(info.get('path',''))
        root=Path(self.controller.backup_root()).resolve(strict=True)
        if path.is_symlink() or not path.is_dir() or path.resolve(strict=True).parent!=root:
            raise ValueError('Unexpected live backup path')
        verified=verify_restorable_backup(path,freeze_id=self.operation,scratch_parent=self.archive,require_paused=require_paused)
        if verified['pending_commands']:raise ValueError('Live backup contains pending commands')
        _write(self.archive/('live-backup-'+label+'.json'),json.dumps(info,sort_keys=True).encode(),0o600)

    def _await_reboot(self):
        self._start_paused()
        self._live_backup('before-reboot',require_paused=True)
        boot=self.controller.boot_id()
        if not isinstance(boot,str) or not boot:raise ValueError('Boot identity unavailable')
        if self.bootfile.exists():
            if self.bootfile.is_symlink() or self.bootfile.read_text()!=boot:raise ValueError('Boot identity changed during preparation')
        else:_write(self.bootfile,boot.encode(),0o600)

    def reboot_verified(self):
        current=self.controller.boot_id()
        return (isinstance(current,str) and bool(current) and self.bootfile.is_file() and not self.bootfile.is_symlink() and current!=self.bootfile.read_text()
                and self.controller.wait_healthy_paused() is True and self._paused_hold())

    def _release(self):
        if not self.reboot_verified():raise ValueError('Reboot and paused runtime not proven')
        self._live_backup('after-reboot',require_paused=True)
        release_entries_after_cutover(self.shared,self.queue,self.release_id)

    def _pause_on_error(self):
        # Fail closed even when reconciliation itself has failed. Every normal
        # resume checks this hold in the same financial database transaction.
        with self.shared.transaction():
            for market in ('spot','futures'):self.shared._set(market,'paused',True)
            self.shared.db.execute("UPDATE wallet_state SET paused=1 WHERE wallet='futures_cross'")
            self.shared.db.execute("INSERT OR REPLACE INTO ledger_meta VALUES('deployment_hold','true')")

    def _complete(self):
        try:
            if self.shared.deployment_hold() or self.controller.wait_healthy_after_release() is not True:
                raise ValueError('Runtime health after release not proven')
            self._live_backup('after-release',require_paused=False)
        except Exception:
            self._pause_on_error()
            raise
        _write(self.archive/'complete',b'verified',0o600)

    def rollback_old_code_paused(self):
        """Restore only unit files if the stopped-state guard explicitly allows it.

        No financial, queue, or observation snapshot is ever restored here.
        After any cross financial event the guard refuses this code rollback.
        """
        if not self._paused_hold():raise ValueError('Rollback requires paused entries and deployment hold')
        self._arm();self._stop()
        with hold_writer_locks(self.financial):
            def safe():
                result=check_cross_code_rollback(self.financial,self.queue_path,self.before/'queue.db')
                return result['safe_for_old_code'] is True
            if not safe():raise ValueError('Rollback safety not proven')
            self.controller.disable_cross()
            restore_unit_files(self.unit_dir,self.switch_archive,self.units,assert_safe=self._safe_stopped,verify_rollback=safe)
            if not self._reload() or self.controller.old_units_loaded(self.unit_dir,self.units,self.new_units) is not True:
                raise ValueError('Restored units not proven loaded')
            release_interlock(self.unit_dir,self.marker,self.controller.interlocked_services(),self.operation,
                              verify_safe=lambda:self._safe_stopped() and safe())
        self.controller.start_old_paused()
        if self.controller.wait_healthy_old_paused() is not True or not self._paused_hold():
            raise ValueError('Old paused runtime health not proven')

    def actions(self):
        return {'interlock_armed':self._arm,'frozen':self._freeze,'stopped':self._stop,
                'backup_verified':lambda:self._backup(self.before),'units_switched':self._switch,
                'started_paused':self._start_paused,'allocated':self._allocate,'backup_after':self._backup_after,
                'awaiting_reboot':self._await_reboot,'released':self._release,'complete':self._complete}

    def probes(self):
        def backup_probe(path):
            if not path.exists():return 'NOT_STARTED'
            try:
                result=verify_restorable_backup(path,freeze_id=self.operation,scratch_parent=self.archive)
                if result['pending_commands']:return 'UNKNOWN'
                if path==self.after:verify_same_price_migration(self.before/'financial.db',path/'financial.db',scratch_parent=self.archive)
                return 'DONE'
            except (OSError,ValueError,KeyError,TypeError):return 'UNKNOWN'
        def frozen_probe():
            row=self.shared.db.execute("SELECT value FROM ledger_meta WHERE key='cross_cutover_freeze'").fetchone()
            if not row:return 'NOT_STARTED' if not self.shared.deployment_hold() else 'UNKNOWN'
            try:return 'DONE' if json.loads(row[0])['request_id']==self.operation and self._paused_hold() else 'UNKNOWN'
            except (ValueError,KeyError,TypeError):return 'UNKNOWN'
        def switch_probe():
            if self._units_match():return 'DONE' if self.controller.interlock_loaded(self.marker) is True else 'NOT_STARTED'
            return 'NOT_STARTED' if self._safe_stopped() else 'UNKNOWN'
        def allocated_probe():
            row=self.shared.db.execute("SELECT value FROM ledger_meta WHERE key='cross_allocation_verified'").fetchone()
            if not row:return 'NOT_STARTED'
            try:return 'DONE' if json.loads(row[0])['request_id']==self.allocation and self._safe_stopped() else 'UNKNOWN'
            except (ValueError,KeyError,TypeError):return 'UNKNOWN'
        def release_probe():
            row=self.shared.db.execute("SELECT value FROM ledger_meta WHERE key='cross_cutover_release'").fetchone()
            if not row:return 'NOT_STARTED'
            try:return 'DONE' if json.loads(row[0])['request_id']==self.release_id else 'UNKNOWN'
            except (ValueError,KeyError,TypeError):return 'UNKNOWN'
        return {'interlock_armed':lambda:'DONE' if self._check_marker() and self.controller.interlock_loaded(self.marker) is True else 'NOT_STARTED',
                'frozen':frozen_probe,'stopped':lambda:'DONE' if self._safe_stopped() else ('NOT_STARTED' if self._check_marker() and self._paused_hold() else 'UNKNOWN'),
                'backup_verified':lambda:backup_probe(self.before),'units_switched':switch_probe,
                'started_paused':lambda:'DONE' if not self.marker.exists() and self._units_match() and self._paused_hold() and self.controller.wait_healthy_paused() is True else 'NOT_STARTED',
                'allocated':allocated_probe,'backup_after':lambda:backup_probe(self.after),
                'awaiting_reboot':lambda:'DONE' if self.bootfile.is_file() and not self.bootfile.is_symlink() else 'NOT_STARTED',
                'released':release_probe,'complete':lambda:'DONE' if (self.archive/'complete').is_file() else 'NOT_STARTED'}

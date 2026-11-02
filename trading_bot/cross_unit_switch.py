"""Crash-recoverable unit-file switch; not a complete VM installer.

The caller must hold an exclusive host-level cutover lock, freeze both markets,
stop all writers and prevent automatic restart. This module never starts a
service or restores a database. Callbacks must prove those preconditions before
each replacement; they are not distributed fencing.
"""
import hashlib
import json
import os
import tempfile
import stat
from contextlib import contextmanager, ExitStack
from pathlib import Path


@contextmanager
def hold_writer_locks(financial):
    """Hold the four existing process-lock inodes; callers must stop services first.

    This fences cooperative paper workers and the relay on this host only. It
    cannot fence code that writes SQLite without using the process locks.
    """
    import fcntl
    path=Path(financial)
    if path.is_symlink() or not path.is_file():raise ValueError('Invalid financial database path')
    path=path.resolve(strict=True);owner=path.stat()
    with ExitStack() as stack:
        for suffix in ('.spot.lock','.futures.lock','.cross.lock','.delivery.lock'):
            lock=Path(str(path)+suffix);flags=os.O_RDWR|getattr(os,'O_NOFOLLOW',0)
            try:
                fd=os.open(lock,flags|os.O_CREAT|os.O_EXCL,0o600);created=True
            except FileExistsError:
                fd=os.open(lock,flags);created=False
            stack.callback(os.close,fd)
            info=os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_nlink!=1:raise ValueError('Invalid process lock file')
            if created:os.fchown(fd,owner.st_uid,owner.st_gid)
            elif (info.st_uid,info.st_gid)!=(owner.st_uid,owner.st_gid):raise ValueError('Unexpected process lock owner')
            try:fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
            except BlockingIOError:raise ValueError('Paper worker or relay is still running') from None
        yield


def _digest(data):return hashlib.sha256(data).hexdigest()


def _require(check, message):
    if check() is not True:raise ValueError(message)


def _write(path, data, mode):
    fd,name=tempfile.mkstemp(prefix='.cross-unit-',dir=path.parent)
    try:
        with os.fdopen(fd,'wb') as stream:
            os.fchmod(stream.fileno(),mode);stream.write(data);stream.flush();os.fsync(stream.fileno())
        os.replace(name,path)
        dfd=os.open(path.parent,os.O_RDONLY)
        try:os.fsync(dfd)
        finally:os.close(dfd)
    finally:
        if os.path.exists(name):os.unlink(name)


def switch_unit_files(unit_dir, candidate_dir, archive, units, *, assert_safe, new_units=(), fault=lambda p:None):
    """Replace named unit files while all writers remain stopped.

    Replaying after a crash is safe only while the caller preserves the same
    host lock and rechecks stopped writers. Unknown changes are refused.
    """
    if any(Path(p).is_symlink() for p in (unit_dir,candidate_dir,archive)):raise ValueError('Symlink directory rejected')
    unit_dir=Path(unit_dir).resolve(strict=True);candidate_dir=Path(candidate_dir).resolve(strict=True)
    archive=Path(archive).resolve(strict=True);names=tuple(units);new_units=set(new_units)
    if not names or len(set(names))!=len(names) or any(Path(n).name!=n or not n.endswith(('.service','.timer')) for n in names):
        raise ValueError('Invalid unit names')
    if not new_units.issubset(names):raise ValueError('Unknown new unit')
    _require(assert_safe,'Stopped writers not proven')
    manifest=archive/'unit-switch.json'
    if manifest.is_symlink():raise ValueError('Invalid switch manifest')
    resuming=manifest.exists()
    originals={};desired={}
    for name in names:
        src=candidate_dir/name;target=unit_dir/name
        if src.is_symlink() or target.is_symlink() or not src.is_file() or (target.exists() and not target.is_file()):raise ValueError('Unit files must be regular files')
        if not target.exists() and name not in new_units:raise ValueError('Existing unit is missing')
        if target.exists() and name in new_units and not resuming:raise ValueError('New unit already exists')
        originals[name]=target.read_bytes() if target.exists() else None;desired[name]=src.read_bytes()
    if not resuming:
        for name in names:
            saved=archive/(name+'.before')
            if saved.exists() or saved.is_symlink():raise ValueError('Unresolved unit archive')
            if originals[name] is not None:_write(saved,originals[name],0o600)
        state={'units':{name:{'before':_digest(originals[name]) if originals[name] is not None else None,'after':_digest(desired[name])} for name in names}}
        _write(manifest,json.dumps(state,sort_keys=True).encode(),0o600)
    else:
        state=json.loads(manifest.read_text())
        if set(state.get('units',{}))!=set(names):raise ValueError('Unit manifest mismatch')
        if {name for name in names if state['units'][name]['before'] is None}!=new_units:raise ValueError('New unit set changed')
        for name in names:
            saved=archive/(name+'.before')
            before=state['units'][name]['before']
            if saved.is_symlink() or (before is None and saved.exists()) or (before is not None and (not saved.is_file() or _digest(saved.read_bytes())!=before)) or _digest(desired[name])!=state['units'][name]['after']:
                raise ValueError('Unit archive or candidate changed')
            current=_digest(originals[name]) if originals[name] is not None else None
            if current not in (before,state['units'][name]['after']):
                raise ValueError('Unexpected current unit content')
    for name in names:
        _require(assert_safe,'Stopped writers not proven')
        target=unit_dir/name
        if not target.exists() or _digest(target.read_bytes())!=state['units'][name]['after']:
            _write(unit_dir/name,desired[name],0o644)
        fault('switched:'+name)
    _require(assert_safe,'Stopped writers not proven')
    return {'units':list(names),'all_switched':True}


def restore_unit_files(unit_dir, archive, units, *, assert_safe, verify_rollback, fault=lambda p:None):
    """Restore unit files only; never replace a financial or command database."""
    if any(Path(p).is_symlink() for p in (unit_dir,archive)):raise ValueError('Symlink directory rejected')
    unit_dir=Path(unit_dir).resolve(strict=True);archive=Path(archive).resolve(strict=True);names=tuple(units)
    if not names or len(set(names))!=len(names) or any(Path(n).name!=n or not n.endswith(('.service','.timer')) for n in names):
        raise ValueError('Invalid unit names')
    manifest=archive/'unit-switch.json'
    if manifest.is_symlink() or not manifest.is_file():raise ValueError('Missing unit manifest')
    state=json.loads(manifest.read_text())
    if set(state.get('units',{}))!=set(names):raise ValueError('Unit manifest mismatch')
    _require(assert_safe,'Stopped writers not proven');_require(verify_rollback,'Rollback safety not proven')
    for name in names:
        target=unit_dir/name;saved=archive/(name+'.before')
        before=state['units'][name]['before']
        if target.is_symlink() or saved.is_symlink() or (target.exists() and not target.is_file()) or (before is not None and not saved.is_file()) or (before is None and saved.exists()):raise ValueError('Invalid archived unit')
        current=_digest(target.read_bytes()) if target.exists() else None
        if (before is not None and _digest(saved.read_bytes())!=before) or current not in (before,state['units'][name]['after']):
            raise ValueError('Unit archive or current unit changed')
    for name in names:
        _require(assert_safe,'Stopped writers not proven');_require(verify_rollback,'Rollback safety not proven')
        target=unit_dir/name;before=state['units'][name]['before']
        if before is None and target.exists():
            target.unlink();dfd=os.open(unit_dir,os.O_RDONLY)
            try:os.fsync(dfd)
            finally:os.close(dfd)
        elif before is not None and (not target.exists() or _digest(target.read_bytes())!=before):
            _write(target,(archive/(name+'.before')).read_bytes(),0o644)
        fault('restored:'+name)
    _require(assert_safe,'Stopped writers not proven');_require(verify_rollback,'Rollback safety not proven')
    return {'units':list(names),'all_restored':True}

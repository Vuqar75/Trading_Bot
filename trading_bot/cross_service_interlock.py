"""Persistent systemd start interlock for a staged paper cutover.

Requires an external exclusive host-level lock. It does not stop existing
services, validate financial state, or perform a deployment. The caller must
stop/fence all writers and verify all unit files before releasing the marker.
"""
import os
import re
from pathlib import Path
from .cross_unit_switch import _require, _write


def _paths(unit_dir,marker,units):
    root=Path(unit_dir);marker=Path(marker);names=tuple(units)
    if root.is_symlink() or not root.is_dir() or not marker.is_absolute() or marker.is_symlink():raise ValueError('Invalid interlock path')
    if not re.fullmatch(r'/[A-Za-z0-9_./-]+',str(marker)) or '..' in marker.parts or not marker.parent.is_dir() or any(p.is_symlink() for p in marker.parents):raise ValueError('Invalid marker path')
    if not names or len(set(names))!=len(names) or any(Path(n).name!=n or not n.endswith('.service') for n in names):raise ValueError('Invalid service names')
    return root.resolve(strict=True),marker,names


def arm_interlock(unit_dir,marker,units,operation,*,reload_units,fault=lambda p:None):
    """Install drop-ins, reload, then persist marker before any unit switch."""
    root,marker,names=_paths(unit_dir,marker,units)
    if not isinstance(operation,str) or not 13<len(operation)<=256 or not re.fullmatch(r'cross:freeze:[A-Za-z0-9_.:-]+',operation):raise ValueError('Invalid operation')
    content=('[Unit]\nConditionPathExists=!'+str(marker)+'\n').encode()
    for name in names:
        directory=root/(name+'.d')
        if directory.is_symlink():raise ValueError('Invalid drop-in directory')
        created=not directory.exists();directory.mkdir(mode=0o755,exist_ok=True)
        if created:
            fd=os.open(root,os.O_RDONLY)
            try:os.fsync(fd)
            finally:os.close(fd)
        target=directory/'90-paper-cross-interlock.conf'
        if target.is_symlink() or (target.exists() and target.read_bytes()!=content):raise ValueError('Unexpected drop-in content')
        if not target.exists():_write(target,content,0o644)
        fault('dropin:'+name)
    _require(reload_units,'Systemd reload failed')
    fault('after_reload')
    if marker.exists():
        if not marker.is_file() or marker.read_text()!=operation:raise ValueError('Existing interlock belongs to another operation')
    else:_write(marker,operation.encode(),0o600)
    fault('marker_armed')
    return True


def release_interlock(unit_dir,marker,units,operation,*,verify_safe,fault=lambda p:None):
    """Release only after the caller proves coherent units and paused state."""
    root,marker,names=_paths(unit_dir,marker,units)
    _require(verify_safe,'Interlock release not proven safe')
    if not marker.exists() or not marker.is_file() or marker.read_text()!=operation:raise ValueError('Interlock marker mismatch')
    content=('[Unit]\nConditionPathExists=!'+str(marker)+'\n').encode()
    for name in names:
        target=root/(name+'.d')/'90-paper-cross-interlock.conf'
        if target.is_symlink() or not target.is_file() or target.read_bytes()!=content:raise ValueError('Interlock drop-in changed')
    _require(verify_safe,'Interlock release not proven safe')
    fault('before_release')
    marker.unlink()
    fd=os.open(marker.parent,os.O_RDONLY)
    try:os.fsync(fd)
    finally:os.close(fd)
    fault('after_release')
    return True

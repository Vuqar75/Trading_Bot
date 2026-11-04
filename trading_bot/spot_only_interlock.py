"""Dedicated start interlock for one Spot unit; never touches other drop-ins."""
import os
import re
from pathlib import Path

from .cross_service_interlock import _paths
from .cross_unit_switch import _require, _write


DROPIN = '91-paper-spothelp-interlock.conf'


def arm_interlock(unit_dir, marker, units, operation, *, reload_units, fault=lambda p: None):
    root, marker, names = _paths(unit_dir, marker, units)
    if len(names) != 1 or not re.fullmatch(r'cross:freeze:spothelp-[A-Za-z0-9_.:-]+', operation):
        raise ValueError('Invalid Spot interlock')
    content = ('[Unit]\nConditionPathExists=!' + str(marker) + '\n').encode()
    directory = root / (names[0] + '.d')
    if directory.is_symlink():
        raise ValueError('Invalid Spot drop-in directory')
    created = not directory.exists()
    directory.mkdir(mode=0o755, exist_ok=True)
    if created:
        fd = os.open(root, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    target = directory / DROPIN
    if target.is_symlink() or (target.exists() and target.read_bytes() != content):
        raise ValueError('Unexpected Spot drop-in')
    if not target.exists():
        _write(target, content, 0o644)
    fault('dropin:' + names[0])
    _require(reload_units, 'Systemd reload failed')
    fault('after_reload')
    if marker.exists():
        if not marker.is_file() or marker.read_text() != operation:
            raise ValueError('Existing Spot interlock belongs to another operation')
    else:
        _write(marker, operation.encode(), 0o600)
    fault('marker_armed')
    return True


def release_interlock(unit_dir, marker, units, operation, *, verify_safe, fault=lambda p: None):
    root, marker, names = _paths(unit_dir, marker, units)
    if len(names) != 1:
        raise ValueError('Exactly one Spot service required')
    _require(verify_safe, 'Spot interlock release not proven safe')
    if not marker.is_file() or marker.read_text() != operation:
        raise ValueError('Spot interlock marker mismatch')
    target = root / (names[0] + '.d') / DROPIN
    content = ('[Unit]\nConditionPathExists=!' + str(marker) + '\n').encode()
    if target.is_symlink() or not target.is_file() or target.read_bytes() != content:
        raise ValueError('Spot interlock drop-in changed')
    _require(verify_safe, 'Spot interlock release not proven safe')
    fault('before_release')
    marker.unlink()
    fd = os.open(marker.parent, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)
    fault('after_release')
    return True

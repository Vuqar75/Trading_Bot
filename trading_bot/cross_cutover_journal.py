"""Durable phase intent for one-host cross paper cutovers.

This journal does not perform a deployment. An intent may be replayed only
after its external action is proven idempotent and its actual state checked.
The persistent service interlock and financial hold are separate safeguards.
"""
import fcntl
import json
import os
import re
import stat
from contextlib import contextmanager
from pathlib import Path
from .cross_unit_switch import _write

STAGES=('created','interlock_armed','frozen','stopped','backup_verified',
        'units_switched','started_paused','allocated','backup_after',
        'awaiting_reboot','released','complete')


@contextmanager
def cutover_journal(directory,operation,commit,*,fault=lambda p:None):
    root=Path(directory)
    if root.is_symlink() or not root.is_dir() or stat.S_IMODE(root.stat().st_mode)&0o077:
        raise ValueError('Private cutover directory required')
    if not isinstance(operation,str) or not re.fullmatch(r'cross:freeze:[A-Za-z0-9_.:-]+',operation) or len(operation)>256:
        raise ValueError('Invalid operation identity')
    if not isinstance(commit,str) or not re.fullmatch(r'[0-9a-f]{40}',commit):raise ValueError('Invalid candidate commit')
    root=root.resolve(strict=True);lock=root/'journal.lock';manifest=root/'journal.json'
    flags=os.O_RDWR|os.O_CREAT|getattr(os,'O_NOFOLLOW',0)
    fd=os.open(lock,flags,0o600)
    try:
        info=os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink!=1:raise ValueError('Invalid journal lock')
        try:fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:raise ValueError('Another cutover owns this journal') from None
        if manifest.is_symlink():raise ValueError('Invalid cutover journal')
        if not manifest.exists():
            state={'version':1,'operation':operation,'commit':commit,'stage':'created','intent':None}
            _write(manifest,json.dumps(state,sort_keys=True).encode(),0o600)
        else:state=json.loads(manifest.read_text())
        if state.get('version')!=1 or state.get('operation')!=operation or state.get('commit')!=commit or state.get('stage') not in STAGES:
            raise ValueError('Cutover journal identity mismatch')
        if state.get('intent') is not None and (state['stage']=='complete' or state['intent']!=STAGES[STAGES.index(state['stage'])+1]):
            raise ValueError('Invalid cutover intent')
        yield CutoverJournal(manifest,state,fault)
    finally:os.close(fd)


class CutoverJournal:
    def __init__(self,path,state,fault):self.path=path;self.state=state;self.fault=fault

    def _save(self,state):
        self.fault('before_save')
        _write(self.path,json.dumps(state,sort_keys=True).encode(),0o600)
        self.state=state
        self.fault('after_save')

    def begin(self,target):
        stage=self.state['stage'];index=STAGES.index(stage)
        if index==len(STAGES)-1 or target!=STAGES[index+1]:raise ValueError('Invalid cutover transition')
        if self.state['intent'] not in (None,target):raise ValueError('Different cutover action pending')
        if self.state['intent'] is None:self._save(dict(self.state,intent=target))

    def complete(self,target):
        if self.state['intent']!=target:raise ValueError('Cutover intent missing')
        self._save(dict(self.state,stage=target,intent=None))

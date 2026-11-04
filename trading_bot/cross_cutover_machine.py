"""Offline cutover phase runner; real systemd backend is not yet connected.

Every probe must inspect durable state and return DONE, NOT_STARTED, or UNKNOWN.
UNKNOWN halts without retry. An action must be idempotent and its probe must
prove completion; this runner does not itself authorize financial operations.
"""
from .cross_cutover_journal import STAGES


def run_cutover_phases(journal, actions, probes, *, after_reboot=False,
                       verify_reboot=lambda:False, fault=lambda p:None):
    if journal.state['stage']=='awaiting_reboot':
        if not after_reboot:return 'awaiting_reboot'
        if verify_reboot() is not True:raise ValueError('Reboot and paused health not proven')
    while journal.state['stage']!='complete':
        current=journal.state['stage'];target=STAGES[STAGES.index(current)+1]
        if target not in actions or target not in probes:raise ValueError('Cutover action or probe missing')
        journal.begin(target)
        observation=probes[target]()
        if observation not in ('DONE','NOT_STARTED','UNKNOWN'):raise ValueError('Invalid phase observation')
        if observation=='UNKNOWN':raise ValueError('Cutover state uncertain; preserve pause')
        if observation=='NOT_STARTED':
            actions[target]()
            fault('after_action:'+target)
            if probes[target]()!='DONE':raise ValueError('Cutover action not verified')
        journal.complete(target)
        if target=='awaiting_reboot':return 'awaiting_reboot'
    return 'complete'

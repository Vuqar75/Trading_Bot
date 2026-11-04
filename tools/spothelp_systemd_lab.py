"""Isolated systemd rehearsal using dummy workers and synthetic SQLite data.

No Telegram transport or trading worker is started. Run only on a disposable
test fixture; unit names are uniquely prefixed and cleanup is restricted to them.
"""
import os
import subprocess
import sys
import uuid
from contextlib import ExitStack
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

if os.geteuid() != 0:
    raise SystemExit('root required for isolated systemd lab')

from tests import test_spothelp_switch_vm as fixture
from trading_bot.paper_shared_store import SharedPaperStore
from trading_bot import cross_unit_switch

switcher = fixture.switcher
case = fixture.SpothelpSwitchVmTests('test_only_spot_stops_and_restarts_on_hold')
case.setUp()
prefix = 'paper-spothelp-lab-' + uuid.uuid4().hex[:12]
mapping = {name: prefix + '-' + name + '.service' for name in ('spot', 'futures', 'cross', 'margin', 'monitor')}
logical = {f'tradingbot-{name}.service': mapping[name] for name in ('spot', 'futures', 'cross', 'margin', 'monitor')}
logical['tradingbot-futures.service'] = mapping['futures']
started = []
fault_mode = '--fault' in sys.argv


def run(*args):
    return subprocess.run(args, check=True, capture_output=True, text=True, timeout=60)


def start(name):
    run('systemd-run', '--quiet', '--unit=' + name.removesuffix('.service'),
        '--property=Type=exec', '/usr/bin/sleep', '600')
    started.append(name)


def prop(name, key):
    name = logical.get(name, name)
    return run('systemctl', 'show', name, '--property=' + key, '--value').stdout.strip()


def live(name):
    return prop(name, 'ActiveState')


def counts():
    return {name: int(live(unit) == 'active') for name, unit in mapping.items()}


def unit_property(name, key):
    if key == 'MainPID':
        return prop(name, key)
    return {'DropInPaths': str(case.unitdir / (name + '.d') / '91-paper-spothelp-interlock.conf'),
            'FragmentPath': str(case.unitdir / name), 'NeedDaemonReload': 'no'}[key]


def command(*args, **kwargs):
    if args[0] == 'daemon-reload':
        run('systemctl', 'daemon-reload')
    elif args[0] == 'stop':
        if args[1:] != ('tradingbot-spot.service',):
            raise AssertionError('attempted to stop a non-Spot service')
        run('systemctl', 'stop', mapping['spot'])
    elif args[0] == 'start':
        if args[1:] != ('tradingbot-spot.service',):
            raise AssertionError('attempted to start a non-Spot service')
        start(mapping['spot'])
    else:
        raise AssertionError(args)
    return ''


def output(args, **kwargs):
    if args == ['hostname']:
        return 'tradingbot-paper-test\n'
    if 'rev-parse' in args:
        return case.candidate + '\n'
    if 'status' in args:
        return ''
    raise AssertionError(args)


try:
    for unit in mapping.values():
        start(unit)
    other_pids = {unit: prop(unit, 'MainPID') for name, unit in mapping.items() if name != 'spot'}
    for suffix in ('.spot.lock', '.delivery.lock'):
        Path(str(case.financial) + suffix).touch()
    # Timers are not touched by the candidate; report them active to the
    # controller while dummy worker state comes from real systemd.
    def active(name):
        return 'active' if name in (switcher.TIMER, *switcher.AUX_TIMERS) else live(name)
    patches = [patch.object(switcher, 'ARCHIVE', case.archive),
               patch.object(switcher, 'FINANCIAL', case.financial),
               patch.object(switcher, 'QUEUE', case.queue),
               patch.object(switcher, 'OBSERVER', case.observer),
               patch.object(switcher, 'UNITDIR', case.unitdir),
               patch.object(switcher, 'MARKER', case.marker),
               patch.object(switcher, 'active', side_effect=active),
               patch.object(switcher, 'property_value', side_effect=unit_property),
               patch.object(switcher, 'process_counts', side_effect=counts),
               patch.object(switcher, 'command', side_effect=command),
               patch.object(switcher.subprocess, 'check_output', side_effect=output),
               patch.object(switcher.signal, 'signal')]
    if fault_mode:
        original_switch = cross_unit_switch.switch_unit_files
        def interrupted(*args, **kwargs):
            def fault(stage):
                if stage == 'switched:tradingbot-spot.service':
                    store = SharedPaperStore(case.financial)
                    try:
                        with store.ledger.transaction():
                            store.ledger._post('lab:new-event', 'FIXTURE', {}, [
                                ('reserve:free', 'USDT', Decimal(1)),
                                ('external:fixture', 'USDT', Decimal(-1))])
                    finally:
                        store.close()
                    raise RuntimeError('simulated crash after replacement')
            return original_switch(*args, **dict(kwargs, fault=fault))
        patches.append(patch.object(cross_unit_switch, 'switch_unit_files', side_effect=interrupted))
    with ExitStack() as stack:
        for item in patches:
            stack.enter_context(item)
        if fault_mode:
            try:
                switcher.main()
                raise AssertionError('fault was not injected')
            except RuntimeError as exc:
                if str(exc) != 'simulated crash after replacement':
                    raise
        else:
            switcher.main()
    if fault_mode:
        import importlib.util
        spec = importlib.util.spec_from_file_location('spothelp_lab_recovery',
                Path(__file__).resolve().parent / 'spothelp_recovery_vm.py')
        recovery = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(recovery)
        patches = [patch.object(recovery, 'ARCHIVE', case.archive),
                   patch.object(recovery, 'FINANCIAL', case.financial),
                   patch.object(recovery, 'UNITDIR', case.unitdir),
                   patch.object(recovery, 'MARKER', case.marker),
                   patch.object(recovery, 'candidate_allowed', return_value=True),
                   patch.object(recovery, 'counts', side_effect=counts),
                   patch.object(recovery, 'command', side_effect=command),
                   patch.object(recovery.subprocess, 'check_output', side_effect=output)]
        with ExitStack() as stack:
            for item in patches:
                stack.enter_context(item)
            recovery.recover()
        store = SharedPaperStore(case.financial)
        try:
            if not store.db.execute("SELECT 1 FROM ledger_events WHERE id='lab:new-event'").fetchone():
                raise AssertionError('financial event was lost')
            if not store.deployment_hold():
                raise AssertionError('recovery lost the entry hold')
        finally:
            store.close()
    if any(prop(unit, 'MainPID') != pid for unit, pid in other_pids.items()):
        raise AssertionError('dummy non-Spot service restarted')
    if counts() != {name: 1 for name in mapping}:
        raise AssertionError('dummy worker set changed')
    print('SPOTHELP_SYSTEMD_LAB_OK:', 'fault and recovery' if fault_mode else 'normal switch',
          '; actual dummy Spot stop/start; other PIDs preserved; no Telegram')
finally:
    for name in mapping.values():
        subprocess.run(['systemctl', 'stop', name], capture_output=True, timeout=30)
        subprocess.run(['systemctl', 'reset-failed', name], capture_output=True, timeout=30)
    case.doCleanups()

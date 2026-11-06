"""Server-owned one-shot executor. No Windows, sibling checkout or key prompt.

Timer ticks may collect before the first send claim only. A send claim without
a durable response blocks every future network attempt; saved replies recover
locally. This does not authorize a recurring paid schedule or trading.
"""
import argparse
from contextlib import contextmanager, closing
import fcntl
import json
import os
from pathlib import Path
import sqlite3
import time

from .adaptive_ai_executor import run, recover, ResearchAPIError
from .adaptive_ai_export import export
from .adaptive_budget_resilience import verify, migrate
from .adaptive_shadow import collect, PERIOD


@contextmanager
def exclusive(state):
    state = Path(state)
    if not state.is_dir() or state.is_symlink():
        raise ValueError('existing_private_state_required')
    fd = os.open(state / 'server-executor.lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ValueError('executor_already_running') from None
        yield
    finally:
        os.close(fd)


def credential():
    directory = os.environ.get('CREDENTIALS_DIRECTORY')
    if not directory:
        raise ValueError('systemd_credential_not_bound')
    p = Path(directory) / 'TRADING_BOT_AI'
    if p.is_symlink() or not p.is_file() or p.stat().st_size > 8192:
        raise ValueError('systemd_credential_invalid')
    value = p.read_text().strip()
    if not value or any(c in value for c in ('\n', '\r', '\x00')):
        raise ValueError('systemd_credential_invalid')
    return value


def owner(state):
    state = Path(state)
    if (state / 'migration-source-frozen.json').exists():
        raise ValueError('budget_moved_source_disabled')
    marker = state / 'server-owner.json'
    if marker.is_symlink():
        raise ValueError('server_ownership_not_imported')
    data = json.loads(marker.read_text())
    if data.get('role') != 'ACTIVE_SERVER' or not data.get('migration_id'):
        raise ValueError('server_ownership_not_imported')
    for name in ('experiment-budget.db', 'interactive-once.lock', 'approved-followup-once.lock'):
        p = state / name
        if not p.is_file() or p.is_symlink():
            raise ValueError('original_budget_or_locks_missing')
    return data


def journal(state):
    with closing(sqlite3.connect((Path(state) / 'experiment-budget.db').resolve().as_uri() + '?mode=ro', uri=True)) as db:
        rows = verify(db)
        decisions = db.execute('SELECT request_id FROM adaptive_send_claims').fetchall()
        saved = {r[0] for r in db.execute('SELECT request_id FROM adaptive_responses')}
        complete = {r[0] for r in rows if r[3] is not None}
    return rows, [r[0] for r in decisions], saved, complete


def execute(state, public, *, clock=lambda: int(time.time()*1000), fetch=collect,
            secret=credential, send=run, recovery=recover, publish=export):
    state, public = Path(state), Path(public)
    with exclusive(state):
        owner(state)
        rows, claims, saved, complete = journal(state)
        next_gate = state / 'adaptive-next-once.lock'
        if next_gate.exists():
            # Never collect or send again after any claim, including a crash.
            adaptive = [r[0] for r in rows if r[0].startswith('adaptive:')]
            rid = adaptive[-1] if adaptive else None
            if rid in saved:
                if rid not in complete:
                    recovery(state, rid)
                try:
                    publish(state/'experiment-budget.db', public, clock())
                except ValueError:
                    return {'status': 'stored_result_not_current', 'network_retry': False}
                return {'status': 'completed', 'network_retry': False}
            return {'status': 'attempt_uncertain_or_gate_consumed', 'network_retry': False}
        if len(rows) != 2 or sum(r[1] for r in rows) != 5718 or any(r[3] is not None for r in rows):
            raise ValueError('original_budget_state_changed')
        if claims or saved:
            raise ValueError('existing_send_claim_requires_review')
        now = clock()
        boundary = now // PERIOD * PERIOD
        if not 120000 <= now - boundary <= 300000:
            return {'status': 'waiting_for_next_boundary', 'network_retry': False}
        rid = 'adaptive:' + str(boundary)
        if any(r[0] == rid for r in rows):
            return {'status': 'waiting_for_unused_boundary', 'network_retry': False}
        migrate(state)
        key = secret()  # systemd credential, never input(), argv or Windows.
        snapshot, baseline = fetch()
        now = clock()
        if not 120000 <= now-boundary <= 300000 or snapshot.get('latest_4h_ms') != boundary-PERIOD:
            raise ValueError('snapshot_window_or_boundary_invalid')
        previous = {k: os.environ.get(k) for k in ('OPENAI_API_KEY', 'TRADING_BOT_AI')}
        os.environ.pop('OPENAI_API_KEY', None)
        os.environ['TRADING_BOT_AI'] = key
        del key
        try:
            try:
                result = send(snapshot, baseline, state, next_authorized=True, clock=clock)
            except Exception:
                # Recovery is local only. Uncertain requests are never resent.
                _, _, saved, _ = journal(state)
                if rid not in saved:
                    raise
                result = recovery(state, rid)
        finally:
            for k, v in previous.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v
        publish(state/'experiment-budget.db', public, clock())
        return {'status': 'completed', 'mode': result['response']['mode'],
                'baseline': baseline, 'reserved_micro_usd': result['reserved_micro_usd'],
                'usage': result.get('usage'), 'network_retry': False}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--state', required=True)
    parser.add_argument('--public', required=True)
    parser.add_argument('--authorized-next', action='store_true', required=True)
    args = parser.parse_args()
    # Never print arbitrary exception messages/tracebacks or credential values.
    try:
        result = execute(args.state, args.public)
    except ResearchAPIError as exc:
        result = {'status': 'not_completed', 'diagnostic': exc.safe_details, 'network_retry': False}
    except Exception:
        result = {'status': 'not_completed_check_private_journal', 'network_retry': False}
    # This timer services one authorization only. Terminal markers suppress
    # future ticks; manual recovery can still read the durable response.
    result['checked_ms'] = int(time.time()*1000)
    state = Path(args.state)
    if state.is_dir():
        terminal = result['status'] in ('completed', 'stored_result_not_current', 'attempt_uncertain_or_gate_consumed')
        if result['status'].startswith('not_completed') and (state/'adaptive-next-once.lock').exists():
            terminal = True
        for name in ('server-status.json', 'server-terminal.json') if terminal else ('server-status.json',):
            temporary = state / (name+'.next')
            fd = os.open(temporary, os.O_CREAT|os.O_TRUNC|os.O_WRONLY|os.O_NOFOLLOW, 0o600)
            with os.fdopen(fd, 'w') as f:
                json.dump(result, f, sort_keys=True); f.flush(); os.fsync(f.fileno())
            os.replace(temporary, state/name)
        fd = os.open(state, os.O_RDONLY|os.O_DIRECTORY)
        try: os.fsync(fd)
        finally: os.close(fd)
    print(json.dumps(result, sort_keys=True))


if __name__ == '__main__':
    main()

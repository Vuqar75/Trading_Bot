"""Interactive, one-shot market observation; never accepts a secret as argv."""
import getpass
import json
import os
import sqlite3
import sys
import warnings
from pathlib import Path

from trading_bot.market_research import ResearchError, build_snapshot, canonical, offline_report, render_report
from trading_bot.openai_research import prepare_request
from trading_bot.research_api_budget import APP_ID, MODEL, paid_analysis
from trading_bot.research_public_inputs import capture_public


def _require_unused_budget(path):
    """Do not turn an earlier/uncertain experiment into a second API call."""
    if not path.exists():
        return
    db = sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True)
    try:
        if db.execute('PRAGMA application_id').fetchone()[0] != APP_ID:
            raise ResearchError('not_a_research_budget_database')
        if db.execute('SELECT count(*) FROM api_attempts').fetchone()[0]:
            raise ResearchError('existing_attempt_requires_inspection')
    finally:
        db.close()


def run(home=None):
    """Capture fresh data and make at most one attempt using the user's key."""
    if not sys.stdin.isatty():
        raise ResearchError('interactive_terminal_required')
    state = (Path.home() if home is None else Path(home)) / '.local/state/tradingbot-ai'
    budget = state / 'experiment-budget.db'
    gate = state / 'interactive-once.lock'
    if gate.exists():
        raise ResearchError('prior_attempt_requires_inspection')
    _require_unused_budget(budget)
    # getpass fallback can echo the secret; reject rather than use it.
    with warnings.catch_warnings():
        warnings.simplefilter('error', getpass.GetPassWarning)
        secret = getpass.getpass('TRADING_BOT_AI (ввод скрыт): ')
    if not secret.strip():
        raise ResearchError('empty_key')
    old_umask = os.umask(0o077)
    old_standard = os.environ.pop('OPENAI_API_KEY', None)
    old_binding = os.environ.get('TRADING_BOT_AI')
    os.environ['TRADING_BOT_AI'] = secret
    del secret
    try:
        if not os.environ.get('TRADING_BOT_AI'):
            raise ResearchError('openai_api_key_not_configured')
        print('TRADING_BOT_AI: задана для этого процесса; получаю свежий публичный срез.', flush=True)
        raw = capture_public(interval='15m')
        snapshot = build_snapshot(raw)
        request = prepare_request(snapshot, model=MODEL)
        out = Path('research_results/ai-public') / snapshot['snapshot_id']
        out.mkdir(parents=True, exist_ok=True)
        report = offline_report(snapshot)
        for name, value in (('input.json', raw), ('snapshot.json', snapshot),
                            ('report.json', report), ('openai-request.json', request)):
            (out / name).write_text(canonical(value) + '\n', encoding='utf-8')
        (out / 'report.md').write_text(render_report(report), encoding='utf-8')
        state.mkdir(parents=True, exist_ok=True, mode=0o700)
        _require_unused_budget(budget)
        # Durable exclusive gate applies to *all* snapshots, not just one
        # request hash. Never remove automatically, including after failure.
        fd = os.open(gate, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, 'w', encoding='ascii') as marker:
            marker.write(snapshot['snapshot_id'] + '\n')
            marker.flush()
            os.fsync(marker.fileno())
        # Flush the directory entry as well: after a crash the gate must not
        # disappear while the budget database still contains an uncertain call.
        directory = os.open(state, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
        print('Срез сохранён; выполняется единственная попытка API.', flush=True)
        result = paid_analysis(snapshot, budget, enabled=True)
        (out / 'model-analysis.json').write_text(canonical(result) + '\n', encoding='utf-8')
        print('Готово. Срез:', snapshot['snapshot_id'], flush=True)
        print('Результат:', out / 'model-analysis.json', flush=True)
        return snapshot['snapshot_id']
    finally:
        os.environ.pop('TRADING_BOT_AI', None)
        if old_binding is not None:
            os.environ['TRADING_BOT_AI'] = old_binding
        if old_standard is not None:
            os.environ['OPENAI_API_KEY'] = old_standard
        os.umask(old_umask)


def main():
    try:
        run()
    except (ResearchError, getpass.GetPassWarning) as exc:
        # ResearchError values are fixed codes, never HTTP bodies or secrets.
        print('Не выполнено:', str(exc) if isinstance(exc, ResearchError) else 'hidden_input_unavailable', file=sys.stderr)
        return 1
    except (EOFError, KeyboardInterrupt):
        print('\nВвод отменён.', file=sys.stderr)
        return 1
    except Exception:
        # Provider bodies, exception messages, headers and URLs stay private.
        print('Не выполнено: локальная ошибка; проверьте журнал бюджета, повторно API не запускать.', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())

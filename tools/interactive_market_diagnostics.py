"""Hidden key input + one model metadata GET, never a paid Responses request."""
import getpass
import json
import os
import sqlite3
import sys
import warnings
from pathlib import Path
from trading_bot.market_research import ResearchError, canonical
from trading_bot.research_api_budget import APP_ID, ResearchAPIError, check_model_access


def budget_summary(path):
    if not path.exists():
        return {"exists": False, "attempts": 0, "reserved_micro_usd": 0}
    db = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
    try:
        if db.execute("PRAGMA application_id").fetchone()[0] != APP_ID:
            raise ResearchError("not_a_research_budget_database")
        count, amount = db.execute("SELECT COUNT(*), COALESCE(SUM(reserved_micro_usd),0) FROM api_attempts").fetchone()
        return {"exists": True, "attempts": count, "reserved_micro_usd": amount}
    finally:
        db.close()


def run(home=None):
    if not sys.stdin.isatty():
        raise ResearchError("interactive_terminal_required")
    state = (Path.home() if home is None else Path(home)) / ".local/state/tradingbot-ai"
    budget = state / "experiment-budget.db"
    before = budget_summary(budget)
    with warnings.catch_warnings():
        warnings.simplefilter("error", getpass.GetPassWarning)
        secret = getpass.getpass("TRADING_BOT_AI (ввод скрыт; только диагностика): ")
    if not secret.strip():
        raise ResearchError("empty_key")
    old_standard = os.environ.pop("OPENAI_API_KEY", None)
    old_alias = os.environ.get("TRADING_BOT_AI")
    os.environ["TRADING_BOT_AI"] = secret
    del secret
    try:
        try:
            result = check_model_access()
        except ResearchAPIError as exc:
            result = {"model_access_confirmed": False, "inference_requests": 0,
                      "diagnostic": exc.safe_details}
        after = budget_summary(budget)
        if before != after:
            raise ResearchError("budget_changed_during_read_only_diagnostic")
        result["budget"] = after
        # Does not remove the one-shot gate or mutate/open the budget for writing.
        state.mkdir(parents=True, exist_ok=True, mode=0o700)
        path = state / "model-access-diagnostic.json"
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as out:
            out.write(canonical(result) + "\n")
        print(canonical(result))
        return result
    finally:
        os.environ.pop("TRADING_BOT_AI", None)
        if old_alias is not None:
            os.environ["TRADING_BOT_AI"] = old_alias
        if old_standard is not None:
            os.environ["OPENAI_API_KEY"] = old_standard


def main():
    try:
        run()
    except (ResearchError, getpass.GetPassWarning) as exc:
        print(str(exc) if isinstance(exc, ResearchError) else "hidden_input_unavailable", file=sys.stderr)
        return 1
    except (EOFError, KeyboardInterrupt):
        print("Ввод отменён.", file=sys.stderr)
        return 1
    except Exception:
        print("local_diagnostic_error_no_automatic_retry", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

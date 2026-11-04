"""Explicit one-shot API experiment, independent of the trading application.

Reserve conservative cost before HTTP; retain reservations on failure/timeout.
This bounds requests at the documented rates, not the provider's billing system.
No automatic retry, tool calls, polling, alternative hosts or auto top-ups.
"""
import hashlib
import json
import os
import sqlite3
import time
from decimal import Decimal, ROUND_CEILING
from pathlib import Path
from urllib.request import HTTPRedirectHandler, Request, build_opener
from .market_research import ResearchError, canonical
from .openai_research import analyze_with_transport, prepare_request


MODEL = "gpt-6-luna"
# Standard rates per 1M tokens, checked 2026-11-04 against
# https://developers.openai.com/api/docs/pricing/ . No caching discounts assumed.
INPUT_USD_PER_MILLION = Decimal("0.10")
OUTPUT_USD_PER_MILLION = Decimal("0.50")
TOTAL_EXPERIMENT_MICRO_USD = 250000  # $0.25 for this experiment, NOT per day/run.
APP_ID = 0x41494F42


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise ResearchError("api_redirect_rejected")


def _api_key():
    # Standard name wins; accept the user's existing secret binding as an alias.
    # Never copy either value into command text, files, logs or request JSON.
    return os.environ.get("OPENAI_API_KEY") or os.environ.get("TRADING_BOT_AI")


def _send(request):
    key = _api_key()
    if not key:
        raise ResearchError("openai_api_key_not_configured")
    req = Request("https://api.openai.com/v1/responses", data=canonical(request).encode(),
                  headers={"Authorization": "Bearer " + key, "Content-Type": "application/json"}, method="POST")
    try:
        with build_opener(_NoRedirect()).open(req, timeout=45) as response:
            body = response.read(1000001)
        if len(body) > 1000000:
            raise ResearchError("api_response_too_large")
        return json.loads(body)
    except Exception:
        # Do not include URLs, body, headers, key, provider error or request text.
        raise ResearchError("openai_request_failed_reservation_retained") from None


def _connect(path):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path, timeout=15, isolation_level=None)
    try:
        db.execute("BEGIN IMMEDIATE")
        app_id = db.execute("PRAGMA application_id").fetchone()[0]
        tables = db.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        if app_id != APP_ID and (tables or app_id):
            raise ResearchError("not_a_research_budget_database")
        db.execute(f"PRAGMA application_id={APP_ID}")
        db.execute("CREATE TABLE IF NOT EXISTS api_attempts (request_id TEXT PRIMARY KEY, reserved_micro_usd INTEGER NOT NULL, created_at REAL NOT NULL, result_json TEXT)")
        db.commit()
        return db
    except Exception:
        db.rollback()
        db.close()
        raise


def paid_analysis(snapshot, budget_path, *, enabled=False, transport=None):
    if enabled is not True:
        raise ResearchError("paid_api_disabled")
    if transport is None and (any(m["source"] == "synthetic" for m in snapshot["markets"].values()) or
                              any(r["source_type"] == "synthetic" for r in snapshot["news"] + snapshot["fundamentals"])):
        raise ResearchError("paid_synthetic_input_rejected")
    # A locally prepared prompt has no browsing, search tools or other paid tools.
    request = prepare_request(snapshot, model=MODEL)
    request["service_tier"] = "default"
    # One token per UTF-8 byte is a deliberately loose upper bound for ordinary
    # text plus schema. Add 4096 for framing; output cap includes reasoning.
    input_bound = len(canonical(request).encode()) + 4096
    reserve = int((Decimal(input_bound)*INPUT_USD_PER_MILLION +
                   Decimal(request["max_output_tokens"])*OUTPUT_USD_PER_MILLION).to_integral_value(rounding=ROUND_CEILING))
    if transport is None and not _api_key():
        raise ResearchError("openai_api_key_not_configured")
    ident = hashlib.sha256(canonical(request).encode()).hexdigest()
    db = _connect(budget_path)
    try:
        db.execute("BEGIN IMMEDIATE")
        old = db.execute("SELECT result_json FROM api_attempts WHERE request_id=?", (ident,)).fetchone()
        if old:
            db.rollback()
            if old[0] is None:
                raise ResearchError("request_already_attempted_no_automatic_retry")
            return json.loads(old[0])
        spent = db.execute("SELECT COALESCE(SUM(reserved_micro_usd),0) FROM api_attempts").fetchone()[0]
        if spent + reserve > TOTAL_EXPERIMENT_MICRO_USD:
            db.rollback()
            raise ResearchError("research_experiment_budget_exhausted")
        db.execute("INSERT INTO api_attempts VALUES (?,?,?,NULL)", (ident, reserve, time.time()))
        db.commit()
        response = (transport or _send)(request)
        result = analyze_with_transport(snapshot, model=MODEL, transport=lambda _: response)
        result["budget"] = {"request_id": ident, "reserved_micro_usd": reserve,
                            "experiment_cap_micro_usd": TOTAL_EXPERIMENT_MICRO_USD,
                            "rate_checked_date": "2026-11-04"}
        with db:
            db.execute("UPDATE api_attempts SET result_json=? WHERE request_id=?", (canonical(result), ident))
        return result
    except ResearchError:
        raise
    except Exception:
        raise ResearchError("research_api_attempt_failed_reservation_retained") from None
    finally:
        db.close()

"""Explicit one-shot API experiment, independent of the trading application.

Reserve conservative cost before HTTP; retain reservations on failure/timeout.
This bounds requests at the documented rates, not the provider's billing system.
No automatic retry, tool calls, polling, alternative hosts or auto top-ups.
"""
import hashlib
import json
import os
import socket
import sqlite3
import ssl
import time
from decimal import Decimal, ROUND_CEILING
from pathlib import Path
from urllib.error import HTTPError, URLError
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

SAFE_PROVIDER_CODES = {
    "invalid_api_key", "insufficient_quota", "model_not_found", "permission_denied",
    "rate_limit_exceeded", "invalid_request_error", "unsupported_parameter",
    "unsupported_value", "invalid_value", "invalid_json_schema", "server_error",
    "billing_hard_limit_reached", "access_terminated", "authentication_error",
}


class ResearchAPIError(ResearchError):
    """Only allowlisted metadata may leave the HTTP boundary."""
    def __init__(self, category, http_status=None, provider_code=None):
        # All category values originate in code, not exception/source messages.
        self.safe_details = {"category": category, "http_status": http_status,
                             "provider_code": provider_code, "automatic_retry": False}
        suffix = f"_http_{http_status}" if http_status is not None else ""
        if provider_code:
            suffix += "_" + provider_code
        super().__init__("openai_" + category + suffix)


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise ResearchError("api_redirect_rejected")


def _api_key():
    # Standard name wins; accept the user's existing secret binding as an alias.
    # Never copy either value into command text, files, logs or request JSON.
    return os.environ.get("OPENAI_API_KEY") or os.environ.get("TRADING_BOT_AI")


def _request_json(req):
    try:
        with build_opener(_NoRedirect()).open(req, timeout=45) as response:
            body = response.read(1000001)
        if len(body) > 1000000:
            raise ResearchAPIError("response_too_large")
        try:
            return json.loads(body)
        except (ValueError, UnicodeError):
            raise ResearchAPIError("invalid_response_json") from None
    except HTTPError as exc:
        code = None
        try:
            error = json.loads(exc.read(8192)).get("error", {})
            if isinstance(error, dict):
                for field in ("code", "type"):
                    candidate = error.get(field)
                    if isinstance(candidate, str) and candidate in SAFE_PROVIDER_CODES:
                        code = candidate
                        break
        except Exception:
            pass
        status = exc.code if type(exc.code) is int and 100 <= exc.code <= 599 else None
        # HTTPError owns a response stream. Close it even on malformed bodies:
        # otherwise a ResourceWarning can expose its arbitrary reason string.
        try:
            exc.close()
        except Exception:
            pass
        raise ResearchAPIError("http_error", status, code) from None
    except (TimeoutError, socket.timeout):
        raise ResearchAPIError("timeout") from None
    except ssl.SSLError:
        raise ResearchAPIError("tls_error") from None
    except URLError as exc:
        reason = exc.reason
        category = "timeout" if isinstance(reason, (TimeoutError, socket.timeout)) else (
            "tls_error" if isinstance(reason, ssl.SSLError) else "network_error")
        raise ResearchAPIError(category) from None
    except ResearchError:
        raise
    except Exception:
        raise ResearchAPIError("transport_error") from None


def _send(request):
    key = _api_key()
    if not key:
        raise ResearchError("openai_api_key_not_configured")
    req = Request("https://api.openai.com/v1/responses", data=canonical(request).encode(),
                  headers={"Authorization": "Bearer " + key, "Content-Type": "application/json"}, method="POST")
    return _request_json(req)


def check_model_access():
    """One read-only model metadata GET; no inference, budget write or retry."""
    key = _api_key()
    if not key:
        raise ResearchError("openai_api_key_not_configured")
    req = Request("https://api.openai.com/v1/models/" + MODEL,
                  headers={"Authorization": "Bearer " + key}, method="GET")
    response = _request_json(req)
    if not isinstance(response, dict) or response.get("id") != MODEL:
        raise ResearchAPIError("unexpected_model_metadata")
    return {"requested_model": MODEL, "model_access_confirmed": True,
            "responses_write_confirmed": False, "inference_requests": 0}


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
        db.execute("CREATE TABLE IF NOT EXISTS api_diagnostics (request_id TEXT PRIMARY KEY, details_json TEXT NOT NULL)")
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
    except ResearchAPIError as exc:
        # Preserve the attempt and reservation. A separate table leaves the
        # existing four-column attempt journal compatible with old tooling.
        try:
            db.execute("INSERT OR REPLACE INTO api_diagnostics VALUES (?,?)", (ident, canonical(exc.safe_details)))
        except sqlite3.Error:
            pass  # Do not obscure the original sanitized cause.
        raise
    except ResearchError:
        raise
    except Exception:
        raise ResearchError("research_api_attempt_failed_reservation_retained") from None
    finally:
        db.close()

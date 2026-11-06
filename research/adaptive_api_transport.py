"""Portable transport extracted from the reviewed 9e88e76 HTTP boundary.

No budget creation, inference retry, alternate host or metadata probe.
"""
import json
import os
import socket
import ssl
from decimal import Decimal
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, Request, build_opener
from .adaptive_shadow import canonical

class ResearchError(ValueError):
    pass

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



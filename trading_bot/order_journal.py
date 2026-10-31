"""Stage 2/block 1: isolated, offline order intent journal.

No imports of Engine, credentials, Telegram or exchange HTTP clients. This is not
a production executor: UNKNOWN/SUBMITTING cannot be retried or resolved here.
"""
import re
import sqlite3
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from decimal import Decimal
from enum import Enum


class State(str, Enum):
    PREPARED = "PREPARED"
    SUBMITTING = "SUBMITTING"
    ACKNOWLEDGED = "ACKNOWLEDGED"
    REJECTED = "REJECTED"
    UNKNOWN = "UNKNOWN"


class FaultPoint(str, Enum):
    SCHEMA_BEFORE_COMMIT = "schema_before_commit"
    INTENT_WRITTEN = "intent_written"
    INTENT_BEFORE_COMMIT = "intent_before_commit"
    INTENT_COMMITTED = "intent_committed"
    CLAIM_WRITTEN = "claim_written"
    CLAIM_BEFORE_COMMIT = "claim_before_commit"
    CLAIM_COMMITTED = "claim_committed"
    RESPONSE_RECEIVED = "response_received"
    RESULT_WRITTEN = "result_written"
    RESULT_BEFORE_COMMIT = "result_before_commit"
    RESULT_COMMITTED = "result_committed"


class JournalError(Exception):
    """Safe, fixed messages only: do not include transport exception bodies."""


class IntentConflict(JournalError):
    pass


class DispatchBlocked(JournalError):
    pass


class SubmissionUncertain(JournalError):
    pass


def no_fault(point):
    pass


def _identifier(value):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,100}", value):
        raise ValueError("Invalid internal identifier")
    return value


def _amount(value):
    if value is None:
        return None
    # Accept decimal text only, never binary floats, exponents or arbitrary blobs.
    if not isinstance(value, str) or not re.fullmatch(r"[0-9]{1,18}(?:\.[0-9]{1,18})?", value):
        raise ValueError("Expected positive decimal text")
    number = Decimal(value)
    if number <= 0:
        raise ValueError("Expected positive decimal text")
    whole, _, fraction = value.partition(".")
    canonical = str(int(whole))
    fraction = fraction.rstrip("0")
    return canonical + ("." + fraction if fraction else "")


@dataclass(frozen=True)
class Request:
    side: str
    quantity: str | None = None
    quote_quantity: str | None = None
    symbol: str = "BTCUSDT"
    market: str = "spot"
    order_type: str = "MARKET"

    def __post_init__(self):
        if self.symbol != "BTCUSDT" or self.market != "spot" or self.order_type != "MARKET":
            raise ValueError("Only simulated BTCUSDT spot MARKET intents are supported")
        if self.side not in {"BUY", "SELL"}:
            raise ValueError("Invalid side")
        if (self.quantity is None) == (self.quote_quantity is None):
            raise ValueError("Specify exactly one amount")
        if self.side == "SELL" and self.quote_quantity is not None:
            raise ValueError("SELL requires base quantity")
        object.__setattr__(self, "quantity", _amount(self.quantity))
        object.__setattr__(self, "quote_quantity", _amount(self.quote_quantity))


@dataclass(frozen=True)
class Receipt:
    exchange_order_id: str | None
    status: str  # Observed status only; no fill ledger/settlement in this block.

    def validate(self):
        if self.status not in {"NEW", "FILLED", "PARTIALLY_FILLED", "REJECTED"}:
            raise ValueError("Unsupported simulated receipt")
        if self.exchange_order_id is not None:
            _identifier(self.exchange_order_id)
        elif self.status != "REJECTED":
            raise ValueError("Missing simulated order identifier")


@dataclass(frozen=True)
class Intent:
    intent_id: str
    namespace: str
    business_key: str
    client_order_id: str
    request: Request
    state: State
    revision: int
    created_ms: int
    updated_ms: int
    exchange_order_id: str | None
    exchange_status: str | None


class OrderJournal:
    """A dedicated SQLite file, never the bot Store or a migrated live database.

    Opening an existing journal does NOT reset SUBMITTING or grant another send.
    BEGIN IMMEDIATE serializes claims across independent connections/processes.
    """
    VERSION = 1

    def __init__(self, path, *, clock_ms=None, fault=no_fault):
        self.clock_ms = clock_ms or (lambda: time.time_ns() // 1_000_000)
        self.fault = fault
        self.db = sqlite3.connect(path, isolation_level=None, timeout=5)
        self.db.row_factory = sqlite3.Row
        try:
            self.db.execute("PRAGMA foreign_keys=ON")
            self.db.execute("PRAGMA synchronous=FULL")
            self._initialize()
        except BaseException:
            self.db.close()
            raise

    @contextmanager
    def _transaction(self):
        self.db.execute("BEGIN IMMEDIATE")
        try:
            yield
            self.db.commit()
        except BaseException:
            self.db.rollback()
            raise

    def _initialize(self):
        with self._transaction():
            tables = {r[0] for r in self.db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")}
            if tables:
                if tables not in ({"order_schema", "order_intents", "order_events"},
                                  {"order_schema", "order_intents", "order_events", "ledger_control", "ledger_fills", "ledger_account"}):
                    raise JournalError("Refusing a foreign database; use a dedicated test journal")
                rows = self.db.execute("SELECT version FROM order_schema WHERE singleton=1").fetchall()
                if len(rows) != 1 or rows[0][0] != self.VERSION:
                    raise JournalError("Unsupported order journal schema")
                return
            self.db.execute("CREATE TABLE order_schema (singleton INTEGER PRIMARY KEY CHECK(singleton=1), version INTEGER NOT NULL)")
            self.db.execute("INSERT INTO order_schema VALUES (1,1)")
            self.db.execute("""CREATE TABLE order_intents (
                intent_id TEXT PRIMARY KEY, namespace TEXT NOT NULL,
                business_key TEXT NOT NULL, client_order_id TEXT NOT NULL UNIQUE,
                environment TEXT NOT NULL DEFAULT 'simulation' CHECK(environment='simulation'),
                symbol TEXT NOT NULL CHECK(symbol='BTCUSDT'), side TEXT NOT NULL CHECK(side IN ('BUY','SELL')),
                quantity TEXT, quote_quantity TEXT,
                state TEXT NOT NULL CHECK(state IN ('PREPARED','SUBMITTING','ACKNOWLEDGED','REJECTED','UNKNOWN')),
                revision INTEGER NOT NULL CHECK(revision>=0),
                created_ms INTEGER NOT NULL, updated_ms INTEGER NOT NULL,
                exchange_order_id TEXT, exchange_status TEXT,
                UNIQUE(namespace,business_key),
                CHECK((quantity IS NULL)!=(quote_quantity IS NULL)),
                CHECK(side!='SELL' OR quantity IS NOT NULL))""")
            self.db.execute("""CREATE TABLE order_events (
                event_id INTEGER PRIMARY KEY, intent_id TEXT NOT NULL REFERENCES order_intents(intent_id),
                revision INTEGER NOT NULL, from_state TEXT, to_state TEXT NOT NULL,
                reason TEXT NOT NULL CHECK(reason IN ('INTENT_PREPARED','SUBMIT_STARTED',
                    'RESPONSE_RECORDED','DEFINITE_REJECTION','SUBMIT_EXCEPTION','INVALID_RESPONSE')),
                ts_ms INTEGER NOT NULL, UNIQUE(intent_id,revision))""")
            for operation in ("UPDATE", "DELETE"):
                self.db.execute(f"""CREATE TRIGGER events_no_{operation.lower()}
                    BEFORE {operation} ON order_events BEGIN
                    SELECT RAISE(ABORT,'Order events are append-only'); END""")
            self.fault(FaultPoint.SCHEMA_BEFORE_COMMIT)

    def close(self):
        self.db.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    @staticmethod
    def _decode(row):
        if row is None:
            raise JournalError("Unknown intent")
        return Intent(row["intent_id"], row["namespace"], row["business_key"],
                      row["client_order_id"], Request(row["side"], row["quantity"], row["quote_quantity"]),
                      State(row["state"]), row["revision"], row["created_ms"], row["updated_ms"],
                      row["exchange_order_id"], row["exchange_status"])

    def get(self, intent_id):
        return self._decode(self.db.execute("SELECT * FROM order_intents WHERE intent_id=?", (intent_id,)).fetchone())

    def events(self, intent_id):
        return [dict(r) for r in self.db.execute("SELECT * FROM order_events WHERE intent_id=? ORDER BY revision", (intent_id,))]

    def prepare(self, namespace, business_key, request):
        _identifier(namespace)
        _identifier(business_key)
        if not isinstance(request, Request):
            raise TypeError("Expected a normalized Request")
        with self._transaction():
            row = self.db.execute("SELECT * FROM order_intents WHERE namespace=? AND business_key=?",
                                  (namespace, business_key)).fetchone()
            if row:
                intent = self._decode(row)
                if intent.request != request:
                    raise IntentConflict("Business key already belongs to a different request")
                return intent
            now = self.clock_ms()
            intent_id, client_id = uuid.uuid4().hex, "tb" + uuid.uuid4().hex
            self.db.execute("""INSERT INTO order_intents
                (intent_id,namespace,business_key,client_order_id,symbol,side,quantity,quote_quantity,state,revision,created_ms,updated_ms)
                VALUES (?,?,?,?,?,?,?,?, 'PREPARED',0,?,?)""",
                (intent_id,namespace,business_key,client_id,request.symbol,request.side,request.quantity,request.quote_quantity,now,now))
            self.fault(FaultPoint.INTENT_WRITTEN)
            self.db.execute("INSERT INTO order_events(intent_id,revision,from_state,to_state,reason,ts_ms) VALUES (?,0,NULL,'PREPARED','INTENT_PREPARED',?)", (intent_id,now))
            self.fault(FaultPoint.INTENT_BEFORE_COMMIT)
        self.fault(FaultPoint.INTENT_COMMITTED)
        return self.get(intent_id)

    def _transition(self, intent_id, expected, target, reason, *, receipt=None, claim=False):
        written = FaultPoint.CLAIM_WRITTEN if claim else FaultPoint.RESULT_WRITTEN
        before_commit = FaultPoint.CLAIM_BEFORE_COMMIT if claim else FaultPoint.RESULT_BEFORE_COMMIT
        after_commit = FaultPoint.CLAIM_COMMITTED if claim else FaultPoint.RESULT_COMMITTED
        with self._transaction():
            intent = self.get(intent_id)
            if claim:
                has_ledger = self.db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='ledger_control'").fetchone()
                if has_ledger and self.db.execute("SELECT blocked FROM ledger_control WHERE singleton=1").fetchone()[0]:
                    raise DispatchBlocked("Accounting requires review")
            if intent.state != expected:
                raise DispatchBlocked("Intent state does not permit this transition")
            now = self.clock_ms()
            self.db.execute("""UPDATE order_intents SET state=?,revision=revision+1,updated_ms=?,
                exchange_order_id=?,exchange_status=? WHERE intent_id=? AND revision=?""",
                (target.value,now,receipt.exchange_order_id if receipt else None,
                 receipt.status if receipt else None,intent_id,intent.revision))
            self.fault(written)
            self.db.execute("""INSERT INTO order_events
                (intent_id,revision,from_state,to_state,reason,ts_ms) VALUES (?,?,?,?,?,?)""",
                (intent_id,intent.revision+1,expected.value,target.value,reason,now))
            self.fault(before_commit)
        self.fault(after_commit)
        return self.get(intent_id)

    def claim(self, intent_id):
        return self._transition(intent_id,State.PREPARED,State.SUBMITTING,"SUBMIT_STARTED",claim=True)

    def record_receipt(self, intent_id, receipt):
        if not isinstance(receipt, Receipt):
            raise TypeError("Expected a normalized receipt")
        receipt.validate()
        rejected = receipt.status == "REJECTED"
        return self._transition(intent_id,State.SUBMITTING,
                                State.REJECTED if rejected else State.ACKNOWLEDGED,
                                "DEFINITE_REJECTION" if rejected else "RESPONSE_RECORDED",receipt=receipt)

    def mark_unknown(self, intent_id, *, invalid_response=False):
        return self._transition(intent_id,State.SUBMITTING,State.UNKNOWN,
                                "INVALID_RESPONSE" if invalid_response else "SUBMIT_EXCEPTION")


class OfflineOrderCoordinator:
    """Explicitly offline seam for testing; no production adapter integration."""
    def __init__(self, journal, simulator, *, fault=no_fault):
        if getattr(simulator, "is_simulator", False) is not True:
            raise ValueError("This block only accepts an explicit simulator")
        self.journal, self.simulator, self.fault = journal, simulator, fault

    def dispatch(self, intent_id):
        intent = self.journal.claim(intent_id)  # Commit completes BEFORE submit.
        try:
            receipt = self.simulator.submit(intent)
        except Exception:
            self.journal.mark_unknown(intent_id)
            raise SubmissionUncertain("Submission outcome unknown; automatic resend disabled") from None
        self.fault(FaultPoint.RESPONSE_RECEIVED)
        try:
            if not isinstance(receipt, Receipt):
                raise ValueError("Unnormalized response")
            receipt.validate()
        except (ValueError, TypeError):
            self.journal.mark_unknown(intent_id, invalid_response=True)
            raise SubmissionUncertain("Invalid response; automatic resend disabled") from None
        # A DB failure/hard crash leaves SUBMITTING, or a committed final state.
        # Neither is eligible for a second dispatch. Never retry the simulator.
        return self.journal.record_receipt(intent_id,receipt)

"""No-network simulator with durable receipts in a SEPARATE test database.

It deliberately does not deduplicate client IDs: repeated submits would really
be counted twice, so the fault harness cannot hide a coordinator retry bug.
"""
import sqlite3
from .order_journal import Receipt


class SimulatedExchange:
    is_simulator = True

    def __init__(self, path, *, scenario="ack", fault=lambda point: None):
        if scenario not in {"ack", "reject", "timeout_before_accept", "timeout_after_accept", "invalid_response"}:
            raise ValueError("Unknown simulator scenario")
        self.path, self.scenario, self.fault = path, scenario, fault
        with self._connect() as db:
            tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")}
            if tables and tables != {"simulator_calls"}:
                raise ValueError("Simulator requires a separate test database")
            db.execute("""CREATE TABLE IF NOT EXISTS simulator_calls (
                id INTEGER PRIMARY KEY, client_order_id TEXT NOT NULL,
                side TEXT NOT NULL, quantity TEXT, quote_quantity TEXT,
                accepted INTEGER NOT NULL CHECK(accepted IN (0,1)))""")

    def _connect(self):
        # sqlite3 connection contexts commit/rollback but do not CLOSE a handle.
        from contextlib import closing
        return closing(sqlite3.connect(self.path))

    def submit(self, intent):
        with self._connect() as db:
            accepted = self.scenario not in {"reject", "timeout_before_accept"}
            cursor = db.execute("INSERT INTO simulator_calls(client_order_id,side,quantity,quote_quantity,accepted) VALUES (?,?,?,?,?)",
                                (intent.client_order_id,intent.request.side,intent.request.quantity,intent.request.quote_quantity,int(accepted)))
            identifier = str(cursor.lastrowid)
            db.commit()  # Survives a crash of the coordinator process.
        self.fault("simulator_after_submit")
        if self.scenario.startswith("timeout_"):
            raise TimeoutError("Simulated transport failure")
        if self.scenario == "invalid_response":
            return {"unsafe_raw_response": "not accepted by the journal"}
        return Receipt(None,"REJECTED") if self.scenario == "reject" else Receipt("sim-"+identifier,"NEW")

    def counts(self):
        with self._connect() as db:
            total,accepted = db.execute("SELECT COUNT(*),COALESCE(SUM(accepted),0) FROM simulator_calls").fetchone()
            return {"submissions":total,"accepted":accepted}

    def query(self, client_order_id):
        """Read-only simulated lookup; absence is not evidence of non-submission."""
        with self._connect() as db:
            rows = db.execute("SELECT id,client_order_id,side,quantity,quote_quantity FROM simulator_calls WHERE client_order_id=? AND accepted=1", (client_order_id,)).fetchall()
        if len(rows) != 1:
            return None
        ident, client, side, quantity, quote = rows[0]
        return {"client_order_id": client, "exchange_order_id": "sim-" + str(ident),
                "symbol": "BTCUSDT", "side": side, "quantity": quantity,
                "quote_quantity": quote, "status": "NEW"}

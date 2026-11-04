"""Offline hard-crash matrix. Default execution uses disposable databases only.

The worker is killed with os._exit at persistence/submit boundaries, bypassing
finally/rollback handlers. A new process/connection then attempts the same intent.
No exchanges, credentials, bot launchers or live data directories are accessed.
"""
import argparse
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from trading_bot.order_journal import (DispatchBlocked, FaultPoint, OfflineOrderCoordinator,
                                       OrderJournal, Request, State, SubmissionUncertain)
from trading_bot.order_simulator import SimulatedExchange

CRASH_EXIT = 86
REQUEST = Request("BUY",quote_quantity="10")
CASES = [
    ("schema_before_commit", None, 0, 0),
    ("before_prepare", None, 0, 0),
    ("intent_written", None, 0, 0),
    ("intent_before_commit", None, 0, 0),
    ("intent_committed", "PREPARED", 0, 0),
    ("claim_written", "PREPARED", 0, 0),
    ("claim_before_commit", "PREPARED", 0, 0),
    ("claim_committed", "SUBMITTING", 0, 0),
    ("simulator_after_submit", "SUBMITTING", 1, 1),
    ("response_received", "SUBMITTING", 1, 1),
    ("result_written", "SUBMITTING", 1, 1),
    ("result_before_commit", "SUBMITTING", 1, 1),
    ("result_committed", "ACKNOWLEDGED", 1, 1),
    ("clean", "ACKNOWLEDGED", 1, 1),
    ("timeout_before_accept", "UNKNOWN", 1, 0),
    ("timeout_after_accept", "UNKNOWN", 1, 1),
    ("invalid_response", "UNKNOWN", 1, 1),
    ("reject", "REJECTED", 1, 0),
]


def deny_network(*args, **kwargs):
    raise AssertionError("Network access is forbidden in the offline fault harness")


def worker(root, case):
    socket.socket = deny_network
    socket.create_connection = deny_network
    def crash(point):
        if getattr(point,"value",point) == case:
            os._exit(CRASH_EXIT)
    scenario = case if case in {"timeout_before_accept","timeout_after_accept","invalid_response","reject"} else "ack"
    simulator=SimulatedExchange(root/"simulator.db",scenario=scenario,fault=crash)
    with OrderJournal(root/"intents.db",fault=crash) as journal:
        crash("before_prepare")
        intent=journal.prepare("fault-harness","candle-1-buy",REQUEST)
        try:
            OfflineOrderCoordinator(journal,simulator,fault=crash).dispatch(intent.intent_id)
        except (DispatchBlocked,SubmissionUncertain):
            pass


def check_journal(journal, intent):
    events=journal.events(intent.intent_id)
    assert [r["revision"] for r in events] == list(range(intent.revision+1))
    assert events[0]["from_state"] is None
    assert events[-1]["to_state"] == intent.state.value
    assert all(a["to_state"] == b["from_state"] for a,b in zip(events,events[1:]))
    assert journal.db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    assert journal.db.execute("PRAGMA foreign_key_check").fetchall() == []


def run_matrix():
    rows=[]
    # The parent also has network disabled; subprocess pipes and SQLite are local.
    original_socket,original_connection=socket.socket,socket.create_connection
    socket.socket=deny_network
    socket.create_connection=deny_network
    try:
        for case,expected_state,submissions,accepted in CASES:
            with tempfile.TemporaryDirectory(prefix="order-fault-") as tmp:
                root=Path(tmp)
                p=subprocess.run([sys.executable,"-B",str(Path(__file__).resolve()),
                                  "--worker",case,"--root",str(root)],capture_output=True,text=True,timeout=20)
                expected_exit=0 if case in {"clean","timeout_before_accept","timeout_after_accept","invalid_response","reject"} else CRASH_EXIT
                assert p.returncode==expected_exit,(case,p.returncode,p.stderr)
                simulator=SimulatedExchange(root/"simulator.db")
                assert simulator.counts()=={"submissions":submissions,"accepted":accepted},case
                with OrderJournal(root/"intents.db") as journal:
                    existing=journal.db.execute("SELECT intent_id FROM order_intents").fetchall()
                    before=journal.get(existing[0][0]) if existing else None
                    assert (before.state.value if before else None)==expected_state,case
                    if before:check_journal(journal,before)
                    else:assert journal.db.execute("SELECT COUNT(*) FROM order_events").fetchone()[0]==0
                    intent=journal.prepare("fault-harness","candle-1-buy",REQUEST)
                    if before:
                        assert intent.intent_id==before.intent_id and intent.client_order_id==before.client_order_id
                    safe_first_attempt=expected_state in {None,"PREPARED"}
                    if safe_first_attempt:
                        OfflineOrderCoordinator(journal,simulator).dispatch(intent.intent_id)
                    else:
                        try:OfflineOrderCoordinator(journal,simulator).dispatch(intent.intent_id)
                        except DispatchBlocked:pass
                        else:raise AssertionError("Unsafe redispatch allowed")
                # Repeat the reopen/replay twice, with no new exchange requests.
                for _ in range(2):
                    with OrderJournal(root/"intents.db") as journal:
                        replay=journal.prepare("fault-harness","candle-1-buy",REQUEST)
                        assert replay.intent_id==intent.intent_id
                        try:OfflineOrderCoordinator(journal,simulator).dispatch(replay.intent_id)
                        except DispatchBlocked:pass
                        else:raise AssertionError("Second dispatch allowed")
                        check_journal(journal,journal.get(replay.intent_id))
                counts=simulator.counts()
                assert counts["submissions"]==submissions+int(safe_first_attempt)
                assert counts["submissions"]<=1 and counts["accepted"]<=1
                rows.append(dict(case=case,state_after_failure=expected_state,
                                 submissions_before_reopen=submissions,accepted_before_reopen=accepted,
                                 safe_first_attempt=safe_first_attempt,submissions_final=counts["submissions"],
                                 accepted_final=counts["accepted"],status="passed"))
        # Two actual processes compete for the same business intent and claim.
        with tempfile.TemporaryDirectory(prefix="order-race-") as tmp:
            root=Path(tmp)
            simulator=SimulatedExchange(root/"simulator.db")
            with OrderJournal(root/"intents.db") as journal:
                journal.prepare("fault-harness","candle-1-buy",REQUEST)
            args=[sys.executable,"-B",str(Path(__file__).resolve()),"--worker","clean","--root",str(root)]
            processes=[subprocess.Popen(args,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True) for _ in range(2)]
            for p in processes:
                _,err=p.communicate(timeout=20)
                assert p.returncode==0,err
            assert simulator.counts()=={"submissions":1,"accepted":1}
            with OrderJournal(root/"intents.db") as journal:
                intent=journal.prepare("fault-harness","candle-1-buy",REQUEST)
                assert intent.state==State.ACKNOWLEDGED
                check_journal(journal,intent)
            rows.append(dict(case="two_process_claim_race",submissions_final=1,accepted_final=1,status="passed"))
    finally:
        socket.socket, socket.create_connection=original_socket,original_connection
    return dict(schema_version=1,network="blocked",database_scope="temporary simulator-only databases",
                scenarios=len(rows),passed=len(rows),cases=rows)


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument("--output",type=Path)
    p.add_argument("--worker",choices=[r[0] for r in CASES],help=argparse.SUPPRESS)
    p.add_argument("--root",type=Path,help=argparse.SUPPRESS)
    args=p.parse_args()
    if args.worker:
        if args.root is None:p.error("Worker requires a temporary root")
        worker(args.root,args.worker)
        return
    report=run_matrix()
    if args.output:
        args.output.parent.mkdir(parents=True,exist_ok=True)
        args.output.write_text(json.dumps(report,indent=2)+"\n",encoding="utf-8")
    print(json.dumps(report,indent=2))


if __name__=="__main__":main()

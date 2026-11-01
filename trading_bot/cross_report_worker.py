"""Offline cross /report adapter; other cross commands are not implemented."""
from .cross_trade_report import cross_trade_report


def process_cross_reports(queue, shared, *, now=None):
    for job in queue.pending('futures_cross')[:20]:
        # Do not pass a pending mutation or pause to answer a later report.
        if job['command']!='/report':return
        queue.acknowledge(job['id'],cross_trade_report(shared,now=now))

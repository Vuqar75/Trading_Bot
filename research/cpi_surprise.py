"""Reproduce the preliminary CPI reaction screen with archived inputs."""
import argparse
from collections import defaultdict
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
from zoneinfo import ZoneInfo

from research import extreme_reversal as core
from research.flow_weekend import metrics
from research.mark_dislocation import SPLIT, simulate


def scheduled_releases(html):
    pattern = (r'<td class="date-cell"><p>([^<]+)</p></td>\s*'
               r'<td class="time-cell"><p>([^<]+)</p></td>\s*'
               r'<td class="desc-cell"><p><strong>Consumer Price Index</strong> for ([^<]+)')
    releases = {}
    for date, time, reference in re.findall(pattern, html):
        local = datetime.strptime(f"{date} {time}", "%A, %B %d, %Y %I:%M %p")
        utc = local.replace(tzinfo=ZoneInfo("America/New_York")).astimezone(timezone.utc)
        releases[utc] = reference
    return releases


def events_from_calendar(html):
    match = re.search(r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>', html)
    if not match:
        raise ValueError("Missing Investing calendar payload")
    store = json.loads(match[1])["props"]["pageProps"]["state"]["economicCalendarEventStore"]
    if store["event"]["event_id"] != 69:
        raise ValueError("Unexpected Investing event")
    events = {}
    for row in store["occurrences"]:
        time = datetime.fromisoformat(row["occurrence_time"].replace("Z", "+00:00"))
        if time in events:
            raise ValueError("Duplicate Investing event time")
        events[time] = row
    return events


def run(args):
    paths = [args.calendar, *args.schedules]
    source = [dict(file=path.name, sha256=hashlib.sha256(path.read_bytes()).hexdigest()) for path in paths]
    calendar = events_from_calendar(args.calendar.read_text(encoding="utf-8"))
    schedules = {}
    for path in args.schedules:
        schedules.update(scheduled_releases(path.read_text(encoding="utf-8")))
    signals, releases = [], []
    for time, reference in sorted(schedules.items()):
        stamp = int(time.timestamp() * 1000)
        if not core.BEGIN <= stamp < core.END:
            continue
        archived = calendar.get(time, {})
        actual, forecast = archived.get("actual"), archived.get("forecast")
        signal = dict(signal_ms=stamp, release_utc=time.isoformat(), reference=reference,
                      actual=actual, forecast=forecast)
        if actual is None or forecast is None or not reference.startswith(archived["reference_period"]):
            direction = "EXCLUDED"
        elif actual == forecast:
            direction = "WAIT"
        else:
            direction = "SHORT" if actual > forecast else "LONG"
        signal["direction"] = direction
        releases.append(signal)
        if direction in ("LONG", "SHORT"):
            signals.append(signal)

    trade, mark, funding, files, overlaps = core.load_data(args.archive, "BTCUSDT")
    by_minute = defaultdict(list)
    for stamp, rate in funding:
        by_minute[core.ix(stamp - stamp % core.M)].append((stamp, rate))
    scenarios = {}
    for name in ("base", "stress"):
        rows, account = simulate(signals, trade, mark, by_minute, "BTCUSDT",
                                 name == "stress", hold_minutes=30)
        total = metrics(rows, account)
        if not total["unresolved"] and abs(account["cash"] - 1000 - total["net"]) > 1e-8:
            raise ValueError("Ledger mismatch")
        scenarios[name] = dict(records=rows, total=total,
                               year_2025=metrics([row for row in rows if row["signal_ms"] < SPLIT]),
                               year_2026=metrics([row for row in rows if row["signal_ms"] >= SPLIT]))
    args.output.write_text(json.dumps(dict(calendar_sources=source, price_sources=files,
                                           overlaps=overlaps, releases=releases, scenarios=scenarios),
                                      indent=2, allow_nan=False) + "\n", encoding="utf-8")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("archive", type=Path)
    parser.add_argument("calendar", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("schedules", nargs=2, type=Path)
    run(parser.parse_args())

"""Offline CLI: python3 -m tools.market_research --input FILE --output DIR."""
import argparse
import json
from pathlib import Path
from trading_bot.market_research import ResearchError, build_snapshot, canonical, offline_report, render_report
from trading_bot.openai_research import prepare_request
from trading_bot.research_public_inputs import capture_public
from trading_bot.research_api_budget import paid_analysis


def main():
    parser = argparse.ArgumentParser(description="Offline market observer: no API requests or trading")
    source_group = parser.add_mutually_exclusive_group(required=True)
    source_group.add_argument("--input")
    source_group.add_argument("--collect-public", action="store_true", help="Free public Binance + Fed/ECB RSS GETs; no OpenAI")
    parser.add_argument("--interval", choices=["5m", "15m", "1h"], default="15m")
    parser.add_argument("--output", required=True)
    parser.add_argument("--prepare-model", help="Explicit model identifier; prepare JSON only, never send")
    parser.add_argument("--paid-once", action="store_true", help="Explicit single OpenAI analysis; $0.25 total experiment cap")
    parser.add_argument("--budget-db", help="Required persistent research-only budget path for paid use")
    args = parser.parse_args()
    if args.paid_once and not args.budget_db:
        parser.error("--paid-once requires the same persistent --budget-db for the whole experiment")
    try:
        if args.collect_public:
            raw = capture_public(interval=args.interval)
        else:
            source = Path(args.input)
            if source.stat().st_size > 1000000:
                raise ResearchError("input_too_large")
            raw = json.loads(source.read_text(encoding="utf-8"))
        snapshot = build_snapshot(raw)
        report = offline_report(snapshot)
        out = Path(args.output) / snapshot["snapshot_id"]
        out.mkdir(parents=True, exist_ok=True)
        for name, obj in (("input.json", raw), ("snapshot.json", snapshot), ("report.json", report)):
            (out / name).write_text(canonical(obj) + "\n", encoding="utf-8")
        (out / "report.md").write_text(render_report(report), encoding="utf-8")
        if args.prepare_model:
            (out / "openai-request.json").write_text(canonical(prepare_request(snapshot, model=args.prepare_model)) + "\n", encoding="utf-8")
        if args.paid_once:
            analysis = paid_analysis(snapshot, args.budget_db, enabled=True)
            (out / "model-analysis.json").write_text(canonical(analysis) + "\n", encoding="utf-8")
    except (ResearchError, ValueError, OSError) as exc:
        # Arbitrary paths, supplied content and exception messages stay private.
        parser.exit(2, f"Research failed: {str(exc) if isinstance(exc, ResearchError) else type(exc).__name__}\n")
    print(f"RESEARCH_OK {out}; mode={'paid_or_cached' if args.paid_once else 'offline'}; trading_actions=0")


if __name__ == "__main__":
    main()

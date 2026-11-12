"""Shared-wallet, cross-symbol funding and portfolio-criterion verification."""
import gzip
import json
from pathlib import Path
import sys

from research.usdm_intraday_data import zipped
from tools.audit_dynamic_research import audit,same


def main(summary_path,root):
    audit(summary_path)
    summary=json.loads(summary_path.read_text());stage='development' if summary['stage'].startswith('development') else 'validation'
    journal=json.load(gzip.open(summary_path.parent/(stage+'_journal.json.gz'),'rt'));universe=summary['universe'];rates={}
    for symbol in universe:
        rates[symbol]={}
        for source in summary['sources'][symbol]:
            if source['kind']!='fundingRate':continue
            for row in zipped(root/source['file']):
                if row[0].isdigit():rates[symbol][int(row[0])]=float(row[2])
    for key,data in journal.items():
        result=summary['results'][key];totals={s:0. for s in universe};counts={s:0 for s in universe};months=set()
        import datetime as dt
        for trade in data['trades']:
            symbol=trade['symbol']
            if symbol not in universe:raise ValueError('unregistered position asset')
            expected={t:r for t,r in rates[symbol].items() if trade['entry_ms']<=t<trade['exit_ms']}
            actual={e['event_ms']:e['rate'] for e in trade['funding_events']}
            if actual!=expected:raise ValueError('funding missed or wrong asset')
            totals[symbol]+=trade['net'];counts[symbol]+=1
            months.add(dt.datetime.fromtimestamp(trade['entry_ms']/1000,dt.timezone.utc).strftime('%Y-%m'))
        for symbol in universe:
            same(result['by_asset'][symbol]['net'],totals[symbol])
            if result['by_asset'][symbol]['closed']!=counts[symbol]:raise ValueError('asset count')
        same(result['without_best_asset'],result['net']-max(totals.values()))
        if result['active_months']!=len(months):raise ValueError('active month count')
        if result['max_concurrent_positions']!=(1 if data['trades'] else 0):raise ValueError('exposure claim')
    base,stress=summary['results']['base'],summary['results']['stress'];year='2025' if stage=='development' else '2026'
    checks={'counts':all(r['closed']>=30 and r['long']>=10 and r['short']>=10 for r in (base,stress)),
            'positive_both':base['net']>0 and stress['net']>0,'both_halves_positive':all(stress['segments'][year+'H'+str(k)]>0 for k in (1,2)),
            'without_best_positive':stress['without_best']>0,'drawdown':max(base['drawdown'],stress['drawdown'])<=.02,
            'without_best_asset_positive':stress['without_best_asset']>0,'active_months':stress['active_months']>=6}
    if checks!=summary['checks'] or all(checks.values())!=summary['candidate']:raise ValueError('qualification mismatch')
    report={'status':'PASS','shared_cash':'verified','single_position':'verified','source_symbol_funding':'verified',
            'without_best_asset':'verified','candidate':summary['candidate'],'new_simulations':0}
    (summary_path.parent/(stage+'_portfolio_audit.json')).write_text(json.dumps(report,indent=2)+'\n');print(json.dumps(report,indent=2))


if __name__=='__main__':main(Path(sys.argv[1]),Path(sys.argv[2]))

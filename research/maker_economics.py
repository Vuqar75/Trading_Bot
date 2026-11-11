"""Arithmetic on saved paths only. No orders, fills, backtests or network."""
import collections, csv, gzip, hashlib, json, pathlib

ROOT=pathlib.Path(__file__).resolve().parent.parent
INTRADAY=ROOT/'research/usdm_screen_summary.csv.gz'
SESSION=ROOT/'research/session_momentum_results/journal.json.gz'
OUT=ROOT/'research/maker_economics.csv'

def measures(gross,funding,turnover):
    if turnover<=0:raise ValueError('zero turnover')
    # Reference is half round-trip turnover; all bps use this same denominator.
    denominator=turnover/2
    breakeven=(gross+funding)/denominator*10000
    maker=turnover*.0002
    mixed_approx=turnover*.00035 # actual entry/exit notionals not available in intraday summary
    return {'breakeven_bps':breakeven,'maker_maker_fee':maker,'maker_taker_fee_approx':mixed_approx,
            'maker_maker_headroom_bps':breakeven-4,'maker_taker_headroom_bps_approx':breakeven-7}

def sha(p):return hashlib.sha256(p.read_bytes()).hexdigest()

def calculate():
    result=[];groups=collections.defaultdict(list)
    with gzip.open(INTRADAY,'rt',encoding='utf8',newline='') as f:
        for r in csv.DictReader(f):
            if r['scenario'] in ('base','stress'):
                groups[tuple(r[k] for k in ('timeframe','family','side','symbol','scenario'))].append(r)
    for (tf,family,side,symbol,scenario),rows in sorted(groups.items()):
        if len(rows)!=4:raise ValueError('incomplete segment group')
        if any(abs(float(r['gross'])+float(r['funding'])-float(r['fees'])-float(r['impact'])-float(r['net']))>1e-8 for r in rows):raise ValueError('accounting mismatch')
        gross=sum(float(r['gross']) for r in rows)
        if gross<=0:continue
        funding=sum(float(r['funding']) for r in rows);turnover=sum(float(r['turnover']) for r in rows)
        if turnover<=0:continue
        result.append({'study':'usdm_intraday','timeframe':tf,'family':family,'side':side,'symbol':symbol,'scenario':scenario,
                       'closed_trades':sum(int(r['closed']) for r in rows),'entries':sum(int(r['entries']) for r in rows),
                       'marked_open_segments':sum(bool(r['open_mark_net']) for r in rows),'gross':gross,'funding':funding,
                       'turnover_both_sides':turnover,**measures(gross,funding,turnover),
                       'mixed_fee_exact':'','notes':'independent per-segment account resets; maker/taker approximate; may include final MTM'})
    with gzip.open(SESSION,'rt',encoding='utf8') as f:journal=json.load(f)
    for scenario in ('base','stress'):
        for symbol in ('BTCUSDT','ETHUSDT'):
            for side in ('BOTH','LONG','SHORT'):
                rows=[r for r in journal if r['scenario']==scenario and r['symbol']==symbol and 'net' in r and (side=='BOTH' or r['direction']==side)]
                gross=sum(r['gross'] for r in rows)
                if not rows or gross<=0:continue
                funding=sum(r['funding'] for r in rows);entry=sum(r['qty']*r['reference_in'] for r in rows);exit_=sum(r['qty']*r['reference_out'] for r in rows)
                turnover=entry+exit_;m=measures(gross,funding,turnover)
                result.append({'study':'session_momentum','timeframe':'30m','family':'ONFH_SLH','side':side,'symbol':symbol,'scenario':scenario,
                               'closed_trades':len(rows),'entries':len(rows),'marked_open_segments':0,'gross':gross,'funding':funding,
                               'turnover_both_sides':turnover,**m,'mixed_fee_exact':entry*.0002+exit_*.0005,
                               'notes':'BOTH is one account path; directional rows partition it; costs use reference prices'})
    return result

def main():
    rows=calculate();OUT.parent.mkdir(parents=True,exist_ok=True)
    with OUT.open('w',encoding='utf8',newline='') as f:
        w=csv.DictWriter(f,fieldnames=list(rows[0]));w.writeheader();w.writerows(rows)
    print(json.dumps({'rows':len(rows),'intraday_input_sha256':sha(INTRADAY),'session_input_sha256':sha(SESSION),'output_sha256':sha(OUT),
        'positive_after_maker_maker':sum(x['maker_maker_headroom_bps']>0 for x in rows),
        'positive_after_mixed_approx':sum(x['maker_taker_headroom_bps_approx']>0 for x in rows)},indent=2))

if __name__=='__main__':main()

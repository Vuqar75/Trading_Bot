"""Spot study diagnostics; uses the existing archived OHLC model, never account APIs."""
import argparse,collections,datetime as dt,hashlib,json,math
from pathlib import Path
from research.strategy_study import PERIOD,STEP,SYMBOLS,load_symbol,signals

def day_regimes(rows):
    """Last completed UTC day only: >=4% daily move = volatile; else +/-2% vs 30d mean = trend."""
    daily=[];last=None;out=[]
    for i,r in enumerate(rows):
        day=r[0]//86400000
        if day!=last:
            if i:daily.append(rows[i-1][4])
            last=day
        if len(daily)<31:out.append('unknown')
        elif abs(daily[-1]/daily[-2]-1)>=.04:out.append('volatile')
        elif abs(daily[-1]/(sum(daily[-30:])/30)-1)>=.02:out.append('trend')
        else:out.append('sideways')
    return out

def diagnostic(rows,sig,symbol,start,end,bps=5,hold=False):
    ix=[i for i,r in enumerate(rows) if start<=r[0]<end];reg=day_regimes(rows)
    cash=1000.;qty=0.;pos=None;pending=0;turn=fee=impact=0.;bars=0;exposure=0.;reject=collections.Counter();trades=[]
    equity=[];potential=0
    def buy(price,i):
        nonlocal cash,qty,pos,turn,fee,impact,potential
        potential+=1;px=price*(1+bps/10000);q=math.floor((10/px)/STEP[symbol])*STEP[symbol];gross=q*px;comm=gross*.001
        if gross<5:reject['min_notional']+=1;return
        if gross+comm>cash:reject['insufficient_cash']+=1;return
        cash-=gross+comm;qty=q;fee+=comm;impact+=q*(px-price);turn+=gross
        pos={'entry_ref':price,'entry_px':px,'entry_fee':comm,'cost':gross+comm,'qty':q,'regime':reg[i-1] if i else 'unknown','open_i':i,'mae_bound':0.,'mfe_bound':0.}
    def sell(ref,reason,i):
        nonlocal cash,qty,pos,turn,fee,impact
        px=ref*(1-bps/10000);gross=qty*px;comm=gross*.001;cash+=gross-comm;fee+=comm;impact+=qty*(ref-px);turn+=gross
        raw=qty*(ref-pos['entry_ref']);cost=pos['entry_fee']+comm+qty*((pos['entry_px']-pos['entry_ref'])+(ref-px));net=gross-comm-pos['cost']
        if not math.isclose(raw-cost,net,abs_tol=1e-9):raise AssertionError('Trade cost reconciliation')
        trades.append({**pos,'gross':raw,'cost':cost,'net':net,'reason':reason,'bars':i-pos['open_i']+1,'closed_i':i,'end_valuation':reason=='period_end'})
        qty=0.;pos=None
    for j,i in enumerate(ix):
        t,o,h,l,c=rows[i]
        if hold and j==0:buy(o,i)
        elif pending<0 and qty:sell(o,'signal',i)
        elif pending>0 and not qty:buy(o,i)
        if qty:
            pos['mae_bound']=min(pos['mae_bound'],l/pos['entry_px']-1)
            pos['mfe_bound']=max(pos['mfe_bound'],h/pos['entry_px']-1)
            if not hold:
                stop=pos['entry_px']*.98;take=pos['entry_px']*1.04
                if o<=stop:sell(o,'stop_gap',i)
                elif o>=take:sell(o,'take_gap',i)
                elif l<=stop and h>=take:sell(stop,'ambiguous_stop_first',i)
                elif l<=stop:sell(stop,'stop',i)
                elif h>=take:sell(take,'take',i)
        if qty:bars+=1;exposure+=qty*c
        equity.append(cash+qty*c*(1-bps/10000)*.999)
        pending=sig[i] if not hold else 0
    open_pnl=0.;open_notional=0.
    if qty:
        mark=rows[ix[-1]][4];open_notional=qty*mark;open_pnl=qty*mark*(1-bps/10000)*.999-pos['cost'];sell(mark,'period_end',ix[-1])
    gross=sum(x['gross'] for x in trades);net=cash-1000
    if not math.isclose(gross-fee-impact,net,abs_tol=1e-7):raise AssertionError('Period cost reconciliation')
    eq=[1000.]+equity;peak=1000.;dd=0
    for value in eq:peak=max(peak,value);dd=max(dd,(peak-value)/peak)
    return {'net':net,'return_account_pct':net/10,'gross':gross,'fees':fee,'spread_slippage':impact,'costs':fee+impact,'turnover':turn,'exposure_pct':bars/len(ix)*100,'mean_exposure_usdt':exposure/len(ix),'trade_count':len(trades),'entries_attempted':potential,'rejected':dict(reject),'open_end_pnl':open_pnl,'open_end_notional':open_notional,'max_dd_pct':dd*100,'trades':trades}

def verify_archives(root):
    manifest=json.loads(Path('research/strategy_manifest.json').read_text(encoding='utf8'))
    for x in manifest['files']:
        p=root/x['file']
        if hashlib.sha256(p.read_bytes()).hexdigest()!=x['sha256']:raise ValueError('Archive changed: '+x['file'])

def main():
    p=argparse.ArgumentParser();p.add_argument('--data',required=True);p.add_argument('--output',required=True);a=p.parse_args();root=Path(a.data);verify_archives(root);out={}
    for s in SYMBOLS:
        rows=load_symbol(s,root);ss,_=signals(rows)
        for year in (2024,2025):
            start=int(dt.datetime(year,1,1,tzinfo=dt.timezone.utc).timestamp()*1000);end=int(dt.datetime(year+1,1,1,tzinfo=dt.timezone.utc).timestamp()*1000)
            for kind in ('current','trend','breakout','reversion','hold','none'):
                sig=ss[kind];out[f'{s}|{year}|{kind}']=diagnostic(rows,sig,s,start,end,hold=kind=='hold')
        print('diagnosed',s,flush=True)
    Path(a.output).write_text(json.dumps(out),encoding='utf8')
if __name__=='__main__':main()

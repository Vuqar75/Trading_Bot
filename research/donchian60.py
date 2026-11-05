"""Offline one-rule Donchian60, yearly mark-to-liquidation, no actual orders."""
import argparse,datetime as dt,json,math
from pathlib import Path
from research.donchian_data import DAY,aggregate,earlier,existing,ms
from research.strategy_study import STEP,SYMBOLS,signals

class Book:
    def __init__(self,symbol,bps):
        self.s=symbol;self.bps=bps;self.cash=1000.;self.pos=None;self.trades=[];self.events=[];self.marks=[];self.levels=[];self.rejected=0;self.pending=0;self.level=None;self.new_level=None
    def buy(self,t,price,level=None):
        fill=price*(1+self.bps/10000);qty=math.floor((10/fill)/STEP[self.s])*STEP[self.s];gross=qty*fill;fee=gross*.001
        if gross<5 or gross+fee>self.cash:self.rejected+=1;return False
        self.cash-=gross+fee;self.pos={'entry_ms':t,'ref':price,'fill':fill,'qty':qty,'cost':gross+fee,'entry_fee':fee,'peak':gross+fee};self.level=level
        self.events.append({'t':t,'fee':fee,'impact':qty*(fill-price),'turnover':gross,'type':'BUY'});return True
    def sell(self,t,price):
        p=self.pos;fill=price*(1-self.bps/10000);gross=p['qty']*fill;fee=gross*.001;cash=gross-fee;self.cash+=cash
        self.events.append({'t':t,'fee':fee,'impact':p['qty']*(price-fill),'turnover':gross,'type':'SELL'})
        self.trades.append({'entry_ms':p['entry_ms'],'exit_ms':t,'entry_ref':p['ref'],'exit_ref':price,'qty':p['qty'],'entry_notional':p['qty']*p['ref'],'gross':p['qty']*(price-p['ref']),'net':cash-p['cost'],'duration_days':(t-p['entry_ms'])/DAY})
        self.pos=None;self.level=None
    def mark(self,end,close):
        p=self.pos
        if p:
            raw=p['qty']*close;liquid=raw*(1-self.bps/10000)*.999;u=liquid-p['cost'];g=raw-p['qty']*p['ref'];p['peak']=max(p['peak'],liquid)
            adverse=max(0.,(p['cost']-liquid)/p['cost']);posdd=max(0.,(p['peak']-liquid)/p['peak'])
        else:raw=liquid=u=g=adverse=posdd=0.
        self.marks.append({'end':end,'equity':self.cash+liquid,'raw_equity':self.cash+raw,'unrealised_net':u,'unrealised_gross':g,'position_mark':raw,'exposed':bool(p),'adverse':adverse,'posdd':posdd})

def run_daily(rows,symbol,start,end,kind='donchian',bps=5):
    b=Book(symbol,bps);closes=[r[4] for r in rows]
    for i,(t,o,h,l,c) in enumerate(rows):
        if not start<=t<end:continue
        if kind=='hold' and t==start:b.buy(t,o)
        elif b.pending==-1 and b.pos:b.sell(t,o)
        elif b.pending==1 and not b.pos:b.buy(t,o,b.new_level)
        b.pending=0;b.new_level=None
        if kind=='donchian' and i>=60:
            previous=closes[i-60:i];current=closes[i-59:i+1];mid=(max(current)+min(current))/2
            if b.pos:
                previous_level=b.level
                if c<b.level:b.pending=-1
                else:b.level=max(b.level,mid)
                b.levels.append((t,previous_level,b.level,b.pending))
            elif c>max(previous):b.pending=1;b.new_level=mid
        b.mark(t+DAY,c)
    return b

def run_sma(rows,symbol,start,end,bps=5):
    b=Book(symbol,bps);sigs,_=signals(rows);pending=0
    for i,(t,o,h,l,c) in enumerate(rows):
        if not start<=t<end:continue
        if pending<0 and b.pos:b.sell(t,o)
        elif pending>0 and not b.pos:b.buy(t,o)
        if b.pos:
            stop=b.pos['fill']*.98;take=b.pos['fill']*1.04
            if o<=stop:b.sell(t,o)
            elif o>=take:b.sell(t,o)
            elif l<=stop:b.sell(t,stop)
            elif h>=take:b.sell(t,take)
        pending=sigs['current'][i]
        if (t+900000)%DAY==0:b.mark(t+900000,c)
    return b

def summarise(b,start,end):
    marks=[m for m in b.marks if start<m['end']<=end]
    if not marks:raise ValueError('Missing period marks')
    before=next((m for m in reversed(b.marks) if m['end']<=start),None)
    initial=before['equity'] if before else 1000.;initial_u=before['unrealised_net'] if before else 0.;initial_g=before['unrealised_gross'] if before else 0.
    trades=[x for x in b.trades if start<=x['exit_ms']<end];events=[x for x in b.events if start<=x['t']<end]
    realised=sum(x['net'] for x in trades);g=sum(x['gross'] for x in trades)+marks[-1]['unrealised_gross']-initial_g;net=marks[-1]['equity']-initial
    if not math.isclose(net,realised+marks[-1]['unrealised_net']-initial_u,abs_tol=1e-7):raise ValueError('Year equity reconciliation')
    peak=initial;dd=0.
    for x in marks:peak=max(peak,x['equity']);dd=max(dd,(peak-x['equity'])/peak)
    n=len(trades);best=max((x['net'] for x in trades),default=None);worst=min((x['net'] for x in trades),default=None)
    return {'net':net,'account_return_pct':net/initial*100,'gross':g,'realised':realised,'unrealised_end':marks[-1]['unrealised_net'],'unrealised_start':initial_u,'raw_equity_end':marks[-1]['raw_equity'],'liquidation_equity_end':marks[-1]['equity'],'turnover':sum(x['turnover'] for x in events),'fees':sum(x['fee'] for x in events),'impact':sum(x['impact'] for x in events),'closed':n,'buys':sum(x['type']=='BUY' for x in events),'rejected_total_run':b.rejected,'avg_net':realised/n if n else None,'avg_bps':sum(x['net']/x['entry_notional']*10000 for x in trades)/n if n else None,'avg_holding_days':sum(x['duration_days'] for x in trades)/n if n else None,'exposure_pct':sum(x['exposed'] for x in marks)/len(marks)*100,'mean_exposure':sum(x['position_mark'] for x in marks)/len(marks),'account_dd_pct':dd*100,'position_adverse_pct':max(x['adverse'] for x in marks)*100,'position_peak_dd_pct':max(x['posdd'] for x in marks)*100,'best':best,'worst':worst,'without_best':net-best if best is not None else None,'open_end':bool(marks[-1]['exposed'])}

def main():
    p=argparse.ArgumentParser();p.add_argument('--data',required=True);p.add_argument('--early',required=True);p.add_argument('--early-manifest',required=True);p.add_argument('--output',required=True);a=p.parse_args()
    old,manifest=earlier(a.early,a.early_manifest);out={}
    for s in SYMBOLS:
        candles=existing(s,a.data);days=aggregate(candles)
        for bps in (5,10):
            for kind in ('donchian','hold','none','sma'):
                book=run_sma(candles,s,ms(2024),ms(2026,11),bps) if kind=='sma' else run_daily(days,s,ms(2024),ms(2026,11),kind,bps)
                for y in (2024,2025,2026):out[f'{s}|{y}|{kind}|{bps}']=summarise(book,ms(y),ms(y+1) if y<2026 else ms(2026,11))
                out[f'{s}|all|{kind}|{bps}']=summarise(book,ms(2024),ms(2026,11))
            for kind in ('donchian','hold','none'):
                book=run_daily(old[s],s,ms(2021),ms(2023),kind,bps)
                out[f'{s}|2021-22|{kind}|{bps}']=summarise(book,ms(2021),ms(2023))
        print('completed',s,flush=True)
    Path(a.output).write_text(json.dumps({'early_manifest':manifest,'results':out},indent=2))
if __name__=='__main__':main()

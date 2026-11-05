"""Count masked signals without rerunning or retuning the independent backtest."""
import argparse,datetime as dt,hashlib,json
from pathlib import Path
from research.spot_diagnosis import verify_archives
from research.spot_hypothesis import bull_filter,load_2026
from research.strategy_study import SYMBOLS,load_symbol,signals

def main():
    p=argparse.ArgumentParser();p.add_argument('--data',required=True);p.add_argument('--output',required=True);a=p.parse_args();root=Path(a.data)
    verify_archives(root)
    manifest=json.loads(Path('research/spot_independent_manifest_2026.json').read_text())
    for item in manifest['files']:
        if hashlib.sha256((root/item['file']).read_bytes()).hexdigest()!=item['sha256']:raise ValueError('Independent archive version changed: '+item['file'])
    result={}
    for s in SYMBOLS:
        rows=load_symbol(s,root)+load_2026(s,root);sig,_=signals(rows);bull=bull_filter(rows)
        for year in (2024,2025,2026):
            start=int(dt.datetime(year,1,1,tzinfo=dt.timezone.utc).timestamp()*1000);end=int(dt.datetime(year+1,1,1,tzinfo=dt.timezone.utc).timestamp()*1000) if year<2026 else int(dt.datetime(2026,11,1,tzinfo=dt.timezone.utc).timestamp()*1000)
            result[f'{s}|{year}']=sum(1 for i,x in enumerate(sig['current']) if x==1 and not bull[i] and start<=rows[i][0]<end)
    Path(a.output).write_text(json.dumps(result),encoding='utf8')
if __name__=='__main__':main()

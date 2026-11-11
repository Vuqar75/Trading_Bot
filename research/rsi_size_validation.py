"""Fixed2026 continuation; never modifies production modules on disk."""
from pathlib import Path
import sys
import json
from research import rsi15m_verification as r
from research.size_feasibility import fixed_size


def configure():
    c=r.core
    c.START,c.BEGIN,c.END=r.START,r.BEGIN,r.END
    c.N=(r.END-r.START)//r.M
    c.SEGMENTS=[('Q'+str(q),c.stamp(f'2026-{3*q-2:02d}-01'),
                 min(c.stamp(f'2026-{3*q+1:02d}-01'),r.END) if q<4 else r.END)
                for q in range(1,5)]


def main(root,out):
    r.START=r.core.stamp('2025-12-01')
    r.BEGIN=r.core.stamp('2026-01-01')
    r.END=r.core.stamp('2026-11-01')
    r.FOMC={20260128,20260318,20260429,20260617}
    r.configure_period=configure
    r.core.size_for=fixed_size
    r.main(root,out)
    p=out/'summary.json';s=json.loads(p.read_text())
    s['protocol']='RSI_SIZE_VALIDATION_PROTOCOL.md'
    s['context']={'start':'2026-01-01','end_exclusive':'2026-11-01','fixed_qty_btc':.001,'notional_cap':150}
    p.write_text(json.dumps(s,indent=2)+'\n')

if __name__=='__main__':main(Path(sys.argv[1]),Path(sys.argv[2]))

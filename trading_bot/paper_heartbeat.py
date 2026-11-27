"""Independent liveness and read-only bracket protection telemetry."""
import json
import subprocess
import time
import urllib.request
from datetime import datetime,timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path

PROJECT='gen-lang-client-0854183882'
METRIC='custom.googleapis.com/tradingbot/paper_vm_heartbeat'
BRACKET_METRIC='custom.googleapis.com/tradingbot/paper_bracket_protection'
METADATA='http://metadata.google.internal/computeMetadata/v1/instance/service-accounts/default/token'


def bracket_health(path=Path('/var/lib/ai-bracket-public/orders.json'), *, clock=time.time, active=None):
    """0 healthy/flat, 1 stopped with exposure, 2 stale under exposure, 3 unknown."""
    now=int(clock()*1000)
    def fresh(stamp, limit):
        return type(stamp) is int and 0<=now-stamp<=limit
    try:
        with path.open('rb') as stream:
            raw=stream.read(1_000_001)
        if len(raw)>1_000_000:
            return 3
        state=json.loads(raw)
        if (state['paper_only'] is not True or state['ledger_balanced'] is not True
                or not isinstance(state['plans'],list)):
            return 3
        exposed=set()
        for plan in state['plans']:
            qty=Decimal(plan['open_qty'])
            if not qty.is_finite() or qty<0 or qty and not isinstance(plan['symbol'],str):
                return 3
            if qty:
                exposed.add(plan['symbol'])
        margin=Decimal(state['balances']['margin'])
        if not margin.is_finite() or margin<0 or bool(margin)!=bool(exposed):
            return 3
        if not exposed:
            return 0 if fresh(state['as_of_ms'],15000) else 3
        if active is None:
            check=subprocess.run(['systemctl','show','-p','ActiveState','--value','ai-bracket-paper.service'],
                                 capture_output=True,text=True,timeout=3,check=True)
            active=check.stdout.strip()=='active'
        if not active:
            return 1
        if not fresh(state['as_of_ms'],15000):
            return 2
        for symbol in exposed:
            stream=state['quote_feeds'].get(symbol,{})
            components=stream.get('components',{})
            if (stream.get('time_rejections')!=[] or
                    not all(stream.get('channels',{}).get(k,{}).get('connected') is True for k in ('book','market')) or
                    not fresh(state['quote_asof_ms'].get(symbol),5000) or
                    not all(fresh(components.get(k,{}).get(field),5000)
                            for k in ('book','mark') for field in ('source_ms','event_ms','received_ms'))):
                return 2
        return 0
    except (OSError, ValueError, TypeError, KeyError, AttributeError, InvalidOperation, subprocess.SubprocessError):
        return 3


def send(*,clock=time.time,opener=None,protection=None,scope='production',liveness=True):
    try:
        if opener is None:opener=urllib.request.build_opener(urllib.request.ProxyHandler({})).open
        request=urllib.request.Request(METADATA,headers={'Metadata-Flavor':'Google'})
        with opener(request,timeout=10) as response:credential=json.load(response)['access_token']
        if not isinstance(credential,str) or not credential:raise ValueError('Missing credential')
        stamp=datetime.fromtimestamp(clock(),timezone.utc).isoformat().replace('+00:00','Z')
        series=[]
        if liveness:
            series.append(({'type':METRIC},1))
        if protection is not None:
            series.append(({'type':BRACKET_METRIC,'labels':{'scope':scope}},protection))
        # Publish liveness separately so a rejected protection sample cannot
        # suppress the existing missing-VM check.
        for metric,value in series:
            body={'timeSeries':[{'metric':metric,'resource':{'type':'global','labels':{'project_id':PROJECT}},
                                'points':[{'interval':{'endTime':stamp},'value':{'int64Value':str(value)}}]}]}
            request=urllib.request.Request('https://monitoring.googleapis.com/v3/projects/'+PROJECT+'/timeSeries',
                data=json.dumps(body,separators=(',',':')).encode(),
                headers={'Authorization':'Bearer '+credential,'Content-Type':'application/json'},method='POST')
            with opener(request,timeout=15) as response:
                if not 200<=response.status<300:raise ValueError('Heartbeat rejected')
        return True
    except Exception:
        # Provider exceptions can contain auth headers, URLs or response bodies.
        raise RuntimeError('Paper heartbeat unavailable') from None

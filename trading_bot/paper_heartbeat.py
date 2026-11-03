"""VM-liveness heartbeat. This does not assert trading or protection health."""
import json
import time
import urllib.request
from datetime import datetime,timezone

PROJECT='gen-lang-client-0854183882'
METRIC='custom.googleapis.com/tradingbot/paper_vm_heartbeat'
METADATA='http://metadata.google.internal/computeMetadata/v1/instance/service-accounts/default/token'


def send(*,clock=time.time,opener=None):
    try:
        if opener is None:opener=urllib.request.build_opener(urllib.request.ProxyHandler({})).open
        request=urllib.request.Request(METADATA,headers={'Metadata-Flavor':'Google'})
        with opener(request,timeout=10) as response:credential=json.load(response)['access_token']
        if not isinstance(credential,str) or not credential:raise ValueError('Missing credential')
        stamp=datetime.fromtimestamp(clock(),timezone.utc).isoformat().replace('+00:00','Z')
        body={'timeSeries':[{'metric':{'type':METRIC},'resource':{'type':'global','labels':{'project_id':PROJECT}},
                             'points':[{'interval':{'endTime':stamp},'value':{'int64Value':'1'}}]}]}
        request=urllib.request.Request('https://monitoring.googleapis.com/v3/projects/'+PROJECT+'/timeSeries',
            data=json.dumps(body,separators=(',',':')).encode(),
            headers={'Authorization':'Bearer '+credential,'Content-Type':'application/json'},method='POST')
        with opener(request,timeout=15) as response:
            if not 200<=response.status<300:raise ValueError('Heartbeat rejected')
        return True
    except Exception:
        # Provider exceptions can contain auth headers, URLs or response bodies.
        raise RuntimeError('Paper heartbeat unavailable') from None

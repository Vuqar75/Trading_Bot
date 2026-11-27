import io
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from trading_bot.paper_heartbeat import bracket_health,send,BRACKET_METRIC,METRIC,PROJECT


class Response(io.BytesIO):
    def __init__(self,content,status=200):super().__init__(content);self.status=status


class HeartbeatTests(unittest.TestCase):
    def test_stopped_stale_unknown_and_recovery_without_writable_state(self):
        now=10000
        stamp=now*1000
        tick=dict(source_ms=stamp,event_ms=stamp,received_ms=stamp)
        state=dict(paper_only=True,ledger_balanced=True,as_of_ms=stamp,
                   plans=[dict(open_qty='0.001',symbol='BTCUSDT')],balances=dict(margin='60'),
                   quote_asof_ms={'BTCUSDT':stamp},
                   quote_feeds={'BTCUSDT':dict(time_rejections=[],
                       channels={key:{'connected':True} for key in ('book','market')},
                       components=dict(book=dict(tick),mark=dict(tick)))})
        with TemporaryDirectory() as folder:
            path=Path(folder)/'orders.json'
            check=lambda active=True:bracket_health(path,clock=lambda:now,active=active)
            def publish():
                path.write_text(json.dumps(state))
            self.assertEqual(check(),3)
            publish()
            self.assertEqual(check(),0)
            self.assertEqual(check(False),1)
            state['quote_feeds']['BTCUSDT']['components']['book']['source_ms']-=5001
            publish()
            self.assertEqual(check(),2)
            state['quote_feeds']['BTCUSDT']['components']['book']=tick
            state['as_of_ms']-=15001
            publish()
            self.assertEqual(check(),2)
            state['as_of_ms']=stamp
            publish()
            path.chmod(0o444)
            try:self.assertEqual(check(),0)
            finally:path.chmod(0o600)

    def test_protection_uses_same_channel_without_mixing_vm_and_probe(self):
        calls=[]
        def opener(req,timeout):
            calls.append(req)
            return Response(b'{"access_token":"secret"}' if len(calls)==1 else b'{}')
        send(clock=lambda:10000,opener=opener,protection=3)
        payload=[json.loads(req.data)['timeSeries'][0] for req in calls[1:]]
        self.assertEqual([p['metric']['type'] for p in payload],[METRIC,BRACKET_METRIC])
        self.assertEqual(payload[1]['metric']['labels'],{'scope':'production'})
        self.assertEqual(payload[1]['points'][0]['value'],{'int64Value':'3'})
    def test_global_liveness_not_trading_health(self):
        calls=[]
        def opener(req,timeout):
            calls.append(req)
            return Response(b'{"access_token":"secret-fixture"}') if len(calls)==1 else Response(b'{}')
        self.assertTrue(send(clock=lambda:10000,opener=opener));self.assertEqual(len(calls),2)
        payload=json.loads(calls[1].data)['timeSeries'][0]
        self.assertEqual(payload['metric']['type'],METRIC);self.assertEqual(payload['resource'],{'type':'global','labels':{'project_id':PROJECT}})
        self.assertNotIn('health',json.dumps(payload));self.assertNotIn('secret-fixture',json.dumps(payload))

    def test_transport_failure_does_not_disclose_token_or_payload(self):
        def opener(req,timeout):raise ValueError('secret-fixture https://private')
        with self.assertRaisesRegex(RuntimeError,'Paper heartbeat unavailable') as caught:send(opener=opener)
        self.assertNotIn('secret-fixture',str(caught.exception));self.assertIsNone(caught.exception.__cause__)


if __name__=='__main__':unittest.main()

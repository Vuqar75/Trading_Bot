import io
import json
import unittest
from trading_bot.paper_heartbeat import send,METRIC,PROJECT


class Response(io.BytesIO):
    def __init__(self,content,status=200):super().__init__(content);self.status=status


class HeartbeatTests(unittest.TestCase):
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

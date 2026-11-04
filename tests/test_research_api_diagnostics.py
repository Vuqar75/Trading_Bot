import contextlib
import io
import json
import os
import sqlite3
import ssl
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch
from urllib.error import HTTPError, URLError

from tools import interactive_market_diagnostics as diagnostic
from trading_bot import research_api_budget as api
from trading_bot.market_research import ResearchError, build_snapshot
from tests.test_market_research_observer import fixture


class ResearchHTTPDiagnosticsTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.db=Path(self.tmp.name)/"budget.db"

    def error(self, status, code):
        body=json.dumps({"error":{"code":code,"message":"secret-error-body"}}).encode()
        return HTTPError("https://private.invalid/secret",status,"secret-http-reason",{},io.BytesIO(body))

    def test_http_status_and_known_code_survive_but_body_does_not(self):
        for status, code in ((401,"invalid_api_key"),(403,"permission_denied"),(404,"model_not_found"),(429,"insufficient_quota"),(400,"unsupported_parameter"),(500,"server_error")):
            with self.subTest(status=status):
                opener=Mock();opener.open.side_effect=self.error(status,code)
                with patch.object(api,"build_opener",return_value=opener),patch.dict(os.environ,{"TRADING_BOT_AI":"unit-test-key"},clear=True):
                    with self.assertRaises(api.ResearchAPIError) as caught:api._send({"model":api.MODEL})
                self.assertEqual(caught.exception.safe_details["http_status"],status)
                self.assertEqual(caught.exception.safe_details["provider_code"],code)
                self.assertNotIn("secret",str(caught.exception))
                self.assertNotIn("unit-test-key",json.dumps(caught.exception.safe_details))
                self.assertEqual(opener.open.call_count,1)

    def test_unknown_provider_code_is_not_copied_from_untrusted_body(self):
        opener=Mock();opener.open.side_effect=self.error(400,"secret-not-an-allowed-code")
        with patch.object(api,"build_opener",return_value=opener),patch.dict(os.environ,{"TRADING_BOT_AI":"unit-test-key"},clear=True):
            with self.assertRaises(api.ResearchAPIError) as caught:api._send({})
        self.assertIsNone(caught.exception.safe_details["provider_code"])
        self.assertNotIn("secret",json.dumps(caught.exception.safe_details))

    def test_http_error_stream_is_closed_even_if_body_is_malformed(self):
        stream=io.BytesIO(b'not-json')
        error=HTTPError('https://private.invalid/secret',403,'secret-http-reason',{},stream)
        opener=Mock();opener.open.side_effect=error
        with patch.object(api,'build_opener',return_value=opener),patch.dict(os.environ,{'TRADING_BOT_AI':'unit-test-key'},clear=True):
            with self.assertRaises(api.ResearchAPIError) as caught:api.check_model_access()
        self.assertTrue(stream.closed)
        self.assertEqual(caught.exception.safe_details['http_status'],403)
        self.assertNotIn('secret',str(caught.exception))
        self.assertEqual(opener.open.call_count,1)

    def test_timeout_tls_and_network_are_distinguished_without_retry(self):
        for exc,category in ((TimeoutError("secret"),"timeout"),(URLError(TimeoutError("secret")),"timeout"),(URLError(ssl.SSLError("secret")),"tls_error"),(URLError("secret-host"),"network_error")):
            with self.subTest(category=category):
                opener=Mock();opener.open.side_effect=exc
                with patch.object(api,"build_opener",return_value=opener),patch.dict(os.environ,{"TRADING_BOT_AI":"unit-test-key"},clear=True):
                    with self.assertRaises(api.ResearchAPIError) as caught:api._send({})
                self.assertEqual(caught.exception.safe_details["category"],category)
                self.assertEqual(opener.open.call_count,1)
                self.assertNotIn("secret",str(caught.exception))

    def test_safe_error_is_durable_and_reserved_attempt_is_not_erased(self):
        snap=build_snapshot(fixture())
        def fail(_):raise api.ResearchAPIError("http_error",401,"invalid_api_key")
        with self.assertRaises(api.ResearchAPIError):api.paid_analysis(snap,self.db,enabled=True,transport=fail)
        with sqlite3.connect(self.db) as db:
            count,reserve=db.execute("SELECT COUNT(*),SUM(reserved_micro_usd) FROM api_attempts").fetchone()
            self.assertEqual(count,1);self.assertGreater(reserve,0)
            details=json.loads(db.execute("SELECT details_json FROM api_diagnostics").fetchone()[0])
            self.assertEqual(details["http_status"],401)
        with self.assertRaisesRegex(ResearchError,"already_attempted"):
            api.paid_analysis(snap,self.db,enabled=True,transport=lambda _:self.fail("retried"))

    def test_model_metadata_uses_get_and_does_not_prove_responses_write(self):
        requests=[]
        def metadata(req):requests.append(req);return {"id":api.MODEL}
        with patch.object(api,"_request_json",side_effect=metadata),patch.dict(os.environ,{"TRADING_BOT_AI":"unit-test-key"},clear=True):
            result=api.check_model_access()
        self.assertEqual(len(requests),1)
        self.assertEqual(requests[0].get_method(),"GET")
        self.assertEqual(requests[0].full_url,"https://api.openai.com/v1/models/"+api.MODEL)
        self.assertIsNone(requests[0].data)
        self.assertTrue(result["model_access_confirmed"])
        self.assertFalse(result["responses_write_confirmed"])

    def test_old_four_column_budget_is_preserved_when_diagnostics_table_added(self):
        with sqlite3.connect(self.db) as db:
            db.execute(f"PRAGMA application_id={api.APP_ID}")
            db.execute("CREATE TABLE api_attempts (request_id TEXT PRIMARY KEY,reserved_micro_usd INTEGER,created_at REAL,result_json TEXT)")
            db.execute("INSERT INTO api_attempts VALUES ('historic',3630,1,NULL)")
        db=api._connect(self.db)
        try:self.assertEqual(db.execute("SELECT * FROM api_attempts").fetchone(),('historic',3630,1,None))
        finally:db.close()


class InteractiveDiagnosticsTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.home=Path(self.tmp.name);self.state=self.home/".local/state/tradingbot-ai"
        self.db=self.state/"experiment-budget.db"
        db=api._connect(self.db)
        db.execute("INSERT INTO api_attempts VALUES ('prior',3630,1,NULL)");db.close()
        self.gate=self.state/"interactive-once.lock";self.gate.write_text("prior-snapshot\n")

    def test_diagnostic_keeps_previous_attempt_gate_and_secret_private(self):
        before=self.db.read_bytes();gate_before=self.gate.read_bytes()
        output=io.StringIO()
        with patch.object(diagnostic.sys.stdin,"isatty",return_value=True),patch.object(diagnostic.getpass,"getpass",return_value="unit-test-private-key"),patch.object(diagnostic,"check_model_access",return_value={"model_access_confirmed":True,"inference_requests":0}),patch.dict(os.environ,{"OPENAI_API_KEY":"previous-value"}),contextlib.redirect_stdout(output):
            result=diagnostic.run(self.home)
            self.assertEqual(os.environ["OPENAI_API_KEY"],"previous-value")
        self.assertEqual(self.db.read_bytes(),before);self.assertEqual(self.gate.read_bytes(),gate_before)
        self.assertEqual(result["budget"]["attempts"],1)
        self.assertEqual(result["budget"]["reserved_micro_usd"],3630)
        self.assertNotIn("unit-test-private-key",output.getvalue())
        self.assertNotIn("unit-test-private-key",(self.state/"model-access-diagnostic.json").read_text())

    def test_safe_diagnostic_error_saved_without_changing_budget(self):
        before=self.db.read_bytes()
        with patch.object(diagnostic.sys.stdin,"isatty",return_value=True),patch.object(diagnostic.getpass,"getpass",return_value="unit-test-private-key"),patch.object(diagnostic,"check_model_access",side_effect=api.ResearchAPIError("http_error",404,"model_not_found")),contextlib.redirect_stdout(io.StringIO()):
            result=diagnostic.run(self.home)
        self.assertEqual(result["diagnostic"]["http_status"],404)
        self.assertFalse(result["model_access_confirmed"])
        self.assertEqual(self.db.read_bytes(),before)

    def test_no_tty_refuses_without_prompt_or_network(self):
        with patch.object(diagnostic.sys.stdin,"isatty",return_value=False),patch.object(diagnostic.getpass,"getpass") as entry,patch.object(diagnostic,"check_model_access") as network:
            with self.assertRaisesRegex(ResearchError,"interactive_terminal"):diagnostic.run(self.home)
        entry.assert_not_called();network.assert_not_called()

    def test_echo_fallback_refuses_without_network(self):
        with patch.object(diagnostic.sys.stdin,"isatty",return_value=True),patch.object(diagnostic.getpass,"getpass",side_effect=diagnostic.getpass.GetPassWarning("unavailable")),patch.object(diagnostic,"check_model_access") as network:
            with self.assertRaises(diagnostic.getpass.GetPassWarning):diagnostic.run(self.home)
        network.assert_not_called()


if __name__ == "__main__":unittest.main()

"""A rejected or unknown model cannot self-promote, including legacy views."""
import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import numpy as np

from research import technical_patterns as tech
from research.pattern_model_qualification import PATTERN_MODEL_ID, qualification
from tests import test_technical_patterns_ai as ai_fixture
from tests import test_technical_plan_history as history_fixture


class QualificationTests(unittest.TestCase):
    def test_high_scores_do_not_admit_failed_model(self):
        bundle = ai_fixture.PatternAIContract().bundle()
        bundle['model_id'] = PATTERN_MODEL_ID
        bundle['admitted'] = True
        bundle['qualification'] = {'candidate_allowed': True}
        result = tech.infer(bundle, np.zeros(34), symbol='BTCUSDT', signal_ms=2000, now_ms=2001)
        self.assertEqual(result['p_win_estimate'], .7)
        self.assertEqual(result['expected_stress_net_return'], .001)
        self.assertEqual(result['threshold_action'], 'CANDIDATE')
        self.assertEqual(result['action'], 'NO_TRADE')
        self.assertFalse(result['trade_allowed'])
        self.assertEqual(result['qualification']['status'], 'failed_economic_validation')

    def test_new_identity_is_not_automatic_qualification(self):
        for model_id in ('new-model', '', None, ['untrusted']):
            self.assertEqual(qualification(model_id)['status'], 'not_evaluated')
            self.assertFalse(qualification(model_id)['candidate_allowed'])
        result = qualification(PATTERN_MODEL_ID)
        result['candidate_allowed'] = True
        self.assertFalse(qualification(PATTERN_MODEL_ID)['candidate_allowed'])

    def test_legacy_positive_snapshot_is_not_rendered_as_recommendation(self):
        packet = ai_fixture.TechnicalTelegramPanel().packet()
        packet['ml'].update(model_id=PATTERN_MODEL_ID, action='CANDIDATE', p_win_estimate=.99, expected_stress_net_return=.01)
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder)/'packet.json'
            path.write_text(json.dumps(packet))
            with patch('urllib.request.urlopen', side_effect=AssertionError('network')):
                from trading_bot.technical_ai_view import render
                text = render(path, now=902)
        self.assertIn('не прошёл проверку', text)
        self.assertIn('рекомендации сделки отключены', text)
        self.assertNotIn('исследовательский кандидат', text)
        self.assertIn('это не подтверждённая вероятность', text)
        self.assertIn('бычий пинбар', text)

    def test_history_retains_status_without_mutating_prior_records(self):
        fixture = history_fixture.PlanHistoryTest()
        from research.technical_plan_history import advance, BAR
        old = fixture.start()
        prior = copy.deepcopy(old['records'][0])
        packet = fixture.packet(signal=2*BAR)
        packet['ml'].update(qualification=qualification(PATTERN_MODEL_ID), threshold_action='CANDIDATE')
        current = advance(old, packet, fixture.capture(2*BAR), 2*BAR+120000)
        retained = current['records'][-1]['packet']['ml']
        self.assertEqual(retained['threshold_action'], 'CANDIDATE')
        self.assertFalse(retained['qualification']['candidate_allowed'])
        self.assertEqual(current['records'][0]['id'], prior['id'])
        self.assertEqual(current['records'][0]['packet'], prior['packet'])
        packet['ml']['qualification']['candidate_allowed'] = True
        self.assertFalse(retained['qualification']['candidate_allowed'])

    def test_prompt_uses_registry_not_untrusted_packet_approval(self):
        from research.adaptive_ai_executor import request
        snapshot = ai_fixture.PatternAIContract().enriched_snapshot()
        snapshot['technical_context']['ml'] = {'model_id': PATTERN_MODEL_ID, 'qualification': {'candidate_allowed': True}}
        with patch('urllib.request.urlopen', side_effect=AssertionError('network')):
            content = request(snapshot)['input'][0]['content']
        self.assertIn('has no verified economic qualification', content)
        self.assertIn('do not promote it to a trade recommendation', content)

    def test_qualification_change_is_not_a_market_revision(self):
        fixture = history_fixture.PlanHistoryTest()
        from research.technical_plan_history import advance, BAR
        from research.technical_trade_scenarios import build
        packet = fixture.packet()
        packet['ml'].update(action='CANDIDATE', p_win_estimate=.7, expected_stress_net_return=.001)
        packet['trade_scenarios'] = build(packet)
        state = advance(None, packet, fixture.capture(), BAR+120000)
        old = copy.deepcopy(state)
        packet['ml'].update(action='NO_TRADE', threshold_action='CANDIDATE', qualification=qualification('model-1'))
        packet['trade_scenarios'] = build(packet)
        result = advance(state, packet, fixture.capture(), BAR+120000)
        self.assertEqual(result, old)

    def test_replay_accepts_new_policy_but_detects_forged_qualification(self):
        from tests import test_pattern_runtime_diagnostics as fixture
        from tools.audit_pattern_runtime import packet_audit
        bundle = fixture.RuntimeDiagnosticsTest().bundle()
        packet = fixture.RuntimeDiagnosticsTest().packet(bundle)
        # Force both numeric thresholds above their unchanged cutoffs.
        bundle['classifier'] = ai_fixture.PatternAIContract().bundle()['classifier']
        bundle['regressor'] = ai_fixture.PatternAIContract().bundle()['regressor']
        packet['ml'] = tech.infer(bundle, tech.vector(
            [packet['features'][k] for k in tech.FEATURES[:30]], 1, 'BTCUSDT'),
            symbol='BTCUSDT', signal_ms=900000, now_ms=900001)
        self.assertEqual(packet_audit(bundle, packet)['status'], 'match')
        packet['ml']['qualification']['candidate_allowed'] = True
        self.assertEqual(packet_audit(bundle, packet)['status'], 'prediction_mismatch')


if __name__ == '__main__':
    unittest.main()

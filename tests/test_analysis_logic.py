import ast
import base64
import hashlib
import hmac
import math
import os
import statistics
import time
import unittest
from pathlib import Path
from unittest.mock import patch


def load_logic():
    source = (Path(__file__).parents[1] / 'app.py').read_text()
    tree = ast.parse(source)
    names = {
        'as_float', 'has_chinese', 'chinese_list', 'fallback_analysis',
        'normalize_analysis', 'build_risk_flags', 'validate_decision',
        'clamp', 'normalized_symbol', 'score_discovery_candidate',
        'build_discovery_recommendation',
        'longbridge_signature', 'rates_to_cny', 'aggregate_positions',
        'build_account_snapshot', 'issue_session_token', 'valid_session_token',
    }
    module = ast.Module(
        body=[node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name in names],
        type_ignores=[],
    )
    namespace = {
        'RATINGS': {'Buy', 'Overweight', 'Hold', 'Underweight', 'Sell'},
        'RATING_SCORE': {'Sell': -2, 'Underweight': -1, 'Hold': 0, 'Overweight': 1, 'Buy': 2},
        'EVIDENCE_KEYWORDS': ('价格', '收益', '仓位', '敞口', '集中', '回撤', '成本', '现金', '风险'),
        'math': math,
        'statistics': statistics,
        'hashlib': hashlib,
        'hmac': hmac,
        'base64': base64,
        'os': os,
        'time': time,
        'SECTOR_BY_SYMBOL': {'AAPL':'科技', '0700.HK':'通信服务'},
    }
    exec(compile(module, 'app.py', 'exec'), namespace)
    return namespace


class DecisionAuditTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.logic = load_logic()

    def setUp(self):
        self.body = {
            'symbol': 'TEST', 'price': 100, 'cost': 90, 'return_pct': 11.1, 'sector': '科技',
            'portfolio_context': {'position_weight': 0.1, 'company_weight': 0.1},
        }

    def run_decision(self, raw, body=None):
        body = dict(body or self.body)
        flags = self.logic['build_risk_flags'](body)
        normalized = self.logic['normalize_analysis'](raw, body, flags)
        return self.logic['validate_decision'](normalized, body, flags)

    def test_conflicting_action_is_downgraded(self):
        result = self.run_decision({
            'rating': 'Overweight', 'executive_summary': '建议减仓控制风险。',
            'position_sizing': '建议减仓。',
        })
        self.assertEqual(result['rating'], 'Hold')
        self.assertEqual(result['consistency']['status'], 'adjusted')

    def test_invalid_price_levels_are_reset(self):
        result = self.run_decision({
            'rating': 'Buy', 'executive_summary': '建议分批加仓。', 'position_sizing': '建议加仓。',
            'entry_price': 120, 'stop_loss': 110, 'price_target': 90,
        })
        self.assertEqual((result['entry_price'], result['stop_loss'], result['price_target']), (96, 90, 112))

    def test_unexplained_rating_change_is_downgraded(self):
        body = dict(self.body, previous_decision={'rating': 'Buy', 'price': 99, 'generated_at': '2026-07-31T00:00:00Z'})
        result = self.run_decision({
            'rating': 'Sell', 'executive_summary': '建议卖出。', 'position_sizing': '建议卖出。',
            'change_reason': '趋势改变。', 'new_evidence': [],
        }, body)
        self.assertEqual(result['rating'], 'Hold')
        self.assertTrue(any('变化阈值' in item for item in result['consistency']['violations']))

    def test_valid_new_evidence_allows_change(self):
        body = dict(self.body, previous_decision={'rating': 'Buy', 'price': 99, 'generated_at': '2026-07-31T00:00:00Z'})
        result = self.run_decision({
            'rating': 'Underweight', 'executive_summary': '建议减仓。', 'position_sizing': '建议减仓。',
            'change_reason': '当前回撤扩大。', 'new_evidence': ['当前价格跌破成本且回撤扩大'],
        }, body)
        self.assertEqual(result['rating'], 'Underweight')
        self.assertEqual(result['target_position_pct'], 6.0)
        self.assertEqual(len(result['action_steps']), 3)

    def test_concentration_gate_preserves_original_rating_in_audit(self):
        body = dict(self.body, portfolio_context={'position_weight': 0.19, 'company_weight': 0.21})
        result = self.run_decision({
            'rating': 'Overweight', 'executive_summary': '建议加仓。', 'position_sizing': '建议加仓。',
        }, body)
        self.assertEqual(result['rating'], 'Hold')
        self.assertEqual(result['consistency']['original_rating'], 'Overweight')
        self.assertEqual(result['consistency']['status'], 'adjusted')

    def test_confidence_is_calibrated_by_history(self):
        body = dict(self.body, validation_context={'sample_size': 10, 'directional_hit_rate': 0.6})
        result = self.run_decision({
            'rating': 'Hold', 'confidence': 88, 'executive_summary': '建议持有。',
            'position_sizing': '维持仓位。',
        }, body)
        self.assertEqual(result['confidence'], 60)

    def test_reduction_never_increases_target_position(self):
        body = dict(self.body, portfolio_context={'position_weight': 0.02, 'company_weight': 0.02})
        result = self.run_decision({
            'rating': 'Underweight', 'executive_summary': '建议减仓。', 'position_sizing': '建议减仓。',
        }, body)
        self.assertEqual(result['target_position_pct'], 0.0)
        self.assertIsNone(result['risk_reward_ratio'])

    def test_hk_symbols_are_normalized_for_exclusion(self):
        normalize = self.logic['normalized_symbol']
        self.assertEqual(normalize('700.HK'), '0700.HK')
        self.assertEqual(normalize('META.US'), 'META')

    def test_session_token_is_signed_and_expires(self):
        with patch.dict(os.environ, {'DASHBOARD_SESSION_SECRET':'test-secret'}):
            with patch.object(time, 'time', return_value=1_000):
                token=self.logic['issue_session_token'](60)
                self.assertTrue(self.logic['valid_session_token'](token))
            with patch.object(time, 'time', return_value=1_061):
                self.assertFalse(self.logic['valid_session_token'](token))
            self.assertFalse(self.logic['valid_session_token'](token+'x'))

    def test_discovery_score_rewards_missing_sector(self):
        closes = [100 + index * .35 + math.sin(index / 5) for index in range(252)]
        score = self.logic['score_discovery_candidate']
        missing = score(closes, 0)
        crowded = score(closes, .30)
        self.assertTrue(missing['eligible'])
        self.assertEqual(missing['score'] - crowded['score'], 15)

    def test_discovery_recommendation_has_bounded_weight_and_price_risk(self):
        closes = [80 + index * .25 + math.sin(index / 7) for index in range(252)]
        history = {'dates':[f'day-{index}' for index in range(252)], 'closes':closes, 'as_of':'2026-07-31', 'source':'test'}
        meta = {'symbol':'TEST', 'name':'测试公司', 'ccy':'USD', 'sector':'医疗健康', 'group':'TEST'}
        result = self.logic['build_discovery_recommendation'](meta, history, 0)
        self.assertIsNotNone(result)
        self.assertLess(result['stop_loss'], result['entry_price'])
        self.assertGreater(result['price_target'], result['price'])
        self.assertLessEqual(result['target_position_pct'], 5)
        self.assertEqual(len(result['history']), 120)

    def test_longbridge_signature_matches_official_protocol_fixture(self):
        signature = self.logic['longbridge_signature'](
            'GET', '/v1/asset/stock', '',
            {'authorization':'test_token', 'x-api-key':'test_key', 'x-timestamp':'1700000000000'},
            'test_secret',
        )
        self.assertEqual(signature, '6c26283969179bb29d59ec78c1ce6d8fddd02433efe11f4986a34734273cba7c')

    def test_account_snapshot_merges_channels_and_uses_live_cost(self):
        stock_data = {'list':[
            {'account_channel':'cash', 'stock_info':[{'symbol':'AAPL.US','symbol_name':'Apple','quantity':'2','available_quantity':'2','currency':'USD','cost_price':'100','market':'US'}]},
            {'account_channel':'margin', 'stock_info':[{'symbol':'AAPL.US','symbol_name':'Apple','quantity':'1','available_quantity':'1','currency':'USD','cost_price':'130','market':'US'}]},
        ]}
        positions = self.logic['aggregate_positions'](stock_data)
        self.assertEqual(len(positions), 1)
        self.assertEqual(positions[0]['quantity'], 3)
        self.assertEqual(positions[0]['cost_price'], 110)
        self.assertEqual(positions[0]['account_channels'], ['cash', 'margin'])

    def test_account_snapshot_converts_live_balances_without_defaults(self):
        stock_data = {'list':[{'account_channel':'cash','stock_info':[{'symbol':'AAPL.US','symbol_name':'Apple','quantity':'2','available_quantity':'2','currency':'USD','cost_price':'100','market':'US'}]}]}
        balance_data = {'list':[{'currency':'USD','net_assets':'1000','buy_power':'400','cash_infos':[{'currency':'USD','available_cash':'100'}]}]}
        exchange_data = {'exchanges':[{'base_currency':'CNY','other_currency':'USD','average_rate':7.0}]}
        snapshot = self.logic['build_account_snapshot'](stock_data,balance_data,exchange_data,{'AAPL':120},'2026-08-01T00:00:00Z')
        self.assertTrue(snapshot['complete'])
        self.assertEqual(snapshot['account']['net_assets_cny'],7000)
        self.assertEqual(snapshot['account']['total_cash_cny'],700)
        self.assertEqual(snapshot['positions'][0]['market_value_cny'],1680)
        self.assertEqual(snapshot['account']['account_currency'],'USD')
        self.assertEqual(snapshot['account']['net_assets_native'],1000)

    def test_account_snapshot_keeps_positions_when_optional_source_fails(self):
        stock_data = {'list':[{'account_channel':'cash','stock_info':[{'symbol':'AAPL.US','symbol_name':'Apple','quantity':'3','available_quantity':'3','currency':'USD','cost_price':'150','market':'US'}]}]}
        snapshot = self.logic['build_account_snapshot'](
            stock_data, {'list':[]}, {'exchanges':[]}, {},
            source_errors={'prices':'temporary timeout'},
        )
        self.assertEqual(snapshot['positions'][0]['quantity'],3)
        self.assertEqual(snapshot['positions'][0]['cost_price'],150)
        self.assertFalse(snapshot['complete'])
        self.assertIn('prices',snapshot['source_errors'])


if __name__ == '__main__':
    unittest.main()

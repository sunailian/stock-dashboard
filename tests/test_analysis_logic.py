import ast
import unittest
from pathlib import Path


def load_logic():
    source = (Path(__file__).parents[1] / 'app.py').read_text()
    tree = ast.parse(source)
    names = {
        'as_float', 'has_chinese', 'chinese_list', 'fallback_analysis',
        'normalize_analysis', 'build_risk_flags', 'validate_decision',
    }
    module = ast.Module(
        body=[node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name in names],
        type_ignores=[],
    )
    namespace = {
        'RATINGS': {'Buy', 'Overweight', 'Hold', 'Underweight', 'Sell'},
        'RATING_SCORE': {'Sell': -2, 'Underweight': -1, 'Hold': 0, 'Overweight': 1, 'Buy': 2},
        'EVIDENCE_KEYWORDS': ('价格', '收益', '仓位', '敞口', '集中', '回撤', '成本', '现金', '风险'),
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
        self.assertTrue(any('没有基于' in item for item in result['consistency']['violations']))

    def test_valid_new_evidence_allows_change(self):
        body = dict(self.body, previous_decision={'rating': 'Buy', 'price': 99, 'generated_at': '2026-07-31T00:00:00Z'})
        result = self.run_decision({
            'rating': 'Underweight', 'executive_summary': '建议减仓。', 'position_sizing': '建议减仓。',
            'change_reason': '当前回撤扩大。', 'new_evidence': ['当前价格跌破成本且回撤扩大'],
        }, body)
        self.assertEqual(result['rating'], 'Underweight')

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


if __name__ == '__main__':
    unittest.main()

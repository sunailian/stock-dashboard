import ast
import base64
import calendar
import hashlib
import hmac
import math
import json
import os
import statistics
import time
import unittest
from datetime import date, datetime, timezone
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
        'technical_snapshot',
        'utc_timestamp', 'month_end_dates', 'max_drawdown_from_returns',
        'classify_cash_flow', 'xirr',
        'iso_now', 'investment_policy', 'percentile', 'daily_returns',
        'realized_volatility_series', 'market_regime_from_history',
        'pearson', 'company_group_symbol', 'portfolio_risk_from_histories',
        'deterministic_price_plan', 'factor_analysis_from_history',
        'longbridge_symbol', 'longbridge_counter_id', 'nested_dicts',
        'quote_timestamp', 'latest_quote_point',
        'normalize_longbridge_candlesticks',
        'normalize_valuation_snapshot', 'normalize_financial_snapshot',
        'normalize_eps_forecast', 'normalize_rating_snapshot', 'research_evidence',
        'normalize_order_status', 'normalize_pending_orders', 'parse_event_day',
        'symbol_from_counter', 'normalize_finance_events',
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
        'calendar': calendar,
        'date': date,
        'datetime': datetime,
        'timezone': timezone,
        'SECTOR_BY_SYMBOL': {'AAPL':'科技', '0700.HK':'通信服务'},
        'POLICY_DEFAULT': {
            'version':1, 'base_currency':'CNY', 'annual_return_objective':.2,
            'benchmark_by_market':{'US':'SPY.US','HK':'HSI.HK'},
            'risk':{}, 'limits':{}, 'target_bands':[], 'confirmed_by_user':False, 'updated_at':None,
        },
        'DISCOVERY_ALIASES': {'BABA':'ALIBABA', '9988.HK':'ALIBABA'},
        'ACTIVE_ORDER_STATUSES': {'new','waittonew','partialfilled','waittocancel','pendingreplace'},
        'json': json,
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

    def test_real_history_technical_snapshot_detects_uptrend(self):
        points=[]
        for index in range(250):
            close=100+index*.25
            points.append({'date':str(index),'open':close-.2,'close':close,'high':close+.5,'low':close-.7,'volume':1000+index})
        result=self.logic['technical_snapshot'](points)
        self.assertEqual(result['trend'],'上升')
        self.assertGreater(result['momentum_60d'],0)
        self.assertGreater(result['sma50'],result['sma200'])
        self.assertGreater(result['atr_14d'],0)

    def test_performance_month_ends_and_drawdown_are_not_fabricated(self):
        ends=self.logic['month_end_dates'](date(2026,1,1),date(2026,3,12))
        self.assertEqual(ends,[date(2026,1,31),date(2026,2,28),date(2026,3,12)])
        drawdown=self.logic['max_drawdown_from_returns']([.10,.05,.20,-.10])
        self.assertAlmostEqual(drawdown,-.25)
        self.assertEqual(self.logic['utc_timestamp'](date(1970,1,1)),0)

    def test_external_cash_flow_classification_excludes_trading_activity(self):
        classify=self.logic['classify_cash_flow']
        self.assertEqual(classify('银行入金'),'deposit')
        self.assertEqual(classify('提款出金'),'withdrawal')
        self.assertEqual(classify('现金分红'),'income')
        self.assertEqual(classify('货币兑换入账'),'internal')
        self.assertEqual(classify('新股申购额退回'),'internal')

    def test_xirr_uses_dated_external_cashflows(self):
        result=self.logic['xirr']([(date(2025,1,1),-1000),(date(2026,1,1),1100)])
        self.assertAlmostEqual(result,.10,places=5)
        self.assertIsNone(self.logic['xirr']([(date(2025,1,1),1000)]))

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

    def test_market_regime_missing_optional_signals_stays_auditable(self):
        points=[]
        for index in range(260):
            close=100+index*.2+math.sin(index/8)
            points.append({'date':f'2026-{index:03d}','open':close-.2,'close':close,'high':close+.5,'low':close-.6,'volume':1000})
        history={'points':points,'as_of':'2026-07-31'}
        result=self.logic['market_regime_from_history'](history,'US')
        self.assertEqual(result['data_coverage'],.65)
        self.assertEqual(result['signals']['breadth']['score'],12.5)
        self.assertEqual(result['signals']['sentiment']['score'],5.0)
        self.assertTrue(result['quality']['warnings'])

    def test_factor_shadow_never_influences_decision(self):
        points=[]
        for index in range(250):
            close=80+index*.3
            points.append({'date':str(index),'open':close-.1,'close':close,'high':close+.4,'low':close-.5,'volume':1000})
        history={'points':points,'technical':self.logic['technical_snapshot'](points),'as_of':'2026-07-31','source':'test'}
        result=self.logic['factor_analysis_from_history']('AAPL.US',history)
        self.assertEqual(result['model_status'],'shadow')
        self.assertEqual(result['composite']['decision_weight'],0)
        self.assertEqual(result['composite']['signal'],'neutral')
        self.assertEqual(result['composite']['data_coverage'],.4)
        self.assertTrue(all(item['contribution']==0 for item in result['factors'].values()))

    def test_research_normalizers_and_shadow_coverage(self):
        valuation=self.logic['normalize_valuation_snapshot']({'overview':{'date':'2026-07-31','metrics':{'pe':{'metric':'20','industry_median':'18'}}},'history':{'metrics':{'pe':{'list':[{'value':'10'},{'value':'30'}]}}}})
        financial=self.logic['normalize_financial_snapshot']({'report':'2026.Q2','indicators':[{'field_name':'operating_revenue','indicator_value':'1000','yoy':'12.5%'},{'field_name':'roe','indicator_value':'18%'}]})
        forecast=self.logic['normalize_eps_forecast']({'items':[{'forecast_start_date':'1000000','forecast_eps_mean':'2','institution_total':'10'},{'forecast_start_date':str(1000000+30*86400),'forecast_eps_mean':'2.2','institution_total':'12'}]})
        ratings=self.logic['normalize_rating_snapshot']({'instratings':{'target':'125','recommend':'buy','evaluate':{'strong_buy':'4','buy':'3','hold':'2'}}},{})
        self.assertEqual(valuation['metrics']['pe']['historical_percentile'],50)
        self.assertEqual(financial['indicators']['operating_revenue']['yoy'],.125)
        self.assertEqual(financial['indicators']['roe']['value'],.18)
        self.assertEqual(forecast['revision_30d_pct'],10)
        self.assertEqual(ratings['coverage_count'],9)
        points=[{'date':str(index),'open':100+index*.1,'close':100+index*.1,'high':101+index*.1,'low':99+index*.1,'volume':1000} for index in range(250)]
        research={'valuation':valuation,'financial':financial,'forecast':forecast,'ratings':ratings}
        result=self.logic['factor_analysis_from_history']('NVDA.US',{'points':points,'as_of':'2026-07-31'},research=research)
        self.assertEqual(result['composite']['data_coverage'],1)
        self.assertEqual(result['composite']['decision_weight'],0)

    def test_research_evidence_is_explicit_and_auditable(self):
        snapshot={'coverage_pct':75,'valuation':{'metrics':{'pe':{'value':20,'historical_percentile':25}}},'financial':{'indicators':{'operating_revenue':{'yoy':.1},'net_profit':{'yoy':-.05}}},'forecast':{'revision_30d_pct':2.5},'ratings':{'target_price':110,'coverage_count':12}}
        evidence=self.logic['research_evidence'](snapshot,100)
        self.assertTrue(any('历史分位' in item for item in evidence['bull']))
        self.assertTrue(any('净利润同比' in item for item in evidence['bear']))
        self.assertTrue(any('覆盖 75%' in item for item in evidence['limitations']))

    def test_portfolio_risk_metrics_and_contributions_are_consistent(self):
        points_a=[];points_b=[]
        for index in range(260):
            close_a=100*(1.001**index)*(1+.01*math.sin(index/7))
            close_b=80*(1.0007**index)*(1+.008*math.sin(index/7))
            day=f'2026-{index:03d}'
            points_a.append({'date':day,'close':close_a})
            points_b.append({'date':day,'close':close_b})
        snapshot={'positions':[
            {'symbol':'AAPL','market_value_cny':400,'sector':'科技','currency':'USD','market':'US','quantity':1,'price':100,'fx_to_cny':7},
            {'symbol':'0700.HK','market_value_cny':300,'sector':'通信服务','currency':'HKD','market':'HK','quantity':1,'price':80,'fx_to_cny':.9},
        ],'account':{'net_assets_cny':1000,'total_cash_cny':300}}
        histories={'AAPL':{'points':points_a},'0700.HK':{'points':points_b}}
        result=self.logic['portfolio_risk_from_histories'](snapshot,histories,self.logic['investment_policy']())
        self.assertIsNotNone(result['metrics']['historical_var_95_1d'])
        self.assertEqual(result['rebalance']['status'],'not_configured')
        self.assertAlmostEqual(sum(item['variance_contribution_pct'] for item in result['risk_contributions']),100,places=1)
        self.assertTrue(result['quality']['cross_market_lag_warning'])
        self.assertFalse(result['fx_history_covered'])

    def test_pending_orders_only_keep_active_unfilled_orders(self):
        raw={'orders':[
            {'order_id':'1','symbol':'AAPL.US','side':'Buy','status':'PartialFilled','price':'190','quantity':'10','executed_quantity':'4'},
            {'order_id':'2','symbol':'NVDA.US','side':'Sell','status':'Filled','price':'180','quantity':'2'},
        ]}
        result=self.logic['normalize_pending_orders'](raw)
        self.assertEqual(len(result),1)
        self.assertEqual(result[0]['symbol'],'AAPL')
        self.assertEqual(result[0]['side'],'buy')
        self.assertEqual(result[0]['executed_quantity'],4)

    def test_latest_quote_point_uses_newest_extended_session(self):
        quote={
            'last_done':'200.75','timestamp':'2026-07-31T20:00:00Z',
            'pre_market_quote':{'last_done':'198.49','timestamp':'2026-07-31T13:30:00Z'},
            'post_market_quote':{'last_done':'198.95','timestamp':'2026-07-31T23:59:59Z'},
            'overnight_quote':{'last_done':'198.33','timestamp':'2026-07-31T08:00:00Z'},
        }
        result=self.logic['latest_quote_point'](quote)
        self.assertEqual(result['session'],'post_market')
        self.assertEqual(result['price'],198.95)

    def test_longbridge_candlesticks_are_sorted_and_deduplicated(self):
        rows=[
            {'timestamp':'2026-07-31T00:00:00Z','open':'198','high':'202','low':'197','close':'200.75','volume':'100'},
            {'timestamp':'2026-07-30T00:00:00Z','open':'195','high':'199','low':'194','close':'195.04','volume':'90'},
            {'timestamp':'2026-07-31T00:00:00Z','open':'198','high':'203','low':'197','close':'201','volume':'110'},
        ]
        result=self.logic['normalize_longbridge_candlesticks'](rows)
        self.assertEqual([item['date'] for item in result],['2026-07-30','2026-07-31'])
        self.assertEqual(result[-1]['close'],201)
        self.assertEqual(result[-1]['volume'],110)

    def test_account_snapshot_exposes_margin_and_pending_orders(self):
        stock_data={'list':[{'stock_info':[{'symbol':'AAPL.US','quantity':'1','available_quantity':'1','cost_price':'100','currency':'USD','market':'US'}]}]}
        balance_data={'list':[{'currency':'USD','net_assets':'1000','buy_power':'300','init_margin':'100','maintenance_margin':'80','margin_call':'5','max_finance_amount':'500','remaining_finance_amount':'200','risk_level':'warning','cash_infos':[]}]}
        exchange_data={'exchanges':[{'base_currency':'CNY','other_currency':'USD','average_rate':7}]}
        orders={'orders':[{'order_id':'1','symbol':'AAPL.US','side':'Buy','status':'New','quantity':'2','executed_quantity':'0'}]}
        result=self.logic['build_account_snapshot'](stock_data,balance_data,exchange_data,{'AAPL':120},orders_data=orders)
        self.assertEqual(result['account']['margin_call_cny'],35)
        self.assertEqual(result['account']['maintenance_margin_cny'],560)
        self.assertEqual(result['account']['risk_levels'],['warning'])
        self.assertEqual(len(result['pending_orders']),1)
        self.assertEqual(result['price_source_status'],'degraded_until_longbridge_quote_sdk')

    def test_finance_calendar_normalization_and_event_guard(self):
        raw={'list':[{'counter_id':'ST/US/AAPL','report_date':'2026-08-03','title':'季度业绩发布','type':'report'}]}
        events=self.logic['normalize_finance_events'](raw,['AAPL.US'],date(2026,8,1))
        self.assertEqual(events[0]['symbol'],'AAPL')
        self.assertEqual(events[0]['days_until'],2)
        body=dict(self.body,upcoming_events=events)
        result=self.run_decision({'rating':'Buy','executive_summary':'建议加仓。','position_sizing':'建议加仓。'},body)
        self.assertEqual(result['rating'],'Hold')
        self.assertTrue(any('财报事件数据' in item for item in result['consistency']['violations']))

    def test_pending_buy_order_blocks_duplicate_add(self):
        body=dict(self.body,portfolio_context={'position_weight':.05,'company_weight':.05,'pending_buy_quantity':10})
        result=self.run_decision({'rating':'Overweight','executive_summary':'建议加仓。','position_sizing':'建议加仓。'},body)
        self.assertEqual(result['rating'],'Hold')
        self.assertTrue(any('订单状态' in item for item in result['consistency']['violations']))

    def test_missing_order_or_event_data_blocks_add(self):
        body=dict(self.body,portfolio_context={'position_weight':.05,'company_weight':.05,'order_data_complete':False},event_data_complete=False)
        flags=self.logic['build_risk_flags'](body)
        result=self.run_decision({'rating':'Buy','executive_summary':'建议加仓。','position_sizing':'建议加仓。'},body)
        self.assertEqual(result['rating'],'Hold')
        self.assertTrue(any('订单风险覆盖不完整' in item for item in flags))
        self.assertTrue(any('财报事件日历暂不可用' in item for item in flags))


if __name__ == '__main__':
    unittest.main()

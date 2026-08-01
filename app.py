"""股票看板 API — 行情代理 + AI 分析。环境变量：DEEPSEEK_API_KEY"""
import json, os, urllib.request, ssl, time
from flask import Flask, request, jsonify

app = Flask(__name__)
CTX = ssl.create_default_context()
CTX.check_hostname = False
CTX.verify_mode = False
SINA = 'gb_goog,gb_aapl,gb_msft,gb_nvda,gb_tsla,gb_baba,gb_paas,gb_tlt,gb_smh,gb_appx,hk09988,hk00981,hk06030,hk00100,hk02824,gb_$inx,gb_ixic'

RATINGS = {'Buy', 'Overweight', 'Hold', 'Underweight', 'Sell'}
RATING_SCORE = {'Sell': -2, 'Underweight': -1, 'Hold': 0, 'Overweight': 1, 'Buy': 2}
EVIDENCE_KEYWORDS = ('价格', '收益', '仓位', '敞口', '集中', '回撤', '成本', '现金', '风险')

def as_float(value, default=0.0):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default

def has_chinese(value):
    return any('\u4e00' <= char <= '\u9fff' for char in str(value))

def chinese_list(value, default=None, limit=4):
    if not isinstance(value, list):
        return list(default or [])
    cleaned = [str(item).strip() for item in value if str(item).strip() and has_chinese(item)]
    return cleaned[:limit] or list(default or [])

def fallback_analysis(body, risk_flags):
    price = max(as_float(body.get('price')), 0.01)
    ret = as_float(body.get('return_pct'))
    sector = str(body.get('sector', '未知'))
    if sector == '杠杆ETF' or ret <= -40:
        rating = 'Sell'
    elif ret >= 50 or any('集中度' in flag for flag in risk_flags):
        rating = 'Underweight'
    else:
        rating = 'Hold'
    return {
        'rating': rating,
        'confidence': 55,
        'executive_summary': f'当前收益率 {ret:.1f}%。在缺少完整财报、新闻和真实技术指标时，先执行仓位纪律并等待更多证据。',
        'bull_case': [
            '当前仍有可用购买力，可以分批执行而不是一次性交易。',
            f'{sector}敞口可与组合其他资产共同评估。',
        ],
        'bear_case': [
            '当前分析只使用持仓、价格和组合权重，尚未验证最新基本面。',
            '模型示意曲线不是真实历史 K 线，不能单独作为买卖依据。',
        ],
        'position_sizing': '维持或降低现有仓位；新增仓位需通过集中度上限校验。',
        'entry_price': round(price * 0.96, 2),
        'stop_loss': round(price * 0.90, 2),
        'price_target': round(price * 1.12, 2),
        'time_horizon': '1-3个月观察',
        'invalidation_conditions': ['跌破风险价位且投资逻辑没有新增证据支持', '公司或行业基本面出现实质恶化'],
        'change_reason': '首次生成建议，暂无上一条决策可比较。',
        'new_evidence': [],
        'source': 'rule_fallback',
    }

def normalize_analysis(raw, body, risk_flags):
    fallback = fallback_analysis(body, risk_flags)
    rating = str(raw.get('rating', fallback['rating'])).strip().title()
    aliases = {'买入':'Buy', '加仓':'Overweight', '持有':'Hold', '减仓':'Underweight', '卖出':'Sell'}
    rating = aliases.get(str(raw.get('rating', '')).strip(), rating)
    if rating not in RATINGS:
        rating = fallback['rating']
    model_rating = rating
    context = body.get('portfolio_context') or {}
    position_weight = as_float(context.get('position_weight'))
    company_weight = as_float(context.get('company_weight'))
    concentrated = position_weight >= 0.18 or company_weight >= 0.20
    blocked_add = concentrated and rating in {'Buy', 'Overweight'}
    if blocked_add:
        rating = 'Hold'
        risk_flags.append('硬风控已拦截加仓：单一仓位或同公司经济敞口过高')

    def chinese_text(key, default):
        value = str(raw.get(key) or '').strip()
        return value if has_chinese(value) else default

    def text_list(key, default):
        return chinese_list(raw.get(key), default)

    result = fallback.copy()
    result.update({
        'rating': rating,
        'model_rating': model_rating,
        'confidence': max(0, min(100, round(as_float(raw.get('confidence'), fallback['confidence'])))),
        'executive_summary': chinese_text('executive_summary', fallback['executive_summary']),
        'bull_case': text_list('bull_case', fallback['bull_case']),
        'bear_case': text_list('bear_case', fallback['bear_case']),
        'position_sizing': chinese_text('position_sizing', fallback['position_sizing']),
        'time_horizon': chinese_text('time_horizon', fallback['time_horizon']),
        'invalidation_conditions': text_list('invalidation_conditions', fallback['invalidation_conditions']),
        'change_reason': chinese_text('change_reason', fallback['change_reason']),
        'new_evidence': text_list('new_evidence', []),
        'source': 'deepseek_structured',
    })
    if concentrated:
        exposure = max(position_weight, company_weight) * 100
        action = '优先按计划降低敞口' if rating in {'Underweight', 'Sell'} else '仅维持现有仓位并等待敞口下降'
        result['position_sizing'] = f'硬风控：当前集中敞口 {exposure:.1f}%，禁止新增仓位；{action}。'
        if blocked_add:
            result['executive_summary'] = (
                '模型原始结论包含加仓倾向，但因集中度超过上限，最终操作已强制调整为持有且禁止加仓。'
                + result['executive_summary']
            )
    for field in ('entry_price', 'stop_loss', 'price_target'):
        value = as_float(raw.get(field), fallback[field])
        result[field] = round(value, 2) if value > 0 else fallback[field]
    return result

def validate_decision(result, body, risk_flags):
    """用确定性规则审批模型建议；LLM 不能绕过价格、仓位和建议稳定性约束。"""
    result = dict(result)
    original_rating = result.get('model_rating', result.get('rating', 'Hold'))
    final_rating = result.get('rating', 'Hold') if result.get('rating') in RATINGS else 'Hold'
    violations, adjustments = [], []
    price = max(as_float(body.get('price')), 0.01)
    context = body.get('portfolio_context') or {}
    concentrated = as_float(context.get('position_weight')) >= 0.18 or as_float(context.get('company_weight')) >= 0.20
    if original_rating != final_rating:
        violations.append(f'模型原始评级 {original_rating} 已被前置硬风控调整为 {final_rating}')
        adjustments.append('保留硬风控调整后的评级')

    text = '；'.join((str(result.get('executive_summary', '')), str(result.get('position_sizing', ''))))
    action_text = text.replace('不建议加仓', '').replace('禁止加仓', '').replace('禁止新增仓位', '').replace('不得加仓', '')
    positive_phrases = ('建议加仓', '建议增持', '逢低加仓', '小幅增持', '增加仓位')
    negative_phrases = ('建议减仓', '建议卖出', '降低仓位', '降低敞口', '清仓')
    if final_rating in {'Buy', 'Overweight'} and any(word in action_text for word in negative_phrases):
        violations.append('评级为买入/加仓，但文字包含减仓或卖出指令')
    if final_rating in {'Underweight', 'Sell'} and any(word in action_text for word in positive_phrases):
        violations.append('评级为减仓/卖出，但文字包含加仓指令')
    if final_rating == 'Hold' and any(word in action_text for word in positive_phrases + negative_phrases):
        violations.append('评级为持有，但文字包含明确的加仓或减仓指令')
    if concentrated and final_rating in {'Buy', 'Overweight'}:
        violations.append('集中度超过硬上限，禁止新增仓位')

    stop_loss = as_float(result.get('stop_loss'))
    price_target = as_float(result.get('price_target'))
    entry_price = as_float(result.get('entry_price'))
    if stop_loss <= 0 or stop_loss >= price:
        result['stop_loss'] = round(price * 0.90, 2)
        violations.append('止损价必须低于当前价格')
        adjustments.append('已重置止损价')
    if final_rating in {'Buy', 'Overweight', 'Hold'} and (price_target <= price or price_target <= stop_loss):
        result['price_target'] = round(price * 1.12, 2)
        violations.append('做多或持有建议的目标价必须高于当前价和止损价')
        adjustments.append('已重置目标价')
    if final_rating in {'Buy', 'Overweight'} and (entry_price <= result['stop_loss'] or entry_price > price * 1.05):
        result['entry_price'] = round(price * 0.96, 2)
        violations.append('加仓价格与止损价或当前价格关系不合理')
        adjustments.append('已重置建议入场价')

    previous = body.get('previous_decision') or {}
    previous_rating = str(previous.get('rating', ''))
    changed = previous_rating in RATINGS and previous_rating != final_rating
    previous_price = as_float(previous.get('price'))
    price_changed = previous_price > 0 and abs(price - previous_price) / previous_price >= 0.01
    previous_context = previous.get('portfolio_context') or {}
    exposure_changed = any(
        abs(as_float(context.get(key)) - as_float(previous_context.get(key))) >= 0.01
        for key in ('position_weight', 'sector_weight', 'company_weight')
    ) if previous_context else False
    material_change = price_changed or exposure_changed
    valid_new_evidence = [
        item for item in chinese_list(result.get('new_evidence'), [])
        if any(keyword in item for keyword in EVIDENCE_KEYWORDS)
    ]
    result['new_evidence'] = valid_new_evidence
    if changed and concentrated:
        result['change_reason'] = '组合集中度已达到硬风控阈值，本次方向变化由仓位风险约束触发。'
    elif changed and (not valid_new_evidence or not material_change):
        violations.append('建议发生变化，但价格或组合敞口没有达到可验证的变化阈值')
        final_rating = 'Hold'
        adjustments.append('建议已降级为持有，等待可验证的新证据')
        result['change_reason'] = '本次建议与上次不同，但价格变化不足1%且组合敞口未明显改变，因此暂不执行方向变化。'
    elif changed:
        result['change_reason'] = result.get('change_reason') or '建议因新的价格或组合风险证据发生变化。'
    elif previous_rating in RATINGS:
        result['change_reason'] = '与上次操作方向一致，当前没有触发方向反转的充分证据。'

    if violations and any('文字包含' in item for item in violations):
        final_rating = 'Hold'
        result['executive_summary'] = '本次模型建议存在方向矛盾，未通过一致性校验；在获得一致且可验证的证据前暂时持有。'
        result['position_sizing'] = '一致性校验未通过，暂不新增或主动减少仓位。'
        adjustments.append('已消除矛盾指令并降级为持有')
    if concentrated and final_rating in {'Buy', 'Overweight'}:
        final_rating = 'Hold'
        result['position_sizing'] = '集中度超过硬上限，禁止新增仓位；仅维持现有仓位并等待敞口下降。'
        adjustments.append('集中度硬风控已将建议降级为持有')

    result['rating'] = final_rating
    stats = body.get('validation_context') or {}
    sample_size = max(0, int(as_float(stats.get('sample_size'))))
    hit_rate = max(0.0, min(1.0, as_float(stats.get('directional_hit_rate'))))
    model_confidence = max(0, min(100, round(as_float(result.get('confidence'), 50))))
    if sample_size >= 10:
        calibrated = round(hit_rate * 100)
        result['confidence'] = min(model_confidence, calibrated)
        result['confidence_basis'] = f'基于 {sample_size} 个已验证窗口校准，历史方向命中率 {calibrated}%'
    else:
        result['confidence'] = min(model_confidence, 70)
        result['confidence_basis'] = f'历史有效样本仅 {sample_size} 个，置信度暂按模型值封顶 70%'

    current_weight = max(0.0, as_float(context.get('position_weight')))
    if final_rating == 'Buy':
        target_weight = min(0.18, current_weight + 0.03)
    elif final_rating == 'Overweight':
        target_weight = min(0.18, current_weight + 0.02)
    elif final_rating == 'Underweight':
        target_weight = max(0.0, current_weight - 0.04)
    elif final_rating == 'Sell':
        target_weight = 0.0
    else:
        target_weight = current_weight
    result['current_position_pct'] = round(current_weight * 100, 1)
    result['target_position_pct'] = round(target_weight * 100, 1)
    result['expected_upside_pct'] = round((as_float(result.get('price_target')) / price - 1) * 100, 1)
    downside = max(0.01, (price - as_float(result.get('stop_loss'))) / price)
    upside = max(0.0, (as_float(result.get('price_target')) - price) / price)
    result['risk_reward_ratio'] = round(upside / downside, 2) if final_rating in {'Buy', 'Overweight', 'Hold'} else None
    if final_rating in {'Buy', 'Overweight'}:
        result['position_sizing'] = f'当前仓位约 {current_weight*100:.1f}%，建议仅在入场价附近分批提高至 {target_weight*100:.1f}%，不得一次性追高。'
        result['action_steps'] = ['等待价格进入建议入场区间', '分两批完成目标仓位', '跌破止损价停止加仓并重新评估']
    elif final_rating == 'Underweight':
        result['position_sizing'] = f'当前仓位约 {current_weight*100:.1f}%，建议分批降低至 {target_weight*100:.1f}%，优先收敛组合风险。'
        result['action_steps'] = ['先降低四分之一至二分之一仓位', '反弹接近目标价时继续减仓', '若风险继续扩大则提前复核']
    elif final_rating == 'Sell':
        result['position_sizing'] = f'当前仓位约 {current_weight*100:.1f}%，目标仓位为 0%，建议按流动性分批退出。'
        result['action_steps'] = ['停止新增资金', '按计划分批退出', '退出后继续记录价格以验证卖出判断']
    else:
        result['position_sizing'] = f'当前仓位约 {current_weight*100:.1f}%，目标维持 {target_weight*100:.1f}%；未出现新证据前不主动改变方向。'
        result['action_steps'] = ['维持现有仓位', '不因短期波动追涨杀跌', '触发目标价、止损价或敞口变化后复核']
    result['review_trigger'] = '价格较本次分析变化1%、触及止损/目标价，或单股/同公司敞口变化1个百分点时重新分析。'

    result['consistency'] = {
        'status': 'adjusted' if violations else 'passed',
        'passed': not violations,
        'original_rating': original_rating,
        'final_rating': final_rating,
        'violations': violations,
        'adjustments': list(dict.fromkeys(adjustments)),
    }
    return result

def build_risk_flags(body):
    context = body.get('portfolio_context') or {}
    flags = []
    position_weight = as_float(context.get('position_weight'))
    sector_weight = as_float(context.get('sector_weight'))
    company_weight = as_float(context.get('company_weight'))
    if position_weight >= 0.15:
        flags.append(f'单一持仓占比 {position_weight*100:.1f}%，集中度偏高')
    if sector_weight >= 0.35:
        flags.append(f'板块占比 {sector_weight*100:.1f}%，主题暴露偏高')
    if company_weight >= 0.15:
        flags.append(f'同公司合并敞口 {company_weight*100:.1f}%（含跨市场持仓）')
    if as_float(body.get('return_pct')) <= -30:
        flags.append('当前回撤超过 30%，禁止仅因价格下跌而机械补仓')
    if as_float(body.get('cost')) <= 0:
        flags.append('持仓为负成本，收益率不能直接用于常规止盈判断')
    return flags

@app.after_request
def no_cache(response):
    response.headers['Cache-Control'] = 'no-store, no-cache, must-revalidate, max-age=0'
    response.headers['Pragma'] = 'no-cache'
    response.headers['Expires'] = '0'
    response.headers['Access-Control-Allow-Origin'] = '*'
    response.headers['Access-Control-Allow-Headers'] = 'Content-Type, Authorization'
    response.headers['Access-Control-Allow-Methods'] = 'GET, POST, OPTIONS'
    return response

@app.route('/health')
def health():
    return jsonify({'status': 'ok', 'routes': ['/health', '/prices', '/analysis']})

@app.route('/prices')
def prices():
    try:
        req = urllib.request.Request(f'http://hq.sinajs.cn/list={SINA}',
            headers={'Referer': 'https://finance.sina.com.cn'})
        raw = urllib.request.urlopen(req, timeout=10, context=CTX).read().decode('gbk')
        prices = {}
        for line in raw.strip().split('\n'):
            if '=' not in line: continue
            var = line.split('=')[0]
            try:
                parts = line.split('"')[1].split(',')
            except IndexError:
                continue
            if len(parts) < 8 or not parts[1]: continue  # 空行/字段不足直接跳过
            if 'gb_' in var:
                # 新浪美股: [1]=现价  [2]=涨跌幅  [3]=时间
                # gb_$inx 是标普500 -> 映射为 SPX
                sym = var.split('_')[-1].upper()  # GOOG / $INX / IXIC
                if sym == '$INX': sym = 'SPX'
                prices[sym + '.US'] = float(parts[1])
            else:
                # 新浪港股: [1]=名称  [3]=昨收  [6]=现价  [8]=涨跌幅
                prices[str(int(var.split('hk')[-1])) + '.HK'] = float(parts[6])
        return jsonify({'prices': prices, 'updated': time.strftime('%H:%M:%S')})
    except Exception as e:
        import traceback
        return jsonify({'error': str(e), 'trace': traceback.format_exc()[-300:]}), 500

@app.route('/analysis', methods=['POST'])
def analysis():
    body = request.get_json(force=True, silent=True) or {}
    risk_flags = build_risk_flags(body)
    key = os.getenv('DEEPSEEK_API_KEY', '')
    if not key:
        result = fallback_analysis(body, risk_flags)
    else:
        prompt = f"""你是个人投资组合的研究经理。只根据下面提供的数据工作，不得编造实时新闻、财报、估值或历史K线。
目标年化收益为20%，但风险纪律优先于收益目标。请同时给出多头和空头证据，并输出严格 JSON，不要使用 Markdown。
除 rating 的固定枚举值和股票代码外，所有分析文字必须使用简体中文回复，不得输出英文操作建议。
rating 必须是 Buy、Overweight、Hold、Underweight、Sell 之一。
价格必须使用标的原始报价币种。confidence 为0到100整数。
如果建议与上一条不同，必须在 change_reason 中说明变化原因，并在 new_evidence 数组中列出本次输入里真实发生变化的价格、收益率、仓位或风险证据；不得把未提供的新闻或财报当作新证据。
JSON字段：rating, confidence, executive_summary, bull_case(数组), bear_case(数组), position_sizing, entry_price, stop_loss, price_target, time_horizon, invalidation_conditions(数组), change_reason, new_evidence(数组)。
持仓数据：{json.dumps(body, ensure_ascii=False)}
硬风控提示：{json.dumps(risk_flags, ensure_ascii=False)}"""
        req = urllib.request.Request('https://api.deepseek.com/v1/chat/completions',
            data=json.dumps({'model':'deepseek-chat','messages':[{'role':'user','content':prompt}],
            'response_format':{'type':'json_object'},'max_tokens':900,'temperature':0.25}).encode(),
            method='POST', headers={'Content-Type':'application/json','Authorization':f'Bearer {key}'})
        try:
            resp = json.loads(urllib.request.urlopen(req, timeout=40, context=CTX).read())
            content = resp['choices'][0]['message']['content'] if resp.get('choices') else '{}'
            start, end = content.find('{'), content.rfind('}')
            parsed = json.loads(content[start:end+1]) if start >= 0 and end > start else {}
            result = normalize_analysis(parsed, body, risk_flags)
        except Exception:
            result = fallback_analysis(body, risk_flags)
            result['source'] = 'rule_fallback_after_error'
    result = validate_decision(result, body, risk_flags)
    result.update({
        'symbol': str(body.get('symbol', 'UNKNOWN')),
        'decision_version': 2,
        'risk_flags': risk_flags,
        'data_scope': 'position_price_portfolio_only',
        'generated_at': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
    })
    return jsonify(result)

if __name__ == '__main__':
    port = int(os.environ.get('FC_SERVER_PORT', 9000))
    app.run(host='0.0.0.0', port=port)

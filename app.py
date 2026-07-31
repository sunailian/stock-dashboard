"""股票看板 API — 行情代理 + AI 分析。环境变量：DEEPSEEK_API_KEY"""
import json, os, urllib.request, ssl, time
from flask import Flask, request, jsonify

app = Flask(__name__)
CTX = ssl.create_default_context()
CTX.check_hostname = False
CTX.verify_mode = False
SINA = 'gb_goog,gb_aapl,gb_msft,gb_nvda,gb_tsla,gb_baba,gb_paas,gb_tlt,gb_smh,gb_appx,hk09988,hk00981,hk06030,hk00100,hk02824,gb_$inx,gb_ixic'

RATINGS = {'Buy', 'Overweight', 'Hold', 'Underweight', 'Sell'}

def as_float(value, default=0.0):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default

def has_chinese(value):
    return any('\u4e00' <= char <= '\u9fff' for char in str(value))

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
        'source': 'rule_fallback',
    }

def normalize_analysis(raw, body, risk_flags):
    fallback = fallback_analysis(body, risk_flags)
    rating = str(raw.get('rating', fallback['rating'])).strip().title()
    aliases = {'买入':'Buy', '加仓':'Overweight', '持有':'Hold', '减仓':'Underweight', '卖出':'Sell'}
    rating = aliases.get(str(raw.get('rating', '')).strip(), rating)
    if rating not in RATINGS:
        rating = fallback['rating']
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
        value = raw.get(key)
        if not isinstance(value, list):
            return default
        cleaned = [str(item).strip() for item in value if str(item).strip() and has_chinese(item)]
        return cleaned[:4] or default

    result = fallback.copy()
    result.update({
        'rating': rating,
        'confidence': max(0, min(100, round(as_float(raw.get('confidence'), fallback['confidence'])))),
        'executive_summary': chinese_text('executive_summary', fallback['executive_summary']),
        'bull_case': text_list('bull_case', fallback['bull_case']),
        'bear_case': text_list('bear_case', fallback['bear_case']),
        'position_sizing': chinese_text('position_sizing', fallback['position_sizing']),
        'time_horizon': chinese_text('time_horizon', fallback['time_horizon']),
        'invalidation_conditions': text_list('invalidation_conditions', fallback['invalidation_conditions']),
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
    return jsonify({'status': 'ok', 'routes': ['/health', '/prices', '/ai', '/analysis']})

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
JSON字段：rating, confidence, executive_summary, bull_case(数组), bear_case(数组), position_sizing, entry_price, stop_loss, price_target, time_horizon, invalidation_conditions(数组)。
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
    result.update({
        'symbol': str(body.get('symbol', 'UNKNOWN')),
        'risk_flags': risk_flags,
        'data_scope': 'position_price_portfolio_only',
        'generated_at': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
    })
    return jsonify(result)

@app.route('/ai', methods=['POST'])
def ai():
    try:
        body = request.get_json(force=True, silent=True) or {}
    except:
        body = {}
    key = os.getenv('DEEPSEEK_API_KEY', '')
    if not key:
        return jsonify({'error': 'DEEPSEEK_API_KEY not set'}), 503
    symbol = body.get('symbol', 'UNKNOWN')
    prompt = f"你是灼沅的股票分析师。灼沅年化目标20%。请对{symbol}给出50字以内操作建议，用【持有】【加仓】【减仓】【止损】结尾。"
    req = urllib.request.Request('https://api.deepseek.com/v1/chat/completions',
        data=json.dumps({'model': 'deepseek-chat', 'messages': [{'role': 'user', 'content': prompt}],
        'max_tokens': 150, 'temperature': 0.6}).encode(),
        method='POST', headers={'Content-Type': 'application/json',
        'Authorization': f'Bearer {key}'})
    resp = json.loads(urllib.request.urlopen(req, timeout=30, context=CTX).read())
    result = resp['choices'][0]['message']['content'] if resp.get('choices') else ''
    return jsonify({'analysis': result, 'symbol': symbol})

if __name__ == '__main__':
    port = int(os.environ.get('FC_SERVER_PORT', 9000))
    app.run(host='0.0.0.0', port=port)

"""股票看板 API — 行情代理、组合外标的筛选与 AI 分析。"""
import json, os, urllib.request, urllib.error, ssl, time, math, statistics, hashlib, hmac, base64
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import urlencode
from flask import Flask, request, jsonify

app = Flask(__name__)
CTX = ssl.create_default_context()
CTX.check_hostname = False
CTX.verify_mode = False
RATINGS = {'Buy', 'Overweight', 'Hold', 'Underweight', 'Sell'}
RATING_SCORE = {'Sell': -2, 'Underweight': -1, 'Hold': 0, 'Overweight': 1, 'Buy': 2}
EVIDENCE_KEYWORDS = ('价格', '收益', '仓位', '敞口', '集中', '回撤', '成本', '现金', '风险')

# 候选池只是研究范围，不代表预设推荐。最终顺序每天由真实日线指标和组合互补性计算。
DISCOVERY_UNIVERSE = (
    {'symbol':'META', 'provider_symbol':'usMETA.OQ', 'name':'Meta Platforms', 'ccy':'USD', 'sector':'通信服务', 'group':'META'},
    {'symbol':'AMZN', 'provider_symbol':'usAMZN.OQ', 'name':'Amazon', 'ccy':'USD', 'sector':'可选消费', 'group':'AMAZON'},
    {'symbol':'JPM', 'provider_symbol':'usJPM.N', 'name':'JPMorgan Chase', 'ccy':'USD', 'sector':'金融', 'group':'JPM'},
    {'symbol':'V', 'provider_symbol':'usV.N', 'name':'Visa', 'ccy':'USD', 'sector':'金融', 'group':'VISA'},
    {'symbol':'XOM', 'provider_symbol':'usXOM.N', 'name':'Exxon Mobil', 'ccy':'USD', 'sector':'能源', 'group':'XOM'},
    {'symbol':'LLY', 'provider_symbol':'usLLY.N', 'name':'Eli Lilly', 'ccy':'USD', 'sector':'医疗健康', 'group':'LLY'},
    {'symbol':'COST', 'provider_symbol':'usCOST.OQ', 'name':'Costco', 'ccy':'USD', 'sector':'必选消费', 'group':'COST'},
    {'symbol':'CAT', 'provider_symbol':'usCAT.N', 'name':'Caterpillar', 'ccy':'USD', 'sector':'工业', 'group':'CAT'},
    {'symbol':'NEE', 'provider_symbol':'usNEE.N', 'name':'NextEra Energy', 'ccy':'USD', 'sector':'公用事业', 'group':'NEE'},
    {'symbol':'AVGO', 'provider_symbol':'usAVGO.OQ', 'name':'Broadcom', 'ccy':'USD', 'sector':'半导体', 'group':'AVGO'},
    {'symbol':'0700.HK', 'provider_symbol':'hk00700', 'name':'腾讯控股', 'ccy':'HKD', 'sector':'通信服务', 'group':'TENCENT'},
    {'symbol':'1299.HK', 'provider_symbol':'hk01299', 'name':'友邦保险', 'ccy':'HKD', 'sector':'金融', 'group':'AIA'},
    {'symbol':'0005.HK', 'provider_symbol':'hk00005', 'name':'汇丰控股', 'ccy':'HKD', 'sector':'金融', 'group':'HSBC'},
    {'symbol':'0883.HK', 'provider_symbol':'hk00883', 'name':'中国海洋石油', 'ccy':'HKD', 'sector':'能源', 'group':'CNOOC'},
    {'symbol':'0669.HK', 'provider_symbol':'hk00669', 'name':'创科实业', 'ccy':'HKD', 'sector':'工业', 'group':'TTI'},
    {'symbol':'2020.HK', 'provider_symbol':'hk02020', 'name':'安踏体育', 'ccy':'HKD', 'sector':'可选消费', 'group':'ANTA'},
    {'symbol':'0388.HK', 'provider_symbol':'hk00388', 'name':'香港交易所', 'ccy':'HKD', 'sector':'金融', 'group':'HKEX'},
    {'symbol':'2318.HK', 'provider_symbol':'hk02318', 'name':'中国平安', 'ccy':'HKD', 'sector':'金融', 'group':'PINGAN'},
)
DISCOVERY_ALIASES = {'BABA':'ALIBABA', '9988.HK':'ALIBABA'}
DISCOVERY_MARKET_CACHE = {'saved_at': 0, 'items': {}}
ACCOUNT_CACHE = {'saved_at': 0, 'snapshot': None}
SECTOR_BY_SYMBOL = {
    'GOOG':'科技', 'AAPL':'科技', 'MSFT':'科技', 'NVDA':'半导体', 'TSLA':'汽车',
    'BABA':'电商', 'PAAS':'矿业', 'TLT':'债券', 'SMH':'半导体', 'APPX':'杠杆ETF',
    '9988.HK':'电商', '0981.HK':'半导体', '981.HK':'半导体', '6030.HK':'金融',
    '0100.HK':'AI', '100.HK':'AI', '2824.HK':'黄金',
}

def as_float(value, default=0.0):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default

def clamp(value, low, high):
    return max(low, min(high, value))

def normalized_symbol(symbol):
    value = str(symbol or '').upper().strip()
    if value.endswith('.US'):
        value = value[:-3]
    if value.endswith('.HK'):
        value = value[:-3].zfill(4) + '.HK'
    return value

def credential(name, legacy_name):
    return os.getenv(name) or os.getenv(legacy_name) or ''

def session_configured():
    return bool(os.getenv('DASHBOARD_PASSWORD_HASH') and os.getenv('DASHBOARD_SESSION_SECRET'))

def issue_session_token(ttl_seconds=12 * 60 * 60):
    expires = str(int(time.time()) + ttl_seconds)
    signature = hmac.new(os.environ['DASHBOARD_SESSION_SECRET'].encode(), expires.encode(), hashlib.sha256).hexdigest()
    return base64.urlsafe_b64encode(f'{expires}.{signature}'.encode()).decode().rstrip('=')

def valid_session_token(token):
    try:
        padded = str(token or '') + '=' * (-len(str(token or '')) % 4)
        expires, signature = base64.urlsafe_b64decode(padded).decode().split('.', 1)
        if int(expires) < int(time.time()):
            return False
        expected = hmac.new(os.environ['DASHBOARD_SESSION_SECRET'].encode(), expires.encode(), hashlib.sha256).hexdigest()
        return hmac.compare_digest(signature, expected)
    except (ValueError, TypeError, KeyError, UnicodeDecodeError):
        return False

def request_authorized():
    header = request.headers.get('Authorization', '')
    return session_configured() and header.startswith('Bearer ') and valid_session_token(header[7:])

def auth_error():
    return jsonify({'error':'登录已失效，请重新验证'}), 401

def longbridge_signature(method, path, query, headers, secret, body=b''):
    signed = ('authorization', 'x-api-key', 'x-timestamp')
    header_text = ''.join(f'{key}:{str(headers.get(key, "")).strip()}\n' for key in signed)
    plain = f'{method.upper()}|{path}|{query}|{header_text}|{";".join(signed)}|'
    if body:
        plain += hashlib.sha1(body).hexdigest()
    text_to_sign = 'HMAC-SHA256|' + hashlib.sha1(plain.encode()).hexdigest()
    return hmac.new(secret.encode(), text_to_sign.encode(), hashlib.sha256).hexdigest()

def longbridge_request(path, query_params=None):
    app_key = credential('LONGBRIDGE_APP_KEY', 'LONGPORT_APP_KEY')
    app_secret = credential('LONGBRIDGE_APP_SECRET', 'LONGPORT_APP_SECRET')
    access_token = credential('LONGBRIDGE_ACCESS_TOKEN', 'LONGPORT_ACCESS_TOKEN')
    if not all((app_key, app_secret, access_token)):
        raise RuntimeError('LongPort credentials are not configured in FC environment variables')
    query = urlencode(query_params or {}, doseq=True)
    region = 'us' if any(value.removeprefix('Bearer ').startswith('us_') for value in (app_key, app_secret, access_token)) else 'ap'
    url = (os.getenv('LONGBRIDGE_HTTP_URL') or 'https://openapi.longbridge.com') + path
    if query:
        url += '?' + query
    payload, last_error = None, None
    for attempt in range(2):
        headers = {
            'authorization': access_token.removeprefix('Bearer '),
            'x-api-key': app_key.removeprefix('Bearer '),
            'x-timestamp': str(int(time.time() * 1000)),
            'x-dc-region': region,
            'accept-language': 'zh-CN',
        }
        signature = longbridge_signature('GET', path, query, headers, app_secret)
        headers['x-api-signature'] = 'HMAC-SHA256 SignedHeaders=authorization;x-api-key;x-timestamp, Signature=' + signature
        try:
            req = urllib.request.Request(url, headers=headers)
            payload = json.loads(urllib.request.urlopen(req, timeout=15, context=CTX).read())
            break
        except (urllib.error.URLError, TimeoutError, ssl.SSLError) as exc:
            last_error = exc
            if attempt == 0:
                time.sleep(.2)
    if payload is None:
        raise RuntimeError(f'LongPort network error: {last_error}')
    if as_float(payload.get('code'), -1) != 0:
        raise RuntimeError(f"LongPort API error {payload.get('code')}: {payload.get('message', 'unknown error')}")
    return payload.get('data') or {}

def rates_to_cny(exchange_data):
    graph = {'CNY': {'CNY': 1.0}}
    for item in exchange_data.get('exchanges', []):
        base, other, rate = str(item.get('base_currency', '')).upper(), str(item.get('other_currency', '')).upper(), as_float(item.get('average_rate'))
        if not base or not other or rate <= 0:
            continue
        graph.setdefault(other, {})[base] = rate
        graph.setdefault(base, {})[other] = 1 / rate
    result = {'CNY': 1.0}
    for source in graph:
        queue, visited = [(source, 1.0)], set()
        while queue:
            currency, factor = queue.pop(0)
            if currency in visited:
                continue
            visited.add(currency)
            if currency == 'CNY':
                result[source] = factor
                break
            for target, rate in graph.get(currency, {}).items():
                if target not in visited:
                    queue.append((target, factor * rate))
    return result

def sina_codes(symbols):
    codes, mapping = ['gb_$inx', 'gb_ixic'], {'gb_$inx':'SPX.US', 'gb_ixic':'IXIC.US'}
    for raw in symbols:
        symbol = normalized_symbol(raw)
        if not symbol:
            continue
        if symbol.endswith('.HK'):
            code = 'hk' + symbol[:-3].zfill(5)
            mapping[code] = symbol
        else:
            code = 'gb_' + symbol.lower()
            mapping[code] = symbol
        if code not in codes:
            codes.append(code)
    return codes, mapping

def fetch_sina_prices(symbols):
    codes, mapping = sina_codes(symbols)
    req = urllib.request.Request('http://hq.sinajs.cn/list=' + ','.join(codes), headers={'Referer':'https://finance.sina.com.cn'})
    raw = urllib.request.urlopen(req, timeout=10, context=CTX).read().decode('gbk')
    prices = {}
    for line in raw.strip().split('\n'):
        if '=' not in line:
            continue
        var = line.split('=')[0].split('hq_str_')[-1]
        try:
            parts = line.split('"')[1].split(',')
            price = as_float(parts[1] if var.startswith('gb_') else parts[6])
        except (IndexError, ValueError):
            continue
        symbol = mapping.get(var)
        if symbol and price > 0:
            prices[symbol] = price
    return prices

def aggregate_positions(stock_data):
    combined = {}
    for channel in stock_data.get('list', []):
        account_channel = str(channel.get('account_channel', ''))
        for item in channel.get('stock_info', []):
            symbol = normalized_symbol(item.get('symbol'))
            quantity, cost = as_float(item.get('quantity')), as_float(item.get('cost_price'))
            if not symbol or quantity == 0:
                continue
            target = combined.setdefault(symbol, {
                'symbol':symbol, 'name':str(item.get('symbol_name') or symbol),
                'quantity':0.0, 'available_quantity':0.0, 'cost_value':0.0,
                'currency':str(item.get('currency') or ('HKD' if symbol.endswith('.HK') else 'USD')).upper(),
                'market':str(item.get('market') or ('HK' if symbol.endswith('.HK') else 'US')),
                'account_channels':[],
            })
            target['quantity'] += quantity
            target['available_quantity'] += as_float(item.get('available_quantity'))
            target['cost_value'] += cost * quantity
            if account_channel and account_channel not in target['account_channels']:
                target['account_channels'].append(account_channel)
    positions = []
    for item in combined.values():
        quantity = item.pop('quantity')
        cost_value = item.pop('cost_value')
        item.update({
            'quantity':quantity, 'cost_price':cost_value / quantity if quantity else 0,
            'sector':SECTOR_BY_SYMBOL.get(item['symbol'], '未分类'),
        })
        positions.append(item)
    return sorted(positions, key=lambda item: (item['market'], item['symbol']))

def build_account_snapshot(stock_data, balance_data, exchange_data, prices, fetched_at=None, source_errors=None):
    positions = aggregate_positions(stock_data)
    fx = rates_to_cny(exchange_data)
    for item in positions:
        item['price'] = prices.get(item['symbol'])
        item['fx_to_cny'] = fx.get(item['currency'])
        if item['price'] is not None and item['fx_to_cny'] is not None:
            item['market_value_cny'] = item['price'] * item['quantity'] * item['fx_to_cny']
        else:
            item['market_value_cny'] = None
    balances = balance_data.get('list', [])
    cash_by_currency = {}
    net_assets_cny = buy_power_cny = 0.0
    missing_conversion = []
    for balance in balances:
        currency, rate = str(balance.get('currency') or '').upper(), fx.get(str(balance.get('currency') or '').upper())
        if rate is None:
            missing_conversion.append(currency)
            continue
        net_assets_cny += as_float(balance.get('net_assets')) * rate
        buy_power_cny += as_float(balance.get('buy_power')) * rate
        for cash in balance.get('cash_infos', []):
            cash_currency = str(cash.get('currency') or '').upper()
            cash_by_currency[cash_currency] = cash_by_currency.get(cash_currency, 0) + as_float(cash.get('available_cash'))
    total_cash_cny = 0.0
    for currency, amount in cash_by_currency.items():
        if currency not in fx:
            missing_conversion.append(currency)
        else:
            total_cash_cny += amount * fx[currency]
    missing_prices = [item['symbol'] for item in positions if item['price'] is None]
    missing_fx = [item['currency'] for item in positions if item['fx_to_cny'] is None]
    errors = source_errors or {}
    return {
        'source':'longbridge_openapi', 'fetched_at':fetched_at or time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
        'positions':positions, 'prices':prices, 'fx_to_cny':fx,
        'account':{
            'net_assets_cny':round(net_assets_cny, 2), 'total_cash_cny':round(total_cash_cny, 2),
            'buy_power_cny':round(buy_power_cny, 2), 'cash_by_currency':cash_by_currency,
            'balances':balances,
        },
        'complete':not (missing_prices or missing_fx or missing_conversion or errors),
        'missing_prices':missing_prices, 'missing_currencies':sorted(set(missing_fx + missing_conversion)),
        'source_errors':errors,
    }

def get_account_snapshot(force=False):
    cache = ACCOUNT_CACHE
    if not force and cache['snapshot'] and time.time() - cache['saved_at'] < 15:
        return cache['snapshot']
    source_errors = {}
    with ThreadPoolExecutor(max_workers=3) as pool:
        futures = {
            'positions':pool.submit(longbridge_request, '/v1/asset/stock'),
            'account':pool.submit(longbridge_request, '/v1/asset/account'),
            'exchange_rates':pool.submit(longbridge_request, '/v1/asset/exchange_rates'),
        }
        results = {}
        for name, future in futures.items():
            try:
                results[name] = future.result()
            except Exception as exc:
                source_errors[name] = str(exc)
                results[name] = {}
    stock_data, balance_data, exchange_data = results['positions'], results['account'], results['exchange_rates']
    if 'positions' in source_errors:
        raise RuntimeError('券商持仓接口不可用：' + source_errors['positions'])
    symbols = [item['symbol'] for item in aggregate_positions(stock_data)]
    try:
        prices = fetch_sina_prices(symbols)
    except Exception as exc:
        prices = {}
        source_errors['prices'] = str(exc)
    snapshot = build_account_snapshot(stock_data, balance_data, exchange_data, prices, source_errors=source_errors)
    cache.update({'saved_at':time.time(), 'snapshot':snapshot})
    return snapshot

def company_group_symbol(symbol):
    return DISCOVERY_ALIASES.get(normalized_symbol(symbol), normalized_symbol(symbol))

def live_position_context(snapshot, position):
    invested = sum(as_float(item.get('market_value_cny')) for item in snapshot['positions']) or 1
    position_value = as_float(position.get('market_value_cny'))
    sector_value = sum(as_float(item.get('market_value_cny')) for item in snapshot['positions'] if item.get('sector') == position.get('sector'))
    group = company_group_symbol(position['symbol'])
    company_value = sum(as_float(item.get('market_value_cny')) for item in snapshot['positions'] if company_group_symbol(item['symbol']) == group)
    account = snapshot.get('account') or {}
    return {
        'position_weight':position_value / invested, 'sector_weight':sector_value / invested,
        'company_weight':company_value / invested, 'company_group':group,
        'cash_ratio':as_float(account.get('total_cash_cny')) / (as_float(account.get('net_assets_cny')) or 1),
        'annual_target':20, 'account_fetched_at':snapshot.get('fetched_at'), 'account_source':snapshot.get('source'),
    }

def score_discovery_candidate(closes, sector_weight=0.0):
    """Return an auditable 0-100 score from trend, momentum, risk and diversification."""
    clean = [as_float(value) for value in closes if as_float(value) > 0]
    if len(clean) < 205:
        return None
    price = clean[-1]
    momentum = lambda days: price / clean[-days-1] - 1
    mom20, mom60, mom120 = momentum(20), momentum(60), momentum(120)
    sma20 = statistics.fmean(clean[-20:])
    sma50 = statistics.fmean(clean[-50:])
    sma200 = statistics.fmean(clean[-200:])
    daily_returns = [math.log(clean[i] / clean[i-1]) for i in range(len(clean)-20, len(clean))]
    volatility = statistics.pstdev(daily_returns) * math.sqrt(252)
    drawdown = price / max(clean[-120:]) - 1
    diversification = 15 if sector_weight < .01 else 10 if sector_weight < .08 else 5 if sector_weight < .15 else 0
    score = (
        clamp((mom20 + .10) / .20, 0, 1) * 10
        + clamp((mom60 + .05) / .30, 0, 1) * 20
        + clamp((mom120 + .05) / .45, 0, 1) * 20
        + (10 if price > sma50 else 0)
        + (15 if price > sma200 else 0)
        + clamp((.65 - volatility) / .45, 0, 1) * 10
        + clamp((drawdown + .30) / .30, 0, 1) * 10
        + diversification
    )
    return {
        'score': round(clamp(score, 0, 100)), 'price': price,
        'momentum_20d': mom20, 'momentum_60d': mom60, 'momentum_120d': mom120,
        'sma20': sma20, 'sma50': sma50, 'sma200': sma200,
        'volatility_20d': volatility, 'drawdown_120d': drawdown,
        'sector_weight': max(0.0, sector_weight), 'diversification_score': diversification,
        'eligible': price > sma200 and mom60 > 0 and drawdown > -.30 and volatility < .65,
    }

def build_discovery_recommendation(meta, history, sector_weight=0.0):
    metrics = score_discovery_candidate(history.get('closes', []), sector_weight)
    if not metrics or not metrics['eligible']:
        return None
    price, vol = metrics['price'], metrics['volatility_20d']
    entry = min(price, max(metrics['sma20'], metrics['sma50']))
    risk_pct = clamp(vol * .25, .07, .14)
    stop = min(entry * (1 - risk_pct), metrics['sma50'] * .97)
    target = max(price * 1.08, entry + 2 * (entry - stop))
    target_weight = 5.0 if sector_weight < .01 and vol < .30 else 4.0 if vol < .40 else 2.5
    public_meta = {key: value for key, value in meta.items() if key != 'provider_symbol'}
    return {
        **public_meta, **metrics, 'price': round(price, 2),
        'entry_price': round(entry, 2), 'stop_loss': round(stop, 2),
        'price_target': round(target, 2), 'target_position_pct': target_weight,
        'expected_upside_pct': round((target / price - 1) * 100, 1),
        'risk_reward_ratio': round((target - entry) / max(.01, entry - stop), 2),
        'summary': f"该标的未在当前持仓中；量化评分 {metrics['score']} 分，优先用于补充{meta['sector']}暴露。等待进入建议区间后再分批评估，不追涨。",
        'bull_case': [
            f"60日动量 {metrics['momentum_60d']*100:+.1f}%，且价格位于200日均线上方。",
            f"当前组合该行业权重约 {sector_weight*100:.1f}%，行业互补得分 {metrics['diversification_score']}/15。",
        ],
        'bear_case': [
            f"20日年化波动约 {vol*100:.1f}%，建议仓位必须受限。",
            f"较120日高点回撤 {metrics['drawdown_120d']*100:.1f}%，趋势失效时不得补仓摊低成本。",
        ],
        'position_sizing': f"当前未持有；若价格进入建议区间，可分两批建立不超过组合 {target_weight:.1f}% 的观察仓。",
        'action_steps': ['等待价格进入建议区间', '首批只建立目标仓位的一半', '跌破止损价退出并记录验证结果'],
        'invalidation_conditions': ['日线跌破200日均线', '60日动量转负', '跌破风险价位'],
        'history': [{'date': d, 'close': round(c, 2)} for d, c in zip(history.get('dates', [])[-120:], history.get('closes', [])[-120:])],
        'as_of': history.get('as_of'), 'source': history.get('source', '腾讯证券日线'),
    }

def fetch_candidate_history(meta):
    provider_symbol = meta['provider_symbol']
    url = f'https://web.ifzq.gtimg.cn/appstock/app/kline/kline?param={provider_symbol},day,,,320'
    req = urllib.request.Request(url, headers={'User-Agent':'Mozilla/5.0 stock-dashboard/1.0', 'Accept':'application/json', 'Referer':'https://gu.qq.com/'})
    payload = json.loads(urllib.request.urlopen(req, timeout=12, context=CTX).read())
    rows = payload['data'][provider_symbol].get('qfqday') or payload['data'][provider_symbol].get('day') or []
    points = [(str(row[0]), as_float(row[2])) for row in rows if len(row) >= 3 and as_float(row[2]) > 0]
    if len(points) < 205:
        raise ValueError(f"{meta['symbol']} historical data is insufficient")
    return {'dates':[p[0] for p in points], 'closes':[p[1] for p in points], 'as_of':points[-1][0], 'source':'腾讯证券日线'}

def discovery_market_data():
    cache = DISCOVERY_MARKET_CACHE
    if cache['items'] and time.time() - cache['saved_at'] < 6 * 60 * 60:
        return cache['items']
    items = {}
    with ThreadPoolExecutor(max_workers=6) as pool:
        futures = {pool.submit(fetch_candidate_history, meta): meta for meta in DISCOVERY_UNIVERSE}
        for future in as_completed(futures):
            meta = futures[future]
            try:
                items[meta['symbol']] = future.result()
            except Exception:
                continue
    if items:
        cache.update({'saved_at': time.time(), 'items': items})
    return items

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
    configured = all((
        credential('LONGBRIDGE_APP_KEY', 'LONGPORT_APP_KEY'),
        credential('LONGBRIDGE_APP_SECRET', 'LONGPORT_APP_SECRET'),
        credential('LONGBRIDGE_ACCESS_TOKEN', 'LONGPORT_ACCESS_TOKEN'),
    ))
    return jsonify({'status':'ok', 'account_source':'longbridge_openapi', 'account_configured':configured, 'session_configured':session_configured(), 'routes':['/health', '/session', '/account', '/prices', '/recommendations', '/analysis']})

@app.route('/session', methods=['POST', 'OPTIONS'])
def create_session():
    if request.method == 'OPTIONS':
        return ('', 204)
    if not session_configured():
        return jsonify({'error':'FC 尚未配置 DASHBOARD_PASSWORD_HASH 和 DASHBOARD_SESSION_SECRET'}), 503
    body = request.get_json(force=True, silent=True) or {}
    supplied = hashlib.sha256(str(body.get('password') or '').encode()).hexdigest()
    if not hmac.compare_digest(supplied, os.environ['DASHBOARD_PASSWORD_HASH']):
        return jsonify({'error':'密码错误'}), 401
    return jsonify({'token':issue_session_token(), 'expires_in':12 * 60 * 60})

@app.route('/account')
def account():
    if not request_authorized():
        return auth_error()
    try:
        return jsonify(get_account_snapshot(force=request.args.get('force') == '1'))
    except Exception as exc:
        return jsonify({'error':'实时账户数据获取失败，已阻断持仓展示与个股分析', 'detail':str(exc), 'source':'longbridge_openapi'}), 503

@app.route('/recommendations', methods=['POST', 'OPTIONS'])
def recommendations():
    if request.method == 'OPTIONS':
        return ('', 204)
    if not request_authorized():
        return auth_error()
    body = request.get_json(force=True, silent=True) or {}
    try:
        snapshot = get_account_snapshot()
    except Exception as exc:
        return jsonify({'recommendations':[], 'error':'实时账户数据不可用，无法安全生成组合补充建议', 'detail':str(exc)}), 503
    if not snapshot.get('complete'):
        return jsonify({'recommendations':[], 'error':'账户快照缺少价格或汇率，无法准确计算组合互补性', 'missing_prices':snapshot.get('missing_prices'), 'missing_currencies':snapshot.get('missing_currencies')}), 503
    held_symbols = {normalized_symbol(item.get('symbol')) for item in snapshot.get('positions', [])}
    held_groups = {DISCOVERY_ALIASES.get(symbol, symbol) for symbol in held_symbols}
    invested = sum(as_float(item.get('market_value_cny')) for item in snapshot['positions']) or 1
    sector_weights = {}
    for item in snapshot['positions']:
        sector = str(item.get('sector') or '未分类')
        sector_weights[sector] = sector_weights.get(sector, 0) + as_float(item.get('market_value_cny')) / invested
    market_data = discovery_market_data()
    ranked = []
    for meta in DISCOVERY_UNIVERSE:
        symbol = normalized_symbol(meta['symbol'])
        if symbol in held_symbols or meta['group'] in held_groups:
            continue
        history = market_data.get(meta['symbol'])
        if not history:
            continue
        candidate = build_discovery_recommendation(meta, history, sector_weights.get(meta['sector'], 0))
        if candidate:
            ranked.append(candidate)
    ranked.sort(key=lambda item: (item['score'], item['diversification_score'], item['momentum_60d']), reverse=True)
    for index, item in enumerate(ranked[:5], 1):
        item['rank'] = index
    if not ranked:
        return jsonify({'recommendations': [], 'error':'候选行情暂不可用或没有标的通过趋势与风险筛选', 'generated_at':time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}), 503
    return jsonify({
        'recommendations': ranked[:5], 'universe_size': len(DISCOVERY_UNIVERSE),
        'eligible_size': len(ranked), 'method_version':'discovery_v1',
        'generated_at':time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
        'disclaimer':'量化候选仅供研究，不构成收益承诺或自动交易指令。',
    })

@app.route('/prices')
def prices():
    if not request_authorized():
        return auth_error()
    try:
        snapshot = get_account_snapshot()
        return jsonify({'prices':snapshot['prices'], 'updated':snapshot['fetched_at'], 'source':snapshot['source']})
    except Exception as e:
        return jsonify({'error':'实时账户行情获取失败', 'detail':str(e)}), 503

@app.route('/analysis', methods=['POST'])
def analysis():
    if not request_authorized():
        return auth_error()
    body = request.get_json(force=True, silent=True) or {}
    try:
        snapshot = get_account_snapshot(force=True)
    except Exception as exc:
        return jsonify({'error':'实时账户数据不可用，已阻断个股分析', 'detail':str(exc)}), 503
    if not snapshot.get('complete'):
        return jsonify({'error':'账户快照不完整，已阻断个股分析', 'missing_prices':snapshot.get('missing_prices'), 'missing_currencies':snapshot.get('missing_currencies')}), 503
    symbol = normalized_symbol(body.get('symbol'))
    position = next((item for item in snapshot['positions'] if normalized_symbol(item.get('symbol')) == symbol), None)
    if not position:
        return jsonify({'error':'该标的不在实时持仓中，不能生成持仓操作建议', 'symbol':symbol}), 409
    body.update({
        'symbol':position['symbol'], 'name':position['name'], 'ccy':position['currency'],
        'cost':position['cost_price'], 'price':position['price'], 'qty':position['quantity'],
        'return_pct':((position['price'] - position['cost_price']) / abs(position['cost_price']) * 100) if position['cost_price'] else 0,
        'sector':position['sector'], 'portfolio_context':live_position_context(snapshot, position),
        'price_updated_at':snapshot['fetched_at'], 'account_verified':True,
    })
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
        'data_scope': 'verified_live_longbridge_position_price_portfolio',
        'generated_at': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
    })
    return jsonify(result)

if __name__ == '__main__':
    port = int(os.environ.get('FC_SERVER_PORT', 9000))
    app.run(host='0.0.0.0', port=port)

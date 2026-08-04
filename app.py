"""股票看板 API — 行情代理、组合外标的筛选与 AI 分析。"""
import json, os, urllib.request, urllib.error, ssl, time, math, statistics, hashlib, hmac, base64, calendar, threading
from datetime import date, datetime, timedelta, timezone
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import urlencode
from flask import Flask, request, jsonify

app = Flask(__name__)
CTX = ssl.create_default_context()
CTX.check_hostname = False
CTX.verify_mode = False
RATINGS = {'Buy', 'Overweight', 'Hold', 'Underweight', 'Sell'}
RATING_SCORE = {'Sell': -2, 'Underweight': -1, 'Hold': 0, 'Overweight': 1, 'Buy': 2}
EVIDENCE_KEYWORDS = ('价格', '收益', '仓位', '敞口', '集中', '回撤', '成本', '现金', '风险', '趋势', '动量', '估值', '盈利', '预期', '因子', '财报')

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
HISTORY_CACHE = {}
PERFORMANCE_CACHE = {'saved_at':0, 'data':None}
ACCOUNT_CACHE = {'saved_at': 0, 'snapshot': None}
MARKET_CACHE = {'saved_at': 0, 'data': None}
PORTFOLIO_RISK_CACHE = {'saved_at': 0, 'signature': None, 'data': None}
EVENT_CACHE = {'saved_at': 0, 'signature': None, 'data': None}
RESEARCH_CACHE = {}
QUOTE_CONTEXT_CACHE = {'signature': None, 'context': None}
QUOTE_CONTEXT_LOCK = threading.Lock()
ACTIVE_ORDER_STATUSES = {
    'new', 'waittonew', 'notreported', 'pending', 'submitted', 'partialfilled',
    'partiallyfilled', 'waittocancel', 'pendingcancel', 'pendingreplace', 'replacednotreported',
}
POLICY_DEFAULT = {
    'version': 1, 'base_currency': 'CNY', 'annual_return_objective': .20,
    'benchmark_by_market': {'US':'SPY.US', 'HK':'HSI.HK'},
    'risk': {'target_volatility_annualized':None, 'max_drawdown_tolerance':None, 'minimum_cash_pct':None, 'maximum_invested_pct':None},
    'limits': {'single_position_pct':None, 'same_company_pct':None, 'sector_pct':None, 'leveraged_etf_pct':None},
    'target_bands': [], 'confirmed_by_user': False, 'updated_at': None,
}
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
        code = value[:-3]
        value = (code.zfill(4) if code.isdigit() else code) + '.HK'
    return value

def iso_now():
    return time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())

def investment_policy():
    """Load immutable policy from FC environment; never infer risk appetite from returns."""
    policy = json.loads(json.dumps(POLICY_DEFAULT))
    raw = os.getenv('INVESTMENT_POLICY_JSON', '').strip()
    if raw:
        try:
            supplied = json.loads(raw)
            for key in ('version', 'base_currency', 'annual_return_objective', 'benchmark_by_market', 'risk', 'limits', 'target_bands', 'confirmed_by_user', 'updated_at'):
                if key in supplied:
                    if key in ('risk', 'limits', 'benchmark_by_market') and isinstance(supplied[key], dict):
                        policy[key].update(supplied[key])
                    else:
                        policy[key] = supplied[key]
        except (TypeError, ValueError, json.JSONDecodeError):
            policy['configuration_error'] = 'INVESTMENT_POLICY_JSON 不是有效 JSON，已使用未确认默认配置'
    return policy

def validate_policy(policy):
    errors = []
    for section in ('risk', 'limits'):
        if not isinstance(policy.get(section), dict):
            errors.append(f'{section} 必须为对象')
            continue
        for key, value in policy[section].items():
            if value is not None and not 0 <= as_float(value, -1) <= 1:
                errors.append(f'{section}.{key} 必须在0到1之间或为null')
    for index, band in enumerate(policy.get('target_bands') or []):
        low, target, high = (band.get('min_pct'), band.get('target_pct'), band.get('max_pct'))
        if any(value is None for value in (low, target, high)) or not 0 <= as_float(low, -1) <= as_float(target, -1) <= as_float(high, -1) <= 1:
            errors.append(f'target_bands[{index}] 必须满足 0≤min≤target≤max≤1')
    return errors

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
    app_key = app_key.removeprefix('Bearer ')
    app_secret = app_secret.removeprefix('Bearer ')
    access_token = access_token.removeprefix('Bearer ')
    query = urlencode(query_params or {}, doseq=True)
    region = os.getenv('LONGBRIDGE_DC_REGION') or ('us' if any(value.removeprefix('Bearer ').startswith('us_') for value in (app_key, app_secret, access_token)) else 'ap')
    configured_url = os.getenv('LONGBRIDGE_HTTP_URL')
    access_region = str(os.getenv('LONGBRIDGE_REGION') or '').lower()
    if configured_url:
        hosts = [configured_url.rstrip('/')]
    elif region == 'ap' and (access_region == 'cn' or str(os.getenv('FC_REGION') or os.getenv('FC_REGION_NAME') or '').startswith('cn-')):
        hosts = ['https://openapi.longbridge.cn', 'https://openapi.longbridge.com']
    else:
        hosts = ['https://openapi.longbridge.com']
        if region == 'ap':
            hosts.append('https://openapi.longbridge.cn')
    payload, last_error = None, None
    for host in hosts:
        url = host + path + (('?' + query) if query else '')
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
            except urllib.error.HTTPError as exc:
                try:
                    error_payload = json.loads(exc.read())
                    code = error_payload.get('code', exc.code)
                    message = error_payload.get('message') or error_payload.get('msg') or str(exc.reason)
                except Exception:
                    code, message = exc.code, str(exc.reason)
                if exc.code == 429 and attempt == 0:
                    time.sleep(1.0)
                    continue
                raise RuntimeError(f'LongPort HTTP {exc.code}, code {code}: {message}') from exc
            except (urllib.error.URLError, TimeoutError, ssl.SSLError) as exc:
                last_error = f'{host}: {exc}'
                if attempt == 0:
                    time.sleep(.2)
        if payload is not None:
            break
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

def longbridge_symbol(symbol):
    value = normalized_symbol(symbol)
    if value.endswith('.HK'):
        code = value[:-3]
        return f'{int(code) if code.isdigit() else code}.HK'
    return value + '.US' if value and '.' not in value else value

def longbridge_counter_id(symbol):
    value = longbridge_symbol(symbol)
    if value.endswith('.HK'):
        return 'ST/HK/' + value[:-3]
    if value.endswith('.US'):
        return 'ST/US/' + value[:-3]
    return value

def quote_timestamp(value):
    if isinstance(value, datetime):
        return value.timestamp()
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value or '').strip().replace('Z', '+00:00')
    try:
        return datetime.fromisoformat(text).timestamp()
    except ValueError:
        return 0.0

def latest_quote_point(quote):
    """Select the newest regular/pre/post/overnight price from an SDK quote."""
    def field(obj, name):
        return obj.get(name) if isinstance(obj, dict) else getattr(obj, name, None)
    candidates = [('regular', quote)]
    for session, name in (
        ('pre_market', 'pre_market_quote'), ('post_market', 'post_market_quote'),
        ('overnight', 'overnight_quote'),
    ):
        value = field(quote, name)
        if value is not None:
            candidates.append((session, value))
    valid = []
    for session, value in candidates:
        price = as_float(field(value, 'last_done') or field(value, 'last'))
        if price <= 0:
            continue
        timestamp = field(value, 'timestamp')
        valid.append((quote_timestamp(timestamp), session, price, timestamp))
    if not valid:
        return None
    _, session, price, timestamp = max(valid, key=lambda item:item[0])
    return {'price':price, 'session':session, 'timestamp':timestamp.isoformat() if isinstance(timestamp, datetime) else str(timestamp or '')}

def longbridge_quote_context():
    app_key = credential('LONGBRIDGE_APP_KEY', 'LONGPORT_APP_KEY')
    app_secret = credential('LONGBRIDGE_APP_SECRET', 'LONGPORT_APP_SECRET')
    access_token = credential('LONGBRIDGE_ACCESS_TOKEN', 'LONGPORT_ACCESS_TOKEN')
    if not all((app_key, app_secret, access_token)):
        raise RuntimeError('LongPort credentials are not configured in FC environment variables')
    app_key = app_key.removeprefix('Bearer ')
    app_secret = app_secret.removeprefix('Bearer ')
    access_token = access_token.removeprefix('Bearer ')
    signature = hashlib.sha256(f'{app_key}|{access_token}'.encode()).hexdigest()
    with QUOTE_CONTEXT_LOCK:
        if QUOTE_CONTEXT_CACHE['context'] is not None and QUOTE_CONTEXT_CACHE['signature'] == signature:
            return QUOTE_CONTEXT_CACHE['context']
        try:
            from longport.openapi import Config, QuoteContext
        except (ImportError, OSError) as exc:
            raise RuntimeError('Longport行情SDK未打包到FC，请使用build_fc_bundle.py生成部署包') from exc
        region = os.getenv('LONGBRIDGE_DC_REGION') or ('us' if access_token.startswith('us_') else 'ap')
        access_region = str(os.getenv('LONGBRIDGE_REGION') or '').lower()
        fc_region = str(os.getenv('FC_REGION') or os.getenv('FC_REGION_NAME') or '')
        default_http_url = 'https://openapi.longbridge.cn' if region == 'ap' and (access_region == 'cn' or fc_region.startswith('cn-')) else 'https://openapi.longbridge.com'
        kwargs = {
            'http_url':os.getenv('LONGBRIDGE_HTTP_URL') or os.getenv('LONGPORT_HTTP_URL') or default_http_url,
            'enable_overnight':True, 'enable_print_quote_packages':False, 'log_path':'/tmp',
        }
        quote_ws_url = os.getenv('LONGBRIDGE_QUOTE_WS_URL') or os.getenv('LONGPORT_QUOTE_WS_URL')
        if quote_ws_url:
            kwargs['quote_ws_url'] = quote_ws_url
        if hasattr(Config, 'from_apikey'):
            config = Config.from_apikey(app_key, app_secret, access_token, **kwargs)
        else:
            config = Config(app_key, app_secret, access_token, **kwargs)
        context = QuoteContext(config)
        QUOTE_CONTEXT_CACHE.update({'signature':signature, 'context':context})
        return context

def fetch_longbridge_sdk_prices(symbols):
    clean = sorted({normalized_symbol(symbol) for symbol in symbols if normalized_symbol(symbol)})
    if not clean:
        return {}, {}
    response = longbridge_quote_context().quote([longbridge_symbol(symbol) for symbol in clean])
    prices, details = {}, {}
    for quote in response:
        raw_symbol = quote.get('symbol') if isinstance(quote, dict) else getattr(quote, 'symbol', None)
        symbol = normalized_symbol(raw_symbol)
        point = latest_quote_point(quote)
        if not symbol or point is None:
            continue
        prices[symbol] = point['price']
        details[symbol] = {'source':'longbridge_quote_sdk', **point}
    return prices, details

def nested_dicts(value):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from nested_dicts(child)
    elif isinstance(value, list):
        for child in value:
            yield from nested_dicts(child)

def normalize_order_status(value):
    return ''.join(char for char in str(value or '').lower() if char.isalnum())

def normalize_pending_orders(data):
    orders, seen = [], set()
    for item in nested_dicts(data):
        order_id = item.get('order_id') or item.get('id')
        symbol = item.get('symbol') or item.get('code')
        status = item.get('status') or item.get('order_status')
        if not order_id or not symbol or normalize_order_status(status) not in ACTIVE_ORDER_STATUSES:
            continue
        key = str(order_id)
        if key in seen:
            continue
        seen.add(key)
        side = str(item.get('side') or item.get('action') or '').lower()
        orders.append({
            'order_id':key, 'symbol':normalized_symbol(symbol),
            'side':'buy' if side in {'buy','b'} else 'sell' if side in {'sell','s'} else side,
            'status':str(status), 'price':as_float(item.get('price')) or None,
            'quantity':as_float(item.get('quantity') or item.get('qty')),
            'executed_quantity':as_float(item.get('executed_quantity') or item.get('filled_quantity')),
            'updated_at':item.get('updated_at') or item.get('submitted_at') or item.get('created_at'),
        })
    return orders

def parse_event_day(value):
    if value is None:
        return None
    if isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(value / 1000 if value > 10_000_000_000 else value, timezone.utc).date()
        except (ValueError, OSError, OverflowError):
            return None
    text = str(value).strip().replace('Z', '+00:00')
    try:
        return datetime.fromisoformat(text).date()
    except ValueError:
        try:
            return date.fromisoformat(text[:10])
        except ValueError:
            return None

def symbol_from_counter(value):
    parts = str(value or '').split('/')
    if len(parts) >= 3 and parts[-2] in {'US', 'HK'}:
        return normalized_symbol(parts[-1] + '.' + parts[-2])
    return normalized_symbol(value)

def normalize_finance_events(data, requested_symbols, today=None):
    today = today or datetime.now(timezone.utc).date()
    requested = {normalized_symbol(symbol) for symbol in requested_symbols}
    events, seen = [], set()
    date_keys = ('event_date', 'report_date', 'announce_date', 'date', 'calendar_date', 'timestamp', 'time')
    for item in nested_dicts(data):
        event_day = None
        for key in date_keys:
            event_day = parse_event_day(item.get(key))
            if event_day:
                break
        raw_symbol = item.get('symbol') or item.get('stock_symbol') or item.get('counter_id') or item.get('counter')
        symbol = symbol_from_counter(raw_symbol)
        title = item.get('title') or item.get('name') or item.get('event_name') or item.get('report_type')
        if not event_day or not symbol or symbol not in requested or not title:
            continue
        key = (symbol, str(event_day), str(title))
        if key in seen:
            continue
        seen.add(key)
        days_until = (event_day - today).days
        events.append({
            'symbol':symbol, 'date':str(event_day), 'days_until':days_until,
            'title':str(title), 'event_type':str(item.get('type') or item.get('event_type') or 'earnings'),
            'session':item.get('session') or item.get('market_session'), 'source':'longbridge_finance_calendar',
        })
    return sorted((item for item in events if item['days_until'] >= 0), key=lambda item:(item['date'], item['symbol']))

def get_upcoming_finance_events(symbols, force=False, today=None):
    today = today or datetime.now(timezone(timedelta(hours=8))).date()
    clean = sorted({normalized_symbol(symbol) for symbol in symbols if normalized_symbol(symbol)})
    signature = '|'.join(clean) + '|' + str(today)
    if not force and EVENT_CACHE['data'] is not None and EVENT_CACHE['signature'] == signature and time.time() - EVENT_CACHE['saved_at'] < 30 * 60:
        return EVENT_CACHE['data']
    if not clean:
        return {'events':[], 'complete':True, 'source':'longbridge_finance_calendar', 'fetched_at':iso_now()}
    params = {
        'date':str(today), 'date_end':str(today + timedelta(days=7)), 'count':'100', 'offset':'0', 'next':'later',
        'types[]':['report', 'financial'], 'counter_ids[]':[longbridge_counter_id(symbol) for symbol in clean],
    }
    try:
        raw = longbridge_request('/v1/quote/finance_calendar', params)
        data = {'events':normalize_finance_events(raw, clean, today), 'complete':True, 'source':'longbridge_finance_calendar', 'fetched_at':iso_now()}
    except Exception as exc:
        data = {'events':[], 'complete':False, 'error':str(exc), 'source':'longbridge_finance_calendar', 'fetched_at':iso_now()}
    EVENT_CACHE.update({'saved_at':time.time(), 'signature':signature, 'data':data})
    return data

def normalize_valuation_snapshot(data):
    overview = data.get('overview') or {}
    metrics = overview.get('metrics') or data.get('metrics') or {}
    history_metrics = ((data.get('history') or {}).get('metrics') or {})
    normalized = {}
    for key in ('pe', 'pb', 'ps', 'dvd_yld'):
        item = metrics.get(key) or {}
        value = as_float(str(item.get('metric') or item.get('value') or '').rstrip('xX%'), float('nan'))
        if not math.isfinite(value):
            continue
        historical = [as_float(row.get('value'), float('nan')) for row in ((history_metrics.get(key) or {}).get('list') or [])]
        historical = [number for number in historical if math.isfinite(number)]
        normalized[key] = {
            'value':round(value, 4), 'industry_median':as_float(item.get('industry_median')) or None,
            'historical_percentile':round(sum(number <= value for number in historical) / len(historical) * 100, 1) if historical else None,
            'history_count':len(historical),
        }
    return {'as_of':overview.get('date'), 'currency_symbol':overview.get('ccy_symbol'), 'metrics':normalized}

def normalize_financial_snapshot(data):
    indicators = {}
    for item in data.get('indicators') or []:
        name = str(item.get('field_name') or '')
        if not name:
            continue
        raw_value = str(item.get('indicator_value') or '').strip()
        is_percent = raw_value.endswith('%')
        value = as_float(raw_value.rstrip('%'), float('nan'))
        yoy_text = str(item.get('yoy') or '').strip()
        yoy = as_float(yoy_text.rstrip('%'), float('nan'))
        if math.isfinite(yoy) and yoy_text.endswith('%'):
            yoy /= 100
        indicators[name] = {
            'value':round(value / 100 if is_percent and math.isfinite(value) else value, 6) if math.isfinite(value) else None,
            'display_value':raw_value or None, 'yoy':yoy,
        }
        if not math.isfinite(indicators[name]['yoy']):
            indicators[name]['yoy'] = None
    return {'period':data.get('report'), 'period_label':data.get('report_txt'), 'currency':data.get('currency'), 'indicators':indicators}

def normalize_eps_forecast(data):
    rows = []
    for item in data.get('items') or []:
        mean = as_float(item.get('forecast_eps_mean'), float('nan'))
        start = int(as_float(item.get('forecast_start_date')))
        if not math.isfinite(mean) or start <= 0:
            continue
        rows.append({
            'timestamp':start, 'date':datetime.fromtimestamp(start, timezone.utc).date().isoformat(),
            'mean':mean, 'median':as_float(item.get('forecast_eps_median')) or None,
            'low':as_float(item.get('forecast_eps_lowest')) or None, 'high':as_float(item.get('forecast_eps_highest')) or None,
            'institution_total':int(as_float(item.get('institution_total'))),
        })
    rows.sort(key=lambda item:item['timestamp'])
    if not rows:
        return {'latest':None, 'revision_30d_pct':None, 'history_count':0}
    latest = rows[-1]
    cutoff = latest['timestamp'] - 30 * 86400
    previous = min(rows, key=lambda item:abs(item['timestamp'] - cutoff))
    revision = (latest['mean'] / previous['mean'] - 1) * 100 if previous['mean'] else None
    return {'latest':latest, 'revision_30d_pct':round(revision, 2) if revision is not None else None, 'history_count':len(rows)}

def normalize_rating_snapshot(latest_data, detail_data):
    candidates = list(nested_dicts({'latest':latest_data, 'detail':detail_data}))
    evaluate = next((item.get('evaluate') for item in candidates if isinstance(item.get('evaluate'), dict) and any(key in item['evaluate'] for key in ('strong_buy', 'buy', 'hold', 'sell'))), {})
    target = next((item.get('target') for item in candidates if isinstance(item.get('target'), (str, int, float)) and as_float(item.get('target')) > 0), None)
    target_detail = next((item.get('target') for item in candidates if isinstance(item.get('target'), dict) and item['target'].get('highest_price')), {})
    recommendation = next((item.get('recommend') for item in candidates if item.get('recommend')), None)
    updated_at = next((item.get('updated_at') for item in candidates if item.get('updated_at')), None)
    total = sum(int(as_float(evaluate.get(key))) for key in ('strong_buy', 'buy', 'hold', 'under', 'sell'))
    return {
        'recommendation':recommendation, 'target_price':as_float(target) or None,
        'target_low':as_float(target_detail.get('lowest_price')) or None, 'target_high':as_float(target_detail.get('highest_price')) or None,
        'ratings':{key:int(as_float(evaluate.get(key))) for key in ('strong_buy', 'buy', 'hold', 'under', 'sell')},
        'coverage_count':total, 'updated_at':updated_at,
    }

def get_research_snapshot(symbol, force=False):
    symbol = normalized_symbol(symbol)
    cached = RESEARCH_CACHE.get(symbol)
    if not force and cached and time.time() - cached['saved_at'] < 6 * 60 * 60:
        return cached['data']
    counter_id = longbridge_counter_id(symbol)
    endpoints = {
        'valuation':('/v1/quote/valuation/detail', {'counter_id':counter_id}),
        'financial':('/v1/quote/financials/latest-report', {'counter_id':counter_id}),
        'forecast':('/v1/quote/forecast-eps', {'counter_id':counter_id}),
        'rating_latest':('/v1/quote/institution-rating-latest', {'counter_id':counter_id}),
        'rating_detail':('/v1/quote/institution-ratings', {'counter_id':counter_id}),
    }
    raw, errors = {}, {}
    with ThreadPoolExecutor(max_workers=5) as pool:
        futures = {name:pool.submit(longbridge_request, path, params) for name, (path, params) in endpoints.items()}
        for name, future in futures.items():
            try:
                raw[name] = future.result()
            except Exception as exc:
                raw[name], errors[name] = {}, str(exc)
    normalizers = {
        'valuation':lambda:normalize_valuation_snapshot(raw['valuation']),
        'financial':lambda:normalize_financial_snapshot(raw['financial']),
        'forecast':lambda:normalize_eps_forecast(raw['forecast']),
        'ratings':lambda:normalize_rating_snapshot(raw['rating_latest'], raw['rating_detail']),
    }
    defaults = {
        'valuation':{'as_of':None, 'currency_symbol':None, 'metrics':{}},
        'financial':{'period':None, 'period_label':None, 'currency':None, 'indicators':{}},
        'forecast':{'latest':None, 'revision_30d_pct':None, 'history_count':0},
        'ratings':{'recommendation':None, 'target_price':None, 'target_low':None, 'target_high':None, 'ratings':{}, 'coverage_count':0, 'updated_at':None},
    }
    sections = {}
    for name, normalizer in normalizers.items():
        try:
            sections[name] = normalizer()
        except Exception as exc:
            sections[name], errors[f'{name}_normalize'] = defaults[name], str(exc)
    available = {
        'valuation':bool(sections['valuation']['metrics']),
        'financial':bool(sections['financial']['indicators']),
        'forecast':sections['forecast']['latest'] is not None,
        'ratings':bool(sections['ratings']['recommendation'] or sections['ratings']['coverage_count']),
    }
    data = {
        'symbol':symbol, **sections, 'available':available,
        'coverage_pct':round(sum(available.values()) / len(available) * 100),
        'complete':all(available.values()), 'errors':errors,
        'source':'longbridge_fundamental_research', 'fetched_at':iso_now(),
    }
    RESEARCH_CACHE[symbol] = {'saved_at':time.time(), 'data':data}
    return data

def research_evidence(snapshot, current_price):
    bull, bear, limitations = [], [], []
    pe = (((snapshot.get('valuation') or {}).get('metrics') or {}).get('pe') or {})
    percentile_value = pe.get('historical_percentile')
    if pe.get('value') is not None:
        text = f"PE(TTM) {pe['value']:.2f} 倍，近5年历史分位 {percentile_value:.1f}%" if percentile_value is not None else f"PE(TTM) {pe['value']:.2f} 倍"
        (bull if percentile_value is not None and percentile_value <= 35 else bear if percentile_value is not None and percentile_value >= 70 else limitations).append(text)
    indicators = ((snapshot.get('financial') or {}).get('indicators') or {})
    revenue_yoy = (indicators.get('operating_revenue') or {}).get('yoy')
    profit_yoy = (indicators.get('net_profit') or {}).get('yoy')
    if revenue_yoy is not None:
        (bull if revenue_yoy > 0 else bear).append(f'最新报告期营业收入同比 {revenue_yoy*100:+.1f}%')
    if profit_yoy is not None:
        (bull if profit_yoy > 0 else bear).append(f'最新报告期净利润同比 {profit_yoy*100:+.1f}%')
    revision = (snapshot.get('forecast') or {}).get('revision_30d_pct')
    if revision is not None:
        (bull if revision > 1 else bear if revision < -1 else limitations).append(f'过去30天一致EPS均值修正 {revision:+.1f}%')
    ratings = snapshot.get('ratings') or {}
    target = as_float(ratings.get('target_price'))
    if target > 0 and current_price > 0:
        upside = (target / current_price - 1) * 100
        (bull if upside > 5 else bear if upside < -5 else limitations).append(f'机构一致目标价较当前价空间 {upside:+.1f}%（覆盖 {ratings.get("coverage_count") or 0}）')
    if snapshot.get('coverage_pct', 0) < 100:
        limitations.append(f"Longbridge研究数据覆盖 {snapshot.get('coverage_pct', 0)}%，缺失部分不参与判断")
    return {'bull':bull[:4], 'bear':bear[:4], 'limitations':limitations[:4]}

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
    req = urllib.request.Request('https://hq.sinajs.cn/list=' + ','.join(codes), headers={'Referer':'https://finance.sina.com.cn', 'User-Agent':'Mozilla/5.0 stock-dashboard/1.0'})
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

def fetch_tencent_prices(symbols):
    codes, mapping = [], {}
    for raw_symbol in symbols:
        symbol = normalized_symbol(raw_symbol)
        if symbol.endswith('.HK'):
            code = 'hk' + symbol[:-3].zfill(5)
        else:
            code = 'us' + symbol
        codes.append(code)
        mapping[code] = symbol
    if not codes:
        return {}
    req = urllib.request.Request(
        'https://qt.gtimg.cn/q=' + ','.join(codes),
        headers={'User-Agent':'Mozilla/5.0 stock-dashboard/1.0', 'Referer':'https://gu.qq.com/'},
    )
    raw = urllib.request.urlopen(req, timeout=10, context=CTX).read().decode('gbk')
    prices = {}
    for line in raw.strip().split(';'):
        if '=' not in line:
            continue
        code = line.split('=')[0].strip().removeprefix('v_')
        try:
            parts = line.split('"')[1].split('~')
            price = as_float(parts[3])
        except (IndexError, ValueError):
            continue
        symbol = mapping.get(code)
        if symbol and price > 0:
            prices[symbol] = price
    return prices

def fetch_market_prices(symbols):
    errors, prices = [], {}
    try:
        prices.update(fetch_sina_prices(symbols))
    except Exception as exc:
        errors.append(f'新浪 HTTPS: {exc}')
    missing = [symbol for symbol in symbols if normalized_symbol(symbol) not in prices]
    if missing:
        try:
            prices.update(fetch_tencent_prices(missing))
        except Exception as exc:
            errors.append(f'腾讯 HTTPS: {exc}')
    if not prices and errors:
        raise RuntimeError('；'.join(errors))
    return prices

def normalize_longbridge_candlesticks(rows):
    def field(obj, name):
        return obj.get(name) if isinstance(obj, dict) else getattr(obj, name, None)
    points = []
    for row in rows or []:
        timestamp = field(row, 'timestamp') or field(row, 'time')
        if isinstance(timestamp, datetime):
            day = timestamp.date().isoformat()
        else:
            day = str(timestamp or '')[:10]
        close = as_float(field(row, 'close'))
        if len(day) != 10 or close <= 0:
            continue
        points.append({
            'date':day, 'open':as_float(field(row, 'open')), 'close':close,
            'high':as_float(field(row, 'high')), 'low':as_float(field(row, 'low')),
            'volume':as_float(field(row, 'volume')), 'turnover':as_float(field(row, 'turnover')),
        })
    by_date = {item['date']:item for item in points}
    return [by_date[day] for day in sorted(by_date)]

def fetch_longbridge_history(symbol):
    try:
        from longport.openapi import AdjustType, Period, TradeSessions
    except (ImportError, OSError) as exc:
        raise RuntimeError('Longport历史行情SDK不可用') from exc
    end = datetime.now(timezone(timedelta(hours=8))).date()
    start = end - timedelta(days=520)
    rows = longbridge_quote_context().history_candlesticks_by_date(
        longbridge_symbol(symbol), Period.Day, AdjustType.ForwardAdjust,
        start, end, TradeSessions.Intraday,
    )
    points = normalize_longbridge_candlesticks(rows)
    if len(points) < 60:
        raise ValueError(f'{normalized_symbol(symbol)} Longbridge历史行情不足')
    return points

def tencent_history_symbol(symbol):
    symbol = normalized_symbol(symbol)
    if symbol == 'HSI.HK':
        return 'hkHSI'
    if symbol.endswith('.HK'):
        return 'hk' + symbol[:-3].zfill(5)
    req = urllib.request.Request(
        'https://qt.gtimg.cn/q=us' + symbol,
        headers={'User-Agent':'Mozilla/5.0 stock-dashboard/1.0', 'Referer':'https://gu.qq.com/'},
    )
    raw = urllib.request.urlopen(req, timeout=10, context=CTX).read().decode('gbk')
    try:
        provider_symbol = raw.split('"')[1].split('~')[2]
    except IndexError as exc:
        raise ValueError(f'{symbol} 无法识别美股交易所') from exc
    if not provider_symbol or '.' not in provider_symbol:
        raise ValueError(f'{symbol} 缺少交易所后缀')
    return 'us' + provider_symbol

def technical_snapshot(points):
    closes = [as_float(item.get('close')) for item in points if as_float(item.get('close')) > 0]
    if len(closes) < 60:
        raise ValueError('真实日线少于60个交易日')
    price = closes[-1]
    sma = lambda days: statistics.fmean(closes[-min(days, len(closes)):])
    momentum = lambda days: price / closes[-min(days + 1, len(closes))] - 1
    returns = [math.log(closes[i] / closes[i - 1]) for i in range(max(1, len(closes) - 20), len(closes))]
    volatility = statistics.pstdev(returns) * math.sqrt(252) if len(returns) > 1 else 0
    true_ranges = []
    clean_points = [item for item in points if as_float(item.get('close')) > 0]
    start = max(0, len(clean_points) - 15)
    for index in range(start, len(clean_points)):
        item = clean_points[index]
        high, low = as_float(item.get('high')), as_float(item.get('low'))
        previous = as_float(clean_points[index - 1].get('close')) if index > 0 else as_float(item.get('open'))
        true_ranges.append(max(high - low, abs(high - previous), abs(low - previous)))
    peak, max_drawdown = closes[0], 0.0
    for close in closes:
        peak = max(peak, close)
        max_drawdown = min(max_drawdown, close / peak - 1)
    sma20, sma50, sma200 = sma(20), sma(50), sma(200)
    return {
        'price':round(price, 4), 'sma20':round(sma20, 4), 'sma50':round(sma50, 4), 'sma200':round(sma200, 4),
        'momentum_20d':round(momentum(20), 6), 'momentum_60d':round(momentum(60), 6), 'momentum_120d':round(momentum(120), 6),
        'volatility_20d':round(volatility, 6), 'atr_14d':round(statistics.fmean(true_ranges[-14:]), 4),
        'current_drawdown_120d':round(price / max(closes[-min(120, len(closes)):]) - 1, 6),
        'max_drawdown_period':round(max_drawdown, 6),
        'trend':'上升' if price > sma50 > sma200 else '下降' if price < sma50 < sma200 else '震荡',
        'sample_days':len(closes),
    }

def percentile(values, probability):
    clean = sorted(as_float(value) for value in values if math.isfinite(as_float(value, float('nan'))))
    if not clean:
        return None
    position = clamp(probability, 0, 1) * (len(clean) - 1)
    lower, upper = math.floor(position), math.ceil(position)
    if lower == upper:
        return clean[lower]
    return clean[lower] + (clean[upper] - clean[lower]) * (position - lower)

def daily_returns(points):
    by_date = {str(item.get('date')):as_float(item.get('close')) for item in points if as_float(item.get('close')) > 0}
    dates = sorted(by_date)
    return {dates[index]:by_date[dates[index]] / by_date[dates[index - 1]] - 1 for index in range(1, len(dates))}

def realized_volatility_series(points, window):
    values = list(daily_returns(points).values())
    return [statistics.pstdev(values[index-window:index]) * math.sqrt(252) for index in range(window, len(values) + 1) if window > 1]

def market_regime_from_history(history, market, breadth=None, sentiment=None):
    points = history.get('points') or []
    if len(points) < 200:
        return {'regime':'unavailable', 'score':None, 'risk_multiplier':None, 'confidence':0, 'data_coverage':0, 'signals':{}, 'quality':{'warnings':['基准历史少于200个交易日']}}
    technical = technical_snapshot(points)
    trend_score = (20 if technical['price'] > technical['sma200'] else 0) + (20 if technical['sma50'] > technical['sma200'] else 0)
    vol20_series, vol60_series = realized_volatility_series(points, 20), realized_volatility_series(points, 60)
    current20 = vol20_series[-1] if vol20_series else None
    current60 = vol60_series[-1] if vol60_series else None
    history_vol = vol60_series[:-1]
    if history_vol and current60 is not None:
        rank = sum(value <= current60 for value in history_vol) / len(history_vol)
        vol_score = 25 if rank <= .40 else 0 if rank >= .80 else 25 * (.80 - rank) / .40
    else:
        rank, vol_score = None, 12.5
    breadth_available = isinstance(breadth, dict) and as_float(breadth.get('rise')) + as_float(breadth.get('fall')) > 0
    if breadth_available:
        rise, fall, flat = as_float(breadth.get('rise')), as_float(breadth.get('fall')), as_float(breadth.get('flat'))
        breadth_ratio = rise / (rise + fall)
        breadth_score = clamp((breadth_ratio - .35) / .30 * 25, 0, 25)
    else:
        rise = fall = flat = 0
        breadth_ratio, breadth_score = None, 12.5
    sentiment_available = isinstance(sentiment, dict) and any(sentiment.get(key) is not None for key in ('market_temperature', 'large_net_flow'))
    sentiment_score = 5.0
    if sentiment_available:
        temperature = sentiment.get('market_temperature')
        flow = sentiment.get('large_net_flow')
        sentiment_score = (clamp(as_float(temperature, 50) / 100 * 5, 0, 5) if temperature is not None else 2.5)
        sentiment_score += (5 if as_float(flow) > 0 else 0 if as_float(flow) < 0 else 2.5)
    score = round(trend_score + vol_score + breadth_score + sentiment_score, 1)
    coverage = .65 + (.25 if breadth_available else 0) + (.10 if sentiment_available else 0)
    regime = 'aggressive' if score >= 65 else 'defensive' if score <= 40 else 'balanced'
    return {
        'regime':regime, 'score':score, 'risk_multiplier':{'aggressive':1.0, 'balanced':.85, 'defensive':.65}[regime],
        'confidence':round(coverage, 2), 'data_coverage':round(coverage, 2),
        'signals':{
            'trend':{'score':trend_score, 'above_ma200':technical['price'] > technical['sma200'], 'ma50_above_ma200':technical['sma50'] > technical['sma200'], 'as_of':history.get('as_of')},
            'volatility':{'score':round(vol_score, 1), 'annualized_20d':round(current20, 4) if current20 is not None else None, 'annualized_60d':round(current60, 4) if current60 is not None else None, 'historical_percentile':round(rank, 3) if rank is not None else None},
            'breadth':{'score':round(breadth_score, 1), 'rise':rise, 'fall':fall, 'flat':flat, 'ratio':round(breadth_ratio, 3) if breadth_ratio is not None else None},
            'sentiment':{'score':round(sentiment_score, 1), 'large_net_flow':(sentiment or {}).get('large_net_flow'), 'market_temperature':(sentiment or {}).get('market_temperature')},
        },
        'quality':{'warnings':[message for condition, message in ((not breadth_available, '市场宽度暂缺，按中性分处理'), (not sentiment_available, '市场温度与资金流暂缺，按中性分处理')) if condition]},
        'market':market,
    }

def get_market_regime(force=False):
    if not force and MARKET_CACHE['data'] and time.time() - MARKET_CACHE['saved_at'] < 15 * 60:
        return MARKET_CACHE['data']
    policy = investment_policy()
    markets = {}
    for market, benchmark in policy['benchmark_by_market'].items():
        try:
            history = fetch_symbol_history(benchmark, force=force)
            markets[market] = market_regime_from_history(history, market)
        except Exception as exc:
            markets[market] = {'regime':'unavailable', 'score':None, 'risk_multiplier':None, 'confidence':0, 'data_coverage':0, 'signals':{}, 'quality':{'warnings':[str(exc)]}, 'market':market}
    available = [item for item in markets.values() if item.get('score') is not None]
    weighted_score = statistics.fmean(item['score'] for item in available) if available else None
    data = {
        'model_version':'market-regime-v1', 'model_status':'shadow', 'markets':markets,
        'portfolio_weighted_regime':('aggressive' if weighted_score is not None and weighted_score >= 65 else 'defensive' if weighted_score is not None and weighted_score <= 40 else 'balanced' if weighted_score is not None else 'unavailable'),
        'policy_version':policy['version'], 'source':'longbridge_history_with_public_fallback', 'fetched_at':iso_now(),
    }
    MARKET_CACHE.update({'saved_at':time.time(), 'data':data})
    return data

def fetch_tencent_history(symbol):
    provider_symbol = tencent_history_symbol(symbol)
    url = f'https://web.ifzq.gtimg.cn/appstock/app/kline/kline?param={provider_symbol},day,,,320'
    req = urllib.request.Request(url, headers={'User-Agent':'Mozilla/5.0 stock-dashboard/1.0', 'Accept':'application/json', 'Referer':'https://gu.qq.com/'})
    payload = json.loads(urllib.request.urlopen(req, timeout=12, context=CTX).read())
    item = (payload.get('data') or {}).get(provider_symbol) or {}
    rows = item.get('qfqday') or item.get('day') or []
    points = []
    for row in rows:
        if len(row) < 6 or as_float(row[2]) <= 0:
            continue
        points.append({'date':str(row[0]), 'open':as_float(row[1]), 'close':as_float(row[2]), 'high':as_float(row[3]), 'low':as_float(row[4]), 'volume':as_float(row[5])})
    if len(points) < 60:
        raise ValueError(f'{symbol} 真实历史行情不足')
    return points, provider_symbol

def fetch_symbol_history(symbol, force=False):
    symbol = normalized_symbol(symbol)
    cached = HISTORY_CACHE.get(symbol)
    if not force and cached and time.time() - cached['saved_at'] < 6 * 60 * 60:
        return cached['data']
    source_error = None
    try:
        points = fetch_longbridge_history(symbol)
        provider_symbol = longbridge_symbol(symbol)
        source, source_status = 'Longbridge前复权日线', 'live'
    except Exception as exc:
        source_error = str(exc)
        points, provider_symbol = fetch_tencent_history(symbol)
        source, source_status = '腾讯证券前复权日线（降级）', 'degraded'
    data = {
        'symbol':symbol, 'provider_symbol':provider_symbol, 'as_of':points[-1]['date'],
        'source':source, 'source_status':source_status, 'source_error':source_error,
        'points':points[-250:], 'technical':technical_snapshot(points[-250:]),
    }
    HISTORY_CACHE[symbol] = {'saved_at':time.time(), 'data':data}
    return data

def utc_timestamp(day, end=False):
    value = datetime(day.year, day.month, day.day, tzinfo=timezone.utc)
    return int(value.timestamp()) + (86399 if end else 0)

def month_end_dates(start, end):
    dates, year, month = [], start.year, start.month
    while (year, month) <= (end.year, end.month):
        last = date(year, month, calendar.monthrange(year, month)[1])
        dates.append(min(last, end))
        month += 1
        if month == 13:
            year, month = year + 1, 1
    return dates

def max_drawdown_from_returns(values):
    peak, drawdown = 1.0, 0.0
    for value in values:
        wealth = max(0.0, 1 + as_float(value))
        peak = max(peak, wealth)
        drawdown = min(drawdown, wealth / peak - 1 if peak else 0)
    return drawdown

def classify_cash_flow(name):
    value = str(name or '').lower()
    if any(word in value for word in ('入金', '存入', '转入资金', 'deposit')):
        return 'deposit'
    if any(word in value for word in ('出金', '提款', '提取', '转出资金', 'withdraw')):
        return 'withdrawal'
    if any(word in value for word in ('分红', '股息', '利息收入', '资金入账', 'dividend')):
        return 'income'
    if any(word in value for word in ('费用', '收费', '融资利息', '手续费', 'fee')):
        return 'cost'
    if any(word in value for word in ('兑换', '新股', '认购', '申购')):
        return 'internal'
    return 'other'

def xirr(cashflows):
    clean = sorted((day, as_float(amount)) for day, amount in cashflows if as_float(amount) != 0)
    if len(clean) < 2 or not any(amount < 0 for _, amount in clean) or not any(amount > 0 for _, amount in clean):
        return None
    start = clean[0][0]
    def npv(rate):
        return sum(amount / ((1 + rate) ** ((day - start).days / 365)) for day, amount in clean)
    low, high = -.9999, 10.0
    low_value, high_value = npv(low), npv(high)
    while low_value * high_value > 0 and high < 10000:
        high *= 10
        high_value = npv(high)
    if low_value * high_value > 0:
        return None
    for _ in range(120):
        middle = (low + high) / 2
        value = npv(middle)
        if abs(value) < .000001:
            return middle
        if low_value * value <= 0:
            high, high_value = middle, value
        else:
            low, low_value = middle, value
    return (low + high) / 2

def cash_flow_summary(start_day, end_day, fx_to_cny, target_currency):
    data = longbridge_request('/v1/asset/cashflow', {
        'start_time':str(utc_timestamp(start_day)), 'end_time':str(utc_timestamp(end_day, True)),
        'business_type':'1', 'page':'1', 'size':'10000',
    })
    totals, external = {}, []
    target_rate = fx_to_cny.get(target_currency)
    for item in data.get('list', []):
        category = classify_cash_flow(item.get('transaction_flow_name'))
        currency = str(item.get('currency') or '').upper()
        amount = abs(as_float(item.get('balance')))
        key = f'{category}_by_currency'
        totals.setdefault(key, {})[currency] = totals.setdefault(key, {}).get(currency, 0) + amount
        if category in {'deposit', 'withdrawal'}:
            source_rate = fx_to_cny.get(currency)
            if source_rate and target_rate:
                converted = amount * source_rate / target_rate
                flow_day = datetime.fromtimestamp(int(item.get('business_time')), timezone.utc).date()
                external.append((flow_day, -converted if category == 'deposit' else converted))
    return {'count':len(data.get('list', [])), 'totals':totals, 'external_flows':external}

def get_performance_snapshot(force=False, today=None):
    cache = PERFORMANCE_CACHE
    if not force and cache['data'] and time.time() - cache['saved_at'] < 30 * 60:
        return cache['data']
    end_day = today or date.today()
    start_day = date(end_day.year, 1, 1)
    period_ends = month_end_dates(start_day, end_day)
    def fetch_period(period_start, period_end):
        summary = longbridge_request('/v1/portfolio/profit-analysis-summary', {'start':str(utc_timestamp(period_start)), 'end':str(utc_timestamp(period_end, True))})
        return period_start, period_end, summary
    latest = longbridge_request('/v1/portfolio/profit-analysis-summary', {'start':str(utc_timestamp(start_day)), 'end':str(utc_timestamp(end_day, True))})
    monthly = []
    for index, period_end in enumerate(period_ends):
        time.sleep(.35)
        period_start = date(period_end.year, period_end.month, 1)
        try:
            monthly.append(fetch_period(period_start, period_end))
        except Exception:
            continue
    monthly.sort(key=lambda item:item[1])
    linked, points = 1.0, []
    for _, period_end, summary in monthly:
        linked *= 1 + as_float(summary.get('sum_profit_rate'))
        points.append({'date':period_end.isoformat(), 'return':linked - 1, 'period_return':as_float(summary.get('sum_profit_rate'))})
    ytd_return = as_float(latest.get('sum_profit_rate'))
    elapsed_days = max(1, (end_day - start_day).days + 1)
    annualized = (1 + ytd_return) ** (365 / elapsed_days) - 1 if ytd_return > -1 and elapsed_days >= 30 else None
    benchmark = fetch_symbol_history('SPY')
    benchmark_points = benchmark['points']
    first = next((item for item in benchmark_points if item['date'] >= start_day.isoformat()), benchmark_points[0])
    benchmark_series = []
    for point in points:
        eligible = [item for item in benchmark_points if item['date'] <= point['date']]
        close = eligible[-1]['close'] if eligible else first['close']
        benchmark_series.append({'date':point['date'], 'return':close / first['close'] - 1})
    spy_ytd = benchmark_series[-1]['return'] if benchmark_series else None
    currency = str(latest.get('currency') or 'USD').upper()
    flows = {'count':0, 'totals':{}, 'external_flows':[], 'available':False}
    try:
        exchange_data = longbridge_request('/v1/asset/exchange_rates')
        flows.update(cash_flow_summary(start_day, end_day, rates_to_cny(exchange_data), currency))
        flows['available'] = True
    except Exception as exc:
        app.logger.error('performance_cashflow_failed: %s', exc)
    xirr_flows = [(start_day, -as_float(latest.get('initial_asset_value'))), *flows['external_flows'], (end_day, as_float(latest.get('ending_asset_value')))]
    money_weighted = xirr(xirr_flows) if flows['available'] else None
    linked_return = linked - 1 if points else None
    result = {
        'period':{'start':str(latest.get('start_date') or start_day), 'end':str(latest.get('end_date') or end_day), 'days':elapsed_days},
        'currency':currency, 'current_total_asset':as_float(latest.get('current_total_asset')),
        'initial_asset_value':as_float(latest.get('initial_asset_value')), 'ending_asset_value':as_float(latest.get('ending_asset_value')),
        'invest_amount':as_float(latest.get('invest_amount')), 'total_profit':as_float(latest.get('sum_profit')),
        'ytd_return':ytd_return, 'annualized_pace':annualized, 'goal_return':.20,
        'monthly_linked_return':linked_return, 'xirr':money_weighted,
        'goal_gap':None if annualized is None else .20 - annualized,
        'monthly_max_drawdown':max_drawdown_from_returns([item['return'] for item in points]),
        'benchmark':{'symbol':'SPY', 'ytd_return':spy_ytd, 'excess_return':None if spy_ytd is None else ytd_return - spy_ytd},
        'points':points, 'benchmark_points':benchmark_series,
        'cash_flow_summary':{'available':flows['available'], 'count':flows['count'], 'totals':flows['totals'], 'external_flow_count':len(flows['external_flows'])},
        'source':'LongPort profit-analysis-summary', 'benchmark_source':benchmark['source'],
        'methodology':'账户YTD直接采用长桥区间总收益率；月度链式收益按自然月连接，是月频TWR近似；XIRR只把明确入金/出金作为外部现金流，分红、换汇和证券交易不会当作入金。',
        'fetched_at':time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
    }
    cache.update({'saved_at':time.time(), 'data':result})
    return result

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

def build_account_snapshot(stock_data, balance_data, exchange_data, prices, fetched_at=None, source_errors=None, orders_data=None, optional_source_errors=None, price_source='sina_tencent_fallback', price_source_status='degraded_until_longbridge_quote_sdk', price_details=None):
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
    init_margin_cny = maintenance_margin_cny = margin_call_cny = 0.0
    max_finance_cny = remaining_finance_cny = 0.0
    risk_levels = []
    missing_conversion = []
    for balance in balances:
        currency, rate = str(balance.get('currency') or '').upper(), fx.get(str(balance.get('currency') or '').upper())
        if rate is None:
            missing_conversion.append(currency)
            continue
        net_assets_cny += as_float(balance.get('net_assets')) * rate
        buy_power_cny += as_float(balance.get('buy_power')) * rate
        init_margin_cny += as_float(balance.get('init_margin')) * rate
        maintenance_margin_cny += as_float(balance.get('maintenance_margin')) * rate
        margin_call_cny += as_float(balance.get('margin_call')) * rate
        max_finance_cny += as_float(balance.get('max_finance_amount')) * rate
        remaining_finance_cny += as_float(balance.get('remaining_finance_amount')) * rate
        if balance.get('risk_level') is not None:
            risk_levels.append(str(balance.get('risk_level')))
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
    primary_balance = balances[0] if len(balances) == 1 else None
    errors = source_errors or {}
    pending_orders = normalize_pending_orders(orders_data or {})
    return {
        'source':'longbridge_openapi', 'fetched_at':fetched_at or time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
        'positions':positions, 'prices':prices, 'fx_to_cny':fx,
        'price_source':price_source, 'price_source_status':price_source_status, 'price_details':price_details or {},
        'pending_orders':pending_orders, 'order_data_complete':'orders' not in (optional_source_errors or {}),
        'account':{
            'net_assets_cny':round(net_assets_cny, 2), 'total_cash_cny':round(total_cash_cny, 2),
            'buy_power_cny':round(buy_power_cny, 2), 'cash_by_currency':cash_by_currency,
            'account_currency':str(primary_balance.get('currency') or '').upper() if primary_balance else 'CNY',
            'net_assets_native':as_float(primary_balance.get('net_assets')) if primary_balance else round(net_assets_cny, 2),
            'total_cash_native':as_float(primary_balance.get('total_cash')) if primary_balance else round(total_cash_cny, 2),
            'buy_power_native':as_float(primary_balance.get('buy_power')) if primary_balance else round(buy_power_cny, 2),
            'risk_levels':sorted(set(risk_levels)), 'init_margin_cny':round(init_margin_cny, 2),
            'maintenance_margin_cny':round(maintenance_margin_cny, 2), 'margin_call_cny':round(margin_call_cny, 2),
            'max_finance_amount_cny':round(max_finance_cny, 2), 'remaining_finance_amount_cny':round(remaining_finance_cny, 2),
            'balances':balances,
        },
        'complete':not (missing_prices or missing_fx or missing_conversion or errors),
        'missing_prices':missing_prices, 'missing_currencies':sorted(set(missing_fx + missing_conversion)),
        'source_errors':errors, 'optional_source_errors':optional_source_errors or {},
    }

def get_account_snapshot(force=False):
    cache = ACCOUNT_CACHE
    if not force and cache['snapshot'] and time.time() - cache['saved_at'] < 15:
        return cache['snapshot']
    source_errors = {}
    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = {
            'positions':pool.submit(longbridge_request, '/v1/asset/stock'),
            'account':pool.submit(longbridge_request, '/v1/asset/account'),
            'exchange_rates':pool.submit(longbridge_request, '/v1/asset/exchange_rates'),
            'orders':pool.submit(longbridge_request, '/v1/trade/order/today'),
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
    optional_errors = {}
    prices, price_details = {}, {}
    price_source, price_source_status = 'longbridge_quote_sdk', 'live'
    try:
        prices, price_details = fetch_longbridge_sdk_prices(symbols)
    except Exception as exc:
        optional_errors['longbridge_quote_sdk'] = str(exc)
    sdk_price_count = len(prices)
    missing = [symbol for symbol in symbols if normalized_symbol(symbol) not in prices]
    if missing:
        price_source = 'longbridge_quote_sdk' if sdk_price_count else 'unavailable'
        price_source_status = 'partial_degraded' if sdk_price_count else 'unavailable'
        try:
            fallback_prices = fetch_market_prices(missing)
            prices.update(fallback_prices)
            for symbol in fallback_prices:
                price_details[symbol] = {'source':'sina_tencent_fallback', 'session':'unknown', 'timestamp':iso_now()}
            if sdk_price_count == 0:
                price_source, price_source_status = 'sina_tencent_fallback', 'degraded_until_longbridge_quote_sdk'
            elif fallback_prices:
                price_source, price_source_status = 'longbridge_with_third_party_fallback', 'partial_degraded'
        except Exception as exc:
            source_errors['prices'] = str(exc)
    if 'orders' in source_errors:
        optional_errors['orders'] = source_errors.pop('orders')
    snapshot = build_account_snapshot(
        stock_data, balance_data, exchange_data, prices, source_errors=source_errors,
        orders_data=results.get('orders'), optional_source_errors=optional_errors,
        price_source=price_source, price_source_status=price_source_status, price_details=price_details,
    )
    cache.update({'saved_at':time.time(), 'snapshot':snapshot})
    return snapshot

def pearson(left, right):
    if len(left) != len(right) or len(left) < 2:
        return None
    left_mean, right_mean = statistics.fmean(left), statistics.fmean(right)
    numerator = sum((a-left_mean) * (b-right_mean) for a, b in zip(left, right))
    denominator = math.sqrt(sum((a-left_mean)**2 for a in left) * sum((b-right_mean)**2 for b in right))
    return numerator / denominator if denominator else None

def portfolio_risk_from_histories(snapshot, histories, policy=None):
    policy = policy or investment_policy()
    positions = snapshot.get('positions') or []
    net_assets = as_float((snapshot.get('account') or {}).get('net_assets_cny'))
    signature_payload = {
        'policy_version':policy.get('version'),
        'positions':[(item.get('symbol'), item.get('quantity'), item.get('price'), item.get('fx_to_cny')) for item in positions],
    }
    snapshot_id = hashlib.sha256(json.dumps(signature_payload, sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:24]
    warnings, returns_by_symbol = [], {}
    covered_value = 0.0
    for item in positions:
        symbol = normalized_symbol(item.get('symbol'))
        history = histories.get(symbol)
        if history and len(history.get('points') or []) >= 60:
            returns_by_symbol[symbol] = daily_returns(history['points'])
            covered_value += abs(as_float(item.get('market_value_cny')))
            if history.get('source_status') == 'degraded':
                warnings.append(f"{symbol} 历史日线已降级到腾讯：{history.get('source_error') or 'Longbridge历史接口不可用'}")
        else:
            warnings.append(f'{symbol} 历史行情不足，未参与协方差计算')
    gross_value = sum(abs(as_float(item.get('market_value_cny'))) for item in positions)
    net_value = sum(as_float(item.get('market_value_cny')) for item in positions)
    weights = {normalized_symbol(item.get('symbol')):as_float(item.get('market_value_cny')) / net_assets for item in positions if net_assets}
    covered_symbols = [symbol for symbol in weights if symbol in returns_by_symbol]
    common_dates = sorted(set.intersection(*(set(returns_by_symbol[symbol]) for symbol in covered_symbols))) if covered_symbols else []
    common_dates = common_dates[-252:]
    portfolio_returns = [sum(weights[symbol] * returns_by_symbol[symbol][day] for symbol in covered_symbols) for day in common_dates]
    portfolio_vol = statistics.pstdev(portfolio_returns) * math.sqrt(252) if len(portfolio_returns) > 1 else None
    var95_threshold = percentile(portfolio_returns, .05)
    var99_threshold = percentile(portfolio_returns, .01)
    tail95 = [value for value in portfolio_returns if var95_threshold is not None and value <= var95_threshold]
    historical_var95 = max(0.0, -var95_threshold) if var95_threshold is not None else None
    historical_var99 = max(0.0, -var99_threshold) if var99_threshold is not None else None
    cvar95 = max(0.0, -statistics.fmean(tail95)) if tail95 else None
    wealth, peak, max_dd, peak_date, trough_date, running_peak_date = 1.0, 1.0, 0.0, None, None, None
    for day, value in zip(common_dates, portfolio_returns):
        wealth *= 1 + value
        if wealth > peak:
            peak, running_peak_date = wealth, day
        drawdown = wealth / peak - 1 if peak else 0
        if drawdown < max_dd:
            max_dd, peak_date, trough_date = drawdown, running_peak_date, day
    correlations, diversifiers = [], []
    for index, left_symbol in enumerate(covered_symbols):
        for right_symbol in covered_symbols[index+1:]:
            dates = sorted(set(returns_by_symbol[left_symbol]) & set(returns_by_symbol[right_symbol]))
            item = {'left':left_symbol, 'right':right_symbol}
            for window in (60, 252):
                sample = dates[-window:]
                value = pearson([returns_by_symbol[left_symbol][day] for day in sample], [returns_by_symbol[right_symbol][day] for day in sample])
                item[f'correlation_{window}d'] = round(value, 3) if value is not None else None
                item[f'sample_days_{window}d'] = len(sample)
            if item['correlation_60d'] is not None and item['correlation_60d'] > .75:
                correlations.append(item)
            elif item['correlation_60d'] is not None and item['correlation_60d'] < -.50:
                diversifiers.append(item)
    risk_contributions = []
    if len(common_dates) > 1 and covered_symbols:
        vectors = {symbol:[returns_by_symbol[symbol][day] for day in common_dates] for symbol in covered_symbols}
        covariance = {}
        for left in covered_symbols:
            for right in covered_symbols:
                left_mean, right_mean = statistics.fmean(vectors[left]), statistics.fmean(vectors[right])
                covariance[left, right] = sum((a-left_mean)*(b-right_mean) for a, b in zip(vectors[left], vectors[right])) / len(common_dates)
        portfolio_variance = sum(weights[left] * weights[right] * covariance[left, right] for left in covered_symbols for right in covered_symbols)
        for symbol in covered_symbols:
            marginal = sum(covariance[symbol, other] * weights[other] for other in covered_symbols)
            contribution = weights[symbol] * marginal / portfolio_variance if portfolio_variance > 0 else 0
            risk_contributions.append({'symbol':symbol, 'weight_pct':round(weights[symbol]*100, 2), 'variance_contribution_pct':round(contribution*100, 2)})
        risk_contributions.sort(key=lambda item:abs(item['variance_contribution_pct']), reverse=True)
    def grouped_weights(key_fn):
        grouped = {}
        for item in positions:
            key = key_fn(item)
            grouped[key] = grouped.get(key, 0) + as_float(item.get('market_value_cny')) / (net_assets or 1)
        return [{'key':key, 'weight_pct':round(value*100, 2)} for key, value in sorted(grouped.items(), key=lambda pair:abs(pair[1]), reverse=True)]
    position_weights = sorted((abs(as_float(item.get('market_value_cny'))) / (net_assets or 1) for item in positions), reverse=True)
    sector_weights = grouped_weights(lambda item:str(item.get('sector') or '未分类'))
    company_weights = grouped_weights(lambda item:company_group_symbol(item.get('symbol')))
    currency_weights = grouped_weights(lambda item:str(item.get('currency') or '未知'))
    rebalance = {'status':'not_configured', 'drift':[]}
    if policy.get('target_bands'):
        rebalance = {'status':'calculated', 'drift':[]}
        for band in policy['target_bands']:
            current = next((item['weight_pct']/100 for item in grouped_weights(lambda item:normalized_symbol(item.get('symbol'))) if item['key'] == normalized_symbol(band.get('key'))), 0) if band.get('type') == 'symbol' else None
            rebalance['drift'].append({'key':band.get('key'), 'type':band.get('type'), 'current_pct':round(current*100, 2) if current is not None else None, 'target_pct':round(as_float(band.get('target_pct'))*100, 2), 'status':'unsupported_group' if current is None else 'below' if current < as_float(band.get('min_pct')) else 'above' if current > as_float(band.get('max_pct')) else 'within'})
    if any(item.get('market') == 'HK' for item in positions) and any(item.get('market') != 'HK' for item in positions):
        warnings.append('美港股收盘时点不同，跨市场相关性存在时差偏差')
    if positions:
        warnings.append('暂无历史汇率序列：波动率基于本币股票收益与当前权重，未覆盖汇率历史风险')
    account = snapshot.get('account') or {}
    if as_float(account.get('margin_call_cny')) > 0:
        warnings.append(f"账户存在追缴保证金要求 ¥{as_float(account.get('margin_call_cny')):,.2f}，禁止新增风险")
    if account.get('risk_levels'):
        warnings.append('Longbridge账户风险等级：' + '、'.join(account['risk_levels']))
    if snapshot.get('pending_orders'):
        warnings.append(f"当前有 {len(snapshot['pending_orders'])} 笔未完成订单，交易后敞口尚未完全反映在持仓中")
    if snapshot.get('order_data_complete') is False:
        warnings.append('未完成订单接口不可用，订单风险覆盖不完整，禁止新增仓位')
    return {
        'model_version':'portfolio-risk-v1', 'model_status':'shadow', 'policy_version':policy['version'],
        'policy_status':'confirmed' if policy.get('confirmed_by_user') else 'unconfirmed', 'snapshot_id':snapshot_id,
        'risk_currency':policy.get('base_currency', 'CNY'), 'fx_history_covered':False,
        'metrics':{
            'portfolio_vol_annualized':round(portfolio_vol, 4) if portfolio_vol is not None else None,
            'historical_var_95_1d':round(historical_var95, 4) if historical_var95 is not None else None,
            'historical_var_99_1d':round(historical_var99, 4) if historical_var99 is not None else None,
            'historical_cvar_95_1d':round(cvar95, 4) if cvar95 is not None else None,
            'max_drawdown_252d':round(max_dd, 4) if portfolio_returns else None,
            'max_drawdown_peak_date':peak_date, 'max_drawdown_trough_date':trough_date,
            'gross_exposure_pct':round(gross_value/(net_assets or 1)*100, 2), 'net_exposure_pct':round(net_value/(net_assets or 1)*100, 2),
            'cash_pct':round(as_float((snapshot.get('account') or {}).get('total_cash_cny'))/(net_assets or 1)*100, 2),
            'init_margin_cny':round(as_float(account.get('init_margin_cny')), 2),
            'maintenance_margin_cny':round(as_float(account.get('maintenance_margin_cny')), 2),
            'margin_call_cny':round(as_float(account.get('margin_call_cny')), 2),
            'pending_order_count':len(snapshot.get('pending_orders') or []),
        },
        'risk_contributions':risk_contributions, 'correlation_risks':correlations, 'potential_diversifiers':diversifiers,
        'concentration':{'top1_pct':round((position_weights[:1] or [0])[0]*100, 2), 'top5_pct':round(sum(position_weights[:5])*100, 2), 'hhi':round(sum(value*value for value in position_weights), 4), 'sector_weights':sector_weights, 'company_group_weights':company_weights, 'currency_weights':currency_weights},
        'stress_tests':[
            {'scenario':'科技与AI相关持仓下跌20%', 'estimated_portfolio_impact_pct':round(-.20 * sum(item['weight_pct'] for item in sector_weights if item['key'] in {'科技','半导体','AI'}), 2)},
            {'scenario':'美元及港币兑人民币下跌5%', 'estimated_portfolio_impact_pct':round(-.05 * sum(item['weight_pct'] for item in currency_weights if item['key'] in {'USD','HKD'}), 2)},
        ],
        'rebalance':rebalance,
        'quality':{'history_days':len(common_dates), 'position_coverage_pct':round(covered_value/(gross_value or 1)*100, 2), 'fx_history_covered':False, 'order_data_complete':snapshot.get('order_data_complete', False), 'history_sources':{symbol:{'source':history.get('source'), 'status':history.get('source_status')} for symbol, history in histories.items()}, 'cross_market_lag_warning':any('收盘时点' in item for item in warnings), 'warnings':warnings},
        'source':'longbridge_account+longbridge_history_with_public_fallback', 'fetched_at':iso_now(),
    }

def get_portfolio_risk(force=False, snapshot=None):
    snapshot = snapshot or get_account_snapshot(force=force)
    policy = investment_policy()
    signature = hashlib.sha256(json.dumps({'policy':policy, 'positions':[(item.get('symbol'), item.get('quantity'), item.get('price'), item.get('fx_to_cny')) for item in snapshot.get('positions', [])]}, sort_keys=True).encode()).hexdigest()
    if not force and PORTFOLIO_RISK_CACHE['data'] and PORTFOLIO_RISK_CACHE['signature'] == signature and time.time() - PORTFOLIO_RISK_CACHE['saved_at'] < 30 * 60:
        return PORTFOLIO_RISK_CACHE['data']
    histories = {}
    with ThreadPoolExecutor(max_workers=6) as pool:
        futures = {pool.submit(fetch_symbol_history, item['symbol'], force):normalized_symbol(item['symbol']) for item in snapshot.get('positions', [])}
        for future in as_completed(futures):
            try:
                histories[futures[future]] = future.result()
            except Exception:
                continue
    data = portfolio_risk_from_histories(snapshot, histories, policy)
    PORTFOLIO_RISK_CACHE.update({'saved_at':time.time(), 'signature':signature, 'data':data})
    return data

def company_group_symbol(symbol):
    return DISCOVERY_ALIASES.get(normalized_symbol(symbol), normalized_symbol(symbol))

def live_position_context(snapshot, position):
    invested = sum(as_float(item.get('market_value_cny')) for item in snapshot['positions']) or 1
    position_value = as_float(position.get('market_value_cny'))
    sector_value = sum(as_float(item.get('market_value_cny')) for item in snapshot['positions'] if item.get('sector') == position.get('sector'))
    group = company_group_symbol(position['symbol'])
    company_value = sum(as_float(item.get('market_value_cny')) for item in snapshot['positions'] if company_group_symbol(item['symbol']) == group)
    account = snapshot.get('account') or {}
    symbol_orders = [item for item in snapshot.get('pending_orders', []) if normalized_symbol(item.get('symbol')) == normalized_symbol(position['symbol'])]
    return {
        'position_weight':position_value / invested, 'sector_weight':sector_value / invested,
        'company_weight':company_value / invested, 'company_group':group,
        'cash_ratio':as_float(account.get('total_cash_cny')) / (as_float(account.get('net_assets_cny')) or 1),
        'annual_target':20, 'account_fetched_at':snapshot.get('fetched_at'), 'account_source':snapshot.get('source'),
        'account_risk_levels':account.get('risk_levels') or [], 'margin_call_cny':as_float(account.get('margin_call_cny')),
        'order_data_complete':snapshot.get('order_data_complete', False),
        'pending_orders':symbol_orders, 'pending_buy_quantity':sum(item['quantity']-item['executed_quantity'] for item in symbol_orders if item.get('side') == 'buy'),
        'pending_sell_quantity':sum(item['quantity']-item['executed_quantity'] for item in symbol_orders if item.get('side') == 'sell'),
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

def deterministic_price_plan(technical):
    price = max(as_float(technical.get('price')), .01)
    atr = max(as_float(technical.get('atr_14d')), price * .02)
    sma20, sma50 = as_float(technical.get('sma20'), price), as_float(technical.get('sma50'), price)
    entry = min(price, max(min(sma20, sma50), price - 1.5 * atr))
    structural_stop = min(sma50 - atr, price - 2 * atr)
    stop = max(.01, min(structural_stop, entry - atr))
    target = price + 2 * max(price - stop, atr)
    return {
        'entry_price':round(entry, 2), 'stop_loss':round(stop, 2), 'price_target':round(target, 2),
        'reward_risk_ratio':round((target-price)/max(.01, price-stop), 2),
        'basis':{'entry':'MA20/MA50与1.5倍ATR支撑区间', 'stop':'MA50结构位与2倍ATR取更保守值', 'target':'当前价向上2倍初始风险距离'},
    }

def factor_analysis_from_history(symbol, history, market=None, research=None):
    points = history.get('points') or []
    research = research or {}
    technical = history.get('technical') or technical_snapshot(points)
    market = market or ('HK' if normalized_symbol(symbol).endswith('.HK') else 'US')
    closes = [as_float(item.get('close')) for item in points if as_float(item.get('close')) > 0]
    simple_returns = [closes[index]/closes[index-1]-1 for index in range(1, len(closes))]
    annualized_vol = lambda window:statistics.pstdev(simple_returns[-min(window, len(simple_returns)):])*math.sqrt(252) if len(simple_returns) > 1 else None
    downside = [value for value in simple_returns[-min(252, len(simple_returns)):] if value < 0]
    downside_vol = statistics.pstdev(downside)*math.sqrt(252) if len(downside) > 1 else None
    momentum_60_ex_last5 = closes[-6] / closes[-66] - 1 if len(closes) >= 66 else None
    distance_from_52w_high = closes[-1] / max(closes[-min(252, len(closes)):]) - 1
    momentum_raw = {
        'return_20d':technical.get('momentum_20d'), 'return_60d':technical.get('momentum_60d'),
        'return_120d':technical.get('momentum_120d'), 'above_ma200':technical.get('price', 0) > technical.get('sma200', 0),
        'ma50_above_ma200':technical.get('sma50', 0) > technical.get('sma200', 0),
        'return_60d_ex_last5':round(momentum_60_ex_last5, 6) if momentum_60_ex_last5 is not None else None,
        'distance_from_52w_high':round(distance_from_52w_high, 6),
    }
    low_vol_raw = {
        'volatility_60d':round(annualized_vol(60), 6) if annualized_vol(60) is not None else None,
        'volatility_252d':round(annualized_vol(252), 6) if annualized_vol(252) is not None else None,
        'downside_volatility_252d':round(downside_vol, 6) if downside_vol is not None else None,
        'max_drawdown_252d':technical.get('max_drawdown_period'),
    }
    positive_trend = bool(momentum_raw['above_ma200'] and momentum_raw['ma50_above_ma200'] and as_float(momentum_raw['return_60d']) > 0)
    negative_trend = bool(not momentum_raw['above_ma200'] and as_float(momentum_raw['return_60d']) < 0)
    valuation = research.get('valuation') or {}
    financial = research.get('financial') or {}
    forecast = research.get('forecast') or {}
    ratings = research.get('ratings') or {}
    valuation_metrics = valuation.get('metrics') or {}
    financial_indicators = financial.get('indicators') or {}
    pe = valuation_metrics.get('pe') or {}
    value_raw = {
        'pe_ttm':pe.get('value'), 'pe_historical_percentile':pe.get('historical_percentile'),
        'pe_industry_median':pe.get('industry_median'), 'pb':(valuation_metrics.get('pb') or {}).get('value'),
        'ps':(valuation_metrics.get('ps') or {}).get('value'),
    }
    quality_raw = {
        'report_period':financial.get('period'),
        'revenue_yoy':(financial_indicators.get('operating_revenue') or {}).get('yoy'),
        'net_profit_yoy':(financial_indicators.get('net_profit') or {}).get('yoy'),
        'roe':(financial_indicators.get('roe') or {}).get('value'),
        'net_profit_margin':(financial_indicators.get('net_profit_margin') or {}).get('value'),
        'total_assets':(financial_indicators.get('total_assets') or {}).get('value'),
        'total_debts':(financial_indicators.get('total_debts') or {}).get('value'),
    }
    expectation_raw = {
        'eps_forecast':forecast.get('latest'), 'eps_revision_30d_pct':forecast.get('revision_30d_pct'),
        'recommendation':ratings.get('recommendation'), 'target_price':ratings.get('target_price'),
        'target_upside_pct':round((as_float(ratings.get('target_price')) / closes[-1] - 1) * 100, 2) if as_float(ratings.get('target_price')) > 0 else None,
        'coverage_count':ratings.get('coverage_count'), 'rating_distribution':ratings.get('ratings'),
    }
    def finite_values(values):
        return [as_float(value, float('nan')) for value in values if value is not None and math.isfinite(as_float(value, float('nan')))]

    def average_score(values):
        clean = finite_values(values)
        return round(sum(clean) / len(clean), 2) if clean else None

    return_60d = momentum_raw.get('return_60d')
    momentum_score = average_score([
        clamp(as_float(return_60d) / .20, -1, 1) * 100 if return_60d is not None else None,
        100 if momentum_raw['above_ma200'] else -100,
        100 if momentum_raw['ma50_above_ma200'] else -100,
    ])
    vol_60d, drawdown = low_vol_raw.get('volatility_60d'), low_vol_raw.get('max_drawdown_252d')
    low_vol_score = average_score([
        clamp((.50 - as_float(vol_60d)) / .35, -1, 1) * 100 if vol_60d is not None else None,
        clamp((as_float(drawdown) + .25) / .25, -1, 1) * 100 if drawdown is not None else None,
    ])
    pe_value, pe_percentile, industry_pe = value_raw.get('pe_ttm'), value_raw.get('pe_historical_percentile'), value_raw.get('pe_industry_median')
    value_score = average_score([
        clamp((50 - as_float(pe_percentile)) * 2, -100, 100) if pe_percentile is not None else None,
        clamp((as_float(industry_pe) / as_float(pe_value) - 1) * 200, -100, 100) if as_float(pe_value) > 0 and as_float(industry_pe) > 0 else None,
    ])
    assets, debts = quality_raw.get('total_assets'), quality_raw.get('total_debts')
    quality_score = average_score([
        clamp(as_float(quality_raw['revenue_yoy']) / .25, -1, 1) * 100 if quality_raw.get('revenue_yoy') is not None else None,
        clamp(as_float(quality_raw['net_profit_yoy']) / .35, -1, 1) * 100 if quality_raw.get('net_profit_yoy') is not None else None,
        clamp((as_float(quality_raw['roe']) - .12) / .12, -1, 1) * 100 if quality_raw.get('roe') is not None else None,
        clamp((as_float(quality_raw['net_profit_margin']) - .10) / .20, -1, 1) * 100 if quality_raw.get('net_profit_margin') is not None else None,
        clamp((.60 - as_float(debts) / as_float(assets)) / .40, -1, 1) * 100 if as_float(assets) > 0 and debts is not None else None,
    ])
    distribution = expectation_raw.get('rating_distribution') or {}
    rating_total = sum(as_float(distribution.get(key)) for key in ('strong_buy', 'buy', 'hold', 'under', 'sell'))
    rating_balance = ((2*as_float(distribution.get('strong_buy')) + as_float(distribution.get('buy')) - as_float(distribution.get('under')) - 2*as_float(distribution.get('sell'))) / (2*rating_total) * 100) if rating_total else None
    expectation_score = average_score([
        clamp(as_float(expectation_raw['eps_revision_30d_pct']) / 10, -1, 1) * 100 if expectation_raw.get('eps_revision_30d_pct') is not None else None,
        clamp((as_float(expectation_raw['target_upside_pct']) - 5) / 25, -1, 1) * 100 if expectation_raw.get('target_upside_pct') is not None else None,
        rating_balance,
    ])
    factor_weights = {'momentum':.25, 'value':.20, 'quality':.25, 'low_volatility':.15, 'expectation_revision':.15}
    factor_scores = {'momentum':momentum_score, 'value':value_score, 'quality':quality_score, 'low_volatility':low_vol_score, 'expectation_revision':expectation_score}
    available_map = {key:value is not None for key, value in factor_scores.items()}
    coverage = sum(factor_weights[key] for key, available in available_map.items() if available)
    composite_score = round(sum(factor_scores[key] * factor_weights[key] for key in factor_scores if available_map[key]) / coverage, 2) if coverage else 0
    active = coverage >= .70
    contributions = {key:round((factor_scores[key] or 0) * factor_weights[key] / coverage, 2) if active and available_map[key] else 0 for key in factor_scores}
    missing = [key for key, available in available_map.items() if not available]
    warnings = ['当前没有合格横截面，因此不生成伪造的百分位或Z分数']
    if active:
        warnings.append('绝对区间多因子评分已参与操作评级，但尚未完成横截面IC与样本外收益验收')
    if missing:
        warnings.append('研究证据仍缺失：' + '、'.join(missing))
    if research.get('errors'):
        warnings.append('Longbridge部分研究接口不可用：' + '、'.join(sorted(research['errors'])))
    reason = '覆盖率低于70%，模型保持shadow且不影响最终建议' if not active else '覆盖率达到70%，启用可解释区间评分；横截面Z分数与IC验证仍保持关闭'
    signal = 'positive' if active and composite_score >= 35 else 'negative' if active and composite_score <= -30 else 'neutral'
    return {
        'symbol':normalized_symbol(symbol), 'model_version':'multifactor-v1.1.0', 'model_status':'active_rules' if active else 'shadow', 'snapshot_date':history.get('as_of'),
        'universe':{'market':market, 'name':'.SPX.US' if market == 'US' else 'HSI/HSTECH', 'size':None, 'status':'cross_section_pending'},
        'factors':{
            'momentum':{'research_weight':.25, 'available':available_map['momentum'], 'raw':momentum_raw, 'absolute_score':momentum_score, 'z_score':None, 'percentile':None, 'contribution':contributions['momentum']},
            'value':{'research_weight':.20, 'available':available_map['value'], 'raw':value_raw, 'absolute_score':value_score, 'z_score':None, 'percentile':None, 'contribution':contributions['value']},
            'quality':{'research_weight':.25, 'available':available_map['quality'], 'raw':quality_raw, 'absolute_score':quality_score, 'z_score':None, 'percentile':None, 'contribution':contributions['quality']},
            'low_volatility':{'research_weight':.15, 'available':available_map['low_volatility'], 'raw':low_vol_raw, 'absolute_score':low_vol_score, 'z_score':None, 'percentile':None, 'contribution':contributions['low_volatility']},
            'expectation_revision':{'research_weight':.15, 'available':available_map['expectation_revision'], 'raw':expectation_raw, 'absolute_score':expectation_score, 'z_score':None, 'percentile':None, 'contribution':contributions['expectation_revision']},
        },
        'composite':{'score':composite_score, 'signal':signal, 'technical_observation':'positive' if positive_trend else 'negative' if negative_trend else 'mixed', 'signal_percentile':None, 'confidence':round(coverage * (.55 + min(abs(composite_score), 80) / 200), 2), 'data_coverage':coverage, 'decision_weight':1 if active else 0, 'thresholds':{'overweight':35, 'underweight':-30, 'sell':-60}, 'reason':reason},
        'flow_overlay':{'available':False, 'score_effect':0}, 'price_plan':deterministic_price_plan(technical),
        'quality':{'missing_fields':missing, 'stale_fields':[], 'warnings':warnings},
        'source':history.get('source', 'public_history_fallback') + '+longbridge_research', 'fetched_at':iso_now(),
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
        'as_of': history.get('as_of'), 'source': history.get('source', '公开日线降级源'),
        'source_status':history.get('source_status'),
    }

def fetch_candidate_history(meta):
    history = fetch_symbol_history(meta['symbol'])
    points = [(item['date'], as_float(item['close'])) for item in history.get('points', []) if as_float(item.get('close')) > 0]
    if len(points) < 205:
        raise ValueError(f"{meta['symbol']} historical data is insufficient")
    return {
        'dates':[p[0] for p in points], 'closes':[p[1] for p in points],
        'as_of':points[-1][0], 'source':history.get('source'),
        'source_status':history.get('source_status'), 'source_error':history.get('source_error'),
    }

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
    technical = body.get('technical_data') or {}
    price_plan = deterministic_price_plan(technical) if technical else {'entry_price':round(price*.96, 2), 'stop_loss':round(price*.90, 2), 'price_target':round(price*1.12, 2)}
    if sector == '杠杆ETF' or ret <= -40:
        rating = 'Sell'
    elif technical.get('trend') == '下降' and as_float(technical.get('momentum_60d')) < 0:
        rating = 'Underweight'
    elif ret >= 50 or any('集中度' in flag for flag in risk_flags):
        rating = 'Underweight'
    else:
        rating = 'Hold'
    return {
        'rating': rating,
        'confidence': 45,
        'executive_summary': f"当前收益率 {ret:.1f}%，真实日线趋势为{technical.get('trend', '未知')}。在完整基本面证据接入前，先执行仓位纪律。",
        'bull_case': [
            '当前仍有可用购买力，可以分批执行而不是一次性交易。',
            f'{sector}敞口可与组合其他资产共同评估。',
        ],
        'bear_case': [
            '当前分析只使用持仓、价格和组合权重，尚未验证最新基本面。',
            '历史曲线虽为真实日线，但缺少完整基本面与预期证据，不能单独作为买卖依据。',
        ],
        'position_sizing': '维持或降低现有仓位；新增仓位需通过集中度上限校验。',
        'entry_price': price_plan['entry_price'],
        'stop_loss': price_plan['stop_loss'],
        'price_target': price_plan['price_target'],
        'time_horizon': '1-3个月观察',
        'invalidation_conditions': ['跌破风险价位且投资逻辑没有新增证据支持', '公司或行业基本面出现实质恶化'],
        'change_reason': '首次生成建议，暂无上一条决策可比较。',
        'new_evidence': [],
        'source': 'deterministic_policy_engine',
    }

def deterministic_factor_decision(body, risk_flags, factor_result):
    """Translate a sufficiently covered, auditable factor score into a held-position action."""
    result = fallback_analysis(body, risk_flags)
    composite = (factor_result or {}).get('composite') or {}
    if as_float(composite.get('decision_weight')) <= 0:
        result['decision_score'] = None
        result['decision_basis'] = 'insufficient_factor_coverage'
        return result
    score = clamp(as_float(composite.get('score')), -100, 100)
    previous_rating = str((body.get('previous_decision') or {}).get('rating') or '')
    if score <= -60 and composite.get('technical_observation') == 'negative':
        rating = 'Sell'
    elif score <= (-15 if previous_rating == 'Underweight' else -30):
        rating = 'Underweight'
    elif score >= (20 if previous_rating == 'Overweight' else 35):
        rating = 'Overweight'
    else:
        rating = 'Hold'
    context = body.get('portfolio_context') or {}
    position_weight = as_float(context.get('position_weight'))
    company_weight = as_float(context.get('company_weight'))
    if rating == 'Overweight' and (position_weight >= .18 or company_weight >= .20):
        rating = 'Hold'
    elif rating == 'Hold' and (position_weight >= .22 or company_weight >= .24):
        rating = 'Underweight'

    labels = {'momentum':'动量', 'value':'估值', 'quality':'盈利质量', 'low_volatility':'波动风险', 'expectation_revision':'盈利预期'}
    ranked = sorted(
        ((key, as_float(item.get('contribution'))) for key, item in ((factor_result or {}).get('factors') or {}).items() if item.get('available')),
        key=lambda item:abs(item[1]), reverse=True,
    )
    evidence = [f'{labels.get(key, key)}因子贡献 {value:+.1f} 分' for key, value in ranked[:3]]
    bull_case = [f'{labels.get(key, key)}因子贡献 {value:+.1f} 分，形成正向支持' for key, value in ranked if value > 0][:3]
    bear_case = [f'{labels.get(key, key)}因子贡献 {value:+.1f} 分，构成负向压力' for key, value in ranked if value < 0][:3]
    action = {'Overweight':'加仓', 'Underweight':'减仓', 'Sell':'卖出'}.get(rating, '持有')
    result.update({
        'rating':rating, 'model_rating':rating, 'decision_score':round(score, 1),
        'decision_basis':'multifactor_absolute_score_v1',
        'confidence':round(as_float(composite.get('confidence')) * 100),
        'executive_summary':f"综合因子评分 {score:+.1f}/100，数据覆盖 {as_float(composite.get('data_coverage'))*100:.0f}%，当前操作评级为{action}。",
        'bull_case':bull_case or ['当前没有达到可量化阈值的正向因子'],
        'bear_case':bear_case or ['当前没有达到可量化阈值的负向因子'],
        'new_evidence':evidence,
        'change_reason':f'综合因子评分为 {score:+.1f}，依据加仓 ≥35、减仓 ≤-30、卖出 ≤-60 且趋势为负的阈值生成。',
        'source':'deterministic_policy_engine',
    })
    return result

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
    account_or_event_block = (
        as_float(context.get('margin_call_cny')) > 0
        or as_float(context.get('pending_buy_quantity')) > 0
        or context.get('order_data_complete') is False
        or body.get('event_data_complete') is False
        or any(0 <= int(as_float(item.get('days_until'), 99)) <= 3 for item in (body.get('upcoming_events') or []))
    )
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
    if account_or_event_block and final_rating in {'Buy', 'Overweight'}:
        violations.append('保证金、订单状态或财报事件数据触发硬风控，禁止新增仓位')

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
    previous_score = previous.get('decision_score')
    current_score = result.get('decision_score')
    score_changed = previous_score is not None and current_score is not None and abs(as_float(current_score) - as_float(previous_score)) >= 10
    threshold_evidence = previous_score is None and current_score is not None and (as_float(current_score) >= 35 or as_float(current_score) <= -30)
    material_change = price_changed or exposure_changed or score_changed or threshold_evidence
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
    if account_or_event_block and final_rating in {'Buy', 'Overweight'}:
        final_rating = 'Hold'
        result['position_sizing'] = '账户或事件硬风控已触发；等待保证金、订单或财报事件状态确认前不新增仓位。'
        adjustments.append('账户/事件硬风控已将建议降级为持有')

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
    if as_float(context.get('margin_call_cny')) > 0:
        flags.append(f"账户存在追缴保证金要求 ¥{as_float(context.get('margin_call_cny')):,.2f}，硬风控禁止新增风险")
    if as_float(context.get('pending_buy_quantity')) > 0:
        flags.append(f"存在未完成买单 {as_float(context.get('pending_buy_quantity')):g} 股，禁止重复加仓")
    if as_float(context.get('pending_sell_quantity')) > 0:
        flags.append(f"存在未完成卖单 {as_float(context.get('pending_sell_quantity')):g} 股，建议等待成交状态确认")
    if context.get('order_data_complete') is False:
        flags.append('未完成订单数据暂不可用，订单风险覆盖不完整，禁止新增仓位')
    upcoming_events = body.get('upcoming_events') or []
    near_events = [item for item in upcoming_events if 0 <= int(as_float(item.get('days_until'), 99)) <= 3]
    if near_events:
        nearest = near_events[0]
        flags.append(f"{nearest.get('date')} 临近事件：{nearest.get('title')}，禁止仅凭技术信号加仓")
    if body.get('event_data_complete') is False:
        flags.append('财报事件日历暂不可用，事件风险覆盖不完整')
    research = body.get('research_snapshot') or {}
    if research and as_float(research.get('coverage_pct')) < 75:
        flags.append(f"基本面与一致预期覆盖仅 {as_float(research.get('coverage_pct')):.0f}%，研究证据不足，不允许据此提高评级")
    if as_float(body.get('return_pct')) <= -30:
        flags.append('当前回撤超过 30%，禁止仅因价格下跌而机械补仓')
    if as_float(body.get('cost')) <= 0:
        flags.append('持仓为负成本，收益率不能直接用于常规止盈判断')
    technical = body.get('technical_data') or {}
    if technical.get('trend') == '下降':
        flags.append('真实日线处于下降趋势，禁止仅因浮亏机械补仓')
    if as_float(technical.get('volatility_20d')) >= .50:
        flags.append(f"20日年化波动率 {as_float(technical.get('volatility_20d'))*100:.1f}%，需降低目标仓位")
    if as_float(technical.get('current_drawdown_120d')) <= -.20:
        flags.append(f"较120日高点回撤 {as_float(technical.get('current_drawdown_120d'))*100:.1f}%")
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
    import platform, sys
    configured = all((
        credential('LONGBRIDGE_APP_KEY', 'LONGPORT_APP_KEY'),
        credential('LONGBRIDGE_APP_SECRET', 'LONGPORT_APP_SECRET'),
        credential('LONGBRIDGE_ACCESS_TOKEN', 'LONGPORT_ACCESS_TOKEN'),
    ))
    quote_sdk_error = None
    quote_sdk_path = None
    try:
        import longport
        quote_sdk_available = True
        quote_sdk_path = getattr(longport, '__file__', None)
    except (ImportError, OSError) as exc:
        quote_sdk_available = False
        quote_sdk_error = f'{type(exc).__name__}: {exc}'
    return jsonify({
        'status':'ok', 'account_source':'longbridge_openapi',
        'quote_source_policy':'longbridge_sdk_then_sina_tencent_fallback',
        'quote_sdk_available':quote_sdk_available, 'quote_sdk_error':quote_sdk_error,
        'quote_sdk_path':quote_sdk_path,
        'runtime':{'python':platform.python_version(), 'implementation':platform.python_implementation(), 'machine':platform.machine(), 'libc':platform.libc_ver(), 'executable':sys.executable},
        'account_configured':configured, 'session_configured':session_configured(),
        'routes':['/health', '/session', '/account', '/performance', '/prices', '/history', '/recommendations', '/events', '/research', '/investment-policy', '/market', '/portfolio-risk', '/factor-analysis', '/analysis'],
    })

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
        app.logger.error('account_snapshot_failed: %s', exc)
        return jsonify({'error':'实时账户数据获取失败，已阻断持仓展示与个股分析', 'detail':str(exc), 'source':'longbridge_openapi'}), 503

@app.route('/investment-policy', methods=['GET', 'PUT', 'OPTIONS'])
def policy_route():
    if request.method == 'OPTIONS':
        return ('', 204)
    if not request_authorized():
        return auth_error()
    if request.method == 'PUT':
        supplied = request.get_json(force=True, silent=True) or {}
        errors = validate_policy(supplied)
        if errors:
            return jsonify({'error':'投资策略配置校验失败', 'details':errors, 'source':'investment_policy', 'retryable':False, 'fetched_at':iso_now()}), 400
        return jsonify({'error':'FC 无持久化配置存储，拒绝把策略写入临时实例；请将已确认配置写入 INVESTMENT_POLICY_JSON 后重新部署', 'source':'investment_policy', 'retryable':False, 'fetched_at':iso_now()}), 409
    policy = investment_policy()
    return jsonify({**policy, 'provisional':not policy.get('confirmed_by_user'), 'storage':'fc_environment', 'fetched_at':iso_now()})

@app.route('/market')
def market():
    if not request_authorized():
        return auth_error()
    try:
        return jsonify(get_market_regime(force=request.args.get('force') == '1'))
    except Exception as exc:
        return jsonify({'error':'市场环境计算失败', 'detail':str(exc), 'source':'market_regime', 'retryable':True, 'fetched_at':iso_now()}), 503

@app.route('/portfolio-risk')
def portfolio_risk():
    if not request_authorized():
        return auth_error()
    try:
        snapshot = get_account_snapshot(force=request.args.get('force') == '1')
        if not snapshot.get('complete'):
            return jsonify({'error':'账户快照不完整，已阻断组合风险计算', 'source':'longbridge_openapi', 'retryable':True, 'fetched_at':iso_now(), 'missing_prices':snapshot.get('missing_prices'), 'missing_currencies':snapshot.get('missing_currencies')}), 503
        return jsonify(get_portfolio_risk(force=request.args.get('force') == '1', snapshot=snapshot))
    except Exception as exc:
        return jsonify({'error':'组合风险计算失败', 'detail':str(exc), 'source':'portfolio_risk', 'retryable':True, 'fetched_at':iso_now()}), 503

@app.route('/factor-analysis')
def factor_analysis():
    if not request_authorized():
        return auth_error()
    symbol = normalized_symbol(request.args.get('symbol'))
    if not symbol:
        return jsonify({'error':'缺少symbol参数', 'source':'factor_model', 'retryable':False, 'fetched_at':iso_now()}), 400
    try:
        history_data = fetch_symbol_history(symbol, force=request.args.get('force') == '1')
        research_data = get_research_snapshot(symbol, force=request.args.get('force') == '1')
        return jsonify(factor_analysis_from_history(symbol, history_data, research=research_data))
    except Exception as exc:
        return jsonify({'error':'因子影子分析失败', 'detail':str(exc), 'symbol':symbol, 'source':'factor_model', 'retryable':True, 'fetched_at':iso_now()}), 503

@app.route('/research')
def research():
    if not request_authorized():
        return auth_error()
    symbol = normalized_symbol(request.args.get('symbol'))
    if not symbol:
        return jsonify({'error':'缺少symbol参数'}), 400
    try:
        return jsonify(get_research_snapshot(symbol, force=request.args.get('force') == '1'))
    except Exception as exc:
        return jsonify({'error':'Longbridge研究数据获取失败', 'detail':str(exc), 'symbol':symbol, 'retryable':True, 'fetched_at':iso_now()}), 503

@app.route('/events')
def events():
    if not request_authorized():
        return auth_error()
    try:
        snapshot = get_account_snapshot()
        symbols = [item['symbol'] for item in snapshot.get('positions', [])]
        data = get_upcoming_finance_events(symbols, force=request.args.get('force') == '1')
        status = 200 if data.get('complete') else 206
        return jsonify(data), status
    except Exception as exc:
        return jsonify({'error':'财报事件日历获取失败', 'detail':str(exc), 'source':'longbridge_finance_calendar', 'retryable':True, 'fetched_at':iso_now()}), 503

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
    event_data = get_upcoming_finance_events([item['symbol'] for item in ranked[:5]])
    for item in ranked[:5]:
        item_events = [event for event in event_data.get('events', []) if normalized_symbol(event.get('symbol')) == normalized_symbol(item['symbol'])]
        item['upcoming_events'] = item_events
        item['event_data_complete'] = event_data.get('complete', False)
        if any(0 <= event['days_until'] <= 3 for event in item_events):
            item['event_guard'] = 'near_event'
            item['summary'] += ' 三天内存在财报事件，事件落地前仅观察，不按技术信号建仓。'
    policy = investment_policy()
    if not policy.get('confirmed_by_user'):
        for item in ranked:
            item['research_position_cap_pct'] = item.pop('target_position_pct', None)
            item['target_position_pct'] = None
            item['position_sizing'] = '投资策略尚未确认；当前仅列入研究候选，不生成建仓比例。'
            item['action_steps'] = ['继续观察真实日线和基本面数据', '等待多因子覆盖率与影子验证达标', '确认投资策略后再计算目标仓位']
    for index, item in enumerate(ranked[:5], 1):
        item['rank'] = index
    if not ranked:
        return jsonify({'recommendations': [], 'error':'候选行情暂不可用或没有标的通过趋势与风险筛选', 'generated_at':time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}), 503
    return jsonify({
        'recommendations': ranked[:5], 'universe_size': len(DISCOVERY_UNIVERSE),
        'eligible_size': len(ranked), 'method_version':'discovery_v1',
        'model_status':'shadow', 'policy_status':'confirmed' if policy.get('confirmed_by_user') else 'unconfirmed',
        'generated_at':time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
        'disclaimer':'量化候选仅供研究，不构成收益承诺或自动交易指令。',
    })

@app.route('/prices')
def prices():
    if not request_authorized():
        return auth_error()
    try:
        snapshot = get_account_snapshot()
        return jsonify({'prices':snapshot['prices'], 'updated':snapshot['fetched_at'], 'source':snapshot.get('price_source'), 'source_status':snapshot.get('price_source_status'), 'account_source':snapshot['source']})
    except Exception as e:
        return jsonify({'error':'实时账户行情获取失败', 'detail':str(e)}), 503

@app.route('/performance')
def performance():
    if not request_authorized():
        return auth_error()
    try:
        return jsonify(get_performance_snapshot(force=request.args.get('force') == '1'))
    except Exception as exc:
        app.logger.error('performance_snapshot_failed: %s', exc)
        return jsonify({'error':'长桥真实账户绩效获取失败', 'detail':str(exc)}), 503

@app.route('/history')
def history():
    if not request_authorized():
        return auth_error()
    symbol = normalized_symbol(request.args.get('symbol'))
    try:
        snapshot = get_account_snapshot()
    except Exception as exc:
        return jsonify({'error':'实时持仓不可用，不能确认历史行情权限范围', 'detail':str(exc)}), 503
    held = {normalized_symbol(item.get('symbol')) for item in snapshot.get('positions', [])}
    if symbol not in held:
        return jsonify({'error':'仅允许读取当前实时持仓的历史行情'}), 403
    try:
        return jsonify(fetch_symbol_history(symbol, force=request.args.get('force') == '1'))
    except Exception as exc:
        return jsonify({'error':'真实历史行情获取失败', 'detail':str(exc), 'symbol':symbol}), 503

@app.route('/analysis', methods=['POST'])
def analysis():
    if not request_authorized():
        return auth_error()
    body = request.get_json(force=True, silent=True) or {}
    force_refresh = bool(body.get('force'))
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
    try:
        history_data = fetch_symbol_history(symbol, force=force_refresh)
    except Exception as exc:
        return jsonify({'error':'真实历史行情不可用，已阻断个股分析', 'detail':str(exc), 'symbol':symbol}), 503
    event_data = get_upcoming_finance_events([symbol], force=force_refresh)
    research_data = get_research_snapshot(symbol, force=force_refresh)
    body.update({
        'symbol':position['symbol'], 'name':position['name'], 'ccy':position['currency'],
        'cost':position['cost_price'], 'price':position['price'], 'qty':position['quantity'],
        'return_pct':((position['price'] - position['cost_price']) / abs(position['cost_price']) * 100) if position['cost_price'] else 0,
        'sector':position['sector'], 'portfolio_context':live_position_context(snapshot, position),
        'price_updated_at':snapshot['fetched_at'], 'account_verified':True,
        'technical_data':history_data['technical'], 'history_as_of':history_data['as_of'], 'history_source':history_data['source'], 'history_source_status':history_data.get('source_status'),
        'research_snapshot':research_data,
        'upcoming_events':event_data.get('events', []), 'event_data_complete':event_data.get('complete', False),
        'event_data_source':event_data.get('source'),
    })
    factor_result = factor_analysis_from_history(symbol, history_data, research=research_data)
    try:
        market_result = get_market_regime(force=force_refresh)
    except Exception as exc:
        market_result = {'model_version':'market-regime-v1', 'model_status':'shadow', 'error':str(exc)}
    try:
        portfolio_risk_result = get_portfolio_risk(force=force_refresh, snapshot=snapshot)
    except Exception as exc:
        portfolio_risk_result = {'model_version':'portfolio-risk-v1', 'model_status':'shadow', 'error':str(exc), 'snapshot_id':None}
    risk_flags = build_risk_flags(body)
    result = deterministic_factor_decision(body, risk_flags, factor_result)
    result.update(factor_result['price_plan'])
    evidence = research_evidence(research_data, position['price'])
    result['bull_case'] = list(dict.fromkeys(result['bull_case'] + evidence['bull']))[:4]
    result['bear_case'] = list(dict.fromkeys(result['bear_case'] + evidence['bear']))[:4]
    narrative = {
        'executive_summary':result['executive_summary'], 'bull_case':result['bull_case'], 'bear_case':result['bear_case'],
        'counterarguments':[], 'invalidation_conditions':result['invalidation_conditions'],
        'data_limitations':list(dict.fromkeys(factor_result['quality']['warnings'] + evidence['limitations'])), 'change_explanation':result['change_reason'],
    }
    key = os.getenv('DEEPSEEK_API_KEY', '')
    if key:
        prompt = f"""你是个人投资组合的反方研究员。最终评级、仓位和价格已经由确定性程序计算，你无权修改。
只根据下面提供的数据，用简体中文解释并主动寻找反例；不得编造新闻、财报、估值、目标价或历史行情。
输出严格JSON且不要Markdown。字段只能是：executive_summary, bull_case(数组), bear_case(数组), counterarguments(数组), invalidation_conditions(数组), data_limitations(数组), change_explanation。
确定性最终评级：{result['rating']}。允许动作说明：{result['position_sizing']}。
实时持仓和技术数据：{json.dumps(body, ensure_ascii=False)}
市场环境影子结果：{json.dumps(market_result, ensure_ascii=False)}
组合风险影子结果：{json.dumps(portfolio_risk_result, ensure_ascii=False)}
多因子确定性结果：{json.dumps(factor_result, ensure_ascii=False)}
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
            summary = str(parsed.get('executive_summary') or '').strip()
            change = str(parsed.get('change_explanation') or '').strip()
            narrative = {
                'executive_summary':summary if has_chinese(summary) else result['executive_summary'],
                'bull_case':chinese_list(parsed.get('bull_case'), result['bull_case']),
                'bear_case':chinese_list(parsed.get('bear_case'), result['bear_case']),
                'counterarguments':chinese_list(parsed.get('counterarguments'), []),
                'invalidation_conditions':chinese_list(parsed.get('invalidation_conditions'), result['invalidation_conditions']),
                'data_limitations':chinese_list(parsed.get('data_limitations'), factor_result['quality']['warnings']),
                'change_explanation':change if has_chinese(change) else result['change_reason'],
            }
            result['source'] = 'deterministic_policy_engine+deepseek_narrative'
        except Exception:
            result['source'] = 'deterministic_policy_engine+narrative_fallback'
    narrative['bull_case'] = list(dict.fromkeys(evidence['bull'] + narrative['bull_case']))[:4]
    narrative['bear_case'] = list(dict.fromkeys(evidence['bear'] + narrative['bear_case']))[:4]
    narrative['data_limitations'] = list(dict.fromkeys(evidence['limitations'] + narrative['data_limitations']))[:6]
    result = validate_decision(result, body, risk_flags)
    policy = investment_policy()
    if not policy.get('confirmed_by_user'):
        result['target_position_pct'] = None
        current_pct = as_float((body.get('portfolio_context') or {}).get('position_weight')) * 100
        if result['rating'] in {'Buy', 'Overweight'}:
            result['position_sizing'] = f'当前仓位约 {current_pct:.1f}%；加仓信号已通过因子阈值与硬风控，但投资策略参数尚未确认，因此只建议在入场区间小额分批，不生成目标仓位。'
        elif result['rating'] in {'Underweight', 'Sell'}:
            result['position_sizing'] = f'当前仓位约 {current_pct:.1f}%；减仓信号已通过因子阈值或风险约束，但投资策略参数尚未确认，因此不生成精确目标仓位。'
        else:
            result['position_sizing'] = f'当前仓位约 {current_pct:.1f}%；综合评分未跨越操作阈值，维持现有仓位。投资策略参数尚未确认，因此不生成目标仓位。'
    result.update({
        'symbol': str(body.get('symbol', 'UNKNOWN')),
        'decision_version': 4, 'rating_source':'deterministic_policy_engine',
        'desired_weight':None, 'binding_constraint':'policy_not_configured' if not policy.get('confirmed_by_user') else 'shadow_models_not_advisory',
        'risk_flags': risk_flags,
        'data_scope': 'verified_live_longbridge_position_cost_account_and_preferred_quote_with_audited_fallback',
        'price_source':snapshot.get('price_source'), 'price_source_status':snapshot.get('price_source_status'),
        'technical_data':history_data['technical'], 'history_as_of':history_data['as_of'], 'history_source':history_data['source'], 'history_source_status':history_data.get('source_status'),
        'factor_analysis':factor_result, 'market_regime':market_result, 'portfolio_risk':portfolio_risk_result, 'narrative':narrative,
        'research_snapshot':research_data,
        'upcoming_events':event_data.get('events', []), 'event_data_complete':event_data.get('complete', False),
        'audit':{'data_snapshot_id':portfolio_risk_result.get('snapshot_id'), 'factor_model_version':factor_result['model_version'], 'factor_model_status':factor_result['model_status'], 'factor_decision_weight':factor_result['composite']['decision_weight'], 'decision_score':result.get('decision_score'), 'decision_basis':result.get('decision_basis'), 'risk_model_version':portfolio_risk_result.get('model_version'), 'policy_version':policy['version'], 'consistency':result.get('consistency', {}).get('status')},
        'generated_at': iso_now(),
    })
    return jsonify(result)

if __name__ == '__main__':
    port = int(os.environ.get('FC_SERVER_PORT', 9000))
    app.run(host='0.0.0.0', port=port)

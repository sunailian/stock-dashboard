import json, os, urllib.request, ssl, time
from urllib.parse import urlparse, parse_qs

CTX = ssl.create_default_context()
CTX.check_hostname = False
CTX.verify_mode = False

SINA = 'gb_goog,gb_aapl,gb_msft,gb_nvda,gb_tsla,gb_baba,gb_paas,gb_tlt,gb_smh,gb_appx,hk09988,hk00981,hk06030,hk00100,hk02824'

# ── WSGI Handler (FC HTTP 触发器默认) ──────────────────
def handler(environ, start_response):
    path = environ.get('PATH_INFO', '/')
    method = environ.get('REQUEST_METHOD', 'GET')
    qs = environ.get('QUERY_STRING', '')
    try:
        cl = int(environ.get('CONTENT_LENGTH', 0))
        body_raw = environ['wsgi.input'].read(cl) if cl > 0 else b'{}'
        body = json.loads(body_raw) if body_raw else {}
    except:
        body = {}

    try:
        if path == '/prices':
            data = _prices()
        elif path == '/ai':
            data = _ai(body)
        elif path == '/health':
            data = {'status': 'ok', 'routes': ['/prices', '/ai']}
        else:
            data = {'error': 'not found: ' + path}
            code = '404 Not Found'
            start_response(code, [('Content-Type', 'application/json'), ('Access-Control-Allow-Origin', '*')])
            return [json.dumps(data, ensure_ascii=False).encode()]
        start_response('200 OK', [('Content-Type', 'application/json'), ('Access-Control-Allow-Origin', '*')])
        return [json.dumps(data, ensure_ascii=False).encode()]
    except Exception as e:
        start_response('500 Internal Server Error', [('Content-Type', 'application/json'), ('Access-Control-Allow-Origin', '*')])
        return [json.dumps({'error': str(e)}).encode()]

def _prices():
    url = f'http://hq.sinajs.cn/list={SINA}'
    req = urllib.request.Request(url, headers={'Referer': 'https://finance.sina.com.cn'})
    raw = urllib.request.urlopen(req, timeout=10, context=CTX).read().decode('gbk')
    prices = {}
    for line in raw.strip().split('\n'):
        if '=' not in line: continue
        var = line.split('=')[0]
        parts = line.split('"')[1].split(',')
        if not parts[1]: continue
        if 'gb_' in var:
            prices[var.split('_')[-1].upper() + '.US'] = float(parts[1])
        else:
            prices[str(int(var.split('hk')[-1])) + '.HK'] = float(parts[3])
    return {'prices': prices, 'updated': time.strftime('%H:%M:%S')}

def _ai(body):
    key = os.getenv('DEEPSEEK_API_KEY', '')
    if not key: return {'error': 'DEEPSEEK_API_KEY not set'}

    symbol = body.get('symbol', 'UNKNOWN')
    prompt = f"你是灼沅的股票分析师。灼沅年化目标20%。请对{symbol}给出50字以内操作建议，用【持有】【加仓】【减仓】【止损】结尾。"
    req = urllib.request.Request('https://api.deepseek.com/v1/chat/completions',
        data=json.dumps({'model': 'deepseek-chat', 'messages': [{'role': 'user', 'content': prompt}], 'max_tokens': 150, 'temperature': 0.6}).encode(),
        method='POST', headers={'Content-Type': 'application/json', 'Authorization': f'Bearer {key}'})
    resp = json.loads(urllib.request.urlopen(req, timeout=30, context=CTX).read())
    result = resp['choices'][0]['message']['content'] if resp.get('choices') else ''
    return {'analysis': result, 'symbol': symbol}

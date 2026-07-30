import json, os, urllib.request, ssl, time

CTX = ssl.create_default_context()
CTX.check_hostname = False
CTX.verify_mode = False

SINA = 'gb_goog,gb_aapl,gb_msft,gb_nvda,gb_tsla,gb_baba,gb_paas,gb_tlt,gb_smh,gb_appx,hk09988,hk00981,hk06030,hk00100,hk02824'

def handler(event, context):
    path = event.get('path', '/')
    try:
        body = json.loads(event.get('body', '{}')) if event.get('body') else {}
    except:
        body = {}

    if path == '/prices':
        return _prices()
    elif path == '/ai':
        return _ai(body)
    elif path == '/health':
        return _ok({'status': 'ok', 'routes': ['/prices', '/ai']})
    return _err(404, f'unknown: {path}')

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
    return _ok({'prices': prices, 'updated': time.strftime('%H:%M:%S')})

def _ai(body):
    key = os.getenv('DEEPSEEK_API_KEY', '')
    if not key: return _err(503, 'DEEPSEEK_API_KEY not set')

    symbol = body.get('symbol', '')
    prompt = f"""你是灼沅的股票分析师。灼沅年化目标20%。请对{symbol}给出50字以内操作建议，格式：一句话建议。标签用【持有】【加仓】【减仓】【止损】之一结尾。"""
    req = urllib.request.Request('https://api.deepseek.com/v1/chat/completions',
        data=json.dumps({'model': 'deepseek-chat', 'messages': [{'role': 'user', 'content': prompt}], 'max_tokens': 150, 'temperature': 0.6}).encode(),
        method='POST', headers={'Content-Type': 'application/json', 'Authorization': f'Bearer {key}'})
    resp = json.loads(urllib.request.urlopen(req, timeout=30, context=CTX).read())
    result = resp['choices'][0]['message']['content'] if resp.get('choices') else '分析不可用'
    return _ok({'analysis': result, 'symbol': symbol})

def _ok(data):
    return {'statusCode': 200, 'headers': {'Content-Type': 'application/json', 'Access-Control-Allow-Origin': '*'}, 'body': json.dumps(data, ensure_ascii=False), 'isBase64Encoded': False}

def _err(code, msg):
    return {'statusCode': code, 'headers': {'Content-Type': 'application/json', 'Access-Control-Allow-Origin': '*'}, 'body': json.dumps({'error': msg}), 'isBase64Encoded': False}

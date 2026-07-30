import json, os, urllib.request, ssl, time

CTX = ssl.create_default_context()
CTX.check_hostname = False
CTX.verify_mode = False
SINA = 'gb_goog,gb_aapl,gb_msft,gb_nvda,gb_tsla,gb_baba,gb_paas,gb_tlt,gb_smh,gb_appx,hk09988,hk00981,hk06030,hk00100,hk02824'

def handler(event, context):
    path = event.get('path', '/')
    qs = event.get('queryString', event.get('queryParameters', {}))
    method = event.get('httpMethod', event.get('method', 'GET'))
    body = event.get('body', '{}')
    if isinstance(body, str):
        try: body = json.loads(body)
        except: body = {}
    try:
        if 'health' in path or path == '/':
            return ok({'status': 'ok', 'routes': ['/health','/prices','/ai']})
        elif 'prices' in path:
            return ok(get_prices())
        elif 'ai' in path:
            return ok(get_ai(body))
        else:
            return err(404, 'not found: ' + path)
    except Exception as e:
        return err(500, str(e))

def get_prices():
    req = urllib.request.Request(f'http://hq.sinajs.cn/list={SINA}', headers={'Referer': 'https://finance.sina.com.cn'})
    raw = urllib.request.urlopen(req, timeout=10, context=CTX).read().decode('gbk')
    prices = {}
    for line in raw.strip().split('\n'):
        if '=' not in line: continue
        var = line.split('=')[0]; parts = line.split('"')[1].split(',')
        if not parts[1]: continue
        if 'gb_' in var:
            prices[var.split('_')[-1].upper() + '.US'] = float(parts[1])
        else:
            prices[str(int(var.split('hk')[-1])) + '.HK'] = float(parts[3])
    return {'prices': prices, 'updated': time.strftime('%H:%M:%S')}

def get_ai(body):
    key = os.getenv('DEEPSEEK_API_KEY', '')
    if not key: return {'error': 'DEEPSEEK_API_KEY not set'}
    symbol = body.get('symbol', 'UNKNOWN')
    prompt = f"你是灼沅的股票分析师。灼沅年化目标20%。请对{symbol}给出50字以内操作建议，用【持有】【加仓】【减仓】【止损】结尾。"
    req = urllib.request.Request('https://api.deepseek.com/v1/chat/completions',
        data=json.dumps({'model': 'deepseek-chat', 'messages': [{'role': 'user', 'content': prompt}], 'max_tokens': 150, 'temperature': 0.6}).encode(),
        method='POST', headers={'Content-Type': 'application/json', 'Authorization': f'Bearer {key}'})
    resp = json.loads(urllib.request.urlopen(req, timeout=30, context=CTX).read())
    return {'analysis': resp['choices'][0]['message']['content'] if resp.get('choices') else '', 'symbol': symbol}

def ok(data):
    return {'statusCode': 200, 'headers': {'Content-Type': 'application/json', 'Access-Control-Allow-Origin': '*'}, 'body': json.dumps(data, ensure_ascii=False), 'isBase64Encoded': False}

def err(code, msg):
    return {'statusCode': code, 'headers': {'Content-Type': 'application/json', 'Access-Control-Allow-Origin': '*'}, 'body': json.dumps({'error': msg}), 'isBase64Encoded': False}

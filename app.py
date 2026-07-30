"""股票看板 API — 行情代理 + AI 分析
环境变量：DEEPSEEK_API_KEY"""
import json, os, urllib.request, ssl, time
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse

CTX = ssl.create_default_context()
CTX.check_hostname = False
CTX.verify_mode = False
SINA = 'gb_goog,gb_aapl,gb_msft,gb_nvda,gb_tsla,gb_baba,gb_paas,gb_tlt,gb_smh,gb_appx,hk09988,hk00981,hk06030,hk00100,hk02824'
PORT = int(os.getenv('FC_SERVER_PORT', 9000))

def get_prices():
    url = f'http://hq.sinajs.cn/list={SINA}'
    req = urllib.request.Request(url, headers={'Referer': 'https://finance.sina.com.cn'})
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

class Handler(BaseHTTPRequestHandler):
    def _send(self, code, data):
        body = json.dumps(data, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)
    def _route(self, method):
        path = urlparse(self.path).path.rstrip('/') or '/'
        for seg in ['/prices', '/health', '/ai']:
            if path.endswith(seg) or seg in path:
                return seg
        return path

    def _parse_event(self):
        """FC event function mode: reads original request from POST /invoke body"""
        cl = int(self.headers.get('Content-Length', 0))
        if cl > 0:
            try:
                event = json.loads(self.rfile.read(cl))
                self._event_body = event.get('body', '')
                return event
            except: pass
        return {}

    def do_GET(self):
        path = self._route('GET')
        if path == '/health': self._send(200, {'status': 'ok', 'routes': ['/prices','/ai']})
        elif path == '/prices':
            try: self._send(200, get_prices())
            except Exception as e: self._send(500, {'error': str(e)})
        else: self._send(404, {'error': 'not found', 'path': urlparse(self.path).path})

    def do_POST(self):
        path = self._route('POST')
        # FC event mode: POST /invoke with the actual request in body
        if path != '/ai' and path != '/prices' and path != '/health':
            event = self._parse_event()
            if event:
                rpath = event.get('path', event.get('rawPath', ''))
                if 'health' in rpath:
                    self._send(200, {'status': 'ok', 'routes': ['/prices','/ai'], 'mode': 'event'})
                    return
                if 'prices' in rpath:
                    self._send(200, get_prices())
                    return
                if 'ai' in rpath or event.get('httpMethod') == 'POST':
                    body = event.get('body', '{}')
                    if isinstance(body, str):
                        try: body = json.loads(body)
                        except: body = {}
                    self._send(200, get_ai(body))
                    return
            # fallback: normal POST
            cl = int(self.headers.get('Content-Length', 0))
            body = json.loads(self.rfile.read(cl)) if cl > 0 else {}
            if path == '/ai' or '/ai' in urlparse(self.path).path:
                self._send(200, get_ai(body))
                return
        else:
            cl = int(self.headers.get('Content-Length', 0))
            body = json.loads(self.rfile.read(cl)) if cl > 0 else {}
            if path == '/ai':
                self._send(200, get_ai(body))
                return
        self._send(404, {'error': 'not found', 'path': urlparse(self.path).path})
    def log_message(self, *args): pass  # silent

if __name__ == '__main__':
    server = HTTPServer(('0.0.0.0', PORT), Handler)
    print(f'Stock API on port {PORT}', flush=True)
    # Handle one request then exit (FC event mode re-invokes per request)
    server.handle_request()
    server.server_close()

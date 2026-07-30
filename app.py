"""股票看板 API — 行情代理 + AI 分析。环境变量：DEEPSEEK_API_KEY"""
import json, os, urllib.request, ssl, time
from flask import Flask, request, jsonify

app = Flask(__name__)
CTX = ssl.create_default_context()
CTX.check_hostname = False
CTX.verify_mode = False
SINA = 'gb_goog,gb_aapl,gb_msft,gb_nvda,gb_tsla,gb_baba,gb_paas,gb_tlt,gb_smh,gb_appx,hk09988,hk00981,hk06030,hk00100,hk02824,gb_spx,gb_ixic'

@app.route('/health')
def health():
    return jsonify({'status': 'ok', 'routes': ['/health', '/prices', '/ai']})

@app.route('/prices')
def prices():
    try:
        req = urllib.request.Request(f'http://hq.sinajs.cn/list={SINA}',
            headers={'Referer': 'https://finance.sina.com.cn'})
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
        return jsonify({'prices': prices, 'updated': time.strftime('%H:%M:%S')})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

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

#!/usr/bin/env python3
"""Read-only live account API for the dashboard (live server side, standard library only).

Runs only on the live server (the API key is bound to its IP), as the unprivileged service user, behind Caddy
(HTTPS, https://139-180-146-42.sslip.io). It never writes anything to the trader's files and never trades:

  * Every REFRESH seconds it reads the account with GET requests only (`OKX` client from okx_demo_trader, forced
    dry-run, so every non-GET request is refused below this code), plus the trader's own files read-only
    (okx_live/summary.json, state.json, actions.jsonl tail) for stops / targets / seeds / fills / closed trades.
  * It builds the same public documents as tools/live_sync.py (same whitelist functions: build_snapshot,
    build_fills, build_trades) and runs the same secret check (live_sync.checked) before anything is served.
  * The result is cached in memory. HTTP requests only ever return the cached bytes (OKX is never called per
    request), so the request rate cannot reach OKX.
  * Public market tickers (no key) are cached the same way for the dashboard's market fallback (/tickers.json).

Routes (GET / HEAD only; every other method answers 405, every other path 404):
  /live.json     {"snapshot": ..., "fills": ..., "trades": ..., "api": {...}}   same fields as live-data/*.json
  /tickers.json  USDT perpetual tickers (instId, last, open24h, volCcy24h, ts) from the OKX public endpoint
  /healthz       {"ok": true/false, "age_s": ...}

Credentials come from the environment (systemd EnvironmentFile, root-owned 0600); they are never printed, logged,
or written anywhere, and a document containing them (or anything shaped like a key) is refused by the secret check.
"""
import argparse
import json
import os
import subprocess
import sys
import threading
import time
from collections import deque
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import live_sync as ls  # noqa: E402  (whitelist + secret check, shared with the hourly live-data sync)

DEFAULT_DATA = '/var/lib/modi-okx-live/okx_live'
DEFAULT_LISTEN, DEFAULT_PORT = '127.0.0.1', 8787
REFRESH = 60                 # seconds between account reads (2–3 GET requests to OKX per minute)
TICKERS_REFRESH = 60
STALE_KEEP = 600             # an OKX read failure keeps the last good account for up to 10 minutes, then the file summary
ALLOWED_ORIGIN = 'https://modi-cjh.github.io'
RATE_PER_MIN, RATE_BURST = 30, 15   # per client IP (Caddy passes the client address in X-Forwarded-For)
MAX_CLIENTS = 5000
PUBLIC_BASE = 'https://www.okx.com'
ROUTES = ('/live.json', '/tickers.json', '/healthz')


def iso(ts=None):
    return datetime.fromtimestamp(time.time() if ts is None else ts, timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


def read_json(path):
    try:
        return json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return None


def read_tail(path, lines=ls.ACTIONS_TAIL):
    try:
        with open(path, encoding='utf-8', errors='replace') as f:
            return ''.join(deque(f, maxlen=lines))
    except OSError:
        return ''


# ---- OKX reads (GET only) -----------------------------------------------------------------------
def make_client(env=os.environ):
    """The trader's own signed client, live mode, forced dry-run: request() refuses every non-GET method."""
    import okx_demo_trader as t
    mode = t.trading_mode(env)
    if mode != 'live':
        raise SystemExit('live_readapi 只在实盘服务器上运行（OKX_TRADING_MODE=live），已拒绝启动')
    creds = [str(env.get(k, '')).strip() for k in ls.SECRET_NAMES]
    if not all(creds):
        raise SystemExit('未配置 OKX 实盘密钥环境变量，已拒绝启动')
    client = t.OKX(*creds, attempts=2, mode='live', dry_run=True)
    if client.dry_run is not True:
        raise SystemExit('客户端必须是只读（dry-run），已拒绝启动')
    return client


def ok(resp):
    return isinstance(resp, dict) and resp.get('code') == '0' and isinstance(resp.get('data'), list)


class AccountError(RuntimeError):
    pass


def read_account(client):
    """Equity, available and every USDT swap position, straight from OKX (three GET requests)."""
    cfg = client.request('GET', '/api/v5/account/config')
    if not ok(cfg) or not cfg['data']:
        raise AccountError('account config')
    if 'withdraw' in str(cfg['data'][0].get('perm', '')):
        raise AccountError('密钥开启了提现权限，已拒绝读取')        # same refusal as the trader's load()
    bal = client.request('GET', '/api/v5/account/balance?ccy=USDT')
    if not ok(bal):
        raise AccountError('balance')
    usdt = next((x for x in (bal['data'] or [{}])[0].get('details', []) if x.get('ccy') == 'USDT'), None)
    if usdt is None:
        raise AccountError('balance: no USDT')
    pos = client.request('GET', '/api/v5/account/positions?instType=SWAP')
    if not ok(pos):
        raise AccountError('positions')
    rows = [p for p in pos['data'] if str(p.get('instId', '')).endswith('-USDT-SWAP') and ls.number(p.get('pos'))]
    return {'equity': usdt.get('eq'), 'available': usdt.get('availBal'), 'positions': rows}


def position_side(p):
    side = p.get('posSide')
    if side in ('long', 'short'):
        return side
    n = ls.number(p.get('pos')) or 0          # net mode: the sign of pos is the side
    return 'long' if n > 0 else 'short' if n < 0 else None


def merge_summary(summary, state, account, now_ms, ct_vals):
    """The trader's hourly summary with the account part replaced by the fresh OKX read (same keys as Mirror.summary)."""
    s = dict(summary or {})
    mirrors = [m for m in (state.get('mirrored') or {}).values() if isinstance(m, dict) and m.get('status') == 'open']
    known_ct = {p.get('inst'): p.get('ctVal') for p in s.get('positions') or [] if isinstance(p, dict)}
    positions = []
    for p in account['positions']:
        inst = p.get('instId')
        positions.append(dict(
            inst=inst, side=position_side(p), contracts=p.get('pos'), avgPx=p.get('avgPx'), markPx=p.get('markPx'),
            upl=p.get('upl'), uplRatio=p.get('uplRatio'), lever=p.get('lever'), margin=p.get('margin') or p.get('imr'),
            mgnMode=p.get('mgnMode'), ctVal=ct_vals.get(inst) or known_ct.get(inst),
            ours=any(m.get('inst') == inst for m in mirrors)))
    equity = ls.number(account['equity'])
    s.update(updated_at=iso(now_ms / 1000), equity=account['equity'], available=account['available'], positions=positions,
             trading_mode='live')
    history = [p for p in s.get('history') or [] if isinstance(p, (list, tuple)) and len(p) >= 2]
    if equity is not None:
        history = history + [[now_ms, round(equity, 2)]]     # the curve ends at this read
    s['history'] = history
    return s


def public_get(path, timeout=10):
    """Unsigned GET to an OKX public endpoint (no key involved)."""
    out = subprocess.run(['curl', '-sS', '-m', str(timeout), PUBLIC_BASE + path], capture_output=True, text=True)
    if out.returncode != 0:
        raise AccountError('public request failed')
    try:
        return json.loads(out.stdout)
    except ValueError:
        raise AccountError('public response is not JSON')


def build_tickers(resp, now=None):
    if not ok(resp):
        raise AccountError('tickers')
    rows = []
    for r in resp['data']:
        inst = ls.inst(r.get('instId'))
        last = ls.number(r.get('last'))
        if not inst or not last or last <= 0:
            continue
        rows.append(ls.clean({'instId': inst, 'last': last, 'open24h': ls.number(r.get('open24h')),
                              'volCcy24h': ls.number(r.get('volCcy24h')), 'ts': ls.number(r.get('ts'), 15)}))
    if not rows:
        raise AccountError('tickers: empty')
    return {'schema_version': 1, 'source': 'okx-public', 'updated_at': iso(now), 'tickers': rows}


# ---- cache -----------------------------------------------------------------------------------------
class Feed:
    """Builds and holds the served documents. `client` and `public` are injectable for tests."""
    def __init__(self, data_dir, client, public=public_get, clock=time.time):
        self.data, self.client, self.public, self.clock = Path(data_dir), client, public, clock
        self.lock = threading.Lock()
        self.live = self.tickers = None             # rendered bytes
        self.live_at = self.tickers_at = 0.0         # when the served document was built
        self.account = None                          # last good OKX read
        self.account_at = 0.0
        self.ct_vals = {}
        self.ct_at = 0.0
        self.last_error = None

    def contract_values(self, now):
        """ctVal per swap from the public instruments list, refreshed hourly (needed for live PnL in the browser)."""
        if self.ct_vals and now - self.ct_at < 3600:
            return self.ct_vals
        try:
            r = self.public('/api/v5/public/instruments?instType=SWAP')
            if ok(r):
                vals = {d.get('instId'): ls.number(d.get('ctVal')) for d in r['data'] if d.get('instId')}
                self.ct_vals = {k: v for k, v in vals.items() if v}
                self.ct_at = now
        except (AccountError, OSError):
            pass
        return self.ct_vals

    def refresh_live(self):
        now = self.clock()
        summary = read_json(self.data / 'summary.json')
        state = read_json(self.data / 'state.json')
        if not isinstance(summary, dict) or not isinstance(state, dict):
            self.last_error = 'summary.json / state.json 不可读'
            return False
        source = 'okx'
        try:
            self.account, self.account_at = read_account(self.client), now
        except Exception as e:          # noqa: BLE001  (network, OKX error, bad JSON: never fatal, never echoed)
            self.last_error = 'OKX 读取失败' if not isinstance(e, AccountError) else f'OKX 读取失败：{e}'
            if self.account is None or now - self.account_at > STALE_KEEP:
                source = 'file'
        if source == 'okx':
            merged = merge_summary(summary, state, self.account, int(self.account_at * 1000), self.contract_values(now))
        else:
            merged = summary                      # the trader's hourly summary as it is (same as live-data)
        try:
            snapshot = ls.build_snapshot(merged, state)
            actions = ls.parse_actions(read_tail(self.data / 'actions.jsonl'))
            doc = {
                'schema_version': 1, 'source': 'okx-live-api', 'updated_at': snapshot['updated_at'],
                'api': {'account': source, 'built_at': iso(now), 'refresh_s': REFRESH},
                'snapshot': snapshot, 'fills': ls.build_fills(actions, merged),
                'trades': ls.build_trades(actions, state, merged),
            }
            body = ls.checked(doc, 'live.json').encode()
        except ls.SyncError as e:
            self.last_error = str(e) if e.code != 2 else '密钥检查未通过，未更新'
            return False
        with self.lock:
            self.live, self.live_at = body, now
        if source == 'okx':
            self.last_error = None
        return True

    def refresh_tickers(self):
        now = self.clock()
        try:
            body = ls.checked(build_tickers(self.public('/api/v5/market/tickers?instType=SWAP'), now), 'tickers.json').encode()
        except (AccountError, ls.SyncError, OSError):
            return False
        with self.lock:
            self.tickers, self.tickers_at = body, now
        return True

    def get(self, path):
        with self.lock:
            if path == '/live.json':
                return self.live
            if path == '/tickers.json':
                return self.tickers
        age = None if not self.live_at else round(self.clock() - self.live_at)
        return json.dumps({'ok': age is not None and age < 3 * REFRESH, 'age_s': age}).encode()

    def loop(self, stop):
        last = ''
        while not stop.is_set():
            self.refresh_live()
            self.refresh_tickers()
            if self.last_error != last:                # short reasons only, never data or credentials
                last = self.last_error
                print(f'状态：{last or "正常"}', flush=True)
            stop.wait(REFRESH)


# ---- HTTP ------------------------------------------------------------------------------------------
class RateLimit:
    """Token bucket per client address."""
    def __init__(self, per_min=RATE_PER_MIN, burst=RATE_BURST, clock=time.monotonic):
        self.rate, self.burst, self.clock = per_min / 60.0, burst, clock
        self.buckets, self.lock = {}, threading.Lock()

    def allow(self, who):
        now = self.clock()
        with self.lock:
            if len(self.buckets) > MAX_CLIENTS:
                self.buckets.clear()
            tokens, at = self.buckets.get(who, (self.burst, now))
            tokens = min(self.burst, tokens + (now - at) * self.rate)
            if tokens < 1:
                self.buckets[who] = (tokens, now)
                return False
            self.buckets[who] = (tokens - 1, now)
            return True


def make_handler(feed, limiter):
    class Handler(BaseHTTPRequestHandler):
        server_version, sys_version = 'modi-readapi', ''
        protocol_version = 'HTTP/1.1'

        def log_message(self, *args):      # no access log (nothing sensitive in requests, but keep the journal quiet)
            pass

        def client(self):
            fwd = self.headers.get('X-Forwarded-For', '')
            return fwd.split(',')[-1].strip() if fwd else self.client_address[0]

        def send(self, code, body=b'', ctype='application/json; charset=utf-8', head=False, extra=()):
            self.send_response(code)
            self.send_header('Content-Type', ctype)
            self.send_header('Content-Length', str(len(body)))
            self.send_header('Access-Control-Allow-Origin', ALLOWED_ORIGIN)
            self.send_header('Vary', 'Origin')
            self.send_header('Cache-Control', 'no-store' if code != 200 else 'public, max-age=15')
            self.send_header('X-Content-Type-Options', 'nosniff')
            for k, v in extra:
                self.send_header(k, v)
            self.end_headers()
            if not head:
                self.wfile.write(body)

        def serve(self, head=False):
            path = self.path.split('?', 1)[0]
            if path not in ROUTES:
                return self.send(404, b'{"error":"not found"}', head=head)
            if not limiter.allow(self.client()):
                return self.send(429, b'{"error":"too many requests"}', head=head, extra=(('Retry-After', '10'),))
            body = feed.get(path)
            if body is None:
                return self.send(503, b'{"error":"warming up"}', head=head, extra=(('Retry-After', '30'),))
            self.send(200, body, head=head)

        def do_GET(self):
            self.serve()

        def do_HEAD(self):
            self.serve(head=True)

        def refuse(self):
            self.send(405, b'{"error":"read-only"}', extra=(('Allow', 'GET, HEAD'),))

        do_POST = do_PUT = do_PATCH = do_DELETE = do_OPTIONS = do_TRACE = do_CONNECT = refuse

    return Handler


def main(argv=None):
    ap = argparse.ArgumentParser(description='OKX 实盘只读接口（GET /live.json，白名单字段，无密钥）')
    ap.add_argument('--data', default=DEFAULT_DATA, help='okx_live 目录（只读）')
    ap.add_argument('--listen', default=DEFAULT_LISTEN)
    ap.add_argument('--port', type=int, default=DEFAULT_PORT)
    args = ap.parse_args(argv)
    if args.listen not in ('127.0.0.1', '::1', 'localhost'):
        raise SystemExit('只监听本机回环地址（对外由 Caddy 提供 HTTPS）')
    feed = Feed(args.data, make_client())
    stop = threading.Event()
    threading.Thread(target=feed.loop, args=(stop,), daemon=True).start()
    server = ThreadingHTTPServer((args.listen, args.port), make_handler(feed, RateLimit()))
    server.daemon_threads = True
    print(f'live_readapi 已启动：{args.listen}:{args.port} · 每 {REFRESH} 秒只读刷新 · 只提供 GET', flush=True)
    try:
        server.serve_forever()
    finally:
        stop.set()
        server.server_close()

if __name__ == '__main__':
    sys.exit(main())

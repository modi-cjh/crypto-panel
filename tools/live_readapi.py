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
  * Public candles (/candles, no key) for the coin page / position chart when the browser cannot reach OKX: strict
    parameter whitelist, one OKX public request per instId+bar+limit+after at most every CANDLE_TTL seconds.
  * Public mark prices of the open positions pushed as Server-Sent Events (/stream): while at least one browser is
    connected, a background thread polls the OKX public mark-price list (one request every MARK_POLL seconds, no key,
    whatever the number of clients) and every client gets the latest prices once a second. The account itself
    (equity base, positions, avgPx) still comes only from the minute refresh above.

Routes (every other method answers 405, every other path 404):
  /live.json     GET/HEAD {"snapshot": ..., "fills": ..., "trades": ..., "api": {...}}   same fields as live-data/*.json
  /tickers.json  GET/HEAD USDT perpetual tickers (instId, last, open24h, volCcy24h, ts) from the OKX public endpoint
  /healthz       GET/HEAD {"ok": true/false, "age_s": ...}
  /candles       GET/HEAD ?instId=BTC-USDT-SWAP&bar=1H&limit=300[&after=ms]  -> {"code":"0","msg":"","data":[[ts,o,h,l,c,vol,...]]}
                 (same shape as OKX market/candles; `after` reads market/history-candles, at most 100 rows); bad parameters 400
  /stream        GET only, text/event-stream: `data: {"ts":ms,"marks":{"SOL-USDT-SWAP":70.1},"live_at":"..."}` about once a
                 second (repeated every PING_EVERY seconds when unchanged); at most MAX_STREAMS connections (MAX_STREAMS_PER_IP per address), else 503

Credentials come from the environment (systemd EnvironmentFile, root-owned 0600); they are never printed, logged,
or written anywhere, and a document containing them (or anything shaped like a key) is refused by the secret check.
"""
import argparse
import json
import os
import re
import select
import socket
import subprocess
import sys
import threading
import time
from collections import deque
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs

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
CANDLE_BARS = frozenset(('1m', '3m', '5m', '15m', '30m', '1H', '2H', '4H', '6H', '12H', '1D', '2D', '3D', '1W', '1M',
                         '6Hutc', '12Hutc', '1Dutc', '2Dutc', '3Dutc', '1Wutc', '1Mutc'))
CANDLE_MAX, CANDLE_HISTORY_MAX, CANDLE_DEFAULT = 300, 100, 300
CANDLE_TTL = 4               # seconds: the same instId+bar+limit+after reaches OKX at most once per TTL
CANDLE_CACHE_MAX = 256
DIGITS_RE = re.compile(r'^[0-9]{1,13}$')
NUM_RE = re.compile(r'^-?[0-9]+(\.[0-9]+)?([eE][-+]?[0-9]+)?$')
MARK_POLL = 1.0              # seconds between OKX public mark-price reads while a browser is connected
PUSH_EVERY = 1.0             # seconds between pushes to each stream client
PING_EVERY = 15              # an unchanged event is repeated this often (keep-alive the browser can see)
STREAM_MAX_AGE = 1800        # a stream is closed after 30 minutes; EventSource reconnects by itself
MAX_STREAMS, MAX_STREAMS_PER_IP = 50, 4


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


def candle_query(params):
    """(instId, bar, limit, after) from the query string, or ValueError. Only whitelisted values ever reach OKX."""
    if set(params) - {'instId', 'bar', 'limit', 'after', 't'}:
        raise ValueError('unknown parameter')
    if any(len(v) != 1 for v in params.values()):
        raise ValueError('repeated parameter')
    one = {k: v[0] for k, v in params.items()}
    inst = ls.inst(one.get('instId'))
    if not inst:
        raise ValueError('instId')
    bar = one.get('bar', '1H')
    if bar not in CANDLE_BARS:
        raise ValueError('bar')
    after = one.get('after', '')
    if after and not DIGITS_RE.match(after):
        raise ValueError('after')
    limit = one.get('limit', str(CANDLE_DEFAULT))
    if not DIGITS_RE.match(limit) or not 1 <= int(limit) <= CANDLE_MAX:
        raise ValueError('limit')
    limit = min(int(limit), CANDLE_HISTORY_MAX) if after else int(limit)
    return inst, bar, limit, after


def build_candles(resp):
    """OKX candle rows kept only when every field is a plain number string (same shape as market/candles)."""
    if not ok(resp):
        raise AccountError('candles')
    rows = [[str(x) for x in r[:9]] for r in resp['data']
            if isinstance(r, list) and len(r) >= 5 and all(isinstance(x, str) and NUM_RE.match(x) for x in r[:9])]
    return {'code': '0', 'msg': '', 'data': rows}


def build_marks(resp, insts):
    """instId -> mark price for the wanted instruments, from the OKX public mark-price list."""
    if not ok(resp):
        raise AccountError('mark-price')
    wanted, out = set(insts), {}
    for r in resp['data']:
        inst = r.get('instId') if isinstance(r, dict) else None
        price = ls.number(r.get('markPx')) if inst in wanted else None
        if price and price > 0:
            out[inst] = price
    return out


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
        self.insts = ()                              # instIds of the served snapshot's positions (what /stream pushes)
        self.live_updated = None                     # snapshot updated_at, pushed so browsers re-read /live.json at once
        self.candles = {}                            # key -> (built_at, bytes)
        self.candle_lock = threading.Lock()          # one OKX candle request at a time

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
            self.insts = tuple(sorted({p['inst'] for p in snapshot.get('positions') or [] if ls.inst(p.get('inst'))}))
            self.live_updated = snapshot.get('updated_at')
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

    def get_candles(self, params):
        """Validated candle query -> served bytes (cached CANDLE_TTL seconds). ValueError: bad parameters; AccountError: OKX."""
        key = candle_query(params)
        now = self.clock()
        with self.candle_lock:
            hit = self.candles.get(key)
            if hit and 0 <= now - hit[0] < CANDLE_TTL:
                return hit[1]
            inst, bar, limit, after = key
            path = (f'/api/v5/market/history-candles?instId={inst}&bar={bar}&limit={limit}&after={after}' if after else
                    f'/api/v5/market/candles?instId={inst}&bar={bar}&limit={limit}')
            body = ls.checked(build_candles(self.public(path, timeout=6)), 'candles').encode()
            if len(self.candles) >= CANDLE_CACHE_MAX:
                for k in sorted(self.candles, key=lambda k: self.candles[k][0])[:CANDLE_CACHE_MAX // 4]:
                    del self.candles[k]
            self.candles[key] = (now, body)
            return body

    def stream_targets(self):
        with self.lock:
            return self.insts, self.live_updated

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


# ---- push (Server-Sent Events) ---------------------------------------------------------------------
class Hub:
    """Latest public mark prices for the positions, shared by every /stream client.

    `poll()` runs in its own thread: it reads OKX only while someone is connected and there are positions to price,
    one public request per MARK_POLL seconds in total (never per client)."""
    def __init__(self, feed, public=public_get, clock=time.time, max_streams=MAX_STREAMS, per_ip=MAX_STREAMS_PER_IP):
        self.feed, self.public, self.clock = feed, public, clock
        self.max_streams, self.per_ip = max_streams, per_ip
        self.lock = threading.Lock()
        self.clients = {}                       # address -> open streams
        self.marks, self.marks_at = {}, 0.0
        self.polls = 0

    @property
    def count(self):
        with self.lock:
            return sum(self.clients.values())

    def join(self, who):
        with self.lock:
            if sum(self.clients.values()) >= self.max_streams or self.clients.get(who, 0) >= self.per_ip:
                return False
            self.clients[who] = self.clients.get(who, 0) + 1
            return True

    def leave(self, who):
        with self.lock:
            n = self.clients.get(who, 0) - 1
            if n > 0:
                self.clients[who] = n
            else:
                self.clients.pop(who, None)

    def poll_once(self):
        """One public mark-price read when it is needed; True when prices were updated."""
        insts, _ = self.feed.stream_targets()
        if not insts or not self.count:
            return False
        marks = build_marks(self.public('/api/v5/public/mark-price?instType=SWAP', timeout=5), insts)
        self.polls += 1
        with self.lock:
            self.marks, self.marks_at = marks, self.clock()
        return True

    def poll(self, stop):
        failures = 0
        while not stop.is_set():
            try:
                self.poll_once()
                failures = 0
            except Exception:                   # noqa: BLE001  (network / OKX error: back off, keep the last prices)
                failures += 1
            stop.wait(MARK_POLL if not failures else min(30, 2 ** failures))

    def message(self):
        """The event pushed to every client: prices of the snapshot's current positions only (fresh ones)."""
        insts, live_at = self.feed.stream_targets()
        with self.lock:
            fresh = self.clock() - self.marks_at < 10
            marks = {k: v for k, v in self.marks.items() if k in insts} if fresh else {}
            ts = int(self.marks_at * 1000) if fresh else None
        return json.dumps({'ts': ts, 'marks': marks, 'live_at': live_at}, separators=(',', ':'))


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


def make_handler(feed, limiter, hub=None, push_every=PUSH_EVERY, max_age=STREAM_MAX_AGE):
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
            path, _, query = self.path.partition('?')
            if path == '/stream' and hub is not None:
                return self.refuse() if head else self.stream()
            if path not in ROUTES and path != '/candles':
                return self.send(404, b'{"error":"not found"}', head=head)
            if not limiter.allow(self.client()):
                return self.send(429, b'{"error":"too many requests"}', head=head, extra=(('Retry-After', '10'),))
            if path == '/candles':
                return self.candles(query, head)
            body = feed.get(path)
            if body is None:
                return self.send(503, b'{"error":"warming up"}', head=head, extra=(('Retry-After', '30'),))
            self.send(200, body, head=head)

        def candles(self, query, head):
            try:
                params = parse_qs(query, keep_blank_values=True, strict_parsing=bool(query), max_num_fields=8)
                body = feed.get_candles(params)
            except ValueError:
                return self.send(400, b'{"error":"bad parameters"}', head=head)
            except Exception:                   # noqa: BLE001  (OKX unreachable / error answer: never echoed)
                return self.send(502, b'{"error":"upstream unavailable"}', head=head, extra=(('Retry-After', '5'),))
            self.send(200, body, head=head)

        def stream(self):
            who = self.client()
            self.close_connection = True               # a stream (or its refusal) always ends the connection
            if not limiter.allow(who):
                return self.send(429, b'{"error":"too many requests"}', extra=(('Retry-After', '10'),))
            if not hub.join(who):
                return self.send(503, b'{"error":"too many streams"}', extra=(('Retry-After', '30'),))
            try:
                self.send_response(200)
                for k, v in (('Content-Type', 'text/event-stream; charset=utf-8'), ('Cache-Control', 'no-store, no-transform'),
                             ('Access-Control-Allow-Origin', ALLOWED_ORIGIN), ('Vary', 'Origin'), ('Connection', 'close'),
                             ('X-Accel-Buffering', 'no'), ('X-Content-Type-Options', 'nosniff')):
                    self.send_header(k, v)
                self.end_headers()
                self.wfile.write(b'retry: 3000\n: modi live stream\n\n')
                self.wfile.flush()
                started = last_ping = time.monotonic()
                last = None
                while time.monotonic() - started < max_age and not self.gone():
                    msg = hub.message()
                    now = time.monotonic()
                    if msg != last or now - last_ping >= PING_EVERY:   # unchanged prices are repeated as the keep-alive
                        self.wfile.write(f'data: {msg}\n\n'.encode())
                        last, last_ping = msg, now
                    self.wfile.flush()
                    time.sleep(push_every)
            except (OSError, ValueError):       # client went away (broken pipe / reset): just clean up
                pass
            finally:
                hub.leave(who)

        def gone(self):
            """The client closed its side (readable with EOF, or an error): stop pushing and free the slot."""
            try:
                readable, _, _ = select.select([self.connection], [], [], 0)
                return bool(readable) and not self.connection.recv(1, socket.MSG_PEEK)
            except (OSError, ValueError):
                return True

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
    hub = Hub(feed)
    stop = threading.Event()
    threading.Thread(target=feed.loop, args=(stop,), daemon=True).start()
    threading.Thread(target=hub.poll, args=(stop,), daemon=True).start()
    server = ThreadingHTTPServer((args.listen, args.port), make_handler(feed, RateLimit(), hub))
    server.daemon_threads = True
    print(f'live_readapi 已启动：{args.listen}:{args.port} · 每 {REFRESH} 秒只读刷新 · 只提供 GET（含 /candles、/stream 推送）', flush=True)
    try:
        server.serve_forever()
    finally:
        stop.set()
        server.server_close()

if __name__ == '__main__':
    sys.exit(main())

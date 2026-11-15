"""tools/live_readapi.py: read-only live API. No network: the OKX client and the public endpoint are fakes,
the trader's files are written to a temporary directory."""
import http.client
import json
import os
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'tools'))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import live_readapi as ra  # noqa: E402
import live_sync as ls  # noqa: E402
from test_live_sync import ACTIONS, FAKE_KEY, FAKE_SECRET, sample_state, sample_summary  # noqa: E402

ORDER_ID = '1234567890123456789'
CL_ORD = 'abcDEF1234567890abcdef'


def okx_rows(**over):
    return {
        'config': {'code': '0', 'data': [{'perm': 'read_only,trade', 'acctLv': '2', 'posMode': 'long_short_mode', 'uid': '998877665544332211'}]},
        'balance': {'code': '0', 'data': [{'details': [{'ccy': 'USDT', 'eq': '130.5', 'availBal': '100.25', 'frozenBal': '30'}]}]},
        'positions': {'code': '0', 'data': [
            {'instId': 'SOL-USDT-SWAP', 'posSide': 'short', 'pos': '1.58', 'avgPx': '72.43', 'markPx': '70.1', 'upl': '3.68',
             'uplRatio': '0.32', 'lever': '10', 'margin': '', 'imr': '11.1', 'mgnMode': 'cross', 'posId': ORDER_ID,
             'tradeId': '998877', 'cTime': '1794751218000'},
            {'instId': 'ETH-USDT-SWAP', 'posSide': 'net', 'pos': '-2', 'avgPx': '3000', 'markPx': '2990', 'upl': '0.2',
             'lever': '3', 'margin': '20', 'mgnMode': 'isolated'},
            {'instId': 'BTC-USD-SWAP', 'posSide': 'long', 'pos': '1', 'avgPx': '90000'},                 # coin-margined: not shown
            {'instId': 'DOGE-USDT-SWAP', 'posSide': 'long', 'pos': '0', 'avgPx': '0.1'},                 # closed: not shown
        ]},
        **over,
    }


class FakeClient:
    """GET-only fake of okx_demo_trader.OKX (dry-run): any other method raises, like the real client."""
    dry_run = True

    def __init__(self, rows=None, fail=False):
        self.rows, self.fail, self.calls = rows or okx_rows(), fail, []

    def request(self, method, path, body=None):
        self.calls.append((method, path))
        if method != 'GET':
            raise AssertionError('non-GET request')
        if self.fail:
            raise RuntimeError(f'GET {path} failed key={FAKE_KEY}')        # an exception text must never be served
        for name in ('config', 'balance', 'positions'):
            if f'/account/{name}' in path:
                return self.rows[name]
        raise AssertionError(path)


CANDLE_ROWS = [['1794751200000', '91000.5', '91200', '90900', '91100', '12.5', '0.125', '11387.5', '0'],
               ['1794747600000', '90900', '91050', '90800', '91000.5', '10', '0.1', '9100', '1']]
PUBLIC_CALLS = []


def fake_public(path, timeout=10):
    PUBLIC_CALLS.append(path)
    if 'mark-price' in path:
        return {'code': '0', 'data': [{'instId': 'SOL-USDT-SWAP', 'markPx': '70.5', 'ts': '1794751218000'},
                                      {'instId': 'ETH-USDT-SWAP', 'markPx': '2990.1'}, {'instId': 'BTC-USDT-SWAP', 'markPx': '91000'}]}
    if 'candles' in path:
        if 'FAIL-USDT' in path:
            return {'code': '51001', 'msg': 'Instrument ID does not exist', 'data': []}
        return {'code': '0', 'msg': '', 'data': CANDLE_ROWS + [['1794744000000', '<script>', '1', '1', '1', '1'], 'bad']}
    if 'instruments' in path:
        return {'code': '0', 'data': [{'instId': 'SOL-USDT-SWAP', 'ctVal': '1'}, {'instId': 'ETH-USDT-SWAP', 'ctVal': '0.1'}]}
    if 'tickers' in path:
        return {'code': '0', 'data': [
            {'instId': 'BTC-USDT-SWAP', 'last': '91000.5', 'open24h': '90000', 'volCcy24h': '12345.6', 'ts': '1794751218000', 'bidPx': '1'},
            {'instId': 'BTC-USD-SWAP', 'last': '91000'},
            {'instId': 'ETH-USDT-SWAP', 'last': '0'},
        ]}
    raise AssertionError(path)


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        (self.dir / 'summary.json').write_text(json.dumps(sample_summary()))
        (self.dir / 'state.json').write_text(json.dumps(sample_state()))
        (self.dir / 'actions.jsonl').write_text(ACTIONS)
        self.now = [1794760000.0]
        self.env = mock.patch.dict(os.environ, {'OKX_API_KEY': FAKE_KEY, 'OKX_API_SECRET': FAKE_SECRET, 'OKX_API_PASSPHRASE': 'Pass-Phrase-77'})
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def feed(self, client=None):
        return ra.Feed(self.dir, client or FakeClient(), public=fake_public, clock=lambda: self.now[0])


class Build(Base):
    def test_fresh_account_whitelisted(self):
        f = self.feed()
        self.assertTrue(f.refresh_live())
        text = f.get('/live.json').decode()
        doc = json.loads(text)
        self.assertEqual(doc['api']['account'], 'okx')
        snap = doc['snapshot']
        self.assertEqual(snap['equity'], 130.5)
        self.assertEqual(snap['available'], 100.25)
        self.assertEqual(snap['updated_at'], ra.iso(self.now[0]))
        self.assertEqual(snap['day_start'], 126.670001)                  # from the trader's summary
        self.assertEqual([(p['inst'], p['side']) for p in snap['positions']], [('SOL-USDT-SWAP', 'short'), ('ETH-USDT-SWAP', 'short')])
        sol, eth = snap['positions']
        self.assertEqual(sol['stop'], 80.18402)                          # merged from state.json mirrors
        self.assertEqual(sol['champion'], 's15653')
        self.assertEqual(sol['margin'], 11.1)                               # imr when margin is empty (cross)
        self.assertEqual(sol['ctVal'], 1.0)
        self.assertTrue(sol['ours'])
        self.assertFalse(eth['ours'])
        self.assertEqual(eth['ctVal'], 0.1)
        self.assertEqual(snap['history'][-1], [int(self.now[0] * 1000), 130.5])
        self.assertTrue(doc['fills']['fills'])
        self.assertIn('stats', doc['trades'])
        for bad in (ORDER_ID, CL_ORD, FAKE_KEY, FAKE_SECRET, 'Pass-Phrase-77', 'posId', 'tradeId', 'uid', '998877665544332211', 'ordId', 'clOrdId'):
            self.assertNotIn(bad, text)
        self.assertFalse(ls.secret_problems(text))
        self.assertEqual({m for m, _ in f.client.calls}, {'GET'})

    def test_okx_failure_keeps_last_then_falls_back_to_file(self):
        client = FakeClient()
        f = self.feed(client)
        f.refresh_live()
        client.fail = True
        self.now[0] += 120
        f.refresh_live()
        doc = json.loads(f.get('/live.json'))
        self.assertEqual(doc['api']['account'], 'okx')                      # last good read, still recent
        self.assertEqual(doc['snapshot']['equity'], 130.5)
        self.assertNotIn(FAKE_KEY, f.last_error or '')
        self.now[0] += ra.STALE_KEEP
        f.refresh_live()
        text = f.get('/live.json').decode()
        doc = json.loads(text)
        self.assertEqual(doc['api']['account'], 'file')                     # the trader's hourly summary as it is
        self.assertEqual(doc['snapshot']['updated_at'], '2026-11-15T14:02:46Z')
        self.assertNotIn(FAKE_KEY, text)

    def test_withdraw_permission_refused(self):
        rows = okx_rows(config={'code': '0', 'data': [{'perm': 'read_only,withdraw,trade'}]})
        f = self.feed(FakeClient(rows))
        f.refresh_live()
        self.assertEqual(json.loads(f.get('/live.json'))['api']['account'], 'file')
        self.assertIn('提现', f.last_error)

    def test_secret_in_served_document_refused(self):
        # A credential reaching a whitelisted field (here: the env secret equal to a champion id) blocks the update.
        state = sample_state()
        state['mirrored']['SOL-USDT-SWAP|1794700800000']['champion'] = 'Pass-Phrase-77'
        (self.dir / 'state.json').write_text(json.dumps(state))
        f = self.feed()
        self.assertFalse(f.refresh_live())
        self.assertIsNone(f.get('/live.json'))
        self.assertIn('密钥检查', f.last_error)

    def test_demo_data_refused(self):
        (self.dir / 'state.json').write_text(json.dumps(sample_state(mode='demo')))
        f = self.feed()
        self.assertFalse(f.refresh_live())
        self.assertIsNone(f.get('/live.json'))

    def test_tickers(self):
        f = self.feed()
        self.assertTrue(f.refresh_tickers())
        doc = json.loads(f.get('/tickers.json'))
        self.assertEqual(doc['tickers'], [{'instId': 'BTC-USDT-SWAP', 'last': 91000.5, 'open24h': 90000.0, 'volCcy24h': 12345.6, 'ts': 1794751218000.0}])

    def test_net_mode_side(self):
        self.assertEqual(ra.position_side({'posSide': 'net', 'pos': '-3'}), 'short')
        self.assertEqual(ra.position_side({'posSide': 'net', 'pos': '3'}), 'long')
        self.assertEqual(ra.position_side({'posSide': 'long', 'pos': '3'}), 'long')

    def test_client_is_read_only(self):
        env = {'OKX_TRADING_MODE': 'live', 'OKX_DRY_RUN': '0', 'OKX_API_KEY': FAKE_KEY, 'OKX_API_SECRET': FAKE_SECRET,
               'OKX_API_PASSPHRASE': 'Pass-Phrase-77'}
        client = ra.make_client(env)
        self.assertTrue(client.dry_run)                                     # forced, even with OKX_DRY_RUN=0
        import okx_demo_trader as t
        with self.assertRaises(t.OKXError):
            client.request('POST', '/api/v5/trade/order', {'instId': 'BTC-USDT-SWAP'})
        self.assertNotIn(FAKE_SECRET, repr(client))
        with self.assertRaises(SystemExit):
            ra.make_client({**env, 'OKX_TRADING_MODE': 'demo'})
        with self.assertRaises(SystemExit):
            ra.make_client({**env, 'GITHUB_ACTIONS': 'true'})
        with self.assertRaises(SystemExit):
            ra.make_client({**env, 'OKX_API_SECRET': ''})


class Http(Base):
    def setUp(self):
        super().setUp()
        self.f = self.feed()
        self.f.refresh_live()
        self.f.refresh_tickers()
        self.hub = ra.Hub(self.f, public=fake_public, clock=lambda: self.now[0], max_streams=3, per_ip=2)
        handler = ra.make_handler(self.f, ra.RateLimit(per_min=60, burst=5), self.hub, push_every=0.05, max_age=5)
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.base = f'http://127.0.0.1:{self.server.server_address[1]}'

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        super().tearDown()

    def call(self, path, method='GET', ip=None, body=None):
        req = urllib.request.Request(self.base + path, method=method, data=body,
                                     headers={'X-Forwarded-For': ip or '203.0.113.' + str(abs(hash(path + method)) % 200)})
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                return r.status, dict(r.headers), r.read()
        except urllib.error.HTTPError as e:
            return e.code, dict(e.headers), e.read()

    def test_get_live(self):
        code, headers, body = self.call('/live.json?t=1')
        self.assertEqual(code, 200)
        self.assertEqual(headers['Access-Control-Allow-Origin'], 'https://modi-cjh.github.io')
        self.assertEqual(json.loads(body)['snapshot']['trading_mode'], 'live')
        self.assertEqual(self.call('/tickers.json')[0], 200)
        self.assertTrue(json.loads(self.call('/healthz')[2])['ok'])

    def test_only_get(self):
        for method in ('POST', 'PUT', 'DELETE', 'PATCH', 'OPTIONS'):
            code, headers, _ = self.call('/live.json', method, body=b'{}' if method in ('POST', 'PUT', 'PATCH') else None)
            self.assertEqual(code, 405, method)
            self.assertEqual(headers['Allow'], 'GET, HEAD')
        for path in ('/', '/snapshot.json', '/../state.json', '/api/v5/trade/order', '/summary.json'):
            self.assertEqual(self.call(path)[0], 404, path)
        self.assertEqual(self.f.client.calls and {m for m, _ in self.f.client.calls}, {'GET'})

    def test_rate_limit(self):
        codes = [self.call('/live.json', ip='198.51.100.7')[0] for _ in range(8)]
        self.assertEqual(codes[:5], [200] * 5)
        self.assertIn(429, codes[5:])
        self.assertEqual(self.call('/live.json', ip='198.51.100.8')[0], 200)   # other clients unaffected

    def test_warming_up(self):
        empty = ra.Feed(self.dir, FakeClient(), public=fake_public)
        server = ThreadingHTTPServer(('127.0.0.1', 0), ra.make_handler(empty, ra.RateLimit()))
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            with self.assertRaises(urllib.error.HTTPError) as e:
                urllib.request.urlopen(f'http://127.0.0.1:{server.server_address[1]}/live.json', timeout=5)
            self.assertEqual(e.exception.code, 503)
            e.exception.close()
        finally:
            server.shutdown()
            server.server_close()


class Candles(Base):
    def setUp(self):
        super().setUp()
        PUBLIC_CALLS.clear()

    def test_query_whitelist(self):
        q = lambda **kw: ra.candle_query({k: [v] for k, v in kw.items()})
        self.assertEqual(q(instId='BTC-USDT-SWAP', bar='1H', limit='10'), ('BTC-USDT-SWAP', '1H', 10, ''))
        self.assertEqual(q(instId='BTC-USDT-SWAP'), ('BTC-USDT-SWAP', '1H', 300, ''))
        self.assertEqual(q(instId='BTC-USDT-SWAP', bar='1Dutc', limit='300', after='1794751200000'), ('BTC-USDT-SWAP', '1Dutc', 100, '1794751200000'))
        for bad in (dict(instId='btc-usdt-swap'), dict(instId='BTC-USDT'), dict(instId='BTC-USD-SWAP'), dict(instId='BTC-USDT-SWAP&x=1'),
                    dict(instId='../../etc/passwd'), dict(instId='BTC-USDT-SWAP', bar='7H'), dict(instId='BTC-USDT-SWAP', bar='1h'),
                    dict(instId='BTC-USDT-SWAP', limit='301'), dict(instId='BTC-USDT-SWAP', limit='0'), dict(instId='BTC-USDT-SWAP', limit='-1'),
                    dict(instId='BTC-USDT-SWAP', limit='1e2'), dict(instId='BTC-USDT-SWAP', after='abc'),
                    dict(instId='BTC-USDT-SWAP', after='12345678901234'), dict(instId='BTC-USDT-SWAP', apiKey='x'), {}):
            with self.assertRaises(ValueError, msg=bad):
                q(**bad)
        with self.assertRaises(ValueError):
            ra.candle_query({'instId': ['BTC-USDT-SWAP', 'ETH-USDT-SWAP']})

    def test_rows_and_short_cache(self):
        f = self.feed()
        p = {'instId': ['BTC-USDT-SWAP'], 'bar': ['1H'], 'limit': ['10']}
        doc = json.loads(f.get_candles(p))
        self.assertEqual(doc, {'code': '0', 'msg': '', 'data': CANDLE_ROWS})          # junk rows dropped, OKX shape kept
        self.assertEqual(PUBLIC_CALLS, ['/api/v5/market/candles?instId=BTC-USDT-SWAP&bar=1H&limit=10'])
        for _ in range(5):
            f.get_candles(p)
        self.assertEqual(len(PUBLIC_CALLS), 1)                                         # same key within the TTL: cached
        f.get_candles({**p, 'limit': ['20']})
        self.assertEqual(len(PUBLIC_CALLS), 2)
        self.now[0] += ra.CANDLE_TTL
        f.get_candles(p)
        self.assertEqual(len(PUBLIC_CALLS), 3)                                         # expired: one new request
        f.get_candles({**p, 'after': ['1794751200000'], 'limit': ['300']})
        self.assertEqual(PUBLIC_CALLS[-1], '/api/v5/market/history-candles?instId=BTC-USDT-SWAP&bar=1H&limit=100&after=1794751200000')
        with self.assertRaises(ra.AccountError):
            f.get_candles({'instId': ['FAIL-USDT-SWAP']})
        self.assertFalse(f.client.calls)                                               # never the signed client


class Push(Http):
    def open_stream(self, ip='192.0.2.10'):
        conn = http.client.HTTPConnection('127.0.0.1', self.server.server_address[1], timeout=5)
        conn.request('GET', '/stream', headers={'X-Forwarded-For': ip})
        resp = conn.getresponse()
        conn.close = lambda c=conn.close, r=resp: (r.close(), c())     # closing the connection also drops the response's socket
        return conn, resp

    def events(self, resp, n):
        out = []
        while len(out) < n:
            line = resp.fp.readline().decode()
            if line.startswith('data: '):
                out.append(json.loads(line[6:]))
        return out

    def wait_count(self, n):
        for _ in range(100):
            if self.hub.count == n:
                return
            time.sleep(0.02)
        self.fail(f'stream count {self.hub.count} != {n}')

    def test_stream_pushes_position_marks(self):
        PUBLIC_CALLS.clear()
        self.assertFalse(self.hub.poll_once())                       # nobody connected: OKX is not read
        self.assertEqual(PUBLIC_CALLS, [])
        conn, resp = self.open_stream()
        try:
            self.assertEqual(resp.status, 200)
            self.assertTrue(resp.getheader('Content-Type').startswith('text/event-stream'))
            self.assertEqual(resp.getheader('Access-Control-Allow-Origin'), 'https://modi-cjh.github.io')
            self.assertEqual(resp.getheader('Content-Length'), None)
            first = self.events(resp, 1)[0]
            self.assertEqual(first['marks'], {})                          # nothing polled yet
            self.assertEqual(first['live_at'], ra.iso(self.now[0]))
            self.wait_count(1)
            self.assertTrue(self.hub.poll_once())
            self.assertEqual(PUBLIC_CALLS, ['/api/v5/public/mark-price?instType=SWAP'])
            ev = self.events(resp, 1)[0]
            self.assertEqual(ev['marks'], {'SOL-USDT-SWAP': 70.5, 'ETH-USDT-SWAP': 2990.1})   # positions only, not BTC
            self.assertEqual(ev['ts'], int(self.now[0] * 1000))
            self.now[0] += 1
            self.hub.poll_once()
            self.assertEqual(self.events(resp, 1)[0]['ts'], int(self.now[0] * 1000))   # a new push every poll
            self.now[0] += 11
            self.assertEqual(self.events(resp, 1)[0]['marks'], {})          # stale prices are not pushed
            self.assertEqual({m for m, _ in self.f.client.calls}, {'GET'})
        finally:
            conn.close()
        self.wait_count(0)                                                  # client gone: slot released

    def test_stream_limits(self):
        a = [self.open_stream('192.0.2.1') for _ in range(2)]
        try:
            for _, r in a:
                self.assertEqual(r.status, 200)
            conn, r = self.open_stream('192.0.2.1')                         # per address cap
            self.assertEqual(r.status, 503)
            conn.close()
            b = self.open_stream('192.0.2.2')
            self.assertEqual(b[1].status, 200)
            conn, r = self.open_stream('192.0.2.3')                         # total cap
            self.assertEqual(r.status, 503)
            self.assertEqual(r.getheader('Access-Control-Allow-Origin'), 'https://modi-cjh.github.io')
            conn.close()
            b[0].close()
        finally:
            for c, _ in a:
                c.close()
        self.wait_count(0)
        self.assertEqual(self.open_stream('192.0.2.3')[1].status, 200)

    def test_stream_get_only_and_rate_limited(self):
        for method in ('POST', 'PUT', 'DELETE', 'PATCH', 'OPTIONS', 'HEAD'):
            code, headers, _ = self.call('/stream', method, body=b'{}' if method in ('POST', 'PUT', 'PATCH') else None)
            self.assertEqual(code, 405, method)
        codes = []
        for _ in range(8):
            conn, r = self.open_stream('198.51.100.9')
            codes.append(r.status)
            conn.close()
        self.assertIn(429, codes)

    def test_candles_http(self):
        code, headers, body = self.call('/candles?instId=BTC-USDT-SWAP&bar=1H&limit=10')
        self.assertEqual(code, 200)
        self.assertEqual(headers['Access-Control-Allow-Origin'], 'https://modi-cjh.github.io')
        self.assertEqual(json.loads(body)['data'], CANDLE_ROWS)
        for q in ('', '?instId=x', '?instId=BTC-USDT-SWAP&bar=2h', '?instId=BTC-USDT-SWAP&limit=1000', '?instId=BTC-USDT-SWAP&foo=1',
                  '?instId=BTC-USDT-SWAP&&&', '?instId=%00', '?' + '&'.join(f'a{i}=1' for i in range(20))):
            code, _, body = self.call('/candles' + q, ip='192.0.2.%d' % (60 + len(q) % 100))
            self.assertEqual(code, 400, q)
            self.assertEqual(json.loads(body), {'error': 'bad parameters'})
        code, _, body = self.call('/candles?instId=FAIL-USDT-SWAP', ip='192.0.2.51')
        self.assertEqual(code, 502)
        self.assertNotIn(b'Instrument', body)
        for method in ('POST', 'DELETE'):
            self.assertEqual(self.call('/candles?instId=BTC-USDT-SWAP', method, body=b'{}' if method == 'POST' else None)[0], 405)
        self.assertEqual(self.call('/candles/x')[0], 404)
        codes = [self.call('/candles?instId=BTC-USDT-SWAP', ip='198.51.100.70')[0] for _ in range(8)]
        self.assertEqual(codes[:5], [200] * 5)
        self.assertIn(429, codes[5:])


if __name__ == '__main__':
    unittest.main()

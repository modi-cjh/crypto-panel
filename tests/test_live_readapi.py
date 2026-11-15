"""tools/live_readapi.py: read-only live API. No network: the OKX client and the public endpoint are fakes,
the trader's files are written to a temporary directory."""
import json
import os
import sys
import tempfile
import threading
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


def fake_public(path):
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
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), ra.make_handler(self.f, ra.RateLimit(per_min=60, burst=5)))
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


if __name__ == '__main__':
    unittest.main()

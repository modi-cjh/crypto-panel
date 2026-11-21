"""demo / live switch for the OKX mirror: headers, dry-run default, credentials from env only, unchanged risk rules.

Nothing here reaches OKX: every request goes to a fake `curl` on PATH that answers like the exchange and records what
it was sent. Credentials are throwaway test strings.
"""
import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'tools'))
import okx_demo_trader as odt  # noqa: E402
import okx_demo_check as chk  # noqa: E402
import okx_demo_smoke as smoke  # noqa: E402
import stop_demo  # noqa: E402
import fetch_signals  # noqa: E402

KEY, SECRET, PASS = 'TESTKEY-0001', 'TESTSECRET-0002', 'TESTPASS-0003'

FAKE_CURL = r'''#!/usr/bin/env python3
import json, os, sys
log = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'requests.jsonl')
argv = sys.argv[1:]
if '-K' in argv:
    cfg = sys.stdin.read()
    headers, url, method, data = {}, '', 'GET', ''
    for line in cfg.splitlines():
        k, _, v = line.partition(' = ')
        v = v[1:-1]
        if k == 'header':
            hk, _, hv = v.partition(': '); headers[hk] = hv
        elif k == 'url': url = v
        elif k == 'request': method = v
        elif k == 'data': data = v.replace('\\"', '"').replace('\\\\', '\\')
else:
    cfg, headers, method, data = '', {}, 'GET', ''
    url = next(a for a in argv if a.startswith('http'))
path = url.split('okx.com', 1)[-1]
with open(log, 'a') as f:
    f.write(json.dumps(dict(argv=argv, method=method, path=path, headers=headers, body=data)) + '\n')
def out(x): print(json.dumps(x)); sys.exit(0)
if method != 'GET':
    out({'code': '0', 'data': [{'sCode': '0', 'ordId': '7'}]})
spec = dict(instId='BTC-USDT-SWAP', ctVal='0.01', lotSz='0.01', minSz='0.01', tickSz='0.1', lever='100')
if path.startswith('/api/v5/account/config'): out({'code': '0', 'data': [{'acctLv': '2', 'perm': 'read_only,trade', 'posMode': 'net_mode'}]})
if path.startswith('/api/v5/account/balance'): out({'code': '0', 'data': [{'details': [{'ccy': 'USDT', 'eq': '10000', 'availBal': '10000'}]}]})
if path.startswith('/api/v5/account/positions'): out({'code': '0', 'data': []})
if path.startswith('/api/v5/trade/orders-algo-pending'): out({'code': '0', 'data': []})
if path.startswith('/api/v5/public/instruments'): out({'code': '0', 'data': [spec]})
if path.startswith('/api/v5/market/ticker'): out({'code': '0', 'data': [{'last': '70000'}]})
out({'code': '0', 'data': []})
'''


def signals(age_hours=0.5):
    now = datetime.now(timezone.utc)
    opened = int(now.timestamp() * 1000) - 20 * 60_000
    ts = datetime.fromtimestamp(now.timestamp() - age_hours * 3600, timezone.utc).isoformat()
    return dict(updated_at=ts, champion='c1', team=['c1'], instruments=['BTC-USDT-SWAP'],
                positions={'BTC-USDT-SWAP': dict(side=1, entry=70000.0, stop=68600.0, tp=None, opened=opened, strategy='evolved',
                                                 leverage=7, risk=.03, rank=1, champion='c1')})


class LiveModeBase(unittest.TestCase):
    GLOBALS = ('INSTRUMENTS', 'RISK', 'SAME_SIDE_CAP', 'ACTIVE_PROFILE', 'DAILY_HALT', 'MARGIN_MODE')

    def setUp(self):
        saved = {k: getattr(odt, k) for k in self.GLOBALS}
        self.addCleanup(lambda: [setattr(odt, k, v) for k, v in saved.items()])
        self.tmp = Path(tempfile.mkdtemp())
        self.bin = self.tmp / 'bin'; self.bin.mkdir()
        (self.bin / 'curl').write_text(FAKE_CURL); (self.bin / 'curl').chmod(0o755)
        self.data = self.tmp / 'data'
        (self.data / 'evolve').mkdir(parents=True)
        self.write_signals(signals())

    def write_signals(self, sig):
        (self.data / 'evolve' / 'signals.json').write_text(json.dumps(sig))

    def env(self, **extra):
        base = {'PATH': f"{self.bin}{os.pathsep}{os.environ.get('PATH', '')}", 'OKX_API_KEY': KEY, 'OKX_API_SECRET': SECRET,
                'OKX_API_PASSPHRASE': PASS}
        base.update(extra)
        return {k: v for k, v in base.items() if v is not None}

    def requests(self):
        log = self.bin / 'requests.jsonl'
        rows = [json.loads(l) for l in log.read_text().splitlines()] if log.exists() else []
        log.unlink(missing_ok=True)
        return rows

    def main(self, action='run', **env):
        out, err = io.StringIO(), io.StringIO()
        code = None
        with mock.patch.dict(os.environ, self.env(**env), clear=True), mock.patch.object(sys, 'argv', ['okx_demo_trader.py', '--data', str(self.data), '--action', action]), \
                redirect_stdout(out), redirect_stderr(err):
            try:
                odt.main()
            except SystemExit as exc:
                code = exc.code
        return out.getvalue() + err.getvalue() + (str(code) if code else ''), code

    def resume(self, mode):
        path = self.data / odt.STATE_DIRS[mode] / 'state.json'
        state = json.loads(path.read_text()); state['paused'] = False; path.write_text(json.dumps(state))


class ModeSwitchTests(unittest.TestCase):
    def test_mode_defaults_to_demo_and_only_accepts_demo_or_live(self):
        self.assertEqual(odt.trading_mode({}), 'demo')
        self.assertEqual(odt.trading_mode({'OKX_TRADING_MODE': ' LIVE '}), 'live')
        for bad in ('real', 'prod', '1', 'livee'):
            with self.assertRaises(SystemExit):
                odt.trading_mode({'OKX_TRADING_MODE': bad})

    def test_live_is_refused_in_github_actions_and_in_the_demo_runtime_service(self):
        with self.assertRaises(SystemExit):
            odt.trading_mode({'OKX_TRADING_MODE': 'live', 'GITHUB_ACTIONS': 'true'})
        with self.assertRaises(SystemExit):
            odt.trading_mode({'OKX_TRADING_MODE': 'live', 'PANEL_RUNTIME_CONTROL_FILE': '/x/control.json'})
        self.assertEqual(odt.trading_mode({'GITHUB_ACTIONS': 'true'}), 'demo')   # the scheduled demo mirror is unaffected

    def test_dry_run_is_on_by_default_in_live_and_only_an_explicit_zero_turns_it_off(self):
        self.assertTrue(odt.dry_run_forced('live', {}))
        for v in ('1', 'true', 'false', 'no', 'off', ' ', '00'):
            self.assertTrue(odt.dry_run_forced('live', {'OKX_DRY_RUN': v}), v)
        self.assertFalse(odt.dry_run_forced('live', {'OKX_DRY_RUN': '0'}))
        # demo keeps its existing default (the GitHub demo mirror places demo orders); OKX_DRY_RUN=1 still forces a dry run
        self.assertFalse(odt.dry_run_forced('demo', {}))
        self.assertTrue(odt.dry_run_forced('demo', {'OKX_DRY_RUN': '1'}))

    def test_demo_headers_carry_the_simulated_flag_and_live_headers_never_do(self):
        demo = odt.signed_headers('k', 's', 't', 'p', 'demo')
        live = odt.signed_headers('k', 's', 't', 'p', 'live')
        self.assertEqual(demo['x-simulated-trading'], '1')
        self.assertNotIn('x-simulated-trading', {k.lower() for k in live})
        self.assertEqual({k: v for k, v in demo.items() if k != 'x-simulated-trading'}, live)
        with self.assertRaises(SystemExit):
            odt.signed_headers('k', 's', 't', 'p', 'real')

    def test_demo_mode_still_refuses_a_tampered_simulated_flag(self):
        self.addCleanup(setattr, odt, 'SIMULATED_VALUE', '1')
        odt.SIMULATED_VALUE = '0'
        with self.assertRaises(SystemExit):
            odt.signed_headers('k', 's', 't', 'p', 'demo')
        odt.signed_headers('k', 's', 't', 'p', 'live')   # live does not use the flag at all

    def test_client_repr_never_shows_credentials(self):
        c = odt.OKX(KEY, SECRET, PASS, mode='live', dry_run=True)
        for s in (KEY, SECRET, PASS):
            self.assertNotIn(s, repr(c)); self.assertNotIn(s, str(c))

    def test_risk_parameters_are_the_same_in_both_modes(self):
        # The switch only changes the transport (header / state folder / dry-run); the evolved profile is shared.
        self.assertEqual(odt.RISK_RANGE, (.05, .10)); self.assertEqual(odt.MAX_TRADE_RISK, .10)
        self.assertEqual(odt.LEADER_RISK, .10); self.assertEqual(odt.CROWD_STEPS, ((10, .5),))
        self.assertEqual(odt.EVOLVED_MAX_LEVERAGE, 20); self.assertEqual(odt.EVOLVED_DAILY_HALT, .25)
        self.assertEqual(odt.MAX_SAME_SIDE, 20)
        saved = {k: getattr(odt, k) for k in LiveModeBase.GLOBALS}
        try:
            p = odt.configure_evolved(['BTC-USDT-SWAP'])
            self.assertEqual((p['margin_mode'], p['same_side_cap'], p['daily_halt'], p['max_leverage']), ('cross', None, .25, 20))
            self.assertEqual(p['max_same_side'], 20)
            self.assertEqual((p['risk'], p['risk_max'], p['max_risk'], p['leader_risk']), (.05, .10, .10, .10))
        finally:
            for k, v in saved.items(): setattr(odt, k, v)


class ClientTests(LiveModeBase):
    def request(self, mode, dry, method='GET', path='/api/v5/account/config', body=None):
        with mock.patch.dict(os.environ, self.env(), clear=True):
            return odt.OKX(KEY, SECRET, PASS, mode=mode, dry_run=dry).request(method, path, body)

    def test_live_requests_omit_the_simulated_header_demo_requests_carry_it(self):
        self.request('live', False, 'POST', '/api/v5/trade/order', {'instId': 'BTC-USDT-SWAP'})
        self.request('demo', False, 'POST', '/api/v5/trade/order', {'instId': 'BTC-USDT-SWAP'})
        live, demo = self.requests()
        self.assertNotIn('x-simulated-trading', live['headers']); self.assertEqual(demo['headers']['x-simulated-trading'], '1')
        for r in (live, demo):
            self.assertEqual(r['headers']['OK-ACCESS-KEY'], KEY)
            for s in (KEY, SECRET, PASS):
                self.assertNotIn(s, ' '.join(r['argv']))   # secrets reach curl through stdin only
            self.assertNotIn(SECRET, json.dumps(r['headers']))   # the secret itself is never sent, only the signature

    def test_dry_run_client_refuses_every_write_before_curl_runs_but_can_read(self):
        for method in ('POST', 'PUT', 'DELETE'):
            with self.assertRaises(odt.OKXError):
                self.request('live', True, method, '/api/v5/trade/order', {'instId': 'BTC-USDT-SWAP'})
        self.assertEqual(self.requests(), [])
        self.assertEqual(self.request('live', True)['code'], '0')
        self.assertEqual([r['method'] for r in self.requests()], ['GET'])

    def test_client_reads_the_mode_and_dry_run_from_the_environment_by_default(self):
        with mock.patch.dict(os.environ, {'OKX_TRADING_MODE': 'live'}, clear=True):
            c = odt.OKX(KEY, SECRET, PASS)
        self.assertEqual((c.mode, c.dry_run), ('live', True))
        with mock.patch.dict(os.environ, {}, clear=True):
            c = odt.OKX(KEY, SECRET, PASS)
        self.assertEqual((c.mode, c.dry_run), ('demo', False))


class MainTests(LiveModeBase):
    def test_live_default_is_a_dry_run_that_sends_no_order_and_saves_no_state(self):
        text, code = self.main(OKX_TRADING_MODE='live')
        self.assertIsNone(code, text)
        reqs = self.requests()
        self.assertTrue(reqs)
        self.assertEqual({r['method'] for r in reqs}, {'GET'})
        self.assertFalse(any('x-simulated-trading' in r['headers'] for r in reqs))
        live = self.data / 'okx_live'
        self.assertFalse((live / 'state.json').exists()); self.assertFalse((live / 'summary.json').exists())
        self.assertFalse((self.data / 'okx_demo').exists())
        preview = json.loads((live / 'dry_run_preview.json').read_text())
        self.assertTrue(preview['dry_run']); self.assertEqual(preview['mode'], 'live')
        opened = [a for a in preview['actions'] if a['action'] == 'open']
        self.assertEqual(len(opened), 1); self.assertTrue(opened[0]['dry_run'])
        self.assertIn('dry-run', text)
        blob = text + (live / 'dry_run_preview.json').read_text()
        for s in (KEY, SECRET, PASS):
            self.assertNotIn(s, blob)

    def test_live_resume_and_flatten_are_previews_while_dry_run(self):
        for action in ('resume', 'flatten', 'pause', 'follow-current'):
            text, code = self.main(action, OKX_TRADING_MODE='live')
            self.assertIsNone(code, text)
            self.assertEqual({r['method'] for r in self.requests()}, {'GET'}, action)
            self.assertFalse((self.data / 'okx_live' / 'state.json').exists(), action)

    def test_live_orders_need_both_dry_run_off_and_an_explicit_resume(self):
        # armed but the new live state starts paused: still no order
        text, code = self.main(OKX_TRADING_MODE='live', OKX_DRY_RUN='0')
        self.assertIsNone(code, text)
        self.assertEqual({r['method'] for r in self.requests()}, {'GET'})
        state = json.loads((self.data / 'okx_live' / 'state.json').read_text())
        self.assertTrue(state['paused']); self.assertEqual(state['mode'], 'live')
        summary = json.loads((self.data / 'okx_live' / 'summary.json').read_text())
        self.assertEqual(summary['trading_mode'], 'live'); self.assertFalse(summary['dry_run'])
        # resumed: real (live) orders, without the simulated header
        self.resume('live')
        text, code = self.main(OKX_TRADING_MODE='live', OKX_DRY_RUN='0')
        self.assertIsNone(code, text)
        posts = [r for r in self.requests() if r['method'] == 'POST']
        self.assertEqual([p['path'] for p in posts], ['/api/v5/account/set-leverage', '/api/v5/trade/order'])
        self.assertFalse(any('x-simulated-trading' in p['headers'] for p in posts))

    def test_live_and_demo_place_the_same_order_from_the_same_signal(self):
        """Risk rules are shared: identical account + signal → identical leverage, size, stop and margin mode."""
        bodies = {}
        for mode in ('demo', 'live'):
            env = dict(OKX_TRADING_MODE=mode) if mode == 'live' else {}
            self.main(**env, OKX_DRY_RUN='0'); self.requests()
            self.resume(mode)
            text, code = self.main(**env, OKX_DRY_RUN='0')
            self.assertIsNone(code, text)
            reqs = self.requests()
            posts = [r for r in reqs if r['method'] == 'POST']
            signed = [r for r in reqs if 'OK-ACCESS-KEY' in r['headers']]   # demo also reads the public real-market ticker, unsigned
            self.assertTrue(signed)
            self.assertEqual({r['headers'].get('x-simulated-trading') for r in signed}, {'1'} if mode == 'demo' else {None})
            if mode == 'live':
                self.assertEqual(len(signed), len(reqs))   # live skips the demo-vs-real price comparison
            bodies[mode] = [(p['path'], json.loads(p['body'])) for p in posts]
        self.assertEqual(bodies['demo'], bodies['live'])
        (lev_path, lev), (order_path, order) = bodies['live']
        self.assertEqual((lev['lever'], lev['mgnMode'], order['tdMode']), ('7', 'cross', 'cross'))   # leverage gene, cross
        self.assertEqual(order['attachAlgoOrds'][0]['slTriggerPx'], '68600')                          # stop always attached
        self.assertEqual(order['attachAlgoOrds'][0]['tpTriggerPx'], '74200')                          # TP always attached: no paper tp → entry + 3R
        # risk gene 3 % is raised to the 5 % floor, × major factor 2 = 10 % of 10 000 = 1 000 USDT at a 1 400 stop distance → 0.7142 BTC = 71.42 contracts (lot 0.01)
        self.assertEqual(order['sz'], '71.42')
        self.assertTrue((self.data / 'okx_demo' / 'state.json').exists()); self.assertTrue((self.data / 'okx_live' / 'state.json').exists())

    def test_live_refuses_a_demo_state_and_demo_refuses_a_live_state(self):
        (self.data / 'okx_live').mkdir()
        (self.data / 'okx_live' / 'state.json').write_text(json.dumps(odt.new_state()))   # no mode stamp = demo
        text, code = self.main(OKX_TRADING_MODE='live', OKX_DRY_RUN='0')
        self.assertTrue(code); self.assertIn('demo', text)
        self.assertEqual(self.requests(), [])
        (self.data / 'okx_demo').mkdir()
        (self.data / 'okx_demo' / 'state.json').write_text(json.dumps(odt.new_state('live')))
        text, code = self.main()
        self.assertTrue(code)
        self.assertEqual(self.requests(), [])

    def test_live_refuses_stale_or_missing_signals_and_never_falls_back_to_the_retired_engine(self):
        self.write_signals(signals(age_hours=10))
        text, code = self.main(OKX_TRADING_MODE='live', OKX_DRY_RUN='0')
        self.assertTrue(code); self.assertIn('过期', text)
        self.assertEqual(self.requests(), [])
        (self.data / 'evolve' / 'signals.json').unlink()
        (self.data / 'combined').mkdir(); (self.data / 'combined' / 'state.json').write_text(json.dumps({'positions': {}}))
        text, code = self.main(OKX_TRADING_MODE='live', OKX_DRY_RUN='0')
        self.assertTrue(code); self.assertIn('signals.json', text)
        self.assertEqual(self.requests(), [])

    def test_live_refuses_signals_stamped_in_the_future(self):
        """PR #92 review: a future updated_at gave a negative age and slipped past the max-age check."""
        for hours_ahead in (0.01, 1, 24 * 365):
            self.write_signals(signals(age_hours=-hours_ahead))
            text, code = self.main(OKX_TRADING_MODE='live', OKX_DRY_RUN='0')
            self.assertTrue(code, hours_ahead); self.assertIn('未来', text)
            self.assertEqual(self.requests(), [], hours_ahead)
        # a fresh signal from the past still goes through to the account
        self.write_signals(signals(age_hours=.5))
        text, code = self.main(OKX_TRADING_MODE='live', OKX_DRY_RUN='0')
        self.assertFalse(code, text)
        self.assertTrue(self.requests())

    def test_signal_age_is_none_for_future_missing_or_bad_timestamps(self):
        now = datetime(2026, 11, 15, 12, tzinfo=timezone.utc)
        self.assertEqual(odt.signal_age_ms({'updated_at': '2026-11-15T11:00:00+00:00'}, now), 3600_000)
        self.assertEqual(odt.signal_age_ms({'updated_at': '2026-11-15T12:00:00Z'}, now), 0)
        self.assertIsNone(odt.signal_age_ms({'updated_at': '2026-11-15T12:00:01+00:00'}, now))   # 1 s in the future
        self.assertIsNone(odt.signal_age_ms({'updated_at': '2099-01-01T00:00:00+00:00'}, now))
        self.assertIsNone(odt.signal_age_ms({}, now))
        self.assertIsNone(odt.signal_age_ms({'updated_at': 'yesterday'}, now))

    def test_credentials_come_only_from_the_environment(self):
        for missing in ('OKX_API_KEY', 'OKX_API_SECRET', 'OKX_API_PASSPHRASE'):
            text, code = self.main(OKX_TRADING_MODE='live', **{missing: None})
            self.assertTrue(code, missing); self.assertIn('环境变量', text)
            for s in (KEY, SECRET, PASS):
                self.assertNotIn(s, text)
        self.assertEqual(self.requests(), [])
        # a credential file lying around in the data folder is ignored
        (self.data / 'okx_live').mkdir(exist_ok=True)
        (self.data / 'okx_live' / 'okx-live.env').write_text(f'OKX_API_KEY={KEY}\n')
        text, code = self.main(OKX_TRADING_MODE='live', OKX_API_KEY=None)
        self.assertTrue(code); self.assertEqual(self.requests(), [])

    def test_invalid_mode_refuses_before_any_request(self):
        text, code = self.main(OKX_TRADING_MODE='production')
        self.assertTrue(code); self.assertEqual(self.requests(), [])


class ToolTests(LiveModeBase):
    def test_stop_refuses_in_live_dry_run_and_uses_the_live_state_when_armed(self):
        with mock.patch.dict(os.environ, self.env(OKX_TRADING_MODE='live'), clear=True), \
                mock.patch.object(sys, 'argv', ['stop_demo.py', '--data', str(self.data)]), self.assertRaises(SystemExit) as e:
            stop_demo.main()
        self.assertIn('OKX_DRY_RUN=0', str(e.exception.code))
        self.assertEqual(self.requests(), [])
        with mock.patch.dict(os.environ, self.env(OKX_TRADING_MODE='live', OKX_DRY_RUN='0'), clear=True), \
                mock.patch.object(sys, 'argv', ['stop_demo.py', '--data', str(self.data)]), redirect_stdout(io.StringIO()):
            stop_demo.main()
        self.assertTrue(json.loads((self.data / 'okx_live' / 'state.json').read_text())['paused'])
        self.assertFalse((self.data / 'okx_demo').exists())
        self.assertFalse(any('x-simulated-trading' in r['headers'] for r in self.requests()))

    def test_smoke_test_is_demo_only(self):
        with mock.patch.dict(os.environ, self.env(OKX_TRADING_MODE='live', OKX_DRY_RUN='0'), clear=True), \
                mock.patch.object(sys, 'argv', ['okx_demo_smoke.py', '--output', str(self.tmp / 'smoke.json')]), self.assertRaises(SystemExit):
            smoke.main()
        self.assertEqual(self.requests(), [])

    def test_key_check_signs_for_the_selected_mode(self):
        chk.signed_get('/api/v5/account/config', KEY, SECRET, PASS, curl=str(self.bin / 'curl'), mode='live')
        chk.signed_get('/api/v5/account/config', KEY, SECRET, PASS, curl=str(self.bin / 'curl'))
        live, demo = self.requests()
        self.assertNotIn('x-simulated-trading', live['headers']); self.assertEqual(demo['headers']['x-simulated-trading'], '1')
        ok, lines = chk.check(lambda p: {'code': '0', 'data': [{'perm': 'read_only,trade,withdraw'}]} if 'config' in p else {'code': '1'}, 'live')
        self.assertFalse(ok); self.assertIn('实盘', lines[0]); self.assertIn('提现', '\n'.join(lines))

    def test_fetched_signals_are_validated_before_they_replace_the_local_copy(self):
        self.assertTrue(fetch_signals.valid(signals()))
        bad = signals(); bad['positions']['BTC-USDT-SWAP'].pop('stop')
        self.assertFalse(fetch_signals.valid(bad))
        self.assertFalse(fetch_signals.valid(dict(signals(), instruments=['BTC/USDT'])))
        self.assertFalse(fetch_signals.valid({'positions': {}}))


if __name__ == '__main__':
    unittest.main()

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'tools'))
import stop_demo
from okx_demo_trader import OKXError, new_state
from test_okx_demo_trader import FakeOKX


class ClosingExchange(FakeOKX):
    def request(self, method, path, body=None):
        result = super().request(method, path, body)
        if method == 'POST' and path == '/api/v5/trade/close-position':
            self.positions = [p for p in self.positions if p['instId'] != body['instId']]
        return result


class StopDemoTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.root = Path(self.tmp.name)
        self.out = self.root / 'okx_demo'; self.out.mkdir()
        state = new_state(); state.update(paused=False, mirrored={'btc': dict(status='open', inst='BTC-USDT-SWAP', side=1, posSide='long')})
        (self.out / 'state.json').write_text(json.dumps(state))
        (self.out / 'summary.json').write_text(json.dumps(dict(updated_at='2026-11-03T21:00:00Z',equity=5000,paused=False,positions=[],closed_trades=0)))
        self.positions = [dict(instId='BTC-USDT-SWAP', posSide='long', pos='3.57'), dict(instId='XRP-USDT-SWAP', posSide='short', pos='1')]

    def tearDown(self):
        self.tmp.cleanup()

    def read(self, name):
        return json.loads((self.out / name).read_text())

    def test_stop_flattens_owned_positions_verifies_and_keeps_manual_positions(self):
        c = ClosingExchange(positions=self.positions)
        result = stop_demo.stop(self.root, c, wait=lambda _: None)
        self.assertEqual(result['status'], 'completed')
        self.assertEqual([path for path, _ in c.posts], ['/api/v5/trade/close-position'])
        self.assertEqual(c.positions[0]['instId'], 'XRP-USDT-SWAP')
        self.assertTrue(self.read('state.json')['paused'])
        summary = self.read('summary.json')
        self.assertEqual(summary['stop_control']['status'], 'completed')
        self.assertEqual(summary['positions'][0]['inst'], 'XRP-USDT-SWAP')
        self.assertFalse(summary['positions'][0]['ours'])
        self.assertEqual(summary['closed_trades'], 1)
        c.posts.clear(); stop_demo.stop(self.root, c, wait=lambda _: None)
        self.assertEqual(c.posts, [])

    def test_accepted_close_without_position_disappearing_is_failure_and_keeps_management(self):
        c = FakeOKX(positions=self.positions)
        with self.assertRaises(OKXError): stop_demo.stop(self.root, c, wait=lambda _: None)
        state = self.read('state.json'); summary = self.read('summary.json')
        self.assertTrue(state['paused'])
        self.assertEqual(state['mirrored']['btc']['status'], 'open')
        self.assertEqual(summary['stop_control']['status'], 'failed')
        self.assertEqual(summary['closed_trades'], 0)
        self.assertEqual(summary['updated_at'], '2026-11-03T21:00:00Z')

    def test_api_outage_still_publishes_paused_state_without_claiming_flatten_success(self):
        class Offline:
            def request(self, *args):
                raise OKXError('offline')
        with self.assertRaises(OKXError): stop_demo.stop(self.root, Offline(), wait=lambda _: None)
        self.assertTrue(self.read('state.json')['paused'])
        self.assertEqual(self.read('state.json')['mirrored']['btc']['status'], 'open')
        self.assertEqual(self.read('summary.json')['stop_control']['status'], 'failed')


if __name__ == '__main__':
    unittest.main()

import copy
import json
import sys
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'tools'))
import paper_trader as pt
import okx_demo_trader as odt
from volume_test import TEST_START, TEST_UNTIL, profile
from test_okx_demo_trader import FakeOKX, SPECS, long_btc, paper, run


class VolumeTest(unittest.TestCase):
    def setUp(self):
        self.saved = (pt.INSTRUMENTS, pt.RISK, copy.deepcopy(pt.STRATEGIES), odt.INSTRUMENTS, odt.RISK)

    def tearDown(self):
        pt.INSTRUMENTS, pt.RISK, pt.STRATEGIES, odt.INSTRUMENTS, odt.RISK = self.saved

    def test_window_and_restore(self):
        self.assertFalse(profile(TEST_START - timedelta(seconds=1))['active'])
        self.assertTrue(profile(TEST_START)['active'])
        self.assertTrue(profile(TEST_UNTIL - timedelta(seconds=1))['active'])
        pt.configure_volume_test(TEST_START); odt.configure_volume_test(TEST_START)
        self.assertEqual(len(pt.INSTRUMENTS), 10)
        self.assertEqual(pt.INSTRUMENTS, odt.INSTRUMENTS)
        pt.configure_volume_test(TEST_UNTIL); odt.configure_volume_test(TEST_UNTIL)
        self.assertEqual((pt.RISK, odt.RISK, len(pt.INSTRUMENTS)), (.01, .01, 5))
        self.assertEqual(pt.STRATEGIES['mean_reversion']['rsi'], 25)
        self.assertEqual(pt.STRATEGIES['breakout_long']['lookback'], 48)
        self.assertEqual((pt.SAME_SIDE_CAP, pt.DAILY_HALT, odt.DAILY_HALT, odt.MAX_LEVERAGE), (.05, .08, .08, 10))

    def test_demo_order_size_doubles_and_stop_is_preserved(self):
        baseline = FakeOKX(); run(baseline, paper(**{'BTC-USDT-SWAP': long_btc()}))
        odt.configure_volume_test(TEST_START)
        test = FakeOKX(); run(test, paper(**{'BTC-USDT-SWAP': long_btc()}))
        self.assertEqual(baseline.posts[-1][1]['sz'], '3.57')
        self.assertEqual(test.posts[-1][1]['sz'], '7.14')
        self.assertEqual(test.posts[-1][1]['attachAlgoOrds'], baseline.posts[-1][1]['attachAlgoOrds'])

    def test_shorter_breakout_window_creates_additional_signal(self):
        x = dict(c=[100.] * 201, h=[100.] * 201, e50=[101.] * 201, e200=[100.] * 201)
        x['h'][160] = 110.; x['c'][200] = 105.
        self.assertEqual(pt.signal(x, 200, pt.STRATEGIES['breakout_long']), 0)
        pt.configure_volume_test(TEST_START)
        self.assertEqual(pt.signal(x, 200, pt.STRATEGIES['breakout_long']), 1)

    def test_paper_still_fetches_test_positions_after_expiry(self):
        pt.configure_volume_test(TEST_UNTIL)
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp) / pt.ACCOUNT; d.mkdir()
            state = pt.new_account()
            state['positions']['LINK-USDT-SWAP'] = dict(entry=10., side=1, qty=1., margin=1.)
            (d / 'state.json').write_text(json.dumps(state))
            fetched = []
            def fetch(inst, bar):
                fetched.append((inst, bar)); return []
            with patch.object(pt, 'summarize', return_value={}), patch.object(pt, 'write_readme'), patch.object(pt, 'write_dashboard'):
                pt.run_once(tmp, fetch=fetch)
            self.assertIn(('LINK-USDT-SWAP', '1H'), fetched)
            self.assertIn(('LINK-USDT-SWAP', '1Dutc'), fetched)
            self.assertNotIn('LINK-USDT-SWAP', pt.INSTRUMENTS)

    def test_expired_demo_position_can_close_without_touching_manual_position(self):
        odt.configure_volume_test(TEST_START)
        fake = FakeOKX(prices={'LINK-USDT-SWAP': 70000.})
        with patch.dict(SPECS, {'LINK-USDT-SWAP': SPECS['BTC-USDT-SWAP']}):
            _, state = run(fake, paper(**{'LINK-USDT-SWAP': long_btc()}))
        fake.positions = [dict(instId='LINK-USDT-SWAP', posSide='long', pos='7.14'),
                          dict(instId='AVAX-USDT-SWAP', posSide='long', pos='1')]
        fake.posts.clear(); odt.configure_volume_test(TEST_UNTIL)
        run(fake, paper(), state)
        self.assertEqual(len(fake.posts), 1)
        self.assertEqual(fake.posts[0][0], '/api/v5/trade/close-position')
        self.assertEqual(fake.posts[0][1]['instId'], 'LINK-USDT-SWAP')


if __name__ == '__main__':
    unittest.main()

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
from volume_test import TEST_START, TEST_UNTIL, AGGRESSIVE_START, AGGRESSIVE_UNTIL, profile
from test_okx_demo_trader import FakeOKX, SPECS, long_btc, paper, run


class VolumeTest(unittest.TestCase):
    def setUp(self):
        self.saved = (pt.INSTRUMENTS, pt.RISK, copy.deepcopy(pt.STRATEGIES), odt.INSTRUMENTS, odt.RISK, pt.SAME_SIDE_CAP, odt.SAME_SIDE_CAP)

    def tearDown(self):
        pt.INSTRUMENTS, pt.RISK, pt.STRATEGIES, odt.INSTRUMENTS, odt.RISK, pt.SAME_SIDE_CAP, odt.SAME_SIDE_CAP = self.saved

    def test_window_and_restore(self):
        self.assertFalse(profile(TEST_START - timedelta(seconds=1))['active'])
        self.assertTrue(profile(TEST_START)['active'])
        self.assertTrue(profile(TEST_UNTIL - timedelta(seconds=1))['active'])
        pt.configure_volume_test(TEST_START); odt.configure_volume_test(TEST_START)
        self.assertEqual(len(pt.INSTRUMENTS), 10)
        self.assertEqual(pt.INSTRUMENTS, odt.INSTRUMENTS)
        pt.configure_volume_test(AGGRESSIVE_UNTIL); odt.configure_volume_test(AGGRESSIVE_UNTIL)
        self.assertEqual((pt.RISK, odt.RISK, len(pt.INSTRUMENTS)), (.01, .01, 5))
        self.assertEqual(pt.STRATEGIES['mean_reversion']['rsi'], 25)
        self.assertEqual(pt.STRATEGIES['breakout_long']['lookback'], 48)
        self.assertEqual((pt.SAME_SIDE_CAP, pt.DAILY_HALT, odt.DAILY_HALT, odt.MAX_LEVERAGE), (.05, .08, .10, 10))

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
        pt.configure_volume_test(AGGRESSIVE_UNTIL)
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
        fake.posts.clear(); odt.configure_volume_test(AGGRESSIVE_UNTIL)
        run(fake, paper(), state)
        self.assertEqual(len(fake.posts), 1)
        self.assertEqual(fake.posts[0][0], '/api/v5/trade/close-position')
        self.assertEqual(fake.posts[0][1]['instId'], 'LINK-USDT-SWAP')

    def test_aggressive_window_applies_consistent_limits_and_expires(self):
        self.assertEqual(profile(AGGRESSIVE_START - timedelta(seconds=1))['mode'], 'volume_test')
        universe=[f'C{i:03d}-USDT-SWAP' for i in range(100)]
        p = pt.configure_volume_test(AGGRESSIVE_START,universe); odt.configure_volume_test(AGGRESSIVE_START,universe)
        self.assertEqual(p['mode'], 'aggressive')
        self.assertEqual(len(pt.INSTRUMENTS), 100)
        self.assertEqual(pt.INSTRUMENTS, odt.INSTRUMENTS)
        self.assertEqual((pt.RISK,odt.RISK,pt.SAME_SIDE_CAP,odt.SAME_SIDE_CAP),(.03,.03,.06,.06))
        self.assertEqual((pt.STRATEGIES['mean_reversion']['rsi'],pt.STRATEGIES['mean_reversion']['trend']), (35,.03))
        self.assertEqual(pt.STRATEGIES['breakout_long']['lookback'],12)
        self.assertEqual((pt.DAILY_HALT,odt.DAILY_HALT,odt.MAX_LEVERAGE),(.08,.10,10))
        self.assertEqual(profile(TEST_UNTIL)['mode'], 'aggressive')
        self.assertEqual(profile(AGGRESSIVE_UNTIL)['mode'], 'standard')

    def test_ranked_universe_uses_quote_turnover_and_only_live_linear_usdt_swaps(self):
        tickers=[dict(instId=f'C{i:03d}-USDT-SWAP',last='1',volCcy24h=str(i+1)) for i in range(104)]
        specs=[dict(instId=t['instId'],state='live',settleCcy='USDT',ctType='linear') for t in tickers]
        tickers[0]['last']='1000'  # quote turnover, not raw coin volume
        tickers.append(dict(instId='BTC-USD-SWAP',last='100000',volCcy24h='100000000'))
        specs.append(dict(instId='BTC-USD-SWAP',state='live',settleCcy='BTC',ctType='inverse'))
        selected=pt.ranked_universe(tickers,specs)
        self.assertEqual(len(selected),100)
        self.assertEqual(selected[0],'C000-USDT-SWAP')
        self.assertEqual(selected[1],'C103-USDT-SWAP')
        self.assertNotIn('BTC-USD-SWAP',selected)
        specs[2]['state']='suspend';tickers[3]['volCcy24h']='NaN'
        self.assertNotIn('C002-USDT-SWAP',pt.ranked_universe(tickers,specs))
        self.assertNotIn('C003-USDT-SWAP',pt.ranked_universe(tickers,specs))
        with self.assertRaises(RuntimeError): pt.ranked_universe(tickers[:99],specs)

    def test_demo_mirror_accepts_the_selected_hundred_not_only_old_fixed_coins(self):
        universe=[f'C{i:03d}-USDT-SWAP' for i in range(100)]
        odt.configure_volume_test(AGGRESSIVE_START,universe)
        fake=FakeOKX(prices={'C099-USDT-SWAP':70000.})
        with patch.dict(SPECS,{'C099-USDT-SWAP':SPECS['BTC-USDT-SWAP']}):
            run(fake,paper(**{'C099-USDT-SWAP':long_btc()}))
        self.assertEqual(fake.posts[-1][1]['instId'],'C099-USDT-SWAP')
        self.assertEqual(fake.posts[-1][1]['sz'],'10.71')

    def test_aggressive_rsi_and_trend_allow_additional_mean_reversion_signal(self):
        x = {k:[v]*201 for k,v in dict(c=100.,atr=1.,q=1.,e50=102.5,e200=100.,rsi=32.,lo=98.,hi=102.,contiguous=True).items()}
        x['c'][199]=97.
        pt.configure_volume_test(TEST_START)
        self.assertEqual(pt.signal(x,200,pt.STRATEGIES['mean_reversion']),0)
        pt.configure_volume_test(AGGRESSIVE_START)
        self.assertEqual(pt.signal(x,200,pt.STRATEGIES['mean_reversion']),1)

    def test_demo_aggressive_sizing_and_actual_same_side_cap(self):
        odt.configure_volume_test(AGGRESSIVE_START)
        fake = FakeOKX()
        run(fake,paper(**{'BTC-USDT-SWAP':long_btc()}))
        self.assertEqual(fake.posts[-1][1]['sz'],'10.71')
        # 31 contracts of 100 XRP with a 0.05 USDT stop risk = 155 USDT;
        # adding 150 would exceed 6% of 5000, even with sufficient free margin.
        held=dict(instId='XRP-USDT-SWAP',posSide='long',pos='31',avgPx='1.25')
        fake=FakeOKX(positions=[held])
        state=dict(odt.new_state(),paused=False,mirrored={'xrp':dict(status='open',inst='XRP-USDT-SWAP',side=1,posSide='long',stop=1.2)})
        from datetime import datetime, timezone
        m=odt.Mirror(fake,state,paper(),datetime.now(timezone.utc))
        m.load();m.open('btc','BTC-USDT-SWAP',long_btc())
        self.assertEqual(fake.posts,[])
        self.assertEqual(m.actions[-1]['reason'],'same_side_risk')

    def test_new_orders_in_one_batch_reserve_risk_before_exchange_position_refresh(self):
        odt.configure_volume_test(AGGRESSIVE_START)
        fake=FakeOKX(prices={'BTC-USDT-SWAP':70000.,'ETH-USDT-SWAP':70000.,'SOL-USDT-SWAP':70000.})
        from datetime import datetime,timezone
        m=odt.Mirror(fake,dict(odt.new_state(),paused=False),paper(),datetime.now(timezone.utc))
        with patch.dict(SPECS,{'ETH-USDT-SWAP':SPECS['BTC-USDT-SWAP'],'SOL-USDT-SWAP':SPECS['BTC-USDT-SWAP']}):
            m.load()
            for inst in ['BTC-USDT-SWAP','ETH-USDT-SWAP','SOL-USDT-SWAP']:
                m.open(inst,inst,long_btc())
        self.assertEqual(sum(path=='/api/v5/trade/order' for path,_ in fake.posts),2)
        self.assertEqual(m.actions[-1]['reason'],'same_side_risk')
        self.assertLess(m.available,5000.)


if __name__ == '__main__':
    unittest.main()

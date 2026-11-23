"""OKX_ADOPT: the live trader adopts one configured manual position and manages its exit (trail, reverse signal)."""
import json
import unittest
import unittest.mock
from test_okx_demo_trader import FakeOKX, NOW, NOW_MS, paper, long_btc, run
import okx_demo_trader as odt

BTC = 'BTC-USDT-SWAP'
POS_ID, ALGO_ID = '4121814466832101378', '4125258999062327297'
CFG = [dict(inst=BTC, posId=POS_ID, posSide='long', side=1, stop=62300, algoId=ALGO_ID, r_dist=1500)]
KEY = f'adopt|{BTC}|{POS_ID}'


def manual_btc(pos_id=POS_ID, pos='1.61'):
    return dict(instId=BTC, posSide='long', posId=pos_id, pos=pos, avgPx='60970.4857', mgnMode='isolated', lever='100',
                markPx='64000', upl='48', cTime=str(NOW_MS - 86400_000))


def btc_stop(px='62300', algo_id=ALGO_ID):
    return dict(instId=BTC, posSide='long', algoId=algo_id, slTriggerPx=px, ordType='conditional')


def adopt_run(fake, paper_state, state=None, cfg=CFG, action='run'):
    state = state or dict(odt.new_state(mode='live'), paused=False)
    m = odt.Mirror(fake, state, paper_state, NOW, adopt=cfg)
    m.run(action)
    return m, state


def candle(ts, high, low):
    return [str(ts), '0', str(high), str(low), '0', '0', '0', '0', '1']


def short_btc(opened):
    return dict(strategy='breakout_long', side=-1, entry=64000.0, stop=65000.0, tp=None, leverage=10, opened=opened)


class AdoptConfigTests(unittest.TestCase):
    def test_parse(self):
        env = {'OKX_ADOPT': json.dumps(CFG)}
        self.assertEqual(odt.adopt_config(env), [dict(inst=BTC, posSide='long', side=1, stop=62300.0, posId=POS_ID, algoId=ALGO_ID, r_dist=1500.0)])
        self.assertEqual(odt.adopt_config({}), [])
        self.assertEqual(odt.adopt_config({'OKX_ADOPT': ''}), [])
        self.assertEqual(odt.adopt_config({'OKX_ADOPT': 'not json'}), [])
        self.assertEqual(odt.adopt_config({'OKX_ADOPT': json.dumps([dict(CFG[0], side=-1)])}), [])        # side ≠ posSide
        self.assertEqual(odt.adopt_config({'OKX_ADOPT': json.dumps([dict(CFG[0], stop=0)])}), [])
        self.assertEqual(odt.adopt_config({'OKX_ADOPT': json.dumps([dict(CFG[0], inst='BTC')])}), [])
        no_r = odt.adopt_config({'OKX_ADOPT': json.dumps([{k: v for k, v in CFG[0].items() if k != 'r_dist'}])})
        self.assertIsNone(no_r[0]['r_dist'])
        self.assertIsNone(odt.adopt_config({'OKX_ADOPT': json.dumps([dict(CFG[0], r_dist=-3)])})[0]['r_dist'])


class AdoptTests(unittest.TestCase):
    def fake(self, **kw):
        kw.setdefault('positions', [manual_btc()]); kw.setdefault('algos', [btc_stop()])
        kw.setdefault('prices', {BTC: 64000.0, 'XRP-USDT-SWAP': 1.25})
        return FakeOKX(**kw)

    def test_no_config_is_identical_to_today(self):
        outs = []
        for cfg in (None, []):
            fake = self.fake()
            m, state = adopt_run(fake, paper(**{BTC: short_btc(NOW_MS - 600_000)}), cfg=cfg)
            outs.append((fake.posts, state, m.actions, m.summary()))
        fake = self.fake()
        m, state = run(fake, paper(**{BTC: short_btc(NOW_MS - 600_000)}), dict(odt.new_state(mode='live'), paused=False))
        self.assertEqual(outs[0], (fake.posts, state, m.actions, m.summary()))
        self.assertEqual(outs[0], outs[1])
        self.assertEqual(fake.posts, [])                          # BTC short skipped: exchange_position_exists_not_ours
        self.assertEqual(m.actions[-1]['reason'], 'exchange_position_exists_not_ours')
        self.assertFalse(m.summary()['positions'][0]['ours'])
        self.assertNotIn('adopted', m.summary()['positions'][0])

    def test_adopts_with_config_and_marks_summary(self):
        fake = self.fake()
        m, state = adopt_run(fake, paper())
        rec = state['mirrored'][KEY]
        self.assertTrue(rec['adopted']); self.assertEqual(rec['status'], 'open')
        self.assertEqual((rec['entry'], rec['stop'], rec['floor'], rec['contracts'], rec['mgnMode'], rec['algoId'], rec['opened_at']),
                         (60970.4857, 62300, 62300, '1.61', 'isolated', ALGO_ID, NOW_MS))
        self.assertFalse(rec['leader'])
        self.assertEqual(fake.posts, [])                          # no candles yet → best = entry → nothing to trail
        pos = m.summary()['positions'][0]
        self.assertTrue(pos['ours']); self.assertTrue(pos['adopted'])
        self.assertEqual([a['action'] for a in m.actions], ['adopt'])

    def test_pos_id_mismatch_or_missing_position_never_adopts(self):
        fake = self.fake(positions=[manual_btc(pos_id='999')])
        m, state = adopt_run(fake, paper())
        self.assertNotIn(KEY, state['mirrored']); self.assertEqual(fake.posts, [])
        self.assertEqual(m.actions[0]['reason'], 'adopt_posId_mismatch')
        m, state = adopt_run(fake, paper(), state)                # logged once only
        self.assertEqual(m.actions, [])
        fake = self.fake(positions=[dict(manual_btc(), posSide='short')])
        _, state = adopt_run(fake, paper())
        self.assertNotIn(KEY, state['mirrored'])
        fake = self.fake(positions=[dict(manual_btc(), instId='ETH-USDT-SWAP')], algos=[])
        _, state = adopt_run(fake, paper())
        self.assertEqual(state['mirrored'], {})                   # no other manual position is adopted

    def test_trail_amends_upward_only_on_the_adopted_algo_and_never_below_the_floor(self):
        fake = self.fake()
        _, state = adopt_run(fake, paper())
        # best 64000 = entry + 3029 ≥ 0.5 R (750) → stop = 64000 − 1500 = 62500 > 62300 (by 0.32 % > 0.3 % tolerance)
        fake.candles = {BTC: [candle(NOW_MS + 3600_000, 64000, 63000)]}
        fake.posts.clear(); adopt_run(fake, paper(), state)
        self.assertEqual(fake.posts, [('/api/v5/trade/amend-algos', {'instId': BTC, 'algoId': ALGO_ID, 'newSlTriggerPx': '62500'})])
        self.assertEqual(state['mirrored'][KEY]['stop'], 62500.0); self.assertTrue(state['mirrored'][KEY]['trailing'])
        # price falls back: the stop never moves down
        fake.algos = [btc_stop('62500')]; fake.prices[BTC] = 62800.0
        fake.candles = {BTC: [candle(NOW_MS + 3600_000, 63000, 62700)]}
        fake.posts.clear(); adopt_run(fake, paper(), state)
        self.assertEqual(fake.posts, []); self.assertEqual(state['mirrored'][KEY]['stop'], 62500.0)

    def test_small_trail_level_below_the_floor_is_not_used(self):
        fake = self.fake(prices={BTC: 62500.0})
        _, state = adopt_run(fake, paper())
        fake.candles = {BTC: [candle(NOW_MS + 3600_000, 63000, 62400)]}   # best − 1R = 61500 < floor 62300
        fake.posts.clear(); adopt_run(fake, paper(), state)
        self.assertEqual(fake.posts, []); self.assertEqual(state['mirrored'][KEY]['stop'], 62300)

    def test_amend_failure_keeps_the_stop_and_never_cancels(self):
        fake = self.fake()
        _, state = adopt_run(fake, paper())
        fake.candles = {BTC: [candle(NOW_MS + 3600_000, 66000, 65000)]}
        fake.prices[BTC] = 65500.0
        posts = []
        def fail(method, path, body=None, _orig=fake.request):
            if method == 'POST':
                posts.append(path); return {'code': '51000', 'msg': 'amend failed', 'data': []}
            return _orig(method, path, body)
        fake.request = fail
        m, _ = adopt_run(fake, paper(), state)
        self.assertEqual(posts, ['/api/v5/trade/amend-algos'])
        self.assertEqual(state['mirrored'][KEY]['stop'], 62300); self.assertEqual(state['mirrored'][KEY]['status'], 'open')
        self.assertEqual(m.actions[-1]['step'], 'adopt_amend_stop')
        self.assertFalse(any('cancel' in p for p in posts))

    def test_missing_algo_places_a_new_stop_and_does_not_close(self):
        fake = self.fake()
        _, state = adopt_run(fake, paper())
        fake.algos = []; fake.posts.clear()
        adopt_run(fake, paper(), state)
        self.assertEqual(len(fake.posts), 1)
        path, body = fake.posts[0]
        self.assertEqual(path, '/api/v5/trade/order-algo')
        self.assertEqual((body['ordType'], body['closeFraction'], body['reduceOnly'], body['side'], body['posSide'], body['tdMode'], body['slTriggerPx']),
                         ('conditional', '1', True, 'sell', 'long', 'isolated', '62300'))
        self.assertEqual(state['mirrored'][KEY]['status'], 'open')
        # price already below the stop with no algo: the existing stop_missing semantics close it
        fake.prices[BTC] = 62000.0; fake.posts.clear()
        adopt_run(fake, paper(), state)
        self.assertEqual([p for p, _ in fake.posts], ['/api/v5/trade/close-position'])
        self.assertEqual(state['mirrored'][KEY]['closed_reason'], 'stop_missing_price_beyond_stop')

    def test_reverse_short_signal_closes_the_adopted_long_once_and_opens_nothing_this_run(self):
        fake = self.fake(positions=[manual_btc(), dict(instId='ETH-USDT-SWAP', posSide='long', pos='2', avgPx='3000')])
        _, state = adopt_run(fake, paper())
        fake.posts.clear()
        sig = paper(**{BTC: short_btc(NOW_MS + 1)})
        m, _ = adopt_run(fake, sig, state)
        self.assertEqual(fake.posts, [('/api/v5/trade/close-position', {'instId': BTC, 'mgnMode': 'isolated', 'autoCxl': True, 'posSide': 'long'})])
        self.assertEqual(state['mirrored'][KEY]['closed_reason'], 'reverse_signal')
        self.assertIn('adopted_closed_this_run', [a.get('reason') for a in m.actions])
        self.assertNotIn(f'{BTC}|{NOW_MS + 1}', state['mirrored'])   # the short is left for the next run
        # next run: long gone, the short entry is followed normally; the adopted record stays done
        fake.positions = [p for p in fake.positions if p['instId'] != BTC]; fake.algos = []; fake.posts.clear()
        adopt_run(fake, sig, state)
        self.assertEqual([p for p, _ in fake.posts], ['/api/v5/account/set-leverage', '/api/v5/trade/order'])
        self.assertEqual(state['mirrored'][KEY]['status'], 'done')

    def test_short_signal_older_than_the_adoption_is_not_a_reverse_signal(self):
        fake = self.fake()
        m, state = adopt_run(fake, paper(**{BTC: short_btc(NOW_MS - 600_000)}))
        self.assertFalse(any(p == '/api/v5/trade/close-position' for p, _ in fake.posts))
        self.assertEqual(state['mirrored'][KEY]['status'], 'open')

    def test_long_signal_does_not_close_or_double_open(self):
        fake = self.fake()
        _, state = adopt_run(fake, paper())
        fake.posts.clear()
        m, _ = adopt_run(fake, paper(**{BTC: dict(long_btc(), opened=NOW_MS + 1, entry=64000.0, stop=63000.0)}), state)
        self.assertEqual(fake.posts, []); self.assertEqual(state['mirrored'][KEY]['status'], 'open')
        self.assertEqual(m.actions[-1]['reason'], 'exchange_position_exists_not_ours')

    def test_daily_halt_leaves_it_but_manual_flatten_closes_it(self):
        fake = self.fake()
        _, state = adopt_run(fake, paper())
        state['day_start'] = 10000; state['day'] = odt.local_day(NOW); fake.posts.clear()
        adopt_run(fake, paper(), state)
        self.assertTrue(state['halted'])
        self.assertEqual(fake.posts, []); self.assertEqual(state['mirrored'][KEY]['status'], 'open')
        adopt_run(fake, paper(), state, action='flatten')
        self.assertEqual([p for p, _ in fake.posts], ['/api/v5/trade/close-position'])
        self.assertEqual(state['mirrored'][KEY]['status'], 'done')

    def test_not_counted_as_a_same_side_strategy_position(self):
        fake = self.fake()
        _, state = adopt_run(fake, paper())
        fake.posts.clear()
        with unittest.mock.patch.object(odt, 'MAX_SAME_SIDE', 1), unittest.mock.patch.object(odt, 'SAME_SIDE_CAP', None), \
                unittest.mock.patch.object(odt, 'ACTIVE_PROFILE', {'mode': 'evolved'}):
            eth = dict(strategy='breakout_long', side=1, entry=3000.0, stop=2940.0, tp=None, leverage=10, opened=NOW_MS - 600_000)
            fake.prices['ETH-USDT-SWAP'] = 3000.0
            m, _ = adopt_run(fake, paper(**{'ETH-USDT-SWAP': eth}), state)
        self.assertEqual([p for p, _ in fake.posts], ['/api/v5/account/set-leverage', '/api/v5/trade/order'])

    def test_position_gone_marks_done_and_is_never_adopted_again(self):
        fake = self.fake()
        _, state = adopt_run(fake, paper())
        fake.positions, fake.algos = [], []
        fake.history = [dict(instId=BTC, posSide='long', uTime=str(NOW_MS + 5000), realizedPnl='21.4', pnl='21.4', closeAvgPx='62300', fee='-0.5')]
        m, _ = adopt_run(fake, paper(), state)
        rec = state['mirrored'][KEY]
        self.assertEqual((rec['status'], rec['realized_pnl'], rec['closed_reason']), ('done', 21.4, 'stop_or_manual'))
        self.assertEqual(m.actions[-1]['action'], 'closed_on_exchange')
        fake.positions, fake.algos = [manual_btc()], [btc_stop()]; fake.posts.clear()   # same posId shows up again
        m, _ = adopt_run(fake, paper(), state)
        self.assertEqual(state['mirrored'][KEY]['status'], 'done'); self.assertEqual(fake.posts, []); self.assertEqual(m.actions, [])

    def test_without_r_dist_the_floor_is_protected_but_never_trailed(self):
        cfg = [{k: v for k, v in odt.adopt_config({'OKX_ADOPT': json.dumps(CFG)})[0].items()}]
        cfg[0]['r_dist'] = None
        fake = self.fake()
        fake.candles = {BTC: [candle(NOW_MS + 3600_000, 70000, 65000)]}
        m, state = adopt_run(fake, paper(), cfg=cfg)
        self.assertEqual(fake.posts, []); self.assertIn('adopt_no_r_dist_no_trail', [a.get('reason') for a in m.actions])
        m, _ = adopt_run(fake, paper(), state, cfg=cfg)
        self.assertEqual(m.actions, [])                           # logged once
        fake.algos = [btc_stop('60000')]                          # resting stop looser than the floor → raised to the floor
        adopt_run(fake, paper(), state, cfg=cfg)
        self.assertEqual(fake.posts, [('/api/v5/trade/amend-algos', {'instId': BTC, 'algoId': ALGO_ID, 'newSlTriggerPx': '62300'})])
        fake.algos = [btc_stop('62250')]; fake.posts.clear()     # even within the 0.3 % amend tolerance
        adopt_run(fake, paper(), state, cfg=cfg)
        self.assertEqual(fake.posts, [('/api/v5/trade/amend-algos', {'instId': BTC, 'algoId': ALGO_ID, 'newSlTriggerPx': '62300'})])

    def test_pause_still_trails_like_mirrored_positions(self):
        fake = self.fake()
        _, state = adopt_run(fake, paper())
        fake.candles = {BTC: [candle(NOW_MS + 3600_000, 64000, 63000)]}; fake.posts.clear()
        adopt_run(fake, paper(), state, action='pause')
        self.assertEqual([p for p, _ in fake.posts], ['/api/v5/trade/amend-algos'])

    def test_unsetting_the_config_releases_it_back_to_manual_and_sends_nothing(self):
        fake = self.fake()
        _, state = adopt_run(fake, paper())
        fake.candles = {BTC: [candle(NOW_MS + 3600_000, 66000, 65000)]}; fake.posts.clear()
        m, _ = adopt_run(fake, paper(**{BTC: short_btc(NOW_MS + 1)}), state, cfg=[])
        self.assertEqual(state['mirrored'][KEY]['status'], 'released')
        self.assertEqual(fake.posts, [])                          # no trail, no reverse close: manual again
        self.assertNotIn('adopted', m.summary()['positions'][0])
        fake.candles = {}
        _, state = adopt_run(fake, paper(), state)                # configured again: adopted afresh
        self.assertEqual(state['mirrored'][KEY]['status'], 'open')

    def test_tools_without_the_setting_keep_managing_and_flatten_includes_it(self):
        fake = self.fake()
        _, state = adopt_run(fake, paper())
        fake.posts.clear()
        adopt_run(fake, {'positions': {}}, state, cfg=None, action='flatten')   # as stop_demo does
        self.assertEqual([p for p, _ in fake.posts], ['/api/v5/trade/close-position'])

    def test_other_positions_untouched(self):
        eth = dict(instId='ETH-USDT-SWAP', posSide='long', pos='2', avgPx='3000', posId='1')
        fake = self.fake(positions=[manual_btc(), eth])
        fake.candles = {BTC: [candle(NOW_MS + 3600_000, 64000, 63000)]}
        _, state = adopt_run(fake, paper())
        adopt_run(fake, paper(), state)
        self.assertTrue(all(b.get('instId') == BTC for _, b in fake.posts))
        self.assertEqual(list(state['mirrored']), [KEY])


if __name__ == '__main__':
    unittest.main()

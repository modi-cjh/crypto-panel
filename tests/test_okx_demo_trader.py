import json
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'tools'))
import okx_demo_trader as odt

NOW = datetime(2026, 11, 4, 1, 30, tzinfo=timezone.utc)
NOW_MS = int(NOW.timestamp() * 1000)
SPECS = {'BTC-USDT-SWAP': dict(ctVal='0.01', lotSz='0.01', minSz='0.01', tickSz='0.1'), 'XRP-USDT-SWAP': dict(ctVal='100', lotSz='1', minSz='1', tickSz='0.0001'),
         'ETH-USDT-SWAP': dict(ctVal='0.1', lotSz='0.01', minSz='0.01', tickSz='0.01'), 'SOL-USDT-SWAP': dict(ctVal='1', lotSz='0.01', minSz='0.01', tickSz='0.001'),
         'DOGE-USDT-SWAP': dict(ctVal='1000', lotSz='0.01', minSz='0.01', tickSz='0.00001')}


class FakeOKX:
    def __init__(self, equity=5000, positions=(), prices=None, algos=(), acct='2', perm='read_only,trade', pos_mode='long_short_mode', instruments=None):
        self.equity, self.positions, self.algos = equity, list(positions), list(algos)
        self.prices = prices or {'BTC-USDT-SWAP': 70000.0, 'XRP-USDT-SWAP': 1.25}
        self.acct, self.perm, self.pos_mode, self.posts = acct, perm, pos_mode, []
        self.history, self.history_calls = [], 0
        self.instruments = instruments   # None = default SPECS; [] / False = list endpoint fails

    def request(self, method, path, body=None):
        if method == 'POST':
            self.posts.append((path, body))
            return {'code': '0', 'data': [{'sCode': '0', 'ordId': '42'}]}
        if path.startswith('/api/v5/account/config'):
            return {'code': '0', 'data': [{'acctLv': self.acct, 'perm': self.perm, 'posMode': self.pos_mode}]}
        if path.startswith('/api/v5/account/balance'):
            return {'code': '0', 'data': [{'details': [{'ccy': 'USDT', 'eq': str(self.equity), 'availBal': str(self.equity)}]}]}
        if path.startswith('/api/v5/account/positions-history'):
            self.history_calls += 1
            inst = path.split('instId=')[1].split('&')[0]
            return {'code': '0', 'data': [h for h in self.history if h['instId'] == inst]}
        if path.startswith('/api/v5/account/positions'):
            return {'code': '0', 'data': self.positions}
        if path.startswith('/api/v5/trade/orders-algo-pending'):
            return {'code': '0', 'data': self.algos if 'conditional' in path else []}
        if path.startswith('/api/v5/public/instruments'):
            src = SPECS if self.instruments is None else self.instruments
            if src is False:
                return {'code': '50014', 'msg': 'unavailable', 'data': []}
            def row(inst, spec):
                return dict(spec, instId=inst)
            if 'instId=' in path:
                inst = path.split('instId=')[1].split('&')[0]
                if inst not in src:
                    return {'code': '0', 'data': []}
                return {'code': '0', 'data': [row(inst, src[inst])]}
            return {'code': '0', 'data': [row(i, s) for i, s in src.items()]}
        if path.startswith('/api/v5/market/ticker'):
            return {'code': '0', 'data': [{'last': str(self.prices[path.split('instId=')[1]])}]}
        raise AssertionError(path)


def paper(**positions):
    return {'positions': positions}


def long_btc(opened_ago_min=30, stop=68600.0, **kw):
    return dict(dict(strategy='breakout_long', side=1, entry=70000.0, stop=stop, tp=None, leverage=10, opened=NOW_MS - opened_ago_min * 60_000), **kw)


def run(fake, paper_state, state=None, action='run', dry=False):
    state = state or dict(odt.new_state(), paused=False)
    m = odt.Mirror(fake, state, paper_state, NOW, dry)
    m.run(action)
    return m, state


class MirrorTests(unittest.TestCase):
    def test_fresh_paper_entry_opens_isolated_with_exchange_stop_sized_to_one_percent(self):
        fake = FakeOKX()
        m, state = run(fake, paper(**{'BTC-USDT-SWAP': long_btc()}))
        paths = [p for p, _ in fake.posts]
        self.assertEqual(paths, ['/api/v5/account/set-leverage', '/api/v5/trade/order'])
        order = fake.posts[1][1]
        self.assertEqual((order['tdMode'], order['side'], order['posSide'], order['ordType']), ('isolated', 'buy', 'long', 'market'))
        self.assertEqual(order['attachAlgoOrds'][0]['slTriggerPx'], '68600')
        # 1% of 5000 = 50 USDT risk at a 2% stop -> 2500 USDT notional = 0.0357 BTC = 3.57 contracts of 0.01 BTC
        self.assertEqual(order['sz'], '3.57')
        self.assertLessEqual(int(fake.posts[0][1]['lever']), 10)
        key = f"BTC-USDT-SWAP|{long_btc()['opened']}"
        self.assertEqual(state['mirrored'][key]['status'], 'open')
        # A second pass does not open again.
        fake.positions = [dict(instId='BTC-USDT-SWAP', posSide='long', pos='3.57')]; fake.posts.clear()
        run(fake, paper(**{'BTC-USDT-SWAP': long_btc()}), state)
        self.assertEqual(fake.posts, [])

    def test_mean_reversion_short_attaches_target_and_net_mode_omits_pos_side(self):
        fake = FakeOKX(pos_mode='net_mode')
        p = dict(strategy='mean_reversion', side=-1, entry=1.25, stop=1.27, tp=1.23, leverage=10, opened=NOW_MS - 600_000)
        run(fake, paper(**{'XRP-USDT-SWAP': p}))
        order = fake.posts[-1][1]
        self.assertEqual(order['side'], 'sell'); self.assertNotIn('posSide', order)
        self.assertEqual(order['attachAlgoOrds'][0]['tpTriggerPx'], '1.23')

    def test_far_take_profit_is_capped_at_tp_max_dist_from_the_fill_price(self):
        fake = FakeOKX()                                            # BTC 70000: long target +40 % → capped at +15 % = 80500
        run(fake, paper(**{'BTC-USDT-SWAP': long_btc(tp=98000.0)}))
        self.assertEqual(fake.posts[-1][1]['attachAlgoOrds'][0]['tpTriggerPx'], '80500')
        fake = FakeOKX(pos_mode='net_mode')                         # XRP 1.25: short target −48 % → capped at −15 % = 1.0625
        p = dict(strategy='mean_reversion', side=-1, entry=1.25, stop=1.27, tp=0.65, leverage=10, opened=NOW_MS - 600_000)
        _, state = run(fake, paper(**{'XRP-USDT-SWAP': p}))
        self.assertEqual(fake.posts[-1][1]['attachAlgoOrds'][0]['tpTriggerPx'], '1.0625')
        self.assertAlmostEqual(list(state['mirrored'].values())[0]['target'], 1.0625)
        fake = FakeOKX()                                            # no target in the signal → still no exchange target
        run(fake, paper(**{'BTC-USDT-SWAP': long_btc()}))
        self.assertNotIn('tpTriggerPx', fake.posts[-1][1]['attachAlgoOrds'][0])

    def test_old_or_crossed_entries_are_skipped_without_orders(self):
        fake = FakeOKX(prices={'BTC-USDT-SWAP': 71500.0})          # old entry and the price has run away (2× the stop distance)
        _, state = run(fake, paper(**{'BTC-USDT-SWAP': long_btc(opened_ago_min=360)}))
        self.assertEqual(fake.posts, []); self.assertEqual(list(state['mirrored'].values())[0]['status'], 'skipped')
        fake = FakeOKX(prices={'BTC-USDT-SWAP': 69200.0})          # old entry, more than half way to the stop
        m, _ = run(fake, paper(**{'BTC-USDT-SWAP': long_btc(opened_ago_min=360)}))
        self.assertEqual(fake.posts, []); self.assertEqual(m.actions[-1]['reason'], 'paper_entry_too_old')
        fake = FakeOKX(prices={'BTC-USDT-SWAP': 68500.0})          # already below the stop
        m, _ = run(fake, paper(**{'BTC-USDT-SWAP': long_btc()}))
        self.assertEqual(fake.posts, []); self.assertEqual(m.actions[-1]['reason'], 'stop_too_close_or_crossed')

    def test_paper_close_closes_mirror_and_exchange_stop_is_never_reopened(self):
        fake = FakeOKX(); _, state = run(fake, paper(**{'BTC-USDT-SWAP': long_btc()}))
        fake.positions = [dict(instId='BTC-USDT-SWAP', posSide='long', pos='3.57')]; fake.posts.clear()
        run(fake, paper(), state)                                     # paper position gone
        self.assertEqual(fake.posts[0][0], '/api/v5/trade/close-position')
        self.assertTrue(fake.posts[0][1]['autoCxl'])
        fake2 = FakeOKX(); _, s2 = run(fake2, paper(**{'BTC-USDT-SWAP': long_btc()}))
        fake2.posts.clear()                                           # exchange stop hit: position vanished
        run(fake2, paper(**{'BTC-USDT-SWAP': long_btc()}), s2)
        self.assertEqual(fake2.posts, []); self.assertEqual(list(s2['mirrored'].values())[0]['status'], 'done')

    def test_trailing_paper_stop_amends_the_exchange_stop_only_upwards_for_longs(self):
        fake = FakeOKX(); _, state = run(fake, paper(**{'BTC-USDT-SWAP': long_btc()}))
        fake.positions = [dict(instId='BTC-USDT-SWAP', posSide='long', pos='3.57')]
        fake.algos = [dict(instId='BTC-USDT-SWAP', posSide='long', algoId='a1')]; fake.posts.clear()
        run(fake, paper(**{'BTC-USDT-SWAP': long_btc(stop=69300.0)}), state)
        self.assertEqual(fake.posts, [('/api/v5/trade/amend-algos', {'instId': 'BTC-USDT-SWAP', 'algoId': 'a1', 'newSlTriggerPx': '69300'})])
        fake.posts.clear(); run(fake, paper(**{'BTC-USDT-SWAP': long_btc(stop=69000.0)}), state)   # never loosened
        self.assertEqual(fake.posts, [])

    def test_daily_loss_halt_is_ten_percent(self):
        fake = FakeOKX(); _, state = run(fake, paper(**{'BTC-USDT-SWAP': long_btc()}))
        fake.positions = [dict(instId='BTC-USDT-SWAP', posSide='long', pos='3.57')]; fake.equity = 4550; fake.posts.clear()   # -9 %
        run(fake, paper(**{'BTC-USDT-SWAP': long_btc()}), state)
        self.assertFalse(state['halted']); self.assertEqual(fake.posts, [])
        fake.equity = 4499                                                                                                   # just over -10 %
        m, _ = run(fake, paper(**{'BTC-USDT-SWAP': long_btc()}), state)
        self.assertTrue(state['halted']); self.assertIn('daily_halt', [a['action'] for a in m.actions])

    def test_daily_loss_halt_flattens_and_blocks_entries_until_next_day(self):
        fake = FakeOKX(); _, state = run(fake, paper(**{'BTC-USDT-SWAP': long_btc()}))
        fake.positions = [dict(instId='BTC-USDT-SWAP', posSide='long', pos='3.57')]; fake.equity = 4500; fake.posts.clear()
        m, _ = run(fake, paper(**{'BTC-USDT-SWAP': long_btc()}), state)
        self.assertTrue(state['halted']); self.assertEqual(fake.posts[0][0], '/api/v5/trade/close-position')
        fake.positions = []; fake.posts.clear()
        run(fake, paper(**{'XRP-USDT-SWAP': dict(strategy='mean_reversion', side=-1, entry=1.25, stop=1.27, tp=1.2, leverage=10, opened=NOW_MS - 60_000)}), state)
        self.assertEqual(fake.posts, [])

    def test_a_new_mirror_starts_paused_so_nothing_trades_before_resume(self):
        fake = FakeOKX()
        m, state = run(fake, paper(**{'BTC-USDT-SWAP': long_btc()}), odt.new_state())
        self.assertTrue(state['paused']); self.assertEqual(fake.posts, [])
        m, _ = run(fake, paper(**{'BTC-USDT-SWAP': long_btc()}), odt.new_state(), dry=True)   # dry-run previews what resume would do
        self.assertEqual([a['action'] for a in m.actions], ['open']); self.assertEqual(fake.posts, [])

    def test_dry_run_sends_nothing_and_unsafe_accounts_are_refused(self):
        fake = FakeOKX(); m, _ = run(fake, paper(**{'BTC-USDT-SWAP': long_btc()}), dry=True)
        self.assertEqual(fake.posts, []); self.assertEqual(m.actions[-1]['action'], 'open'); self.assertTrue(m.actions[-1]['dry_run'])
        with self.assertRaises(odt.OKXError): run(FakeOKX(acct='1'), paper())
        with self.assertRaises(odt.OKXError): run(FakeOKX(perm='read_only,trade,withdraw'), paper())

    def test_manual_positions_are_left_alone_and_pause_flatten_work(self):
        fake = FakeOKX(positions=[dict(instId='BTC-USDT-SWAP', posSide='long', pos='1')])
        m, state = run(fake, paper(**{'BTC-USDT-SWAP': long_btc()}))
        self.assertEqual(fake.posts, []); self.assertEqual(m.actions[-1]['reason'], 'exchange_position_exists_not_ours')
        run(fake, paper(), state, action='flatten')                   # not ours: still untouched
        self.assertEqual(fake.posts, []); self.assertTrue(state['paused'])
        fake = FakeOKX(); _, state = run(fake, paper(), action='pause')
        run(fake, paper(**{'BTC-USDT-SWAP': long_btc()}), state)
        self.assertEqual(fake.posts, [])
        run(fake, paper(**{'BTC-USDT-SWAP': long_btc()}), state, action='resume')
        self.assertEqual(fake.posts[-1][0], '/api/v5/trade/order')

    def test_exchange_stop_or_target_fill_records_realised_pnl_and_which_order_hit(self):
        fake = FakeOKX(); _, state = run(fake, paper(**{'BTC-USDT-SWAP': {**long_btc(), 'tp': 72000.0}}))
        fake.history = [dict(instId='BTC-USDT-SWAP', posSide='long', realizedPnl='-50.8', pnl='-49.0', fundingFee='-0.6', closeAvgPx='68610', openAvgPx='70000', fee='-1.2', type='2', uTime=str(NOW_MS + 60_000))]
        m, _ = run(fake, paper(**{'BTC-USDT-SWAP': {**long_btc(), 'tp': 72000.0}}), state)   # position vanished: the stop filled
        a = next(a for a in m.actions if a['action'] == 'closed_on_exchange')
        self.assertEqual((a['pnl'], a['close_price'], a['outcome']), (-50.8, 68610.0, 'stop'))
        self.assertEqual((a['price_pnl'], a['fee'], a['funding']), (-49.0, -1.2, -0.6))
        fake2 = FakeOKX(); _, s2 = run(fake2, paper(**{'BTC-USDT-SWAP': {**long_btc(), 'tp': 72000.0}}))
        fake2.history = [dict(instId='BTC-USDT-SWAP', posSide='long', realizedPnl='88.1', closeAvgPx='71990', type='2', uTime=str(NOW_MS + 60_000))]
        m2, _ = run(fake2, paper(**{'BTC-USDT-SWAP': {**long_btc(), 'tp': 72000.0}}), s2)
        self.assertEqual(next(a for a in m2.actions if a['action'] == 'closed_on_exchange')['outcome'], 'target')
        fake3 = FakeOKX(); _, s3 = run(fake3, paper(**{'BTC-USDT-SWAP': long_btc()}))   # no history yet: still logged, no amount
        m3, _ = run(fake3, paper(**{'BTC-USDT-SWAP': long_btc()}), s3)
        self.assertNotIn('pnl', next(a for a in m3.actions if a['action'] == 'closed_on_exchange'))

    def test_summary_fills_in_pnl_for_older_closes_once(self):
        fake = FakeOKX(); m, state = run(fake, paper())
        ts = '2026-11-04T01:33:13Z'; t = int(odt.datetime.fromisoformat(ts.replace('Z', '+00:00')).timestamp() * 1000)
        state['mirrored']['XRP-USDT-SWAP|1'] = dict(inst='XRP-USDT-SWAP', side=-1, posSide='short', status='done', opened_at=t - 3600_000, stop=1.2918, target=1.2557)
        fake.history = [dict(instId='XRP-USDT-SWAP', posSide='short', realizedPnl='5.9', closeAvgPx='1.2556', type='2', uTime=str(t - 60_000))]
        actions = [dict(ts=ts, action='closed_on_exchange', inst='XRP-USDT-SWAP')]
        odt.enrich_closes(m, state, actions)
        self.assertEqual((actions[0]['pnl'], actions[0]['outcome']), (5.9, 'target'))
        calls = fake.history_calls; again = [dict(ts=ts, action='closed_on_exchange', inst='XRP-USDT-SWAP')]
        odt.enrich_closes(m, state, again)
        self.assertEqual(again[0]['pnl'], 5.9); self.assertEqual(fake.history_calls, calls)   # cached

    def test_a_close_never_takes_the_result_of_an_older_close_of_the_same_coin(self):
        fake = FakeOKX(); m, state = run(fake, paper())
        ts = '2026-11-11T02:49:06Z'; t = int(odt.datetime.fromisoformat(ts.replace('Z', '+00:00')).timestamp() * 1000)
        day = 86400_000
        state['mirrored']['SOL-USDT-SWAP|1'] = dict(inst='SOL-USDT-SWAP', side=-1, posSide='short', status='done', opened_at=t - 6 * day)
        state['mirrored']['SOL-USDT-SWAP|2'] = dict(inst='SOL-USDT-SWAP', side=-1, posSide='short', status='done', opened_at=t - 6 * 3600_000)
        old = dict(instId='SOL-USDT-SWAP', posSide='short', realizedPnl='3.13', closeAvgPx='89.12', type='2', uTime=str(t - 5 * day))
        fake.history = [old]                                               # today's close is not in the exchange history yet
        actions = [dict(ts=ts, action='close', inst='SOL-USDT-SWAP')]
        odt.enrich_closes(m, state, actions)
        self.assertNotIn('pnl', actions[0])                                # waits instead of showing the 5-day-old result
        fake.history = [old, dict(instId='SOL-USDT-SWAP', posSide='short', realizedPnl='-1.4', closeAvgPx='83.5', type='2', uTime=str(t + 1000))]
        odt.enrich_closes(m, state, actions)
        self.assertEqual((actions[0]['pnl'], actions[0]['close_price']), (-1.4, 83.5))
        self.assertNotIn('close_results_v2', state)

    def test_old_entry_is_followed_late_while_the_price_is_still_near_the_paper_entry(self):
        fake = FakeOKX(prices={'BTC-USDT-SWAP': 70300.0})          # 6 h old, price 0.4 % above the paper entry
        m, state = run(fake, paper(**{'BTC-USDT-SWAP': long_btc(opened_ago_min=360)}))
        order = [b for q, b in fake.posts if q == '/api/v5/trade/order'][-1]
        self.assertEqual(order['attachAlgoOrds'][0]['slTriggerPx'], '68600')        # the paper stop, size from the distance left
        opened = [a for a in m.actions if a['action'] == 'open'][-1]
        self.assertAlmostEqual(opened['risk_usdt'], odt.RISK * 5000, delta=1)
        self.assertEqual(list(state['mirrored'].values())[0]['status'], 'open')

    def test_follow_current_opens_entries_skipped_for_age_but_keeps_price_checks(self):
        fake = FakeOKX(prices={'BTC-USDT-SWAP': 71500.0})          # too far from the paper entry for an automatic late follow
        _, state = run(fake, paper(**{'BTC-USDT-SWAP': long_btc(opened_ago_min=360)}))
        self.assertEqual(fake.posts, [])                                       # too old for a normal run
        run(fake, paper(**{'BTC-USDT-SWAP': long_btc(opened_ago_min=360)}), state)
        self.assertEqual(fake.posts, [])                                       # and stays skipped
        m, _ = run(fake, paper(**{'BTC-USDT-SWAP': long_btc(opened_ago_min=360)}), state, action='follow-current')
        self.assertEqual([p[0] for p in fake.posts][-1], '/api/v5/trade/order')
        self.assertEqual([a['action'] for a in m.actions], ['follow_current', 'open'])
        fake2 = FakeOKX(prices={'BTC-USDT-SWAP': 68500.0})                     # stop already crossed: still skipped
        m2, _ = run(fake2, paper(**{'BTC-USDT-SWAP': long_btc(opened_ago_min=360)}), action='follow-current')
        self.assertEqual(fake2.posts, []); self.assertIn('stop_too_close_or_crossed', [a.get('reason') for a in m2.actions])

    def test_manual_positions_on_any_usdt_swap_are_shown_but_never_managed(self):
        SPECS.setdefault('PEPE-USDT-SWAP', dict(ctVal='10000000', lotSz='1', minSz='1', tickSz='0.00000001'))
        fake = FakeOKX(positions=[dict(instId='PEPE-USDT-SWAP', posSide='long', pos='2', avgPx='0.00001', markPx='0.000011', upl='20', lever='5', margin='40'),
                                  dict(instId='BTC-USD-SWAP', posSide='long', pos='1', avgPx='70000', upl='1', lever='5')])
        m, _ = run(fake, paper())
        s = m.summary()
        self.assertEqual([(p['inst'], p['ours']) for p in s['positions']], [('PEPE-USDT-SWAP', False)])   # coin-margined swaps are left out
        self.assertNotIn(('PEPE-USDT-SWAP', 'long'), m.positions)                                       # and nothing manages it
        m2, _ = run(fake, paper(), action='flatten')
        self.assertFalse(any(p[0] == '/api/v5/trade/close-position' for p in fake.posts))

    def test_following_the_evolution_champion_uses_each_strategys_risk_on_its_coins(self):
        self.addCleanup(setattr, odt, 'MAJOR_FACTOR', odt.MAJOR_FACTOR); odt.MAJOR_FACTOR = 1.0     # majors' extra size is tested on its own
        try:
            p = odt.configure_evolved()
            self.assertEqual((p['mode'], odt.RISK, p['risk_max'], odt.SAME_SIDE_CAP, odt.DAILY_HALT, p['margin_mode']), ('evolved', .02, .10, .20, .10, 'isolated'))
            self.assertEqual(odt.INSTRUMENTS, odt.EVOLVED_INSTRUMENTS)
            odt.configure_evolved(['BTC-USDT-SWAP', 'PEPE-USDT-SWAP', 'WIF-USDT-SWAP'])     # the evolution's top-100 list
            self.assertEqual(odt.INSTRUMENTS, ['BTC-USDT-SWAP', 'PEPE-USDT-SWAP', 'WIF-USDT-SWAP'])
            odt.configure_evolved(['BTC-USD-SWAP'])                                          # not a USDT perpetual: majors only
            self.assertEqual(odt.INSTRUMENTS, odt.EVOLVED_INSTRUMENTS)
            odt.configure_evolved()
            fake = FakeOKX(); m, _ = run(fake, paper(**{'BTC-USDT-SWAP': dict(long_btc(), strategy='evolved')}))
            self.assertTrue(fake.posts[-1][1]['clOrdId'].startswith('evbtc'))
            self.assertEqual(m.summary()['execution_profile']['mode'], 'evolved')
            # the order risks the strategy's own risk gene, clamped to 2–10 %
            px = 78000.0
            for gene, want in ((.03, .03), (.004, .02), (.15, .10)):
                fake = FakeOKX(prices={'BTC-USDT-SWAP': px}); m, _ = run(fake, paper(**{'BTC-USDT-SWAP': dict(long_btc(), strategy='evolved', risk=gene)}))
                o = [b for q, b in fake.posts if q == '/api/v5/trade/order'][-1]
                opened = [a for a in m.actions if a['action'] == 'open'][-1]
                self.assertAlmostEqual(opened['risk_usdt'] / m.equity, want, delta=.002)
        finally:
            odt.ACTIVE_PROFILE = None; odt.configure_volume_test(odt.datetime(2026, 1, 1, tzinfo=odt.timezone.utc))

    def test_same_side_signals_stop_at_twenty_percent_of_risk(self):
        self.addCleanup(setattr, odt, 'MAJOR_FACTOR', odt.MAJOR_FACTOR); odt.MAJOR_FACTOR = 1.0     # majors' extra size is tested on its own
        try:
            odt.configure_evolved()
            px = {'BTC-USDT-SWAP': 78000.0, 'ETH-USDT-SWAP': 2340.0, 'SOL-USDT-SWAP': 93.0, 'XRP-USDT-SWAP': 1.28, 'DOGE-USDT-SWAP': .0742}
            sig = {k: dict(strategy='evolved', side=-1, entry=v, stop=v * 1.04, tp=v * .9, leverage=10, opened=NOW_MS - 10 * 60_000, rank=2, risk=.03) for k, v in px.items()}
            sig['DOGE-USDT-SWAP']['rank'] = 1
            fake = FakeOKX(prices=px); m, state = run(fake, paper(**sig))
            orders = [b['instId'] for p, b in fake.posts if p == '/api/v5/trade/order']
            self.assertEqual(orders, ['DOGE-USDT-SWAP', 'BTC-USDT-SWAP', 'ETH-USDT-SWAP', 'SOL-USDT-SWAP', 'XRP-USDT-SWAP'])
            # 3 % + 3 % + 3 % + 1.5 % + 1.5 % (crowding from the 4th) = 12 %, under the 20 % cap
            self.assertEqual([round(a['risk_usdt'] / m.equity, 3) for a in m.actions if a['action'] == 'open'], [.03, .03, .03, .015, .015])
            odt.SAME_SIDE_CAP = .07                                                               # a tighter cap: the third 3 % does not fit
            fake = FakeOKX(prices=px); m, state = run(fake, paper(**sig))
            self.assertEqual([b['instId'] for p, b in fake.posts if p == '/api/v5/trade/order'], ['DOGE-USDT-SWAP', 'BTC-USDT-SWAP'])
            self.assertEqual(len([a for a in m.actions if a.get('reason') == 'same_side_risk']), 3)
            odt.SAME_SIDE_CAP = .20
            for k in sig: sig[k]['risk'] = .02
            fake = FakeOKX(prices=px); m, state = run(fake, paper(**sig))
            self.assertEqual(len([1 for p, b in fake.posts if p == '/api/v5/trade/order']), 5)
        finally:
            odt.ACTIVE_PROFILE = None; odt.configure_volume_test(odt.datetime(2026, 1, 1, tzinfo=odt.timezone.utc))

    def test_the_leader_uses_its_own_risk_and_leverage_genes(self):
        self.addCleanup(setattr, odt, 'MAJOR_FACTOR', odt.MAJOR_FACTOR); odt.MAJOR_FACTOR = 1.0
        try:
            odt.configure_evolved()
            px = {'BTC-USDT-SWAP': 78000.0, 'ETH-USDT-SWAP': 2340.0, 'SOL-USDT-SWAP': 93.0, 'XRP-USDT-SWAP': 1.28, 'DOGE-USDT-SWAP': .0742}
            caps = {k: dict(SPECS[k], lever='100') for k in px}
            sig = {k: dict(strategy='evolved', side=-1, entry=v, stop=v * 1.04, tp=v * .9, leverage=10, opened=NOW_MS - 10 * 60_000, rank=2, risk=.03) for k, v in px.items()}
            # XRP is a 0.5 % MMR major: a 1 % stop can keep 50×.
            sig['XRP-USDT-SWAP'].update(rank=1, leader=True, risk=.08, leverage=50, stop=px['XRP-USDT-SWAP'] * 1.01)
            fake = FakeOKX(equity=100000, prices=px, instruments=caps); m, state = run(fake, paper(**sig))
            opened = {a['inst']: a for a in m.actions if a['action'] == 'open'}
            lead = opened['XRP-USDT-SWAP']
            self.assertAlmostEqual(lead['risk_usdt'] / m.equity, .08, delta=.002)
            self.assertEqual(odt.LEADER_RISK, .10); self.assertEqual(lead['leverage'], 50); self.assertTrue(lead['leader'])
            self.assertLessEqual(max(a['leverage'] for a in opened.values()), 100)
            # the leader counts toward the same-side cap (8 % + four more 3 % = 20 %, all fit)
            self.assertEqual(len(opened), 5)
            self.assertTrue(next(v for v in state['mirrored'].values() if v['inst'] == 'XRP-USDT-SWAP')['leader'])
        finally:
            odt.ACTIVE_PROFILE = None; odt.configure_volume_test(odt.datetime(2026, 1, 1, tzinfo=odt.timezone.utc))

    def test_daily_halt_includes_the_leader_and_stops_everyone(self):
        self.addCleanup(setattr, odt, 'MAJOR_FACTOR', odt.MAJOR_FACTOR); odt.MAJOR_FACTOR = 1.0
        try:
            odt.configure_evolved()
            px = {'BTC-USDT-SWAP': 78000.0, 'XRP-USDT-SWAP': 1.28, 'DOGE-USDT-SWAP': .0742}
            mk = lambda k, **kw: dict(dict(strategy='evolved', side=-1, entry=px[k], stop=px[k] * 1.04, tp=px[k] * .9, leverage=10, opened=NOW_MS - 10 * 60_000, rank=2, risk=.03), **kw)
            sig = {'BTC-USDT-SWAP': mk('BTC-USDT-SWAP', rank=1, leader=True), 'XRP-USDT-SWAP': mk('XRP-USDT-SWAP')}
            fake = FakeOKX(equity=10000, prices=px); m, state = run(fake, paper(**sig))
            held = lambda inst, upl: dict(instId=inst, posSide='short', pos='1', upl=str(upl))
            # the account is down 8 % (leader 5 % + other 3 %): no halt yet
            fake.positions = [held('BTC-USDT-SWAP', -500), held('XRP-USDT-SWAP', -300)]; fake.equity = 9200; fake.posts.clear()
            m, _ = run(fake, paper(**sig), state)
            self.assertFalse(state['halted']); self.assertEqual(fake.posts, [])
            # the whole account including the leader is down 10 %: halt closes everyone
            fake.positions = [held('BTC-USDT-SWAP', -700), held('XRP-USDT-SWAP', -300)]; fake.equity = 9000
            m, _ = run(fake, paper(**sig), state)
            self.assertTrue(state['halted'])
            closed = [b['instId'] for q, b in fake.posts if q == '/api/v5/trade/close-position']
            self.assertEqual(sorted(closed), ['BTC-USDT-SWAP', 'XRP-USDT-SWAP'])
            # while halted, even a new leader signal is ignored
            fake.positions = []; fake.posts.clear()
            sig2 = {'DOGE-USDT-SWAP': mk('DOGE-USDT-SWAP', leader=True, rank=1, opened=NOW_MS - 5 * 60_000)}
            m, _ = run(fake, paper(**sig2), state)
            self.assertEqual([b['instId'] for q, b in fake.posts if q == '/api/v5/trade/order'], [])
        finally:
            odt.ACTIVE_PROFILE = None; odt.configure_volume_test(odt.datetime(2026, 1, 1, tzinfo=odt.timezone.utc))

    def test_evolved_mode_trades_isolated_margin_and_every_position_keeps_a_stop_on_the_exchange(self):
        try:
            odt.configure_evolved()
            self.assertEqual(odt.MARGIN_MODE, 'isolated')
            sig = {'BTC-USDT-SWAP': dict(long_btc(), strategy='evolved', risk=.02)}
            fake = FakeOKX(); m, state = run(fake, paper(**sig))
            lev = next(b for q, b in fake.posts if q == '/api/v5/account/set-leverage'); order = next(b for q, b in fake.posts if q == '/api/v5/trade/order')
            self.assertEqual((lev['mgnMode'], lev.get('posSide'), order['tdMode']), ('isolated', 'long', 'isolated'))
            self.assertEqual(order['attachAlgoOrds'][0]['slTriggerPx'], '68600')                    # the entry carries its stop
            mir = next(iter(state['mirrored'].values())); self.assertEqual(mir['mgnMode'], 'isolated')
            # next run: the position is there but no stop is resting on the exchange → it is placed again
            later = odt.Mirror(fake, state, paper(**sig), NOW + odt.timedelta(hours=4), False)
            fake.positions = [dict(instId='BTC-USDT-SWAP', posSide='long', pos='3')]; fake.posts.clear()
            later.run('run')
            algo = next(b for q, b in fake.posts if q == '/api/v5/trade/order-algo')
            self.assertEqual((algo['slTriggerPx'], algo['tdMode'], algo['side'], algo['posSide'], algo['closeFraction']), ('68600', 'isolated', 'sell', 'long', '1'))
            self.assertIn('restore_stop', [a['action'] for a in later.actions])
            # with a stop resting, nothing is sent
            fake.algos = [dict(instId='BTC-USDT-SWAP', posSide='long', algoId='9', slTriggerPx='68600')]; fake.posts.clear()
            again = odt.Mirror(fake, state, paper(**sig), NOW + odt.timedelta(hours=8), False); again.run('run')
            self.assertEqual(fake.posts, [])
            # no stop and the price is already beyond it: close instead of placing a stop that would fire at once
            fake.algos = []; fake.prices = {'BTC-USDT-SWAP': 68000.0}; fake.posts.clear()
            gone = odt.Mirror(fake, state, paper(**sig), NOW + odt.timedelta(hours=12), False); gone.run('run')
            close = next(b for q, b in fake.posts if q == '/api/v5/trade/close-position')
            self.assertEqual(close['mgnMode'], 'isolated'); self.assertIn('stop_missing_price_beyond_stop', [a.get('reason') for a in gone.actions])
        finally:
            odt.ACTIVE_PROFILE = None; odt.configure_volume_test(odt.datetime(2026, 1, 1, tzinfo=odt.timezone.utc))
        self.assertEqual(odt.MARGIN_MODE, 'isolated')                                           # the retired strategy engine stays isolated

    def test_the_five_majors_risk_twice_as_much_as_other_coins(self):
        SPECS.setdefault('PEPE-USDT-SWAP', dict(ctVal='10000000', lotSz='1', minSz='1', tickSz='0.00000001'))
        try:
            odt.configure_evolved(['BTC-USDT-SWAP', 'PEPE-USDT-SWAP'])
            self.assertEqual((odt.MAJORS, odt.MAJOR_FACTOR), (('BTC-USDT-SWAP', 'ETH-USDT-SWAP', 'SOL-USDT-SWAP', 'BNB-USDT-SWAP', 'XRP-USDT-SWAP', 'DOGE-USDT-SWAP'), 2.0))
            px = {'BTC-USDT-SWAP': 78000.0, 'PEPE-USDT-SWAP': .00000537}
            sig = {k: dict(strategy='evolved', side=1, entry=v, stop=v * .96, tp=None, leverage=10, opened=NOW_MS - 10 * 60_000, rank=2, risk=.02) for k, v in px.items()}
            fake = FakeOKX(equity=100000, prices=px); m, _ = run(fake, paper(**sig))
            risk = {a['inst']: round(a['risk_usdt'] / m.equity, 3) for a in m.actions if a['action'] == 'open'}
            self.assertEqual(risk, {'BTC-USDT-SWAP': .04, 'PEPE-USDT-SWAP': .02})
            self.assertEqual(m.summary()['execution_profile']['major_factor'], 2.0)
        finally:
            odt.ACTIVE_PROFILE = None; odt.configure_volume_test(odt.datetime(2026, 1, 1, tzinfo=odt.timezone.utc))

    def test_no_entry_while_the_demo_price_is_away_from_the_real_market(self):
        state = dict(odt.new_state(), paused=False)
        fake = FakeOKX()                                                           # demo BTC 70000
        m = odt.Mirror(fake, state, paper(**{'BTC-USDT-SWAP': long_btc()}), NOW, False); m.live = lambda inst: 68000.0   # real market 2.9 % lower
        m.run('run')
        self.assertEqual(fake.posts, []); self.assertEqual(m.actions[-1]['reason'], 'demo_price_off'); self.assertEqual(state['mirrored'], {})
        m2 = odt.Mirror(fake, state, paper(**{'BTC-USDT-SWAP': long_btc()}), NOW, False); m2.live = lambda inst: 69800.0  # back within 1 %: followed
        m2.run('run')
        self.assertEqual([q for q, b in fake.posts][-1], '/api/v5/trade/order')
        m3 = odt.Mirror(FakeOKX(), dict(odt.new_state(), paused=False), paper(**{'BTC-USDT-SWAP': long_btc()}), NOW, False); m3.live = lambda inst: None
        m3.run('run'); self.assertEqual([a['action'] for a in m3.actions], ['open'])  # real price unavailable: the check is skipped

    def test_positions_opened_this_run_are_in_the_summary_right_away(self):
        fake = FakeOKX(); m, _ = run(fake, paper(**{'BTC-USDT-SWAP': long_btc()}))
        self.assertEqual(m.summary()['positions'], [])                                   # snapshot read before the order
        fake.positions = [dict(instId='BTC-USDT-SWAP', posSide='long', pos='3', avgPx='70000', markPx='70000', upl='0', lever='10')]
        m.settle()
        self.assertEqual([(p['inst'], p['ours']) for p in m.summary()['positions']], [('BTC-USDT-SWAP', True)])
        quiet = FakeOKX(); q, _ = run(quiet, paper()); calls = []
        quiet.request = lambda *a, **k: calls.append(a) or {}
        q.settle(); self.assertEqual(calls, [])                                           # nothing traded: no extra requests

    def test_summary_carries_equity_curve_start_equity_and_contract_value(self):
        fake = FakeOKX(positions=[dict(instId='BTC-USDT-SWAP', posSide='long', pos='3', avgPx='70000', markPx='70100', upl='3', lever='5')])
        m, state = run(fake, paper())
        m.record_equity(); fake.equity = 5010
        m2 = odt.Mirror(fake, state, paper(), NOW + timedelta(minutes=5)); m2.run(); m2.record_equity()
        s = m2.summary()                                  # 5 minutes later: not stored, but the curve ends at the latest equity
        self.assertEqual(s['start_equity'], 5000); self.assertEqual([p[1] for p in s['history']], [5000, 5010]); self.assertEqual(len(state['history']), 1)
        m3 = odt.Mirror(fake, state, paper(), NOW + timedelta(minutes=16)); m3.run(); m3.record_equity()
        self.assertEqual([p[1] for p in state['history']], [5000, 5010])
        self.assertEqual(s['positions'][0]['ctVal'], float(SPECS['BTC-USDT-SWAP']['ctVal']))
        self.assertEqual(s['closed_trades'], 0)
        state['history'] = [[i, 1.0] for i in range(odt.HISTORY_POINTS + 5)]; m3.record_equity()
        self.assertEqual(len(state['history']), odt.HISTORY_POINTS)

    def test_evolved_leverage_follows_the_gene_then_stop_then_instrument_cap(self):
        try:
            odt.configure_evolved()
            px, stop = 78000.0, 78000.0 * 0.98   # 2 % stop: max L before liq is < 1/(0.02+0.005+0.0005) ≈ 39
            sig = {'BTC-USDT-SWAP': dict(long_btc(stop=stop), strategy='evolved', risk=.02, leverage=100, entry=px)}
            caps = {'BTC-USDT-SWAP': dict(SPECS['BTC-USDT-SWAP'], lever='100')}
            fake = FakeOKX(equity=100000, prices={'BTC-USDT-SWAP': px}, instruments=caps)
            m, _ = run(fake, paper(**sig))
            opened = [a for a in m.actions if a['action'] == 'open'][-1]
            self.assertLess(opened['leverage'], 100)
            self.assertEqual(opened['leverage'], odt.clamp_leverage(100, (px - stop) / px, cap=100))
            # instrument cap 15× is applied after the stop rule
            caps['BTC-USDT-SWAP'] = dict(SPECS['BTC-USDT-SWAP'], lever='15')
            fake = FakeOKX(equity=100000, prices={'BTC-USDT-SWAP': px}, instruments=caps)
            m, _ = run(fake, paper(**sig))
            self.assertEqual([a for a in m.actions if a['action'] == 'open'][-1]['leverage'], 15)
            # tight 0.3 % stop can keep a 50× gene when the instrument allows 75×
            tight = px * 0.997
            caps['BTC-USDT-SWAP'] = dict(SPECS['BTC-USDT-SWAP'], lever='75')
            sig2 = {'BTC-USDT-SWAP': dict(long_btc(stop=tight), strategy='evolved', risk=.02, leverage=50, entry=px)}
            fake = FakeOKX(equity=100000, prices={'BTC-USDT-SWAP': px}, instruments=caps)
            m, _ = run(fake, paper(**sig2))
            self.assertEqual([a for a in m.actions if a['action'] == 'open'][-1]['leverage'], 50)
        finally:
            odt.ACTIVE_PROFILE = None; odt.configure_volume_test(odt.datetime(2026, 1, 1, tzinfo=odt.timezone.utc))

    def test_instrument_max_leverage_falls_back_to_twenty_when_the_spec_is_missing(self):
        try:
            odt.configure_evolved()
            px, stop = 78000.0, 78000.0 * 0.997   # tight stop, gene 80× would otherwise stand
            sig = {'BTC-USDT-SWAP': dict(long_btc(stop=stop), strategy='evolved', risk=.02, leverage=80, entry=px)}
            # list endpoint fails and the per-inst row has no lever
            bare = {'BTC-USDT-SWAP': dict(SPECS['BTC-USDT-SWAP'])}  # no lever key
            fake = FakeOKX(equity=100000, prices={'BTC-USDT-SWAP': px}, instruments=False)
            # per-inst fetch still works (FakeOKX list=False only fails the list path); force both to lack lever
            fake.instruments = False
            # When the list fails, inst_max_lever falls through to spec(); give spec no lever
            class Bare(FakeOKX):
                def request(self, method, path, body=None):
                    if path.startswith('/api/v5/public/instruments') and 'instId=' in path:
                        inst = path.split('instId=')[1].split('&')[0]
                        return {'code': '0', 'data': [dict(SPECS[inst], instId=inst)]}  # no lever
                    if path.startswith('/api/v5/public/instruments'):
                        return {'code': '50014', 'msg': 'unavailable', 'data': []}
                    return super().request(method, path, body)
            fake = Bare(equity=100000, prices={'BTC-USDT-SWAP': px})
            m, _ = run(fake, paper(**sig))
            self.assertEqual([a for a in m.actions if a['action'] == 'open'][-1]['leverage'], 20)
            self.assertEqual(odt.FALLBACK_INST_LEVERAGE, 20)
        finally:
            odt.ACTIVE_PROFILE = None; odt.configure_volume_test(odt.datetime(2026, 1, 1, tzinfo=odt.timezone.utc))

    def test_a_major_missing_from_the_spec_falls_back_to_twenty_like_paper(self):
        try:
            odt.configure_evolved()
            self.assertIs(odt.MAJORS, __import__('execution', fromlist=['MAJORS']).MAJORS)
            self.assertEqual(odt.FALLBACK_INST_LEVERAGE, 20)
            px, stop = 78000.0, 78000.0 * 0.997
            sig = {'BTC-USDT-SWAP': dict(long_btc(stop=stop), strategy='evolved', risk=.02, leverage=80, entry=px)}
            class BareMajor(FakeOKX):
                def request(self, method, path, body=None):
                    if path.startswith('/api/v5/public/instruments') and 'instId=' in path:
                        inst = path.split('instId=')[1].split('&')[0]
                        return {'code': '0', 'data': [dict(SPECS[inst], instId=inst)]}  # no lever
                    if path.startswith('/api/v5/public/instruments'):
                        return {'code': '50014', 'msg': 'unavailable', 'data': []}
                    return super().request(method, path, body)
            fake = BareMajor(equity=100000, prices={'BTC-USDT-SWAP': px})
            m, _ = run(fake, paper(**sig))
            self.assertEqual([a for a in m.actions if a['action'] == 'open'][-1]['leverage'], 20)
        finally:
            odt.ACTIVE_PROFILE = None; odt.configure_volume_test(odt.datetime(2026, 1, 1, tzinfo=odt.timezone.utc))

    def test_evolved_entry_is_skipped_when_even_one_x_cannot_place_the_stop_before_liq(self):
        try:
            odt.configure_evolved()
            px = 78000.0
            stop = px * 0.001   # ~99.9 % stop, wider than 1× room under 0.5 % MMR
            sig = {'BTC-USDT-SWAP': dict(long_btc(stop=stop), strategy='evolved', risk=.02, leverage=10, entry=px)}
            caps = {'BTC-USDT-SWAP': dict(SPECS['BTC-USDT-SWAP'], lever='100')}
            fake = FakeOKX(equity=100000, prices={'BTC-USDT-SWAP': px}, instruments=caps)
            m, _ = run(fake, paper(**sig))
            self.assertFalse([a for a in m.actions if a['action'] == 'open'])
            skips = [a for a in m.actions if a.get('reason') == 'stop_before_liquidation']
            self.assertTrue(skips)
        finally:
            odt.ACTIVE_PROFILE = None; odt.configure_volume_test(odt.datetime(2026, 1, 1, tzinfo=odt.timezone.utc))

    def test_refuses_to_run_or_send_without_the_simulated_trading_flag(self):
        self.addCleanup(setattr, odt, 'DEMO_ONLY', True)
        self.addCleanup(setattr, odt, 'SIMULATED_VALUE', '1')
        self.addCleanup(setattr, odt, 'SIMULATED_HEADER', 'x-simulated-trading')
        odt.assert_demo_only()   # normal lock is fine
        odt.DEMO_ONLY = False
        with self.assertRaises(SystemExit):
            odt.assert_demo_only()
        odt.DEMO_ONLY = True
        odt.SIMULATED_VALUE = '0'
        with self.assertRaises(SystemExit):
            odt.assert_demo_only()
        odt.SIMULATED_VALUE = '1'
        odt.SIMULATED_HEADER = 'x-other'
        with self.assertRaises(SystemExit):
            odt.assert_demo_only()
        odt.SIMULATED_HEADER = 'x-simulated-trading'
        # a request is still forced to carry the header; secrets stay off argv (see the next test)

    def test_signed_requests_carry_the_demo_header_and_keep_secrets_off_argv(self):
        import tempfile
        fake = Path(tempfile.mkdtemp()) / 'curl'
        fake.write_text('#!/bin/sh\necho "$@" > "$(dirname "$0")/args"\ncat > "$(dirname "$0")/stdin"\necho \'{"code":"0","data":[]}\'\n')
        fake.chmod(0o755)
        odt.OKX('KEY1', 'SECRET1', 'PASS1', curl=str(fake)).request('POST', '/api/v5/trade/order', {'instId': 'BTC-USDT-SWAP', 'sz': '1'})
        args, cfg = (fake.parent / 'args').read_text(), (fake.parent / 'stdin').read_text()
        for s in ('KEY1', 'SECRET1', 'PASS1'): self.assertNotIn(s, args)
        self.assertIn('x-simulated-trading: 1', cfg); self.assertIn('request = "POST"', cfg); self.assertIn('\\"instId\\"', cfg)


if __name__ == '__main__':
    unittest.main()

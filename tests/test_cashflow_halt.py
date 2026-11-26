"""CASHFLOW HALT RULE (2026-11-26): the daily halt ignores deposits / withdrawals / fund transfers of the trading account.

adjusted_day_pnl = (equity - day_start) - net_flow, net_flow = signed USDT balChg of GET /api/v5/account/bills type 1
(Transfer) with ts in [max(00:00 UTC+7 of the day, day_start_ts), now]. No network: fake clients only."""
import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit, parse_qs
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'tools'))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import okx_demo_trader as odt
import cashflow
from test_okx_demo_trader import FakeOKX

# The incident: 07:47:32Z withdrawal, 07:50Z run. Local day (UTC+7) 2026-11-26 started at 2026-11-25T17:00Z.
NOW = datetime(2026, 11, 26, 7, 50, tzinfo=timezone.utc)
NOW_MS = int(NOW.timestamp() * 1000)
DAY = '2026-11-26'
DAY0_MS = int(datetime(2026, 11, 25, 17, 0, tzinfo=timezone.utc).timestamp() * 1000)
STAMP_MS = int(datetime(2026, 11, 25, 17, 50, tzinfo=timezone.utc).timestamp() * 1000)   # first run of the day
WITHDRAW_TS = 1795679252000
H = 3600_000


def bill(bid, ts, chg, type_='1', sub='12', ccy='USDT'):
    return dict(billId=str(bid), ts=str(ts), balChg=str(chg), type=type_, subType=sub, ccy=ccy, bal='0')


class BillsOKX(FakeOKX):
    """FakeOKX + a paginated account/bills (newest first, `after` = older than bill id, begin/end/limit honoured)."""
    def __init__(self, bills=(), page=None, server_filter=True, overlap=False, fail=None, **kw):
        super().__init__(**kw)
        self.all_bills = sorted(bills, key=lambda b: (int(b['ts']), int(b['billId'])), reverse=True)
        self.page, self.server_filter, self.overlap, self.fail = page, server_filter, overlap, fail
        self.calls = []

    def request(self, method, path, body=None):
        self.calls.append((method, path))
        if not path.startswith('/api/v5/account/bills'):
            return super().request(method, path, body)
        if self.fail == 'raise':
            raise odt.OKXError('GET /api/v5/account/bills failed')
        if self.fail == 'code':
            return {'code': '50113', 'msg': 'Invalid sign', 'data': []}
        if self.fail == 'malformed':
            return {'code': '0', 'data': [{'ts': 'x'}]}
        if self.fail == 'not_dict':
            return ['oops']
        if self.fail == 'balchg':
            return {'code': '0', 'data': [dict(bill(1, NOW_MS - H, 'abc'))]}
        q = {k: v[0] for k, v in parse_qs(urlsplit(path).query).items()}
        rows = self.all_bills
        if self.server_filter:
            rows = [b for b in rows if int(q['begin']) <= int(b['ts']) <= int(q['end'])]
        if 'after' in q:
            ids = [b['billId'] for b in rows]
            i = ids.index(q['after']) + (0 if self.overlap else 1)   # overlap: the cursor row comes again (de-dup)
            rows = rows[i:]
        return {'code': '0', 'data': rows[:self.page or int(q['limit'])]}


class CashflowHalt(unittest.TestCase):
    def setUp(self):
        odt.configure_evolved()                                     # live profile: daily halt 25 %
        self.assertEqual(odt.DAILY_HALT, .25)
        self.addCleanup(lambda: (setattr(odt, 'ACTIVE_PROFILE', None), odt.configure_volume_test(datetime(2026, 1, 1, tzinfo=timezone.utc))))

    def state(self, day_start=202.26, **kw):
        return dict(odt.new_state('live'), paused=False, day=DAY, day_start=day_start, day_start_ts=STAMP_MS, **kw)

    def go(self, fake, state, now=NOW):
        m = odt.Mirror(fake, state, {'positions': {}}, now)
        m.run('run')
        return m

    def acts(self, m, kind):
        return [a for a in m.actions if a['action'] == kind]

    # ---- the incident --------------------------------------------------------------------------------
    def test_incident_withdrawal_of_100_is_not_a_loss(self):
        fake = BillsOKX([bill(900, WITHDRAW_TS, '-100'), bill(901, WITHDRAW_TS - 60_000, '-0.11', sub='12')], equity=102.03)
        st = self.state()
        m = self.go(fake, st)
        self.assertFalse(st['halted'])
        self.assertEqual(self.acts(m, 'daily_halt'), [])
        self.assertAlmostEqual(st['day_cashflow'], -100.11)
        self.assertEqual(st['day_cashflow_count'], 2)
        s = m.summary()
        self.assertAlmostEqual(s['day_pnl_ex_cashflow'], 102.03 - 202.26 + 100.11, places=6)   # ~ -0.12
        self.assertAlmostEqual(s['day_cashflow'], -100.11); self.assertEqual(s['day_start_ts'], STAMP_MS)
        log = self.acts(m, 'cashflow')[0]
        self.assertEqual((log['net_flow'], log['count']), (-100.11, 2))
        self.assertEqual(log['window_from'], '2026-11-25T17:50:00Z')
        # idempotent: the next run recomputes the same window, does not subtract twice and does not log again
        m2 = self.go(fake, st)
        self.assertAlmostEqual(st['day_cashflow'], -100.11); self.assertEqual(self.acts(m2, 'cashflow'), [])
        self.assertFalse(st['halted'])

    def test_without_the_rule_the_incident_would_have_halted(self):
        fake = BillsOKX([], equity=102.03)                          # same numbers, no bill => old behaviour: halt
        st = self.state()
        m = self.go(fake, st)
        self.assertTrue(st['halted'])
        h = self.acts(m, 'daily_halt')[0]
        self.assertEqual((h['others_day'], h['raw_day'], h['day_cashflow']), (-100.23, -100.23, 0))

    # ---- deposits / transfers both ways ---------------------------------------------------------------
    def test_deposit_only_equity_up_no_halt(self):
        fake = BillsOKX([bill(1, NOW_MS - H, '500', sub='11')], equity=702.26)
        st = self.state(); self.go(fake, st)
        self.assertFalse(st['halted']); self.assertAlmostEqual(st['day_cashflow'], 500)

    def test_deposit_does_not_mask_a_real_loss(self):
        # lost 60 of 202.26 (-29.7 %) then deposited 100: raw equity change is +40, adjusted -60 => halt
        fake = BillsOKX([bill(1, NOW_MS - H, '100', sub='11')], equity=242.26)
        st = self.state(); m = self.go(fake, st)
        self.assertTrue(st['halted'])
        h = self.acts(m, 'daily_halt')[0]
        self.assertEqual((h['raw_day'], h['day_cashflow'], h['others_day']), (40.0, 100.0, -60.0))

    def test_small_loss_plus_deposit_no_halt(self):
        fake = BillsOKX([bill(1, NOW_MS - H, '100', sub='11')], equity=282.26)   # -20 + 100
        st = self.state(); self.go(fake, st)
        self.assertFalse(st['halted'])

    def test_transfer_in_and_out_signed(self):
        bills = [bill(1, NOW_MS - 3 * H, '50', sub='11'), bill(2, NOW_MS - 2 * H, '-80', sub='12'), bill(3, NOW_MS - H, '-20', sub='12')]
        fake = BillsOKX(bills, equity=152.26)                       # 202.26 + 50 - 80 - 20, no trading change
        st = self.state(); self.go(fake, st)
        self.assertAlmostEqual(st['day_cashflow'], -50); self.assertFalse(st['halted'])

    def test_other_bill_types_and_currencies_are_not_cash_flow(self):
        bills = [bill(1, NOW_MS - H, '-60', type_='2', sub='1'),      # trade
                 bill(2, NOW_MS - H, '-5', type_='8', sub='173'),     # funding fee
                 bill(3, NOW_MS - H, '-100', ccy='USDC')]              # transfer of another currency (not in USDT eq)
        fake = BillsOKX(bills, equity=100.0, server_filter=False)
        st = self.state(); self.go(fake, st)
        self.assertEqual(st['day_cashflow'], 0); self.assertTrue(st['halted'])

    # ---- real loss ---------------------------------------------------------------------------------------
    def test_real_loss_with_zero_flows_still_halts_and_flattens(self):
        pos = [dict(instId='BTC-USDT-SWAP', posSide='long', pos='3.57')]
        fake = BillsOKX([], equity=150.0, positions=pos)            # -25.8 %
        st = self.state()
        st['mirrored'] = {'BTC-USDT-SWAP|1': dict(inst='BTC-USDT-SWAP', side=1, posSide='long', status='open', contracts='3.57', strategy='evolved')}
        m = self.go(fake, st)
        self.assertTrue(st['halted'])
        self.assertIn('/api/v5/trade/close-position', [p for p, _ in fake.posts])
        self.assertEqual(self.acts(m, 'daily_halt')[0]['day_cashflow'], 0)

    # ---- window ---------------------------------------------------------------------------------------
    def test_flows_before_day_start_ts_or_before_the_day_are_not_subtracted(self):
        bills = [bill(1, DAY0_MS - H, '-500'),                     # yesterday (UTC+7)
                 bill(2, DAY0_MS + 10 * 60_000, '-100'),           # today but before day_start was captured: already in day_start
                 bill(3, NOW_MS - H, '-30')]                         # counted
        fake = BillsOKX(bills, equity=172.26, server_filter=False)
        st = self.state(); self.go(fake, st)
        self.assertAlmostEqual(st['day_cashflow'], -30)
        path = next(p for _, p in fake.calls if p.startswith('/api/v5/account/bills'))
        q = parse_qs(urlsplit(path).query)
        self.assertEqual((q['begin'], q['end'], q['ccy'], q['type'], q['limit']), ([str(STAMP_MS)], [str(NOW_MS)], ['USDT'], ['1'], ['100']))

    def test_state_without_day_start_ts_falls_back_to_start_of_day(self):
        bills = [bill(1, DAY0_MS - 1, '-500'), bill(2, DAY0_MS + 10 * 60_000, '-100')]
        fake = BillsOKX(bills, equity=102.03, server_filter=False)
        st = self.state(); del st['day_start_ts']
        self.go(fake, st)
        self.assertAlmostEqual(st['day_cashflow'], -100); self.assertFalse(st['halted'])

    def test_window_start_helper(self):
        self.assertEqual(cashflow.day_start_ms(DAY), DAY0_MS)
        self.assertIsNone(cashflow.day_start_ms(None)); self.assertIsNone(cashflow.day_start_ms('bad'))
        self.assertEqual(cashflow.window_start({'day': DAY}, NOW_MS), DAY0_MS)
        self.assertEqual(cashflow.window_start({'day': DAY, 'day_start_ts': STAMP_MS}, NOW_MS), STAMP_MS)
        self.assertEqual(cashflow.window_start({'day': DAY, 'day_start_ts': 'junk'}, NOW_MS), DAY0_MS)
        self.assertEqual(cashflow.window_start({}, NOW_MS), NOW_MS)          # nothing known: empty window

    # ---- pagination ---------------------------------------------------------------------------------
    def test_pagination_and_dedup_across_pages(self):
        bills = [bill(100 + i, NOW_MS - H - i * 1000, '-1') for i in range(250)]
        fake = BillsOKX(bills, equity=1202.26 - 250, overlap=True)  # each next page repeats the cursor row
        st = self.state(day_start=1202.26)
        self.go(fake, st)
        self.assertAlmostEqual(st['day_cashflow'], -250); self.assertEqual(st['day_cashflow_count'], 250)
        pages = [p for _, p in fake.calls if p.startswith('/api/v5/account/bills')]
        self.assertEqual(len(pages), 3)
        self.assertIn('after=', pages[1]); self.assertFalse(st['halted'])

    def test_page_cap_is_unavailable_and_falls_back(self):
        class Endless(FakeOKX):                                     # always a full page of new bills
            n = 0
            def request(self, method, path, body=None):
                if not path.startswith('/api/v5/account/bills'):
                    return super().request(method, path, body)
                Endless.n += 1
                return {'code': '0', 'data': [bill(Endless.n * 1000 + k, NOW_MS - H, '-0.01') for k in range(100)]}
        st = self.state(day_cashflow=-100.0, day_cashflow_count=1)
        m = self.go(Endless(equity=102.03), st)
        self.assertEqual(Endless.n, cashflow.MAX_PAGES)
        warn = self.acts(m, 'cashflow_unavailable')[0]
        self.assertEqual((warn['reason'], warn['fallback']), ('page_cap', -100.0))
        self.assertFalse(st['halted'])

    def test_compute_stops_when_cursor_repeats(self):
        calls = []
        def r(method, path):
            calls.append(path)
            return {'code': '0', 'data': [bill(i, NOW_MS - H, '-1') for i in range(100)][::-1][:99] + [bill(7, NOW_MS - H, '-1')]}
        with self.assertRaises(cashflow.CashflowError) as e:
            cashflow.compute(r, STAMP_MS, NOW_MS)
        self.assertEqual(e.exception.reason, 'pagination_stuck')

    # ---- degraded -----------------------------------------------------------------------------------
    def test_failures_never_crash_and_fall_back_to_last_known_flow(self):
        for fail in ('raise', 'code', 'malformed', 'not_dict', 'balchg'):
            with self.subTest(fail=fail):
                fake = BillsOKX(fail=fail, equity=102.03)
                st = self.state(day_cashflow=-100.11, day_cashflow_count=2)     # earlier good run today
                m = self.go(fake, st)
                warn = self.acts(m, 'cashflow_unavailable')
                self.assertEqual(len(warn), 1); self.assertEqual(warn[0]['fallback'], -100.11)
                self.assertNotIn('sign', str(warn[0]).lower().replace('cashflow', ''))
                self.assertFalse(st['halted']); self.assertEqual(st['day_cashflow'], -100.11)

    def test_failure_without_known_flow_is_old_behaviour(self):
        fake = BillsOKX(fail='raise', equity=102.03)
        st = self.state()
        m = self.go(fake, st)
        self.assertEqual(self.acts(m, 'cashflow_unavailable')[0]['reason'], 'request_failed:OKXError')
        self.assertTrue(st['halted'])                                # fallback 0 = halt exactly as before

    def test_client_without_bills_support_does_not_crash(self):
        class Broken(FakeOKX):
            def request(self, method, path, body=None):
                if 'bills' in path:
                    raise RuntimeError('boom')
                return super().request(method, path, body)
        st = self.state(); m = self.go(Broken(equity=200.0), st)
        self.assertEqual(self.acts(m, 'cashflow_unavailable')[0]['reason'], 'request_failed:RuntimeError')

    # ---- live server state ------------------------------------------------------------------------
    def test_live_server_state_stays_halted_until_the_day_rolls_over(self):
        st = dict(odt.new_state('live'), paused=False, day=DAY, day_start=202.26, halted=True)   # no day_start_ts / day_cashflow
        fake = BillsOKX([bill(900, WITHDRAW_TS, '-100')], equity=102.03)
        later = datetime(2026, 11, 26, 9, 50, tzinfo=timezone.utc)
        m = self.go(fake, st, later)
        self.assertTrue(st['halted'])                               # today's halt is left alone
        self.assertEqual(self.acts(m, 'daily_halt'), [])
        self.assertAlmostEqual(st['day_cashflow'], -100)
        self.assertAlmostEqual(m.summary()['day_pnl_ex_cashflow'], 102.03 - 202.26 + 100, places=6)
        # 17:50Z = 00:50 UTC+7 on 2026-11-27: new day, new day_start, cash flow reset, halt lifted
        nxt = datetime(2026, 11, 26, 17, 50, tzinfo=timezone.utc)
        self.go(fake, st, nxt)
        self.assertEqual(st['day'], '2026-11-27'); self.assertFalse(st['halted'])
        self.assertEqual(st['day_start'], 102.03); self.assertEqual(st['day_start_ts'], int(nxt.timestamp() * 1000))
        self.assertEqual(st['day_cashflow'], 0)

    # ---- read-only ----------------------------------------------------------------------------------
    def test_cashflow_code_only_sends_get(self):
        fake = BillsOKX([bill(1, NOW_MS - H, '-100')], equity=102.03)
        st = self.state(); self.go(fake, st)
        bills = [(m, p) for m, p in fake.calls if 'bills' in p]
        self.assertTrue(bills)
        self.assertTrue(all(m == 'GET' and p.startswith('/api/v5/account/bills?') for m, p in bills))
        self.assertEqual(fake.posts, [])
        self.assertFalse([p for _, p in fake.calls if 'withdrawal' in p or 'transfer' in p])
        src = (Path(__file__).resolve().parents[1] / 'tools' / 'cashflow.py').read_text()
        self.assertNotIn("'POST'", src); self.assertNotIn('/asset/withdrawal', src); self.assertNotIn('/asset/transfer', src)

    def test_dry_run_write_guard_is_untouched(self):
        client = odt.OKX('k', 's', 'p', mode='live', dry_run=True)
        with self.assertRaises(odt.OKXError):
            client.request('POST', '/api/v5/asset/withdrawal', {})  # the write guard is untouched


class DownstreamTools(unittest.TestCase):
    """live_sync / live_alert read the trader's additive `day_cashflow`; old summaries without it behave as before."""
    def test_live_sync_day_pnl_excludes_cashflow(self):
        import live_sync as ls
        from test_live_sync import sample_summary, sample_state
        old = ls.build_snapshot(sample_summary(), sample_state())
        self.assertNotIn('day_cashflow', old)
        self.assertAlmostEqual(old['day_pnl'], 126.82424 - 126.670001, places=5)
        snap = ls.build_snapshot(dict(sample_summary(), equity=26.82423962, day_cashflow=-100), sample_state())
        self.assertEqual(snap['day_cashflow'], -100)
        self.assertAlmostEqual(snap['day_pnl'], 126.82424 - 126.670001, places=5)

    def test_live_alert_day_loss_excludes_cashflow(self):
        import live_alert as la
        from test_live_alert import summary, NOW as ANOW
        self.assertTrue(la.evaluate(summary(equity=26.67, day_start=126.67), [], ANOW))                    # old summary: -79 % alert
        self.assertEqual(la.evaluate(summary(equity=26.67, day_start=126.67, day_cashflow=-100), [], ANOW), [])
        self.assertTrue(la.evaluate(summary(equity=213.0, day_start=126.67, day_cashflow=100), [], ANOW))  # deposit + real -10.8 %


if __name__ == '__main__':
    unittest.main()

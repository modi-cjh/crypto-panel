"""tools/live_alert.py: each anomaly on a local mock sample, the normal case, dedup and the CLI. No server, no network."""
import datetime as dt
import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'tools'))
import live_alert as la  # noqa: E402
import live_sync as ls  # noqa: E402

NOW = dt.datetime(2026, 11, 15, 16, 0, tzinfo=dt.timezone.utc)


def summary(**over):
    s = {'updated_at': '2026-11-15T15:54:09Z', 'equity': 129.46, 'available': 110.0, 'day_start': 126.67,
         'paused': False, 'halted': False, 'dry_run': False, 'trading_mode': 'live', 'positions': [], 'recent_actions': []}
    s.update(over)
    return s


def act(ts, action, inst='BTC-USDT-SWAP', **kw):
    return dict(ts=ts, action=action, inst=inst, dry_run=False, **kw)


NORMAL_ACTIONS = [
    act('2026-11-15T12:00:05Z', 'open', side='long', strategy='momentum'),
    act('2026-11-15T13:00:05Z', 'closed_on_exchange', 'ETH-USDT-SWAP', outcome='stop', pnl=-1.2),
    act('2026-11-15T14:00:05Z', 'closed_on_exchange', 'SOL-USDT-SWAP', outcome='target', pnl=2.0),
    act('2026-11-15T15:00:05Z', 'skip', 'XRP-USDT-SWAP', reason='same_side_risk'),
    act('2026-11-15T12:00:00Z', 'error', 'DOGE-USDT-SWAP', step='order', detail='51008 old'),   # outside 90 min
]


def codes(alerts):
    return [a['code'] for a in alerts]


class Conditions(unittest.TestCase):
    def test_normal_has_no_alert(self):
        self.assertEqual(la.evaluate(summary(), NORMAL_ACTIONS, NOW), [])

    def test_stale(self):
        alerts = la.evaluate(summary(updated_at='2026-11-15T13:30:00Z'), [], NOW)
        self.assertEqual(codes(alerts), ['stale'])
        self.assertIn('2.5 小时', alerts[0]['msg'])
        self.assertEqual(la.evaluate(summary(updated_at='2026-11-15T14:01:00Z'), [], NOW), [])   # 1h59m: fine
        self.assertEqual(codes(la.evaluate(summary(updated_at=None), [], NOW)), ['stale'])

    def test_halted(self):
        self.assertEqual(codes(la.evaluate(summary(halted=True), [], NOW)), ['halted'])

    def test_paused(self):
        self.assertEqual(codes(la.evaluate(summary(paused=True), [], NOW)), ['paused'])

    def test_day_loss(self):
        alerts = la.evaluate(summary(equity=113.0, day_start=126.67), [], NOW)     # -10.79%
        self.assertEqual(codes(alerts), ['day_loss'])
        self.assertIn('-10.79%', alerts[0]['msg'])
        self.assertEqual(la.evaluate(summary(equity=114.1, day_start=126.67), [], NOW), [])  # -9.9%: fine
        self.assertEqual(la.evaluate(summary(day_start=0), [], NOW), [])

    def test_stop_streak(self):
        rows = [
            act('2026-11-15T10:00:00Z', 'closed_on_exchange', 'SOL-USDT-SWAP', outcome='target', pnl=1.0),
            act('2026-11-15T11:00:00Z', 'closed_on_exchange', 'ETH-USDT-SWAP', outcome='stop', pnl=-1.0),
            act('2026-11-15T12:00:00Z', 'open', 'ADA-USDT-SWAP'),
            act('2026-11-15T13:00:00Z', 'close', 'BTC-USDT-SWAP', reason='stop_missing_price_beyond_stop'),
            act('2026-11-15T14:00:00Z', 'closed_on_exchange', 'XRP-USDT-SWAP', outcome='liquidation', pnl=-3.0),
        ]
        alerts = la.evaluate(summary(), rows, NOW)
        self.assertEqual(codes(alerts), ['stop_streak'])
        self.assertIn('连续 3 笔', alerts[0]['msg'])
        self.assertEqual(alerts[0]['fingerprint'], 'stop_streak:2026-11-15T14:00:00Z')
        # a target in between breaks the streak
        rows[2] = act('2026-11-15T12:00:00Z', 'closed_on_exchange', 'ADA-USDT-SWAP', outcome='target', pnl=0.5)
        self.assertEqual(la.evaluate(summary(), rows, NOW), [])

    def test_stop_streak_uses_summary_enrichment(self):
        """The outcome of an exchange close is often only in summary.recent_actions (filled in by a later run)."""
        rows = [act(f'2026-11-15T1{i}:00:00Z', 'closed_on_exchange', 'ETH-USDT-SWAP') for i in range(3)]
        self.assertEqual(la.evaluate(summary(), rows, NOW), [])                      # outcome unknown: no claim
        recent = [dict(r, outcome='stop', pnl=-1.0) for r in rows]
        self.assertEqual(codes(la.evaluate(summary(recent_actions=recent), rows, NOW)), ['stop_streak'])

    def test_api_error(self):
        rows = NORMAL_ACTIONS + [act('2026-11-15T15:20:00Z', 'error', 'SOL-USDT-SWAP', step='order',
                                     detail='51008 Order failed. Insufficient USDT margin in account')]
        alerts = la.evaluate(summary(), rows, NOW)
        self.assertEqual(codes(alerts), ['api_error'])
        self.assertIn('SOL-USDT-SWAP order', alerts[0]['msg'])
        self.assertIn('51008', alerts[0]['msg'])

    def test_error_detail_with_secret_shape_is_hidden(self):
        rows = [act('2026-11-15T15:20:00Z', 'error', step='order', detail='key 0f8b2c4e-1a2b-4c3d-8e9f-0123456789ab')]
        alerts = la.evaluate(summary(), rows, NOW)
        self.assertEqual(codes(alerts), ['api_error'])
        self.assertNotIn('0f8b2c4e', alerts[0]['msg'])

    def test_several_at_once(self):
        s = summary(updated_at='2026-11-15T10:00:00Z', halted=True, paused=True, equity=90.0, day_start=126.67)
        self.assertEqual(codes(la.evaluate(s, [], NOW)), ['stale', 'halted', 'paused', 'day_loss'])


class Dedup(unittest.TestCase):
    def test_same_alert_is_reported_once_then_again_after_clearing(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, 'state.json')
            halted = la.evaluate(summary(halted=True), [], NOW)
            r, code = la.report(halted, NOW, path)
            self.assertEqual((code, r['notify'], codes(r['new_alerts'])), (1, True, ['halted']))
            r, code = la.report(halted, NOW, path)                                  # still halted next hour
            self.assertEqual((code, r['notify'], r['new_alerts']), (2, False, []))
            self.assertFalse(r['ok'])
            both = halted + la.evaluate(summary(paused=True), [], NOW)              # a new condition joins
            r, code = la.report(both, NOW, path)
            self.assertEqual((code, codes(r['new_alerts'])), (1, ['paused']))
            r, code = la.report([], NOW, path)                                      # recovered
            self.assertEqual((code, r['ok'], r['notify'], r['resolved']), (0, True, False, ['halted', 'paused']))
            r, code = la.report(halted, NOW, path)                                  # comes back: report again
            self.assertEqual((code, codes(r['new_alerts'])), (1, ['halted']))

    def test_new_error_event_is_new(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, 'state.json')
            first = [act('2026-11-15T15:20:00Z', 'error', step='order')]
            la.report(la.evaluate(summary(), first, NOW), NOW, path)
            r, code = la.report(la.evaluate(summary(), first, NOW), NOW, path)
            self.assertEqual(code, 2)
            second = first + [act('2026-11-15T15:50:00Z', 'error', step='close')]
            r, code = la.report(la.evaluate(summary(), second, NOW), NOW, path)
            self.assertEqual(code, 1)

    def test_no_state_always_notifies(self):
        halted = la.evaluate(summary(halted=True), [], NOW)
        self.assertEqual(la.report(halted, NOW)[1], 1)
        self.assertEqual(la.report(halted, NOW)[1], 1)

    def test_broken_state_file_is_ignored(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, 'state.json')
            Path(path).write_text('not json')
            self.assertEqual(la.report(la.evaluate(summary(paused=True), [], NOW), NOW, path)[1], 1)


class Cli(unittest.TestCase):
    def run_cli(self, s, rows, *extra):
        with tempfile.TemporaryDirectory() as d:
            sp, ap = os.path.join(d, 'summary.json'), os.path.join(d, 'actions.jsonl')
            Path(sp).write_text(json.dumps(s))
            Path(ap).write_text(''.join(json.dumps(r) + '\n' for r in rows) + '{"half-written')
            out = io.StringIO()
            with redirect_stdout(out):
                code = la.main(['--summary', sp, '--actions', ap, '--state', '', *extra], now=NOW)
            return code, json.loads(out.getvalue())

    def test_normal(self):
        code, r = self.run_cli(summary(), NORMAL_ACTIONS)
        self.assertEqual((code, r['ok'], r['notify'], r['alerts']), (0, True, False, []))
        self.assertEqual(r['checked_at'], '2026-11-15T16:00:00Z')

    def test_alerting(self):
        code, r = self.run_cli(summary(halted=True), NORMAL_ACTIONS)
        self.assertEqual((code, r['ok'], r['notify'], codes(r['alerts']), set(r['alerts'][0])),
                         (1, False, True, ['halted'], {'code', 'msg', 'fingerprint'}))

    def test_read_error_is_an_alert(self):
        with mock.patch.object(ls, 'read_server', side_effect=ls.SyncError('ssh 读取超时（60 秒）')):
            out = io.StringIO()
            with redirect_stdout(out):
                code = la.main(['--state', ''], now=NOW)
        r = json.loads(out.getvalue())
        self.assertEqual((code, codes(r['alerts'])), (1, ['read_error']))
        self.assertIn('ssh 读取超时', r['alerts'][0]['msg'])


if __name__ == '__main__':
    unittest.main()

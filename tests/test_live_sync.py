"""tools/live_sync.py: whitelist assembly, secret refusal, publish flow. No server, no network:
the server read and git remote are replaced by local fakes (a local bare repository stands in for GitHub)."""
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'tools'))
import live_sync as ls  # noqa: E402

FAKE_SECRET = 'A1B2C3D4E5F60718293A4B5C6D7E8F90'          # 32 hex, shaped like an OKX secret
FAKE_KEY = '0f8b2c4e-1a2b-4c3d-8e9f-0123456789ab'         # UUID, shaped like an OKX API key


def sample_summary(**over):
    s = {
        'updated_at': '2026-11-15T14:02:46Z', 'equity': 126.82423962000001, 'available': 109.49615962000001,
        'day_start': 126.67000052, 'paused': False, 'halted': False, 'dry_run': False, 'trading_mode': 'live',
        'start_equity': 126.66403962000001, 'closed_trades': 1,
        'positions': [
            {'inst': 'SOL-USDT-SWAP', 'side': 'short', 'contracts': '1.58', 'avgPx': '72.43', 'markPx': '72.37',
             'upl': '0.0948000000000036', 'uplRatio': '0.0082838602788904', 'lever': '10', 'margin': '11.434460000000001',
             'mgnMode': 'cross', 'ctVal': 1.0, 'ours': True,
             'secret_note': FAKE_SECRET, 'clOrdId': 'abcDEF1234567890abcdef'},          # not whitelisted: must be dropped
            {'inst': 'BTC-USDT-SWAP', 'side': 'long', 'contracts': '0.01', 'avgPx': '90000', 'markPx': '91000',
             'upl': '10', 'uplRatio': '0.1', 'lever': '5', 'margin': '180', 'mgnMode': 'cross', 'ctVal': 0.01, 'ours': False},
        ],
        'history': [[1794751218461, 126.66], [1794751366879, 126.82], ['bad', 1]],
        'execution_profile': {'mode': 'evolved', 'api_key': FAKE_KEY},
        'open_mirrors': [{'clOrdId': 'abcDEF1234567890abcdef', 'ordId': '1234567890123456789'}],
        'recent_actions': [{'ts': '2026-11-15T15:00:00Z', 'action': 'close', 'inst': 'AVAX-USDT-SWAP', 'pnl': -1.25,
                            'close_price': 8.4, 'outcome': 'stop'}],
    }
    s.update(over)
    return s


def sample_state(**over):
    s = {
        'mode': 'live', 'paused': False, 'halted': False,
        'mirrored': {
            'SOL-USDT-SWAP|1794700800000': {'inst': 'SOL-USDT-SWAP', 'side': -1, 'posSide': 'net', 'strategy': 'evolved',
                                            'contracts': '1.58', 'stop': 80.18401709123, 'target': 48.086853951, 'leverage': 10,
                                            'entry': 72.46, 'clOrdId': 'abcDEF1234567890abcdef', 'opened_at': 1794751218000,
                                            'status': 'open', 'leader': False, 'mgnMode': 'cross', 'champion': 's15653',
                                            'ordId': '1234567890123456789'},
            'BTC-USDT-SWAP|1794499200000': {'inst': 'BTC-USDT-SWAP', 'status': 'skipped', 'side': 1, 'posSide': 'net',
                                            'reason': 'too_old', 'stop': 1},
        },
    }
    s.update(over)
    return s


ACTIONS = '\n'.join(json.dumps(a) for a in [
    {'ts': '2026-11-15T14:00:18Z', 'action': 'resume', 'inst': '-', 'dry_run': False},
    {'ts': '2026-11-15T14:00:18Z', 'action': 'open', 'inst': 'SOL-USDT-SWAP', 'dry_run': False, 'side': -1,
     'strategy': 'evolved', 'contracts': '1.58', 'price': 72.46, 'stop': '80.18', 'target': '48.09', 'leverage': 10,
     'risk_usdt': 7.6, 'clOrdId': 'abcDEF1234567890abcdef', 'ordId': '1234567890123456789'},
    {'ts': '2026-11-15T14:00:18Z', 'action': 'skip', 'inst': 'BNB-USDT-SWAP', 'dry_run': False, 'reason': 'same_side_risk'},
    {'ts': '2026-11-15T15:00:00Z', 'action': 'close', 'inst': 'AVAX-USDT-SWAP', 'side': -1, 'contracts': '5.4',
     'reason': 'paper_closed', 'detail': FAKE_SECRET},
]) + '\n{"ts": "2026-11-15T15:0'          # half-written last line


class Assembly(unittest.TestCase):
    def test_snapshot_whitelist_and_merge(self):
        snap = ls.build_snapshot(sample_summary(), sample_state())
        self.assertEqual(set(snap) - {'schema_version', 'source', 'trading_mode', 'updated_at', 'equity', 'available',
                                      'day_start', 'day_pnl', 'day_pnl_pct', 'start_equity', 'total_pnl', 'paused',
                                      'halted', 'dry_run', 'closed_trades', 'positions', 'history'}, set())
        self.assertAlmostEqual(snap['equity'], 126.82424)
        self.assertAlmostEqual(snap['day_pnl'], 126.82424 - 126.670001, places=5)
        self.assertAlmostEqual(snap['day_pnl_pct'], (126.82423962 / 126.67000052 - 1) * 100, places=4)
        sol, btc = snap['positions']
        self.assertEqual(sol['side'], 'short')
        self.assertEqual((sol['stop'], sol['target']), (80.18402, 48.08685))
        self.assertEqual((sol['strategy'], sol['champion'], sol['opened_at']), ('evolved', 's15653', '2026-11-15T14:00:18Z'))
        self.assertEqual(sol['contracts'], 1.58)
        self.assertEqual(sol['margin'], 11.43446)
        self.assertNotIn('secret_note', sol)
        self.assertNotIn('clOrdId', sol)
        self.assertFalse(btc['ours'])
        self.assertNotIn('stop', btc)               # manual positions never get the program's stop
        self.assertEqual(snap['history'], [[1794751218461, 126.66], [1794751366879, 126.82]])
        text = ls.render(snap)
        for leak in (FAKE_SECRET, FAKE_KEY, 'clOrdId', 'ordId', 'api_key', 'execution_profile', 'open_mirrors'):
            self.assertNotIn(leak, text)
        self.assertEqual(ls.secret_problems(text, env={}), [])

    def test_refuses_demo_data(self):
        with self.assertRaises(ls.SyncError):
            ls.build_snapshot(sample_summary(trading_mode='demo'), sample_state())
        with self.assertRaises(ls.SyncError):
            ls.build_snapshot(sample_summary(), sample_state(mode='demo'))
        with self.assertRaises(ls.SyncError):
            ls.build_snapshot(sample_summary(equity='x'), sample_state())

    def test_fills_whitelist_and_order(self):
        fills = ls.build_fills(ls.parse_actions(ACTIONS), sample_summary())
        self.assertEqual([f['action'] for f in fills['fills']], ['close', 'open'])           # newest first, trades only
        self.assertEqual([e['action'] for e in fills['events']], ['skip', 'resume'])
        close, opened = fills['fills']
        self.assertEqual((close['pnl'], close['close_price'], close['outcome']), (-1.25, 8.4, 'stop'))   # from summary
        self.assertEqual(opened, {'ts': '2026-11-15T14:00:18Z', 'action': 'open', 'inst': 'SOL-USDT-SWAP', 'side': 'short',
                                  'contracts': 1.58, 'price': 72.46, 'stop': 80.18, 'target': 48.09, 'leverage': 10.0,
                                  'strategy': 'evolved', 'dry_run': False})
        allowed = {'ts', 'action', 'inst', 'side', 'contracts', 'price', 'stop', 'target', 'leverage', 'reason',
                   'strategy', 'close_price', 'pnl', 'outcome', 'dry_run'}
        for row in fills['fills'] + fills['events']:
            self.assertLessEqual(set(row), allowed)
        text = ls.render(fills)
        self.assertNotIn(FAKE_SECRET, text)
        self.assertEqual(ls.secret_problems(text, env={}), [])

    def test_limits(self):
        rows = [{'ts': '2026-11-15T14:00:18Z', 'action': 'open', 'inst': 'SOL-USDT-SWAP'}] * 80 + \
               [{'ts': '2026-11-15T14:00:18Z', 'action': 'skip', 'inst': 'SOL-USDT-SWAP', 'reason': 'no_price'}] * 80
        f = ls.build_fills(rows)
        self.assertEqual((len(f['fills']), len(f['events'])), (ls.MAX_FILLS, ls.MAX_EVENTS))

    def test_bad_strings_dropped(self):
        row = ls.fill_row({'ts': 'yesterday', 'action': 'Open; rm -rf', 'inst': '<script>', 'reason': FAKE_SECRET})
        self.assertEqual(row, {'dry_run': False})


TRADE_STATE = {
    'mode': 'live',
    'close_results_v3': {'2026-11-15T18:00:00Z|AVAX-USDT-SWAP': {'pnl': -6.5, 'close_price': 8.37, 'fee': -0.02, 'funding': 0.0}},
    'mirrored': {
        'SOL-USDT-SWAP|1': {'inst': 'SOL-USDT-SWAP', 'side': -1, 'posSide': 'net', 'strategy': 'evolved', 'contracts': '1.58',
                            'stop': 75.0, 'target': 48.09, 'leverage': 10, 'entry': 72.46, 'opened_at': 1794751218461,
                            'status': 'done', 'champion': 's15653', 'leader': True, 'clOrdId': 'abcDEF1234567890abcdef'},
        'AVAX-USDT-SWAP|1': {'inst': 'AVAX-USDT-SWAP', 'side': -1, 'posSide': 'net', 'strategy': 'evolved', 'contracts': '5.4',
                             'stop': 8.375, 'target': 4.248, 'leverage': 10, 'entry': 7.258, 'opened_at': 1794751218461,
                             'status': 'done', 'champion': 's20001'},
        'BNB-USDT-SWAP|2': {'inst': 'BNB-USDT-SWAP', 'side': 1, 'posSide': 'net', 'strategy': 'evolved', 'contracts': '8',
                            'stop': 560.0, 'target': 650.0, 'leverage': 5, 'entry': 590.0, 'opened_at': 1794758400000,
                            'status': 'done', 'champion': 's15653'},
        'ADA-USDT-SWAP|3': {'inst': 'ADA-USDT-SWAP', 'side': -1, 'status': 'open', 'opened_at': 1794758400000, 'entry': 0.15},
    },
}
TRADE_ACTIONS = [
    {'ts': '2026-11-15T14:00:18Z', 'action': 'open', 'inst': 'SOL-USDT-SWAP', 'side': -1, 'strategy': 'evolved', 'contracts': '1.58',
     'price': 72.46, 'stop': '80.18', 'target': '48.09', 'leverage': 10, 'risk_usdt': 12.2, 'leader': True, 'dry_run': False},
    {'ts': '2026-11-15T14:00:18Z', 'action': 'open', 'inst': 'AVAX-USDT-SWAP', 'side': -1, 'strategy': 'evolved', 'contracts': '5.4',
     'price': 7.258, 'stop': '8.375', 'target': '4.248', 'leverage': 10, 'risk_usdt': 6.1, 'dry_run': False},
    {'ts': '2026-11-15T16:00:00Z', 'action': 'open', 'inst': 'BNB-USDT-SWAP', 'side': 1, 'strategy': 'evolved', 'contracts': '8',
     'price': 590.0, 'stop': '560', 'target': '650', 'leverage': 5, 'dry_run': False},
    {'ts': '2026-11-15T16:00:00Z', 'action': 'amend_stop', 'inst': 'SOL-USDT-SWAP', 'old': 80.18, 'new': 75.0, 'dry_run': False},
    {'ts': '2026-11-15T17:00:00Z', 'action': 'closed_on_exchange', 'inst': 'SOL-USDT-SWAP', 'strategy': 'evolved', 'pnl': 18.4,
     'close_price': 48.2, 'fee': -0.05, 'funding': 0.1, 'outcome': 'target', 'dry_run': False},
    {'ts': '2026-11-15T18:00:00Z', 'action': 'closed_on_exchange', 'inst': 'AVAX-USDT-SWAP', 'strategy': 'evolved', 'dry_run': False},
    {'ts': '2026-11-15T19:00:00Z', 'action': 'close', 'inst': 'BNB-USDT-SWAP', 'strategy': 'evolved', 'reason': 'paper_closed',
     'pnl': 1.2, 'close_price': 600.0, 'dry_run': False},
    {'ts': '2026-11-15T19:30:00Z', 'action': 'close', 'inst': 'XRP-USDT-SWAP', 'execution_test': True, 'pnl': 5, 'dry_run': False},
    {'ts': '2026-11-15T19:40:00Z', 'action': 'close', 'inst': 'DOGE-USDT-SWAP', 'pnl': 5, 'dry_run': True},
]


class Trades(unittest.TestCase):
    def build(self):
        summary = sample_summary(history=[[1794751218461, 100.0], [1794754800000, 120.0], [1794758400000, 90.0], [1794762000000, 110.0]],
                                 recent_actions=[])
        return ls.build_trades(TRADE_ACTIONS, TRADE_STATE, summary)

    def test_pairs_closes_with_opens(self):
        doc = self.build()
        self.assertEqual(doc['schema_version'], 1)
        bnb, avax, sol = doc['trades']                                   # newest first; test / dry-run closes dropped
        self.assertEqual((sol['inst'], sol['side'], sol['entry'], sol['exit']), ('SOL-USDT-SWAP', 'short', 72.46, 48.2))
        self.assertEqual((sol['pnl'], sol['champion'], sol['leader']), (18.4, 's15653', True))
        self.assertEqual(sol['stop'], 80.18)                             # the stop at entry, not the amended one
        self.assertEqual((sol['opened_at'], sol['closed_at'], sol['hold_minutes']), ('2026-11-15T14:00:18Z', '2026-11-15T17:00:00Z', 180))
        self.assertAlmostEqual(sol['pnl_pct'], (1 - 48.2 / 72.46) * 100, places=3)
        self.assertAlmostEqual(sol['roe_pct'], sol['pnl_pct'] * 10, places=2)
        self.assertAlmostEqual(sol['r'], 18.4 / 12.2, places=3)          # PnL / risk at entry
        self.assertEqual(sol['outcome'], 'target')
        self.assertEqual([t['exit'] for t in doc['trades']], [600.0, 8.37, 48.2])
        self.assertEqual(avax['pnl'], -6.5)                              # from the trader's close_results_v3 cache
        self.assertEqual(bnb['reason'], 'paper_closed')
        self.assertAlmostEqual(bnb['r'], (600 - 590) / 30, places=3)     # no risk_usdt: price R against the entry stop

    def test_exit_kind(self):
        bnb, avax, sol = self.build()['trades']
        # sol: exchange target -> tp; avax: exchange close without a recorded outcome, fill next to the stop -> sl;
        # bnb: closed by the program -> other
        self.assertEqual([t['exit_type'] for t in (sol, avax, bnb)], ['tp', 'sl', 'other'])

    def test_stats(self):
        st = self.build()['stats']
        o = st['overall']
        self.assertEqual((o['trades'], o['wins'], o['losses']), (3, 2, 1))
        self.assertAlmostEqual(o['win_rate'], 200 / 3, places=3)
        self.assertAlmostEqual(o['pnl'], 18.4 - 6.5 + 1.2, places=5)
        self.assertAlmostEqual(o['payoff'], ((18.4 + 1.2) / 2) / 6.5, places=4)
        self.assertAlmostEqual(o['max_drawdown_closed'], 6.5, places=5)
        self.assertEqual((o['tp'], o['sl']), (1, 1))
        seeds = {r['key']: r for r in st['by_strategy']}
        self.assertEqual(seeds['s15653']['trades'], 2)
        self.assertAlmostEqual(seeds['s15653']['pnl'], 19.6, places=5)
        self.assertEqual(seeds['s20001']['losses'], 1)
        dd = st['equity_drawdown']
        self.assertAlmostEqual(dd['pct'], 25.0)
        self.assertAlmostEqual(dd['usdt'], 30.0)

    def test_whitelist_and_secret_check(self):
        text = ls.render(self.build())
        self.assertNotIn('clOrdId', text)
        self.assertNotIn('abcDEF', text)
        self.assertEqual(ls.secret_problems(text, env={}), [])

    def test_empty(self):
        doc = ls.build_trades([], {'mode': 'live'}, {})
        self.assertEqual(doc['trades'], [])
        self.assertEqual(doc['stats']['overall']['trades'], 0)
        self.assertIsNone(doc['stats']['equity_drawdown'])


class SecretCheck(unittest.TestCase):
    def test_flags(self):
        cases = {
            'name': '{"note": "OKX_API_SECRET"}',
            'hex': '{"x": "%s"}' % FAKE_SECRET,
            'uuid': '{"x": "%s"}' % FAKE_KEY,
            'base64': '{"x": "dGhpcyBpcyBhIHNlY3JldA=="}',
            'mixed': '{"x": "abcdefABCDEF12345"}',
        }
        for label, text in cases.items():
            with self.subTest(label):
                self.assertTrue(ls.secret_problems(text, env={}))

    def test_env_value(self):
        self.assertTrue(ls.secret_problems('{"x": "my pass"}', env={'OKX_API_PASSPHRASE': 'my pass'}))

    def test_harmless(self):
        text = ls.render({'t': 1794751218461, 'reason': 'exchange_position_exists_not_ours', 'inst': '1000PEPE-USDT-SWAP',
                          'ts': '2026-11-15T14:00:18Z', 'champion': 's15653'})
        self.assertEqual(ls.secret_problems(text, env={}), [])

    def test_problem_text_never_contains_the_secret(self):
        for p in ls.secret_problems('{"x": "%s %s"}' % (FAKE_SECRET, FAKE_KEY), env={}):
            self.assertNotIn(FAKE_SECRET, p)
            self.assertNotIn(FAKE_KEY, p)

    def test_checked_raises_code_2(self):
        with self.assertRaises(ls.SyncError) as ctx:
            ls.checked({'x': FAKE_SECRET}, 'snapshot.json')
        self.assertEqual(ctx.exception.code, 2)
        self.assertNotIn(FAKE_SECRET, str(ctx.exception))


def git(cwd, *args):
    return subprocess.run(['git', '-C', cwd, *args], capture_output=True, text=True, check=True).stdout


class Publish(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.remote = os.path.join(self.tmp.name, 'remote.git')
        subprocess.run(['git', 'init', '-q', '--bare', self.remote], check=True)
        # A master branch that must stay untouched.
        seed = os.path.join(self.tmp.name, 'seed')
        subprocess.run(['git', 'init', '-q', '-b', 'master', seed], check=True)
        Path(seed, 'index.html').write_text('x')
        git(seed, 'add', '.')
        git(seed, '-c', 'user.name=t', '-c', 'user.email=t@t', 'commit', '-qm', 'seed')
        git(seed, 'push', '-q', self.remote, 'master')
        self.master = git(self.remote, 'rev-parse', 'master').strip()
        self.work = os.path.join(self.tmp.name, 'work')

    def tearDown(self):
        self.tmp.cleanup()

    def run_main(self, summary=None):
        server = (summary or sample_summary(), sample_state(), ACTIONS)
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(ls, 'read_server', return_value=server), redirect_stdout(out), redirect_stderr(err):
            code = ls.main(['--repo', self.remote, '--workdir', self.work, '--branch', 'live-data'])
        return code, out.getvalue(), err.getvalue()

    def test_first_run_creates_orphan_branch_then_idempotent(self):
        code, out, _ = self.run_main()
        self.assertEqual(code, 0)
        self.assertTrue(out.startswith('pushed'))
        files = sorted(git(self.remote, 'ls-tree', '--name-only', 'live-data').split())
        self.assertEqual(files, ['README.md', 'fills.json', 'snapshot.json', 'trades.json'])
        self.assertEqual(git(self.remote, 'rev-list', '--count', 'live-data').strip(), '1')   # orphan: no master history
        snap = json.loads(git(self.remote, 'show', 'live-data:snapshot.json'))
        self.assertEqual(snap['positions'][0]['stop'], 80.18402)
        self.assertEqual(git(self.remote, 'log', '-1', '--format=%s', 'live-data').strip(), ls.COMMIT_MESSAGE)
        # Same data again: nothing to commit.
        code, out, _ = self.run_main()
        self.assertEqual((code, out.split(':')[0]), (0, 'unchanged'))
        self.assertEqual(git(self.remote, 'rev-list', '--count', 'live-data').strip(), '1')
        # New data: one more commit, fast-forward.
        code, out, _ = self.run_main(sample_summary(updated_at='2026-11-15T15:02:46Z', equity=130.0))
        self.assertTrue(out.startswith('pushed'))
        self.assertEqual(git(self.remote, 'rev-list', '--count', 'live-data').strip(), '2')
        self.assertEqual(git(self.remote, 'rev-parse', 'master').strip(), self.master)

    def test_secret_blocks_publish(self):
        bad = sample_summary(updated_at='2026-11-15T14:02:46Z')
        with mock.patch.object(ls, 'render', side_effect=lambda o: json.dumps(o) + FAKE_SECRET):
            code, out, err = self.run_main(bad)
        self.assertEqual(code, 2)
        self.assertNotIn(FAKE_SECRET, out + err)
        self.assertNotEqual(subprocess.run(['git', '-C', self.remote, 'rev-parse', '--verify', 'live-data'],
                                           capture_output=True).returncode, 0)

    def test_server_error_is_reported_without_details(self):
        fake = subprocess.CompletedProcess([], 255, b'', ('Permission denied ' + FAKE_SECRET).encode())
        err = io.StringIO()
        with mock.patch.object(ls.subprocess, 'run', return_value=fake), redirect_stderr(err):
            code = ls.main(['--repo', self.remote, '--workdir', self.work])
        self.assertEqual(code, 1)
        self.assertIn('ssh 读取失败', err.getvalue())
        self.assertNotIn(FAKE_SECRET, err.getvalue())


if __name__ == '__main__':
    unittest.main()

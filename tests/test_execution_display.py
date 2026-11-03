import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'tools'))
import okx_demo_trader as odt
from volume_test import profile, TEST_UNTIL
from test_okx_demo_trader import FakeOKX


def report():
    def fill(order_id, price, timestamp, fee, pnl='0'):
        return dict(state='filled', ordId=order_id, avgPx=price, fillTime=timestamp,
                    accFillSz='.01', lever='1', fee=fee, pnl=pnl)
    return dict(demo=True, instrument='BTC-USDT-SWAP', stop='77097.64',
                entry=fill('entry1', '77876.4', '1793742665902', '-.00389382'),
                exit=fill('exit1', '77876.31', '1793742666429', '-.0038938155', '-.000009'))


class ExecutionDisplayTests(unittest.TestCase):
    def test_confirmed_fills_are_sorted_deduplicated_and_counted_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            for name in ['smoke-test-original.json', 'smoke-test-copy.json']:
                (out / name).write_text(json.dumps(report()))
            old = dict(ts='2026-11-03T20:00:00Z', inst='XRP-USDT-SWAP', action='open')
            rows, closed = odt.dashboard_actions(out, [old])
            self.assertEqual([a['action'] for a in rows], ['close', 'open', 'open'])
            self.assertEqual(closed, 1)
            self.assertEqual(rows[0]['ts'], '2026-11-03T21:51:06.429Z')
            self.assertAlmostEqual(rows[0]['net_pnl'], -.0077966355)
            self.assertTrue(rows[0]['execution_test'])
            self.assertEqual(rows[-1], old)

    def test_open_fill_without_exit_is_not_counted_as_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            data = report(); data.pop('exit')
            (Path(tmp) / 'smoke-test-partial.json').write_text(json.dumps(data))
            rows, closed = odt.dashboard_actions(Path(tmp), [])
            self.assertEqual(len(rows), 1); self.assertEqual(closed, 0)

    def test_snapshot_refreshes_published_feed_without_exchange_writes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); out = root / 'okx_demo'; out.mkdir()
            (root / 'combined').mkdir(); (root / 'combined/state.json').write_text('{"positions":{}}')
            (out / 'state.json').write_text(json.dumps(dict(odt.new_state(), paused=False)))
            (out / 'smoke-test-one.json').write_text(json.dumps(report()))
            fake = FakeOKX()
            with patch.object(sys, 'argv', ['demo', '--data', tmp, '--action', 'snapshot']), patch.dict(odt.os.environ, {'OKX_API_KEY':'fake','OKX_API_SECRET':'fake','OKX_API_PASSPHRASE':'fake'}), patch.object(odt, 'OKX', return_value=fake), patch.object(odt, 'configure_volume_test', return_value=profile(TEST_UNTIL)):
                odt.main()
            summary = json.loads((out / 'summary.json').read_text())
            self.assertEqual(fake.posts, [])
            self.assertEqual(summary['equity'], 5000)
            self.assertEqual(summary['closed_trades'], 1)
            self.assertEqual(summary['execution_test_trades'], 1)
            self.assertEqual(len(summary['recent_actions']), 2)
            self.assertEqual((out / 'actions.jsonl').read_text(), '')


if __name__ == '__main__':
    unittest.main()

import copy
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'tools'))
import okx_demo_smoke as smoke
from okx_demo_trader import OKXError


class Exchange:
    def __init__(self, hedged=False):
        self.hedged, self.orders, self.positions, self.posts = hedged, {}, [], []
        self.minimum = '.01'
        self.pending = []
        self.timeout_entry = False
        self.fail_exit = False

    def request(self, method, path, body=None):
        side = 'long' if self.hedged else 'net'
        if method == 'POST':
            self.posts.append((path, copy.deepcopy(body)))
            if path.endswith('set-leverage'):
                return dict(code='0', data=[dict(sCode='0')])
            if body['side'] == 'sell' and self.fail_exit:
                raise OKXError('exit network timeout')
            o = dict(state='filled', accFillSz=body['sz'], avgPx='70000', posSide=side, ordId=str(len(self.orders)+1))
            self.orders[body['clOrdId']] = o
            self.positions = [dict(instId=smoke.INST, posSide=side, pos=body['sz'])] if body['side'] == 'buy' else []
            if body['side'] == 'buy' and self.timeout_entry:
                raise OKXError('ambiguous response after fill')
            return dict(code='0', data=[dict(sCode='0')])
        if path.startswith('/api/v5/trade/order?'):
            oid = path.split('clOrdId=')[1]
            return dict(code='0', data=[self.orders[oid]]) if oid in self.orders else dict(code='51603', data=[])
        data = ([dict(acctLv='2', perm='read_only,trade', posMode='long_short_mode' if self.hedged else 'net_mode')] if '/account/config' in path
                else self.positions if '/account/positions' in path
                else self.pending if '/orders-pending' in path
                else [] if '/orders-algo-pending' in path
                else [dict(ctVal='.01', minSz=self.minimum, lotSz='.01', tickSz='.1', settleCcy='USDT', ctType='linear', state='live')] if '/public/instruments' in path
                else [dict(last='70000')] if '/market/ticker' in path
                else [dict(details=[dict(ccy='USDT', availBal='4800')])] if '/account/balance' in path else None)
        assert data is not None, path
        return dict(code='0', data=data)


class SmokeTest(unittest.TestCase):
    def execute(self, c):
        self.reports = []
        return smoke.once(c, lambda r: self.reports.append(copy.deepcopy(r)), wait=lambda _: None)

    def test_small_entry_and_reduce_only_exit_are_filled(self):
        c = Exchange(); result = self.execute(c)
        self.assertEqual(result['status'], 'completed')
        self.assertEqual(result['estimated_notional_usdt'], '7')
        self.assertEqual(len(c.posts), 3)
        self.assertEqual(c.posts[0][1]['lever'], '1')
        self.assertEqual(c.posts[1][1]['sz'], '0.01')
        self.assertEqual(c.posts[1][1]['attachAlgoOrds'][0]['slTriggerPx'], '69300')
        self.assertTrue(c.posts[2][1]['reduceOnly'])
        self.assertEqual(c.positions, [])

    def test_hedged_account_closes_only_long_side(self):
        c = Exchange(hedged=True); self.execute(c)
        for _, body in c.posts:
            self.assertEqual(body['posSide'], 'long')
        self.assertNotIn('reduceOnly', c.posts[-1][1])

    def test_rerun_cannot_repeat_completed_entry(self):
        c = Exchange(); self.execute(c); c.posts.clear()
        self.assertEqual(self.execute(c)['status'], 'completed')
        self.assertEqual(c.posts, [])

    def test_existing_btc_position_or_orders_are_untouched(self):
        for existing in ['position', 'order']:
            c = Exchange()
            if existing == 'position': c.positions = [dict(pos='1')]
            else: c.pending = [dict(ordId='manual')]
            with self.assertRaises(OKXError): self.execute(c)
            self.assertEqual(c.posts, [])

    def test_minimum_larger_than_ceiling_prevents_orders(self):
        c = Exchange(); c.minimum = '.1'
        with self.assertRaises(OKXError): self.execute(c)
        self.assertEqual(c.posts, [])

    def test_entry_timeout_is_reconciled_without_resubmission(self):
        c = Exchange(); c.timeout_entry = True
        self.assertEqual(self.execute(c)['status'], 'completed')
        self.assertEqual(sum(b.get('side') == 'buy' for _, b in c.posts), 1)

    def test_failed_exit_retains_report_and_retry_only_exits(self):
        c = Exchange(); c.fail_exit = True
        with self.assertRaises(OKXError): self.execute(c)
        self.assertEqual(self.reports[-1]['status'], 'exit_submitting')
        c.fail_exit = False
        self.assertEqual(self.execute(c)['status'], 'completed')
        self.assertEqual(sum(b.get('side') == 'buy' for _, b in c.posts), 1)

    def test_script_configures_single_attempt_client(self):
        with patch.object(smoke, 'OKX') as client, patch.object(smoke, 'once'), patch.object(sys, 'argv', ['smoke', '--output', '/tmp/smoke-test.json']), patch.dict(smoke.os.environ, {'OKX_API_KEY':'fake', 'OKX_API_SECRET':'fake', 'OKX_API_PASSPHRASE':'fake'}):
            smoke.main()
        self.assertEqual(client.call_args.kwargs['attempts'], 1)


if __name__ == '__main__':
    unittest.main()

import base64
import hashlib
import hmac
import io
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'tools'))
import okx_demo_check as chk


def responses(cfg, bal=None):
    def get(path):
        return cfg if 'config' in path else (bal or {'code': '0', 'data': [{'details': [{'ccy': 'USDT', 'eq': '5000', 'availBal': '5000'}]}]})
    return get


class OkxDemoCheckTests(unittest.TestCase):
    def test_valid_trade_key_passes_and_reports_balance(self):
        ok, lines = chk.check(responses({'code': '0', 'data': [{'perm': 'read_only,trade', 'acctLv': '2', 'posMode': 'net_mode'}]}))
        self.assertTrue(ok)
        text = '\n'.join(lines)
        self.assertIn('密钥有效', text); self.assertIn('5000', text); self.assertIn('合约模式', text)

    def test_withdraw_permission_or_missing_trade_fails(self):
        ok, lines = chk.check(responses({'code': '0', 'data': [{'perm': 'read_only,trade,withdraw'}]}))
        self.assertFalse(ok); self.assertIn('提现权限', '\n'.join(lines))
        ok, lines = chk.check(responses({'code': '0', 'data': [{'perm': 'read_only'}]}))
        self.assertFalse(ok); self.assertIn('没有交易权限', '\n'.join(lines))

    def test_rejected_key_explains_common_causes(self):
        ok, lines = chk.check(responses({'code': '50119', 'msg': "API key doesn't exist"}))
        self.assertFalse(ok); self.assertIn('模拟交易', '\n'.join(lines))

    def test_request_is_signed_for_demo_trading_and_secrets_stay_off_the_command_line(self):
        fake = Path(tempfile.mkdtemp()) / 'curl'
        fake.write_text('#!/bin/sh\necho "$@" > "$(dirname "$0")/args"\ncat > "$(dirname "$0")/stdin"\necho \'{"code":"0","data":[]}\'\n')
        fake.chmod(0o755)
        chk.signed_get('/api/v5/account/config', 'K', 'S3CRET', 'PASS', curl=str(fake))
        args, cfg = (fake.parent / 'args').read_text(), (fake.parent / 'stdin').read_text()
        for secret in ('S3CRET', 'PASS', 'K"'):
            self.assertNotIn(secret, args)
        self.assertIn('x-simulated-trading: 1', cfg); self.assertIn('OK-ACCESS-PASSPHRASE: PASS', cfg)
        ts = cfg.split('OK-ACCESS-TIMESTAMP: ')[1].split('"')[0]
        sign = base64.b64encode(hmac.new(b'S3CRET', f'{ts}GET/api/v5/account/config'.encode(), hashlib.sha256).digest()).decode()
        self.assertIn(f'OK-ACCESS-SIGN: {sign}', cfg); self.assertNotIn('S3CRET', cfg)

    def test_missing_secret_exits_with_guidance(self):
        with mock.patch.dict(os.environ, {}, clear=True), self.assertRaises(SystemExit) as e, redirect_stdout(io.StringIO()):
            chk.main()
        self.assertIn('OKX_API_KEY', str(e.exception.code))


if __name__ == '__main__':
    unittest.main()

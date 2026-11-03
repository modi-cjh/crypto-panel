#!/usr/bin/env python3
"""Read-only connection check for an OKX *demo trading* API key. It never places orders.

Reads OKX_API_KEY / OKX_API_SECRET / OKX_API_PASSPHRASE from the environment (GitHub Actions secrets),
signs requests with the demo-trading header, and reports:
  - whether the key is accepted by the demo environment,
  - the key's permissions, failing if withdrawal is enabled or trading is missing,
  - account mode and USDT balance.
Secrets are passed to curl through stdin (a config file), never on the command line, and are never printed.
"""
import base64
import hashlib
import hmac
import json
import os
import subprocess
import sys
from datetime import datetime, timezone

BASE = 'https://www.okx.com'
ACCT_MODES = {'1': '现货模式', '2': '合约模式（单币种保证金）', '3': '跨币种保证金', '4': '组合保证金'}


def env(name):
    value = os.environ.get(name, '').strip()
    if not value:
        sys.exit(f'缺少 {name}：请在仓库 Settings → Secrets and variables → Actions 中添加。')
    return value


def signed_get(path, key, secret, passphrase, curl='curl'):
    ts = datetime.now(timezone.utc).isoformat(timespec='milliseconds').replace('+00:00', 'Z')
    sign = base64.b64encode(hmac.new(secret.encode(), f'{ts}GET{path}'.encode(), hashlib.sha256).digest()).decode()
    headers = {'OK-ACCESS-KEY': key, 'OK-ACCESS-SIGN': sign, 'OK-ACCESS-TIMESTAMP': ts,
               'OK-ACCESS-PASSPHRASE': passphrase, 'x-simulated-trading': '1', 'Content-Type': 'application/json'}
    config = ''.join(f'header = "{k}: {v}"\n' for k, v in headers.items()) + f'url = "{BASE}{path}"\n'
    out = subprocess.run([curl, '-sS', '-m', '20', '-K', '-'], input=config, capture_output=True, text=True)
    if out.returncode != 0:
        raise RuntimeError(f'网络请求失败（curl 退出码 {out.returncode}）')
    try:
        return json.loads(out.stdout)
    except ValueError:
        raise RuntimeError('OKX 返回的不是 JSON（可能被地区限制或网络拦截）') from None


def check(get):
    """get(path) -> parsed JSON. Returns (ok, lines)."""
    lines, ok = [], True
    cfg = get('/api/v5/account/config')
    if cfg.get('code') != '0':
        return False, [f"❌ 密钥未被 OKX 模拟交易接受：code {cfg.get('code')} · {cfg.get('msg', '')}",
                       '   常见原因：密钥是在真实账户下创建的（需在“模拟交易”模式下创建）、Passphrase 不对、或 IP 白名单不包含 GitHub。']
    c = (cfg.get('data') or [{}])[0]
    perms = {p.strip() for p in str(c.get('perm', '')).split(',') if p.strip()}
    lines.append('✅ 密钥有效，已连上 OKX 模拟交易')
    lines.append(f"   权限：{'、'.join(sorted(perms)) or '未知'}")
    if 'withdraw' in perms:
        ok = False; lines.append('❌ 密钥开启了提现权限：请在 OKX 删除此密钥，重新创建时不要勾选“提现”。')
    if 'trade' not in perms:
        ok = False; lines.append('❌ 密钥没有交易权限：模拟下单需要“交易”权限。')
    lines.append(f"   账户模式：{ACCT_MODES.get(str(c.get('acctLv')), c.get('acctLv', '未知'))} · 持仓模式：{c.get('posMode', '未知')}")
    bal = get('/api/v5/account/balance?ccy=USDT')
    if bal.get('code') == '0':
        d = (bal.get('data') or [{}])[0]
        usdt = next((x for x in d.get('details', []) if x.get('ccy') == 'USDT'), {})
        lines.append(f"   模拟账户 USDT：权益 {usdt.get('eq', '0')} · 可用 {usdt.get('availBal', '0')}")
    else:
        lines.append(f"⚠️ 读取余额失败：code {bal.get('code')} · {bal.get('msg', '')}")
    return ok, lines


def main():
    key, secret, passphrase = env('OKX_API_KEY'), env('OKX_API_SECRET'), env('OKX_API_PASSPHRASE')
    try:
        ok, lines = check(lambda path: signed_get(path, key, secret, passphrase))
    except RuntimeError as exc:
        ok, lines = False, [f'❌ {exc}']
    report = '\n'.join(lines)
    print(report)
    summary = os.environ.get('GITHUB_STEP_SUMMARY')
    if summary:
        with open(summary, 'a') as f:
            f.write('## OKX 模拟交易连接检查\n\n```\n' + report + '\n```\n')
    sys.exit(0 if ok else 1)


if __name__ == '__main__':
    main()

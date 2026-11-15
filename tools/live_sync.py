#!/usr/bin/env python3
"""Publish the OKX live account to the public dashboard (box side, standard library only).

Runs on the dev box, never on the live server: the server holds no GitHub credentials.

1. ssh to the live server and, as the service user, read okx_live/summary.json, state.json
   and the tail of actions.jsonl (read-only; nothing is written on the server).
2. Build two public files from a field whitelist only:
     snapshot.json  equity / available / day PnL / paused / halted / positions (+ stop, target, strategy)
     fills.json     recent opens / closes (+ a short list of skips and pauses)
3. Refuse to publish anything that looks like a credential (variable names, UUID, long base64 / hex tokens).
4. Commit and push them to the separate `live-data` branch (orphan data branch, never force-pushed).

Usage:
  python3 tools/live_sync.py                 # defaults below
  python3 tools/live_sync.py --dry-run       # build + check, print the snapshot, do not push
  python3 tools/live_sync.py --host root@1.2.3.4 --key ~/.ssh/other --branch live-data

Exit codes: 0 published or unchanged, 1 read / validation error, 2 secret check failed, 3 git error.
Messages never contain file contents from the server, only short reasons.
"""
import argparse
import datetime as dt
import json
import math
import os
import re
import subprocess
import sys
import tempfile

DEFAULT_HOST = 'root@139.180.146.42'
DEFAULT_KEY = '/home/box/.ssh/okx_live_sg'
DEFAULT_REMOTE_DIR = '/var/lib/modi-okx-live/okx_live'
DEFAULT_USER = 'modi-live'
DEFAULT_BRANCH = 'live-data'
DEFAULT_REPO = 'https://github.com/modi-cjh/crypto-panel.git'
DEFAULT_WORKDIR = os.path.expanduser('~/.cache/modi-live-sync/repo')
COMMIT_MESSAGE = 'live: sync snapshot'

ACTIONS_TAIL = 3000          # lines of actions.jsonl read from the server
MAX_FILLS = 50               # opens / closes in fills.json
MAX_EVENTS = 30              # skips / pauses / errors in fills.json
MAX_HISTORY = 720            # equity curve points
TRADE_ACTIONS = {'open', 'close', 'closed_on_exchange', 'restore_stop', 'amend_stop', 'daily_halt', 'flatten'}
SECRET_NAMES = ('OKX_API_KEY', 'OKX_API_SECRET', 'OKX_API_PASSPHRASE')

INST_RE = re.compile(r'^[A-Z0-9]{1,20}-USDT-SWAP$')
WORD_RE = re.compile(r'^[a-z][a-z0-9_]{0,39}$')               # action / reason / outcome / strategy codes
CHAMPION_RE = re.compile(r'^[A-Za-z0-9_.\-]{1,15}$')            # e.g. s15653 (kept short: well under the token limit)
STAMP_RE = re.compile(r'^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?Z$')
# A credential: OKX API keys are UUIDs, secrets 32 hex chars, others base64. Any run of >= 16 base64 / hex characters
# is refused, except pure digit runs (millisecond timestamps) and lower-case snake_case codes (reason words such as
# paper_entry_too_old), which are the only long runs the whitelisted fields can produce.
TOKEN_RE = re.compile(r'[A-Za-z0-9+/=_]{16,}')
HARMLESS_RE = re.compile(r'^(\d+|[a-z_]+)$')
UUID_RE = re.compile(r'[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}')


class SyncError(Exception):
    def __init__(self, message, code=1):
        super().__init__(message)
        self.code = code


# ---- field cleaning ---------------------------------------------------------------------------
def number(value, digits=10):
    """Finite float rounded to `digits` significant digits (drops 0.1+0.2 tails), or None."""
    if isinstance(value, bool) or value is None or value == '':
        return None
    try:
        x = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(x):
        return None
    if x == 0:
        return 0.0
    return round(x, digits - 1 - int(math.floor(math.log10(abs(x)))))


def money(value):
    x = number(value)
    return None if x is None else round(x, 6)


def word(value):
    return value if isinstance(value, str) and WORD_RE.match(value) else None


def inst(value):
    return value if isinstance(value, str) and INST_RE.match(value) else None


def stamp(value):
    """ISO UTC timestamp from an ISO string or epoch milliseconds."""
    if isinstance(value, str) and STAMP_RE.match(value):
        return value
    ms = number(value, 15)
    if ms is not None and 1e12 < ms < 1e14:
        return dt.datetime.fromtimestamp(ms / 1000, dt.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
    return None


def side_of(value):
    if value in (1, 'long', 'buy'):
        return 'long'
    if value in (-1, 'short', 'sell'):
        return 'short'
    return None


def clean(row):
    """Drop None values so the public files only carry fields that exist."""
    return {k: v for k, v in row.items() if v is not None}


# ---- assembly ---------------------------------------------------------------------------------
def open_mirrors(state):
    """inst|side -> the program's own open mirror (stop, target, strategy)."""
    out = {}
    for m in (state.get('mirrored') or {}).values():
        if not isinstance(m, dict) or m.get('status') != 'open':
            continue
        key = (inst(m.get('inst')), side_of(m.get('side')) or side_of(m.get('posSide')))
        if all(key):
            out[key] = m
    return out


def build_snapshot(summary, state):
    if not isinstance(summary, dict) or not isinstance(state, dict):
        raise SyncError('summary.json / state.json 不是 JSON 对象')
    if summary.get('trading_mode') != 'live' or state.get('mode') != 'live':
        raise SyncError('数据不是实盘（trading_mode / mode ≠ live），拒绝发布')
    updated = stamp(summary.get('updated_at'))
    equity = money(summary.get('equity'))
    if not updated or equity is None:
        raise SyncError('summary.json 缺少 updated_at 或 equity')
    day_start = money(summary.get('day_start'))
    day_pnl = None if day_start is None or day_start <= 0 else money(equity - day_start)
    day_pct = None if day_pnl is None else number(day_pnl / day_start * 100, 6)
    start = money(summary.get('start_equity'))
    mirrors = open_mirrors(state)
    positions = []
    for p in summary.get('positions') or []:
        if not isinstance(p, dict) or not inst(p.get('inst')):
            continue
        side = side_of(p.get('side'))
        m = mirrors.get((p['inst'], side)) if p.get('ours') else None
        positions.append(clean({
            'inst': p['inst'], 'side': side,
            'contracts': number(p.get('contracts')), 'lever': number(p.get('lever')),
            'mgnMode': p.get('mgnMode') if p.get('mgnMode') in ('cross', 'isolated') else None,
            'avgPx': number(p.get('avgPx')), 'markPx': number(p.get('markPx')),
            'upl': money(p.get('upl')), 'uplRatio': number(p.get('uplRatio'), 6),
            'margin': money(p.get('margin')), 'ctVal': number(p.get('ctVal')),
            'ours': bool(p.get('ours')),
            'stop': number(m.get('stop'), 7) if m else None, 'target': number(m.get('target'), 7) if m else None,
            'strategy': word(m.get('strategy')) if m else None,
            'champion': m.get('champion') if m and isinstance(m.get('champion'), str) and CHAMPION_RE.match(m['champion']) else None,
            'leader': bool(m.get('leader')) if m else None,
            'opened_at': stamp(m.get('opened_at')) if m else None,
        }))
    history = []
    for point in (summary.get('history') or [])[-MAX_HISTORY:]:
        if isinstance(point, (list, tuple)) and len(point) >= 2:
            t, e = number(point[0], 15), money(point[1])
            if t is not None and e is not None and 1e12 < t < 1e14:
                history.append([int(t), e])
    return clean({
        'schema_version': 1, 'source': 'okx-live', 'trading_mode': 'live',
        'updated_at': updated, 'equity': equity, 'available': money(summary.get('available')),
        'day_start': day_start, 'day_pnl': day_pnl, 'day_pnl_pct': day_pct,
        'start_equity': start, 'total_pnl': None if start is None else money(equity - start),
        'paused': bool(summary.get('paused')), 'halted': bool(summary.get('halted')),
        'dry_run': bool(summary.get('dry_run')),
        'closed_trades': int(summary['closed_trades']) if isinstance(summary.get('closed_trades'), int) else None,
        'positions': positions, 'history': history,
    })


def fill_row(a, extra=None):
    """Whitelisted copy of one action. `extra` (from summary.recent_actions) adds realised PnL to closes."""
    e = extra or {}
    return clean({
        'ts': stamp(a.get('ts')), 'action': word(a.get('action')), 'inst': inst(a.get('inst')),
        'side': side_of(a.get('side')), 'contracts': number(a.get('contracts')),
        'price': number(a.get('price')), 'stop': number(a.get('stop')), 'target': number(a.get('target')),
        'leverage': number(a.get('leverage')), 'reason': word(a.get('reason')),
        'strategy': word(a.get('strategy')),
        'close_price': number(a.get('close_price', e.get('close_price'))),
        'pnl': money(a.get('pnl', e.get('pnl'))), 'outcome': word(a.get('outcome', e.get('outcome'))),
        'dry_run': bool(a.get('dry_run')),
    })


def parse_actions(text):
    rows = []
    for line in (text or '').splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            a = json.loads(line)
        except ValueError:
            continue                      # a half-written last line is skipped, not fatal
        if isinstance(a, dict):
            rows.append(a)
    return rows


def build_fills(actions, summary=None):
    recent = {}
    for a in (summary or {}).get('recent_actions') or []:
        if isinstance(a, dict):
            recent[(a.get('ts'), a.get('action'), a.get('inst'))] = a
    fills, events = [], []
    for a in reversed(actions):          # newest first
        row = fill_row(a, recent.get((a.get('ts'), a.get('action'), a.get('inst'))))
        if not row.get('ts') or not row.get('action'):
            continue
        if row['action'] in TRADE_ACTIONS:
            if len(fills) < MAX_FILLS:
                fills.append(row)
        elif len(events) < MAX_EVENTS:
            events.append(row)
        if len(fills) >= MAX_FILLS and len(events) >= MAX_EVENTS:
            break
    return {
        'schema_version': 1, 'source': 'okx-live',
        'updated_at': stamp((summary or {}).get('updated_at')) or (fills[0]['ts'] if fills else None),
        'fills': fills, 'events': events,
    }


# ---- secret check -----------------------------------------------------------------------------
def secret_problems(text, env=os.environ):
    """Reasons the text must not be published (never includes the matching text itself)."""
    problems = []
    for name in SECRET_NAMES:
        if name in text:
            problems.append(f'包含变量名 {name}')
        value = env.get(name)
        if value and len(value) >= 6 and value in text:
            problems.append(f'包含 {name} 的值')
    if UUID_RE.search(text):
        problems.append('包含 UUID 形式的字符串（疑似 API Key）')
    for token in TOKEN_RE.findall(text):
        if not HARMLESS_RE.match(token):
            problems.append(f'包含长度 {len(token)} 的连续 base64 / hex 串（疑似密钥）')
            break
    return problems


def render(obj):
    return json.dumps(obj, ensure_ascii=False, indent=1, sort_keys=False) + '\n'


def checked(obj, name):
    text = render(obj)
    problems = secret_problems(text)
    if problems:
        raise SyncError(f'{name} 未通过密钥检查：{"；".join(problems)}', 2)
    return text


# ---- server read ------------------------------------------------------------------------------
def read_server(host, key, remote_dir, user, timeout=60):
    """summary, state (dicts) and the actions.jsonl tail (text) in one read-only ssh call."""
    if not re.match(r'^[A-Za-z0-9_./-]+$', remote_dir) or not re.match(r'^[a-z_][a-z0-9_-]*$', user):
        raise SyncError('remote_dir / user 含非法字符')
    script = (f"cd {remote_dir} && sudo -n -u {user} cat summary.json && printf '\\0' && "
              f"sudo -n -u {user} cat state.json && printf '\\0' && "
              f"(sudo -n -u {user} tail -n {ACTIONS_TAIL} actions.jsonl 2>/dev/null || true)")
    cmd = ['ssh', '-i', key, '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=15',
           '-o', 'StrictHostKeyChecking=accept-new', host, script]
    try:
        out = subprocess.run(cmd, capture_output=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise SyncError(f'ssh 读取超时（{timeout} 秒）')
    except OSError as e:
        raise SyncError(f'无法运行 ssh：{e.strerror}')
    if out.returncode != 0:
        # Server stderr is short ("Permission denied", "No such file"); still run it through the secret check.
        msg = out.stderr.decode('utf-8', 'replace').strip().splitlines()
        msg = msg[-1][:160] if msg else ''
        if secret_problems(msg):
            msg = '（错误信息已隐藏）'
        raise SyncError(f'ssh 读取失败，退出码 {out.returncode}：{msg}')
    parts = out.stdout.decode('utf-8', 'replace').split('\0')
    if len(parts) != 3:
        raise SyncError('服务器返回的数据不完整')
    try:
        summary, state = json.loads(parts[0]), json.loads(parts[1])
    except ValueError:
        raise SyncError('summary.json 或 state.json 不是有效 JSON')
    return summary, state, parts[2]


# ---- publish ----------------------------------------------------------------------------------
def git(workdir, *args, check=True):
    out = subprocess.run(['git', '-C', workdir, *args], capture_output=True, text=True)
    if check and out.returncode != 0:
        raise SyncError(f'git {args[0]} 失败（退出码 {out.returncode}）', 3)
    return out


def prepare_branch(workdir, repo, branch):
    """A dedicated clone that only ever holds the data branch (other worktrees are never touched)."""
    os.makedirs(workdir, exist_ok=True)
    if not os.path.isdir(os.path.join(workdir, '.git')):
        git(workdir, 'init', '-q')
        git(workdir, 'remote', 'add', 'origin', repo)
    else:
        git(workdir, 'remote', 'set-url', 'origin', repo)
    exists = git(workdir, 'ls-remote', '--exit-code', '--heads', 'origin', branch, check=False)
    if exists.returncode == 0:
        git(workdir, 'fetch', '-q', '--depth', '50', 'origin', branch)
        git(workdir, 'checkout', '-q', '-B', branch, 'FETCH_HEAD')
        git(workdir, 'reset', '-q', '--hard', 'FETCH_HEAD')
        git(workdir, 'clean', '-qfdx')
        return True
    if exists.returncode != 2:
        raise SyncError('无法访问远程仓库（ls-remote 失败）', 3)
    git(workdir, 'checkout', '-q', '--orphan', branch)
    git(workdir, 'rm', '-rqf', '--ignore-unmatch', '.')
    return False


def publish(files, workdir, repo, branch):
    """Write files to the data branch; commit and push only when they changed. Returns a status word."""
    for attempt in range(2):
        existed = prepare_branch(workdir, repo, branch)
        for name, text in files.items():
            with open(os.path.join(workdir, name), 'w', encoding='utf-8') as f:
                f.write(text)
        if not existed:
            with open(os.path.join(workdir, 'README.md'), 'w', encoding='utf-8') as f:
                f.write('# live-data\n\nOKX 实盘看板数据（由 `tools/live_sync.py` 在本地机器上生成并推送，只含白名单字段，无密钥）。请勿手动修改。\n')
        git(workdir, 'add', '-A')
        if not git(workdir, 'status', '--porcelain').stdout.strip():
            return 'unchanged'
        git(workdir, '-c', 'user.name=MODI live sync', '-c', 'user.email=modi-live-sync@users.noreply.github.com',
            'commit', '-q', '-m', COMMIT_MESSAGE)
        if git(workdir, 'push', '-q', 'origin', f'HEAD:refs/heads/{branch}', check=False).returncode == 0:
            return 'pushed'
        # Someone else pushed first (another sync run): start again from the new remote head.
    raise SyncError('git push 失败（已重试一次）', 3)


def main(argv=None):
    ap = argparse.ArgumentParser(description='同步 OKX 实盘账户到 live-data 分支（只读服务器，白名单字段，无密钥）')
    ap.add_argument('--host', default=DEFAULT_HOST, help=f'ssh 目标（默认 {DEFAULT_HOST}）')
    ap.add_argument('--key', default=DEFAULT_KEY, help=f'ssh 私钥（默认 {DEFAULT_KEY}）')
    ap.add_argument('--remote-dir', default=DEFAULT_REMOTE_DIR, help='服务器上的 okx_live 目录')
    ap.add_argument('--remote-user', default=DEFAULT_USER, help='服务器上读取文件的系统用户')
    ap.add_argument('--branch', default=DEFAULT_BRANCH, help=f'数据分支（默认 {DEFAULT_BRANCH}）')
    ap.add_argument('--repo', default=DEFAULT_REPO, help='推送的仓库地址（使用本机 git 凭据）')
    ap.add_argument('--workdir', default=DEFAULT_WORKDIR, help='数据分支专用克隆目录')
    ap.add_argument('--dry-run', action='store_true', help='只生成并检查，打印 snapshot.json，不推送')
    args = ap.parse_args(argv)
    try:
        summary, state, actions_text = read_server(args.host, args.key, args.remote_dir, args.remote_user)
        snapshot = build_snapshot(summary, state)
        fills = build_fills(parse_actions(actions_text), summary)
        files = {'snapshot.json': checked(snapshot, 'snapshot.json'), 'fills.json': checked(fills, 'fills.json')}
        if args.dry_run:
            sys.stdout.write(files['snapshot.json'])
            print(f'dry-run：{len(snapshot["positions"])} 个持仓，{len(fills["fills"])} 条成交，未推送')
            return 0
        status = publish(files, args.workdir, args.repo, args.branch)
        print(f'{status}: {args.branch} · updated_at {snapshot["updated_at"]} · 权益 {snapshot["equity"]} · '
              f'{len(snapshot["positions"])} 个持仓 · {len(fills["fills"])} 条成交')
        return 0
    except SyncError as e:
        print(f'live_sync 失败：{e}', file=sys.stderr)
        return e.code


if __name__ == '__main__':
    sys.exit(main())
